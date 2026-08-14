"""Discovery of candidate feeds for the configured interests.

The pipeline is deliberately linear: the AI turns an interest into web search
queries, the shared ``web_search`` tool runs them, every promising site is
probed for a feed, and each feed the user is not subscribed to yet is scored by
one AI call. Everything the user already follows is filtered out *before* the
scoring call, so discovery never spends tokens re-recommending known sources.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import statistics
from typing import Any, List, Optional, Sequence
from urllib.parse import urljoin, urlsplit

import feedparser
import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field, ValidationError
from rich.console import Console

from ..ai.client import AIClient
from ..ai.utils import parse_json_response
from ..console_icons import get_icons
from ..models import Config, DiscoveryConfig
from ..processing.tools import WebSearchTool
from ..url_security import UnsafeURLError, safe_request

logger = logging.getLogger(__name__)

# Walled gardens and aggregators: a "source" pointing there is never a feed the
# user can subscribe to through Horizon's RSS scraper.
SKIPPED_HOSTS = frozenset(
    {
        "amazon.com",
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "medium.com",
        "pinterest.com",
        "quora.com",
        "reddit.com",
        "tiktok.com",
        "twitter.com",
        "x.com",
        "youtube.com",
    }
)

# Probed in order when a page advertises no feed through <link rel="alternate">.
COMMON_FEED_PATHS = ("/feed", "/feed.xml", "/rss", "/rss.xml", "/atom.xml", "/index.xml")

FEED_LINK_TYPES = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/feed+json"}
)

# Cap on the response text handed to the HTML/feed parsers.
MAX_RESPONSE_CHARS = 500_000


def _hostname(url: str) -> str:
    """Return the lowercase hostname of a URL without its ``www.`` prefix."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _url_key(url: str) -> tuple[str, str]:
    """Return a conservative identity for a feed or site URL."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ("", url)
    path = parsed.path.rstrip("/") or "/"
    return (_hostname(url), path)


def _path_parts(url: str) -> list[str]:
    try:
        return [part for part in urlsplit(url).path.split("/") if part]
    except ValueError:
        return []


@dataclass
class ExistingSources:
    """Everything the user already subscribes to, in comparable form."""

    feed_keys: set[tuple[str, str]] = field(default_factory=set)
    feed_hosts: set[str] = field(default_factory=set)
    subreddits: set[str] = field(default_factory=set)
    github_repos: set[str] = field(default_factory=set)
    twitter_users: set[str] = field(default_factory=set)
    telegram_channels: set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return (
            len(self.feed_keys)
            + len(self.subreddits)
            + len(self.github_repos)
            + len(self.twitter_users)
            + len(self.telegram_channels)
        )

    def covers(self, *urls: str) -> bool:
        """Return whether any of the given URLs is already subscribed to."""
        for url in urls:
            if not url:
                continue
            host = _hostname(url)
            if not host:
                continue
            if _url_key(url) in self.feed_keys or host in self.feed_hosts:
                return True

            parts = _path_parts(url)
            if host.endswith("reddit.com"):
                if len(parts) >= 2 and parts[0] == "r" and parts[1].lower() in self.subreddits:
                    return True
            elif host.endswith("github.com"):
                if len(parts) >= 2 and f"{parts[0]}/{parts[1]}".lower() in self.github_repos:
                    return True
            elif host in {"twitter.com", "x.com"}:
                if parts and parts[0].lower() in self.twitter_users:
                    return True
            elif host in {"t.me", "telegram.me"}:
                if parts and parts[0].lstrip("@").lower() in self.telegram_channels:
                    return True
        return False


def collect_existing_sources(config: Config) -> ExistingSources:
    """Read every configured source into an :class:`ExistingSources` snapshot.

    Disabled entries count as subscribed: they were curated once, and
    re-recommending something the user deliberately turned off is noise.
    """
    existing = ExistingSources()
    sources = config.sources

    for feed in sources.rss:
        url = str(feed.url)
        existing.feed_keys.add(_url_key(url))
        host = _hostname(url)
        if host:
            existing.feed_hosts.add(host)

    for subreddit in sources.reddit.subreddits:
        existing.subreddits.add(subreddit.subreddit.lower())

    for repo in sources.github:
        if repo.owner and repo.repo:
            existing.github_repos.add(f"{repo.owner}/{repo.repo}".lower())

    if sources.twitter:
        for username in sources.twitter.users:
            existing.twitter_users.add(str(username).lstrip("@").lower())

    for channel in sources.telegram.channels:
        existing.telegram_channels.add(channel.channel.lstrip("@").lower())

    return existing


@dataclass
class SourceRecommendation:
    """One feed worth subscribing to, with the evidence behind the score."""

    name: str
    site_url: str
    feed_url: str
    topic: str
    quality_score: float
    reason: str
    recent_posts: List[str] = field(default_factory=list)
    update_frequency: str = "unknown"

    def to_rss_config(self) -> dict[str, Any]:
        """Return an entry ready to paste into ``sources.rss``."""
        return {
            "name": self.name,
            "url": self.feed_url,
            "enabled": True,
            "category": self.topic,
        }


class _SearchQueries(BaseModel):
    """AI-generated web search queries for one topic."""

    queries: List[str] = Field(default_factory=list)


class _QualityVerdict(BaseModel):
    """AI verdict on one candidate feed."""

    score: float = Field(ge=0, le=10)
    reason: str


QUERY_SYSTEM_PROMPT = (
    "You help build a news radar by finding publications worth subscribing to. "
    "Return only valid JSON, with no commentary."
)

SCORING_SYSTEM_PROMPT = (
    "You evaluate whether a feed deserves a place in a curated news radar. "
    "Be strict: most feeds are mediocre. Return only valid JSON, with no commentary."
)


class SourceDiscoverer:
    """Find new feeds for a set of topics, excluding known subscriptions."""

    def __init__(
        self,
        config: DiscoveryConfig,
        ai_client: AIClient,
        existing_sources: Optional[ExistingSources] = None,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        search_tool: Optional[WebSearchTool] = None,
        console: Optional[Console] = None,
        icons: Optional[dict] = None,
        language: str = "en",
    ):
        self.config = config
        self.ai_client = ai_client
        self.existing = existing_sources or ExistingSources()
        self.search_tool = search_tool or WebSearchTool()
        self.console = console or Console(stderr=True)
        self.icons = icons or get_icons()
        self.language = language
        self._owns_client = http_client is None
        self.http_client = http_client or httpx.AsyncClient(
            timeout=config.request_timeout_sec,
            follow_redirects=False,
            headers={"User-Agent": "Horizon source discovery"},
        )
        # Feeds already handled in this run, so two topics cannot both report
        # the same publication.
        self._seen: set[tuple[str, str]] = set()

    async def discover(self, topics: Sequence[str]) -> List[SourceRecommendation]:
        """Return recommendations for every topic, best score first."""
        recommendations: List[SourceRecommendation] = []

        for topic in topics:
            self.console.print(
                f"\n{self.icons['fetch']} Discovering sources for [cyan]{topic}[/cyan]"
            )
            found = await self._discover_topic(topic)
            recommendations.extend(found)
            self.console.print(
                f"  {self.icons['success']} {len(found)} source(s) kept for [cyan]{topic}[/cyan]"
            )

        recommendations.sort(key=lambda item: item.quality_score, reverse=True)
        return recommendations

    async def _discover_topic(self, topic: str) -> List[SourceRecommendation]:
        queries = await self._generate_search_queries(topic)
        candidates = await self._collect_candidates(topic, queries)

        kept: List[SourceRecommendation] = []
        for candidate in candidates[: self.config.max_candidates_per_topic]:
            if len(kept) >= self.config.max_per_topic:
                break

            recommendation = await self._evaluate_candidate(candidate, topic)
            if recommendation is None:
                continue
            if recommendation.quality_score < self.config.quality_threshold:
                self.console.print(
                    f"  {self.icons['detail']} [dim]{recommendation.name} — "
                    f"below threshold ({recommendation.quality_score:.1f})[/dim]"
                )
                continue

            kept.append(recommendation)
            self.console.print(
                f"  {self.icons['filter']} {recommendation.name} "
                f"([green]{recommendation.quality_score:.1f}[/green]) {recommendation.feed_url}"
            )

        return kept

    async def _generate_search_queries(self, topic: str) -> List[str]:
        """Ask the AI for search queries, falling back to a fixed set."""
        count = self.config.queries_per_topic
        prompt = (
            f"Generate {count} web search queries that surface high-quality publications, "
            f'blogs or newsletters about "{topic}", the kind that publish an RSS or Atom feed.\n\n'
            "Rules:\n"
            "- Each query targets independent, regularly updated publications, not one-off articles.\n"
            "- Vary the angle between queries (practitioners, research, industry news).\n"
            '- Terms like "blog", "rss feed" or "newsletter" help.\n\n'
            'Return JSON: {"queries": ["...", "..."]}'
        )

        try:
            response = await self.ai_client.complete(system=QUERY_SYSTEM_PROMPT, user=prompt)
            parsed = parse_json_response(response)
            if parsed is not None:
                queries = [
                    query.strip()
                    for query in _SearchQueries.model_validate(parsed).queries
                    if isinstance(query, str) and query.strip()
                ]
                if queries:
                    return queries[:count]
            logger.warning("Discovery query generation returned no usable JSON for %r", topic)
        except ValidationError as exc:
            logger.warning("Discovery query generation returned invalid JSON for %r: %s", topic, exc)
        except Exception as exc:
            logger.warning("Discovery query generation failed for %r: %s", topic, exc)

        return [
            f"{topic} blog rss feed",
            f"best {topic} newsletters",
            f"{topic} news site rss",
        ][:count]

    async def _collect_candidates(
        self, topic: str, queries: Sequence[str]
    ) -> List[dict[str, str]]:
        """Search the web and resolve results into unique, unsubscribed feeds."""
        candidates: List[dict[str, str]] = []

        for query in queries:
            results = await self._web_search(query)
            for result in results:
                url = result.get("url", "")
                host = _hostname(url)
                if not host or any(
                    host == skip or host.endswith(f".{skip}") for skip in SKIPPED_HOSTS
                ):
                    continue
                if _url_key(url) in self._seen:
                    continue
                if self.existing.covers(url):
                    self._seen.add(_url_key(url))
                    continue

                feed_url = await self._resolve_feed(url)
                if not feed_url:
                    self._seen.add(_url_key(url))
                    continue

                # The site URL is marked as seen only here: when the search
                # result *is* the feed, both keys are the same one.
                feed_key = _url_key(feed_url)
                already_handled = feed_key in self._seen
                self._seen.add(feed_key)
                self._seen.add(_url_key(url))
                if already_handled:
                    continue
                if self.existing.covers(feed_url):
                    self.console.print(
                        f"  {self.icons['detail']} [dim]already subscribed: {feed_url}[/dim]"
                    )
                    continue

                candidates.append(
                    {
                        "name": result.get("title") or host,
                        "site_url": url,
                        "feed_url": feed_url,
                        "topic": topic,
                    }
                )

            if len(candidates) >= self.config.max_candidates_per_topic:
                break

        return candidates

    async def _web_search(self, query: str) -> List[dict[str, str]]:
        try:
            results = await self.search_tool.execute(
                {"query": query, "max_results": self.config.search_results_per_query}
            )
        except Exception as exc:
            logger.warning("Discovery search failed for %r: %s", query, exc)
            return []
        return [
            {"title": result.get("title", ""), "url": result.get("url", "")}
            for result in results
            if result.get("url")
        ]

    async def _fetch_text(self, url: str) -> Optional[str]:
        """GET a URL through the SSRF-safe request path, or return None."""
        try:
            response = await safe_request(self.http_client, "GET", url)
            response.raise_for_status()
        except (UnsafeURLError, httpx.HTTPError) as exc:
            logger.debug("Discovery fetch failed for %s: %s", url, exc)
            return None
        except Exception as exc:
            logger.warning("Unexpected discovery fetch error for %s: %s", url, exc)
            return None
        return response.text[:MAX_RESPONSE_CHARS]

    async def _resolve_feed(self, site_url: str) -> Optional[str]:
        """Return the feed URL advertised by a page, or a probed common path."""
        html = await self._fetch_text(site_url)
        if html is None:
            return None

        if _looks_like_feed(html):
            return site_url

        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            logger.debug("Discovery could not parse HTML from %s: %s", site_url, exc)
            return None

        for link in soup.find_all("link"):
            rel = " ".join(link.get("rel") or []).lower()
            link_type = (link.get("type") or "").lower()
            href = link.get("href")
            if not href or link_type not in FEED_LINK_TYPES:
                continue
            if rel and "alternate" not in rel:
                continue
            candidate = urljoin(site_url, href)
            if await self._is_feed(candidate):
                return candidate

        parsed = urlsplit(site_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        for path in COMMON_FEED_PATHS:
            candidate = base + path
            if await self._is_feed(candidate):
                return candidate

        return None

    async def _is_feed(self, url: str) -> bool:
        text = await self._fetch_text(url)
        return bool(text) and _looks_like_feed(text)

    async def _evaluate_candidate(
        self, candidate: dict[str, str], topic: str
    ) -> Optional[SourceRecommendation]:
        """Fetch a candidate feed and turn it into a scored recommendation."""
        feed_url = candidate["feed_url"]
        content = await self._fetch_text(feed_url)
        if not content:
            return None

        parsed = feedparser.parse(content)
        entries = list(getattr(parsed, "entries", None) or [])
        if not entries:
            return None

        titles = [
            str(entry.get("title", "")).strip()
            for entry in entries[:10]
            if str(entry.get("title", "")).strip()
        ]
        if not titles:
            return None

        feed_title = str((getattr(parsed, "feed", None) or {}).get("title", "")).strip()
        name = feed_title or candidate["name"]
        frequency = _update_frequency(entries)

        score, reason = await self._score_candidate(name, topic, titles, frequency)

        return SourceRecommendation(
            name=name,
            site_url=candidate["site_url"],
            feed_url=feed_url,
            topic=topic,
            quality_score=score,
            reason=reason,
            recent_posts=titles[:5],
            update_frequency=frequency,
        )

    async def _score_candidate(
        self,
        name: str,
        topic: str,
        titles: Sequence[str],
        frequency: str,
    ) -> tuple[float, str]:
        """Return an AI quality score in 0-10 and a one-sentence rationale."""
        recent = "\n".join(f"- {title}" for title in titles[:10])
        prompt = (
            f'Rate this feed as a source for the topic "{topic}".\n\n'
            f"Feed name: {name}\n"
            f"Observed update frequency: {frequency}\n"
            f"Recent posts:\n{recent}\n\n"
            "Score four dimensions, 0 to 2.5 each, and sum them:\n"
            "1. Relevance to the topic\n"
            "2. Depth and originality of the writing\n"
            "3. Publishing regularity\n"
            "4. Authority of the publication\n\n"
            "Anything below 5.5 is not worth subscribing to; reserve 9 and above for a "
            "reference publication in the field.\n\n"
            f'Return JSON: {{"score": <0-10>, "reason": "<one sentence in {self.language}>"}}'
        )

        try:
            response = await self.ai_client.complete(system=SCORING_SYSTEM_PROMPT, user=prompt)
            parsed = parse_json_response(response)
            if parsed is not None:
                verdict = _QualityVerdict.model_validate(parsed)
                return verdict.score, verdict.reason.strip()
            logger.warning("Discovery scoring returned no usable JSON for %r", name)
        except ValidationError as exc:
            logger.warning("Discovery scoring returned an invalid verdict for %r: %s", name, exc)
        except Exception as exc:
            logger.warning("Discovery scoring failed for %r: %s", name, exc)

        # An unscored candidate is dropped rather than guessed at.
        return 0.0, "Quality could not be evaluated."

    async def aclose(self) -> None:
        """Close the HTTP client when this discoverer created it."""
        if self._owns_client:
            await self.http_client.aclose()


def _looks_like_feed(text: str) -> bool:
    """Cheap sniff test for RSS/Atom payloads before handing them to a parser."""
    head = text.lstrip()[:2000].lower()
    return any(
        marker in head
        for marker in ("<rss", "<feed", "<rdf:rdf", "jsonfeed.org/version")
    )


def _entry_datetime(entry: Any) -> Optional[datetime]:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if not parsed:
            continue
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


def _update_frequency(entries: Sequence[Any]) -> str:
    """Describe how often a feed publishes, from the dates it exposes."""
    dates = sorted(
        (date for date in (_entry_datetime(entry) for entry in entries[:20]) if date),
        reverse=True,
    )
    if len(dates) < 2:
        return "unknown"

    gaps_hours = [
        (earlier - later).total_seconds() / 3600 for earlier, later in zip(dates, dates[1:])
    ]
    median_gap = statistics.median(gaps_hours)

    if median_gap <= 8:
        return "several times a day"
    if median_gap <= 36:
        return "daily"
    if median_gap <= 24 * 10:
        return "weekly"
    if median_gap <= 24 * 45:
        return "monthly"
    return "occasional"

import asyncio
import json
from types import SimpleNamespace

import pytest

from src.discovery import discoverer as discovery_module
from src.discovery.cli import resolve_topics
from src.discovery.discoverer import (
    ExistingSources,
    SourceDiscoverer,
    SourceRecommendation,
    _update_frequency,
    collect_existing_sources,
)
from src.discovery.reporter import DiscoveryReporter
from src.models import (
    AIConfig,
    Config,
    DiscoveryConfig,
    GitHubSourceConfig,
    RedditConfig,
    RedditSubredditConfig,
    RSSSourceConfig,
    SourcesConfig,
    TelegramChannelConfig,
    TelegramConfig,
    TwitterConfig,
    WebhookConfig,
)
from src.processing.tools import WebSearchTool
from src.services.webhook import WebhookNotifier


FEED_XML = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Deep Learning Weekly</title>
    <item>
      <title>Scaling laws revisited</title>
      <pubDate>Mon, 04 Aug 2026 10:00:00 GMT</pubDate>
    </item>
    <item>
      <title>A tour of sparse attention</title>
      <pubDate>Mon, 28 Jul 2026 10:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

SITE_HTML = """<html><head>
<link rel="alternate" type="application/rss+xml" href="/feed.xml">
</head><body>hello</body></html>
"""


class FakeAIClient:
    """AI client returning canned JSON, recording the prompts it received."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def complete(self, system, user, temperature=None, max_tokens=None):
        self.calls.append({"system": system, "user": user})
        if not self.responses:
            raise AssertionError("FakeAIClient ran out of responses")
        return self.responses.pop(0)


class FakeSearchTool:
    """Stands in for the DDGS-backed web_search tool."""

    def __init__(self, results_by_query=None, default=None):
        self.results_by_query = results_by_query or {}
        self.default = default if default is not None else []
        self.arguments = []

    async def execute(self, arguments):
        self.arguments.append(arguments)
        return self.results_by_query.get(arguments["query"], self.default)


def _discoverer(monkeypatch, ai_client, *, pages, config=None, existing=None, search_tool=None):
    """Build a discoverer whose HTTP layer serves `pages` and nothing else."""

    async def fake_fetch(self, url):
        return pages.get(url)

    monkeypatch.setattr(SourceDiscoverer, "_fetch_text", fake_fetch)
    return SourceDiscoverer(
        config or DiscoveryConfig(max_per_topic=2),
        ai_client,
        existing,
        http_client=object(),  # never used: _fetch_text is stubbed
        search_tool=search_tool or FakeSearchTool(),
        console=SimpleNamespace(print=lambda *args, **kwargs: None),
        icons={"fetch": "", "success": "", "filter": "", "detail": ""},
    )


def _config(**sources) -> Config:
    return Config(
        ai=AIConfig(provider="openai", model="gpt-4o-mini", api_key_env="OPENAI_API_KEY"),
        sources=SourcesConfig(**sources),
    )


# --- existing sources -------------------------------------------------------


def test_collect_existing_sources_reads_every_source_kind():
    config = _config(
        rss=[
            RSSSourceConfig(name="Feed", url="https://example.com/feed.xml", category="ai"),
            RSSSourceConfig(name="Off", url="https://off.example/rss", enabled=False),
        ],
        reddit=RedditConfig(subreddits=[RedditSubredditConfig(subreddit="MachineLearning")]),
        github=[GitHubSourceConfig(type="repo_releases", owner="OpenAI", repo="Whisper")],
        twitter=TwitterConfig(users=["@karpathy"]),
        telegram=TelegramConfig(channels=[TelegramChannelConfig(channel="@durov")]),
    )

    existing = collect_existing_sources(config)

    assert ("example.com", "/feed.xml") in existing.feed_keys
    assert existing.subreddits == {"machinelearning"}
    assert existing.github_repos == {"openai/whisper"}
    assert existing.twitter_users == {"karpathy"}
    assert existing.telegram_channels == {"durov"}
    # A disabled feed still counts as subscribed.
    assert existing.covers("https://off.example/rss")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/feed.xml",
        "https://www.example.com/feed.xml/",  # www and trailing slash normalized
        "https://example.com/anything-else",  # same host
        "https://reddit.com/r/machinelearning/",
        "https://github.com/OpenAI/Whisper/releases",
        "https://x.com/karpathy",
        "https://t.me/durov",
    ],
)
def test_covers_matches_known_subscriptions(url):
    existing = ExistingSources(
        feed_keys={("example.com", "/feed.xml")},
        feed_hosts={"example.com"},
        subreddits={"machinelearning"},
        github_repos={"openai/whisper"},
        twitter_users={"karpathy"},
        telegram_channels={"durov"},
    )

    assert existing.covers(url)


def test_covers_ignores_unrelated_urls():
    existing = ExistingSources(feed_hosts={"example.com"}, subreddits={"machinelearning"})

    assert not existing.covers("https://other.example/feed")
    assert not existing.covers("https://reddit.com/r/python")


# --- discovery flow ---------------------------------------------------------


def test_discover_returns_scored_recommendation(monkeypatch):
    ai = FakeAIClient(
        [
            json.dumps({"queries": ["deep learning blog rss"]}),
            json.dumps({"score": 8.4, "reason": "Consistently deep technical write-ups."}),
        ]
    )
    search = FakeSearchTool(
        {"deep learning blog rss": [{"title": "DL Weekly", "url": "https://dlweekly.example/"}]}
    )
    pages = {
        "https://dlweekly.example/": SITE_HTML,
        "https://dlweekly.example/feed.xml": FEED_XML,
    }
    discoverer = _discoverer(monkeypatch, ai, pages=pages, search_tool=search)

    results = asyncio.run(discoverer.discover(["deep learning"]))

    assert len(results) == 1
    recommendation = results[0]
    assert recommendation.feed_url == "https://dlweekly.example/feed.xml"
    # The feed's own title wins over the search result title.
    assert recommendation.name == "Deep Learning Weekly"
    assert recommendation.quality_score == pytest.approx(8.4)
    assert recommendation.recent_posts[0] == "Scaling laws revisited"
    assert recommendation.update_frequency == "weekly"


def test_discover_skips_already_subscribed_sites_without_ai_scoring(monkeypatch):
    ai = FakeAIClient([json.dumps({"queries": ["deep learning blog rss"]})])
    search = FakeSearchTool(
        {"deep learning blog rss": [{"title": "DL Weekly", "url": "https://dlweekly.example/"}]}
    )
    existing = ExistingSources(feed_hosts={"dlweekly.example"})
    discoverer = _discoverer(
        monkeypatch,
        ai,
        pages={"https://dlweekly.example/": SITE_HTML},
        search_tool=search,
        existing=existing,
    )

    results = asyncio.run(discoverer.discover(["deep learning"]))

    assert results == []
    # Only the query-generation call was made: no tokens spent on a known source.
    assert len(ai.calls) == 1


def test_discover_drops_candidates_below_the_threshold(monkeypatch):
    ai = FakeAIClient(
        [
            json.dumps({"queries": ["deep learning blog rss"]}),
            json.dumps({"score": 3.0, "reason": "Mostly link roundups."}),
        ]
    )
    search = FakeSearchTool(
        {"deep learning blog rss": [{"title": "DL Weekly", "url": "https://dlweekly.example/"}]}
    )
    discoverer = _discoverer(
        monkeypatch,
        ai,
        pages={
            "https://dlweekly.example/": SITE_HTML,
            "https://dlweekly.example/feed.xml": FEED_XML,
        },
        search_tool=search,
        config=DiscoveryConfig(quality_threshold=5.5),
    )

    assert asyncio.run(discoverer.discover(["deep learning"])) == []


def test_discover_skips_social_hosts(monkeypatch):
    ai = FakeAIClient([json.dumps({"queries": ["deep learning blog rss"]})])
    search = FakeSearchTool(
        {
            "deep learning blog rss": [
                {"title": "A thread", "url": "https://twitter.com/someone/status/1"},
                {"title": "A video", "url": "https://www.youtube.com/watch?v=1"},
            ]
        }
    )
    discoverer = _discoverer(monkeypatch, ai, pages={}, search_tool=search)

    assert asyncio.run(discoverer.discover(["deep learning"])) == []


def test_the_same_feed_is_reported_once_across_topics(monkeypatch):
    ai = FakeAIClient(
        [
            json.dumps({"queries": ["q1"]}),
            json.dumps({"score": 9.0, "reason": "Excellent."}),
            json.dumps({"queries": ["q2"]}),
        ]
    )
    search = FakeSearchTool(
        default=[{"title": "DL Weekly", "url": "https://dlweekly.example/"}]
    )
    discoverer = _discoverer(
        monkeypatch,
        ai,
        pages={
            "https://dlweekly.example/": SITE_HTML,
            "https://dlweekly.example/feed.xml": FEED_XML,
        },
        search_tool=search,
    )

    results = asyncio.run(discoverer.discover(["deep learning", "machine learning"]))

    assert len(results) == 1


def test_query_generation_falls_back_when_the_ai_returns_garbage(monkeypatch):
    ai = FakeAIClient(["not json at all"])
    search = FakeSearchTool()
    discoverer = _discoverer(monkeypatch, ai, pages={}, search_tool=search)

    asyncio.run(discoverer.discover(["robotics"]))

    assert [call["query"] for call in search.arguments] == [
        "robotics blog rss feed",
        "best robotics newsletters",
        "robotics news site rss",
    ]


def test_search_requests_the_configured_number_of_results(monkeypatch):
    ai = FakeAIClient([json.dumps({"queries": ["q"]})])
    search = FakeSearchTool()
    discoverer = _discoverer(
        monkeypatch,
        ai,
        pages={},
        search_tool=search,
        config=DiscoveryConfig(search_results_per_query=25),
    )

    asyncio.run(discoverer.discover(["robotics"]))

    assert search.arguments == [{"query": "q", "max_results": 25}]


def test_unscorable_candidate_is_dropped(monkeypatch):
    ai = FakeAIClient([json.dumps({"queries": ["q"]}), "the model refused"])
    search = FakeSearchTool(default=[{"title": "DL", "url": "https://dlweekly.example/"}])
    discoverer = _discoverer(
        monkeypatch,
        ai,
        pages={
            "https://dlweekly.example/": SITE_HTML,
            "https://dlweekly.example/feed.xml": FEED_XML,
        },
        search_tool=search,
    )

    assert asyncio.run(discoverer.discover(["deep learning"])) == []


def test_feed_urls_found_directly_in_search_results_are_used(monkeypatch):
    ai = FakeAIClient(
        [json.dumps({"queries": ["q"]}), json.dumps({"score": 7.5, "reason": "Solid."})]
    )
    search = FakeSearchTool(default=[{"title": "DL", "url": "https://dlweekly.example/atom"}])
    discoverer = _discoverer(
        monkeypatch,
        ai,
        pages={"https://dlweekly.example/atom": FEED_XML},
        search_tool=search,
    )

    results = asyncio.run(discoverer.discover(["deep learning"]))

    assert [item.feed_url for item in results] == ["https://dlweekly.example/atom"]


def test_common_feed_paths_are_probed_when_the_page_advertises_none(monkeypatch):
    ai = FakeAIClient(
        [json.dumps({"queries": ["q"]}), json.dumps({"score": 7.0, "reason": "Fine."})]
    )
    search = FakeSearchTool(default=[{"title": "DL", "url": "https://plain.example/"}])
    discoverer = _discoverer(
        monkeypatch,
        ai,
        pages={
            "https://plain.example/": "<html><head></head><body>no feed link</body></html>",
            "https://plain.example/feed": FEED_XML,
        },
        search_tool=search,
    )

    results = asyncio.run(discoverer.discover(["deep learning"]))

    assert [item.feed_url for item in results] == ["https://plain.example/feed"]


# --- helpers ----------------------------------------------------------------


def test_update_frequency_reads_entry_dates():
    def entry(day):
        return {"published_parsed": (2026, 8, day, 12, 0, 0, 0, 0, 0)}

    assert _update_frequency([entry(14), entry(13), entry(12)]) == "daily"
    assert _update_frequency([entry(14), entry(7), entry(1)]) == "weekly"
    assert _update_frequency([entry(14)]) == "unknown"


def test_looks_like_feed_rejects_html():
    assert discovery_module._looks_like_feed(FEED_XML)
    assert not discovery_module._looks_like_feed(SITE_HTML)


def test_web_search_tool_clamps_the_requested_result_count():
    tool = WebSearchTool()

    assert tool._max_results({}) == WebSearchTool.DEFAULT_MAX_RESULTS
    assert tool._max_results({"max_results": "nope"}) == WebSearchTool.DEFAULT_MAX_RESULTS
    assert tool._max_results({"max_results": 0}) == 1
    assert tool._max_results({"max_results": 900}) == WebSearchTool.RESULT_LIMIT
    assert tool._max_results({"max_results": 12}) == 12


# --- topics resolution ------------------------------------------------------


def test_resolve_topics_prefers_cli_over_config():
    config = _config(rss=[RSSSourceConfig(name="F", url="https://a.example/f", category="ai")])
    config.discovery.topics = ["from-config"]

    assert resolve_topics(config, ["from-cli"]) == ["from-cli"]
    assert resolve_topics(config, None) == ["from-config"]


def test_resolve_topics_falls_back_to_rss_categories():
    config = _config(
        rss=[
            RSSSourceConfig(name="A", url="https://a.example/f", category="ai"),
            RSSSourceConfig(name="B", url="https://b.example/f", category="ai"),
            RSSSourceConfig(name="C", url="https://c.example/f", category="chips"),
            RSSSourceConfig(name="D", url="https://d.example/f"),
        ]
    )

    assert resolve_topics(config, None) == ["ai", "chips"]


def test_resolve_topics_returns_empty_when_nothing_is_configured():
    assert resolve_topics(_config(), None) == []


# --- report -----------------------------------------------------------------


def _recommendation(**overrides) -> SourceRecommendation:
    defaults = dict(
        name="Deep Learning Weekly",
        site_url="https://dlweekly.example/",
        feed_url="https://dlweekly.example/feed.xml",
        topic="deep learning",
        quality_score=8.6,
        reason="Consistently deep technical write-ups.",
        recent_posts=["Scaling laws revisited"],
        update_frequency="weekly",
    )
    defaults.update(overrides)
    return SourceRecommendation(**defaults)


def test_report_lists_sources_and_a_pastable_config_snippet():
    report = DiscoveryReporter().generate_report(
        [_recommendation()], ["deep learning"], date="2026-08-14", skipped_existing=12
    )

    assert report.startswith("---\nlayout: default\n")
    assert "## deep learning" in report
    assert "https://dlweekly.example/feed.xml" in report
    assert "Already subscribed (excluded)**: 12" in report

    snippet = report.split("```json\n")[1].split("\n```")[0]
    assert json.loads(snippet) == [
        {
            "name": "Deep Learning Weekly",
            "url": "https://dlweekly.example/feed.xml",
            "enabled": True,
            "category": "deep learning",
        }
    ]


def test_empty_report_explains_what_to_change():
    report = DiscoveryReporter().generate_report([], ["deep learning"], date="2026-08-14")

    assert "No new source cleared the quality threshold" in report
    assert "```json" not in report


def test_save_report_creates_parent_directories(tmp_path):
    path = DiscoveryReporter().save_report("# hi\n", tmp_path / "docs" / "discovered.md")

    assert path.read_text(encoding="utf-8") == "# hi\n"


# --- webhook notification ---------------------------------------------------


def test_webhook_discovery_notification_lists_the_top_sources(monkeypatch):
    monkeypatch.setenv("DISCOVERY_WEBHOOK_URL", "https://example.com/hook")
    notifier = WebhookNotifier(
        WebhookConfig(url_env="DISCOVERY_WEBHOOK_URL", enabled=True),
        console=SimpleNamespace(print=lambda *args, **kwargs: None),
    )
    sent = []

    async def capture(variables):
        sent.append(variables)

    monkeypatch.setattr(notifier, "notify", capture)

    asyncio.run(
        notifier.send_discovery_report(
            recommendations=[_recommendation()],
            topics=["deep learning"],
            date="2026-08-14",
            report_path="docs/discovered-sources.md",
        )
    )

    assert len(sent) == 1
    variables = sent[0]
    assert variables["message_kind"] == "discovery"
    assert variables["important_items"] == 1
    assert "Deep Learning Weekly" in variables["summary"]
    assert "docs/discovered-sources.md" in variables["summary"]


def test_webhook_discovery_notification_reports_an_empty_run(monkeypatch):
    monkeypatch.setenv("DISCOVERY_WEBHOOK_URL", "https://example.com/hook")
    notifier = WebhookNotifier(
        WebhookConfig(url_env="DISCOVERY_WEBHOOK_URL", enabled=True),
        console=SimpleNamespace(print=lambda *args, **kwargs: None),
    )
    sent = []

    async def capture(variables):
        sent.append(variables)

    monkeypatch.setattr(notifier, "notify", capture)

    asyncio.run(
        notifier.send_discovery_report(
            recommendations=[],
            topics=["deep learning"],
            date="2026-08-14",
            report_path="docs/discovered-sources.md",
        )
    )

    assert sent[0]["important_items"] == 0
    assert "No new source cleared the quality threshold" in sent[0]["summary"]

"""GitHub scraper implementation."""

import logging
import os
import re
from datetime import datetime
from typing import List, Optional
import httpx

from .base import BaseScraper
from ..models import ContentItem, SourceType, GitHubSourceConfig

logger = logging.getLogger(__name__)

# First "X.Y" or "X.Y.Z" in a tag: matches "v1.2.3", "n8n@2.39.9", "release-4.0".
_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_LEVEL_RANK = {"major": 0, "minor": 1, "patch": 2}


def release_level(tag: str) -> Optional[str]:
    """Return the semver bump a tag represents, or None if it has no version.

    ``X.0.0`` is a major release, ``X.Y.0`` a minor one, anything else a patch.
    Under 1.0 a minor bump is the breaking one, so ``0.Y.0`` counts as major.
    """
    match = _VERSION_RE.search(tag)
    if not match:
        return None
    major, minor, patch = (int(part or 0) for part in match.groups())
    if patch:
        return "patch"
    if minor == 0 or major == 0:
        return "major"
    return "minor"


def keeps_release(tag: str, min_level: Optional[str]) -> bool:
    """Whether a release tag passes a ``release_level`` filter."""
    if min_level is None or min_level == "patch":
        return True
    level = release_level(tag)
    return level is not None and _LEVEL_RANK[level] <= _LEVEL_RANK[min_level]


class GitHubScraper(BaseScraper):
    """Scraper for GitHub events and releases."""

    def __init__(self, sources: List[GitHubSourceConfig], http_client: httpx.AsyncClient):
        """Initialize GitHub scraper.

        Args:
            sources: List of GitHub source configurations
            http_client: Shared async HTTP client
        """
        super().__init__({"sources": sources}, http_client)
        self.token = os.getenv("GITHUB_TOKEN")
        self.base_url = "https://api.github.com"

    def _get_headers(self) -> dict:
        """Get request headers with optional authentication.

        Returns:
            dict: HTTP headers
        """
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Horizon-Aggregator"
        }
        if self.token:
            headers["Authorization"] = f"token {self.token}"
        return headers

    async def fetch(self, since: datetime) -> List[ContentItem]:
        """Fetch GitHub content items.

        Args:
            since: Only fetch items published after this time

        Returns:
            List[ContentItem]: Fetched content items
        """
        items = []
        sources = self.config["sources"]

        for source in sources:
            if not source.enabled:
                continue

            if source.type == "user_events" and source.username:
                user_items = await self._fetch_user_events(source, since)
                items.extend(user_items)
            elif source.type == "repo_releases" and source.owner and source.repo:
                release_items = await self._fetch_repo_releases(source, since)
                items.extend(release_items)

        return items

    async def _fetch_user_events(
        self,
        source: GitHubSourceConfig,
        since: datetime,
    ) -> List[ContentItem]:
        """Fetch public events for a user.

        Args:
            source: GitHub source configuration
            since: Only fetch events after this time

        Returns:
            List[ContentItem]: Event content items
        """
        url = f"{self.base_url}/users/{source.username}/events/public"
        items = []

        try:
            response = await self.client.get(url, headers=self._get_headers(), follow_redirects=True)
            response.raise_for_status()
            events = response.json()

            for event in events:
                created_at = datetime.fromisoformat(
                    event["created_at"].replace("Z", "+00:00")
                )

                if created_at < since:
                    continue

                # Filter interesting event types
                event_type = event["type"]
                if event_type not in [
                    "PushEvent", "CreateEvent", "ReleaseEvent",
                    "PublicEvent", "WatchEvent"
                ]:
                    continue

                item = self._parse_event(event, source)
                if item:
                    items.append(item)

        except httpx.HTTPError as e:
            logger.warning("Error fetching GitHub events for %s: %s", source.username, e)

        return items

    def _parse_event(self, event: dict, source: GitHubSourceConfig) -> Optional[ContentItem]:
        """Parse GitHub event into ContentItem.

        Args:
            event: GitHub event data
            username: GitHub username

        Returns:
            Optional[ContentItem]: Parsed content item or None
        """
        event_type = event["type"]
        event_id = event["id"]
        created_at = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
        username = source.username

        repo_name = event["repo"]["name"]
        repo_url = f"https://github.com/{repo_name}"

        # Generate title and content based on event type
        if event_type == "PushEvent":
            commits = event["payload"].get("commits", [])
            title = f"{username} pushed {len(commits)} commit(s) to {repo_name}"
            content = "\n".join([c.get("message", "") for c in commits[:3]])
        elif event_type == "CreateEvent":
            ref_type = event["payload"].get("ref_type", "repository")
            title = f"{username} created {ref_type} in {repo_name}"
            content = event["payload"].get("description", "")
        elif event_type == "ReleaseEvent":
            release = event["payload"].get("release", {})
            title = f"{username} released {release.get('tag_name', '')} in {repo_name}"
            content = release.get("body", "")
            repo_url = release.get("html_url", repo_url)
        elif event_type == "PublicEvent":
            title = f"{username} made {repo_name} public"
            content = ""
        elif event_type == "WatchEvent":
            title = f"{username} starred {repo_name}"
            content = ""
        else:
            return None

        return ContentItem(
            id=self._generate_id("github", "event", event_id),
            source_type=SourceType.GITHUB,
            title=title,
            url=repo_url,
            content=content,
            author=username,
            published_at=created_at,
            profile=source.profile,
            metadata={
                "event_type": event_type,
                "repo": repo_name,
                "category": source.category,
            }
        )

    async def _fetch_repo_releases(
        self,
        source: GitHubSourceConfig,
        since: datetime,
    ) -> List[ContentItem]:
        """Fetch releases for a repository.

        Args:
            source: GitHub source configuration
            since: Only fetch releases after this time

        Returns:
            List[ContentItem]: Release content items
        """
        owner, repo = source.owner, source.repo
        url = f"{self.base_url}/repos/{owner}/{repo}/releases"
        items = []

        try:
            response = await self.client.get(url, headers=self._get_headers(), follow_redirects=True)
            response.raise_for_status()
            releases = response.json()

            for release in releases:
                published_at = datetime.fromisoformat(
                    release["published_at"].replace("Z", "+00:00")
                )

                if published_at < since:
                    continue

                if release.get("prerelease") and not source.include_prereleases:
                    logger.debug(
                        "Skipping %s/%s pre-release %s", owner, repo, release["tag_name"]
                    )
                    continue

                if not keeps_release(release["tag_name"], source.release_level):
                    logger.debug(
                        "Skipping %s/%s %s below release_level=%s",
                        owner, repo, release["tag_name"], source.release_level,
                    )
                    continue

                item = ContentItem(
                    id=self._generate_id("github", "release", str(release["id"])),
                    source_type=SourceType.GITHUB,
                    title=f"{owner}/{repo} released {release['tag_name']}",
                    url=release["html_url"],
                    content=release.get("body", ""),
                    author=release["author"]["login"],
                    published_at=published_at,
                    profile=source.profile,
                    metadata={
                        "repo": f"{owner}/{repo}",
                        "tag": release["tag_name"],
                        "prerelease": release.get("prerelease", False),
                        "category": source.category,
                    }
                )
                items.append(item)

        except httpx.HTTPError as e:
            logger.warning("Error fetching releases for %s/%s: %s", owner, repo, e)

        if source.collapse_releases and len(items) > 1:
            items = [self._collapse_releases(items)]

        return items

    @staticmethod
    def _collapse_releases(items: List[ContentItem]) -> ContentItem:
        """Fold several releases of one repository into the newest of them.

        The item keeps the newest release's id, URL and date; its title lists
        the other tags and its content chains every changelog, newest first,
        so the analysis still sees what changed across the whole window.
        """
        ordered = sorted(items, key=lambda item: item.published_at, reverse=True)
        newest = ordered[0]
        tags = [item.metadata["tag"] for item in ordered]
        content = "\n\n".join(
            f"## {item.metadata['tag']}\n\n{item.content or ''}".rstrip()
            for item in ordered
        )
        return newest.model_copy(
            update={
                "title": f"{newest.title} (also {', '.join(tags[1:])})",
                "content": content,
                "metadata": {**newest.metadata, "collapsed_tags": tags},
            }
        )

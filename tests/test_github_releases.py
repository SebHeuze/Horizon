"""Release filtering and collapsing for GitHub repo_releases sources."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models import GitHubSourceConfig
from src.scrapers.github import GitHubScraper, keeps_release, release_level

_SINCE = datetime(2020, 1, 1, tzinfo=timezone.utc)
_NOW = datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("tag", "level"),
    [
        ("v44.0.0", "major"),
        ("44.0", "major"),
        ("0.5.0", "major"),
        ("44.108.0", "minor"),
        ("n8n@2.40.0", "minor"),
        ("44.108.2", "patch"),
        ("n8n@2.39.9", "patch"),
        ("stable", None),
        ("beta", None),
    ],
)
def test_release_level(tag, level):
    assert release_level(tag) == level


def test_keeps_release_thresholds():
    assert keeps_release("44.108.2", None)
    assert keeps_release("stable", "patch")
    assert keeps_release("44.108.0", "minor")
    assert not keeps_release("44.108.2", "minor")
    assert keeps_release("45.0.0", "major")
    assert not keeps_release("44.108.0", "major")
    assert not keeps_release("stable", "minor")


def _release(release_id: int, tag: str, hours_ago: int) -> dict:
    published = _NOW - timedelta(hours=hours_ago)
    return {
        "id": release_id,
        "published_at": published.isoformat().replace("+00:00", "Z"),
        "tag_name": tag,
        "html_url": f"https://github.com/acme/tool/releases/tag/{tag}",
        "body": f"notes for {tag}",
        "author": {"login": "bot"},
    }


def _fetch(source: GitHubSourceConfig, releases: list) -> list:
    response = MagicMock()
    response.json.return_value = releases
    client = AsyncMock()
    client.get.return_value = response
    scraper = GitHubScraper([source], client)
    return asyncio.run(scraper._fetch_repo_releases(source, _SINCE))


_RELEASES = [
    _release(4, "44.108.2", 1),
    _release(3, "44.108.1", 3),
    _release(2, "44.108.0", 5),
    _release(1, "44.107.3", 7),
]


def test_release_level_filters_items():
    source = GitHubSourceConfig(
        type="repo_releases", owner="acme", repo="tool", release_level="minor"
    )
    items = _fetch(source, _RELEASES)
    assert [item.metadata["tag"] for item in items] == ["44.108.0"]

    source = source.model_copy(update={"release_level": "major"})
    assert _fetch(source, _RELEASES) == []


def test_collapse_releases_folds_window_into_newest():
    source = GitHubSourceConfig(
        type="repo_releases", owner="acme", repo="tool", collapse_releases=True
    )
    items = _fetch(source, _RELEASES)

    assert len(items) == 1
    item = items[0]
    assert item.metadata["tag"] == "44.108.2"
    assert str(item.url).endswith("/44.108.2")
    assert item.title == (
        "acme/tool released 44.108.2 (also 44.108.1, 44.108.0, 44.107.3)"
    )
    assert item.metadata["collapsed_tags"] == [
        "44.108.2", "44.108.1", "44.108.0", "44.107.3",
    ]
    assert item.content.index("## 44.108.2") < item.content.index("## 44.107.3")
    assert "notes for 44.108.1" in item.content


def test_collapse_keeps_single_release_untouched():
    source = GitHubSourceConfig(
        type="repo_releases", owner="acme", repo="tool", collapse_releases=True
    )
    items = _fetch(source, _RELEASES[:1])
    assert items[0].title == "acme/tool released 44.108.2"
    assert "collapsed_tags" not in items[0].metadata

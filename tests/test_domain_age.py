"""Tests for the registration-age filter applied to web-search results."""

import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from src.models import ProcessingConfig
from src.processing import tools as tools_module
from src.processing.domain_age import BOOTSTRAP_URL, DomainAgeChecker
from src.processing.tools import ToolRegistry, WebSearchTool

NOW = datetime(2026, 10, 7, 8, 0, tzinfo=timezone.utc)
BOOTSTRAP = {
    "services": [
        [["com", "cc"], ["https://rdap.example-registry.test/"]],
        [["uk"], ["http://rdap.nominet.test/", "https://rdap.nominet.test/"]],
    ]
}


def _registration(days_ago: int) -> dict:
    created = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    return {"events": [{"eventAction": "registration", "eventDate": created}]}


class FakeRdap:
    """Serve the IANA bootstrap and per-domain RDAP answers, recording requests."""

    def __init__(self, domains: dict[str, httpx.Response]):
        self.domains = domains
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url == BOOTSTRAP_URL:
            return httpx.Response(200, json=BOOTSTRAP)
        domain = url.rsplit("/domain/", 1)[-1]
        return self.domains.get(domain, httpx.Response(404))


def _checker(rdap: FakeRdap, cache_path=None, now=NOW, min_age_days=15) -> DomainAgeChecker:
    return DomainAgeChecker(
        min_age_days,
        cache_path,
        transport=httpx.MockTransport(rdap),
        now=lambda: now,
    )


def _result(url: str) -> dict[str, str]:
    return {"title": url, "url": url, "text": ""}


def test_drops_result_from_domain_registered_within_threshold():
    # Arrange
    rdap = FakeRdap({"decisionsapi.cc": httpx.Response(200, json=_registration(7))})
    checker = _checker(rdap)

    # Act
    kept = asyncio.run(checker.filter_results([_result("https://decisionsapi.cc/vs-jev")]))

    # Assert
    assert kept == []


def test_keeps_result_from_domain_older_than_threshold():
    # Arrange
    rdap = FakeRdap({"techcrunch.com": httpx.Response(200, json=_registration(7000))})
    checker = _checker(rdap)
    results = [_result("https://techcrunch.com/2026/10/07/story")]

    # Act
    kept = asyncio.run(checker.filter_results(results))

    # Assert
    assert kept == results


def test_subdomain_is_checked_against_its_registered_domain():
    # Arrange
    rdap = FakeRdap({"example.com": httpx.Response(200, json=_registration(3))})
    checker = _checker(rdap)

    # Act
    kept = asyncio.run(checker.filter_results([_result("https://blog.example.com/post")]))

    # Assert
    assert kept == []
    assert "https://rdap.example-registry.test/domain/example.com" in rdap.requests
    assert not any(url.endswith("/domain/blog.example.com") for url in rdap.requests)


def test_multi_label_public_suffix_falls_through_to_registered_domain():
    # Arrange
    rdap = FakeRdap({"shop.co.uk": httpx.Response(200, json=_registration(2))})
    checker = _checker(rdap)

    # Act
    kept = asyncio.run(checker.filter_results([_result("https://shop.co.uk/")]))

    # Assert
    assert kept == []
    assert rdap.requests[1:] == [
        "https://rdap.nominet.test/domain/co.uk",
        "https://rdap.nominet.test/domain/shop.co.uk",
    ]


def test_keeps_result_when_tld_has_no_rdap_service():
    # Arrange
    rdap = FakeRdap({})
    checker = _checker(rdap)
    results = [_result("https://brut.sh/article")]

    # Act
    kept = asyncio.run(checker.filter_results(results))

    # Assert
    assert kept == results
    assert rdap.requests == [BOOTSTRAP_URL]


def test_keeps_result_when_registry_errors_and_retries_only_after_a_day(tmp_path):
    # Arrange
    rdap = FakeRdap({"flaky.com": httpx.Response(503)})
    cache_path = tmp_path / "domain_ages.json"
    results = [_result("https://flaky.com/")]

    # Act
    first = asyncio.run(_checker(rdap, cache_path).filter_results(results))
    same_day = asyncio.run(
        _checker(rdap, cache_path, now=NOW + timedelta(hours=12)).filter_results(results)
    )
    next_day = asyncio.run(
        _checker(rdap, cache_path, now=NOW + timedelta(days=2)).filter_results(results)
    )

    # Assert
    assert first == same_day == next_day == results
    lookups = [url for url in rdap.requests if url.endswith("/domain/flaky.com")]
    assert len(lookups) == 2


def test_known_registration_date_is_reused_from_disk_cache(tmp_path):
    # Arrange
    cache_path = tmp_path / "domain_ages.json"
    asyncio.run(
        _checker(
            FakeRdap({"decisionsapi.cc": httpx.Response(200, json=_registration(7))}),
            cache_path,
        ).filter_results([_result("https://decisionsapi.cc/")])
    )
    offline = FakeRdap({})

    # Act
    kept = asyncio.run(
        _checker(offline, cache_path, now=NOW + timedelta(days=10)).filter_results(
            [_result("https://decisionsapi.cc/")]
        )
    )

    # Assert
    assert kept == [_result("https://decisionsapi.cc/")]
    assert offline.requests == []
    assert "decisionsapi.cc" in json.loads(cache_path.read_text(encoding="utf-8"))


def test_keeps_results_when_bootstrap_is_unavailable():
    # Arrange
    checker = _checker(lambda request: httpx.Response(500))
    results = [_result("https://decisionsapi.cc/")]

    # Act
    kept = asyncio.run(checker.filter_results(results))

    # Assert
    assert kept == results


def test_web_search_tool_filters_results_through_domain_age(monkeypatch):
    # Arrange
    class FakeDDGS:
        def text(self, query, max_results):
            return [
                {"title": "Old", "href": "https://techcrunch.com/a", "body": ""},
                {"title": "Fresh", "href": "https://decisionsapi.cc/b", "body": ""},
            ]

    monkeypatch.setattr(tools_module, "DDGS", FakeDDGS)
    rdap = FakeRdap({
        "techcrunch.com": httpx.Response(200, json=_registration(7000)),
        "decisionsapi.cc": httpx.Response(200, json=_registration(7)),
    })
    registry = ToolRegistry(domain_age=_checker(rdap))

    # Act
    result = asyncio.run(registry.execute(
        request_id="tool-1", block_id="background", tool="web_search",
        arguments={"query": "decisions api"},
    ))

    # Assert
    assert [entry["title"] for entry in result.results] == ["Old"]


def test_web_search_tool_without_checker_returns_every_result(monkeypatch):
    # Arrange
    class FakeDDGS:
        def text(self, query, max_results):
            return [{"title": "Fresh", "href": "https://decisionsapi.cc/b", "body": ""}]

    monkeypatch.setattr(tools_module, "DDGS", FakeDDGS)

    # Act
    results = asyncio.run(WebSearchTool().execute({"query": "decisions api"}))

    # Assert
    assert [entry["url"] for entry in results] == ["https://decisionsapi.cc/b"]


def test_min_domain_age_defaults_to_disabled_and_rejects_negative_values():
    # Arrange / Act
    config = ProcessingConfig()

    # Assert
    assert config.min_domain_age_days == 0
    with pytest.raises(ValueError):
        ProcessingConfig(min_domain_age_days=-1)

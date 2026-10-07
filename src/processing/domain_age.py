"""Registration-age filter for web-search results.

Freshly registered domains are a strong spam signal: outbound mail filters drop
messages linking to them without any bounce. Ages come from RDAP and are cached
on disk, a registration date never changing.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
UNKNOWN_TTL = timedelta(days=1)


class DomainAgeChecker:
    """Drop URLs whose registered domain is younger than ``min_age_days``.

    A domain whose age cannot be determined (TLD without RDAP, registry error)
    is kept: the filter targets the known-fresh case, not the unknown one.
    """

    def __init__(
        self,
        min_age_days: int,
        cache_path: Optional[Path] = None,
        *,
        timeout_sec: float = 5.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.min_age = timedelta(days=min_age_days)
        self.cache_path = cache_path
        self.timeout_sec = timeout_sec
        self.transport = transport
        self.now = now
        self._cache: dict[str, dict[str, Optional[str]]] = self._load_cache()
        self._servers: Optional[dict[str, str]] = None
        self._bootstrap_lock = asyncio.Lock()

    async def filter_results(self, results: list[dict[str, str]]) -> list[dict[str, str]]:
        """Return ``results`` without the entries pointing to a too-young domain."""
        if not results:
            return results
        kept = []
        async with self._client() as client:
            for result in results:
                if not await self._is_too_young(client, result.get("url", "")):
                    kept.append(result)
        self._save_cache()
        return kept

    async def _is_too_young(self, client: httpx.AsyncClient, url: str) -> bool:
        host = (urlsplit(url).hostname or "").rstrip(".").lower()
        if "." not in host:
            return False
        created = await self._creation_date(client, host)
        if created is None:
            return False
        age = self.now() - created
        if age < self.min_age:
            logger.info(
                "Dropping web result %s: domain registered %d days ago", url, age.days
            )
            return True
        return False

    async def _creation_date(self, client: httpx.AsyncClient, host: str) -> Optional[datetime]:
        cached = self._cache.get(host)
        if cached is not None:
            if cached["created"] is not None:
                return datetime.fromisoformat(cached["created"])
            if self.now() - datetime.fromisoformat(cached["checked"]) < UNKNOWN_TTL:
                return None

        created = await self._lookup(client, host)
        self._cache[host] = {
            "created": created.isoformat() if created else None,
            "checked": self.now().isoformat(),
        }
        return created

    async def _lookup(self, client: httpx.AsyncClient, host: str) -> Optional[datetime]:
        labels = host.split(".")
        base_url = (await self._rdap_servers(client)).get(labels[-1])
        if base_url is None:
            return None
        # Shortest candidate first: `example.com` answers for `blog.example.com`
        # in one request, while `co.uk` gets a 404 before `example.co.uk` is tried.
        for start in range(len(labels) - 2, -1, -1):
            candidate = ".".join(labels[start:])
            try:
                response = await client.get(f"{base_url}domain/{candidate}")
            except httpx.HTTPError as exc:
                logger.warning("RDAP lookup failed for %s: %s", candidate, exc)
                return None
            if response.status_code == 404:
                continue
            if response.status_code != 200:
                logger.warning(
                    "RDAP lookup for %s returned HTTP %d", candidate, response.status_code
                )
                return None
            return _registration_date(response.json())
        return None

    async def _rdap_servers(self, client: httpx.AsyncClient) -> dict[str, str]:
        async with self._bootstrap_lock:
            if self._servers is None:
                self._servers = {}
                try:
                    response = await client.get(BOOTSTRAP_URL)
                    response.raise_for_status()
                    for tlds, urls in response.json().get("services", []):
                        base_url = next((u for u in urls if u.startswith("https://")), urls[0])
                        for tld in tlds:
                            self._servers[tld.lower()] = base_url.rstrip("/") + "/"
                except (httpx.HTTPError, ValueError) as exc:
                    logger.warning("RDAP bootstrap unavailable, domain ages unchecked: %s", exc)
            return self._servers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self.timeout_sec, follow_redirects=True, transport=self.transport
        )

    def _load_cache(self) -> dict[str, dict[str, Optional[str]]]:
        if self.cache_path is None or not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable domain age cache %s: %s", self.cache_path, exc)
            return {}

    def _save_cache(self) -> None:
        if self.cache_path is None:
            return
        temporary = self.cache_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._cache, indent=1, sort_keys=True), encoding="utf-8")
        temporary.replace(self.cache_path)


def _registration_date(payload: Any) -> Optional[datetime]:
    for event in payload.get("events", []) if isinstance(payload, dict) else []:
        if event.get("eventAction") == "registration" and event.get("eventDate"):
            try:
                created = datetime.fromisoformat(event["eventDate"].replace("Z", "+00:00"))
            except ValueError:
                return None
            return created if created.tzinfo else created.replace(tzinfo=timezone.utc)
    return None

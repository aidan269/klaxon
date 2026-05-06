"""RSS / Atom collector. feedparser handles the format zoo.

Each feed entry becomes a POST observation. RSS has no concept of accounts,
so `discover_accounts` returns nothing.

Per-feed `match_terms` (in collector options) lets operators pre-filter at
the feed level: only emit entries whose title+summary contains any of these
substrings. Useful for broad feeds like Google News where the source isn't
already brand-scoped. If unset, falls back to `query.keywords` from the runner.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import feedparser
import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from socmon.collectors import register
from socmon.config import BrandEntity, ExecutiveEntity
from socmon.interfaces import Collector
from socmon.models import AccountObservation, CollectorQuery, Observation, ObservationKind

log = logging.getLogger(__name__)

_DEFAULT_USER_AGENT = "socmon/0.1 (+https://github.com/spearbit/socmon)"


@register("rss")
class RssCollector(Collector):
    name = "rss"

    def __init__(self, **options) -> None:
        self.options = options
        self.feeds: list[str] = options.get("feeds", [])
        self.user_agent: str = options.get("user_agent", _DEFAULT_USER_AGENT)
        self.request_timeout: float = float(options.get("request_timeout", 15.0))
        # Per-feed pre-filter substrings. If empty, we use query.keywords from the
        # runner. If both are empty, all entries are accepted.
        self.match_terms: list[str] = [t.lower() for t in options.get("match_terms", [])]

    async def collect(self, query: CollectorQuery) -> AsyncIterator[Observation]:
        if not self.feeds:
            return

        # Match on either configured match_terms or the runtime query keywords.
        terms = self.match_terms or [k.lower() for k in (query.keywords or [])]
        headers = {"User-Agent": self.user_agent}
        async with httpx.AsyncClient(
            headers=headers, timeout=self.request_timeout, follow_redirects=True,
        ) as client:
            for url in self.feeds:
                try:
                    body = await self._fetch(client, url)
                except httpx.HTTPError as e:
                    log.warning("rss fetch failed url=%s err=%s", url, e)
                    continue

                parsed = feedparser.parse(body)
                if parsed.bozo and not parsed.entries:
                    log.warning("rss parse error url=%s err=%s", url, parsed.bozo_exception)
                    continue

                for entry in parsed.entries:
                    obs = _entry_to_observation(entry, feed_url=url)
                    if obs is None:
                        continue
                    if query.since and obs.created_at < query.since:
                        continue
                    if terms:
                        text_l = (obs.text or "").lower()
                        if not any(t in text_l for t in terms):
                            continue
                    yield obs

    async def discover_accounts(
        self,
        brand: BrandEntity,
        executives: list[ExecutiveEntity],
    ) -> AsyncIterator[AccountObservation]:
        # RSS has no concept of accounts — early return.
        return
        yield  # type: ignore[unreachable]

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _fetch(self, client: httpx.AsyncClient, url: str) -> bytes:
        r = await client.get(url)
        r.raise_for_status()
        return r.content


# ---------------------------------------------------------------------------


def _entry_to_observation(entry: dict, feed_url: str) -> Observation | None:
    """Map a feedparser entry into our Observation. Tolerant of missing fields —
    feeds in the wild are inconsistent."""
    # Stable id: prefer entry.id/guid; fall back to a hash of (link + title) so
    # we don't generate duplicates if a feed lacks ids.
    raw_id = entry.get("id") or entry.get("guid")
    link = entry.get("link") or ""
    title = entry.get("title") or ""
    if not raw_id:
        if not (link or title):
            return None
        raw_id = hashlib.sha256(f"{link}|{title}".encode()).hexdigest()[:32]

    created_at = _entry_published(entry)
    summary = entry.get("summary") or entry.get("description") or ""
    text = "\n\n".join(p for p in (title, summary) if p) or None

    author = (
        entry.get("author")
        or (entry.get("source") or {}).get("title")
        or _domain(feed_url)
    )

    return Observation(
        id=f"rss:post:{hashlib.sha256(raw_id.encode()).hexdigest()[:16]}",
        platform="rss",
        kind=ObservationKind.POST,
        author_handle=author,
        author_id=None,
        text=text,
        url=link or None,
        created_at=created_at,
        collected_at=datetime.now(timezone.utc),
        raw={"feed_url": feed_url, "entry_id": raw_id},
    )


def _entry_published(entry: dict) -> datetime:
    """Pull the published timestamp; fall back to now if absent."""
    for key in ("published_parsed", "updated_parsed"):
        struct = entry.get(key)
        if struct:
            try:
                # feedparser returns a struct_time in UTC.
                return datetime(*struct[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    return datetime.now(timezone.utc)


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc or "rss"
    except Exception:
        return "rss"

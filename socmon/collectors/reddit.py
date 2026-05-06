"""Reddit collector — public JSON endpoints, no OAuth.

Vertical-slice scope: only `discover_accounts()` is fully wired (the impersonation
detector consumes it). `collect()` for posts/comments is still stubbed; that lands
with the mention/keyword spike slice.

Reddit's `/users/search.json?q=…` endpoint surfaces accounts whose handle or
display name fuzzy-matches a query. We feed it brand name + aliases + executive
last names and let the impersonation detector score the union.

Avatar pHashing: each candidate's `icon_img` is fetched and run through
imagehash.phash. Failures (404, timeout, non-image) degrade gracefully — the
account still gets emitted, just without `avatar_phash`. The detector treats
missing pHash as "skip that signal," not "score zero."
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import AsyncIterator

import httpx
import imagehash
from PIL import Image
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from socmon.collectors import register
from socmon.config import BrandEntity, ExecutiveEntity
from socmon.interfaces import Collector
from socmon.models import AccountObservation, CollectorQuery, Observation, ObservationKind

log = logging.getLogger(__name__)

_USER_SEARCH_URL = "https://www.reddit.com/users/search.json"
_POST_SEARCH_URL = "https://www.reddit.com/search.json"
_DEFAULT_USER_AGENT = "socmon/0.1 (+https://github.com/spearbit/socmon)"


@register("reddit")
class RedditCollector(Collector):
    name = "reddit"

    def __init__(self, **options) -> None:
        self.options = options
        self.user_agent: str = options.get("user_agent", _DEFAULT_USER_AGENT)
        self.search_limit: int = int(options.get("search_limit", 25))
        # Pulling avatars is the slow part; flag-controlled so tests/backtests can disable it.
        self.fetch_avatars: bool = bool(options.get("fetch_avatars", True))
        self.request_timeout: float = float(options.get("request_timeout", 10.0))
        # In-process polite throttle between hits (Reddit's anonymous limit is ~60/min).
        self.min_request_interval: float = float(options.get("min_request_interval", 1.1))
        self._last_request_at: float = 0.0

    # ----- public API -----

    async def collect(self, query: CollectorQuery) -> AsyncIterator[Observation]:
        """Search Reddit posts matching `query.keywords`. Honors `query.since` by
        stopping at the first post older than the watermark.

        Note: we only fetch the first page (default 100 results) per keyword set.
        For high-volume keywords this can miss observations between polls; the
        detector compensates by polling frequently. Pagination via `after` is a
        TODO when we hit a customer running socmon over a noisy keyword.
        """
        if not query.keywords:
            return
        q = self._build_search_query(query.keywords)
        headers = {"User-Agent": self.user_agent}
        async with httpx.AsyncClient(headers=headers, timeout=self.request_timeout) as client:
            try:
                children = await self._search_posts(client, q)
            except httpx.HTTPError as e:
                log.warning("reddit post search failed q=%r err=%s", q, e)
                return
            for child in children:
                data = child.get("data") or {}
                obs = self._post_from_search_data(data)
                if obs is None:
                    continue
                if query.since and obs.created_at < query.since:
                    continue
                yield obs

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _search_posts(self, client: httpx.AsyncClient, q: str) -> list[dict]:
        await self._throttle()
        r = await client.get(
            _POST_SEARCH_URL,
            params={"q": q, "sort": "new", "limit": 100, "type": "link"},
        )
        r.raise_for_status()
        body = r.json()
        return (body.get("data") or {}).get("children") or []

    def _build_search_query(self, keywords: list[str]) -> str:
        """Reddit's search supports OR'd quoted phrases. We quote each keyword
        so multi-word names like "Acme Corp" stay together."""
        return " OR ".join(f'"{k}"' for k in keywords if k)

    def _post_from_search_data(self, data: dict) -> Observation | None:
        post_id = data.get("id")
        if not post_id:
            return None
        created_utc = data.get("created_utc")
        created_at = (
            datetime.fromtimestamp(created_utc, tz=timezone.utc)
            if created_utc else datetime.now(timezone.utc)
        )
        # Combine title + selftext so detectors only need to look at .text.
        text_parts: list[str] = []
        if data.get("title"):
            text_parts.append(data["title"])
        if data.get("selftext"):
            text_parts.append(data["selftext"])
        text = "\n\n".join(text_parts) or None
        permalink = data.get("permalink")
        url = f"https://www.reddit.com{permalink}" if permalink else data.get("url")

        return Observation(
            id=f"reddit:post:{post_id}",
            platform="reddit",
            kind=ObservationKind.POST,
            author_handle=data.get("author"),
            author_id=data.get("author_fullname"),
            text=text,
            url=url,
            created_at=created_at,
            collected_at=datetime.now(timezone.utc),
            raw=data,
        )

    async def discover_accounts(
        self,
        brand: BrandEntity,
        executives: list[ExecutiveEntity],
    ) -> AsyncIterator[AccountObservation]:
        queries = self._build_queries(brand, executives)
        seen_ids: set[str] = set()

        headers = {"User-Agent": self.user_agent}
        async with httpx.AsyncClient(headers=headers, timeout=self.request_timeout) as client:
            for q in queries:
                try:
                    children = await self._search_users(client, q)
                except httpx.HTTPError as e:
                    log.warning("reddit search failed q=%r err=%s", q, e)
                    continue

                for child in children:
                    data = child.get("data") or {}
                    t2_id = data.get("id")
                    if not t2_id or t2_id in seen_ids:
                        continue
                    seen_ids.add(t2_id)

                    obs = await self._account_from_search_data(client, data)
                    if obs is not None:
                        yield obs

    # ----- query construction -----

    def _build_queries(self, brand: BrandEntity, executives: list[ExecutiveEntity]) -> list[str]:
        """Brand name + aliases + each executive's last name. Reddit's search is fuzzy
        so we don't try to be clever with permutations — that's the detector's job.
        """
        candidates: list[str] = []
        candidates.append(brand.name)
        candidates.extend(brand.aliases)
        for exec_ in executives:
            last = exec_.name.split()[-1] if exec_.name else ""
            if last:
                candidates.append(last)
        # Strip junk, dedup, lowercase for stable cache behavior.
        seen: set[str] = set()
        out: list[str] = []
        for c in candidates:
            c = re.sub(r"[^A-Za-z0-9 ]+", " ", c).strip().lower()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
        return out

    # ----- network -----

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _search_users(self, client: httpx.AsyncClient, q: str) -> list[dict]:
        await self._throttle()
        r = await client.get(
            _USER_SEARCH_URL,
            params={"q": q, "limit": self.search_limit, "type": "user"},
        )
        r.raise_for_status()
        body = r.json()
        return (body.get("data") or {}).get("children") or []

    async def _throttle(self) -> None:
        now = asyncio.get_event_loop().time()
        delta = now - self._last_request_at
        if delta < self.min_request_interval:
            await asyncio.sleep(self.min_request_interval - delta)
        self._last_request_at = asyncio.get_event_loop().time()

    # ----- normalization -----

    async def _account_from_search_data(
        self,
        client: httpx.AsyncClient,
        data: dict,
    ) -> AccountObservation | None:
        handle = data.get("name")
        t2_id = data.get("id")
        if not handle or not t2_id:
            return None

        # `subreddit` carries the user's profile — display_name lives there.
        subreddit = data.get("subreddit") or {}
        display_name = subreddit.get("title") or subreddit.get("display_name_prefixed")
        bio = subreddit.get("public_description") or data.get("public_description")
        icon_url = (
            subreddit.get("icon_img")
            or data.get("icon_img")
            or subreddit.get("community_icon")
        )
        # Reddit returns icon URLs with query params (`?width=…&s=…`); strip for stable hashing.
        icon_url = _strip_query(icon_url) if icon_url else None

        created_utc = data.get("created_utc") or subreddit.get("created_utc")
        account_created_at = (
            datetime.fromtimestamp(created_utc, tz=timezone.utc) if created_utc else None
        )

        avatar_phash = None
        if self.fetch_avatars and icon_url:
            avatar_phash = await self._phash_url(client, icon_url)

        return AccountObservation(
            id=f"reddit:account:{t2_id}",
            platform="reddit",
            kind=ObservationKind.ACCOUNT,
            author_handle=handle,
            author_id=t2_id,
            text=bio,
            url=f"https://www.reddit.com/user/{handle}",
            created_at=account_created_at or datetime.now(timezone.utc),
            collected_at=datetime.now(timezone.utc),
            display_name=display_name,
            bio=bio,
            avatar_url=icon_url,
            avatar_phash=avatar_phash,
            account_created_at=account_created_at,
            verified=bool(data.get("verified")),
            raw=data,
        )

    async def _phash_url(self, client: httpx.AsyncClient, url: str) -> str | None:
        try:
            r = await client.get(url)
            r.raise_for_status()
            img = Image.open(BytesIO(r.content))
            return str(imagehash.phash(img))
        except (httpx.HTTPError, OSError, ValueError) as e:
            log.debug("phash skip url=%s err=%s", url, e)
            return None


def _strip_query(url: str) -> str:
    return url.split("?", 1)[0]

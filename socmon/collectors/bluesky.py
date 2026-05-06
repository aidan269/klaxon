"""Bluesky / AT Protocol collector — STUB.

Bluesky's app-view API (`api.bsky.app/xrpc/...`) is permissive enough that this is
a likely candidate for a third reference implementation post-MVP. Stub for now.
"""

from __future__ import annotations

from typing import AsyncIterator

from socmon.collectors import register
from socmon.config import BrandEntity, ExecutiveEntity
from socmon.interfaces import Collector
from socmon.models import AccountObservation, CollectorQuery, Observation


@register("bluesky")
class BlueskyCollector(Collector):
    name = "bluesky"

    def __init__(self, **options) -> None:
        self.options = options

    async def collect(self, query: CollectorQuery) -> AsyncIterator[Observation]:
        raise NotImplementedError("bluesky collector is a stub")
        yield  # type: ignore[unreachable]

    async def discover_accounts(
        self,
        brand: BrandEntity,
        executives: list[ExecutiveEntity],
    ) -> AsyncIterator[AccountObservation]:
        raise NotImplementedError
        yield  # type: ignore[unreachable]

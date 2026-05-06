"""X / Twitter collector — STUB.

Not a vertical-slice target: the API requires a paid tier for keyword search and the
auth flow is involved. Stub class so the registry resolves; raise on use until wired.
"""

from __future__ import annotations

from typing import AsyncIterator

from socmon.collectors import register
from socmon.config import BrandEntity, ExecutiveEntity
from socmon.interfaces import Collector
from socmon.models import AccountObservation, CollectorQuery, Observation


@register("twitter")
class TwitterCollector(Collector):
    name = "twitter"

    def __init__(self, **options) -> None:
        self.options = options

    async def collect(self, query: CollectorQuery) -> AsyncIterator[Observation]:
        raise NotImplementedError("twitter collector is a stub")
        yield  # type: ignore[unreachable]

    async def discover_accounts(
        self,
        brand: BrandEntity,
        executives: list[ExecutiveEntity],
    ) -> AsyncIterator[AccountObservation]:
        raise NotImplementedError
        yield  # type: ignore[unreachable]

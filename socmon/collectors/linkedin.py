"""LinkedIn collector — STUB.

LinkedIn doesn't expose a public search API for our use case. Realistically this
adapter would target either (a) the company's own ATS feed for legitimate jobs
(comparison set, see legit_jobs in config) or (b) a partnered third-party feed.
Stub-only for now.
"""

from __future__ import annotations

from typing import AsyncIterator

from socmon.collectors import register
from socmon.config import BrandEntity, ExecutiveEntity
from socmon.interfaces import Collector
from socmon.models import AccountObservation, CollectorQuery, Observation


@register("linkedin")
class LinkedInCollector(Collector):
    name = "linkedin"

    def __init__(self, **options) -> None:
        self.options = options

    async def collect(self, query: CollectorQuery) -> AsyncIterator[Observation]:
        raise NotImplementedError("linkedin collector is a stub")
        yield  # type: ignore[unreachable]

    async def discover_accounts(
        self,
        brand: BrandEntity,
        executives: list[ExecutiveEntity],
    ) -> AsyncIterator[AccountObservation]:
        raise NotImplementedError
        yield  # type: ignore[unreachable]

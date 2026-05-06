"""Abstract interfaces. Adding a platform = new Collector. Adding a signal = new Detector.
Adding a notification channel = new Alerter. Nothing else in the system should need to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator, Iterator, TYPE_CHECKING

from socmon.models import (
    AccountObservation,
    CollectorQuery,
    Finding,
    Observation,
    TimeWindow,
)

if TYPE_CHECKING:
    from socmon.config import BrandEntity, ExecutiveEntity
    from socmon.storage.base import Storage


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


class Collector(ABC):
    """One platform adapter. Implementations: reddit, rss, twitter (stub), bluesky (stub), …

    Collectors are async because most of them are network-bound. They are responsible for:
      - Honoring the `since` watermark so we don't re-fetch what we already have.
      - Producing fully-normalized Observation objects (the raw payload goes in `raw`).
      - Being polite to the upstream API (rate limiting, backoff). Use `tenacity` for retries.

    Collectors are NOT responsible for: dedup (storage handles upserts on Observation.id),
    detection logic, or alerting.
    """

    name: str  # unique; used in logs, config, and metrics

    @abstractmethod
    async def collect(self, query: CollectorQuery) -> AsyncIterator[Observation]:
        """Stream content matching `query`. Used by mention/keyword/spike detectors."""
        raise NotImplementedError
        yield  # type: ignore[unreachable]  # marks this as a generator for type-checkers

    @abstractmethod
    async def discover_accounts(
        self,
        brand: "BrandEntity",
        executives: list["ExecutiveEntity"],
    ) -> AsyncIterator[AccountObservation]:
        """Surface accounts that *could* be impersonations. The collector casts a wide net
        (e.g. fuzzy username search); the impersonation detector does the scoring.
        """
        raise NotImplementedError
        yield  # type: ignore[unreachable]

    async def healthcheck(self) -> bool:
        """Optional. Returns True if the collector can reach its upstream."""
        return True


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


class Detector(ABC):
    """Reads Observations from storage, emits Findings.

    Detectors are intentionally synchronous and pure where possible — given the same
    storage state and window, they should produce the same findings. This makes
    `socmon backtest` straightforward.

    State that *must* persist between runs (rolling baselines, seen-account hashes)
    lives in the DB, not in the detector instance.
    """

    name: str
    enabled: bool = True

    @abstractmethod
    def run(self, storage: "Storage", window: TimeWindow) -> Iterator[Finding]:
        """Evaluate the window and yield findings. May yield zero."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Alerter
# ---------------------------------------------------------------------------


class AlertResult(ABC):
    """Subclass-friendly result wrapper. Alerters return one per finding."""
    ok: bool
    detail: str | None = None


class Alerter(ABC):
    """Notification channel. Implementations: slack, email, pagerduty, webhook."""

    name: str

    @abstractmethod
    def send(self, finding: Finding) -> None:
        """Deliver `finding` to the channel. Raise on hard failures so the scheduler
        can record the failure and retry; soft failures (rate-limit) should retry
        internally with backoff.
        """
        raise NotImplementedError

    def test(self) -> None:
        """Send a synthetic finding so operators can verify wiring (`socmon alerts test`)."""
        from datetime import datetime, timezone
        from socmon.models import FindingKind, Severity
        self.send(
            Finding(
                id="test:0",
                kind=FindingKind.MENTION_SPIKE,
                detector="test",
                severity=Severity.LOW,
                score=0,
                title="socmon alert test",
                summary="If you can read this, alerting is wired up.",
                detected_at=datetime.now(timezone.utc),
            )
        )

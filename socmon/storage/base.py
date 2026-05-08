"""Storage interface. SQLite for dev, Postgres for prod — both implement this protocol.

Two tables that matter:
  - observations: append-only-ish (upsert on id). The audit log of what we've seen.
  - findings: detector outputs. Dedup'd by Finding.id so re-running detectors is safe.

Detectors read observations and write findings. Storage owns the schema.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterable, Iterator

from socmon.models import Finding, Observation


class Storage(ABC):
    @abstractmethod
    def init_schema(self) -> None: ...

    # ----- Observations -----

    @abstractmethod
    def upsert_observations(self, items: Iterable[Observation]) -> list[Observation]:
        """Idempotent. Returns the NEW observations (those not previously seen in
        storage). Existing rows are still updated in place; they're just absent
        from the return value so callers can summarize "what's new this run."
        """
        ...

    @abstractmethod
    def query_observations(
        self,
        platform: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        text_match: str | None = None,
        limit: int | None = None,
    ) -> Iterator[Observation]: ...

    @abstractmethod
    def watermark(self, collector_name: str) -> datetime | None:
        """Latest `created_at` we've successfully ingested for this collector."""
        ...

    @abstractmethod
    def set_watermark(self, collector_name: str, ts: datetime) -> None: ...

    # ----- Findings -----

    @abstractmethod
    def insert_finding(self, finding: Finding) -> bool:
        """Returns True if newly inserted, False if a finding with this id already exists
        (dedup). Callers can use the return value to suppress re-alerts.
        """
        ...

    @abstractmethod
    def query_findings(
        self,
        kind: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterator[Finding]: ...

    # ----- Detector state (rolling baselines, seen-account hashes, etc.) -----

    @abstractmethod
    def get_state(self, namespace: str, key: str) -> str | None: ...

    @abstractmethod
    def set_state(self, namespace: str, key: str, value: str) -> None: ...

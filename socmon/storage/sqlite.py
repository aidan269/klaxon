"""SQLAlchemy-backed storage. SQLite for dev, Postgres in prod via the same code path.

Keeps four tables:
  observations   audit log of normalized things-we-saw, upsert by id
  findings       detector outputs, insert-only (dedup via deterministic id)
  watermarks     per-collector "latest created_at successfully ingested"
  kv_state       generic detector state (rolling baselines, seen-account hashes)

Observations are stored as a flattened header (for cheap filtering) plus the full
pydantic model serialized into `payload` JSON, so subclass fields
(`AccountObservation.avatar_phash`, `JobObservation.contact_methods`, …) survive
round-trips without schema gymnastics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Iterator

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    String,
    create_engine,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from socmon.models import (
    AccountObservation,
    Finding,
    JobObservation,
    Observation,
    ObservationKind,
)
from socmon.storage.base import Storage


class _Base(DeclarativeBase):
    pass


class _ObservationRow(_Base):
    __tablename__ = "observations"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    platform: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    author_handle: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    author_id: Mapped[str | None] = mapped_column(String, nullable=True)
    text: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSON)


class _FindingRow(_Base):
    __tablename__ = "findings"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, index=True)
    detector: Mapped[str] = mapped_column(String)
    severity: Mapped[str] = mapped_column(String)
    score: Mapped[float] = mapped_column(Float)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict] = mapped_column(JSON)


class _WatermarkRow(_Base):
    __tablename__ = "watermarks"
    collector: Mapped[str] = mapped_column(String, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class _KvStateRow(_Base):
    __tablename__ = "kv_state"
    namespace: Mapped[str] = mapped_column(String, primary_key=True)
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_aware(ts: datetime) -> datetime:
    """SQLite drops tz info on write; assume UTC on the way out so detector math
    over `created_at` doesn't silently mix naive and aware datetimes."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts


_OBS_CLASSES = {
    ObservationKind.ACCOUNT.value: AccountObservation,
    ObservationKind.JOB.value: JobObservation,
}


def _row_to_observation(row: _ObservationRow) -> Observation:
    cls = _OBS_CLASSES.get(row.kind, Observation)
    return cls.model_validate(row.payload)


def _observation_to_row(obs: Observation) -> _ObservationRow:
    return _ObservationRow(
        id=obs.id,
        platform=obs.platform,
        kind=obs.kind.value if hasattr(obs.kind, "value") else str(obs.kind),
        author_handle=obs.author_handle,
        author_id=obs.author_id,
        text=obs.text,
        url=str(obs.url) if obs.url else None,
        created_at=_ensure_aware(obs.created_at),
        collected_at=_ensure_aware(obs.collected_at),
        payload=obs.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Storage impl
# ---------------------------------------------------------------------------


class SqliteStorage(Storage):
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        # future_=True is the default in 2.0 but keep the hint explicit
        self.engine = create_engine(dsn, future=True)

    def init_schema(self) -> None:
        _Base.metadata.create_all(self.engine)

    # ----- Observations -----

    def upsert_observations(self, items: Iterable[Observation]) -> int:
        """Returns the count of NEW (not previously seen) observations."""
        new_count = 0
        with Session(self.engine) as s:
            for obs in items:
                existing = s.get(_ObservationRow, obs.id)
                if existing is None:
                    s.add(_observation_to_row(obs))
                    new_count += 1
                else:
                    # Update payload + collected_at; leave created_at alone (immutable on-platform).
                    fresh = _observation_to_row(obs)
                    existing.author_handle = fresh.author_handle
                    existing.author_id = fresh.author_id
                    existing.text = fresh.text
                    existing.url = fresh.url
                    existing.collected_at = fresh.collected_at
                    existing.payload = fresh.payload
            s.commit()
        return new_count

    def query_observations(
        self,
        platform: str | None = None,
        kind: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        text_match: str | None = None,
        limit: int | None = None,
    ) -> Iterator[Observation]:
        with Session(self.engine) as s:
            stmt = select(_ObservationRow)
            if platform:
                stmt = stmt.where(_ObservationRow.platform == platform)
            if kind:
                stmt = stmt.where(_ObservationRow.kind == kind)
            if since:
                stmt = stmt.where(_ObservationRow.created_at >= _ensure_aware(since))
            if until:
                stmt = stmt.where(_ObservationRow.created_at < _ensure_aware(until))
            if text_match:
                # Naive LIKE; for prod-Postgres we'd use a tsvector index.
                stmt = stmt.where(_ObservationRow.text.ilike(f"%{text_match}%"))
            stmt = stmt.order_by(_ObservationRow.created_at.desc())
            if limit:
                stmt = stmt.limit(limit)
            for row in s.execute(stmt).scalars():
                yield _row_to_observation(row)

    def watermark(self, collector_name: str) -> datetime | None:
        with Session(self.engine) as s:
            row = s.get(_WatermarkRow, collector_name)
            return _ensure_aware(row.ts) if row else None

    def set_watermark(self, collector_name: str, ts: datetime) -> None:
        with Session(self.engine) as s:
            row = s.get(_WatermarkRow, collector_name)
            if row is None:
                s.add(_WatermarkRow(collector=collector_name, ts=_ensure_aware(ts)))
            else:
                row.ts = _ensure_aware(ts)
            s.commit()

    # ----- Findings -----

    def insert_finding(self, finding: Finding) -> bool:
        """Returns True if newly inserted, False if a finding with this id already exists."""
        with Session(self.engine) as s:
            try:
                s.add(_FindingRow(
                    id=finding.id,
                    kind=finding.kind.value,
                    detector=finding.detector,
                    severity=finding.severity.value,
                    score=finding.score,
                    detected_at=_ensure_aware(finding.detected_at),
                    payload=finding.model_dump(mode="json"),
                ))
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    def query_findings(
        self,
        kind: str | None = None,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Iterator[Finding]:
        with Session(self.engine) as s:
            stmt = select(_FindingRow)
            if kind:
                stmt = stmt.where(_FindingRow.kind == kind)
            if since:
                stmt = stmt.where(_FindingRow.detected_at >= _ensure_aware(since))
            stmt = stmt.order_by(_FindingRow.detected_at.desc())
            if limit:
                stmt = stmt.limit(limit)
            for row in s.execute(stmt).scalars():
                yield Finding.model_validate(row.payload)

    # ----- KV state -----

    def get_state(self, namespace: str, key: str) -> str | None:
        with Session(self.engine) as s:
            row = s.get(_KvStateRow, (namespace, key))
            return row.value if row else None

    def set_state(self, namespace: str, key: str, value: str) -> None:
        with Session(self.engine) as s:
            row = s.get(_KvStateRow, (namespace, key))
            if row is None:
                s.add(_KvStateRow(namespace=namespace, key=key, value=value))
            else:
                row.value = value
            s.commit()

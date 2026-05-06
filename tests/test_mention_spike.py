"""End-to-end tests for the mention-spike detector via storage.

Covers:
  - flat baseline + recent spike → one finding
  - flat-zero baseline + small recent volume gated by min_volume
  - flat-zero baseline + recent volume above min_volume → critical (inf z)
  - elevated-but-not-spike → no finding
  - dedup via deterministic id when re-run on the same data
  - top-author attribution in evidence + metadata
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socmon.detectors.mention_spike import MentionSpikeDetector
from socmon.models import (
    FindingKind,
    Observation,
    ObservationKind,
    Severity,
    TimeWindow,
)
from socmon.storage.sqlite import SqliteStorage


UTC = timezone.utc
NOW = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path) -> SqliteStorage:
    s = SqliteStorage(f"sqlite:///{tmp_path}/socmon.db")
    s.init_schema()
    return s


def _post(when: datetime, idx: int, *, author: str = "u1",
          platform: str = "reddit", text: str = "acme news") -> Observation:
    return Observation(
        id=f"{platform}:post:{idx}",
        platform=platform,
        kind=ObservationKind.POST,
        author_handle=author,
        author_id=f"id_{author}",
        text=text,
        url=f"https://example.test/{platform}/{idx}",
        created_at=when,
        collected_at=when,
    )


# ---------------------------------------------------------------------------
# Helpers to build baseline + spike scenarios
# ---------------------------------------------------------------------------


def _seed_flat_baseline(storage: SqliteStorage, *, per_hour: int, hours: int,
                        end: datetime, idx_start: int = 0) -> int:
    """Seed `per_hour` observations into each of the previous `hours` hour-buckets
    BEFORE the recent (current) bucket — i.e. h=2..hours+1 — so baseline doesn't
    contaminate the "current" bucket. Returns next free observation index."""
    idx = idx_start
    for h in range(2, hours + 2):
        bucket = end - timedelta(hours=h)
        for k in range(per_hour):
            ts = bucket + timedelta(seconds=60 * k)
            storage.upsert_observations([_post(ts, idx, author=f"baseline_{idx % 5}")])
            idx += 1
    return idx


def _seed_recent_bucket(storage: SqliteStorage, *, count: int, end: datetime,
                        idx_start: int, authors: list[str] | None = None) -> int:
    idx = idx_start
    authors = authors or [f"recent_{i}" for i in range(count)]
    bucket_start = end - timedelta(minutes=30)  # within the last hour bucket
    for k in range(count):
        ts = bucket_start + timedelta(seconds=k)
        storage.upsert_observations([_post(ts, idx, author=authors[k % len(authors)])])
        idx += 1
    return idx


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_observations_emits_nothing(storage) -> None:
    det = MentionSpikeDetector(name="mentions")
    window = TimeWindow(start=NOW - timedelta(days=10), end=NOW)
    assert list(det.run(storage, window)) == []


def test_clear_spike_against_flat_baseline(storage) -> None:
    # 7 days of 2 mentions/hr → mean=2 stddev=0; recent hr has 30 → inf z, critical.
    idx = _seed_flat_baseline(storage, per_hour=2, hours=24 * 7, end=NOW)
    _seed_recent_bucket(storage, count=30, end=NOW, idx_start=idx,
                        authors=["alice", "alice", "bob", "carol", "dave"])

    det = MentionSpikeDetector(name="mentions",
                               bucket_seconds=3600, baseline_days=7,
                               z_threshold=3.0, min_volume=5)
    window = TimeWindow(start=NOW - timedelta(days=10), end=NOW)

    findings = list(det.run(storage, window))
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == FindingKind.MENTION_SPIKE
    assert f.severity == Severity.CRITICAL  # inf z lands in critical
    assert f.metadata["current"] == 30
    assert f.metadata["z_score_inf"] is True
    # Top authors recorded; alice appears in 2 of the 5 round-robin slots,
    # so 30 obs spread across [alice, alice, bob, carol, dave] gives alice 12.
    top = dict(f.metadata["top_authors"])
    assert top.get("alice") == 12
    assert top.get("bob") == 6
    # Evidence is per-author-deduped, so 4 unique authors (alice/bob/carol/dave).
    handles = {ev.observation_id for ev in f.evidence}
    assert len(handles) == 4


def test_below_min_volume_is_not_a_spike(storage) -> None:
    # Flat-zero baseline, recent has 3 obs — z=inf, but min_volume=5 gates it.
    _seed_recent_bucket(storage, count=3, end=NOW, idx_start=0)
    det = MentionSpikeDetector(name="mentions", bucket_seconds=3600,
                               baseline_days=7, z_threshold=3.0, min_volume=5)
    window = TimeWindow(start=NOW - timedelta(days=10), end=NOW)
    assert list(det.run(storage, window)) == []


def test_elevated_but_not_spike_is_quiet(storage) -> None:
    # Baseline ~5/hr with stddev ~5. Recent =14 → z = (14-5)/5 = 1.8 < 3 → no finding.
    # Use a noisy baseline to get a meaningful stddev.
    import random
    rng = random.Random(0)
    end = NOW
    idx = 0
    # h=2..169 → skip the recent bucket
    for h in range(2, 24 * 7 + 2):
        bucket = end - timedelta(hours=h)
        n = max(0, int(rng.gauss(5, 5)))
        for k in range(n):
            ts = bucket + timedelta(seconds=60 * k)
            storage.upsert_observations([_post(ts, idx)])
            idx += 1
    _seed_recent_bucket(storage, count=14, end=NOW, idx_start=idx)

    det = MentionSpikeDetector(name="mentions", bucket_seconds=3600,
                               baseline_days=7, z_threshold=3.0, min_volume=5)
    window = TimeWindow(start=NOW - timedelta(days=10), end=NOW)
    assert list(det.run(storage, window)) == []


def test_severity_bands_high_vs_critical(storage) -> None:
    # Build a baseline with mean=10 stddev=2 → ~constant=10 with two outliers
    # at +/-2 to get nonzero stddev. Recent count tunes z.
    end = NOW
    idx = 0
    for h in range(2, 24 * 7 + 2):
        bucket = end - timedelta(hours=h)
        # alternate 8/12 → mean 10 stddev 2
        n = 8 if h % 2 == 0 else 12
        for k in range(n):
            ts = bucket + timedelta(seconds=60 * k)
            storage.upsert_observations([_post(ts, idx)])
            idx += 1

    # Recent = 22 → z = (22 - 10)/2 = 6 → high band
    _seed_recent_bucket(storage, count=22, end=NOW, idx_start=idx)

    det = MentionSpikeDetector(name="mentions", bucket_seconds=3600,
                               baseline_days=7, z_threshold=3.0, min_volume=5)
    findings = list(det.run(storage, TimeWindow(start=NOW - timedelta(days=10), end=NOW)))
    assert len(findings) == 1
    assert findings[0].severity == Severity.HIGH
    assert findings[0].metadata["z_score"] == pytest.approx(6.0, rel=0.05)


def test_dedup_on_rerun(storage) -> None:
    idx = _seed_flat_baseline(storage, per_hour=2, hours=24 * 7, end=NOW)
    _seed_recent_bucket(storage, count=30, end=NOW, idx_start=idx)
    det = MentionSpikeDetector(name="mentions")
    window = TimeWindow(start=NOW - timedelta(days=10), end=NOW)

    f1 = list(det.run(storage, window))[0]
    f2 = list(det.run(storage, window))[0]
    assert f1.id == f2.id  # deterministic id keyed on (detector, entity, bucket)
    assert storage.insert_finding(f1) is True
    assert storage.insert_finding(f2) is False

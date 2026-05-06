"""End-to-end keyword_spike tests via storage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socmon.config import Keyword
from socmon.detectors.keyword_spike import KeywordSpikeDetector
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


def _post(when: datetime, idx: int, text: str, *, author: str = "u1") -> Observation:
    return Observation(
        id=f"reddit:post:{idx}",
        platform="reddit",
        kind=ObservationKind.POST,
        author_handle=author,
        author_id=f"id_{author}",
        text=text,
        url=f"https://example.test/{idx}",
        created_at=when,
        collected_at=when,
    )


# ---------------------------------------------------------------------------


def test_no_keywords_emits_nothing(storage) -> None:
    det = KeywordSpikeDetector(name="kw", keywords=[])
    window = TimeWindow(start=NOW - timedelta(days=10), end=NOW)
    assert list(det.run(storage, window)) == []


def test_keyword_spike_recent_only_above_min_volume(storage) -> None:
    # Flat-zero baseline for this keyword. Recent bucket has 5 matching posts.
    for k in range(5):
        ts = NOW - timedelta(minutes=30) + timedelta(seconds=k)
        storage.upsert_observations([_post(ts, k, "Acme suffered a major breach today",
                                            author=f"reporter_{k}")])

    det = KeywordSpikeDetector(
        name="kw",
        keywords=[Keyword(expr="acme AND breach", severity=Severity.HIGH,
                          label="acme-breach")],
        bucket_seconds=3600, baseline_days=7,
        z_threshold=3.0, min_volume=3,
    )
    findings = list(det.run(storage, TimeWindow(start=NOW - timedelta(days=10), end=NOW)))
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == FindingKind.KEYWORD_SPIKE
    # inf z + critical band → bump from configured HIGH to CRITICAL.
    assert f.severity == Severity.CRITICAL
    assert f.metadata["keyword_label"] == "acme-breach"
    assert f.metadata["current"] == 5
    # All 5 reporters show up in top_authors.
    assert len(f.metadata["top_authors"]) == 5


def test_below_min_volume_is_quiet(storage) -> None:
    for k in range(2):  # below min_volume
        ts = NOW - timedelta(minutes=30) + timedelta(seconds=k)
        storage.upsert_observations([_post(ts, k, "Acme breach")])
    det = KeywordSpikeDetector(
        name="kw",
        keywords=[Keyword(expr="acme AND breach", severity=Severity.HIGH)],
        min_volume=3,
    )
    assert list(det.run(storage, TimeWindow(start=NOW - timedelta(days=10), end=NOW))) == []


def test_severity_floor_respected_for_low_z(storage) -> None:
    # Build a baseline that gives the keyword enough mean+stddev that recent=5
    # is a medium-band z (z ~ 3-4), then verify configured HIGH stays HIGH.
    # Baseline buckets h=2..169: alternate 2 and 4 matches → mean=3, stddev=1.
    idx = 0
    for h in range(2, 24 * 7 + 2):
        bucket = NOW - timedelta(hours=h)
        n = 2 if h % 2 == 0 else 4
        for k in range(n):
            ts = bucket + timedelta(seconds=k * 60)
            storage.upsert_observations([_post(ts, idx, "Acme breach reported")])
            idx += 1
    # Recent: 6 matches → z = (6-3)/1 = 3 → medium band, but configured HIGH wins.
    for k in range(6):
        ts = NOW - timedelta(minutes=30) + timedelta(seconds=k)
        storage.upsert_observations([_post(ts, idx, "Acme breach reported",
                                            author=f"a{k}")])
        idx += 1

    det = KeywordSpikeDetector(
        name="kw",
        keywords=[Keyword(expr="acme AND breach", severity=Severity.HIGH)],
        bucket_seconds=3600, min_volume=3,
        z_threshold=3.0, z_high=5.0, z_critical=8.0,
    )
    findings = list(det.run(storage, TimeWindow(start=NOW - timedelta(days=10), end=NOW)))
    assert len(findings) == 1
    f = findings[0]
    # Configured HIGH > spike's MEDIUM; z<critical so no bump → HIGH.
    assert f.severity == Severity.HIGH


def test_invalid_expression_skipped_without_crashing(storage) -> None:
    det = KeywordSpikeDetector(
        name="kw",
        keywords=[
            Keyword(expr="AND broken", severity=Severity.MEDIUM),  # parse error
            Keyword(expr="acme", severity=Severity.LOW),
        ],
    )
    # Only the valid expression remains parsed; storage is empty so nothing fires.
    assert len(det._parsed) == 1
    assert det._parsed[0][0].expr == "acme"


def test_keyword_does_not_match_unrelated_text(storage) -> None:
    # Recent bucket has 10 posts but none match the keyword.
    for k in range(10):
        ts = NOW - timedelta(minutes=30) + timedelta(seconds=k)
        storage.upsert_observations([_post(ts, k, "Globex announced earnings")])

    det = KeywordSpikeDetector(
        name="kw",
        keywords=[Keyword(expr="acme AND breach", severity=Severity.HIGH)],
        min_volume=3,
    )
    assert list(det.run(storage, TimeWindow(start=NOW - timedelta(days=10), end=NOW))) == []


def test_keyword_dedup_on_rerun(storage) -> None:
    for k in range(5):
        ts = NOW - timedelta(minutes=30) + timedelta(seconds=k)
        storage.upsert_observations([_post(ts, k, "Acme breach reported")])

    det = KeywordSpikeDetector(
        name="kw",
        keywords=[Keyword(expr="acme AND breach", severity=Severity.HIGH)],
        min_volume=3,
    )
    window = TimeWindow(start=NOW - timedelta(days=10), end=NOW)
    f1 = list(det.run(storage, window))[0]
    f2 = list(det.run(storage, window))[0]
    assert f1.id == f2.id
    assert storage.insert_finding(f1) is True
    assert storage.insert_finding(f2) is False

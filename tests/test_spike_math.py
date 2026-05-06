"""Spike math tests — table-driven, no I/O."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from socmon.detectors._spike_math import (
    baseline_stats,
    bucket_counts,
    bucket_floor,
    bump_severity,
    spike_severity,
    z_score,
)
from socmon.models import Severity


UTC = timezone.utc


def test_bucket_floor_hour() -> None:
    ts = datetime(2026, 5, 6, 12, 37, 49, tzinfo=UTC)
    assert bucket_floor(ts, 3600) == datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)


def test_bucket_floor_day() -> None:
    ts = datetime(2026, 5, 6, 12, 37, 49, tzinfo=UTC)
    assert bucket_floor(ts, 86400) == datetime(2026, 5, 6, 0, 0, 0, tzinfo=UTC)


def test_bucket_floor_naive_treated_as_utc() -> None:
    ts = datetime(2026, 5, 6, 12, 37, 49)  # no tz
    assert bucket_floor(ts, 3600).hour == 12


def test_bucket_counts_includes_empty_buckets() -> None:
    start = datetime(2026, 5, 6, 0, tzinfo=UTC)
    end = datetime(2026, 5, 6, 4, tzinfo=UTC)
    timestamps = [
        datetime(2026, 5, 6, 0, 30, tzinfo=UTC),
        datetime(2026, 5, 6, 0, 45, tzinfo=UTC),
        datetime(2026, 5, 6, 3, 5, tzinfo=UTC),
    ]
    counts = bucket_counts(timestamps, start, end, 3600)
    assert sorted(counts) == [
        datetime(2026, 5, 6, 0, tzinfo=UTC),
        datetime(2026, 5, 6, 1, tzinfo=UTC),
        datetime(2026, 5, 6, 2, tzinfo=UTC),
        datetime(2026, 5, 6, 3, tzinfo=UTC),
    ]
    assert counts[datetime(2026, 5, 6, 0, tzinfo=UTC)] == 2
    assert counts[datetime(2026, 5, 6, 1, tzinfo=UTC)] == 0
    assert counts[datetime(2026, 5, 6, 3, tzinfo=UTC)] == 1


def test_bucket_counts_drops_out_of_window() -> None:
    start = datetime(2026, 5, 6, 1, tzinfo=UTC)
    end = datetime(2026, 5, 6, 3, tzinfo=UTC)
    timestamps = [datetime(2026, 5, 6, 0, 30, tzinfo=UTC)]  # before start
    counts = bucket_counts(timestamps, start, end, 3600)
    assert all(v == 0 for v in counts.values())


def test_baseline_stats_excludes_last_bucket() -> None:
    base = datetime(2026, 5, 6, tzinfo=UTC)
    counts = {
        base + timedelta(hours=h): v
        for h, v in enumerate([2, 2, 2, 2, 100])  # last bucket is the "spike"
    }
    mean, stddev = baseline_stats(counts, exclude_last=1)
    assert mean == 2.0
    assert stddev == 0.0


def test_baseline_stats_too_few_buckets() -> None:
    base = datetime(2026, 5, 6, tzinfo=UTC)
    counts = {base: 5}
    assert baseline_stats(counts, exclude_last=1) == (0.0, 0.0)


def test_z_score_flat_baseline_with_jump_is_inf() -> None:
    assert z_score(10, mean=0, stddev=0) == float("inf")


def test_z_score_flat_baseline_no_jump_is_zero() -> None:
    assert z_score(0, mean=0, stddev=0) == 0.0


def test_z_score_normal() -> None:
    z = z_score(10, mean=4, stddev=2)
    assert z == pytest.approx(3.0)


@pytest.mark.parametrize("z,expected", [
    (2.5, None),
    (3.0, Severity.MEDIUM),
    (4.99, Severity.MEDIUM),
    (5.0, Severity.HIGH),
    (7.99, Severity.HIGH),
    (8.0, Severity.CRITICAL),
    (math.inf, Severity.CRITICAL),
])
def test_spike_severity_default_thresholds(z: float, expected) -> None:
    assert spike_severity(z) == expected


def test_spike_severity_custom_thresholds() -> None:
    # Stricter detector can lower thresholds.
    assert spike_severity(2.5, medium=2.0, high=4.0, critical=6.0) == Severity.MEDIUM


@pytest.mark.parametrize("base,levels,expected", [
    (Severity.LOW, 1, Severity.MEDIUM),
    (Severity.MEDIUM, 1, Severity.HIGH),
    (Severity.HIGH, 1, Severity.CRITICAL),
    (Severity.CRITICAL, 1, Severity.CRITICAL),  # capped
    (Severity.LOW, 3, Severity.CRITICAL),
])
def test_bump_severity(base, levels, expected) -> None:
    assert bump_severity(base, levels) == expected

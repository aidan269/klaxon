"""Pure functions for bucketed spike detection — used by mention_spike and
keyword_spike. Kept separate so tests can hit the math without going through
storage or detector wiring.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Iterable

from socmon.models import Severity


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


def bucket_floor(ts: datetime, bucket_seconds: int) -> datetime:
    """Floor `ts` to the start of its bucket (UTC)."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    epoch = int(ts.timestamp())
    floored = epoch - (epoch % bucket_seconds)
    return datetime.fromtimestamp(floored, tz=timezone.utc)


def bucket_counts(
    timestamps: Iterable[datetime],
    start: datetime,
    end: datetime,
    bucket_seconds: int,
) -> dict[datetime, int]:
    """Count timestamps per bucket from `start` (inclusive) to `end` (exclusive).

    Empty buckets are present in the result with value 0 — important for the
    spike math, since "we got 5 mentions in an hour where we usually get 0" is
    only meaningful if the zero-buckets are included in the baseline.
    """
    counts: dict[datetime, int] = {}
    cur = bucket_floor(start, bucket_seconds)
    last = bucket_floor(end - timedelta(microseconds=1), bucket_seconds)
    while cur <= last:
        counts[cur] = 0
        cur += timedelta(seconds=bucket_seconds)
    for ts in timestamps:
        b = bucket_floor(ts, bucket_seconds)
        if b in counts:
            counts[b] += 1
    return counts


# ---------------------------------------------------------------------------
# Baseline + z-score
# ---------------------------------------------------------------------------


def baseline_stats(
    counts: dict[datetime, int],
    *,
    exclude_last: int = 1,
) -> tuple[float, float]:
    """(mean, stddev) over baseline buckets, excluding the last `exclude_last`.

    Population stddev (divide by N, not N-1). With N>=24 the difference is
    negligible and population is what online anomaly detectors typically use.
    """
    sorted_buckets = sorted(counts)
    if len(sorted_buckets) <= exclude_last:
        return 0.0, 0.0
    values = [counts[b] for b in sorted_buckets[:-exclude_last]]
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, math.sqrt(variance)


def z_score(value: float, mean: float, stddev: float) -> float:
    """Z-score with epsilon. If the baseline stddev is ~zero and the current
    value is positive, returns +inf — we still want to flag a "0→N" jump.
    """
    if stddev < 1e-9:
        if value > mean:
            return float("inf")
        return 0.0
    return (value - mean) / stddev


def spike_severity(
    z: float,
    *,
    medium: float = 3.0,
    high: float = 5.0,
    critical: float = 8.0,
) -> Severity | None:
    """Map a z-score onto a severity band. None means "not a spike."""
    if z >= critical:
        return Severity.CRITICAL
    if z >= high:
        return Severity.HIGH
    if z >= medium:
        return Severity.MEDIUM
    return None


def bump_severity(base: Severity, levels: int = 1) -> Severity:
    """Move `base` up by `levels` (capped at CRITICAL). Used by keyword_spike
    to bump configured baseline severity when z is extreme.
    """
    order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
    idx = order.index(base)
    return order[min(idx + levels, len(order) - 1)]

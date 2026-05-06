"""Mention spike detector.

Counts POST/COMMENT observations bucketed in time, treats the most recent
bucket as "current", and asks: is `current` a statistically significant
deviation from the prior `baseline_days` of buckets?

Implementation notes:
  - We assume collectors only ingest brand-relevant content (they're given the
    brand keywords as `CollectorQuery.keywords`). Therefore *every* POST in
    storage counts as a mention. If you want a stricter mention definition
    later, add a per-detector text filter via options.
  - Bucketing happens in Python over query results. For SQLite this is fine
    up to ~hundreds of thousands of observations per baseline window. If
    we outgrow that, push the bucketing into SQL via a date_trunc/strftime
    aggregate — the math helpers don't change.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterator

from socmon.dedup import finding_id
from socmon.detectors import register
from socmon.detectors._spike_math import (
    baseline_stats,
    bucket_counts,
    spike_severity,
    z_score,
)
from socmon.interfaces import Detector
from socmon.models import (
    EvidenceRef,
    Finding,
    FindingKind,
    ObservationKind,
    TimeWindow,
)
from socmon.storage.base import Storage

log = logging.getLogger(__name__)


@register("mention_spike")
class MentionSpikeDetector(Detector):
    name = "mention_spike"

    def __init__(self, name: str | None = None, **options) -> None:
        if name:
            self.name = name
        self.options = options
        self.bucket_seconds: int = int(options.get("bucket_seconds", 3600))  # 1h default
        self.baseline_days: int = int(options.get("baseline_days", 7))
        self.z_threshold: float = float(options.get("z_threshold", 3.0))
        self.z_high: float = float(options.get("z_high", 5.0))
        self.z_critical: float = float(options.get("z_critical", 8.0))
        self.min_volume: int = int(options.get("min_volume", 5))
        # Optional: scope to a single platform (one detector per platform if you
        # want per-platform routing).
        self.platform: str | None = options.get("platform")
        # POST is the default; some operators may want to include COMMENTs too.
        self.kinds: list[str] = list(options.get("kinds", [ObservationKind.POST.value]))

    # -----------------------------------------------------------------

    def run(self, storage: Storage, window: TimeWindow) -> Iterator[Finding]:
        baseline_start = window.end - timedelta(days=self.baseline_days)

        # Single pass over the baseline window — pull just the timestamps.
        timestamps: list[datetime] = []
        for kind in self.kinds:
            for obs in storage.query_observations(
                kind=kind,
                platform=self.platform,
                since=baseline_start,
                until=window.end,
            ):
                timestamps.append(obs.created_at)

        if not timestamps:
            return

        counts = bucket_counts(timestamps, baseline_start, window.end, self.bucket_seconds)
        sorted_buckets = sorted(counts)
        if len(sorted_buckets) < 2:
            return

        last_bucket = sorted_buckets[-1]
        current = counts[last_bucket]
        if current < self.min_volume:
            return

        mean, stddev = baseline_stats(counts, exclude_last=1)
        z = z_score(current, mean, stddev)
        sev = spike_severity(z, medium=self.z_threshold, high=self.z_high,
                             critical=self.z_critical)
        if sev is None:
            return

        # Pull a few representative observations from the spike bucket as evidence.
        # Recent + per-author dedup so a single ranter can't dominate the evidence list.
        evidence, top_authors = self._evidence_for_bucket(
            storage, last_bucket, self.bucket_seconds
        )

        z_display = "inf" if z == float("inf") else f"{z:.1f}"
        score = min(100.0, 30.0 + (z if z != float("inf") else 30) * 7.0)
        platform_label = self.platform or "all platforms"
        bucket_label = _bucket_label(self.bucket_seconds)

        fid = finding_id(
            detector=self.name,
            entity_key=f"mention:{self.platform or 'all'}",
            bucket_start=last_bucket,
            bucket_seconds=self.bucket_seconds,
        )
        yield Finding(
            id=fid,
            kind=FindingKind.MENTION_SPIKE,
            detector=self.name,
            severity=sev,
            score=round(score, 2),
            title=(
                f"Mention spike on {platform_label}: "
                f"{current} in last {bucket_label} (z={z_display}, baseline mean={mean:.1f})"
            ),
            summary=(
                f"Recent bucket [{last_bucket.isoformat()}] saw {current} mentions "
                f"vs baseline mean {mean:.1f} (stddev {stddev:.1f}) over the prior "
                f"{self.baseline_days} days. Top authors driving the spike: "
                f"{', '.join(f'{h} ({n})' for h, n in top_authors[:5]) or 'n/a'}."
            ),
            detected_at=datetime.now(timezone.utc),
            window_start=last_bucket,
            window_end=last_bucket + timedelta(seconds=self.bucket_seconds),
            evidence=evidence,
            metadata={
                "current": current,
                "baseline_mean": round(mean, 4),
                "baseline_stddev": round(stddev, 4),
                "z_score": (None if z == float("inf") else round(z, 4)),
                "z_score_inf": z == float("inf"),
                "platform": self.platform,
                "bucket_seconds": self.bucket_seconds,
                "top_authors": top_authors[:10],
            },
        )

    # -----------------------------------------------------------------

    def _evidence_for_bucket(
        self, storage: Storage, bucket_start: datetime, bucket_seconds: int,
    ) -> tuple[list[EvidenceRef], list[tuple[str, int]]]:
        bucket_end = bucket_start + timedelta(seconds=bucket_seconds)
        seen_authors: set[str] = set()
        evidence: list[EvidenceRef] = []
        author_counter: Counter[str] = Counter()

        # Pull more than we need so per-author dedup still leaves us with 5.
        for kind in self.kinds:
            for obs in storage.query_observations(
                kind=kind,
                platform=self.platform,
                since=bucket_start,
                until=bucket_end,
                limit=200,
            ):
                if obs.author_handle:
                    author_counter[obs.author_handle] += 1
                if len(evidence) < 5:
                    handle = obs.author_handle or "?"
                    if handle not in seen_authors:
                        seen_authors.add(handle)
                        evidence.append(EvidenceRef(
                            observation_id=obs.id,
                            platform=obs.platform,
                            url=str(obs.url) if obs.url else None,
                            snippet=(obs.text or "")[:200],
                        ))
        return evidence, author_counter.most_common()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bucket_label(bucket_seconds: int) -> str:
    if bucket_seconds % 86400 == 0:
        return f"{bucket_seconds // 86400}d"
    if bucket_seconds % 3600 == 0:
        return f"{bucket_seconds // 3600}h"
    if bucket_seconds % 60 == 0:
        return f"{bucket_seconds // 60}m"
    return f"{bucket_seconds}s"

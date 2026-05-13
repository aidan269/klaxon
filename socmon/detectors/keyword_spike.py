"""Keyword spike detector.

Same baseline + z-score math as mention_spike, but applied per configured
`Keyword` expression. The keyword's *configured* severity is the floor; an
extreme z-score (default >=8) bumps the finding one level up.

We do a single pass over observations in the baseline window and evaluate
every keyword expression against each one — that way we read storage once
no matter how many keywords are configured.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterator

from socmon.config import Keyword
from socmon.dedup import finding_id
from socmon.detectors import register
from socmon.detectors._keyword_dsl import ParsedExpr, evaluate, parse
from socmon.detectors._spike_math import (
    baseline_stats,
    bucket_counts,
    bump_severity,
    spike_severity,
    z_score,
)
from socmon.interfaces import Detector
from socmon.models import (
    EvidenceRef,
    Finding,
    FindingKind,
    Observation,
    ObservationKind,
    Severity,
    TimeWindow,
)
from socmon.storage.base import Storage

log = logging.getLogger(__name__)


@register("keyword_spike")
class KeywordSpikeDetector(Detector):
    name = "keyword_spike"

    def __init__(self, keywords: list[Keyword] | None = None,
                 name: str | None = None, **options) -> None:
        if name:
            self.name = name
        self.keywords = keywords or []
        self.options = options
        self.bucket_seconds: int = int(options.get("bucket_seconds", 3600))
        self.baseline_days: int = int(options.get("baseline_days", 7))
        self.z_threshold: float = float(options.get("z_threshold", 3.0))
        self.z_high: float = float(options.get("z_high", 5.0))
        self.z_critical: float = float(options.get("z_critical", 8.0))
        self.min_volume: int = int(options.get("min_volume", 3))
        self.kinds: list[str] = list(options.get("kinds", [ObservationKind.POST.value]))
        # Pre-parse expressions; bad ones are dropped with a warning rather than
        # killing the whole run.
        self._parsed: list[tuple[Keyword, ParsedExpr]] = []
        for kw in self.keywords:
            try:
                self._parsed.append((kw, parse(kw.expr)))
            except ValueError as e:
                log.warning("keyword_spike: skipping invalid expression %r: %s",
                            kw.expr, e)

    # -----------------------------------------------------------------

    def run(self, storage: Storage, window: TimeWindow) -> Iterator[Finding]:
        if not self._parsed:
            return

        baseline_start = window.end - timedelta(days=self.baseline_days)

        # Per-keyword timestamp + recent-observations buffers, populated in one pass.
        per_kw_ts: dict[str, list[datetime]] = {kw.expr: [] for kw, _ in self._parsed}
        per_kw_recent: dict[str, list[Observation]] = {kw.expr: [] for kw, _ in self._parsed}

        # The bucket containing window.end is the "current" one; we identify it
        # below to know which observations to keep as evidence candidates.
        from socmon.detectors._spike_math import bucket_floor
        recent_bucket_start = bucket_floor(
            window.end - timedelta(microseconds=1), self.bucket_seconds,
        )
        recent_bucket_end = recent_bucket_start + timedelta(seconds=self.bucket_seconds)

        for kind in self.kinds:
            for obs in storage.query_observations(
                kind=kind,
                since=baseline_start,
                until=window.end,
            ):
                text = obs.text or ""
                if not text:
                    continue
                in_recent = recent_bucket_start <= obs.created_at < recent_bucket_end
                for kw, parsed in self._parsed:
                    try:
                        if evaluate(parsed, text):
                            per_kw_ts[kw.expr].append(obs.created_at)
                            if in_recent and len(per_kw_recent[kw.expr]) < 50:
                                per_kw_recent[kw.expr].append(obs)
                    except Exception as e:
                        # A bad expression at runtime (shouldn't happen since we
                        # pre-parsed) — skip the keyword for this observation.
                        log.debug("keyword eval error %r: %s", kw.expr, e)

        for kw, _ in self._parsed:
            yield from self._evaluate_keyword(
                kw,
                timestamps=per_kw_ts[kw.expr],
                recent=per_kw_recent[kw.expr],
                baseline_start=baseline_start,
                window_end=window.end,
            )

    # -----------------------------------------------------------------

    def _evaluate_keyword(
        self,
        kw: Keyword,
        *,
        timestamps: list[datetime],
        recent: list[Observation],
        baseline_start: datetime,
        window_end: datetime,
    ) -> Iterator[Finding]:
        if not timestamps:
            return

        counts = bucket_counts(timestamps, baseline_start, window_end, self.bucket_seconds)
        sorted_buckets = sorted(counts)
        if len(sorted_buckets) < 2:
            return

        last_bucket = sorted_buckets[-1]
        current = counts[last_bucket]
        if current < self.min_volume:
            return

        mean, stddev = baseline_stats(counts, exclude_last=1)
        z = z_score(current, mean, stddev)
        # If z is below threshold, no finding — even if the keyword has high
        # configured severity, we only fire on actual anomalies.
        if z < self.z_threshold:
            return

        # Severity = max(configured, spike-band). Bump one level if z >= z_critical.
        spike_sev = spike_severity(z, medium=self.z_threshold, high=self.z_high,
                                   critical=self.z_critical)
        # spike_sev can't be None here (we just checked z >= z_threshold).
        sev = _max_severity(kw.severity, spike_sev) if spike_sev else kw.severity
        if z >= self.z_critical:
            sev = bump_severity(sev, levels=1)

        # Top authors among the recent matches.
        author_counter: Counter[str] = Counter()
        for obs in recent:
            if obs.author_handle:
                author_counter[obs.author_handle] += 1

        # Evidence: 5 most recent observations, deduped by author.
        evidence: list[EvidenceRef] = []
        seen_authors: set[str] = set()
        for obs in sorted(recent, key=lambda o: o.created_at, reverse=True):
            handle = obs.author_handle or "?"
            if handle in seen_authors:
                continue
            seen_authors.add(handle)
            evidence.append(EvidenceRef(
                observation_id=obs.id,
                platform=obs.platform,
                url=str(obs.url) if obs.url else None,
                snippet=(obs.text or "")[:200],
            ))
            if len(evidence) >= 5:
                break

        z_display = "inf" if z == float("inf") else f"{z:.1f}"
        score = min(100.0, 30.0 + (z if z != float("inf") else 30) * 7.0)
        label = kw.label or kw.expr

        fid = finding_id(
            detector=self.name,
            entity_key=f"keyword:{kw.expr}",
            bucket_start=last_bucket,
            bucket_seconds=self.bucket_seconds,
        )
        yield Finding(
            id=fid,
            kind=FindingKind.KEYWORD_SPIKE,
            detector=self.name,
            severity=sev,
            score=round(score, 2),
            title=(
                f"Keyword spike {label!r}: {current} in last "
                f"{_bucket_label(self.bucket_seconds)} (z={z_display})"
            ),
            summary=(
                f"Expression: {kw.expr!r}. "
                f"Recent bucket [{last_bucket.isoformat()}] saw {current} matches "
                f"vs baseline mean {mean:.1f} (stddev {stddev:.1f}) over the prior "
                f"{self.baseline_days} days. "
                f"Top authors: {', '.join(f'{h} ({n})' for h, n in author_counter.most_common(5)) or 'n/a'}."
            ),
            detected_at=datetime.now(timezone.utc),
            window_start=last_bucket,
            window_end=last_bucket + timedelta(seconds=self.bucket_seconds),
            evidence=evidence,
            metadata={
                "keyword_expr": kw.expr,
                "keyword_label": kw.label,
                "configured_severity": kw.severity.value,
                "current": current,
                "baseline_mean": round(mean, 4),
                "baseline_stddev": round(stddev, 4),
                "z_score": (None if z == float("inf") else round(z, 4)),
                "z_score_inf": z == float("inf"),
                "bucket_seconds": self.bucket_seconds,
                "top_authors": author_counter.most_common(10),
            },
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _max_severity(a: Severity, b: Severity) -> Severity:
    order = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
    return a if order.index(a) >= order.index(b) else b


def _bucket_label(bucket_seconds: int) -> str:
    if bucket_seconds % 86400 == 0:
        return f"{bucket_seconds // 86400}d"
    if bucket_seconds % 3600 == 0:
        return f"{bucket_seconds // 3600}h"
    if bucket_seconds % 60 == 0:
        return f"{bucket_seconds // 60}m"
    return f"{bucket_seconds}s"

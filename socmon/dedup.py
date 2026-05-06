"""Deterministic Finding ID construction.

Two findings are "the same" if they're produced by the same detector, about the
same entity, in the same time bucket. The id is sha256 of those parts so dedup
works across process restarts and across replicas.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def finding_id(
    detector: str,
    entity_key: str,
    bucket_start: datetime,
    bucket_seconds: int,
) -> str:
    bucket_start = bucket_start.astimezone(timezone.utc)
    parts = f"{detector}|{entity_key}|{bucket_start.isoformat()}|{bucket_seconds}"
    return hashlib.sha256(parts.encode()).hexdigest()[:32]

"""Sanity check: finding_id is deterministic and bucket-sensitive."""

from datetime import datetime, timezone

from socmon.dedup import finding_id


def test_finding_id_stable() -> None:
    a = finding_id("mention_spike", "acme", datetime(2026, 5, 6, 12, tzinfo=timezone.utc), 3600)
    b = finding_id("mention_spike", "acme", datetime(2026, 5, 6, 12, tzinfo=timezone.utc), 3600)
    assert a == b


def test_finding_id_differs_by_bucket() -> None:
    a = finding_id("mention_spike", "acme", datetime(2026, 5, 6, 12, tzinfo=timezone.utc), 3600)
    b = finding_id("mention_spike", "acme", datetime(2026, 5, 6, 13, tzinfo=timezone.utc), 3600)
    assert a != b

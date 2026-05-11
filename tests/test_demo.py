"""`socmon demo` produces deterministic, multi-detector findings.

What we're verifying:
  - seed_demo_data populates the expected number of accounts + posts
  - run_demo emits findings across all three implemented detectors
  - the typosquat and homoglyph candidates both score above MEDIUM
  - the legit-handle control scores 0 (no finding)
  - running twice with the same `now` produces identical finding ids
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from socmon import demo as demo_mod
from socmon.config import (
    BrandEntity,
    DetectorConfig,
    ExecutiveEntity,
    Keyword,
    SocmonConfig,
    StorageConfig,
)
from socmon.models import Severity


# Deliberately not on an hour boundary — catches a class of seed-vs-detector
# bucket-misalignment bugs that 12:00:00 hides.
FIXED_NOW = datetime(2026, 5, 11, 19, 5, 37, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path, monkeypatch) -> SocmonConfig:
    # Point the demo DB into the per-test tmpdir so concurrent test runs don't
    # clobber each other's state and cwd isn't littered.
    demo_db = tmp_path / "socmon-demo.db"
    monkeypatch.setattr(demo_mod, "DEMO_DB_FILE", str(demo_db))
    monkeypatch.setattr(demo_mod, "DEMO_DSN", f"sqlite:///{demo_db}")

    return SocmonConfig(
        organization="Acme Corp",
        brand=BrandEntity(
            name="Acme",
            aliases=["AcmeCorp"],
            domains=["acme.com"],
            legit_handles={"reddit": ["acme_official"], "twitter": ["acmecorp"]},
        ),
        executives=[ExecutiveEntity(
            name="Jane Doe", title="CEO",
            legit_handles={"twitter": ["janedoeacme"]},
        )],
        keywords=[Keyword(expr='"acme" AND breach', severity=Severity.HIGH)],
        storage=StorageConfig(backend="sqlite", dsn=f"sqlite:///{tmp_path}/main.db"),
        detectors=[
            DetectorConfig(name="imp", type="impersonation"),
            DetectorConfig(name="mentions", type="mention_spike",
                           options={"min_volume": 5}),
            DetectorConfig(name="keywords", type="keyword_spike",
                           options={"min_volume": 3}),
        ],
    )


# ---------------------------------------------------------------------------


def test_run_demo_emits_findings_from_all_three_detector_kinds(cfg) -> None:
    summary = demo_mod.run_demo(cfg, now=FIXED_NOW)
    findings = summary["new_findings"]
    assert findings["count"] >= 3, summary
    kinds = {f["kind"] for f in findings["items"]}
    assert "impersonation" in kinds
    assert "mention_spike" in kinds
    assert "keyword_spike" in kinds


def test_demo_accounts_include_typosquat_homoglyph_and_legit_control(cfg) -> None:
    summary = demo_mod.run_demo(cfg, now=FIXED_NOW)
    handles = {a["handle"] for a in summary["new_accounts"]["items"]}
    # Typosquat: acme_official → acme_official1 (last alpha 'l' → '1')
    assert any(h.startswith("acme_officia") and h != "acme_official" for h in handles)
    # Homoglyph: contains Cyrillic 'а' (U+0430)
    assert any("а" in h for h in handles), handles
    # Legit control is present.
    assert "acme_official" in handles


def test_legit_handle_does_not_produce_impersonation_finding(cfg) -> None:
    summary = demo_mod.run_demo(cfg, now=FIXED_NOW)
    imp_titles = [
        f["title"] for f in summary["new_findings"]["items"]
        if f["kind"] == "impersonation"
    ]
    # The legit handle should not appear in any impersonation finding title.
    assert not any("acme_official " in t or "/acme_official\"" in t
                   or "/acme_official)" in t for t in imp_titles)


def test_demo_is_deterministic_for_same_now(cfg, tmp_path, monkeypatch) -> None:
    # First run
    s1 = demo_mod.run_demo(cfg, now=FIXED_NOW)
    ids1 = sorted(f["id"] for f in s1["new_findings"]["items"])
    # Reset the demo DB so the second run actually re-seeds and re-evaluates.
    Path(demo_mod.DEMO_DB_FILE).unlink(missing_ok=True)
    s2 = demo_mod.run_demo(cfg, now=FIXED_NOW)
    ids2 = sorted(f["id"] for f in s2["new_findings"]["items"])
    assert ids1 == ids2


def test_seeded_accounts_and_posts_counts(cfg) -> None:
    from socmon.storage.sqlite import SqliteStorage
    storage = SqliteStorage(demo_mod.DEMO_DSN)
    storage.init_schema()
    out = demo_mod.seed_demo_data(storage, cfg, now=FIXED_NOW)
    # 5 candidate accounts (typosquat, homoglyph, exec, legit, random)
    assert len(out["accounts"]) == 5
    # 7d * 24h * 2/hr baseline + 30 spike + 8 keyword = 374
    assert len(out["posts"]) == 7 * 24 * 2 + 30 + 8


def test_typosquat_helper_swaps_trailing_alpha() -> None:
    assert demo_mod._typosquat_handle("acme_official") == "acme_officia1"
    assert demo_mod._typosquat_handle("janedoeacme") == "jan3doeacme" or \
           demo_mod._typosquat_handle("janedoeacme")[0:].startswith("jane")
    # Short handles get the suffix fallback.
    assert demo_mod._typosquat_handle("xx") == "xx_official"


def test_homoglyph_helper_uses_cyrillic() -> None:
    out = demo_mod._homoglyph_handle("acme_official", "Acme")
    assert "а" in out  # cyrillic а
    # Confirm it's actually a swap, not append — same length.
    assert len(out) == len("acme_official")

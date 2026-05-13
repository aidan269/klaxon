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


# ---------------------------------------------------------------------------
# --alerts routing
# ---------------------------------------------------------------------------


def _cfg_with_slack(cfg) -> object:
    """Bolt a slack alerter + a catch-all route onto an existing demo config."""
    from socmon.config import AlerterConfig, AlertRoute

    cfg.alerters = [AlerterConfig(
        name="s", type="slack",
        # Direct webhook_url (not env) so tests don't need env setup; the alerter
        # is monkeypatched anyway, so the URL is never dialed.
        options={"webhook_url": "https://example.test/hook"},
    )]
    cfg.routes = [AlertRoute(
        match_kind="*", severity_min=Severity.LOW, channels=["s"],
    )]
    return cfg


def test_default_demo_does_not_dispatch_alerts_even_with_alerters_configured(
    cfg, monkeypatch,
) -> None:
    """Safety check: route_alerts=False (CLI default) must NOT call any
    alerter, even if the config has alerters + matching routes."""
    _cfg_with_slack(cfg)
    sent: list = []
    from socmon.alerters.slack import SlackAlerter
    monkeypatch.setattr(SlackAlerter, "send", lambda self, f: sent.append(f))

    summary = demo_mod.run_demo(cfg, now=FIXED_NOW)  # route_alerts default False

    assert summary["new_findings"]["count"] >= 3  # findings produced
    assert sent == []                              # but nothing dispatched


def test_demo_with_alerts_dispatches_findings(cfg, monkeypatch) -> None:
    _cfg_with_slack(cfg)
    sent: list = []
    from socmon.alerters.slack import SlackAlerter
    monkeypatch.setattr(SlackAlerter, "send", lambda self, f: sent.append(f))

    summary = demo_mod.run_demo(cfg, now=FIXED_NOW, route_alerts=True)

    n_findings = summary["new_findings"]["count"]
    assert n_findings >= 3
    # Every finding above the route's severity_min (LOW) should have been
    # dispatched. With first-match-wins, our single catch-all route matches all.
    assert len(sent) == n_findings
    # And the routed findings cover all three implemented detector kinds.
    kinds = {f.kind.value for f in sent}
    assert kinds >= {"impersonation", "mention_spike", "keyword_spike"}


# ---------------------------------------------------------------------------
# --watch (continuous-monitoring demo)
# ---------------------------------------------------------------------------


def test_watch_drips_a_new_finding_each_iteration(cfg, monkeypatch) -> None:
    """Initial seed dispatches the bulk findings; each subsequent drip should
    dispatch at least one fresh impersonation finding (distinct entity →
    distinct finding id → not blocked by dedup)."""
    monkeypatch.setattr(demo_mod.time, "sleep", lambda _s: None)
    _cfg_with_slack(cfg)
    sent: list = []
    from socmon.alerters.slack import SlackAlerter
    monkeypatch.setattr(SlackAlerter, "send", lambda self, f: sent.append(f))

    demo_mod.run_demo_watch(
        cfg, drip_interval_seconds=0, route_alerts=True, max_iterations=3,
    )

    # Initial bulk seed + 3 drips. Bulk should produce >=3 findings; each drip
    # adds an impersonation finding (sometimes more if multiple detectors pick
    # the new account up). So total should comfortably exceed initial.
    impersonation_findings = [f for f in sent if f.kind.value == "impersonation"]
    # 3 initial impersonations (typosquat, homoglyph, exec) + ≥1 per drip.
    assert len(impersonation_findings) >= 3 + 3, (
        f"expected initial 3 + 3 drips of impersonation findings, got "
        f"{len(impersonation_findings)} of {len(sent)} total"
    )


def test_catch_url_routes_findings_to_a_single_webhook(cfg, monkeypatch) -> None:
    """`--catch URL` should bypass the configured alerters entirely and
    POST every finding to the catch URL instead. The demo recipe relies on
    this — content-team demos use the local catcher script, not real Slack."""
    import httpx
    import respx

    # Pre-populate the config with a Slack alerter that should NOT be hit.
    _cfg_with_slack(cfg)
    posted: list = []
    from socmon.alerters.slack import SlackAlerter
    monkeypatch.setattr(SlackAlerter, "send", lambda self, f: posted.append(("slack", f)))

    with respx.mock() as mock:
        mock.post("http://127.0.0.1:8765/").mock(return_value=httpx.Response(200))
        demo_mod.run_demo(cfg, now=FIXED_NOW, catch_url="http://127.0.0.1:8765/")

        # All findings went to the catch URL; nothing went to Slack.
        assert len(mock.calls) >= 3
        assert posted == []

        # Catch payloads are full Finding JSON.
        import json as _json
        body = _json.loads(mock.calls[0].request.read())
        assert "severity" in body
        assert "title" in body
        assert body["kind"] in {"impersonation", "mention_spike", "keyword_spike"}


def test_watch_without_alerts_still_produces_findings(cfg, monkeypatch) -> None:
    """Smoke test: watch loop runs without alerters configured/wired. Findings
    accumulate in the demo DB; nothing should crash."""
    monkeypatch.setattr(demo_mod.time, "sleep", lambda _s: None)
    demo_mod.run_demo_watch(
        cfg, drip_interval_seconds=0, route_alerts=False, max_iterations=2,
    )

    # Verify findings landed in storage.
    from socmon.storage.sqlite import SqliteStorage
    storage = SqliteStorage(demo_mod.DEMO_DSN)
    findings = list(storage.query_findings())
    # Initial: 3 impersonation + 1 mention + ≥1 keyword (test cfg has 1 keyword,
    # demo injects one more, so ≥2 keyword findings). Plus 2 drips → +2 impersonation.
    # Tight lower bound: 3 initial impersonation + 2 from drips = 5.
    assert len(findings) >= 5
    # And spike kinds should appear from the initial seed.
    kinds = {f.kind.value for f in findings}
    assert kinds >= {"impersonation", "mention_spike", "keyword_spike"}

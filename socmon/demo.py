"""Fixture-based demo data + runner for `socmon demo`.

Seeds a deterministic dataset into a separate database (`socmon-demo.db`)
and runs the detectors over it. Every invocation produces the same findings
regardless of what's happening on Reddit or in RSS feeds today — perfect
for screen-sharing, recordings, and CI smoke tests.

Three detectors fire by design:

  impersonation   →   5 candidate accounts, 3 should score above medium:
                      a typosquat, a homoglyph (Cyrillic 'а'), and an exec
                      impersonation. The legit handle + a random user act
                      as controls and should NOT fire.

  mention_spike   →   168 hours of flat baseline (2 mentions/hr) + a recent
                      30-mention burst → inf z-score → critical.

  keyword_spike   →   8 recent posts matching "{brand} AND breach" against
                      a near-zero baseline → critical band, severity floor
                      bumped from HIGH (configured) by the extreme z.

Safety: the demo uses its own SQLite file (`socmon-demo.db` in cwd) so it
can never touch your real `storage.dsn`. Each run wipes and reseeds, so the
output is repeatable.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from socmon import runner
from socmon.config import Keyword, SocmonConfig
from socmon.detectors._spike_math import bucket_floor
from socmon.models import (
    AccountObservation,
    Observation,
    ObservationKind,
    Severity,
    TimeWindow,
)
from socmon.storage.base import Storage
from socmon.storage.sqlite import SqliteStorage

# 1-hour bucket matches the spike detectors' default; if you change those
# defaults, change this too or the seeded "spike" can straddle two buckets
# and never trip the detector.
_BUCKET_SECONDS = 3600

log = logging.getLogger(__name__)


DEMO_DB_FILE = "socmon-demo.db"
DEMO_DSN = f"sqlite:///{DEMO_DB_FILE}"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_demo(
    cfg: SocmonConfig,
    *,
    now: datetime | None = None,
    route_alerts: bool = False,
) -> dict:
    """Wipe demo DB → seed fixtures → run detectors → return a scan-shaped
    summary dict the CLI can hand straight to `_render_scan_summary`.

    `route_alerts=False` (default) is the safe demo mode: findings are
    produced and returned but never dispatched to Slack / PagerDuty / email
    / webhook. `route_alerts=True` builds the configured alerters and routes
    according to the user's routes table — useful for "show me what this
    actually looks like in our Slack" demos, but fires real messages.
    """
    now = now or datetime.now(timezone.utc)

    # Sqlite-specific wipe; portable enough for v1.
    Path(DEMO_DB_FILE).unlink(missing_ok=True)
    storage = SqliteStorage(DEMO_DSN)
    storage.init_schema()

    seeded = seed_demo_data(storage, cfg, now=now)

    # Inject a guaranteed-matching keyword so keyword_spike has something to fire on
    # regardless of what the user's real config has.
    demo_cfg = cfg.model_copy(deep=True)
    demo_cfg.keywords = list(demo_cfg.keywords) + [
        Keyword(
            expr=f"{cfg.brand.name.lower()} AND breach",
            severity=Severity.HIGH,
            label="demo-breach-chatter",
        )
    ]

    detectors = runner.build_detectors(demo_cfg)
    if route_alerts:
        alerters = runner.build_alerters(demo_cfg)
        routes = demo_cfg.routes
    else:
        # No alerters → run_detectors records findings but never dispatches.
        alerters = {}
        routes = []
    window = TimeWindow(start=now - timedelta(days=30), end=now)
    new_findings = runner.run_detectors(detectors, storage, routes, alerters, window)

    return {
        "window": [window.start.isoformat(), window.end.isoformat()],
        "new_accounts": runner._summarize_accounts(seeded["accounts"], sample=10),
        "new_posts": runner._summarize_posts(seeded["posts"][:10], sample=10),
        "new_findings": runner._summarize_findings(new_findings, sample=10),
        "demo": True,
        "db": DEMO_DB_FILE,
    }


# ---------------------------------------------------------------------------
# Continuous-mode demo: initial seed + periodic drips
# ---------------------------------------------------------------------------


def run_demo_watch(
    cfg: SocmonConfig,
    *,
    drip_interval_seconds: int = 60,
    route_alerts: bool = False,
    max_iterations: int | None = None,
) -> None:
    """Continuous-monitoring demo. Performs the full initial seed (`run_demo`),
    then every `drip_interval_seconds` adds a new impersonation candidate and
    re-runs the detectors so a fresh finding lands in storage (and in Slack,
    if `route_alerts=True`). Ctrl-C exits.

    Designed for live "show me it running" demos: each drip produces ~1 new
    finding so the manager sees a steady stream of alerts, not 7 at t=0 and
    silence after.

    `max_iterations` exists for tests; production callers leave it None and
    rely on Ctrl-C.
    """
    initial = run_demo(cfg, route_alerts=route_alerts)
    log.info(
        "watch: initial seed produced %d finding(s); drip every %ds (Ctrl-C to stop)",
        initial["new_findings"]["count"], drip_interval_seconds,
    )

    storage = SqliteStorage(DEMO_DSN)
    iter_count = 0
    try:
        while max_iterations is None or iter_count < max_iterations:
            time.sleep(drip_interval_seconds)
            iter_count += 1
            now = datetime.now(timezone.utc)

            new_obs = _drip_round(cfg, storage, iter_count=iter_count, now=now)
            log.info("watch: drip %d → seeded %d new candidate(s)", iter_count, len(new_obs))

            new_findings = _run_detectors_for_drip(cfg, storage, now=now,
                                                   route_alerts=route_alerts)
            log.info("watch: drip %d → produced %d new finding(s)",
                     iter_count, len(new_findings))
    except KeyboardInterrupt:
        log.info("watch: stopped after %d drip(s)", iter_count)


def _drip_round(
    cfg: SocmonConfig,
    storage: Storage,
    *,
    iter_count: int,
    now: datetime,
) -> list[AccountObservation]:
    """One new impersonation candidate, varied so each iteration produces a
    genuinely fresh finding (not a dedup'd repeat).

    Impersonations are easier to drip than spikes: each new account is a
    distinct entity → distinct finding id. Spike findings are keyed on time
    buckets, so two spikes in the same hour-bucket dedup to one finding
    regardless of how many fixture posts we add.
    """
    brand = cfg.brand
    platform = next(iter(brand.legit_handles), "reddit")
    legit_list = brand.legit_handles.get(platform, [])
    legit = legit_list[0] if legit_list else brand.name.lower().replace(" ", "")

    # Rotate through impersonation styles so the demo doesn't feel repetitive.
    style_idx = iter_count % 5
    if style_idx == 0:
        base = _typosquat_handle(legit)
    elif style_idx == 1:
        base = _homoglyph_handle(legit, brand.name)
    elif style_idx == 2:
        base = legit + "_support"
    elif style_idx == 3:
        base = legit + "_team"
    else:
        base = legit.replace("_", "") + "_help"

    # Suffix the iteration so the entity is unique across drips.
    handle = f"{base}_{iter_count}"
    account = _make_account(
        now, platform, handle,
        display_name=f"{brand.name} Support",
        bio=f"Official {brand.name} support — DMs open · drip #{iter_count}",
        age_days=3,  # very young → impersonation detector's age signal fires
    )
    storage.upsert_observations([account])
    return [account]


def _run_detectors_for_drip(
    cfg: SocmonConfig, storage: Storage, *,
    now: datetime, route_alerts: bool,
) -> list:
    """Build detectors + alerters fresh each drip so any config-tweaks
    between drips (rare, but supported) take effect."""
    demo_cfg = cfg.model_copy(deep=True)
    demo_cfg.keywords = list(demo_cfg.keywords) + [Keyword(
        expr=f"{cfg.brand.name.lower()} AND breach",
        severity=Severity.HIGH,
        label="demo-breach-chatter",
    )]
    detectors = runner.build_detectors(demo_cfg)
    if route_alerts:
        alerters = runner.build_alerters(demo_cfg)
        routes = demo_cfg.routes
    else:
        alerters, routes = {}, []
    window = TimeWindow(start=now - timedelta(days=30), end=now)
    return runner.run_detectors(detectors, storage, routes, alerters, window)


# ---------------------------------------------------------------------------
# Seeders — each returns the observations it created
# ---------------------------------------------------------------------------


def seed_demo_data(storage: Storage, cfg: SocmonConfig, *, now: datetime) -> dict:
    # Anchor spike posts to the bucket the detector will treat as "current"
    # given window.end=now. The detector picks bucket_floor(window.end - 1us);
    # we mirror that exactly so the spike lands in the right bucket regardless
    # of where in the hour `now` happens to fall.
    last_bucket_start = bucket_floor(
        now - timedelta(microseconds=1), _BUCKET_SECONDS,
    )

    accounts = _make_impersonation_candidates(cfg, now)
    storage.upsert_observations(accounts)

    mention_posts = _make_mention_spike(cfg, now=now, last_bucket_start=last_bucket_start)
    storage.upsert_observations(mention_posts)

    keyword_posts = _make_keyword_spike(cfg, now=now, last_bucket_start=last_bucket_start)
    storage.upsert_observations(keyword_posts)

    return {"accounts": accounts, "posts": mention_posts + keyword_posts}


def _make_impersonation_candidates(cfg: SocmonConfig, now: datetime) -> list[AccountObservation]:
    """Five accounts: typosquat, homoglyph, exec impersonation, legit control,
    random unrelated. Built from whatever brand + execs the user has configured
    so the demo speaks the user's brand language, not 'Acme' specifically."""
    brand = cfg.brand
    # Pick the first platform that has legit handles; default to reddit.
    platform = next(iter(brand.legit_handles), "reddit")
    legit_handles = brand.legit_handles.get(platform, [])
    legit_handle = legit_handles[0] if legit_handles else brand.name.lower().replace(" ", "")

    out: list[AccountObservation] = []

    # 1) Clear typosquat: append a digit / swap trailing letter.
    typosquat = _typosquat_handle(legit_handle)
    out.append(_make_account(
        now, platform, typosquat,
        display_name=f"{brand.name} Official",
        bio=f"The official {brand.name} support account. "
            f"{brand.domains[0] if brand.domains else ''}",
        age_days=10,
    ))

    # 2) Homoglyph: swap first 'a' or 'o' in the brand-handle for a Cyrillic lookalike.
    homoglyph = _homoglyph_handle(legit_handle, brand.name)
    out.append(_make_account(
        now, platform, homoglyph,
        display_name=brand.name,
        bio=f"{brand.name} customer service · DMs open",
        age_days=5,
    ))

    # 3) Exec impersonation (only if we have an exec configured).
    if cfg.executives:
        exec_ = cfg.executives[0]
        exec_platform = next(iter(exec_.legit_handles), platform)
        exec_legit_list = exec_.legit_handles.get(exec_platform, [])
        exec_legit = (
            exec_legit_list[0] if exec_legit_list
            else exec_.name.lower().replace(" ", "")
        )
        exec_typosquat = _typosquat_handle(exec_legit)
        out.append(_make_account(
            now, exec_platform, exec_typosquat,
            display_name=exec_.name,
            bio=f"{exec_.title} of {brand.name}. DMs for partnership inquiries.",
            age_days=7,
        ))

    # 4) Legit handle — should score 0 (control).
    out.append(_make_account(
        now, platform, legit_handle,
        display_name=f"{brand.name} (Verified)",
        bio=f"The official {brand.name} account.",
        age_days=2000,
    ))

    # 5) Random unrelated handle — control, should score low.
    out.append(_make_account(
        now, platform, "cats_4_lyfe_2026",
        display_name="Some Person",
        bio="cat photos, occasional rant",
        age_days=1500,
    ))

    return out


def _make_mention_spike(
    cfg: SocmonConfig, *, now: datetime, last_bucket_start: datetime,
) -> list[Observation]:
    """7 days of low-rate baseline + a recent 30-mention burst that lands in
    the bucket the detector treats as "current"."""
    brand = cfg.brand
    out: list[Observation] = []

    # Baseline: h=2..169 hours before `now`, 2 posts/hr. Skipping h=1 keeps the
    # baseline out of the bucket containing `now`, which is where the spike goes.
    for h in range(2, 24 * 7 + 2):
        bucket = now - timedelta(hours=h)
        for k in range(2):
            ts = bucket + timedelta(minutes=10 * k)
            out.append(_make_post(
                ts, idx=len(out),
                text=f"{brand.name} held a routine product update today.",
                author=f"baseline_{len(out) % 5}",
            ))

    # Spike: 30 posts spread across the current bucket, ending just before `now`.
    spike_count = 30
    authors = ["alice_news", "bob_analyst", "carol_blogger", "dave_anon", "elle_rt"]
    out.extend(_pack_into_bucket(
        n=spike_count, last_bucket_start=last_bucket_start, now=now,
        text_fn=lambda k: (
            f"Anyone else seeing the {brand.name} news? "
            f"This feels like a big deal. ({k})"
        ),
        author_fn=lambda k: authors[k % len(authors)],
        idx_offset=len(out),
    ))
    return out


def _make_keyword_spike(
    cfg: SocmonConfig, *, now: datetime, last_bucket_start: datetime,
) -> list[Observation]:
    """8 recent posts matching '{brand} AND breach' inside the current bucket —
    no baseline, so z=inf and the keyword fires at critical."""
    brand = cfg.brand
    return _pack_into_bucket(
        n=8, last_bucket_start=last_bucket_start, now=now,
        text_fn=lambda k: (
            f"BREAKING: {brand.name} suffered a security breach. "
            f"Customer credentials reportedly posted to a dump site. ({k})"
        ),
        author_fn=lambda k: f"breach_reporter_{k}",
        idx_offset=10_000,  # keep ids distinct from mention_spike posts
    )


def _pack_into_bucket(
    *,
    n: int,
    last_bucket_start: datetime,
    now: datetime,
    text_fn,
    author_fn,
    idx_offset: int,
) -> list[Observation]:
    """Place `n` posts evenly across [last_bucket_start, now). Always lands
    every post inside the current bucket — even when `now` is right after
    the top of the hour and the available window is tiny.
    """
    available_seconds = max((now - last_bucket_start).total_seconds() - 1, 0)
    out: list[Observation] = []
    for k in range(n):
        offset = (available_seconds * k / max(n - 1, 1)) if n > 1 else 0
        ts = last_bucket_start + timedelta(seconds=offset)
        # Defensive: never exceed now (would put us in a future bucket).
        if ts >= now:
            ts = now - timedelta(microseconds=1)
        out.append(_make_post(
            ts, idx=idx_offset + k,
            text=text_fn(k), author=author_fn(k),
        ))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_account(
    now: datetime,
    platform: str,
    handle: str,
    *,
    display_name: str,
    bio: str,
    age_days: int,
) -> AccountObservation:
    created = now - timedelta(days=age_days)
    return AccountObservation(
        id=f"{platform}:account:demo_{handle}",
        platform=platform,
        kind=ObservationKind.ACCOUNT,
        author_handle=handle,
        author_id=f"demo_{handle}",
        text=bio,
        url=f"https://example.test/{platform}/{handle}",
        created_at=created,
        collected_at=now,
        display_name=display_name,
        bio=bio,
        account_created_at=created,
    )


def _make_post(when: datetime, *, idx: int, text: str, author: str) -> Observation:
    return Observation(
        id=f"demo:post:{idx}",
        platform="demo",
        kind=ObservationKind.POST,
        author_handle=author,
        author_id=f"id_{author}",
        text=text,
        url=f"https://example.test/post/{idx}",
        created_at=when,
        collected_at=when,
    )


def _typosquat_handle(legit: str) -> str:
    """Swap the last alphabetic char for a similar-looking digit; if the
    handle is too short or has no swappable char, append '_official'."""
    if len(legit) < 3:
        return legit + "_official"
    swap = {"l": "1", "o": "0", "i": "1", "e": "3", "a": "4", "s": "5"}
    chars = list(legit)
    for i in range(len(chars) - 1, -1, -1):
        if chars[i].lower() in swap:
            chars[i] = swap[chars[i].lower()]
            return "".join(chars)
    # Fallback — append a digit so it's still different from legit.
    return legit + "1"


def _homoglyph_handle(legit: str, brand_name: str) -> str:
    """Swap the first 'a' or 'o' for its Cyrillic lookalike. Falls back to
    appending a Cyrillic 'а' if the handle has neither."""
    swaps = {"a": "а", "o": "о", "e": "е", "p": "р", "c": "с"}
    for i, ch in enumerate(legit):
        if ch.lower() in swaps:
            return legit[:i] + swaps[ch.lower()] + legit[i + 1:]
    return legit + "а"  # cyrillic 'а' suffix

"""One-shot pipeline: collect → detect → route alerts.

`socmon scan` runs this once. The continuous `socmon run` will eventually
schedule per-collector and per-detector cadences via APScheduler, but every
job will ultimately call into the helpers here.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from datetime import datetime, timedelta, timezone

from socmon.alerters import build_alerter
from socmon.collectors import build_collector
from socmon.config import AlertRoute, SocmonConfig
from socmon.detectors import build_detector
from socmon.interfaces import Alerter, Collector, Detector
from socmon.models import CollectorQuery, Finding, FindingKind, Severity, TimeWindow
from socmon.storage import get_storage
from socmon.storage.base import Storage

log = logging.getLogger(__name__)

_SEVERITY_ORDER = {Severity.LOW: 0, Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}


def build_storage(cfg: SocmonConfig) -> Storage:
    storage = get_storage(cfg.storage.backend, cfg.storage.dsn)
    storage.init_schema()
    return storage


def build_alerters(cfg: SocmonConfig) -> dict[str, Alerter]:
    out: dict[str, Alerter] = {}
    for ac in cfg.alerters:
        a = build_alerter(ac.type, name=ac.name, **ac.options)
        a.name = ac.name
        out[ac.name] = a
    return out


def build_collectors(cfg: SocmonConfig) -> list[Collector]:
    cs: list[Collector] = []
    for cc in cfg.collectors:
        if not cc.enabled:
            continue
        c = build_collector(cc.type, **cc.options)
        c.name = cc.name
        cs.append(c)
    return cs


def build_detectors(cfg: SocmonConfig) -> list[Detector]:
    """Detector type -> required kwargs from top-level config."""
    out: list[Detector] = []
    for dc in cfg.detectors:
        if not dc.enabled:
            continue
        kwargs = dict(dc.options)
        if dc.type in ("impersonation", "fake_job"):
            kwargs["brand"] = cfg.brand
        if dc.type == "impersonation":
            kwargs["executives"] = cfg.executives
        if dc.type == "fake_job":
            kwargs["legit_jobs"] = cfg.legit_jobs
        if dc.type == "keyword_spike":
            kwargs["keywords"] = cfg.keywords
        d = build_detector(dc.type, name=dc.name, **kwargs)
        d.name = dc.name
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


async def _collect_accounts(collectors: list[Collector], storage: Storage,
                            cfg: SocmonConfig) -> list:
    """Drive `discover_accounts` on every collector, upsert to storage. Returns
    the AccountObservations that were new this pass (not already in storage).
    """
    new_items: list = []
    for c in collectors:
        log.info("collector %s: discovering accounts", c.name)
        try:
            batch = []
            async for acct in c.discover_accounts(cfg.brand, cfg.executives):
                batch.append(acct)
                if len(batch) >= 50:
                    new_items.extend(storage.upsert_observations(batch))
                    batch.clear()
            if batch:
                new_items.extend(storage.upsert_observations(batch))
        except NotImplementedError:
            log.debug("collector %s does not implement discover_accounts; skipping", c.name)
        except Exception as e:  # don't let one platform sink the whole run
            log.exception("collector %s failed: %s", c.name, e)
    return new_items


async def collect_one(
    collector: Collector, storage: Storage, cfg: SocmonConfig,
) -> tuple[list, list]:
    """One-collector wrapper around `_collect_accounts` + `_collect_posts`.

    The continuous-mode scheduler calls this so each collector can tick on
    its own cadence (set by `CollectorConfig.poll_interval_seconds`) without
    waiting for slower collectors in the same pass.
    """
    accounts = await _collect_accounts([collector], storage, cfg)
    posts = await _collect_posts([collector], storage, cfg)
    return accounts, posts


async def _collect_posts(collectors: list[Collector], storage: Storage,
                         cfg: SocmonConfig, *, fallback_lookback_days: int = 1) -> list:
    """Drive `collect()` on every collector with brand keywords + watermark.

    Watermark = max(observation.created_at) we've successfully ingested for that
    collector. On first run (no watermark), we fall back to `fallback_lookback_days`
    so we don't accidentally pull years of history.
    """
    keywords = [cfg.brand.name, *cfg.brand.aliases]
    new_items: list = []
    for c in collectors:
        watermark = storage.watermark(c.name)
        since = watermark or (datetime.now(timezone.utc) - timedelta(days=fallback_lookback_days))
        query = CollectorQuery(keywords=keywords, since=since)
        log.info("collector %s: collecting posts since %s", c.name, since.isoformat())
        try:
            batch: list = []
            max_seen = since
            async for obs in c.collect(query):
                batch.append(obs)
                if obs.created_at > max_seen:
                    max_seen = obs.created_at
                if len(batch) >= 100:
                    new_items.extend(storage.upsert_observations(batch))
                    batch.clear()
            if batch:
                new_items.extend(storage.upsert_observations(batch))
            if max_seen > since:
                storage.set_watermark(c.name, max_seen)
        except NotImplementedError:
            log.debug("collector %s does not implement collect; skipping", c.name)
        except Exception as e:
            log.exception("collector %s failed: %s", c.name, e)
    return new_items


# ---------------------------------------------------------------------------
# Detection + routing
# ---------------------------------------------------------------------------


def _route_matches(route: AlertRoute, finding: Finding) -> bool:
    if route.match_kind != "*" and route.match_kind != finding.kind:
        return False
    if route.match_detector and not fnmatch.fnmatch(finding.detector, route.match_detector):
        return False
    if _SEVERITY_ORDER[finding.severity] < _SEVERITY_ORDER[route.severity_min]:
        return False
    return True


def _dispatch(finding: Finding, routes: list[AlertRoute],
              alerters: dict[str, Alerter]) -> None:
    matched = False
    for route in routes:
        if not _route_matches(route, finding):
            continue
        matched = True
        if route.digest:
            # TODO: enqueue into a digest table; for now, log and skip.
            log.info("digest queued: %s -> %s", finding.title, route.channels)
            continue
        for ch in route.channels:
            a = alerters.get(ch)
            if a is None:
                log.warning("route references unknown alerter %r", ch)
                continue
            try:
                a.send(finding)
            except Exception as e:
                log.exception("alerter %s failed: %s", ch, e)
        # First matching route wins, per spec.
        break
    if not matched:
        log.debug("no route matched finding %s", finding.id)


def run_detectors(detectors: list[Detector], storage: Storage,
                  routes: list[AlertRoute], alerters: dict[str, Alerter],
                  window: TimeWindow, dry_run: bool = False) -> list[Finding]:
    """Returns the NEW findings (post-dedup). Stubs and per-detector exceptions
    are logged and skipped — they don't sink the whole run.
    """
    new_findings: list[Finding] = []
    for d in detectors:
        log.info("detector %s: running window=[%s..%s]", d.name, window.start, window.end)
        try:
            findings = list(d.run(storage, window))
        except NotImplementedError:
            log.warning("detector %s is a stub (not yet implemented); skipping", d.name)
            continue
        except Exception as e:
            log.exception("detector %s failed: %s", d.name, e)
            continue
        for finding in findings:
            is_new = True if dry_run else storage.insert_finding(finding)
            if not is_new:
                log.debug("dedup: skipping already-seen finding %s", finding.id)
                continue
            new_findings.append(finding)
            sev = finding.severity.value
            log.info("FINDING [%s] %s (score %.1f)", sev.upper(), finding.title, finding.score)
            if not dry_run:
                _dispatch(finding, routes, alerters)
    return new_findings


# ---------------------------------------------------------------------------
# Public entry-points
# ---------------------------------------------------------------------------


def scan(cfg: SocmonConfig, *, window_hours: int = 24, sample_size: int = 10) -> dict:
    """One-shot: collect, detect, alert. Returns a structured summary that
    pairs counts with sampled details (handles, titles, URLs) so callers —
    including the CLI and Claude Code — can render something more useful than
    bare counts.
    """
    storage = build_storage(cfg)
    collectors = build_collectors(cfg)
    detectors = build_detectors(cfg)
    alerters = build_alerters(cfg)

    async def _drive() -> tuple[list, list]:
        a = await _collect_accounts(collectors, storage, cfg)
        p = await _collect_posts(collectors, storage, cfg)
        return a, p

    new_accounts, new_posts = asyncio.run(_drive())

    now = datetime.now(timezone.utc)
    window = TimeWindow(start=now - timedelta(hours=window_hours), end=now)
    new_findings = run_detectors(detectors, storage, cfg.routes, alerters, window)

    return {
        "window": [window.start.isoformat(), window.end.isoformat()],
        "new_accounts": _summarize_accounts(new_accounts, sample_size),
        "new_posts": _summarize_posts(new_posts, sample_size),
        "new_findings": _summarize_findings(new_findings, sample_size),
    }


def backtest(cfg: SocmonConfig, start: datetime, end: datetime,
             detector_names: list[str] | None = None, dry_run: bool = True,
             sample_size: int = 10) -> dict:
    """Replay detectors over already-collected observations. Skips collection."""
    storage = build_storage(cfg)
    detectors = [d for d in build_detectors(cfg)
                 if not detector_names or d.name in detector_names]
    alerters: dict[str, Alerter] = {} if dry_run else build_alerters(cfg)
    window = TimeWindow(start=start, end=end)
    new_findings = run_detectors(detectors, storage, cfg.routes, alerters, window,
                                 dry_run=dry_run)
    return {
        "dry_run": dry_run,
        "window": [start.isoformat(), end.isoformat()],
        "new_findings": _summarize_findings(new_findings, sample_size),
    }


# ---------------------------------------------------------------------------
# Summarizers — small, dict-of-primitives so JSON-serializing is trivial.
# ---------------------------------------------------------------------------


def _summarize_accounts(items: list, sample: int) -> dict:
    return {
        "count": len(items),
        "items": [
            {
                "platform": o.platform,
                "handle": o.author_handle,
                "url": str(o.url) if o.url else None,
            }
            for o in items[:sample]
        ],
        "truncated": len(items) > sample,
    }


def _summarize_posts(items: list, sample: int) -> dict:
    out = []
    for o in items[:sample]:
        # First non-empty line of the post body, capped — usually the title.
        title = next((ln for ln in (o.text or "").splitlines() if ln.strip()), "")
        if len(title) > 100:
            title = title[:97] + "..."
        out.append({
            "platform": o.platform,
            "author": o.author_handle,
            "title": title,
            "url": str(o.url) if o.url else None,
            "created_at": o.created_at.isoformat(),
        })
    return {"count": len(items), "items": out, "truncated": len(items) > sample}


def _summarize_findings(items: list[Finding], sample: int) -> dict:
    return {
        "count": len(items),
        "items": [
            {
                "id": f.id,
                "kind": f.kind.value,
                "severity": f.severity.value,
                "score": f.score,
                "title": f.title,
                "url": (f.evidence[0].url if f.evidence and f.evidence[0].url else None),
            }
            for f in items[:sample]
        ],
        "truncated": len(items) > sample,
    }


def alerts_test(cfg: SocmonConfig, channels: list[str] | None = None) -> None:
    alerters = build_alerters(cfg)
    targets = [a for n, a in alerters.items() if not channels or n in channels]
    for a in targets:
        log.info("testing alerter %s", a.name)
        try:
            a.test()
        except NotImplementedError:
            log.warning("alerter %s is a stub (not yet implemented); skipping", a.name)
        except Exception as e:
            log.exception("alerter %s test failed: %s", a.name, e)

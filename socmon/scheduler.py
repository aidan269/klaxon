"""APScheduler-based continuous mode (`socmon run`).

Two kinds of jobs:
  - One per enabled collector, on its own `poll_interval_seconds`.
  - One detector job (for all enabled detectors), on `detector_interval_seconds`.

Storage, collectors, detectors, and alerters are built ONCE at startup and
reused across every tick — credentials load once, brand-logo pHashes compute
once, etc.

Failure isolation: a per-tick exception is logged but never kills the
scheduler. `coalesce=True` and `max_instances=1` mean if a collector tick
takes longer than its interval we drop the queued duplicate run instead of
piling up backlog.

Shutdown: SIGINT and SIGTERM drain in-flight jobs (the scheduler waits for
them to return) and exit cleanly. On Windows or when run from a non-main
thread the signal hooks are skipped and shutdown comes via `request_shutdown()`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from socmon import runner
from socmon.config import SocmonConfig
from socmon.interfaces import Alerter, Collector, Detector
from socmon.models import TimeWindow
from socmon.storage.base import Storage

log = logging.getLogger(__name__)


# Each detector implements its own time logic relative to window.end (spike
# detectors look back `baseline_days`; impersonation reads everything and
# leans on kv_state dedup). So a very wide start avoids accidentally hiding
# accounts whose on-platform `created_at` is older than the tick window.
_DETECTOR_WINDOW_START = datetime(1970, 1, 1, tzinfo=timezone.utc)

# Give collectors a head start on a fresh DB so the first detector tick has
# something to look at. Past startup this delay is irrelevant.
_DETECTOR_STARTUP_DELAY_SECONDS = 30


class SocmonScheduler:
    """Continuous-mode runtime. Construct once, call `setup()` then `run()`."""

    def __init__(self, cfg: SocmonConfig) -> None:
        self.cfg = cfg
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.storage: Storage | None = None
        self.collectors: list[Collector] = []
        self.detectors: list[Detector] = []
        self.alerters: dict[str, Alerter] = {}
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Build long-lived instances + register jobs. Idempotent only if
        called on a fresh instance — re-running would double-register jobs."""
        self.storage = runner.build_storage(self.cfg)
        self.collectors = runner.build_collectors(self.cfg)
        self.detectors = runner.build_detectors(self.cfg)
        self.alerters = runner.build_alerters(self.cfg)

        now = datetime.now(timezone.utc)
        collectors_by_name = {c.name: c for c in self.collectors}

        # Per-collector ticks.
        for cc in self.cfg.collectors:
            if not cc.enabled:
                continue
            c = collectors_by_name.get(cc.name)
            if c is None:
                # build_collectors silently skipped it; warn so it's not invisible.
                log.warning("scheduler: collector %r enabled in config but not built", cc.name)
                continue
            self.scheduler.add_job(
                self._collector_tick,
                args=[c],
                trigger=IntervalTrigger(seconds=cc.poll_interval_seconds),
                id=f"collector:{c.name}",
                name=f"collector {c.name} (every {cc.poll_interval_seconds}s)",
                next_run_time=now,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=60,
            )

        # Single detector tick.
        if self.detectors:
            self.scheduler.add_job(
                self._detector_tick,
                trigger=IntervalTrigger(seconds=self.cfg.detector_interval_seconds),
                id="detectors",
                name=f"detectors (every {self.cfg.detector_interval_seconds}s)",
                next_run_time=now + timedelta(seconds=_DETECTOR_STARTUP_DELAY_SECONDS),
                coalesce=True,
                max_instances=1,
                misfire_grace_time=60,
            )

    def run(self) -> None:
        """Blocking entry point used by the CLI. Runs until SIGINT/SIGTERM."""
        asyncio.run(self.serve_forever())

    async def serve_forever(self) -> None:
        self._stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop_event.set)
            except (NotImplementedError, ValueError):
                # Windows / non-main-thread — caller must use request_shutdown().
                pass

        self.scheduler.start()
        n_collectors = sum(
            1 for j in self.scheduler.get_jobs() if j.id.startswith("collector:")
        )
        log.info(
            "scheduler started: %d collector job(s), %d detector job(s)",
            n_collectors, 1 if self.detectors else 0,
        )
        try:
            await self._stop_event.wait()
            log.info("shutdown requested; draining in-flight jobs")
        finally:
            self.scheduler.shutdown(wait=True)
            log.info("scheduler stopped")

    def request_shutdown(self) -> None:
        """Programmatic shutdown — used by tests and non-signal callers."""
        if self._stop_event is not None:
            self._stop_event.set()

    # ------------------------------------------------------------------
    # ticks
    # ------------------------------------------------------------------

    async def _collector_tick(self, collector: Collector) -> None:
        if self.storage is None:
            return
        log.info("collector tick: %s", collector.name)
        try:
            new_accounts, new_posts = await runner.collect_one(
                collector, self.storage, self.cfg,
            )
            log.info(
                "collector tick complete: %s — %d new account(s), %d new post(s)",
                collector.name, len(new_accounts), len(new_posts),
            )
        except Exception:
            # Per-tick failures must not kill the scheduler. The next tick at
            # this collector's interval will try again.
            log.exception("collector tick failed: %s", collector.name)

    async def _detector_tick(self) -> None:
        if self.storage is None or not self.detectors:
            return
        now = datetime.now(timezone.utc)
        window = TimeWindow(start=_DETECTOR_WINDOW_START, end=now)
        log.info("detector tick: window=[…%s]", window.end.isoformat())
        try:
            new_findings = runner.run_detectors(
                self.detectors, self.storage,
                self.cfg.routes, self.alerters, window,
            )
            log.info("detector tick complete: %d new finding(s)", len(new_findings))
        except Exception:
            log.exception("detector tick failed")
            # Deliberately do NOT ping the heartbeat on failure — that's the
            # whole point of having an external canary.
            return

        if self.cfg.heartbeat_url:
            await self._ping_heartbeat(self.cfg.heartbeat_url)

    async def _ping_heartbeat(self, url: str) -> None:
        """Fire-and-(mostly-)forget GET to the configured heartbeat URL.

        Compatible with Healthchecks.io's ping URLs (`https://hc-ping.com/…`)
        and any other "GET = I'm alive" endpoint. Ping failures are logged
        but never propagate — a transient network blip shouldn't make us
        look like we died.
        """
        try:
            async with httpx.AsyncClient(
                timeout=self.cfg.heartbeat_timeout_seconds,
            ) as client:
                r = await client.get(url)
                if r.status_code >= 400:
                    log.warning("heartbeat ping → %d %s", r.status_code, url)
                else:
                    log.debug("heartbeat ping ok (%d)", r.status_code)
        except Exception as e:
            log.warning("heartbeat ping failed: %s", e)

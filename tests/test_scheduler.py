"""APScheduler wiring tests for `socmon run`.

Three layers:
  1. setup() registers the right jobs with the right intervals
  2. tick coroutines call the right runner helpers and swallow exceptions
  3. serve_forever() actually fires a collector tick then exits cleanly
     when request_shutdown() is called
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from socmon import runner
from socmon.config import (
    BrandEntity,
    CollectorConfig,
    DetectorConfig,
    SocmonConfig,
    StorageConfig,
)
from socmon.scheduler import SocmonScheduler


def _cfg(tmp_path, *, collectors=None, detectors=None, detector_interval=300):
    return SocmonConfig(
        organization="t",
        brand=BrandEntity(name="t"),
        storage=StorageConfig(backend="sqlite", dsn=f"sqlite:///{tmp_path}/t.db"),
        collectors=collectors or [],
        detectors=detectors or [],
        detector_interval_seconds=detector_interval,
    )


# ---------------------------------------------------------------------------
# setup()
# ---------------------------------------------------------------------------


def test_setup_schedules_one_job_per_enabled_collector(tmp_path):
    cfg = _cfg(tmp_path, collectors=[
        CollectorConfig(name="rss-a", type="rss", poll_interval_seconds=60),
        CollectorConfig(name="rss-b", type="rss", poll_interval_seconds=120),
    ])
    s = SocmonScheduler(cfg)
    s.setup()
    ids = {j.id for j in s.scheduler.get_jobs()}
    assert "collector:rss-a" in ids
    assert "collector:rss-b" in ids


def test_disabled_collectors_are_skipped(tmp_path):
    cfg = _cfg(tmp_path, collectors=[
        CollectorConfig(name="rss-a", type="rss", poll_interval_seconds=60),
        CollectorConfig(name="rss-b", type="rss", poll_interval_seconds=120, enabled=False),
    ])
    s = SocmonScheduler(cfg)
    s.setup()
    ids = {j.id for j in s.scheduler.get_jobs()}
    assert "collector:rss-a" in ids
    assert "collector:rss-b" not in ids


def test_collector_interval_matches_config(tmp_path):
    cfg = _cfg(tmp_path, collectors=[
        CollectorConfig(name="rss-a", type="rss", poll_interval_seconds=237),
    ])
    s = SocmonScheduler(cfg)
    s.setup()
    job = s.scheduler.get_job("collector:rss-a")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 237


def test_detector_job_absent_when_no_detectors(tmp_path):
    cfg = _cfg(tmp_path, collectors=[
        CollectorConfig(name="rss-a", type="rss", poll_interval_seconds=60),
    ])
    s = SocmonScheduler(cfg)
    s.setup()
    assert s.scheduler.get_job("detectors") is None


def test_detector_job_uses_config_interval(tmp_path):
    cfg = _cfg(
        tmp_path,
        detectors=[DetectorConfig(name="m", type="mention_spike")],
        detector_interval=180,
    )
    s = SocmonScheduler(cfg)
    s.setup()
    job = s.scheduler.get_job("detectors")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 180


# ---------------------------------------------------------------------------
# ticks
# ---------------------------------------------------------------------------


def test_collector_tick_calls_collect_one(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, collectors=[
        CollectorConfig(name="rss-a", type="rss", poll_interval_seconds=60),
    ])
    s = SocmonScheduler(cfg)
    s.setup()

    recorded = MagicMock()

    async def fake(c, storage, cfg):
        recorded(c.name)
        return [], []

    monkeypatch.setattr(runner, "collect_one", fake)

    asyncio.run(s._collector_tick(s.collectors[0]))
    recorded.assert_called_once_with("rss-a")


def test_collector_tick_swallows_exceptions(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, collectors=[
        CollectorConfig(name="rss-a", type="rss", poll_interval_seconds=60),
    ])
    s = SocmonScheduler(cfg)
    s.setup()

    async def boom(c, storage, cfg):
        raise RuntimeError("upstream rate limit")

    monkeypatch.setattr(runner, "collect_one", boom)

    # Per-tick failures must not propagate — that would kill the scheduler.
    asyncio.run(s._collector_tick(s.collectors[0]))


def test_detector_tick_calls_run_detectors(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, detectors=[
        DetectorConfig(name="m", type="mention_spike"),
    ])
    s = SocmonScheduler(cfg)
    s.setup()

    recorded = MagicMock()

    def fake(detectors, storage, routes, alerters, window, **kw):
        recorded(len(detectors), window)
        return []

    monkeypatch.setattr(runner, "run_detectors", fake)

    asyncio.run(s._detector_tick())
    assert recorded.call_count == 1
    n_detectors, window = recorded.call_args[0]
    assert n_detectors == 1
    # Wide window — impersonation should never miss accounts due to filter.
    assert window.start.year == 1970


def test_detector_tick_swallows_exceptions(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, detectors=[
        DetectorConfig(name="m", type="mention_spike"),
    ])
    s = SocmonScheduler(cfg)
    s.setup()

    def boom(*a, **k):
        raise RuntimeError("storage hiccup")

    monkeypatch.setattr(runner, "run_detectors", boom)
    asyncio.run(s._detector_tick())  # must not raise


def test_detector_tick_with_no_detectors_is_noop(tmp_path):
    cfg = _cfg(tmp_path)  # no detectors
    s = SocmonScheduler(cfg)
    s.setup()
    # Direct call — should just return without exploding even though there's
    # no scheduled detector job.
    asyncio.run(s._detector_tick())


# ---------------------------------------------------------------------------
# serve_forever
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_serve_forever_fires_collector_then_stops(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, collectors=[
        # Long interval — we only care that the immediate next_run_time fires.
        CollectorConfig(name="rss-a", type="rss", poll_interval_seconds=3600),
    ])
    s = SocmonScheduler(cfg)
    s.setup()

    calls: list[str] = []

    async def fake_collect(c, storage, cfg):
        calls.append(c.name)
        return [], []

    monkeypatch.setattr(runner, "collect_one", fake_collect)

    async def stop_soon():
        # Give the scheduler a moment to start and execute its first job.
        await asyncio.sleep(0.3)
        s.request_shutdown()

    await asyncio.gather(s.serve_forever(), stop_soon())

    assert "rss-a" in calls, "collector job should have fired on startup"


@pytest.mark.asyncio
async def test_serve_forever_returns_promptly_on_shutdown(tmp_path):
    cfg = _cfg(tmp_path)
    s = SocmonScheduler(cfg)
    s.setup()

    async def stop_soon():
        await asyncio.sleep(0.05)
        s.request_shutdown()

    await asyncio.wait_for(
        asyncio.gather(s.serve_forever(), stop_soon()),
        timeout=2.0,
    )


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def _intercept_httpx_get(monkeypatch, *, status_code: int = 200, fail: bool = False):
    """Patch httpx.AsyncClient.get and return a list that records hit URLs."""
    pinged: list[str] = []

    async def fake_get(self, url, **kwargs):
        pinged.append(url)
        if fail:
            raise httpx.ConnectError("upstream down")

        class _R:
            def __init__(self, code): self.status_code = code
        return _R(status_code)

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    return pinged


def test_detector_tick_pings_heartbeat_on_success(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, detectors=[
        DetectorConfig(name="m", type="mention_spike"),
    ])
    cfg.heartbeat_url = "https://hc-ping.test/canary"
    s = SocmonScheduler(cfg)
    s.setup()

    pinged = _intercept_httpx_get(monkeypatch)
    monkeypatch.setattr(runner, "run_detectors", lambda *a, **k: [])

    asyncio.run(s._detector_tick())
    assert pinged == ["https://hc-ping.test/canary"]


def test_detector_tick_skips_heartbeat_when_detectors_fail(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, detectors=[
        DetectorConfig(name="m", type="mention_spike"),
    ])
    cfg.heartbeat_url = "https://hc-ping.test/canary"
    s = SocmonScheduler(cfg)
    s.setup()

    pinged = _intercept_httpx_get(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("storage offline")

    monkeypatch.setattr(runner, "run_detectors", boom)

    asyncio.run(s._detector_tick())
    # Heartbeat must NOT fire if the tick itself failed — that's the entire
    # point of an external canary.
    assert pinged == []


def test_no_heartbeat_url_means_no_ping(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, detectors=[
        DetectorConfig(name="m", type="mention_spike"),
    ])
    # heartbeat_url left at default (None)
    s = SocmonScheduler(cfg)
    s.setup()

    pinged = _intercept_httpx_get(monkeypatch)
    monkeypatch.setattr(runner, "run_detectors", lambda *a, **k: [])

    asyncio.run(s._detector_tick())
    assert pinged == []


def test_heartbeat_ping_failure_does_not_crash_tick(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, detectors=[
        DetectorConfig(name="m", type="mention_spike"),
    ])
    cfg.heartbeat_url = "https://hc-ping.test/canary"
    s = SocmonScheduler(cfg)
    s.setup()

    _intercept_httpx_get(monkeypatch, fail=True)  # ConnectError on ping
    monkeypatch.setattr(runner, "run_detectors", lambda *a, **k: [])

    # Should not raise; a transient blip on the heartbeat URL must not make
    # the scheduler look dead.
    asyncio.run(s._detector_tick())

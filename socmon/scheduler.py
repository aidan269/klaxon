"""APScheduler wiring. The `run` CLI command builds one of these and starts it.

Two job classes:
  - per-collector poll jobs (cadence = collector.poll_interval_seconds)
  - per-detector evaluation jobs (cadence = detector option, default 5 min)

Findings emitted by detectors are routed through `routes` to alerters here, so the
detectors themselves stay storage-only.
"""

from __future__ import annotations

from socmon.config import SocmonConfig
from socmon.storage.base import Storage


class SocmonScheduler:
    def __init__(self, config: SocmonConfig, storage: Storage) -> None:
        self.config = config
        self.storage = storage

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def run_once(self) -> None:
        """Used by `socmon scan` — one pass through every collector + detector."""
        raise NotImplementedError

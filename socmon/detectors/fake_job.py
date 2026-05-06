"""Fake job detector.

Compares JobObservation records against a source-of-truth list (`config.legit_jobs`)
and looks for red flags. Score blends:

  - title/location/company present in legit set                (negative weight: legit)
  - posted_by handle is on the org's verified recruiter list   (negative weight)
  - body solicits payment / personal financial info            (positive)
  - body contains off-platform contact handles (whatsapp/tg)   (positive)
  - apply_url domain typosquats a configured corporate domain  (positive, big)
  - salary range > 2x median for that title/location           (positive, "too good")

Findings carry the matched red flags in metadata so reviewers can act fast.
"""

from __future__ import annotations

from typing import Iterator

from socmon.config import BrandEntity, LegitJobsSource
from socmon.detectors import register
from socmon.interfaces import Detector
from socmon.models import Finding, JobObservation, TimeWindow
from socmon.storage.base import Storage


@register("fake_job")
class FakeJobDetector(Detector):
    name = "fake_job"

    def __init__(
        self,
        brand: BrandEntity | None = None,
        legit_jobs: LegitJobsSource | None = None,
        **options,
    ) -> None:
        self.brand = brand
        self.legit_jobs = legit_jobs
        self.options = options
        self._legit_index: dict | None = None  # populated lazily from legit_jobs source

    def run(self, storage: Storage, window: TimeWindow) -> Iterator[Finding]:
        raise NotImplementedError
        yield  # type: ignore[unreachable]

    def score_job(self, job: JobObservation) -> tuple[float, list[str]]:
        """Returns (score, list of red-flag reasons)."""
        raise NotImplementedError

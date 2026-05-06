"""PagerDuty Events API v2 alerter — for high-severity findings only."""

from __future__ import annotations

from socmon.alerters import register
from socmon.interfaces import Alerter
from socmon.models import Finding


@register("pagerduty")
class PagerDutyAlerter(Alerter):
    name = "pagerduty"

    def __init__(self, **options) -> None:
        # options: routing_key_env, dedup_strategy ("finding_id" | "kind+entity")
        self.options = options

    def send(self, finding: Finding) -> None:
        raise NotImplementedError

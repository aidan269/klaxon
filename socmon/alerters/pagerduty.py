"""PagerDuty Events API v2 alerter.

POSTs a `trigger` event to https://events.pagerduty.com/v2/enqueue using
the `routing_key` of a PagerDuty service. The dedup_key (PagerDuty's
deduplication primitive) is the Finding id by default, so re-running klaxon
over the same data won't produce duplicate pages — PagerDuty groups them
into a single incident until it's resolved.

Severity mapping (Finding → PagerDuty):
  LOW       → info
  MEDIUM    → warning
  HIGH      → error
  CRITICAL  → critical

Reserve PagerDuty for the high-stakes routes (exec impersonation, critical
spikes). Low-severity findings should typically route to Slack/email digest
instead, so on-call doesn't burn out.

Reference: https://developer.pagerduty.com/api-reference/368ae3d938c9e-send-an-event
"""

from __future__ import annotations

import logging
import os

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from socmon.alerters import register
from socmon.interfaces import Alerter
from socmon.models import Finding, Severity

log = logging.getLogger(__name__)


_PD_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

# Finding.severity → PagerDuty severity. Order matters: critical maps to
# critical so on-call gets the highest urgency for the worst findings.
_SEVERITY_MAP: dict[Severity, str] = {
    Severity.LOW: "info",
    Severity.MEDIUM: "warning",
    Severity.HIGH: "error",
    Severity.CRITICAL: "critical",
}


@register("pagerduty")
class PagerDutyAlerter(Alerter):
    name = "pagerduty"

    def __init__(self, **options) -> None:
        self.options = options

        # `routing_key_env` is the env var holding a v2 routing key (32 hex
        # chars). Per-service in PagerDuty; treat as a credential.
        env_name = options.get("routing_key_env")
        self.routing_key: str | None = (
            os.environ[env_name] if env_name and env_name in os.environ
            else options.get("routing_key")
        )

        # How to dedup PagerDuty incidents:
        #   "finding_id"  (default) — each unique Finding.id makes its own
        #                              incident. Re-firing the same finding
        #                              groups under the existing incident.
        #   "kind+entity" — group by detector kind + matched entity. Useful
        #                   if you want all mention spikes on a brand to
        #                   collapse into one rolling incident.
        self.dedup_strategy: str = options.get("dedup_strategy", "finding_id")

        # Optional: how this klaxon instance identifies itself in the
        # `source` field on PagerDuty events.
        self.source: str = options.get("source", "klaxon")
        self.client_name: str = options.get("client_name", "klaxon")
        self.client_url: str | None = options.get("client_url")

        self.timeout: float = float(options.get("timeout", 10.0))

        if name := options.get("name"):
            self.name = name

        if not self.routing_key:
            log.warning(
                "pagerduty alerter %r has no routing_key (set %s in env "
                "or `routing_key` in options); sends will be no-ops",
                self.name, env_name or "<routing_key_env>",
            )

    def send(self, finding: Finding) -> None:
        if not self.routing_key:
            log.info("pagerduty send skipped (no routing_key): %s", finding.title)
            return
        payload = self._build_payload(finding)
        self._post(payload)

    # ----- internals -----

    def _build_payload(self, f: Finding) -> dict:
        dedup_key = (
            f.id
            if self.dedup_strategy == "finding_id"
            else f"klaxon:{f.kind.value}:{f.detector}"
        )
        custom: dict = {
            "kind": f.kind.value,
            "detector": f.detector,
            "score": f.score,
            "detected_at": f.detected_at.isoformat(),
            "metadata": f.metadata,
        }
        if f.evidence:
            custom["evidence"] = [
                {"platform": e.platform, "url": e.url, "snippet": e.snippet}
                for e in f.evidence[:5]
            ]
        links = []
        for e in f.evidence[:5]:
            if e.url:
                links.append({"href": e.url, "text": f"{e.platform}: evidence"})

        payload: dict = {
            "routing_key": self.routing_key,
            "event_action": "trigger",
            "dedup_key": dedup_key,
            "payload": {
                "summary": f.title[:1024],
                "source": self.source,
                "severity": _SEVERITY_MAP[f.severity],
                "component": f.detector,
                "group": f.kind.value,
                "class": "brand_protection",
                "custom_details": custom,
            },
            "client": self.client_name,
        }
        if self.client_url:
            payload["client_url"] = self.client_url
        if links:
            payload["links"] = links
        return payload

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _post(self, payload: dict) -> None:
        r = httpx.post(_PD_EVENTS_URL, json=payload, timeout=self.timeout)
        # PagerDuty returns 202 Accepted for valid events.
        r.raise_for_status()

"""Slack alerter — incoming webhook with Block Kit formatting.

Webhook URL is referenced via env var name in config (`webhook_url_env`), never
stored in YAML. We send a single message per finding; deduplication has already
happened at the storage layer.
"""

from __future__ import annotations

import json
import logging
import os

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from socmon.alerters import register
from socmon.interfaces import Alerter
from socmon.models import Finding, Severity

log = logging.getLogger(__name__)


_SEVERITY_COLOR = {
    Severity.LOW: "#9CA3AF",       # gray
    Severity.MEDIUM: "#FBBF24",    # amber
    Severity.HIGH: "#F97316",      # orange
    Severity.CRITICAL: "#DC2626",  # red
}

_SEVERITY_EMOJI = {
    Severity.LOW: ":information_source:",
    Severity.MEDIUM: ":warning:",
    Severity.HIGH: ":rotating_light:",
    Severity.CRITICAL: ":rotating_light::rotating_light:",
}


@register("slack")
class SlackAlerter(Alerter):
    name = "slack"

    def __init__(self, **options) -> None:
        # `webhook_url_env`: name of env var holding the URL.
        # `webhook_url`: direct URL (discouraged — for local testing only).
        self.options = options
        env_name = options.get("webhook_url_env")
        self.webhook_url: str | None = (
            os.environ[env_name] if env_name and env_name in os.environ
            else options.get("webhook_url")
        )
        self.channel: str | None = options.get("channel")
        self.username: str = options.get("username", "socmon")
        self.icon_emoji: str = options.get("icon_emoji", ":mag:")
        if name := options.get("name"):
            self.name = name
        if not self.webhook_url:
            log.warning(
                "slack alerter %r has no webhook_url; sends will be no-ops "
                "(set %s in the environment)",
                self.name, env_name or "<webhook_url_env>",
            )

    def send(self, finding: Finding) -> None:
        if not self.webhook_url:
            log.info("slack send skipped (no webhook): %s", finding.title)
            return
        payload = self._build_payload(finding)
        self._post(payload)

    # ----- internals -----

    def _build_payload(self, f: Finding) -> dict:
        emoji = _SEVERITY_EMOJI.get(f.severity, "")
        color = _SEVERITY_COLOR.get(f.severity, "#6B7280")

        fields = [
            {"type": "mrkdwn", "text": f"*Severity*\n{f.severity.value.upper()}"},
            {"type": "mrkdwn", "text": f"*Score*\n{f.score:.1f} / 100"},
            {"type": "mrkdwn", "text": f"*Detector*\n`{f.detector}`"},
            {"type": "mrkdwn", "text": f"*Kind*\n`{f.kind.value}`"},
        ]

        blocks: list[dict] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {f.title}"[:150]},
            },
            {"type": "section", "fields": fields},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f.summary[:3000]},
            },
        ]

        # Evidence: link out to the source observation(s).
        if f.evidence:
            ev_lines: list[str] = []
            for e in f.evidence[:5]:
                if e.url:
                    ev_lines.append(f"• <{e.url}|{e.platform}: {e.observation_id}>")
                else:
                    ev_lines.append(f"• `{e.platform}: {e.observation_id}`")
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Evidence*\n" + "\n".join(ev_lines)},
            })

        # Signal breakdown for impersonation findings — helpful for triage.
        if f.metadata and "weighted" in f.metadata:
            w = f.metadata["weighted"]
            row = " · ".join(f"{k}: {v}" for k, v in w.items())
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"*Signal weights:* {row}"}],
            })

        # `attachments` gives us the colored side-bar even though we use blocks.
        msg: dict = {
            "username": self.username,
            "icon_emoji": self.icon_emoji,
            "text": f.title,  # fallback for notifications
            "attachments": [{"color": color, "blocks": blocks}],
        }
        if self.channel:
            msg["channel"] = self.channel
        return msg

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _post(self, payload: dict) -> None:
        # `data=` rather than `json=` because Slack incoming webhooks accept either,
        # but data avoids a `Content-Type: application/json` flap with some proxies.
        r = httpx.post(self.webhook_url, content=json.dumps(payload), timeout=10.0)
        r.raise_for_status()

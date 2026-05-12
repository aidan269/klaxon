"""Generic webhook alerter — POSTs the Finding as JSON.

Designed for piping into a SIEM / SOAR / ticketing system. The whole Finding
model is the body (id, kind, severity, score, title, summary, detected_at,
evidence, metadata) so the receiver has everything it needs to act.

HMAC signing: if `hmac_secret_env` is set, the request body is HMAC-SHA256'd
and the digest goes into `X-Socmon-Signature: sha256=<hex>` (GitHub/Stripe
style). Receivers verify by recomputing the HMAC over the raw request body.
If the env var is configured but missing at startup the alerter logs a loud
warning and falls back to UNSIGNED requests — it does not silently re-enable
itself, so the misconfig is visible in logs and the receiver (if it enforces
signing) will reject the unsigned payloads.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from socmon.alerters import register
from socmon.interfaces import Alerter
from socmon.models import Finding

log = logging.getLogger(__name__)


@register("webhook")
class WebhookAlerter(Alerter):
    name = "webhook"

    def __init__(self, **options) -> None:
        self.options = options
        self.url: str | None = options.get("url")
        self.method: str = str(options.get("method", "POST")).upper()
        self.headers: dict[str, str] = dict(options.get("headers") or {})
        self.timeout: float = float(options.get("timeout", 10.0))

        # Secret is loaded from env so the YAML never holds it.
        env_name = options.get("hmac_secret_env")
        if env_name:
            secret = os.environ.get(env_name)
            if secret is None:
                log.warning(
                    "webhook alerter %r: hmac_secret_env=%s set but env var "
                    "missing; requests will be UNSIGNED until the env var "
                    "is set and the process restarts",
                    options.get("name", self.name), env_name,
                )
                self.hmac_secret: bytes | None = None
            else:
                self.hmac_secret = secret.encode("utf-8")
        else:
            self.hmac_secret = None

        if name := options.get("name"):
            self.name = name

        if not self.url:
            log.warning(
                "webhook alerter %r has no `url`; sends will be no-ops",
                self.name,
            )

    def send(self, finding: Finding) -> None:
        if not self.url:
            log.info("webhook send skipped (no url): %s", finding.title)
            return
        body = finding.model_dump_json().encode("utf-8")
        headers = self._build_headers(body)
        self._request(self.url, headers, body)

    # ----- internals -----

    def _build_headers(self, body: bytes) -> dict[str, str]:
        headers = dict(self.headers)
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("User-Agent", "socmon/0.1")
        if self.hmac_secret:
            digest = hmac.new(self.hmac_secret, body, hashlib.sha256).hexdigest()
            headers["X-Socmon-Signature"] = f"sha256={digest}"
        return headers

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    def _request(self, url: str, headers: dict[str, str], body: bytes) -> None:
        r = httpx.request(
            self.method, url,
            headers=headers, content=body, timeout=self.timeout,
        )
        r.raise_for_status()

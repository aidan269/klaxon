"""Generic webhook alerter — POSTs the Finding as JSON. Useful for SIEM/SOAR."""

from __future__ import annotations

from socmon.alerters import register
from socmon.interfaces import Alerter
from socmon.models import Finding


@register("webhook")
class WebhookAlerter(Alerter):
    name = "webhook"

    def __init__(self, **options) -> None:
        # options: url, headers, hmac_secret_env (signs body with X-Socmon-Signature)
        self.options = options

    def send(self, finding: Finding) -> None:
        raise NotImplementedError

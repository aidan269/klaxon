"""Email alerter — SMTP. Use a transactional provider for reliability in prod."""

from __future__ import annotations

from socmon.alerters import register
from socmon.interfaces import Alerter
from socmon.models import Finding


@register("email")
class EmailAlerter(Alerter):
    name = "email"

    def __init__(self, **options) -> None:
        # options: smtp_host, smtp_port, smtp_user_env, smtp_pass_env, from_addr, to_addrs
        self.options = options

    def send(self, finding: Finding) -> None:
        raise NotImplementedError

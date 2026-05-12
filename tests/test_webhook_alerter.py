"""WebhookAlerter tests — body shape, HMAC signing, configuration edge cases.

Uses respx (already a dev dep) to intercept httpx so the tests stay hermetic.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from socmon.alerters.webhook import WebhookAlerter
from socmon.models import EvidenceRef, Finding, FindingKind, Severity


def _finding() -> Finding:
    return Finding(
        id="f1",
        kind=FindingKind.IMPERSONATION,
        detector="imp",
        severity=Severity.HIGH,
        score=78.3,
        title="Possible impersonation: reddit/u/testbrand_fake",
        summary="Account 'testbrand_fake' resembles 'testbrand'.",
        detected_at=datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc),
        evidence=[EvidenceRef(
            observation_id="reddit:account:t2_x", platform="reddit",
            url="https://example.test/u/testbrand_fake",
        )],
        metadata={"matched_legit": "testbrand"},
    )


# ---------------------------------------------------------------------------


@respx.mock
def test_send_posts_finding_json() -> None:
    route = respx.post("https://hooks.example.test/sink").mock(
        return_value=httpx.Response(200),
    )
    a = WebhookAlerter(url="https://hooks.example.test/sink")
    a.send(_finding())

    assert route.called
    req = route.calls[0].request
    assert req.headers["content-type"] == "application/json"
    body = json.loads(req.content)
    assert body["id"] == "f1"
    assert body["kind"] == "impersonation"
    assert body["severity"] == "high"
    assert body["title"].startswith("Possible impersonation")
    # Evidence carried through.
    assert body["evidence"][0]["platform"] == "reddit"


@respx.mock
def test_send_signs_body_with_hmac_when_secret_env_present(monkeypatch) -> None:
    monkeypatch.setenv("WEBHOOK_HMAC", "topsecret-rotate-me")
    route = respx.post("https://hooks.example.test/sink").mock(
        return_value=httpx.Response(200),
    )
    a = WebhookAlerter(
        url="https://hooks.example.test/sink",
        hmac_secret_env="WEBHOOK_HMAC",
    )
    a.send(_finding())

    req = route.calls[0].request
    sig_header = req.headers.get("x-socmon-signature")
    assert sig_header is not None
    assert sig_header.startswith("sha256=")

    expected = hmac.new(
        b"topsecret-rotate-me", req.content, hashlib.sha256,
    ).hexdigest()
    assert sig_header == f"sha256={expected}"


@respx.mock
def test_missing_hmac_env_var_warns_and_sends_unsigned(monkeypatch, caplog) -> None:
    # No env var set for WEBHOOK_HMAC.
    monkeypatch.delenv("WEBHOOK_HMAC", raising=False)
    route = respx.post("https://hooks.example.test/sink").mock(
        return_value=httpx.Response(200),
    )
    import logging
    with caplog.at_level(logging.WARNING):
        a = WebhookAlerter(
            url="https://hooks.example.test/sink",
            hmac_secret_env="WEBHOOK_HMAC",
        )
    assert any("UNSIGNED" in r.message for r in caplog.records)
    a.send(_finding())
    req = route.calls[0].request
    assert "x-socmon-signature" not in req.headers


def test_no_url_means_send_is_a_noop(caplog) -> None:
    import logging
    a = WebhookAlerter()  # url unset
    with caplog.at_level(logging.INFO):
        a.send(_finding())
    assert any("webhook send skipped" in r.message for r in caplog.records)


@respx.mock
def test_custom_headers_are_forwarded() -> None:
    respx.post("https://hooks.example.test/sink").mock(
        return_value=httpx.Response(204),
    )
    a = WebhookAlerter(
        url="https://hooks.example.test/sink",
        headers={"X-API-Key": "abc123", "X-Tenant": "spearbit"},
    )
    a.send(_finding())
    req = respx.calls[0].request
    assert req.headers["x-api-key"] == "abc123"
    assert req.headers["x-tenant"] == "spearbit"


@respx.mock
def test_non_default_method() -> None:
    route = respx.put("https://hooks.example.test/sink").mock(
        return_value=httpx.Response(200),
    )
    a = WebhookAlerter(url="https://hooks.example.test/sink", method="put")
    a.send(_finding())
    assert route.called


@respx.mock
def test_4xx_response_propagates_for_runner_to_log() -> None:
    # The runner's _dispatch logs alerter exceptions; we WANT the webhook
    # alerter to surface non-2xx so failures are visible, not silent.
    respx.post("https://hooks.example.test/sink").mock(
        return_value=httpx.Response(401, text="bad auth"),
    )
    a = WebhookAlerter(url="https://hooks.example.test/sink")
    with pytest.raises(httpx.HTTPStatusError):
        a.send(_finding())

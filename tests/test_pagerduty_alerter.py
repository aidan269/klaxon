"""PagerDutyAlerter tests.

Goal: confirm the Events API v2 envelope is right, severity mapping is
correct, dedup_strategy works both ways, and no-routing-key is a safe no-op.
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from socmon.alerters.pagerduty import PagerDutyAlerter
from socmon.models import EvidenceRef, Finding, FindingKind, Severity


def _finding(*, severity: Severity = Severity.HIGH, kind: FindingKind = FindingKind.IMPERSONATION,
             id_: str = "f1") -> Finding:
    return Finding(
        id=id_,
        kind=kind,
        detector="imp",
        severity=severity,
        score=80.0,
        title="Possible exec impersonation of Jane Doe — twitter/janedoeacm3",
        summary="Account scored 80 against legit handle janedoeacme.",
        detected_at=datetime(2026, 5, 12, 12, 0, 0, tzinfo=timezone.utc),
        evidence=[EvidenceRef(
            observation_id="twitter:account:42", platform="twitter",
            url="https://example.test/janedoeacm3",
        )],
        metadata={"matched_exec": "Jane Doe", "matched_legit": "janedoeacme"},
    )


# ---------------------------------------------------------------------------


@respx.mock
def test_send_posts_events_api_v2_envelope(monkeypatch) -> None:
    monkeypatch.setenv("PD_RK", "0123456789abcdef0123456789abcdef")
    route = respx.post("https://events.pagerduty.com/v2/enqueue").mock(
        return_value=httpx.Response(202, json={"status": "success"}),
    )
    a = PagerDutyAlerter(routing_key_env="PD_RK", source="klaxon-prod")
    a.send(_finding())

    assert route.called
    body = route.calls[0].request.read()
    import json
    env = json.loads(body)
    assert env["routing_key"] == "0123456789abcdef0123456789abcdef"
    assert env["event_action"] == "trigger"
    assert env["dedup_key"] == "f1"  # finding_id strategy (default)
    p = env["payload"]
    assert p["source"] == "klaxon-prod"
    assert p["severity"] == "error"  # HIGH → error
    assert p["component"] == "imp"
    assert p["group"] == "impersonation"
    assert p["custom_details"]["score"] == 80.0
    assert p["custom_details"]["metadata"]["matched_exec"] == "Jane Doe"
    # Evidence URLs are surfaced as PD links so the on-call can click through.
    assert env["links"][0]["href"] == "https://example.test/janedoeacm3"


@pytest.mark.parametrize("sev,expected", [
    (Severity.LOW, "info"),
    (Severity.MEDIUM, "warning"),
    (Severity.HIGH, "error"),
    (Severity.CRITICAL, "critical"),
])
@respx.mock
def test_severity_mapping(sev: Severity, expected: str, monkeypatch) -> None:
    monkeypatch.setenv("PD_RK", "k" * 32)
    route = respx.post("https://events.pagerduty.com/v2/enqueue").mock(
        return_value=httpx.Response(202),
    )
    PagerDutyAlerter(routing_key_env="PD_RK").send(_finding(severity=sev))
    import json
    env = json.loads(route.calls[0].request.read())
    assert env["payload"]["severity"] == expected


@respx.mock
def test_kind_plus_entity_dedup_strategy(monkeypatch) -> None:
    """Two different findings of the same kind+detector collapse into one
    PagerDuty incident under the `kind+entity` strategy."""
    monkeypatch.setenv("PD_RK", "k" * 32)
    route = respx.post("https://events.pagerduty.com/v2/enqueue").mock(
        return_value=httpx.Response(202),
    )
    a = PagerDutyAlerter(routing_key_env="PD_RK", dedup_strategy="kind+entity")
    a.send(_finding(id_="f1"))
    a.send(_finding(id_="f2"))

    import json
    dedup_keys = {
        json.loads(call.request.read())["dedup_key"]
        for call in route.calls
    }
    assert len(dedup_keys) == 1
    assert "impersonation" in next(iter(dedup_keys))


def test_no_routing_key_means_send_is_a_noop(caplog) -> None:
    import logging
    a = PagerDutyAlerter()  # no routing_key, no env var
    with caplog.at_level(logging.INFO):
        a.send(_finding())
    assert any("pagerduty send skipped" in r.message for r in caplog.records)


@respx.mock
def test_routing_key_env_takes_precedence_over_direct(monkeypatch) -> None:
    """If both routing_key_env (with env var present) and routing_key are
    set, the env var wins — keeps secrets out of YAML."""
    monkeypatch.setenv("PD_RK", "env_value_" + "x" * 22)
    route = respx.post("https://events.pagerduty.com/v2/enqueue").mock(
        return_value=httpx.Response(202),
    )
    a = PagerDutyAlerter(routing_key_env="PD_RK", routing_key="yaml_value_xxx")
    a.send(_finding())
    import json
    env = json.loads(route.calls[0].request.read())
    assert env["routing_key"].startswith("env_value_")


@respx.mock
def test_4xx_propagates_for_runner_to_log(monkeypatch) -> None:
    monkeypatch.setenv("PD_RK", "k" * 32)
    respx.post("https://events.pagerduty.com/v2/enqueue").mock(
        return_value=httpx.Response(400, text="invalid routing_key"),
    )
    a = PagerDutyAlerter(routing_key_env="PD_RK")
    with pytest.raises(httpx.HTTPStatusError):
        a.send(_finding())

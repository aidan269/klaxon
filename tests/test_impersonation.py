"""Impersonation detector tests.

Two layers:
  1. Pure scoring (`score_account`) — fast, no I/O, table-driven.
  2. Detector + storage round-trip — proves run() actually queries observations,
     dedups via kv_state, and emits Findings with severity bands.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from socmon.config import BrandEntity, ExecutiveEntity
from socmon.detectors.impersonation import (
    ImpersonationDetector,
    score_account,
    severity_for,
    _has_confusables,
    _normalize_confusables,
)
from socmon.models import (
    AccountObservation,
    FindingKind,
    ObservationKind,
    Severity,
    TimeWindow,
)
from socmon.storage.sqlite import SqliteStorage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


NOW = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def brand() -> BrandEntity:
    return BrandEntity(
        name="Acme",
        aliases=["AcmeCorp", "Acme Corp"],
        domains=["acme.com", "acme.io"],
        legit_handles={"reddit": ["acme_official"], "twitter": ["acmecorp"]},
        logo_paths=[],  # avatar signal disabled in pure-scoring tests
    )


@pytest.fixture
def executives() -> list[ExecutiveEntity]:
    return [
        ExecutiveEntity(
            name="Jane Doe",
            title="CEO",
            high_value_target=True,
            legit_handles={"twitter": ["janedoeacme"]},
        ),
    ]


def _account(
    handle: str,
    *,
    platform: str = "reddit",
    display_name: str | None = None,
    bio: str | None = None,
    age_days: int = 365,
    avatar_phash: str | None = None,
) -> AccountObservation:
    created = NOW - timedelta(days=age_days)
    return AccountObservation(
        id=f"{platform}:account:t2_{handle}",
        platform=platform,
        kind=ObservationKind.ACCOUNT,
        author_handle=handle,
        author_id=f"t2_{handle}",
        text=bio,
        url=f"https://example.test/{handle}",
        created_at=created,
        collected_at=NOW,
        display_name=display_name,
        bio=bio,
        avatar_phash=avatar_phash,
        account_created_at=created,
    )


# ---------------------------------------------------------------------------
# Confusables
# ---------------------------------------------------------------------------


def test_confusables_normalize_cyrillic_a() -> None:
    # Cyrillic 'а' (U+0430) → Latin 'a'.
    assert _normalize_confusables("аcme") == "acme"
    assert _has_confusables("аcme")
    assert not _has_confusables("acme")


def test_confusables_handles_digits_and_symbols() -> None:
    assert _normalize_confusables("@cme0fficial") == "acmeofficial"


# ---------------------------------------------------------------------------
# Severity bands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("score,hit_exec,expected", [
    (10, False, None),
    (39.9, False, None),
    (40, False, Severity.MEDIUM),
    (69, False, Severity.MEDIUM),
    (70, False, Severity.HIGH),
    (84, False, Severity.HIGH),
    (85, False, Severity.CRITICAL),
    (100, False, Severity.CRITICAL),
    # Exec hit promotes high → critical.
    (70, True, Severity.CRITICAL),
    (60, True, Severity.MEDIUM),  # not high, exec promotion doesn't apply
])
def test_severity_for(score: float, hit_exec: bool, expected: Severity | None) -> None:
    assert severity_for(score, hit_exec=hit_exec) == expected


# ---------------------------------------------------------------------------
# Scoring — exclusions
# ---------------------------------------------------------------------------


def test_legit_handle_is_zero(brand, executives) -> None:
    acct = _account("acme_official")
    score, breakdown = score_account(acct, brand, executives, brand_phashes=[], now=NOW)
    assert score == 0.0
    assert breakdown["excluded"] == "legitimate_handle"


def test_legit_handle_match_is_case_insensitive(brand, executives) -> None:
    acct = _account("ACME_Official")
    score, _ = score_account(acct, brand, executives, brand_phashes=[], now=NOW)
    assert score == 0.0


def test_exec_legit_handle_excluded_per_platform(brand, executives) -> None:
    # janedoeacme is legit on twitter; same handle on reddit is NOT excluded.
    on_twitter = _account("janedoeacme", platform="twitter", age_days=2000)
    score_t, _ = score_account(on_twitter, brand, executives, brand_phashes=[], now=NOW)
    assert score_t == 0.0

    on_reddit = _account("janedoeacme", platform="reddit", age_days=10)
    score_r, _ = score_account(on_reddit, brand, executives, brand_phashes=[], now=NOW)
    assert score_r > 0


# ---------------------------------------------------------------------------
# Scoring — positive cases
# ---------------------------------------------------------------------------


def test_clear_typosquat_scores_high(brand, executives) -> None:
    """`acme_officia1` (with a 1 swapped for the trailing l) should land medium-or-better."""
    acct = _account(
        "acme_officia1",
        display_name="Acme Official",
        bio="The official Acme support account. acme.com",
        age_days=15,
    )
    score, breakdown = score_account(acct, brand, executives, brand_phashes=[], now=NOW)
    assert score >= 40, breakdown
    assert breakdown["signals"]["brand_keyword_in_handle"] is True
    assert breakdown["matched_legit"] == "acme_official"


def test_homoglyph_swap_scores_higher_than_clean_lookalike(brand, executives) -> None:
    """A handle that uses a Cyrillic 'а' to clone 'acme_official' should score
    higher (or at least not lower) than a clean substring match."""
    homoglyph = _account("аcme_official", age_days=5)
    plain = _account("acme_official_x", age_days=5)
    s_h, b_h = score_account(homoglyph, brand, executives, brand_phashes=[], now=NOW)
    s_p, _ = score_account(plain, brand, executives, brand_phashes=[], now=NOW)
    assert b_h["signals"]["homoglyph"] is True
    assert s_h >= s_p


def test_random_handle_scores_low(brand, executives) -> None:
    acct = _account("totally_unrelated_user", display_name="Random Person",
                    bio="cat photos", age_days=2000)
    score, _ = score_account(acct, brand, executives, brand_phashes=[], now=NOW)
    assert score < 40


def test_exec_match_is_recorded_in_breakdown(brand, executives) -> None:
    acct = _account("janedoeacm3", display_name="Jane Doe", bio="CEO of Acme",
                    age_days=3, platform="twitter")
    score, breakdown = score_account(acct, brand, executives, brand_phashes=[], now=NOW)
    # Exec promotion happens at the severity layer; here we just verify the signal
    # got attributed.
    assert breakdown["matched_exec"] == "Jane Doe"
    assert score >= 40


def test_account_age_signal_decays(brand, executives) -> None:
    young = _account("acmecorp_help", age_days=5)
    old = _account("acmecorp_help", age_days=2000)
    s_young, b_young = score_account(young, brand, executives, brand_phashes=[], now=NOW)
    s_old, b_old = score_account(old, brand, executives, brand_phashes=[], now=NOW)
    assert b_young["weighted"]["age"] == 10.0
    assert b_old["weighted"]["age"] == 0.0
    assert s_young > s_old


# ---------------------------------------------------------------------------
# Detector + storage round-trip
# ---------------------------------------------------------------------------


@pytest.fixture
def storage(tmp_path) -> SqliteStorage:
    s = SqliteStorage(f"sqlite:///{tmp_path}/socmon.db")
    s.init_schema()
    return s


def test_detector_emits_finding_and_dedups_on_rerun(storage, brand, executives) -> None:
    storage.upsert_observations([
        _account("acme_officia1", display_name="Acme", bio="acme.com support", age_days=10),
        _account("totally_unrelated_user", age_days=2000),
    ])

    det = ImpersonationDetector(brand=brand, executives=executives, name="imp")
    window = TimeWindow(start=NOW - timedelta(days=365 * 10), end=NOW + timedelta(days=1))

    findings = list(det.run(storage, window))
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == FindingKind.IMPERSONATION
    assert f.severity in (Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL)
    assert f.evidence and "acme_officia1" in f.evidence[0].observation_id

    # Storage dedup: insert succeeds first time.
    assert storage.insert_finding(f) is True
    assert storage.insert_finding(f) is False

    # Detector dedup via kv_state: re-running over an unchanged account yields nothing.
    findings2 = list(det.run(storage, window))
    assert findings2 == []


def test_detector_rescans_when_account_changes(storage, brand, executives) -> None:
    a1 = _account("acme_officia1", display_name="X", age_days=10)
    storage.upsert_observations([a1])

    det = ImpersonationDetector(brand=brand, executives=executives, name="imp")
    window = TimeWindow(start=NOW - timedelta(days=365), end=NOW + timedelta(days=1))

    assert len(list(det.run(storage, window))) == 1

    # Same account id but updated bio — signature changes, detector should re-evaluate.
    a2 = _account("acme_officia1", display_name="Acme Official Support",
                  bio="Official Acme account. Visit acme.com", age_days=10)
    storage.upsert_observations([a2])

    findings = list(det.run(storage, window))
    assert len(findings) == 1

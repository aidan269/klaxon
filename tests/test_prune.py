"""Tests for `Storage.prune()` + the `socmon prune` CLI.

Covers:
  - prune only deletes rows strictly older than the cutoff
  - dry_run returns the counts without mutating storage
  - watermarks and kv_state survive a prune (correctness-critical)
  - the CLI reports counts correctly and respects --dry-run / --yes
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from socmon.cli import main
from socmon.models import (
    EvidenceRef,
    Finding,
    FindingKind,
    Observation,
    ObservationKind,
    Severity,
)
from socmon.storage.sqlite import SqliteStorage


UTC = timezone.utc
NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def storage(tmp_path) -> SqliteStorage:
    s = SqliteStorage(f"sqlite:///{tmp_path}/socmon.db")
    s.init_schema()
    return s


def _obs(idx: int, *, created_at: datetime) -> Observation:
    return Observation(
        id=f"reddit:post:{idx}",
        platform="reddit",
        kind=ObservationKind.POST,
        author_handle=f"user_{idx}",
        text=f"post #{idx}",
        url=f"https://example.test/p/{idx}",
        created_at=created_at,
        collected_at=created_at,
    )


def _finding(idx: int, *, detected_at: datetime) -> Finding:
    return Finding(
        id=f"f{idx}",
        kind=FindingKind.IMPERSONATION,
        detector="imp",
        severity=Severity.HIGH,
        score=80.0,
        title=f"finding #{idx}",
        summary="...",
        detected_at=detected_at,
        evidence=[EvidenceRef(observation_id=f"reddit:account:t2_{idx}", platform="reddit")],
    )


def _seed_mixed_ages(storage) -> None:
    # Old rows (>= 100 days back) and recent rows (<= 7 days back).
    for i in range(5):
        storage.upsert_observations([_obs(i, created_at=NOW - timedelta(days=100 + i))])
        storage.insert_finding(_finding(100 + i, detected_at=NOW - timedelta(days=100 + i)))
    for i in range(3):
        storage.upsert_observations([_obs(50 + i, created_at=NOW - timedelta(days=i))])
        storage.insert_finding(_finding(200 + i, detected_at=NOW - timedelta(days=i)))


# ---------------------------------------------------------------------------
# Storage.prune behavior
# ---------------------------------------------------------------------------


def test_prune_deletes_only_rows_older_than_cutoff(storage) -> None:
    _seed_mixed_ages(storage)
    obs_before = sum(1 for _ in storage.query_observations())
    findings_before = sum(1 for _ in storage.query_findings())
    assert obs_before == 8
    assert findings_before == 8

    cutoff = NOW - timedelta(days=30)
    n_obs, n_findings = storage.prune(before=cutoff)
    assert n_obs == 5
    assert n_findings == 5

    obs_after = list(storage.query_observations())
    findings_after = list(storage.query_findings())
    assert len(obs_after) == 3
    assert len(findings_after) == 3
    # Survivors are all newer than cutoff.
    assert all(o.created_at >= cutoff for o in obs_after)
    assert all(f.detected_at >= cutoff for f in findings_after)


def test_prune_dry_run_reports_counts_but_changes_nothing(storage) -> None:
    _seed_mixed_ages(storage)
    cutoff = NOW - timedelta(days=30)

    n_obs, n_findings = storage.prune(before=cutoff, dry_run=True)
    assert n_obs == 5
    assert n_findings == 5

    # Nothing actually deleted.
    assert sum(1 for _ in storage.query_observations()) == 8
    assert sum(1 for _ in storage.query_findings()) == 8


def test_prune_preserves_watermarks_and_kv_state(storage) -> None:
    _seed_mixed_ages(storage)
    # Set a watermark and a kv_state entry well before the cutoff so a buggy
    # prune that also wiped those tables would lose them.
    old_ts = NOW - timedelta(days=200)
    storage.set_watermark("reddit-main", old_ts)
    storage.set_state("impersonation", "reddit:t2_x", "sig_abc")

    storage.prune(before=NOW - timedelta(days=30))

    # Both must survive — correctness depends on them.
    assert storage.watermark("reddit-main") is not None
    assert storage.get_state("impersonation", "reddit:t2_x") == "sig_abc"


def test_prune_returns_zero_zero_when_nothing_old_enough(storage) -> None:
    # Only recent data — nothing should be pruned.
    for i in range(3):
        storage.upsert_observations([_obs(i, created_at=NOW - timedelta(hours=i))])

    n_obs, n_findings = storage.prune(before=NOW - timedelta(days=30))
    assert (n_obs, n_findings) == (0, 0)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


@pytest.fixture
def cfg_file(tmp_path) -> str:
    """A minimal socmon.yaml pointing at a tmpdir-scoped SQLite DB."""
    db_path = tmp_path / "socmon.db"
    yaml_path = tmp_path / "socmon.yaml"
    yaml_path.write_text(
        "organization: t\n"
        "brand: { name: t }\n"
        f"storage: {{ backend: sqlite, dsn: 'sqlite:///{db_path}' }}\n"
    )
    return str(yaml_path)


def test_cli_prune_dry_run_does_not_delete(cfg_file, tmp_path) -> None:
    db_dsn = f"sqlite:///{tmp_path}/socmon.db"
    storage = SqliteStorage(db_dsn)
    storage.init_schema()
    _seed_mixed_ages(storage)

    result = CliRunner().invoke(
        main,
        ["--config", cfg_file, "prune", "--older-than-days", "30", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "DRY RUN: would delete 5 observation(s) + 5 finding(s)" in result.output
    # Nothing actually deleted.
    assert sum(1 for _ in storage.query_observations()) == 8


def test_cli_prune_with_yes_flag_actually_deletes(cfg_file, tmp_path) -> None:
    db_dsn = f"sqlite:///{tmp_path}/socmon.db"
    storage = SqliteStorage(db_dsn)
    storage.init_schema()
    _seed_mixed_ages(storage)

    result = CliRunner().invoke(
        main,
        ["--config", cfg_file, "prune", "--older-than-days", "30", "--yes"],
    )
    assert result.exit_code == 0, result.output
    assert "deleted 5 observation(s) + 5 finding(s)" in result.output
    assert sum(1 for _ in storage.query_observations()) == 3


def test_cli_prune_with_nothing_to_do(cfg_file, tmp_path) -> None:
    db_dsn = f"sqlite:///{tmp_path}/socmon.db"
    storage = SqliteStorage(db_dsn)
    storage.init_schema()  # empty DB

    result = CliRunner().invoke(
        main,
        ["--config", cfg_file, "prune", "--older-than-days", "30"],
    )
    assert result.exit_code == 0, result.output
    assert "Nothing to prune" in result.output


def test_cli_prune_rejects_zero_or_negative_days(cfg_file) -> None:
    for n in ("0", "-3"):
        result = CliRunner().invoke(
            main,
            ["--config", cfg_file, "prune", "--older-than-days", n, "--yes"],
        )
        assert result.exit_code != 0
        assert "--older-than-days" in result.output


def test_cli_prune_aborts_without_yes_or_confirmation(cfg_file, tmp_path) -> None:
    db_dsn = f"sqlite:///{tmp_path}/socmon.db"
    storage = SqliteStorage(db_dsn)
    storage.init_schema()
    _seed_mixed_ages(storage)

    # Simulate the user typing 'n' at the confirm prompt.
    result = CliRunner().invoke(
        main,
        ["--config", cfg_file, "prune", "--older-than-days", "30"],
        input="n\n",
    )
    assert result.exit_code != 0  # click abort
    # And nothing deleted.
    assert sum(1 for _ in storage.query_observations()) == 8

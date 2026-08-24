"""The upgrade command: what it writes, in what order, and when it refuses."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from migration_fixtures import (
    claim_and_complete,
    event_count,
    last_event_type,
    legacy_pending_request,
    legacy_terminal_event,
    make_store,
    publish_current_request,
    workflow_id,
)
from typer.testing import CliRunner

from maestro import cli
from maestro.state import migration_state as ms
from maestro.state import upgrade_backfill as ub
from maestro.state.funding_workflow import head_key, superseded_key


@pytest.fixture
def config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    path = tmp_path / "operator.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


@pytest.fixture
def store(tmp_path, config_path):
    del config_path  # ordering only: the config names this database
    return make_store(tmp_path)


def _run(config_path, *extra):
    return CliRunner().invoke(
        cli.app,
        ["upgrade-backfill", "--config", str(config_path), "--no-require-quiesce", *extra],
    )


def _boom(*_args, **_kwargs):
    raise RuntimeError("injected crash")


def _quarantine_payloads(store):
    rows = store.list_system_events_by_type(ub.QUARANTINE_EVENT, limit=None)
    return [row["payload"] for row in rows]


def _head_request_ids(store):
    return sorted(head["request_id"] for head in store.list_funding_workflow_heads())


def _schema_less_ack(store, approval_id):
    store.save_system_event(
        f"run_{approval_id}", "telegram_approval_ack", {"approval_id": approval_id}
    )


# --- ordinary runs --------------------------------------------------------


def test_a_clean_upgrade_completes_and_reports_each_category(store, config_path):
    legacy_pending_request(store, "req-1")

    result = _run(config_path)

    assert result.exit_code == 0, result.stdout
    assert "heads_created=1" in result.stdout
    assert "status=completed" in result.stdout
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.COMPLETED


def test_migration_completed_is_the_last_row_written(store, config_path):
    """Anything after it lands in a database that has already declared itself
    migrated, and would be invisible to the next run's classification."""
    legacy_pending_request(store, "req-1")
    _schema_less_ack(store, "ap-1")
    store.mark_signal_package_consumed("sig-1", "run-1")

    _run(config_path)

    assert last_event_type(store) == ms.COMPLETED_EVENT


def test_a_blocking_quarantine_aborts_before_completion(store, config_path):
    legacy_pending_request(store, "req-1")
    legacy_pending_request(store, "req-2")

    result = _run(config_path)

    assert result.exit_code == 1
    assert "ambiguous_pending_requests" in result.stdout
    assert "Do NOT restart services" in result.stdout
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.MIGRATING


def test_a_non_blocking_quarantine_does_not_stop_completion(store, config_path):
    """The runtime already refuses to auto-execute either of these, so an
    operator-owned record is enough; blocking on them would strand a system
    that is safe to run."""
    _schema_less_ack(store, "ap-1")
    store.mark_signal_package_consumed("sig-1", "run-1")

    result = _run(config_path)

    assert result.exit_code == 0, result.stdout
    assert "reason=completion_unprovable" in result.stdout
    assert "reason=legacy_dispatch_no_manifest" in result.stdout
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.COMPLETED


def test_a_completed_migration_reruns_as_a_no_op(store, config_path):
    legacy_pending_request(store, "req-1")
    _run(config_path)
    before = event_count(store)

    result = _run(config_path)

    assert result.exit_code == 0, result.stdout
    assert "state=completed" in result.stdout
    assert event_count(store) == before


def test_an_invalid_marker_aborts_without_writing(store, config_path):
    store.save_system_event(
        "r", ms.COMPLETED_EVENT, {"cutoff": 3, "duplicate_key": ms.COMPLETED_KEY}
    )
    before = event_count(store)

    result = _run(config_path)

    assert result.exit_code == 1
    assert "completed_without_started" in result.stdout
    assert event_count(store) == before


# --- the quiesce barrier --------------------------------------------------


def test_a_live_writer_unit_refuses_the_run(store, config_path, monkeypatch):
    monkeypatch.setattr(
        cli.quiesce,
        "verify_quiesced",
        lambda **_: cli.quiesce.QuiesceReport(
            active_units=("maestro-dashboard.service",), queued_jobs=()
        ),
    )
    result = CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "maestro-dashboard.service" in result.stdout
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.NOT_STARTED


def test_a_queued_job_refuses_the_run(config_path, monkeypatch):
    monkeypatch.setattr(
        cli.quiesce,
        "verify_quiesced",
        lambda **_: cli.quiesce.QuiesceReport(
            active_units=(), queued_jobs=("maestro-heartbeat.service",)
        ),
    )
    result = CliRunner().invoke(cli.app, ["upgrade-backfill", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "queued_job" in result.stdout


def test_the_writer_lock_is_held_for_the_whole_operation(store, config_path, monkeypatch):
    """Not per insert. The cutoff means "nothing existed past here", and that
    is only true if no cooperating writer can append between observing it and
    completing."""
    held: list[bool] = []
    original = ub.backfill_funding_heads

    def spy(store_, run_id, *, cutoff):
        held.append(store_.holds_writer_lock())
        return original(store_, run_id, cutoff=cutoff)

    monkeypatch.setattr(ub, "backfill_funding_heads", spy)
    _run(config_path)

    assert held == [True]


# --- crash injection ------------------------------------------------------


def test_a_crash_before_the_start_marker_leaves_no_ownership(store, monkeypatch):
    monkeypatch.setattr(ub.migration_state, "start_migration", _boom)
    with pytest.raises(RuntimeError):
        ub.run_upgrade_backfill(store, "run-1")

    assert ms.load_migration_state(store).phase is ms.MigrationPhase.NOT_STARTED


def test_a_crash_after_the_start_marker_reuses_the_exact_cutoff(store, monkeypatch):
    legacy_pending_request(store, "req-1")
    monkeypatch.setattr(ub, "backfill_funding_heads", _boom)
    with pytest.raises(RuntimeError):
        ub.run_upgrade_backfill(store, "run-1")
    first = ms.load_migration_state(store)
    legacy_pending_request(store, "req-late", month_key="2026-09")
    monkeypatch.undo()

    ub.run_upgrade_backfill(store, "run-2")

    assert ms.load_migration_state(store).cutoff == first.cutoff


def test_a_crash_midway_through_the_migration_adopts_what_landed(store, monkeypatch):
    legacy_pending_request(store, "req-a", month_key="2026-08")
    legacy_pending_request(store, "req-b", month_key="2026-09")
    monkeypatch.setattr(ub, "classify_legacy_approvals", _boom)
    with pytest.raises(RuntimeError):
        ub.run_upgrade_backfill(store, "run-1")
    landed = _head_request_ids(store)
    monkeypatch.undo()

    result = ub.run_upgrade_backfill(store, "run-2")

    assert landed == ["req-a", "req-b"]
    assert _head_request_ids(store) == landed
    assert result.backfill.heads_created == 0
    assert result.backfill.heads_already_coherent == 2


def test_a_crash_during_quarantine_classification_resumes_deterministically(store, monkeypatch):
    _schema_less_ack(store, "ap-1")
    _schema_less_ack(store, "ap-2")
    monkeypatch.setattr(ub, "classify_legacy_dispatches", _boom)
    with pytest.raises(RuntimeError):
        ub.run_upgrade_backfill(store, "run-1")
    first = _quarantine_payloads(store)
    monkeypatch.undo()

    ub.run_upgrade_backfill(store, "run-2")

    assert first != []
    assert _quarantine_payloads(store)[: len(first)] == first


def test_the_runtime_stays_gated_until_completion_lands(store, monkeypatch):
    legacy_pending_request(store, "req-1")
    monkeypatch.setattr(ub.migration_state, "complete_migration", _boom)
    with pytest.raises(RuntimeError):
        ub.run_upgrade_backfill(store, "run-1")

    assert ms.load_migration_state(store).phase is ms.MigrationPhase.MIGRATING


def test_a_completed_migration_leaves_no_pending_migration_writes(store):
    legacy_pending_request(store, "req-1")
    ub.run_upgrade_backfill(store, "run-1")
    after = event_count(store)

    ub.run_upgrade_backfill(store, "run-2")

    assert event_count(store) == after


# --- rollback, old-code writes, re-upgrade --------------------------------


def test_old_code_writes_after_a_completed_migration_refuse_the_rerun(store, config_path):
    """migration_completed(cutoff=N), a rollback, old-binary writes at N+1..,
    then this binary again. Seeing only the completed marker and calling it
    done would leave that old-generation state unclassified forever."""
    _run(config_path)
    legacy_terminal_event(store, "req-rolled-back")

    result = _run(config_path)

    assert result.exit_code == 1
    assert "reupgrade_after_rollback" in result.stdout
    assert "req-rolled-back" in result.stdout
    assert "legacy_terminal_without_completion" in result.stdout


def test_a_post_cutoff_request_with_no_head_is_also_evidence(store, config_path):
    _run(config_path)
    legacy_pending_request(store, "req-old-writer", month_key="2026-09")

    result = _run(config_path)

    assert result.exit_code == 1
    assert "request_without_head" in result.stdout


def test_a_post_cutoff_schema_less_approval_ack_is_also_evidence(store, config_path):
    """This generation has never written an ack without an int schema_version
    (one write site, always versioned), so a bare one above the cutoff is
    positive evidence of an old writer -- not something to merge quietly."""
    _run(config_path)
    _schema_less_ack(store, "ap-rolled-back")

    result = _run(config_path)

    assert result.exit_code == 1
    assert "reupgrade_after_rollback" in result.stdout
    assert "legacy_ack_without_schema_version" in result.stdout
    assert "ap-rolled-back" in result.stdout


def test_an_ordinary_versioned_ack_is_not_mistaken_for_a_rollback(store, config_path):
    """Current-generation activity must pass the rerun untouched."""
    _run(config_path)
    store.save_system_event(
        "run_ap",
        "telegram_approval_ack",
        {"approval_id": "ap-current", "schema_version": 2},
    )

    result = _run(config_path)

    assert result.exit_code == 0, result.stdout
    assert "reupgrade_after_rollback" not in result.stdout


def test_ordinary_current_generation_activity_is_not_mistaken_for_a_rollback(store, config_path):
    _run(config_path)
    publish_current_request(store, "req-new", month_key="2026-09")
    claim_and_complete(store, "req-new", month_key="2026-09")

    result = _run(config_path)

    assert result.exit_code == 0, result.stdout
    assert "reupgrade_after_rollback" not in result.stdout


def test_the_refusal_does_not_start_a_second_migration_epoch(store, config_path):
    _run(config_path)
    cutoff = ms.load_migration_state(store).cutoff
    legacy_terminal_event(store, "req-rolled-back")
    before = event_count(store)

    _run(config_path)

    state = ms.load_migration_state(store)
    assert state.phase is ms.MigrationPhase.COMPLETED
    assert state.cutoff == cutoff
    assert len(store.list_system_events_by_type(ms.STARTED_EVENT, limit=None)) == 1
    assert event_count(store) == before


def test_an_operator_resolution_lets_a_blocked_migration_finish(store, config_path):
    """The documented manual step: supersede the request the operator did not
    mean, by hand, then rerun. The migration never picks for them."""
    legacy_pending_request(store, "req-1")
    legacy_pending_request(store, "req-2")
    assert _run(config_path).exit_code == 1

    store.save_system_event(
        "run-operator",
        "funding_workflow_superseded",
        {
            "duplicate_key": superseded_key(workflow_id(), "req-2"),
            "workflow_id": workflow_id(),
            "request_id": "req-2",
            "reason": "operator_migration_decision",
        },
    )

    result = _run(config_path)

    assert result.exit_code == 0, result.stdout
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.COMPLETED
    assert store.duplicate_key_exists(head_key(workflow_id(), 1))
    assert store.load_funding_workflow_head(workflow_id())["request_id"] == "req-1"

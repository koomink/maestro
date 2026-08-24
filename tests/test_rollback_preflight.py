"""Rollback preflight: exhaustive, read-only, and specific about why.

Its one job is to refuse a rollback the old binary cannot survive. It is not a
repair tool: R4 in particular fails on a missing compatibility projection
rather than writing one, because complete_workflow commits both legs in a
single transaction and a gap therefore means corruption, a manual mutation or
an intermediate build -- writing the event would erase which.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import yaml
from migration_fixtures import (
    claim_and_complete,
    claim_only,
    delete_events,
    event_count,
    make_store,
    publish_current_request,
)
from typer.testing import CliRunner

from maestro import cli
from maestro.ops import quiesce
from maestro.state import migration_state as ms
from maestro.state import rollback_preflight as rollback_preflight_module
from maestro.state.rollback_preflight import run_rollback_preflight


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


def _start_migration(store):
    with store.writer_lock("test"):
        return ms.start_migration(store, "run-migrate")


def _complete_migration(store):
    with store.writer_lock("test"):
        state = ms.start_migration(store, "run-migrate")
        ms.complete_migration(store, "run-migrate", cutoff=state.cutoff)


def _consumed_but_unsettled(store, signal_run_id, *, with_manifest=True):
    if with_manifest:
        store.save_system_event(
            signal_run_id,
            "signal_dispatch_manifest",
            {
                "signal_run_id": signal_run_id,
                "groups": [],
                "duplicate_key": f"dispatch-manifest:{signal_run_id}",
            },
        )
    store.mark_signal_package_consumed(signal_run_id, f"approval_{signal_run_id}")


def _versioned_ack(store, approval_id):
    store.save_system_event(
        f"run_{approval_id}",
        "telegram_approval_ack",
        {"approval_id": approval_id, "status": "approved", "schema_version": 2},
    )


def _schema_less_ack(store, approval_id):
    store.save_system_event(
        f"run_{approval_id}", "telegram_approval_ack", {"approval_id": approval_id}
    )


def _named(result):
    return [(f.invariant, f.identifier) for f in result.failures]


# --- R0 -------------------------------------------------------------------


def test_a_clean_current_generation_database_is_safe(store):
    publish_current_request(store, "req-1")
    claim_and_complete(store, "req-1")
    _complete_migration(store)

    assert run_rollback_preflight(store).safe is True


def test_a_pre_migration_database_with_nothing_incompatible_is_safe(store):
    """NOT_STARTED is not itself a failure. Whether incompatible state exists
    is what R1-R4 answer, and they run regardless."""
    assert run_rollback_preflight(store).safe is True


def test_r0_refuses_a_half_finished_migration(store):
    _start_migration(store)

    assert _named(run_rollback_preflight(store)) == [
        ("R0_migration_state", str(ms.load_migration_state(store).cutoff))
    ]


def test_r0_refuses_contradictory_markers(store):
    store.save_system_event(
        "r", ms.COMPLETED_EVENT, {"cutoff": 1, "duplicate_key": ms.COMPLETED_KEY}
    )
    failures = run_rollback_preflight(store).failures

    assert failures[0].invariant == "R0_migration_state"
    assert "completed_without_started" in failures[0].detail


# --- R1 -------------------------------------------------------------------


def test_r1_flags_a_claim_with_no_completion(store):
    publish_current_request(store, "req-1")
    claim_only(store, "req-1")
    _complete_migration(store)

    assert _named(run_rollback_preflight(store)) == [
        ("R1_workflow_claim_unresolved", "req-1:funding")
    ]


def test_r1_names_a_claim_the_operator_view_deliberately_hides(store):
    """list_incomplete_workflows drops a claim the head no longer points at.

    That filter is right for an operator card: Resume would be refused as
    ``not_head``, so offering it would leave a button that can never work. It
    is wrong for rollback. The old binary never reads heads at all -- it sees
    only a request event with status "pending" and no legacy ack -- so it would
    re-run the transition regardless of where the head sits.

    The head is moved here by a raw write, which is how it happens outside the
    normal path: the convergence sweep rolling a dangling head back to an
    earlier version, or an operator's manual repair. publish_contribution_request
    would refuse it (``head_claimed``), and that refusal is the protection
    working.
    """
    from migration_fixtures import request_payload

    from maestro.state.funding_workflow import (
        head_key,
        list_incomplete_workflows,
        workflow_id_from_request,
    )

    publish_current_request(store, "req-1")
    claim_only(store, "req-1")
    workflow = workflow_id_from_request(request_payload("req-1"))
    store.save_system_event(
        "run-repair",
        "funding_workflow_head",
        {
            "duplicate_key": head_key(workflow, 2),
            "workflow_id": workflow,
            "version": 2,
            "request_id": "req-elsewhere",
            "phase": "funding",
            "status": "pending",
        },
    )
    _complete_migration(store)

    assert [row["request_id"] for row in list_incomplete_workflows(store)] == []
    assert _named(run_rollback_preflight(store)) == [
        ("R1_workflow_claim_unresolved", "req-1:funding")
    ]


def test_r1_reports_one_failure_per_request_not_per_attempt(store):
    publish_current_request(store, "req-1")
    claim_only(store, "req-1", attempt=1)
    claim_only(store, "req-1", attempt=2)
    _complete_migration(store)

    assert len(run_rollback_preflight(store).failures) == 1


def test_r1_matches_on_phase(store):
    publish_current_request(store, "req-1", phase="budget")
    claim_only(store, "req-1", phase="budget")
    _complete_migration(store)

    assert _named(run_rollback_preflight(store)) == [
        ("R1_workflow_claim_unresolved", "req-1:budget")
    ]


# --- R2 -------------------------------------------------------------------


def test_r2_uses_the_authoritative_settled_definition(store):
    _consumed_but_unsettled(store, "sig-1")
    _complete_migration(store)

    assert _named(run_rollback_preflight(store)) == [("R2_dispatch_unsettled", "sig-1")]


def test_r2_accepts_a_settled_dispatch(store):
    _consumed_but_unsettled(store, "sig-1")
    store.save_system_event("sig-1", "signal_approval_pending", {"signal_run_id": "sig-1"})
    _complete_migration(store)

    assert run_rollback_preflight(store).safe is True


def test_r2_is_not_windowed_at_fifty(store):
    """A window would silently pass a rollback over the 51st unfinished run."""
    for index in range(60):
        _consumed_but_unsettled(store, f"sig-{index:03d}")
    _complete_migration(store)

    failures = [
        f for f in run_rollback_preflight(store).failures if f.invariant == "R2_dispatch_unsettled"
    ]
    assert len(failures) == 60


# --- R3 -------------------------------------------------------------------


def test_r3_flags_a_versioned_ack_with_no_resolution(store):
    _versioned_ack(store, "ap-1")
    _complete_migration(store)

    assert _named(run_rollback_preflight(store)) == [("R3_approval_unresolved", "ap-1")]


def test_r3_ignores_a_schema_less_ack(store):
    """Both binaries read a schema-less ack as terminal. Not an incompatibility."""
    _schema_less_ack(store, "ap-1")
    _complete_migration(store)

    assert run_rollback_preflight(store).safe is True


def test_r3_accepts_a_resolved_approval(store):
    _versioned_ack(store, "ap-1")
    store.save_system_event(
        "run_ap-1", "telegram_approval_resolution_completed", {"approval_id": "ap-1"}
    )
    _complete_migration(store)

    assert run_rollback_preflight(store).safe is True


# --- R4 -------------------------------------------------------------------


def test_r4_fails_on_a_missing_projection_and_does_not_repair_it(store):
    publish_current_request(store, "req-1")
    claim_and_complete(store, "req-1")
    assert delete_events(store, "contribution_funding_request_ack") == 1
    _complete_migration(store)

    result = run_rollback_preflight(store)

    assert _named(result) == [("R4_missing_legacy_projection", "req-1:funding")]
    assert store.list_system_events_by_type("contribution_funding_request_ack", limit=None) == []


def test_r4_covers_the_budget_projection_too(store):
    publish_current_request(store, "req-b", phase="budget")
    claim_and_complete(store, "req-b", phase="budget")
    assert delete_events(store, "contribution_budget_request_decision") == 1
    _complete_migration(store)

    assert _named(run_rollback_preflight(store)) == [
        ("R4_missing_legacy_projection", "req-b:budget")
    ]


def test_r4_is_the_mirror_of_the_runtime_reading_its_own_completion(store, tmp_path):
    """Current runtime truth and rollback compatibility are different facts.

    tests/test_authoritative_funding_state.py runs this exact database the
    other way round: the loader still understands the completion. Both are
    correct at once, which is the point of keeping the projection while not
    reading it.
    """
    from maestro.state.funding_workflow import is_request_pending

    publish_current_request(store, "req-1")
    claim_and_complete(store, "req-1")
    delete_events(store, "contribution_funding_request_ack")
    _complete_migration(store)

    assert is_request_pending(store, "req-1", "funding") is False
    assert run_rollback_preflight(store).safe is False


# --- shape of the whole run -----------------------------------------------


def test_preflight_holds_the_writer_lock_for_the_whole_interval(store, monkeypatch):
    """R0-R4 must describe one writer-fenced interval, not five loose reads.

    Systemd quiesce stops deployed services; it does nothing about a
    cooperating `maestro` CLI or recovery script an operator runs by hand.
    Owning the lock here -- re-entrant, so callers that already hold it nest
    safely -- is what makes it impossible to run an unfenced preflight.
    """
    observed: list[bool] = []
    original = rollback_preflight_module._r1_workflow_claims

    def spy(store_):
        observed.append(store_.holds_writer_lock())
        return original(store_)

    monkeypatch.setattr(rollback_preflight_module, "_r1_workflow_claims", spy)

    run_rollback_preflight(store)

    assert observed == [True]


def test_a_competing_writer_cannot_commit_during_the_preflight_interval(store, monkeypatch):
    """The race the lock exists to close: R1 reads safe, another writer appends
    incompatible state, R2-R4 read the mutated database and report SAFE over
    state that was never actually preflighted."""
    started = threading.Event()
    release = threading.Event()
    original = rollback_preflight_module._r1_workflow_claims

    def gate(store_):
        # Reached only from inside the fenced interval.
        started.set()
        assert release.wait(timeout=10)
        return original(store_)

    monkeypatch.setattr(rollback_preflight_module, "_r1_workflow_claims", gate)
    outcome: list[str] = []
    writer_errors: list[BaseException] = []
    preflight_results: list[object] = []
    preflight_errors: list[BaseException] = []

    def competing_writer():
        try:
            with store.writer_lock("competing-writer", timeout_seconds=0.5):
                store.save_system_event(
                    "run-competing",
                    "telegram_approval_ack",
                    {"approval_id": "ap-raced", "schema_version": 2},
                )
            outcome.append("committed")
        except TimeoutError:
            outcome.append("blocked")
        except BaseException as exc:  # pragma: no cover - diagnostic only
            writer_errors.append(exc)

    def run_preflight():
        try:
            preflight_results.append(run_rollback_preflight(store))
        except BaseException as exc:  # pragma: no cover - diagnostic only
            preflight_errors.append(exc)

    preflight_thread = threading.Thread(target=run_preflight)
    preflight_thread.start()
    assert started.wait(timeout=10), "preflight never reached its fenced interval"

    writer_thread = threading.Thread(target=competing_writer)
    writer_thread.start()
    writer_thread.join(timeout=10)

    release.set()
    preflight_thread.join(timeout=10)

    assert not preflight_errors
    assert not writer_errors
    assert outcome == ["blocked"]
    # The fenced interval saw the database exactly as it was at entry.
    assert preflight_results[0].failures == ()
    assert store.list_system_events_by_type("telegram_approval_ack", limit=None) == []


def test_preflight_nested_under_a_caller_held_lock_still_runs(store):
    """Re-entrant: the upgrade path already holds the lock for its whole run,
    so a preflight invoked from inside it must not deadlock or refuse."""
    with store.writer_lock("outer"):
        assert run_rollback_preflight(store).safe is True


def test_preflight_writes_nothing_at_all(store):
    """Every branch, not just the happy one."""
    publish_current_request(store, "req-1")
    claim_only(store, "req-1")
    _consumed_but_unsettled(store, "sig-1", with_manifest=False)
    _versioned_ack(store, "ap-1")
    _start_migration(store)
    before = event_count(store)

    run_rollback_preflight(store)

    assert event_count(store) == before


def test_every_invariant_is_evaluated_not_just_the_first_to_fail(store):
    """An operator quiesces once and wants the whole list, not one problem per
    stop/start cycle."""
    publish_current_request(store, "req-1")
    claim_only(store, "req-1")
    _consumed_but_unsettled(store, "sig-1")
    _versioned_ack(store, "ap-1")
    _complete_migration(store)

    assert {f.invariant for f in run_rollback_preflight(store).failures} == {
        "R1_workflow_claim_unresolved",
        "R2_dispatch_unsettled",
        "R3_approval_unresolved",
    }


def test_every_failure_explains_why_rollback_is_unsafe(store):
    publish_current_request(store, "req-1")
    claim_only(store, "req-1")
    _complete_migration(store)

    failure = run_rollback_preflight(store).failures[0]
    assert failure.detail
    assert failure.event_ids != ()


# --- CLI ------------------------------------------------------------------


def _invoke(config_path, name="rollback-preflight"):
    return CliRunner().invoke(
        cli.app, [name, "--config", str(config_path), "--no-require-quiesce"]
    )


def test_the_cli_reports_each_failed_invariant_separately(store, config_path):
    _versioned_ack(store, "ap-1")
    _consumed_but_unsettled(store, "sig-1")
    _complete_migration(store)

    result = _invoke(config_path)

    assert result.exit_code == 1
    assert "invariant=R2_dispatch_unsettled" in result.stdout
    assert "invariant=R3_approval_unresolved" in result.stdout


def test_the_cli_reports_safe_on_a_clean_database(store, config_path):
    _complete_migration(store)

    result = _invoke(config_path)

    assert result.exit_code == 0
    assert "status=safe" in result.stdout


def test_the_legacy_command_name_still_works(store, config_path):
    """Renaming without an alias is how a rollback gets attempted with no
    preflight at all."""
    _complete_migration(store)

    assert _invoke(config_path, "approval-rollback-preflight").exit_code == 0


def test_quiesce_now_covers_every_writer_not_just_the_operator(store, config_path, monkeypatch):
    monkeypatch.setattr(
        quiesce,
        "verify_quiesced",
        lambda **_: quiesce.QuiesceReport(
            active_units=("maestro-symphony-signal-kr.timer",), queued_jobs=()
        ),
    )
    result = CliRunner().invoke(cli.app, ["rollback-preflight", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "maestro-symphony-signal-kr.timer" in result.stdout


def test_a_queued_job_alone_refuses_the_preflight(store, config_path, monkeypatch):
    monkeypatch.setattr(
        quiesce,
        "verify_quiesced",
        lambda **_: quiesce.QuiesceReport(
            active_units=(), queued_jobs=("maestro-run-once.service",)
        ),
    )
    result = CliRunner().invoke(cli.app, ["rollback-preflight", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "queued_job" in result.stdout

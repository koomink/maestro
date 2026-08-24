"""Migrate a database the pre-3a-4 binary actually wrote.

The fixture is not hand-shaped rows pushed through the current StateStore. It
is a ``.dump`` of a database seeded by the code at
209ed4f18ed57773a72ab4a146e49efae1747348 -- the merge-base, which has no
``state/funding_workflow.py`` and no ``signal_dispatch_manifest`` -- together
with that binary's own answers about which requests it considered pending.
See scripts/generate_legacy_3a_fixture.py.

One thing this deliberately does *not* claim: that opening it performs a table
migration. ``git diff 209ed4f..HEAD -- src/maestro/state/store.py`` contains no
DDL at all, so the schema is identical and the 3a-4 upgrade is an
event-semantics upgrade. A test that implied otherwise would be theatre;
``test_opening_a_legacy_database_needs_no_ddl_change`` asserts the real
property instead, and would fail the day that stops being true.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from maestro.state import migration_state as ms
from maestro.state import upgrade_backfill as ub
from maestro.state.funding_workflow import (
    funding_workflow_id,
    is_request_pending,
    superseded_key,
)
from maestro.state.rollback_preflight import run_rollback_preflight
from maestro.state.store import StateStore

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
LEGACY_SHA = "209ed4f18ed57773a72ab4a146e49efae1747348"

SCOPE = {
    "contribution_group_id": None,
    "account_id": "paper_cash",
    "execution_sleeve": "krw_contribution",
    "currency": "KRW",
}


def _workflow(month_key: str) -> str:
    return funding_workflow_id(month_key=month_key, **SCOPE)


@pytest.fixture
def legacy_db(tmp_path) -> Path:
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript((FIXTURES / "legacy_3a_state.sql").read_text())
    return path


def _schema_sql(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return sorted(
            str(row[0])
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchall()
        )


def _migrate(store) -> ub.UpgradeResult:
    return ub.run_upgrade_backfill(store, "run-migrate")


def _operator_resolves_the_ambiguity(store) -> None:
    """The documented manual step: supersede the request the operator did not
    mean, by hand. The migration never picks for them."""
    store.save_system_event(
        "run-operator",
        "funding_workflow_superseded",
        {
            "duplicate_key": superseded_key(_workflow("2026-09"), "req-amb-b"),
            "workflow_id": _workflow("2026-09"),
            "request_id": "req-amb-b",
            "reason": "operator_migration_decision",
        },
    )


def _fully_migrated(store):
    _migrate(store)
    _operator_resolves_the_ambiguity(store)
    return _migrate(store)


# --- provenance -----------------------------------------------------------


def test_the_fixture_records_the_commit_it_was_generated_from(legacy_db):
    del legacy_db
    assert LEGACY_SHA in (FIXTURES / "legacy_3a_state.sql").read_text()
    assert json.loads((FIXTURES / "legacy_3a_state.json").read_text())["source_commit"] == (
        LEGACY_SHA
    )


def test_the_fixture_carries_no_current_generation_rows(legacy_db):
    """If it did, it would not be testing a legacy upgrade at all."""
    with sqlite3.connect(legacy_db) as conn:
        types = {
            str(row[0])
            for row in conn.execute("SELECT DISTINCT event_type FROM system_events").fetchall()
        }
    assert not any(name.startswith("funding_workflow") for name in types)
    assert "signal_dispatch_manifest" not in types


def test_opening_a_legacy_database_needs_no_ddl_change(legacy_db):
    """The 3a-4 upgrade is an event-semantics upgrade, not a schema upgrade.

    Asserting it keeps a future schema change from slipping in unnoticed and
    silently making the fixture's DDL stale.
    """
    before = _schema_sql(legacy_db)
    StateStore(legacy_db)
    assert _schema_sql(legacy_db) == before


def test_the_new_reader_agrees_with_the_old_binary_before_migrating(legacy_db):
    """Pinned to the legacy binary's own answers, recorded at generation time.

    Only the requests whose lifecycle both generations can express: an ack the
    old code read as terminal is history the new reader deliberately declines
    to judge, which is checked separately below.
    """
    expected = json.loads((FIXTURES / "legacy_3a_state.json").read_text())
    store = StateStore(legacy_db)
    for request_id in ("req-clean", "req-amb-a", "req-amb-b"):
        assert is_request_pending(store, request_id, "funding") is (
            expected["pending_funding"][request_id]
        )


def test_a_legacy_ack_is_where_the_two_generations_deliberately_differ(legacy_db):
    """The old binary calls req-acked finished on the strength of the ack alone.

    The new reader declines to, because that event is the rollback
    compatibility projection and a bare one proves nothing about phase or
    lineage. The difference is resolved by the migration, under a barrier --
    not by either reader guessing.
    """
    expected = json.loads((FIXTURES / "legacy_3a_state.json").read_text())
    store = StateStore(legacy_db)

    assert expected["pending_funding"]["req-acked"] is False
    assert is_request_pending(store, "req-acked", "funding") is True
    assert _fully_migrated(store).completed is True
    assert "req-acked" not in {h["request_id"] for h in store.list_funding_workflow_heads()}


# --- the migration --------------------------------------------------------


def test_the_migration_blocks_on_the_ambiguous_pair(legacy_db):
    store = StateStore(legacy_db)

    result = _migrate(store)

    assert result.aborted_reason == "blocking_quarantine"
    assert {q.reason for q in result.backfill.blocking} == {"ambiguous_pending_requests"}
    assert result.backfill.blocking[0].detail["request_ids"] == ["req-amb-a", "req-amb-b"]
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.MIGRATING


def test_the_clean_pending_request_gets_a_live_head_even_on_the_blocked_run(legacy_db):
    """One workflow's ambiguity must not strand the others."""
    store = StateStore(legacy_db)

    _migrate(store)

    assert store.load_funding_workflow_head(_workflow("2026-08"))["request_id"] == "req-clean"


def test_after_the_operator_resolves_the_ambiguity_the_migration_completes(legacy_db):
    store = StateStore(legacy_db)

    result = _fully_migrated(store)

    assert result.completed is True
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.COMPLETED
    assert store.load_funding_workflow_head(_workflow("2026-09"))["request_id"] == "req-amb-a"


def test_the_terminal_legacy_requests_are_not_resurrected(legacy_db):
    """An ack or a decision is history, and history does not get a live head
    with a working Confirm button on it."""
    store = StateStore(legacy_db)

    _fully_migrated(store)

    heads = {h["request_id"] for h in store.list_funding_workflow_heads()}
    assert heads == {"req-clean", "req-amb-a"}


def test_no_approval_is_re_executed_and_none_gets_a_synthetic_resolution(legacy_db):
    store = StateStore(legacy_db)

    _fully_migrated(store)

    assert (
        store.list_system_events_by_type("telegram_approval_resolution_completed", limit=None)
        == []
    )
    quarantined = {q.identifier for q in ub.list_quarantines(store) if q.subsystem == "approval"}
    assert quarantined == {"ap-legacy-entered", "ap-legacy-unknown"}


def test_the_provably_complete_legacy_approval_is_left_alone(legacy_db):
    """One group on the run and a completion for it: that is proof, and proof
    does not need an operator."""
    store = StateStore(legacy_db)

    result = _fully_migrated(store)

    assert result.approvals.proven_complete == 1
    assert "ap-legacy-done" not in {q.identifier for q in ub.list_quarantines(store)}


def test_the_persisted_approval_is_marked_as_possibly_entered(legacy_db):
    store = StateStore(legacy_db)

    _fully_migrated(store)

    entered = [
        q for q in ub.list_quarantines(store) if q.identifier == "ap-legacy-entered"
    ]
    assert [q.reason for q in entered] == ["execution_may_have_been_entered"]


def test_the_manifestless_dispatch_is_quarantined_not_replayed(legacy_db):
    store = StateStore(legacy_db)

    _fully_migrated(store)

    quarantined = {q.identifier for q in ub.list_quarantines(store) if q.subsystem == "dispatch"}
    assert quarantined == {"sig-nomanifest"}


def test_migration_completed_is_the_last_row_written(legacy_db):
    store = StateStore(legacy_db)

    _fully_migrated(store)

    with sqlite3.connect(legacy_db) as conn:
        last = conn.execute(
            "SELECT event_type FROM system_events ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert last == ms.COMPLETED_EVENT


def test_rerunning_the_completed_migration_writes_nothing(legacy_db):
    store = StateStore(legacy_db)
    _fully_migrated(store)
    with sqlite3.connect(legacy_db) as conn:
        before = conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0]

    _migrate(store)

    with sqlite3.connect(legacy_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM system_events").fetchone()[0] == before


def test_migrating_adds_nothing_a_rollback_would_trip_over(legacy_db):
    """The migration writes a head, quarantine records and its own markers.

    The old binary reads none of them, so migrating cannot make a rollback less
    safe than it already was. What preflight reports afterwards is exactly what
    it reported before -- the one dispatch the legacy binary itself left
    consumed and unsettled.
    """
    store = StateStore(legacy_db)
    before = {(f.invariant, f.identifier) for f in run_rollback_preflight(store).failures}

    _fully_migrated(store)

    after = {(f.invariant, f.identifier) for f in run_rollback_preflight(store).failures}
    assert before == after == {("R2_dispatch_unsettled", "sig-nomanifest")}


def test_the_stranded_dispatch_is_the_same_item_in_both_views(legacy_db):
    """Quarantine and preflight must agree on what needs a person.

    Two views naming different things would leave the operator believing one of
    them is complete when it is not.
    """
    store = StateStore(legacy_db)
    _fully_migrated(store)

    quarantined = {q.identifier for q in ub.list_quarantines(store) if q.subsystem == "dispatch"}
    unsettled = {
        f.identifier
        for f in run_rollback_preflight(store).failures
        if f.invariant == "R2_dispatch_unsettled"
    }
    assert quarantined == unsettled == {"sig-nomanifest"}


def test_rollback_becomes_safe_once_the_operator_settles_the_stranded_dispatch(legacy_db):
    """Neither generation can recover a manifest-less dispatch on its own, so
    preflight keeps refusing until a person records how it ended. That is the
    intended shape: it refuses, and it does not settle anything itself."""
    store = StateStore(legacy_db)
    _fully_migrated(store)

    store.save_system_event(
        "sig-nomanifest",
        "signal_approval_completed",
        {"signal_run_id": "sig-nomanifest", "settled_by": "operator"},
    )

    assert run_rollback_preflight(store).safe is True


# --- the fixture itself ---------------------------------------------------


def test_the_fixture_is_reproducible_from_the_recorded_commit(tmp_path):
    """Otherwise it drifts and stops representing the old binary at all."""
    if subprocess.run(  # noqa: S603
        ["git", "cat-file", "-e", LEGACY_SHA], cwd=ROOT, capture_output=True
    ).returncode:
        pytest.skip("legacy baseline commit is not present in this clone")

    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp) / "legacy"
        subprocess.run(  # noqa: S603
            ["git", "worktree", "add", "--detach", str(worktree), LEGACY_SHA],
            cwd=ROOT,
            capture_output=True,
            check=True,
        )
        try:
            out = Path(tmp) / "out"
            subprocess.run(  # noqa: S603
                [
                    sys.executable,
                    str(ROOT / "scripts" / "generate_legacy_3a_fixture.py"),
                    "--out",
                    str(out),
                ],
                cwd=worktree,
                env={"PYTHONPATH": str(worktree / "src"), "PATH": "/usr/bin:/bin"},
                capture_output=True,
                check=True,
            )
            for name in ("legacy_3a_state.sql", "legacy_3a_state.json"):
                assert (out / name).read_text() == (FIXTURES / name).read_text(), name
        finally:
            subprocess.run(  # noqa: S603
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=ROOT,
                capture_output=True,
            )

"""The 3a migration's own ownership record.

Not a boolean. A migration that can crash between its first and its last write
needs four distinguishable answers -- nobody owns this database yet, someone
does and is not finished, someone finished, and the markers contradict each
other -- because three of them demand different behaviour from the runtime and
the fourth has to stop it. A flag collapses the middle two, and the middle two
are exactly where funding callbacks and recovery sweeps must not run: half the
legacy history classified, half not.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.state.store import StateStore

MIGRATION_ID = "3a"
#: Kept as the type 3a-4's ``load_migration_cutoff`` already reads, so a
#: database written by either release is understood by both.
STARTED_EVENT = "funding_workflow_migration_started"
COMPLETED_EVENT = "funding_workflow_migration_completed"
STARTED_KEY = f"migration-started:{MIGRATION_ID}"
COMPLETED_KEY = f"migration-completed:{MIGRATION_ID}"


class MigrationPhase(StrEnum):
    NOT_STARTED = "not_started"
    MIGRATING = "migrating"
    COMPLETED = "completed"
    INVALID = "invalid"


@dataclass(frozen=True)
class MigrationState:
    phase: MigrationPhase
    cutoff: int | None = None
    reason: str | None = None


class MigrationStateInvalid(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(f"migration state is invalid: {reason}")
        self.reason = reason


def _cutoffs(store: StateStore, event_type: str) -> list[int] | None:
    """The distinct cutoffs recorded by ``event_type``, or None if any is malformed.

    A malformed cutoff must not degrade to 0. Zero is a *meaningful* boundary --
    it says every row in the database was written after the migration -- so
    coercing garbage into it would expose the entire pre-3a history to the
    convergence sweep, which supersedes what it decides is orphaned. Returning
    None instead routes the caller to INVALID, which stops everything.
    """
    values: set[int] = set()
    for row in store.list_system_events_by_type(event_type, limit=None):
        raw = (row.get("payload") or {}).get("cutoff")
        # bool is an int subclass, and True would otherwise become cutoff 1.
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            return None
        values.add(raw)
    return sorted(values)


def load_migration_state(store: StateStore) -> MigrationState:
    """Which of the four states this database is in, decided in one place."""
    started = _cutoffs(store, STARTED_EVENT)
    if started is None:
        return MigrationState(MigrationPhase.INVALID, reason="malformed_started_marker")
    completed = _cutoffs(store, COMPLETED_EVENT)
    if completed is None:
        return MigrationState(MigrationPhase.INVALID, reason="malformed_completed_marker")
    if len(started) > 1:
        return MigrationState(MigrationPhase.INVALID, reason="conflicting_start_cutoffs")
    if len(completed) > 1:
        return MigrationState(MigrationPhase.INVALID, reason="conflicting_completed_cutoffs")
    if not started:
        if completed:
            # Impossible in sequence: completion requires the start marker
            # atomically. Reaching it means something wrote outside this module.
            return MigrationState(MigrationPhase.INVALID, reason="completed_without_started")
        return MigrationState(MigrationPhase.NOT_STARTED)
    cutoff = started[0]
    if not completed:
        return MigrationState(MigrationPhase.MIGRATING, cutoff=cutoff)
    if completed[0] != cutoff:
        return MigrationState(MigrationPhase.INVALID, reason="cutoff_mismatch")
    return MigrationState(MigrationPhase.COMPLETED, cutoff=cutoff)


def _max_event_id(store: StateStore) -> int:
    with sqlite3.connect(store.path) as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM system_events").fetchone()
    return int(row[0])


def start_migration(store: StateStore, run_id: str) -> MigrationState:
    """Take -- or re-adopt -- ownership of this database's 3a migration.

    The caller must already hold ``store.writer_lock`` for the whole operation,
    not just around this call. Observing ``MAX(id)`` and writing the marker are
    a single decision only if no cooperating writer can append between them;
    otherwise the recorded cutoff names a boundary that had already moved, and
    every request written into that gap is classified as legacy when it is not.

    A crash after this point must never pick a new cutoff. The heads and
    quarantines already written were classified against the old one, and a
    second boundary would silently reclassify them into the other generation.
    """
    if not store.holds_writer_lock():
        raise RuntimeError("start_migration requires the StateStore writer lock")
    state = load_migration_state(store)
    if state.phase is MigrationPhase.INVALID:
        raise MigrationStateInvalid(str(state.reason))
    if state.phase in (MigrationPhase.MIGRATING, MigrationPhase.COMPLETED):
        return state
    cutoff = _max_event_id(store)
    store.save_system_events_atomic(
        run_id,
        [
            {
                "event_type": STARTED_EVENT,
                "payload": {
                    "duplicate_key": STARTED_KEY,
                    "migration_id": MIGRATION_ID,
                    "cutoff": cutoff,
                },
            }
        ],
        forbid_duplicate_keys=(STARTED_KEY,),
    )
    return MigrationState(MigrationPhase.MIGRATING, cutoff=cutoff)


def complete_migration(store: StateStore, run_id: str, *, cutoff: int) -> None:
    """The migration's final write. Nothing may be written after it.

    Requiring the start marker inside the same transaction is what stops a
    completion from existing over a migration nobody ever owned; forbidding its
    own key makes a re-run a no-op instead of a conflicting overlap.
    """
    if not store.holds_writer_lock():
        raise RuntimeError("complete_migration requires the StateStore writer lock")
    state = load_migration_state(store)
    if state.phase is MigrationPhase.INVALID:
        raise MigrationStateInvalid(str(state.reason))
    if state.cutoff != cutoff:
        raise MigrationStateInvalid("cutoff_mismatch")
    store.save_system_events_atomic(
        run_id,
        [
            {
                "event_type": COMPLETED_EVENT,
                "payload": {
                    "duplicate_key": COMPLETED_KEY,
                    "migration_id": MIGRATION_ID,
                    "cutoff": cutoff,
                },
            }
        ],
        require_duplicate_keys=(STARTED_KEY,),
        forbid_duplicate_keys=(COMPLETED_KEY,),
    )


__all__ = [
    "COMPLETED_EVENT",
    "COMPLETED_KEY",
    "MIGRATION_ID",
    "STARTED_EVENT",
    "STARTED_KEY",
    "MigrationPhase",
    "MigrationState",
    "MigrationStateInvalid",
    "complete_migration",
    "load_migration_state",
    "start_migration",
]

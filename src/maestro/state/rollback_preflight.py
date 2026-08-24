"""Whether the pre-3a-4 binary can safely be put back in front of this database.

An inspector, not a repair tool. It writes nothing, and in particular it does
not manufacture a missing compatibility projection to make itself pass:
``complete_workflow`` writes the workflow completion and its legacy twin in one
transaction, so a missing twin means corruption, a manual mutation or an
intermediate build. Writing one here would erase the evidence of which, and
would hand the rollback a history that was never true.

Each invariant answers one question the old binary would get wrong:

R0  the migration must not be half-finished or contradictory
R1  a claim with no completion -- the old handler cannot see claims at all, so
    it would call the request pending and re-run the transition
R2  a consumed package with no settled event -- the old code treats consumed as
    permanent and the approval card is simply lost
R3  a versioned ack with no resolution -- the old handler treats an ack as
    terminal, so approved orders are never placed
R4  a completion with no legacy projection -- the old handler cannot see
    ``funding_workflow_completed``, so it would call the request pending and
    re-run it
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from maestro.state.funding_workflow import LEGACY_TERMINAL_EVENT
from maestro.state.migration_state import MigrationPhase, load_migration_state

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.state.store import StateStore

_LEGACY_TERMINAL_KEY_PREFIX = {"funding": "funding-ack", "budget": "budget-decision"}


@dataclass(frozen=True)
class InvariantFailure:
    invariant: str
    identifier: str
    detail: str
    event_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class RollbackPreflightResult:
    failures: tuple[InvariantFailure, ...]

    @property
    def safe(self) -> bool:
        return not self.failures


def _r0_migration_state(store: StateStore) -> list[InvariantFailure]:
    state = load_migration_state(store)
    if state.phase is MigrationPhase.MIGRATING:
        return [
            InvariantFailure(
                invariant="R0_migration_state",
                identifier=str(state.cutoff),
                detail=(
                    "a migration is in progress: part of the legacy history has been "
                    "classified and part has not, and the old binary understands neither"
                ),
            )
        ]
    if state.phase is MigrationPhase.INVALID:
        return [
            InvariantFailure(
                invariant="R0_migration_state",
                identifier="markers",
                detail=f"migration markers are contradictory: {state.reason}",
            )
        ]
    # NOT_STARTED is not itself a failure. Whether current-generation state the
    # old binary cannot read exists is exactly what R1-R4 detect, and they run
    # regardless of migration state.
    return []


def _r1_workflow_claims(store: StateStore) -> list[InvariantFailure]:
    """Every claim, read straight from the events.

    Deliberately *not* ``list_incomplete_workflows``: that filters out claims
    whose head has moved on, because an operator cannot act on them. The old
    binary can still trip over one -- it never checks heads -- so rollback
    safety needs the exhaustive set, not the actionable one.
    """
    completed = {
        (
            str((row.get("payload") or {}).get("request_id") or ""),
            str((row.get("payload") or {}).get("phase") or ""),
        )
        for row in store.list_system_events_by_type("funding_workflow_completed", limit=None)
    }
    failures: dict[tuple[str, str], InvariantFailure] = {}
    for row in store.list_system_events_by_type("funding_workflow_claim", limit=None):
        payload = row.get("payload") or {}
        key = (
            str(payload.get("request_id") or ""),
            str(payload.get("phase") or ""),
        )
        if key in completed or key in failures:
            continue
        failures[key] = InvariantFailure(
            invariant="R1_workflow_claim_unresolved",
            identifier=f"{key[0]}:{key[1]}",
            detail=(
                "a funding workflow transition was claimed but never completed; the old "
                "handler does not read claims and would treat the request as pending, "
                "re-running run_signal() and re-recording the cash flow"
            ),
            event_ids=(int(row.get("id") or 0),),
        )
    return [failures[key] for key in sorted(failures)]


def _r2_consumed_dispatches(store: StateStore) -> list[InvariantFailure]:
    """Reuses the store's own settled definition rather than re-deriving it.

    ``limit=None`` because a window would silently pass a rollback over the
    51st unfinished run.
    """
    return [
        InvariantFailure(
            invariant="R2_dispatch_unsettled",
            identifier=signal_run_id,
            detail=(
                "the signal package was consumed but the dispatch never settled; the old "
                "code treats consumed as permanent, so the approval card is lost"
            ),
        )
        for signal_run_id in store.list_incomplete_signal_dispatches(limit=None)
    ]


def _r3_versioned_approvals(store: StateStore) -> list[InvariantFailure]:
    """Only versioned acks. A schema-less one is read as terminal by both
    binaries alike, so it is not an incompatibility."""
    resolved = {
        str((row.get("payload") or {}).get("approval_id") or "")
        for row in store.list_system_events_by_type(
            "telegram_approval_resolution_completed", limit=None
        )
    }
    failures: dict[str, InvariantFailure] = {}
    for row in store.list_system_events_by_type("telegram_approval_ack", limit=None):
        payload = row.get("payload") or {}
        if not isinstance(payload.get("schema_version"), int):
            continue
        approval_id = str(payload.get("approval_id") or "")
        if approval_id in resolved or approval_id in failures:
            continue
        failures[approval_id] = InvariantFailure(
            invariant="R3_approval_unresolved",
            identifier=approval_id,
            detail=(
                "the operator's decision was recorded but execution never reported "
                "finishing; the old handler treats the ack alone as terminal, so the "
                "approved orders are never placed"
            ),
            event_ids=(int(row.get("id") or 0),),
        )
    return [failures[key] for key in sorted(failures)]


def _r4_legacy_projection(store: StateStore) -> list[InvariantFailure]:
    """The compatibility projection must exist -- and is never written here.

    complete_workflow commits both legs together, so a gap is abnormal by
    construction. Repairing it would destroy the only evidence of what went
    wrong and would assert a terminal status this code cannot know.
    """
    failures = []
    for row in store.list_system_events_by_type("funding_workflow_completed", limit=None):
        payload = row.get("payload") or {}
        request_id = str(payload.get("request_id") or "")
        phase = str(payload.get("phase") or "")
        prefix = _LEGACY_TERMINAL_KEY_PREFIX.get(phase)
        if prefix is None:
            failures.append(
                InvariantFailure(
                    invariant="R4_missing_legacy_projection",
                    identifier=f"{request_id}:{phase}",
                    detail=f"completion records an unknown phase {phase!r}",
                    event_ids=(int(row.get("id") or 0),),
                )
            )
            continue
        if store.duplicate_key_exists(f"{prefix}:{request_id}"):
            continue
        failures.append(
            InvariantFailure(
                invariant="R4_missing_legacy_projection",
                identifier=f"{request_id}:{phase}",
                detail=(
                    f"funding_workflow_completed has no {LEGACY_TERMINAL_EVENT[phase]}; "
                    "complete_workflow writes both in one transaction, so this is "
                    "corruption, a manual mutation or an intermediate build. It is not "
                    "repaired here -- the old handler would read the request as pending "
                    "and re-run it, and inventing the event would hide which of those "
                    "three happened"
                ),
                event_ids=(int(row.get("id") or 0),),
            )
        )
    return failures


def run_rollback_preflight(store: StateStore) -> RollbackPreflightResult:
    """Every invariant, always -- never short-circuiting on the first failure.

    An operator quiesces the system once and wants the whole list, not one
    problem at a time across five stop/start cycles.

    The whole run sits inside one held ``store.writer_lock``. R0-R4 must
    describe a single writer-fenced interval: systemd quiesce stops deployed
    services, but nothing about it constrains a cooperating ``maestro`` CLI or
    recovery script an operator runs by hand, and a preflight that reads R1
    safe, lets another writer append incompatible state, then reads R2-R4 and
    reports SAFE would clear a rollback over state it never actually examined.
    Owning the lock here (rather than trusting every caller to hold it) is
    re-entrant within a thread, so the upgrade path -- which already holds the
    lock for its own whole operation -- can invoke this without deadlock.
    """
    with store.writer_lock("rollback_preflight", timeout_seconds=60.0):
        failures: list[InvariantFailure] = []
        for check in (
            _r0_migration_state,
            _r1_workflow_claims,
            _r2_consumed_dispatches,
            _r3_versioned_approvals,
            _r4_legacy_projection,
        ):
            failures.extend(check(store))
        return RollbackPreflightResult(failures=tuple(failures))


__all__ = [
    "InvariantFailure",
    "RollbackPreflightResult",
    "run_rollback_preflight",
]

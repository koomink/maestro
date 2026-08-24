"""The 3a upgrade's deterministic classification of pre-cutoff history.

Three rules govern everything in this module.

*Only positive durable evidence counts.* Absence is never evidence: no legacy
terminal event does not prove a request was never decided, no completion record
does not prove an approval never reached the broker, and no manifest does not
prove a dispatch placed nothing. Where the evidence cannot decide, a quarantine
record is written and a person decides.

*Nothing here executes anything.* It places no orders, approves nothing, runs no
signal, records no cash flow and manufactures no history. The only writes are
migration ownership markers, deterministic v1 heads over provably-unique
history, and quarantine records.

*Every write is deterministic.* ``duplicate_key`` comes from stable identifiers
and payloads carry no clock, so a crashed run that restarts recognizes its own
earlier work as a replay instead of dying on a content mismatch
(``StateStore.save_system_events_atomic`` verifies replays by comparing
payloads, not just keys).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from maestro.state.funding_workflow import (
    LEGACY_TERMINAL_EVENT,
    head_key,
    workflow_id_from_request,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.state.store import StateStore

logger = logging.getLogger(__name__)

QUARANTINE_EVENT = "funding_workflow_migration_quarantine"

_REQUEST_EVENT = {
    "funding": "contribution_funding_request",
    "budget": "contribution_budget_request",
}


@dataclass(frozen=True)
class Quarantine:
    """One thing automatic migration refused to decide.

    ``blocking`` separates "this must be resolved before the migration may
    complete, and before the system may be restarted" from "this is isolated
    and the system is safe to run". Funding ownership is blocking because the
    wrong head sends a month's investment to the wrong request. A legacy
    approval is not, because the current runtime already refuses to
    auto-execute one -- the record exists to give it an owner, not a gate.
    """

    subsystem: str
    identifier: str
    reason: str
    blocking: bool
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class BackfillReport:
    legacy_requests_inspected: int = 0
    heads_created: int = 0
    heads_already_coherent: int = 0
    terminal_skipped: int = 0
    superseded_by_newer: int = 0
    quarantines: list[Quarantine] = field(default_factory=list)

    @property
    def blocking(self) -> list[Quarantine]:
        return [item for item in self.quarantines if item.blocking]


def quarantine_key(subsystem: str, identifier: str) -> str:
    return f"migration-quarantine:{subsystem}:{identifier}"


def write_quarantine(store: StateStore, run_id: str, quarantine: Quarantine) -> bool:
    """Record an operator-owned ambiguity. Returns whether this call wrote it.

    Idempotent by construction: the key names the subject, and the payload is a
    pure function of the evidence, so a rerun over the same history reproduces
    byte-identical content and is recognized as a replay.
    """
    outcome = store.save_system_events_atomic(
        run_id,
        [
            {
                "event_type": QUARANTINE_EVENT,
                "payload": {
                    "duplicate_key": quarantine_key(
                        quarantine.subsystem, quarantine.identifier
                    ),
                    "subsystem": quarantine.subsystem,
                    "identifier": quarantine.identifier,
                    "reason": quarantine.reason,
                    "blocking": quarantine.blocking,
                    "detail": quarantine.detail,
                },
            }
        ],
        forbid_duplicate_keys=(quarantine_key(quarantine.subsystem, quarantine.identifier),),
    )
    return bool(outcome["committed"])


def list_quarantines(store: StateStore) -> list[Quarantine]:
    quarantines = [
        Quarantine(
            subsystem=str((row.get("payload") or {}).get("subsystem") or ""),
            identifier=str((row.get("payload") or {}).get("identifier") or ""),
            reason=str((row.get("payload") or {}).get("reason") or ""),
            blocking=bool((row.get("payload") or {}).get("blocking")),
            detail=dict((row.get("payload") or {}).get("detail") or {}),
        )
        for row in store.list_system_events_by_type(QUARANTINE_EVENT, limit=None)
    ]
    return sorted(quarantines, key=lambda item: (item.subsystem, item.identifier))


def _legacy_terminal_request_ids(store: StateStore) -> set[str]:
    """Requests the pre-3a-4 binary recorded a terminal decision for.

    Read here, and only here, as *historical* evidence -- the one job the
    legacy terminal events keep. What they may no longer do is answer the same
    question at runtime; see ``funding_workflow.request_terminal_state``.
    """
    return {
        str((row.get("payload") or {}).get("request_id") or "")
        for event_type in LEGACY_TERMINAL_EVENT.values()
        for row in store.list_system_events_by_type(event_type, limit=None)
    }


def _workflow_terminal_request_ids(store: StateStore) -> set[str]:
    return {
        str((row.get("payload") or {}).get("request_id") or "")
        for event_type in ("funding_workflow_completed", "funding_workflow_superseded")
        for row in store.list_system_events_by_type(event_type, limit=None)
    }


def _request_event_ids(store: StateStore) -> dict[str, int]:
    """``request_id -> system_events.id`` for every recorded contribution request."""
    ids: dict[str, int] = {}
    for event_type in _REQUEST_EVENT.values():
        for row in store.list_system_events_by_type(event_type, limit=None):
            request_id = str((row.get("payload") or {}).get("request_id") or "")
            if request_id:
                ids[request_id] = int(row.get("id") or 0)
    return ids


def backfill_funding_heads(
    store: StateStore, run_id: str, *, cutoff: int
) -> BackfillReport:
    """Give each unambiguous legacy workflow the v1 head 3a-4 assumes exists.

    Without a head, ``claim_workflow_attempt`` refuses every transition on a
    pre-3a-4 request and the operator's month cannot be confirmed at all. With
    the *wrong* head, it confirms the wrong request. So a head is written only
    where exactly one pending request survives every terminal test, and the
    workflow either has no head or already names that same request.

    Everything else is quarantined and blocks completion. Leaving the system
    stopped is strictly better than assigning a live head by guess.
    """
    if not store.holds_writer_lock():
        raise RuntimeError("backfill_funding_heads requires the StateStore writer lock")

    report = BackfillReport()
    terminal = _legacy_terminal_request_ids(store) | _workflow_terminal_request_ids(store)
    request_event_ids = _request_event_ids(store)
    heads = {row["workflow_id"]: row for row in store.list_funding_workflow_heads()}

    # workflow_id -> phase -> [request payloads still open]
    candidates: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for phase, event_type in _REQUEST_EVENT.items():
        for row in store.list_system_events_by_type(event_type, limit=None):
            if int(row.get("id") or 0) > cutoff:
                continue
            report.legacy_requests_inspected += 1
            payload = dict(row.get("payload") or {})
            request_id = str(payload.get("request_id") or "")
            if payload.get("status") != "pending" or request_id in terminal:
                report.terminal_skipped += 1
                continue
            try:
                workflow_id = str(
                    payload.get("funding_workflow_id") or workflow_id_from_request(payload)
                )
            except (ValueError, KeyError):
                # No month_key, so no workflow identity, so no way to know
                # which month's budget this request belongs to. Deriving one
                # would be inventing the very fact that decides where money
                # goes.
                report.quarantines.append(
                    Quarantine(
                        subsystem="funding",
                        identifier=request_id or f"event:{row.get('id')}",
                        reason="malformed_workflow_identity",
                        blocking=True,
                        detail={"phase": phase, "event_id": int(row.get("id") or 0)},
                    )
                )
                continue
            candidates.setdefault((workflow_id, phase), []).append(payload)

    for (workflow_id, phase), pending in sorted(candidates.items()):
        pending.sort(key=lambda item: str(item.get("request_id")))
        request_ids = [str(item["request_id"]) for item in pending]
        if len(pending) > 1:
            # Two live requests for one month. Nothing in the record says which
            # the operator meant, and picking either would hand it the month's
            # investment.
            report.quarantines.append(
                Quarantine(
                    subsystem="funding",
                    identifier=workflow_id,
                    reason="ambiguous_pending_requests",
                    blocking=True,
                    detail={"phase": phase, "request_ids": request_ids},
                )
            )
            continue
        payload = pending[0]
        request_id = request_ids[0]
        head = heads.get(workflow_id)
        if head is not None:
            head_request_id = str(head.get("request_id") or "")
            if head_request_id == request_id:
                report.heads_already_coherent += 1
                continue
            if request_event_ids.get(head_request_id, 0) > cutoff:
                # A post-cutoff request already owns this workflow: 3a
                # published it, atomically, with its own head. That lineage is
                # current and coherent, and the pre-cutoff request is inert --
                # no head points at it, so no transition can be claimed on it.
                report.superseded_by_newer += 1
                continue
            report.quarantines.append(
                Quarantine(
                    subsystem="funding",
                    identifier=workflow_id,
                    reason="head_ownership_conflict",
                    blocking=True,
                    detail={
                        "phase": phase,
                        "pending_request_id": request_id,
                        "head_request_id": head_request_id,
                        "head_version": int(head.get("version") or 0),
                    },
                )
            )
            continue
        outcome = store.save_system_events_atomic(
            str(payload.get("run_id") or f"run_{request_id}"),
            [
                {
                    "event_type": "funding_workflow_head",
                    "payload": {
                        "duplicate_key": head_key(workflow_id, 1),
                        "workflow_id": workflow_id,
                        "version": 1,
                        "request_id": request_id,
                        "phase": phase,
                        "status": "pending",
                        "scope": [
                            payload.get("contribution_group_id"),
                            payload.get("account_id"),
                            payload.get("execution_sleeve"),
                            payload.get("currency"),
                        ],
                        "reason": "legacy_backfill_v1",
                    },
                }
            ],
            # The workflow had no head when this loop read the store, and
            # under the barrier nothing else can give it one -- but pinning it
            # here costs nothing and makes the decision this loop made agree
            # with what SQLite commits rather than with what was true when the
            # snapshot was taken.
            forbid_duplicate_keys=(head_key(workflow_id, 1),),
        )
        if outcome["committed"]:
            report.heads_created += 1
        else:
            report.heads_already_coherent += 1

    for quarantine in report.quarantines:
        write_quarantine(store, run_id, quarantine)
    return report


__all__ = [
    "QUARANTINE_EVENT",
    "BackfillReport",
    "Quarantine",
    "backfill_funding_heads",
    "list_quarantines",
    "quarantine_key",
    "write_quarantine",
]

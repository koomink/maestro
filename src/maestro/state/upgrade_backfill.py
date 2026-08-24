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
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from maestro.state import migration_state
from maestro.state.funding_workflow import (
    LEGACY_TERMINAL_EVENT,
    head_key,
    workflow_id_from_request,
)
from maestro.state.migration_state import (
    MigrationPhase,
    MigrationState,
    load_migration_state,
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


def _legacy_terminal_request_ids(store: StateStore, *, cutoff: int) -> tuple[set[str], set[str]]:
    """Requests the pre-3a-4 binary recorded a terminal decision for.

    Split by the cutoff on purpose. A pre-cutoff ack is *historical* evidence
    -- the one job the legacy terminal events keep; what they may no longer do
    is answer the same question at runtime (see
    ``funding_workflow.request_terminal_state``). An ack **above** the cutoff
    is not history: it is a legacy-generation row written after the migration
    took ownership, which under the barrier nothing may produce. Reading it as
    "this request finished" would silently absorb that breach -- and possibly
    strand a request whose transition already had broker side effects -- so
    those come back separately and become blocking quarantines instead.
    """
    pre_cutoff: set[str] = set()
    post_cutoff: set[str] = set()
    for event_type in LEGACY_TERMINAL_EVENT.values():
        for row in store.list_system_events_by_type(event_type, limit=None):
            request_id = str((row.get("payload") or {}).get("request_id") or "")
            if int(row.get("id") or 0) > cutoff:
                post_cutoff.add(request_id)
            else:
                pre_cutoff.add(request_id)
    return pre_cutoff, post_cutoff


def _workflow_terminal_request_ids(store: StateStore) -> set[str]:
    return {
        str((row.get("payload") or {}).get("request_id") or "")
        for event_type in ("funding_workflow_completed", "funding_workflow_superseded")
        for row in store.list_system_events_by_type(event_type, limit=None)
    }


def _intended_head_payload(
    workflow_id: str, phase: str, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """The exact v1 head this backfill writes for an unambiguous candidate.

    Deterministic by construction -- identifiers only, no clock -- so a
    crashed run's restart reproduces it byte for byte and the store recognizes
    its own prior work as a replay.
    """
    return {
        "duplicate_key": head_key(workflow_id, 1),
        "workflow_id": workflow_id,
        "version": 1,
        "request_id": str(payload.get("request_id")),
        "phase": phase,
        "status": "pending",
        "scope": [
            payload.get("contribution_group_id"),
            payload.get("account_id"),
            payload.get("execution_sleeve"),
            payload.get("currency"),
        ],
        "reason": "legacy_backfill_v1",
    }


def _commit_v1_head(
    store: StateStore, run_id: str, workflow_id: str, intended: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    """Write the v1 head, or classify a lost race against whatever committed.

    Returns ``("created", None)``, ``("coherent", head)`` or
    ``("conflict", head)``. A conflict is never judged from the exception
    alone: the authoritative current head is reloaded and compared to the full
    intended payload. Only byte-identical content counts as coherent --
    anything else (a different request, phase, scope or version) is someone
    else's ownership claim and comes back as a conflict for quarantine.
    """
    try:
        outcome = store.save_system_events_atomic(
            run_id,
            [{"event_type": "funding_workflow_head", "payload": intended}],
            # The workflow had no head when this loop read the store, and
            # under the barrier nothing else can give it one -- but pinning it
            # here costs nothing and makes the decision this loop made agree
            # with what SQLite commits rather than with what was true when the
            # snapshot was taken.
            forbid_duplicate_keys=(head_key(workflow_id, 1),),
        )
    except ValueError:
        # A same-key different-content (or provenance) collision is exactly
        # what losing the v1 slot looks like. Classify from what actually
        # committed -- but only once something *is* there to classify; a
        # ValueError over an absent head is some other fault entirely and
        # keeps its identity as an exception.
        if store.load_funding_workflow_head(workflow_id) is None:
            raise
        return _classify_committed_head(store, workflow_id, intended)
    if outcome["committed"]:
        return "created", None
    return _classify_committed_head(store, workflow_id, intended)


def _classify_committed_head(
    store: StateStore, workflow_id: str, intended: dict[str, Any]
) -> tuple[str, dict[str, Any] | None]:
    committed = store.load_funding_workflow_head(workflow_id)
    if committed == intended:
        return "coherent", committed
    return "conflict", committed


def backfill_funding_heads(
    store: StateStore, run_id: str, *, cutoff: int
) -> BackfillReport:
    """Give each unambiguous legacy workflow the v1 head 3a-4 assumes exists.

    Without a head, ``claim_workflow_attempt`` refuses every transition on a
    pre-3a-4 request and the operator's month cannot be confirmed at all. With
    the *wrong* head, it confirms the wrong request. So candidates are grouped
    by **workflow_id across every phase** -- one workflow has one head, not one
    per phase -- and a head is written only where exactly one pending request
    survives every terminal test and the workflow either has no head or
    already carries exactly the head this backfill intends.

    A legitimate succession is never guessed here. Current-generation lineage
    leaves a durable ``funding_workflow_superseded`` marker behind, and any
    request carrying one is terminal before candidacy begins; therefore no
    live candidate can be provably linked to some other request's head. A head
    pointing anywhere but the sole remaining candidate is a blocking conflict.

    Everything ambiguous is quarantined and blocks completion. Leaving the
    system stopped is strictly better than assigning a live head by guess.
    """
    if not store.holds_writer_lock():
        raise RuntimeError("backfill_funding_heads requires the StateStore writer lock")

    report = BackfillReport()
    legacy_terminal, post_cutoff_terminal = _legacy_terminal_request_ids(store, cutoff=cutoff)
    terminal = legacy_terminal | _workflow_terminal_request_ids(store)
    quarantined_post_cutoff: set[str] = set()
    for request_id in sorted(post_cutoff_terminal):
        if not request_id or request_id in quarantined_post_cutoff:
            continue
        quarantined_post_cutoff.add(request_id)
        report.quarantines.append(
            Quarantine(
                subsystem="funding",
                identifier=request_id,
                reason="post_cutoff_legacy_terminal",
                blocking=True,
                detail={},
            )
        )

    # workflow_id -> [(phase, request payload)] of live candidates, every phase
    # together, so ownership is decided over the whole workflow at once.
    candidates: dict[str, list[tuple[str, dict[str, Any]]]] = {}
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
            if request_id in quarantined_post_cutoff:
                # Its post-cutoff legacy ack already blocked the migration;
                # giving the request a live head on top of that could repeat a
                # transition that may have executed.
                continue
            try:
                workflow_id_str = str(
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
            candidates.setdefault(workflow_id_str, []).append((phase, payload))

    for workflow_id_str, entries in sorted(candidates.items()):
        entries.sort(key=lambda item: (item[0], str(item[1].get("request_id"))))
        if len(entries) > 1:
            # Two live requests for one workflow -- whether they compete in the
            # same phase or across phases, nothing in the record says which the
            # operator meant, and picking either would hand it the month's
            # investment.
            phases = sorted({phase for phase, _ in entries})
            request_ids = sorted(str(payload.get("request_id")) for _, payload in entries)
            report.quarantines.append(
                Quarantine(
                    subsystem="funding",
                    identifier=workflow_id_str,
                    reason="ambiguous_pending_requests",
                    blocking=True,
                    detail={"phases": phases, "request_ids": request_ids},
                )
            )
            continue
        phase, payload = entries[0]
        intended = _intended_head_payload(workflow_id_str, phase, payload)
        head = store.load_funding_workflow_head(workflow_id_str)
        if head is not None:
            if head == intended:
                report.heads_already_coherent += 1
                continue
            report.quarantines.append(
                Quarantine(
                    subsystem="funding",
                    identifier=workflow_id_str,
                    reason="head_ownership_conflict",
                    blocking=True,
                    detail={
                        "phase": phase,
                        "pending_request_id": str(intended["request_id"]),
                        "head_request_id": str(head.get("request_id") or ""),
                        "head_version": int(head.get("version") or 0),
                    },
                )
            )
            continue
        verdict, committed = _commit_v1_head(store, run_id, workflow_id_str, intended)
        if verdict == "created":
            report.heads_created += 1
        elif verdict == "coherent":
            report.heads_already_coherent += 1
        else:
            report.quarantines.append(
                Quarantine(
                    subsystem="funding",
                    identifier=workflow_id_str,
                    reason="head_ownership_conflict",
                    blocking=True,
                    detail={
                        "phase": phase,
                        "pending_request_id": str(intended["request_id"]),
                        "head_request_id": str((committed or {}).get("request_id") or ""),
                        "head_version": int((committed or {}).get("version") or 0),
                    },
                )
            )

    for quarantine in report.quarantines:
        write_quarantine(store, run_id, quarantine)
    return report


@dataclass
class ApprovalClassificationReport:
    acks_inspected: int = 0
    proven_complete: int = 0
    quarantines: list[Quarantine] = field(default_factory=list)


@dataclass
class DispatchClassificationReport:
    dispatches_inspected: int = 0
    resumable: int = 0
    quarantines: list[Quarantine] = field(default_factory=list)


def completed_legacy_approval_ids(store: StateStore) -> set[str]:
    """legacy 완료 판정. **signal_run_id만으로 판정하면 안 된다** -- 하나의
    signal run이 여러 승인 그룹으로 나뉘고(orchestrator의
    ``_approval_order_groups``) 그룹마다 별도 approval_id가 발급되므로, 한
    그룹의 완료가 다른 그룹의 유실을 가린다.

    신규 ``signal_approval_completed``에는 approval_id가 있어 정확히
    매칭된다. approval_id가 없는 구 이벤트는 그 signal run의 승인 그룹이
    하나뿐일 때만 완료로 인정하고, 둘 이상이면 **모호하므로 완료로 치지
    않는다**.

    Lives here rather than on the bot so the migration's classification and
    the runtime's legacy notice judge by one rule. Two copies of a
    conservative rule drift, and the drift stays invisible until one of them
    calls a half-finished approval done.
    """
    groups: dict[str, list[str]] = {}
    for row in store.list_system_events_by_type("telegram_approval_pending", limit=None):
        payload = row.get("payload") or {}
        groups.setdefault(str(payload.get("signal_run_id")), []).append(
            str(payload.get("approval_id"))
        )

    completed: set[str] = set()
    for row in store.list_system_events_by_type("signal_approval_completed", limit=None):
        payload = row.get("payload") or {}
        approval_id = payload.get("approval_id")
        if isinstance(approval_id, str) and approval_id:
            completed.add(approval_id)
            continue
        group = groups.get(str(payload.get("signal_run_id")), [])
        if len(group) == 1:
            completed.add(group[0])
    return completed


def classify_legacy_approvals(
    store: StateStore, run_id: str, *, cutoff: int
) -> ApprovalClassificationReport:
    """Sort pre-two-phase approval acks into proven-complete or quarantined.

    Nothing is synthesized. The old 3a-5 design would have read "a legacy ack,
    no approvals row, no completion evidence" as proof of cancellation and
    written a resolution event saying so. That is a broker's behaviour inferred
    from a gap in local persistence -- the order may have reached the broker
    before the process that was recording it died -- and a synthetic
    cancellation is exactly the record that would later authorize a re-run.

    None of these quarantines block the migration. The current runtime already
    refuses to auto-execute a schema-less ack (``_resume_unresolved_approvals``
    skips one, ``ops.batch_execution`` refuses to settle one), so the record
    exists to give the ambiguity a named owner rather than to add a gate.
    """
    if not store.holds_writer_lock():
        raise RuntimeError("classify_legacy_approvals requires the StateStore writer lock")

    report = ApprovalClassificationReport()
    completed = completed_legacy_approval_ids(store)
    for row in store.list_system_events_by_type("telegram_approval_ack", limit=None):
        if int(row.get("id") or 0) > cutoff:
            continue
        payload = row.get("payload") or {}
        if isinstance(payload.get("schema_version"), int):
            # Current generation: the resume path owns it, and rollback
            # preflight (R3) is what checks it.
            continue
        approval_id = str(payload.get("approval_id") or "")
        report.acks_inspected += 1
        if approval_id in completed:
            report.proven_complete += 1
            continue
        if store.approval_exists(approval_id):
            # The approval itself was persisted, so execution may already have
            # been entered. Strictly worse than "unknown", and labelled so the
            # operator checks the broker first.
            reason = "execution_may_have_been_entered"
        else:
            reason = "completion_unprovable"
        report.quarantines.append(
            Quarantine(
                subsystem="approval",
                identifier=approval_id,
                reason=reason,
                blocking=False,
                detail={"event_id": int(row.get("id") or 0)},
            )
        )
    for quarantine in report.quarantines:
        write_quarantine(store, run_id, quarantine)
    return report


def dispatch_manifest_key(signal_run_id: str) -> str:
    return f"dispatch-manifest:{signal_run_id}"


def has_dispatch_manifest(store: StateStore, signal_run_id: str) -> bool:
    """Whether this dispatch's intent was ever durably recorded."""
    return store.duplicate_key_exists(dispatch_manifest_key(signal_run_id))


def classify_legacy_dispatches(
    store: StateStore, run_id: str, *, cutoff: int
) -> DispatchClassificationReport:
    """Separate resumable dispatches from pre-manifest ones.

    ``consumed + unsettled + a manifest`` is an ordinary crash: the manifest
    says which groups the dispatch was obligated to resolve, so re-entering it
    finishes exactly that work.

    ``consumed + unsettled + no manifest`` is pre-manifest history, and its
    intent was never written down. Rebuilding it would mean re-deriving the
    groups from today's strategy output, capacity, buying power, portfolio and
    account state, which can differ from what the run actually decided -- so a
    replay could place orders the run never intended. The missing manifest also
    does not prove nothing went out. Neither replay nor "assume finished" is
    safe, so it is isolated from automatic resume and handed to a person.

    Read exhaustively (``limit=None``): a window would silently drop the 51st
    unfinished run, and a lost dispatch is the failure this exists to catch.
    """
    if not store.holds_writer_lock():
        raise RuntimeError("classify_legacy_dispatches requires the StateStore writer lock")

    del cutoff  # Settlement, not the migration boundary, decides membership here.
    report = DispatchClassificationReport()
    for signal_run_id in store.list_incomplete_signal_dispatches(limit=None):
        report.dispatches_inspected += 1
        if has_dispatch_manifest(store, signal_run_id):
            report.resumable += 1
            continue
        report.quarantines.append(
            Quarantine(
                subsystem="dispatch",
                identifier=signal_run_id,
                reason="legacy_dispatch_no_manifest",
                blocking=False,
                detail={},
            )
        )
    for quarantine in report.quarantines:
        write_quarantine(store, run_id, quarantine)
    return report



@dataclass
class UpgradeResult:
    state: MigrationState
    backfill: BackfillReport | None = None
    approvals: ApprovalClassificationReport | None = None
    dispatches: DispatchClassificationReport | None = None
    completed: bool = False
    aborted_reason: str | None = None
    reupgrade_evidence: list[dict[str, Any]] = field(default_factory=list)

    @property
    def quarantines(self) -> list[Quarantine]:
        return [
            item
            for report in (self.backfill, self.approvals, self.dispatches)
            if report is not None
            for item in report.quarantines
        ]


def detect_reupgrade_after_rollback(
    store: StateStore, *, cutoff: int
) -> list[dict[str, Any]]:
    """Evidence that an *old* binary wrote to this database after the migration.

    The situation: migration_completed(cutoff=N), then a rollback, then the old
    code writes events N+1.., then this binary is deployed again. A rerun that
    saw only the completed marker would call the database migrated and leave
    that old-generation state unclassified forever -- pending requests with no
    head, terminal events with no completion.

    Both detectors are positive durable evidence of a write this generation
    cannot have made, never an inference from something being absent:

    ``legacy_terminal_without_completion`` -- ``complete_workflow`` writes the
    workflow completion and its legacy projection in one transaction, so the
    split can only come from a writer that does not know about the workflow.

    ``request_without_head`` -- ``plan_contribution_request`` commits the
    request and its head in one batch, so a request no head has ever named is
    likewise only reachable from an old writer.

    ``legacy_ack_without_schema_version`` -- this generation's single ack write
    site always records an int ``schema_version``, so a bare ack above the
    cutoff can only have come from a pre-versioning binary. A *versioned* ack,
    by contrast, is ordinary current activity and never evidence here.

    One legacy shape is deliberately NOT a detector: a consumed-but-unsettled
    dispatch with no manifest. The synchronous approve path (paper / non-async
    modes) legitimately runs without manifests at all, so its own crash could
    produce exactly that shape -- flagging it would accuse this generation of
    being an old binary. The condition is already contained fail-closed
    elsewhere: rollback preflight R2 refuses the rollback, the resume fence
    refuses automatic replay, and the migration quarantined it on first pass.

    Nothing is merged, no second cutoff is chosen and no epoch 2 is created. A
    database in this state needs a dedicated procedure and a person to design
    it; guessing here is how two generations get silently interleaved.
    """
    evidence: list[dict[str, Any]] = []
    completed = {
        (
            str((row.get("payload") or {}).get("request_id") or ""),
            str((row.get("payload") or {}).get("phase") or ""),
        )
        for row in store.list_system_events_by_type("funding_workflow_completed", limit=None)
    }
    for phase, event_type in LEGACY_TERMINAL_EVENT.items():
        for row in store.list_system_events_by_type(event_type, limit=None):
            if int(row.get("id") or 0) <= cutoff:
                continue
            request_id = str((row.get("payload") or {}).get("request_id") or "")
            if (request_id, phase) in completed:
                continue
            evidence.append(
                {
                    "detector": "legacy_terminal_without_completion",
                    "event_id": int(row.get("id") or 0),
                    "identifier": request_id,
                    "event_type": event_type,
                }
            )

    headed = {
        str((row.get("payload") or {}).get("request_id") or "")
        for row in store.list_system_events_by_type("funding_workflow_head", limit=None)
    }
    for event_type in _REQUEST_EVENT.values():
        for row in store.list_system_events_by_type(event_type, limit=None):
            if int(row.get("id") or 0) <= cutoff:
                continue
            request_id = str((row.get("payload") or {}).get("request_id") or "")
            if request_id in headed:
                continue
            evidence.append(
                {
                    "detector": "request_without_head",
                    "event_id": int(row.get("id") or 0),
                    "identifier": request_id,
                    "event_type": event_type,
                }
            )

    for row in store.list_system_events_by_type("telegram_approval_ack", limit=None):
        if int(row.get("id") or 0) <= cutoff:
            continue
        payload = row.get("payload") or {}
        if isinstance(payload.get("schema_version"), int):
            # Current generation: the resume path owns these, and rollback
            # preflight (R3) is what checks them.
            continue
        evidence.append(
            {
                "detector": "legacy_ack_without_schema_version",
                "event_id": int(row.get("id") or 0),
                "identifier": str(payload.get("approval_id") or ""),
                "event_type": "telegram_approval_ack",
            }
        )
    return sorted(evidence, key=lambda item: item["event_id"])


def run_upgrade_backfill(store: StateStore, run_id: str) -> UpgradeResult:
    """The whole 3a migration, under one continuously held writer lock.

    The lock is taken once, around everything, rather than around each insert.
    The cutoff means "no state existed past here when this migration began",
    and that is only true if no cooperating writer -- a `maestro` CLI an
    operator runs by hand, a recovery script -- can append between observing it
    and completing. Per-insert locking would leave a gap after every step.

    ``migration_completed`` is the last write, unconditionally. Anything
    written after it lands in a database that has already declared itself
    migrated, and would be invisible to the next run's classification. A
    blocking quarantine therefore leaves the migration MIGRATING rather than
    completing over it: the runtime gates stay closed and the operator must not
    restart services until the ownership question is answered.
    """
    with store.writer_lock("upgrade_backfill", timeout_seconds=60.0):
        state = load_migration_state(store)
        if state.phase is MigrationPhase.INVALID:
            return UpgradeResult(state=state, aborted_reason=f"invalid:{state.reason}")
        if state.phase is MigrationPhase.COMPLETED:
            evidence = detect_reupgrade_after_rollback(store, cutoff=int(state.cutoff or 0))
            if evidence:
                return UpgradeResult(
                    state=state,
                    aborted_reason="reupgrade_after_rollback",
                    reupgrade_evidence=evidence,
                )
            # Already migrated and nothing old-generation has been written
            # since. A verification pass, not a second migration -- it writes
            # nothing at all.
            return UpgradeResult(state=state, completed=True)

        state = migration_state.start_migration(store, run_id)
        cutoff = int(state.cutoff or 0)
        result = UpgradeResult(state=state)
        # Module-level lookups on purpose: each stage has to be independently
        # replaceable by the crash-injection tests, and each has to observe the
        # work its predecessor already committed.
        result.backfill = backfill_funding_heads(store, run_id, cutoff=cutoff)
        result.approvals = classify_legacy_approvals(store, run_id, cutoff=cutoff)
        result.dispatches = classify_legacy_dispatches(store, run_id, cutoff=cutoff)
        if result.backfill.blocking:
            result.aborted_reason = "blocking_quarantine"
            return result
        migration_state.complete_migration(store, run_id, cutoff=cutoff)
        result.state = load_migration_state(store)
        result.completed = True
        return result


__all__ = [
    "QUARANTINE_EVENT",
    "ApprovalClassificationReport",
    "BackfillReport",
    "DispatchClassificationReport",
    "Quarantine",
    "UpgradeResult",
    "backfill_funding_heads",
    "classify_legacy_approvals",
    "classify_legacy_dispatches",
    "completed_legacy_approval_ids",
    "detect_reupgrade_after_rollback",
    "dispatch_manifest_key",
    "has_dispatch_manifest",
    "list_quarantines",
    "quarantine_key",
    "run_upgrade_backfill",
    "write_quarantine",
]

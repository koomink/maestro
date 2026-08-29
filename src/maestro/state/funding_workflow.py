"""funding/budget 워크플로우의 영속 식별자와 duplicate_key 규약.

같은 계좌·같은 달이라도 contribution_group_id/execution_sleeve/currency가
다르면 독립 워크플로우다. 그래서 키는 scope 전체를 담은 복합 키이고,
직렬화는 타입(null vs 문자열)과 경계(escaping)를 모두 보존하는 canonical
JSON 배열이다 — 문자열 sentinel이나 단순 join은 서로 다른 scope를 같은
head로 합쳐 한쪽의 월간 투자를 조용히 supersede할 수 있다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.state.store import StateStore

logger = logging.getLogger(__name__)

PHASES: tuple[str, str] = ("funding", "budget")


def funding_workflow_id(
    *,
    contribution_group_id: str | None,
    account_id: str | None,
    execution_sleeve: str | None,
    currency: str | None,
    month_key: str,
) -> str:
    """워크플로우의 영속 식별자. 해시가 아니라 전체 scope 문자열이다.

    유니코드 정규화를 하지 않는다: 정규화하면 원시 값이 다른 NFC 등가
    문자열이 같은 head/CAS를 공유해 서로를 supersede할 수 있고, 이는
    "서로 다른 scope는 충돌하지 않는다"는 불변식과 모순된다.
    """
    if not month_key:
        raise ValueError("funding workflow id requires a month_key")
    prefix = scope_prefix([contribution_group_id, account_id, execution_sleeve, currency])
    return f"{prefix}{month_key}"


def scope_prefix(scope: Sequence[Any]) -> str:
    """The ``funding:<canonical scope>:`` prefix every id in this scope shares.

    Shared with the head validation in ``claim_workflow_attempt`` so the two
    can never disagree about what canonical serialization means.
    """
    serialized = json.dumps(list(scope), ensure_ascii=False, separators=(",", ":"))
    return f"funding:{serialized}:"


def workflow_id_from_request(request: Mapping[str, Any]) -> str:
    month_key = request.get("month_key")
    if not month_key:
        raise ValueError("contribution request is missing month_key")
    return funding_workflow_id(
        contribution_group_id=request.get("contribution_group_id"),
        account_id=request.get("account_id"),
        execution_sleeve=request.get("execution_sleeve"),
        currency=request.get("currency"),
        month_key=str(month_key),
    )


def head_key(workflow_id: str, version: int) -> str:
    """head는 버전마다 새 키다. 고정 키를 재사용하면 두 번째 전이부터
    자기 자신과의 partial overlap으로 보여 원자 커밋이 거부된다."""
    return f"head:{workflow_id}:v{version}"


def claim_key(workflow_id: str, phase: str, request_id: str, attempt: int) -> str:
    return f"{workflow_id}:{phase}:{request_id}:a{attempt}"


def child_key(request_id: str, phase: str) -> str:
    return f"child:{request_id}:{phase}"


def load_workflow_child(store: StateStore, request_id: str, phase: str) -> str | None:
    """The signal_run_id already produced for this source request, if any.

    Lookup is purely by the recorded ``child:<request_id>:<phase>`` key,
    never by matching strategy or scope: an inferential match would let two
    unrelated runs that happen to share scope be mistaken for the same
    lineage, silently dropping one of them.
    """
    key = child_key(request_id, phase)
    for row in store.list_system_events_by_type("funding_workflow_child_created", limit=None):
        payload = row.get("payload") or {}
        if payload.get("duplicate_key") == key:
            signal_run_id = payload.get("signal_run_id")
            return str(signal_run_id) if signal_run_id else None
    return None


def completed_key(workflow_id: str, request_id: str, phase: str) -> str:
    return f"wf-completed:{workflow_id}:{request_id}:{phase}"


def superseded_key(workflow_id: str, request_id: str) -> str:
    return f"wf-superseded:{workflow_id}:{request_id}"


TERMINAL_WORKFLOW_EVENTS: tuple[str, str] = (
    "funding_workflow_completed",
    "funding_workflow_superseded",
)


def request_terminal_state(store: StateStore, request_id: str, phase: str) -> str | None:
    """Whether this request's transition is over, per *workflow* state alone.

    Deliberately blind to ``contribution_funding_request_ack`` and
    ``contribution_budget_request_decision``. Those are the rollback
    compatibility projection ``complete_workflow`` writes for the pre-CAS
    binary, not a second opinion this generation is allowed to consult: the
    legacy event cannot express phase, attempt or supersession, so the two
    answers can differ, and a system with two definitions of "finished"
    eventually acts on the wrong one.

    A pre-3a-4 ack with no completion behind it therefore reports ``None``
    here, on purpose. That is not this function calling the request live -- it
    is this function declining to decide. The upgrade backfill classifies such
    history under a quiesce barrier with the whole database in front of it,
    which is the only place the distinction can be drawn safely.
    """
    _require_phase(phase)
    for row in store.list_system_events_by_type("funding_workflow_completed", limit=None):
        payload = row.get("payload") or {}
        if (
            str(payload.get("request_id") or "") == request_id
            and str(payload.get("phase") or "") == phase
        ):
            return "completed"
    # Supersession is matched without phase: the marker names the request the
    # head moved off, and a request id only ever exists in one phase.
    for row in store.list_system_events_by_type("funding_workflow_superseded", limit=None):
        payload = row.get("payload") or {}
        if str(payload.get("request_id") or "") == request_id:
            return "superseded"
    return None


def load_request_payload(
    store: StateStore, request_id: str, phase: str
) -> dict[str, Any] | None:
    """The stored request event's payload, whatever its status."""
    _require_phase(phase)
    for row in store.list_system_events_by_type(_REQUEST_EVENT[phase], limit=None):
        payload = row.get("payload") or {}
        if str(payload.get("request_id") or "") == request_id:
            return dict(payload)
    return None


def is_request_pending(store: StateStore, request_id: str, phase: str) -> bool:
    """Whether this request's transition is still outstanding.

    Completion, and only completion, is disqualifying here. Supersession is
    deliberately *not* consulted, even though ``request_terminal_state``
    reports it: whether a superseded request may still be transitioned is a
    question the head answers atomically inside ``claim_workflow_attempt``, and
    it answers it with more information than any read here could have. A
    request whose own claimed transition produced a legitimate successor stays
    resumable past the head moving on (see ``list_incomplete_workflows``), and
    an ordinarily-replaced one is refused as ``not_head`` -- which is also what
    lets the operator be told "already processed or superseded" instead of the
    much vaguer "no longer active". Screening supersession out up here would
    collapse both cases into silence and would re-strand the claimed parent
    that 3a-4 went to some trouble to keep recoverable.
    """
    payload = load_request_payload(store, request_id, phase)
    if payload is None or payload.get("status") != "pending":
        return False
    return request_terminal_state(store, request_id, phase) != "completed"


_REQUEST_EVENT = {
    "funding": "contribution_funding_request",
    "budget": "contribution_budget_request",
}


def _request_event_keys(request_id: str) -> tuple[str, str]:
    """Both possible ``duplicate_key``s a request event for this id could
    ever be published under -- funding phase or budget. Phase-agnostic on
    purpose: whether the id turns out to have been recorded as funding or
    budget is exactly what the caller does not yet know.
    """
    return tuple(f"{event_type}:{request_id}" for event_type in _REQUEST_EVENT.values())

# The pre-CAS handler (_load_pending_funding_request in
# telegram/handlers.py) decides a request is finished by looking for one of
# these legacy event types, never funding_workflow_completed. Exported (not
# underscore-prefixed) because stage 3a-5's upgrade backfill needs the same
# phase->event mapping to interpret history written before this module
# existed.
LEGACY_TERMINAL_EVENT = {
    "funding": "contribution_funding_request_ack",
    "budget": "contribution_budget_request_decision",
}
_LEGACY_TERMINAL_KEY_PREFIX = {
    "funding": "funding-ack",
    "budget": "budget-decision",
}


def _require_phase(phase: str) -> str:
    if phase not in PHASES:
        raise ValueError(f"unknown funding workflow phase: {phase}")
    return phase


def publish_contribution_request(
    store: StateStore,
    run_id: str,
    request: Mapping[str, Any],
    *,
    phase: str,
    successor_of_request_id: str | None = None,
    successor_of_phase: str | None = None,
) -> dict[str, Any]:
    """요청 저장·이전 요청 supersede·head 전환을 한 트랜잭션으로 커밋한다.

    셋을 따로 쓰면 그 사이의 중단이 head가 가리키지 않는 orphan 요청이나
    실체 없는 dangling head를 남긴다. 새 head 키를 forbid로도 선언하는
    이유는 CAS 패배를 예외가 아니라 결과값으로 받기 위해서다.

    한 요청만 단독으로 커밋한다. 시그널 런처럼 여러 요청과 패키지를 함께
    커밋해야 하는 호출자는 ``plan_contribution_request``로 이벤트만 받아
    자기 트랜잭션에 실어야 한다.

    ``successor_of_request_id``/``successor_of_phase`` declare that this
    publish is happening on behalf of a transition already claimed on the
    current head -- see ``plan_contribution_request`` for why that is the
    only thing allowed to supersede a claimed head.
    """
    plan = plan_contribution_request(
        store,
        request,
        phase=phase,
        successor_of_request_id=successor_of_request_id,
        successor_of_phase=successor_of_phase,
    )
    if plan["refusal"] is not None:
        return {
            "committed": False,
            "conflict": plan["refusal"],
            "workflow_id": plan["workflow_id"],
            "version": plan["version"],
            "payload": plan["payload"],
        }
    outcome = store.save_system_events_atomic(
        run_id,
        plan["events"],
        require_duplicate_keys=plan["extra_require_keys"],
        forbid_duplicate_keys=(plan["head_key"], *plan["extra_forbid_keys"]),
    )
    return {
        "committed": bool(outcome["committed"]),
        "conflict": outcome["conflict"],
        "workflow_id": plan["workflow_id"],
        "version": plan["version"],
        # The stored payload, not the caller's: this one carries the
        # funding_workflow_id and duplicate_key that were added here. A caller
        # that audit-logs its own copy would leave the audit trail unable to
        # say which workflow an event belonged to.
        "payload": plan["payload"],
    }


def plan_contribution_request(
    store: StateStore,
    request: Mapping[str, Any],
    *,
    phase: str,
    successor_of_request_id: str | None = None,
    successor_of_phase: str | None = None,
) -> dict[str, Any]:
    """The events that publishing this request would write, without writing them.

    Split out so a signal run can put its requests, their heads, the package
    describing them and the child lineage into a *single* transaction.
    Publishing first and saving the package afterwards leaves a window where
    a failure strands a live request nobody can see: the operator gets no
    card for it (cards come from the package), and the next run generates a
    new request id rather than adopting it.

    ``refusal`` is set when the request must not be published at all, and is
    the same vocabulary the commit path reports. ``head_key`` is the CAS
    target the caller has to declare as forbidden -- it is what turns losing
    the race for that slot into a result rather than an exception.

    A head with an open (claimed, not yet completed) transition on it must
    not be superseded by an unrelated publish: ``complete_workflow`` never
    re-checks the head, so the claimed request would go on to complete
    legitimately on its own attempt fencing while an independent request that
    overwrote its head became claimable too -- one workflow's single live
    decision executing twice. The one publish allowed to move a claimed head
    is the transition's own legitimate successor -- e.g. the follow-up budget
    request a confirmed funding request's child signal run generates -- which
    must declare so via ``successor_of_request_id``/``successor_of_phase``,
    matching the exact request and phase that is open. A declaration that
    does not match is refused exactly like an undeclared publish would be:
    trusting the label without checking it against the open claim would just
    move the race one field over.

    Both the decision and its precondition are computed from one read here,
    but ``extra_require_keys``/``extra_forbid_keys`` pin that decision to the
    write's own atomic transaction: a claim that lands (or the transition
    that already held one completing) between this read and the caller's
    write is what those keys catch, since ``save_system_events_atomic``
    evaluates them fresh, inside the same ``BEGIN IMMEDIATE`` as the insert --
    not merely what this function happened to observe first.
    """
    _require_phase(phase)
    workflow_id = workflow_id_from_request(request)
    request_id = str(request["request_id"])
    head = store.load_funding_workflow_head(workflow_id)
    # A request that already exists is never published a second time. Request
    # ids are minted per run and never reused, so a redelivery is always a
    # message from a run that has already had its turn, not this one asking
    # again -- and the only sound answer is to leave it exactly as it is.
    #
    # Rebuilding it instead is what used to break. Late (req1 -> req2 -> req3,
    # then req2 arrives again) it becomes a fresh transition carrying one key
    # that exists among three that do not: a partial overlap, which raises and
    # takes down every other request in the signal run that carried it. Early
    # (still at head) it would have to reproduce its original batch byte for
    # byte, and it no longer can -- that batch also held the signal package
    # and any supersede marker, so the fingerprint could never match and the
    # store would refuse it as a provenance mismatch. Neither is an error
    # worth raising: the request exists, which is all the caller wanted, so
    # this is reported the same way a lost CAS is and the caller drops it
    # from the package it is building.
    if store.duplicate_key_exists(f"{_REQUEST_EVENT[phase]}:{request_id}"):
        return {
            "refusal": "already_published",
            "events": [],
            "head_key": "",
            "workflow_id": workflow_id,
            "version": int(head.get("version") or 0) if head else 0,
            "payload": dict(request),
            "extra_require_keys": (),
            "extra_forbid_keys": (),
        }
    previous_request_id = str(head.get("request_id") or "") if head else ""
    extra_require_keys: tuple[str, ...] = ()
    extra_forbid_keys: tuple[str, ...] = ()
    is_legitimate_successor = False
    superseded_open_claim_phase: str | None = None
    if head is not None and previous_request_id:
        head_phase = head.get("phase")
        # A head written before phase existed cannot be judged -- see
        # _head_contradicts_transition's docstring for the same rule applied
        # to claiming. Absent is not evidence of "unclaimed".
        if head_phase is not None:
            open_claim_attempt = _open_claim_attempt(
                store, workflow_id, str(head_phase), previous_request_id
            )
            is_declared_successor = (
                successor_of_request_id is not None
                and str(successor_of_request_id) == previous_request_id
                and successor_of_phase is not None
                and str(successor_of_phase) == str(head_phase)
            )
            if open_claim_attempt is not None and is_declared_successor:
                is_legitimate_successor = True
                superseded_open_claim_phase = str(head_phase)
            if open_claim_attempt is not None and not is_declared_successor:
                return {
                    "refusal": "head_claimed",
                    "events": [],
                    "head_key": "",
                    "workflow_id": workflow_id,
                    "version": int(head.get("version") or 0),
                    "payload": dict(request),
                    "extra_require_keys": (),
                    "extra_forbid_keys": (),
                }
            if open_claim_attempt is not None:
                # Verified above to be a legitimate successor of exactly this
                # claim. Requiring it atomically at the write is not a second
                # opinion on that -- claims are append-only, so if it existed
                # for this read it cannot stop existing -- it is what closes
                # the read-then-write gap against a *different* history this
                # decision did not see.
                extra_require_keys = (
                    claim_key(
                        workflow_id, str(head_phase), previous_request_id, open_claim_attempt
                    ),
                )
            elif not store.duplicate_key_exists(
                claim_key(workflow_id, str(head_phase), previous_request_id, 1)
            ):
                # No open claim observed, and attempt 1 was never claimed at
                # all -- as opposed to claimed and already completed, where
                # attempt 1's key exists permanently and forbidding it would
                # refuse every future publish over a transition that is
                # long since resolved. Forbidding it now, atomically at the
                # write, means a claim landing in the gap between this read
                # and that write refuses the supersession there, rather than
                # letting an unrelated publish silently win a race an
                # operator's claim should have won.
                extra_forbid_keys = (
                    claim_key(workflow_id, str(head_phase), previous_request_id, 1),
                )
    # Always the version right after whatever head currently says: trusting a
    # head we didn't write ourselves (e.g. one Task 11's convergence sweep
    # wrote to repair a dangling head) is intentional here, not a gap. The
    # protection against a genuine race for that slot is
    # ``forbid_duplicate_keys``, not second-guessing head's version.
    version = int(head.get("version") or 0) + 1 if head else 1
    payload = dict(request)
    payload["funding_workflow_id"] = workflow_id
    payload["duplicate_key"] = f"{_REQUEST_EVENT[phase]}:{request_id}"

    events: list[dict[str, Any]] = [
        {"event_type": _REQUEST_EVENT[phase], "payload": payload}
    ]
    if previous_request_id and previous_request_id != request_id:
        superseded_payload: dict[str, Any] = {
            "duplicate_key": superseded_key(workflow_id, previous_request_id),
            "workflow_id": workflow_id,
            "request_id": previous_request_id,
            "superseded_by": request_id,
        }
        if is_legitimate_successor:
            # Durable, explicit proof -- not merely inferable from current
            # head state -- that this supersession was previous_request_id's
            # own claimed transition producing request_id as its causal
            # successor (e.g. the follow-up budget request a confirmed
            # funding request's child signal run generates). This is what
            # lets claim_workflow_attempt and list_incomplete_workflows tell
            # "the parent's own transition moved the head on" apart from "an
            # unrelated request took the head" -- the latter can only reach
            # this branch of plan_contribution_request at all when
            # previous_request_id had no open claim, so it is never marked.
            # Append-only and permanent once written: nothing later can
            # revoke a supersession's own legitimacy.
            superseded_payload["legitimate_successor"] = True
            superseded_payload["successor_of_phase"] = superseded_open_claim_phase
        events.append(
            {
                "event_type": "funding_workflow_superseded",
                "payload": superseded_payload,
            }
        )
    new_head_key = head_key(workflow_id, version)
    events.append(
        {
            "event_type": "funding_workflow_head",
            "payload": {
                "duplicate_key": new_head_key,
                "workflow_id": workflow_id,
                "version": version,
                "request_id": request_id,
                "phase": phase,
                "status": "pending",
                "scope": [
                    request.get("contribution_group_id"),
                    request.get("account_id"),
                    request.get("execution_sleeve"),
                    request.get("currency"),
                ],
            },
        }
    )
    return {
        "refusal": None,
        "events": events,
        "head_key": new_head_key,
        "workflow_id": workflow_id,
        "version": version,
        "payload": payload,
        "extra_require_keys": extra_require_keys,
        "extra_forbid_keys": extra_forbid_keys,
    }


class WorkflowClaimRefused(RuntimeError):
    """This request may not enter this transition -- already claimed or superseded.

    Raised instead of returning a falsy claim so a caller cannot forget to
    check it and fall through into cash-flow recording or ``run_signal()``
    anyway; the exception forces the caller to stop before any side effect.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"funding workflow claim refused: {reason}")
        self.reason = reason


def require_completed_predecessor(
    store: StateStore,
    *,
    workflow_id: str,
    request_id: str,
    phase: str,
) -> None:
    """Fail closed before claim when a legitimate successor's predecessor is incomplete."""
    _require_phase(phase)
    if phase != "budget":
        return

    predecessor_request_ids: set[str] = set()
    for row in store.list_system_events_by_type("funding_workflow_superseded", limit=None):
        payload = row.get("payload") or {}
        if (
            payload.get("workflow_id") == workflow_id
            and str(payload.get("superseded_by") or "") == request_id
            and payload.get("legitimate_successor") is True
            and str(payload.get("successor_of_phase") or "") == "funding"
        ):
            pred_req_id = str(payload.get("request_id") or "")
            if pred_req_id:
                predecessor_request_ids.add(pred_req_id)

    if not predecessor_request_ids:
        return
    if len(predecessor_request_ids) > 1:
        raise WorkflowClaimRefused("predecessor_ambiguous")

    predecessor_request_id = next(iter(predecessor_request_ids))
    if not store.duplicate_key_exists(
        completed_key(workflow_id, predecessor_request_id, "funding")
    ):
        raise WorkflowClaimRefused("predecessor_incomplete")


def claim_workflow_attempt(
    store: StateStore,
    run_id: str,
    *,
    workflow_id: str,
    request_id: str,
    phase: str,
    attempt: int = 1,
    expected_version: int | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """이 요청이 이 전이에 진입해도 되는지를 원자적으로 결정한다.

    head 조회와 claim 삽입을 따로 하면 그 사이에 예약/수동 run이 새 head를
    원자 커밋하는 TOCTOU가 생겨, 이미 superseded된 구 callback이 유효한
    claim을 얻는다. 그래서 "그 버전이 있고(require) 다음 버전은 아직
    없다(forbid)"를 삽입과 같은 트랜잭션에서 건다 — head는 단조 증가하므로
    존재 검사만으로는 교체를 감지할 수 없다.

    request id와 version이 맞아도 head 자체를 무조건 믿지는 않는다: head가
    말하는 scope나 phase가 이 전이의 것과 다르면 손상됐거나 잘못 backfill된
    기록이고, 그런 head를 근거로 claim을 내주면 엉뚱한 전이를 — 예컨대
    budget 결정을 funding 확정으로 — 승인해 버린다. 다만 그 필드가 *없는*
    head는 필드가 생기기 전 릴리스가 쓴 것이므로 판정하지 않고 통과시킨다:
    검증할 수 없는 것과 어긋나는 것은 다르다.

    끝난 워크플로우도 거절한다(``already_completed``). head는 완료됐다고
    내려가지 않으므로 head 검사만으로는 종결을 알 수 없고, 종결된 요청에
    새 attempt를 내주면 그 attempt가 child run과 cash flow를 다시 밟은 뒤
    맨 마지막에야 거절당한다.

    ``attempt``는 1부터 시작해 순차적이어야 한다. attempt N (N>1)의 claim은
    attempt N-1의 claim 키가 이미 존재함을 이 삽입과 같은 트랜잭션에서
    요구한다 -- 그렇지 않으면 attempt 2를 건너뛰고 바로 attempt 3을 claim할
    수 있고, ``complete_workflow``의 fencing은 "바로 다음 attempt가
    없다"만 보므로 attempt 3이 이미 전이를 인수한 뒤에도 attempt 1이
    ``claim_key(..., 2)``가 여전히 없다는 사실만으로 완료를 통과시켜
    버린다. 존재 검사를 여기서 걸어 두면 attempt 3은 애초에 claim조차
    얻지 못한다.
    """
    _require_phase(phase)
    if attempt < 1:
        raise ValueError(f"attempt must be a positive integer, got {attempt}")
    head = store.load_funding_workflow_head(workflow_id)
    if head is None:
        return {"claimed": False, "reason": "no_head", "attempt": attempt, "head_version": 0}
    version = int(expected_version if expected_version is not None else head.get("version") or 0)
    via_legitimate_successor = False
    if str(head.get("request_id") or "") != request_id:
        # Not head -- but not-head-at-all and not-head-because-my-own-claimed-
        # transition-legitimately-moved-on are different situations.
        # ``plan_contribution_request`` marks the latter durably (see its
        # docstring): a ``funding_workflow_superseded`` row for this exact
        # request_id, in this exact phase, that can only ever have been
        # written when this request_id itself had an open claim declaring
        # the very request that replaced it as its successor. An unrelated
        # publish can never produce that marker -- it is refused outright
        # while a claim is open -- so finding it here is proof, not
        # inference, that request_id's own transition is what moved the
        # head away, and that transition may still be resumed to finish its
        # own bookkeeping regardless of what the head reads now.
        marker = store.load_system_event_payload_by_duplicate_key(
            superseded_key(workflow_id, request_id)
        )
        if (
            marker is not None
            and marker.get("legitimate_successor") is True
            and str(marker.get("successor_of_phase")) == phase
        ):
            via_legitimate_successor = True
        else:
            return {
                "claimed": False,
                "reason": "not_head",
                "attempt": attempt,
                "head_version": int(head.get("version") or 0),
            }
    if not via_legitimate_successor and _head_contradicts_transition(
        head, workflow_id=workflow_id, phase=phase
    ):
        return {
            "claimed": False,
            "reason": "head_corrupt",
            "attempt": attempt,
            "head_version": int(head.get("version") or 0),
        }
    claim_payload = {
        "duplicate_key": claim_key(workflow_id, phase, request_id, attempt),
        "workflow_id": workflow_id,
        "request_id": request_id,
        "phase": phase,
        "attempt": attempt,
        "head_version": version,
    }
    if extra:
        # Lets a resumed attempt (Task 10) reuse the operator's original
        # input -- e.g. the selected budget amount -- without asking again.
        # duplicate_key must identify the claim's content, so only
        # deterministic values may ride along here: never a timestamp or a
        # random id, or a legitimate retry would fail to replay byte-for-byte
        # and would be mistaken for a conflicting overlap instead.
        claim_payload.update(dict(extra))
    # head가 움직이지 않았어도 이 전이가 이미 끝났을 수 있다. head는 완료로
    # 내려가지 않고 다음 요청이 올 때까지 그대로이므로, completed 키를 함께
    # 금지하지 않으면 종결된 워크플로우에 attempt N+1이 그대로 진입한다 --
    # 그리고 그 attempt는 cash flow와 child run을 다시 밟은 뒤에야
    # complete_workflow의 replay 판정에 걸린다.
    finished_key = completed_key(workflow_id, request_id, phase)
    required_keys: list[str] = []
    forbid_keys = [finished_key]
    if via_legitimate_successor:
        # request_id's own head slot is not what this claim defends -- it
        # has already moved on, for a reason durably proven above. Requiring
        # that same proof's own key here is the atomic anchor: the marker is
        # append-only, so if it existed for the read above it cannot stop
        # existing by the time this write commits. Pinning to a head
        # version would be meaningless (and wrong) here, since head is
        # expected to differ from whatever it was when request_id was
        # published.
        required_keys.append(superseded_key(workflow_id, request_id))
    else:
        required_keys.append(head_key(workflow_id, version))
        forbid_keys.append(head_key(workflow_id, version + 1))
    previous_attempt_key = None
    if attempt > 1:
        # Sequential-attempt fencing: attempt N may only be claimed once
        # attempt N-1's own claim is on record. Without this, a caller (or a
        # bug) that jumps straight to attempt 3 gets a claim for it, and
        # ``complete_workflow`` -- which only checks that attempt N+1 does
        # not exist -- would later let a stale attempt 1 complete over it,
        # since it never asked whether 2 or 3 existed.
        previous_attempt_key = claim_key(workflow_id, phase, request_id, attempt - 1)
        required_keys.append(previous_attempt_key)
    try:
        outcome = store.save_system_events_atomic(
            run_id,
            [
                {
                    "event_type": "funding_workflow_claim",
                    "payload": claim_payload,
                }
            ],
            require_duplicate_keys=tuple(required_keys),
            forbid_duplicate_keys=tuple(forbid_keys),
        )
    except ValueError as exc:
        # 같은 attempt를 서로 다른 ``extra`` 내용으로 두 번 claim하면
        # (예: 운영자가 같은 카드에서 50만을 눌렀다가 100만을 누름) 키는
        # 같고 내용은 다른 충돌이 되어 store가 ValueError를 던진다. 그건
        # 내부 오류가 아니라 "이 attempt는 이미 누군가 잡았다"는 뜻이므로,
        # 다른 모든 거절과 같은 경로(WorkflowClaimRefused ->
        # _funding_claim_refusal_response)로 흘려보낸다. 이 claim 자신의
        # 키에 대한 content 충돌만 삼킨다 -- provenance 불일치나 다른
        # 원인의 ValueError는 그대로 올려보내야 한다.
        if not _is_own_key_content_conflict(exc, str(claim_payload["duplicate_key"])):
            raise
        return {
            "claimed": False,
            "reason": "already_claimed",
            "attempt": attempt,
            "head_version": version,
        }
    if outcome["committed"]:
        return {"claimed": True, "reason": None, "attempt": attempt, "head_version": version}
    conflict = str(outcome["conflict"])
    conflicting_keys = set(outcome.get("conflicting_keys") or ())
    if conflict == "already_committed":
        reason = "already_claimed"
    elif previous_attempt_key is not None and previous_attempt_key in conflicting_keys:
        reason = "attempt_out_of_order"
    elif finished_key in conflicting_keys:
        reason = "already_completed"
    else:
        reason = "head_moved"
    return {"claimed": False, "reason": reason, "attempt": attempt, "head_version": version}


def _head_contradicts_transition(
    head: Mapping[str, Any], *, workflow_id: str, phase: str
) -> bool:
    """Whether this head describes a different workflow than the one claimed.

    ``load_funding_workflow_head`` selects by ``workflow_id``, so agreement is
    supposed to be structural -- which is exactly why a disagreement here is
    worth refusing on rather than ignoring: it can only come from a record
    that was corrupted or backfilled with a scope or phase that never matched
    its own key. Absent fields are not a disagreement (see the docstring in
    ``claim_workflow_attempt``); only a value that is present and wrong is.
    """
    head_phase = head.get("phase")
    if head_phase is not None and str(head_phase) != phase:
        return True
    scope = head.get("scope")
    if scope is None:
        return False
    if not isinstance(scope, (list, tuple)):
        return True
    return not workflow_id.startswith(scope_prefix(scope))


def _is_own_key_content_conflict(exc: ValueError, duplicate_key: str) -> bool:
    """``save_system_events_atomic``의 "같은 키, 다른 내용" 충돌인가.

    store가 이 한 가지를 위한 예외 타입을 따로 두지 않으므로 메시지로
    판별한다. 좁게 잡는 것이 핵심이다: provenance 불일치(다른 writer가
    같은 키를 썼다)나 malformed batch는 진짜 오류이므로 삼키면 안 된다.
    """
    message = str(exc)
    return "with different content" in message and repr(duplicate_key) in message


def _open_claim_attempt(
    store: StateStore, workflow_id: str, phase: str, request_id: str
) -> int | None:
    """The highest attempt claimed for this request, if the request has no
    completion yet -- ``None`` if it was never claimed, or if it was and has
    since completed. Either of those means no transition is currently
    running on it for a publish to worry about superseding.

    Scans every claim rather than probing a single deterministic key: unlike
    ``head_key`` or ``completed_key``, the attempt number is not known in
    advance here, so there is no key to probe. Matches the scan
    ``list_incomplete_workflows`` already does for the same "claimed but not
    completed" question, scoped to one request instead of every workflow.
    """
    if store.duplicate_key_exists(completed_key(workflow_id, request_id, phase)):
        return None
    latest: int | None = None
    for row in store.list_system_events_by_type("funding_workflow_claim", limit=None):
        payload = row.get("payload") or {}
        if (
            str(payload.get("workflow_id")) != workflow_id
            or str(payload.get("phase")) != phase
            or str(payload.get("request_id")) != request_id
        ):
            continue
        attempt = int(payload.get("attempt") or 0)
        if latest is None or attempt > latest:
            latest = attempt
    return latest


def complete_workflow(
    store: StateStore,
    run_id: str,
    *,
    workflow_id: str,
    request_id: str,
    phase: str,
    attempt: int,
    legacy_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Close out the transition and, in the same batch, write its legacy twin.

    The pre-CAS handler never learns about ``funding_workflow_completed`` --
    it only recognizes the legacy ack/decision event. If ``completed`` landed
    on its own and a rollback followed, that old handler would still see the
    request as pending, and would call ``run_signal()`` and re-record the
    cash flow a second time. Writing both in one ``save_system_events_atomic``
    call means either a rollback sees neither (still pending, correctly) or
    both (finished, correctly) -- never the split state that makes the old
    code re-run work that already happened.

    ``legacy_payload`` must carry no timestamp or other non-deterministic
    field: its ``duplicate_key`` is derived only from ``request_id`` and
    ``phase``, so a legitimate retry has to reproduce byte-identical content
    to be recognized as a replay rather than fail as a conflicting overlap.

    ``attempt`` is the fencing token, and it is only a token if it is checked
    here rather than merely recorded. The attempt's own claim must exist
    (this caller really did enter the transition) and the next attempt's must
    not (nobody has taken it over since). Without both, an attempt 1 that
    stalled long enough for the operator to resume as attempt 2 could still
    surface afterwards -- inside ``run_signal`` on a process that only looked
    dead -- and write the completion for a transition attempt 2 now owns,
    closing the workflow under attempt 2's feet.

    "the next attempt does not exist" only means "attempt is the latest one"
    because ``claim_workflow_attempt`` is the sole writer of claim events and
    refuses to claim attempt N unless attempt N-1's claim already exists --
    attempts can never have a gap. A completion check that instead re-derived
    "latest" by scanning claims would be redundant with that invariant, not a
    second layer of defense; the invariant is what has to hold, and it holds
    at the one place claims are written, not at every place that reads them.

    A fenced-out completion raises ``WorkflowClaimRefused`` rather than
    returning falsy: every caller here has already done the transition's side
    effects by the time it gets to this call, so a return value it could
    ignore would let it report success for a completion that never landed.
    """
    _require_phase(phase)
    legacy = dict(legacy_payload)
    legacy["duplicate_key"] = f"{_LEGACY_TERMINAL_KEY_PREFIX[phase]}:{request_id}"
    outcome = store.save_system_events_atomic(
        run_id,
        [
            {
                "event_type": "funding_workflow_completed",
                "payload": {
                    "duplicate_key": completed_key(workflow_id, request_id, phase),
                    "workflow_id": workflow_id,
                    "request_id": request_id,
                    "phase": phase,
                    "attempt": attempt,
                },
            },
            {"event_type": LEGACY_TERMINAL_EVENT[phase], "payload": legacy},
        ],
        require_duplicate_keys=(claim_key(workflow_id, phase, request_id, attempt),),
        forbid_duplicate_keys=(claim_key(workflow_id, phase, request_id, attempt + 1),),
    )
    conflict = str(outcome["conflict"] or "")
    if conflict == "precondition_missing":
        raise WorkflowClaimRefused("unclaimed_attempt")
    if conflict == "precondition_present":
        raise WorkflowClaimRefused("attempt_superseded")
    return {"committed": bool(outcome["committed"]), "conflict": outcome["conflict"]}


def list_incomplete_workflows(store: StateStore) -> list[dict[str, Any]]:
    """A claim exists for this (request_id, phase) but no completion followed.

    This is only ever consumed by an operator-facing sweep (Task 10): it is
    input to a recovery card, never to an automatic resume. A still-running
    earlier attempt's ``run_signal()`` can be mid-flight in a process that
    only *looks* dead, so nothing here may re-enter the transition on its
    own -- entry happens solely through the operator's explicit [재개] tap,
    which commits ``attempt + 1`` as its own fencing token.

    head가 더 이상 가리키지 않는 요청은 제외한다: 그 요청의 claim은 무슨
    수를 써도 다시 커밋될 수 없으므로([재개]는 ``not_head``로 거절된다)
    attempt가 영원히 오르지 않고, 행은 "조치 필요" 목록에 영구히 남는다.

    각 행의 ``intent``는 운영자가 요청한 종단 전이("confirm"/"cancel")다.
    이 키가 없는 claim은 이 필드가 생기기 전 릴리스가 쓴 것이고, 그 시절
    재개 경로가 수행할 수 있던 전이는 confirm 하나뿐이었으므로 confirm으로
    읽는다. 알 수 없는 값은 confirm으로 뭉개지 않고 그대로 넘겨, 호출자가
    조용히 잘못된 전이를 실행하는 대신 실패하게 한다.
    """
    live = {
        (str(row.get("workflow_id")), str(row.get("request_id")))
        for row in store.list_funding_workflow_heads()
    }
    # A request whose own claimed transition produced a legitimate successor
    # (see plan_contribution_request) stays surfaceable even after the head
    # moves to that successor: complete_workflow never re-checks head, so
    # the transition is still there to finish, and claim_workflow_attempt
    # accepts a resumed attempt on it via the same durable marker checked
    # here. An ordinary (non-successor) supersession is not added -- that
    # marker can only ever be written while the superseded request had an
    # open claim, so a merely-replaced, never-claimed request is correctly
    # left out, exactly as before.
    live.update(
        (str(payload.get("workflow_id")), str(payload.get("request_id")))
        for payload in (
            (row.get("payload") or {})
            for row in store.list_system_events_by_type(
                "funding_workflow_superseded", limit=None
            )
        )
        if payload.get("legitimate_successor") is True
    )
    completed = {
        (
            str((row.get("payload") or {}).get("request_id")),
            str((row.get("payload") or {}).get("phase")),
        )
        for row in store.list_system_events_by_type("funding_workflow_completed", limit=None)
    }
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in store.list_system_events_by_type("funding_workflow_claim", limit=None):
        payload = row.get("payload") or {}
        key = (str(payload.get("request_id")), str(payload.get("phase")))
        if key in completed:
            continue
        workflow_id = str(payload.get("workflow_id"))
        if (workflow_id, key[0]) not in live:
            continue
        current = latest.get(key)
        attempt = int(payload.get("attempt") or 0)
        if current is None or attempt > int(current["attempt"]):
            intent = payload.get("intent")
            latest[key] = {
                "workflow_id": workflow_id,
                "request_id": key[0],
                "phase": key[1],
                "attempt": attempt,
                "intent": "confirm" if intent is None else str(intent),
                "selected_budget": payload.get("selected_budget"),
            }
    return sorted(latest.values(), key=lambda row: (row["phase"], row["request_id"]))


def load_migration_cutoff(store: StateStore) -> int | None:
    """3a 업그레이드 backfill이 고정한 경계. 3a-5가 이 이벤트를 기록한다.

    이 이벤트가 없으면 ``None``을 돌려주고, 수렴 sweep은 켜지지 않는다.
    3a 이전에 만들어진 요청은 head 개념 자체가 없던 시절의 것이라 head가
    없는 게 정상이지 orphan이 아니다. 경계를 모른 채(혹은 0으로 지어내어)
    쓸어담으면 그 요청들이 전부 orphan으로 오판되어 supersede되고, 이번
    달 투자가 조용히 취소된다.

    판정 자체는 ``migration_state``가 단독으로 소유한다. 마커가 서로 모순되면
    ``MigrationStateInvalid``를 올린다: 여기서 조용히 ``None``을 돌려주면
    sweep은 "3a 이전 DB라 수렴할 것이 없다"와 "마커가 깨져 무엇도 믿을 수
    없다"를 같은 무행동으로 처리하고, 운영자는 후자에 대해 아무 신호도 받지
    못한다.
    """
    from maestro.state.migration_state import (
        MigrationPhase,
        MigrationStateInvalid,
        load_migration_state,
    )

    state = load_migration_state(store)
    if state.phase is MigrationPhase.INVALID:
        raise MigrationStateInvalid(str(state.reason))
    return state.cutoff


def converge_workflow_invariants(store: StateStore, *, cutoff: int | None) -> dict[str, int]:
    """head 없는 요청과 실체 없는 head를 수렴시킨다.

    atomic publish(``publish_contribution_request``)가 정상 경로에서 이
    상태들을 만들지 않는다는 것이 1차 방어다. 이 sweep은 그 방어가 닿지
    않는 경로 -- 마이그레이션 중단, 수동 복구 스크립트, 다른 버전의
    writer -- 를 위한 backstop이고, 전이를 재실행하지 않는다: orphan
    요청에는 supersede 마커만 붙이고, dangling head는 실제로 존재하는
    요청을 가리키던 직전 버전으로만 되돌린다.

    **orphan의 정의**: cutoff 이후에 쓰였고, ``status == "pending"``이며,
    어떤 head도 가리키지 않고(``live_request_ids``), *그리고* 이 요청이
    이미 다른 방식으로 결말을 맺은 적도 없는 요청이다. "결말을 맺었다"는
    ``funding_workflow_superseded`` 마커가 이미 있거나(정상적인 in-month
    교체 -- ``publish_contribution_request``가 이미 썼다) 혹은
    ``funding_workflow_completed``가 있는 경우다. head만 보고 판단하면
    이 두 경우를 orphan으로 오판해 이미 존재하는 마커 위에 다른 내용을
    다시 쓰려다 충돌로 예외를 던진다 -- 이는 매 poll 사이클마다 벌어지는
    일상적인 교체이지, crash로 생긴 진짜 orphan이 아니다.

    한 행이 손상돼(``month_key``도 ``funding_workflow_id``도 없는 등)
    처리 중 예외를 던져도 그 행만 건너뛴다: 이 sweep은 여러 워크플로우에
    걸쳐 도는 backstop이므로, 한 행의 결함이 나머지 전체를 막아서는
    안 된다.

    다만 쓰기가 ValueError로 충돌해 건너뛴 행은 ``conflicts_skipped``로
    세어 돌려준다. 그 충돌은 손상된 행이 아니라 *다른 writer가 이 sweep이
    자기 것이라 믿은 키에 다른 내용을 썼다*는 뜻이고, 0이 아니면 사람이
    봐야 하는 사건이다. 그래서 로그도 warning이 아니라 error다.
    """
    if cutoff is None:
        return {"orphans_superseded": 0, "heads_rolled_back": 0, "conflicts_skipped": 0}

    heads = {row["workflow_id"]: row for row in store.list_funding_workflow_heads()}
    live_request_ids = {str(row.get("request_id")) for row in heads.values()}

    accounted_superseded = set()
    for row in store.list_system_events_by_type("funding_workflow_superseded", limit=None):
        payload = row.get("payload") or {}
        accounted_superseded.add((str(payload.get("workflow_id")), str(payload.get("request_id"))))
    accounted_completed = set()
    for row in store.list_system_events_by_type("funding_workflow_completed", limit=None):
        payload = row.get("payload") or {}
        accounted_completed.add(
            (
                str(payload.get("workflow_id")),
                str(payload.get("request_id")),
                str(payload.get("phase")),
            )
        )

    orphans_superseded = 0
    conflicts_skipped = 0
    for phase, event_type in _REQUEST_EVENT.items():
        for row in store.list_system_events_by_type(event_type, limit=None):
            # cutoff is a system_events.id, monotonically increasing: only
            # requests written strictly after the migration marker are
            # candidates. One written at or before it predates heads
            # entirely and is left untouched.
            if int(row.get("id") or 0) <= cutoff:
                continue
            payload = row.get("payload") or {}
            if payload.get("status") != "pending":
                continue
            request_id = str(payload.get("request_id") or "")
            if not request_id or request_id in live_request_ids:
                continue
            try:
                workflow_id = str(
                    payload.get("funding_workflow_id") or workflow_id_from_request(payload)
                )
            except (ValueError, KeyError):
                logger.warning(
                    "converge_workflow_invariants: skipping malformed %s row id=%s "
                    "request_id=%r -- cannot derive workflow_id",
                    event_type,
                    row.get("id"),
                    request_id,
                )
                continue
            if (workflow_id, request_id) in accounted_superseded:
                continue
            if (workflow_id, request_id, phase) in accounted_completed:
                continue
            # ``live_request_ids`` is a snapshot taken once, at the top of
            # this sweep -- everything between here and the write below (an
            # earlier candidate's own write, another writer entirely) can
            # change what it says without this loop ever re-reading it. The
            # candidate might still look headless by every check above and
            # yet have gained a real head in the meantime: a manual repair
            # landing the request this row's own workflow was missing, most
            # plausibly. Pinning the write to the exact head state this
            # snapshot saw -- present and unmoved, or altogether absent --
            # is what makes the decision this loop already made agree with
            # what SQLite actually commits, rather than merely with what was
            # true when the snapshot was taken.
            head_for_workflow = heads.get(workflow_id)
            if head_for_workflow is not None:
                head_version = int(head_for_workflow.get("version") or 0)
                require_keys = (head_key(workflow_id, head_version),)
                forbid_keys = (head_key(workflow_id, head_version + 1),)
            else:
                require_keys = ()
                forbid_keys = (head_key(workflow_id, 1),)
            try:
                outcome = store.save_system_events_atomic(
                    str(row.get("run_id") or request_id),
                    [
                        {
                            "event_type": "funding_workflow_superseded",
                            "payload": {
                                "duplicate_key": superseded_key(workflow_id, request_id),
                                "workflow_id": workflow_id,
                                "request_id": request_id,
                                "phase": phase,
                                "reason": "orphan_no_head",
                            },
                        }
                    ],
                    require_duplicate_keys=require_keys,
                    forbid_duplicate_keys=forbid_keys,
                )
            except ValueError:
                logger.error(
                    "converge_workflow_invariants: skipping request_id=%r "
                    "workflow_id=%r -- supersede write conflicted unexpectedly",
                    request_id,
                    workflow_id,
                )
                conflicts_skipped += 1
                continue
            if outcome["committed"]:
                orphans_superseded += 1

    heads_rolled_back = 0
    for workflow_id, head in heads.items():
        request_id = str(head.get("request_id") or "")
        try:
            if _request_ever_recorded(store, request_id):
                continue
            previous = _previous_head_with_a_real_request(store, workflow_id, head)
        except (ValueError, KeyError):
            logger.warning(
                "converge_workflow_invariants: skipping dangling-head check for "
                "workflow_id=%r -- malformed head history",
                workflow_id,
            )
            continue
        if previous is None:
            continue
        version = int(head.get("version") or 0) + 1
        new_key = head_key(workflow_id, version)
        try:
            outcome = store.save_system_events_atomic(
                workflow_id,
                [
                    {
                        "event_type": "funding_workflow_head",
                        "payload": {
                            **previous,
                            "duplicate_key": new_key,
                            "version": version,
                            "reason": "dangling_head_rollback",
                        },
                    }
                ],
                # ``new_key`` alone (the row's own key) already refuses a
                # rollback that lost the race for the next version -- that
                # collision is caught earlier, as an own-key content
                # mismatch, before forbid is even consulted. What it does
                # not catch is the request itself turning up: the head can
                # stay at exactly this version and still stop being
                # dangling, if the request it names -- missing when
                # ``_request_ever_recorded`` was checked above -- gets
                # recorded (a manual repair, a delayed write from a
                # different process) before this write commits. Forbidding
                # both possible event types for it here, evaluated fresh
                # inside this same transaction, is what makes that repair
                # win instead of being silently rolled back over.
                forbid_duplicate_keys=(new_key, *_request_event_keys(request_id)),
            )
        except ValueError:
            logger.error(
                "converge_workflow_invariants: skipping head rollback for "
                "workflow_id=%r -- write conflicted unexpectedly",
                workflow_id,
            )
            conflicts_skipped += 1
            continue
        if outcome["committed"]:
            heads_rolled_back += 1

    return {
        "orphans_superseded": orphans_superseded,
        "heads_rolled_back": heads_rolled_back,
        "conflicts_skipped": conflicts_skipped,
    }


def _request_ever_recorded(store: StateStore, request_id: str) -> bool:
    """Whether some ``contribution_*_request`` event names this request_id at all.

    Used only to decide whether a head is dangling -- it deliberately does
    not care about status, since a completed or superseded request is still
    proof the head's target once existed.
    """
    if not request_id:
        return False
    for event_type in _REQUEST_EVENT.values():
        for row in store.list_system_events_by_type(event_type, limit=None):
            if str((row.get("payload") or {}).get("request_id") or "") == request_id:
                return True
    return False


def _previous_head_with_a_real_request(
    store: StateStore, workflow_id: str, head: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The most recent version below ``head`` whose request actually exists.

    Not merely ``head.version - 1``: a rollback that landed on another
    dangling version would just move the problem back one slot instead of
    converging it.
    """
    candidates = [
        dict(row.get("payload") or {})
        for row in store.list_system_events_by_type("funding_workflow_head", limit=None)
        if (row.get("payload") or {}).get("workflow_id") == workflow_id
    ]
    candidates.sort(key=lambda row: int(row.get("version") or 0), reverse=True)
    head_version = int(head.get("version") or 0)
    for candidate in candidates:
        if int(candidate.get("version") or 0) >= head_version:
            continue
        if _request_ever_recorded(store, str(candidate.get("request_id") or "")):
            return candidate
    return None


__all__ = [
    "LEGACY_TERMINAL_EVENT",
    "PHASES",
    "TERMINAL_WORKFLOW_EVENTS",
    "WorkflowClaimRefused",
    "child_key",
    "claim_key",
    "claim_workflow_attempt",
    "complete_workflow",
    "completed_key",
    "converge_workflow_invariants",
    "funding_workflow_id",
    "head_key",
    "is_request_pending",
    "list_incomplete_workflows",
    "load_migration_cutoff",
    "load_request_payload",
    "load_workflow_child",
    "plan_contribution_request",
    "publish_contribution_request",
    "request_terminal_state",
    "require_completed_predecessor",
    "scope_prefix",
    "superseded_key",
    "workflow_id_from_request",
]

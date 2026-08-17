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
from collections.abc import Mapping
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
    scope = json.dumps(
        [contribution_group_id, account_id, execution_sleeve, currency],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"funding:{scope}:{month_key}"


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


_REQUEST_EVENT = {
    "funding": "contribution_funding_request",
    "budget": "contribution_budget_request",
}

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
) -> dict[str, Any]:
    """요청 저장·이전 요청 supersede·head 전환을 한 트랜잭션으로 커밋한다.

    셋을 따로 쓰면 그 사이의 중단이 head가 가리키지 않는 orphan 요청이나
    실체 없는 dangling head를 남긴다. 새 head 키를 forbid로도 선언하는
    이유는 CAS 패배를 예외가 아니라 결과값으로 받기 위해서다.
    """
    _require_phase(phase)
    workflow_id = workflow_id_from_request(request)
    request_id = str(request["request_id"])
    head = store.load_funding_workflow_head(workflow_id)
    previous_request_id = str(head.get("request_id") or "") if head else ""
    # A resubmission of the request already at head is the same transition,
    # not a new one: target the same version so the batch matches byte for
    # byte what already landed and the atomic call reports a plain replay
    # instead of a same-request partial overlap with itself. Any other case
    # -- a new request, or a head that changed underneath us -- always
    # targets the version right after whatever head currently says: trusting
    # a head we didn't write ourselves (e.g. one Task 11's convergence sweep
    # wrote to repair a dangling head) is intentional here, not a gap. The
    # protection against a genuine race for that slot is
    # ``forbid_duplicate_keys`` below, not second-guessing head's version.
    if head and previous_request_id == request_id:
        version = int(head.get("version") or 1)
    else:
        version = int(head.get("version") or 0) + 1 if head else 1
    payload = dict(request)
    payload["funding_workflow_id"] = workflow_id
    payload["duplicate_key"] = f"{_REQUEST_EVENT[phase]}:{request_id}"

    events: list[dict[str, Any]] = [
        {"event_type": _REQUEST_EVENT[phase], "payload": payload}
    ]
    if previous_request_id and previous_request_id != request_id:
        events.append(
            {
                "event_type": "funding_workflow_superseded",
                "payload": {
                    "duplicate_key": superseded_key(workflow_id, previous_request_id),
                    "workflow_id": workflow_id,
                    "request_id": previous_request_id,
                    "superseded_by": request_id,
                },
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
    outcome = store.save_system_events_atomic(
        run_id,
        events,
        forbid_duplicate_keys=(new_head_key,),
    )
    return {
        "committed": bool(outcome["committed"]),
        "conflict": outcome["conflict"],
        "workflow_id": workflow_id,
        "version": version,
        # The stored payload, not the caller's: this one carries the
        # funding_workflow_id and duplicate_key that were added here. A caller
        # that audit-logs its own copy would leave the audit trail unable to
        # say which workflow an event belonged to.
        "payload": payload,
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
    """
    _require_phase(phase)
    head = store.load_funding_workflow_head(workflow_id)
    if head is None:
        return {"claimed": False, "reason": "no_head", "attempt": attempt, "head_version": 0}
    version = int(expected_version if expected_version is not None else head.get("version") or 0)
    if str(head.get("request_id") or "") != request_id:
        return {
            "claimed": False,
            "reason": "not_head",
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
    try:
        outcome = store.save_system_events_atomic(
            run_id,
            [
                {
                    "event_type": "funding_workflow_claim",
                    "payload": claim_payload,
                }
            ],
            require_duplicate_keys=(head_key(workflow_id, version),),
            forbid_duplicate_keys=(head_key(workflow_id, version + 1),),
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
    reason = {
        "already_committed": "already_claimed",
        "precondition_present": "head_moved",
        "precondition_missing": "head_moved",
    }.get(str(outcome["conflict"]), "head_moved")
    return {"claimed": False, "reason": reason, "attempt": attempt, "head_version": version}


def _is_own_key_content_conflict(exc: ValueError, duplicate_key: str) -> bool:
    """``save_system_events_atomic``의 "같은 키, 다른 내용" 충돌인가.

    store가 이 한 가지를 위한 예외 타입을 따로 두지 않으므로 메시지로
    판별한다. 좁게 잡는 것이 핵심이다: provenance 불일치(다른 writer가
    같은 키를 썼다)나 malformed batch는 진짜 오류이므로 삼키면 안 된다.
    """
    message = str(exc)
    return "with different content" in message and repr(duplicate_key) in message


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
    )
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
    """
    rows = store.list_system_events_by_type("funding_workflow_migration_started", limit=None)
    if not rows:
        return None
    return min(int((row.get("payload") or {}).get("cutoff") or 0) for row in rows)


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
                forbid_duplicate_keys=(new_key,),
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
    "WorkflowClaimRefused",
    "child_key",
    "claim_key",
    "claim_workflow_attempt",
    "complete_workflow",
    "completed_key",
    "converge_workflow_invariants",
    "funding_workflow_id",
    "head_key",
    "list_incomplete_workflows",
    "load_migration_cutoff",
    "load_workflow_child",
    "publish_contribution_request",
    "superseded_key",
    "workflow_id_from_request",
]

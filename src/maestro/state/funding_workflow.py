"""funding/budget 워크플로우의 영속 식별자와 duplicate_key 규약.

같은 계좌·같은 달이라도 contribution_group_id/execution_sleeve/currency가
다르면 독립 워크플로우다. 그래서 키는 scope 전체를 담은 복합 키이고,
직렬화는 타입(null vs 문자열)과 경계(escaping)를 모두 보존하는 canonical
JSON 배열이다 — 문자열 sentinel이나 단순 join은 서로 다른 scope를 같은
head로 합쳐 한쪽의 월간 투자를 조용히 supersede할 수 있다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.state.store import StateStore

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


def claim_workflow_attempt(
    store: StateStore,
    run_id: str,
    *,
    workflow_id: str,
    request_id: str,
    phase: str,
    attempt: int = 1,
    expected_version: int | None = None,
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
    outcome = store.save_system_events_atomic(
        run_id,
        [
            {
                "event_type": "funding_workflow_claim",
                "payload": {
                    "duplicate_key": claim_key(workflow_id, phase, request_id, attempt),
                    "workflow_id": workflow_id,
                    "request_id": request_id,
                    "phase": phase,
                    "attempt": attempt,
                    "head_version": version,
                },
            }
        ],
        require_duplicate_keys=(head_key(workflow_id, version),),
        forbid_duplicate_keys=(head_key(workflow_id, version + 1),),
    )
    if outcome["committed"]:
        return {"claimed": True, "reason": None, "attempt": attempt, "head_version": version}
    reason = {
        "already_committed": "already_claimed",
        "precondition_present": "head_moved",
        "precondition_missing": "head_moved",
    }.get(str(outcome["conflict"]), "head_moved")
    return {"claimed": False, "reason": reason, "attempt": attempt, "head_version": version}


__all__ = [
    "PHASES",
    "child_key",
    "claim_key",
    "claim_workflow_attempt",
    "completed_key",
    "funding_workflow_id",
    "head_key",
    "load_workflow_child",
    "publish_contribution_request",
    "superseded_key",
    "workflow_id_from_request",
]

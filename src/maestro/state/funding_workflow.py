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


def _count_published_requests(store: StateStore, workflow_id: str) -> int:
    """이 워크플로우 앞으로 실제로 커밋된 요청 이벤트 수.

    head row 개수가 아니라 이 값을 버전의 기준으로 삼는다: head는 요청
    이벤트 없이도 (예: 복구 스크립트, 손상, 또는 이 함수를 우회한 어떤
    경로에 의해) 존재할 수 있는 dangling row다. 그런 head의 version을
    그대로 믿고 +1 하면 요청 이벤트가 없는 슬롯을 뛰어넘어버려서, 그
    슬롯을 실제로 노리고 있던 다른 쓰기와의 충돌을 놓친다.
    """
    count = 0
    for event_type in _REQUEST_EVENT.values():
        for row in store.list_system_events_by_type(event_type, limit=None):
            if (row.get("payload") or {}).get("funding_workflow_id") == workflow_id:
                count += 1
    return count


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
    published = _count_published_requests(store, workflow_id)
    # A resubmission of the request already at head is the same transition,
    # not a new one: target the same version so the batch matches byte for
    # byte what already landed and the atomic call reports a plain replay
    # instead of a same-request partial overlap with itself.
    if head and previous_request_id == request_id:
        version = int(head.get("version") or published or 1)
    else:
        version = published + 1
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
    }


__all__ = [
    "PHASES",
    "child_key",
    "claim_key",
    "completed_key",
    "funding_workflow_id",
    "head_key",
    "publish_contribution_request",
    "superseded_key",
    "workflow_id_from_request",
]

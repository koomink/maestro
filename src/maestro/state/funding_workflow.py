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
from typing import Any

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


__all__ = [
    "PHASES",
    "child_key",
    "claim_key",
    "completed_key",
    "funding_workflow_id",
    "head_key",
    "superseded_key",
    "workflow_id_from_request",
]

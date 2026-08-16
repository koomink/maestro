# 3a-4: funding/budget 워크플로우 head·CAS 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** funding/budget 요청 교체를 영속 workflow head의 CAS로 직렬화하고, 전이를 `claimed → child_created → completed` 3단계로 영속화해 중단·중복 callback·병행 pending 요청이 월간 투자를 중복 실행하거나 조용히 멈추지 못하게 한다.

**Architecture:** 3a-1이 만든 `StateStore.save_system_events_atomic`(조건부 다중 이벤트 원자 커밋) 위에 얇은 워크플로우 계층을 얹는다. 새 모듈 `state/funding_workflow.py`가 workflow_id 파생·duplicate_key 규약·CAS 헬퍼를 소유하고, orchestrator(요청 생성)와 telegram handlers(요청 확인)는 이 헬퍼만 호출한다. 종결은 legacy 종결 이벤트와 같은 배치로 dual-write해 롤백 호환성을 유지한다.

**Tech Stack:** Python 3.11+, SQLite(`sqlite3`, WAL, `BEGIN IMMEDIATE`), pydantic v2, typer, pytest(+pytest-randomly), ruff

**Spec:** `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md` — 「B. 월간 자금 카드」(172~460행), 특히 항목 1~5·8. 마이그레이션 표는 698~708행.

## Global Constraints

- **접근 A의 명시적 예외**: 이 단계만 비즈니스 로직(handlers의 funding 확인 경로, orchestrator의 요청 영속화)을 바꾼다. 그 밖의 기존 handlers 테스트가 깨지면 범위를 벗어난 것이다.
- **roll-forward-only**: 이 단계 배포 후 롤백은 3a-5의 quiesce 장벽 + preflight 통과 뒤에만 허용된다.
- **`funding_workflow_id`는 해시가 아니라 전체 정규화 scope 문자열이다.** 짧은 해시 금지.
- **scope 직렬화**: `json.dumps([contribution_group_id, account_id, execution_sleeve, currency], ensure_ascii=False, separators=(",", ":"))`. null은 JSON `null` 그대로, 문자열은 **유니코드 정규화 금지**(원시 코드포인트 보존).
- **모든 워크플로우 상태 쓰기는 `save_system_events_atomic`을 통한다.** `save_system_event` 단건 호출로 head/claim/completed를 쓰지 않는다.
- **`completed`는 legacy 종결 이벤트와 같은 배치**: funding은 `contribution_funding_request_ack`, budget은 `contribution_budget_request_decision`.
- **duplicate_key는 내용을 식별해야 한다.** payload에 매 호출 달라지는 타임스탬프·난수를 넣으면 재시도가 `ValueError`로 죽는다(`store.py:1103-1120`). 워크플로우 payload에는 `decided_at` 같은 필드를 넣지 않는다.
- **자동 재실행 금지**: claim만 있고 `completed`가 없는 워크플로우를 코드가 스스로 재실행하는 경로는 없다. 재개는 운영자 [재개] 버튼 + attempt 증가로만.
- 테스트는 `pytest -q`, 린트는 `ruff check src tests`. `ruff format`은 이 저장소에서 강제하지 않는다.

---

## File Structure

**Create:**
- `src/maestro/state/funding_workflow.py` — workflow_id 파생, duplicate_key 규약, head 읽기/CAS 헬퍼, 전이 커밋 함수. 이 파일이 규약의 단일 원천이다.
- `tests/test_funding_workflow_keys.py` — 키 파생·충돌 테스트 (순수 함수)
- `tests/test_funding_workflow_head.py` — head 생성/교체/CAS 경쟁
- `tests/test_funding_workflow_transitions.py` — claim·child·completed·dual-write
- `tests/test_funding_workflow_resume.py` — 중단 재개, 복구 카드, 수렴 sweep

**Modify:**
- `src/maestro/state/store.py` — head 조회 API 2개 추가 (`load_funding_workflow_head`, `list_funding_workflow_heads`)
- `src/maestro/orchestration/orchestrator.py:672-682` — 요청 이벤트 기록을 head 원자 커밋으로 교체
- `src/maestro/orchestration/orchestrator.py:282-292` — `run_signal`에 lineage 파라미터 추가
- `src/maestro/integrations/telegram/handlers.py:3056-3158` — funding/budget callback에 claim 게이트
- `src/maestro/integrations/telegram/handlers.py:3288-3385` — 확인 경로를 전이 3단계로
- `src/maestro/integrations/telegram/handlers.py:3878-3949` — `_save_budget_decision` / `_save_funding_ack`을 dual-write로

---

### Task 1: workflow_id 파생과 키 규약

**Files:**
- Create: `src/maestro/state/funding_workflow.py`
- Test: `tests/test_funding_workflow_keys.py`

**Interfaces:**
- Consumes: 없음 (순수 함수)
- Produces:
  - `funding_workflow_id(*, contribution_group_id: str | None, account_id: str | None, execution_sleeve: str | None, currency: str | None, month_key: str) -> str`
  - `workflow_id_from_request(request: Mapping[str, Any]) -> str`
  - `head_key(workflow_id: str, version: int) -> str`
  - `claim_key(workflow_id: str, phase: str, request_id: str, attempt: int) -> str`
  - `child_key(request_id: str, phase: str) -> str`
  - `completed_key(workflow_id: str, request_id: str, phase: str) -> str`
  - `superseded_key(workflow_id: str, request_id: str) -> str`
  - `PHASES: tuple[str, str] = ("funding", "budget")`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_funding_workflow_keys.py
import pytest

from maestro.state.funding_workflow import (
    child_key,
    claim_key,
    completed_key,
    funding_workflow_id,
    head_key,
    superseded_key,
    workflow_id_from_request,
)


def _wid(**kwargs):
    base = {
        "contribution_group_id": "core",
        "account_id": "acct-1",
        "execution_sleeve": "krw",
        "currency": "KRW",
        "month_key": "2026-08",
    }
    base.update(kwargs)
    return funding_workflow_id(**base)


def test_workflow_id_keeps_the_whole_scope_not_a_hash():
    assert _wid() == 'funding:["core","acct-1","krw","KRW"]:2026-08'


def test_null_account_is_json_null_not_a_sentinel_string():
    assert _wid(account_id=None) != _wid(account_id="-")
    assert _wid(account_id=None) != _wid(account_id="null")


def test_separator_bearing_identifiers_do_not_collide():
    left = _wid(contribution_group_id='a","b', account_id=None)
    right = _wid(contribution_group_id="a", account_id="b")
    assert left != right


def test_unicode_equivalent_scopes_stay_distinct():
    # NFC "가" vs NFD "가" - normalizing here would let one supersede the other.
    assert _wid(contribution_group_id="가") != _wid(contribution_group_id="가")


def test_same_scope_in_a_different_month_is_a_different_workflow():
    assert _wid(month_key="2026-08") != _wid(month_key="2026-09")


def test_workflow_id_from_request_reads_the_scope_fields():
    request = {
        "contribution_group_id": "core",
        "account_id": "acct-1",
        "execution_sleeve": "krw",
        "currency": "KRW",
        "month_key": "2026-08",
    }
    assert workflow_id_from_request(request) == _wid()


def test_workflow_id_from_request_rejects_a_request_without_a_month():
    with pytest.raises(ValueError, match="month_key"):
        workflow_id_from_request({"account_id": "acct-1"})


def test_head_key_moves_with_the_version():
    assert head_key("funding:x:2026-08", 1) == "head:funding:x:2026-08:v1"
    assert head_key("funding:x:2026-08", 2) != head_key("funding:x:2026-08", 1)


def test_claim_key_carries_the_attempt():
    assert claim_key("wf", "funding", "req-1", 1) == "wf:funding:req-1:a1"
    assert claim_key("wf", "funding", "req-1", 2) != claim_key("wf", "funding", "req-1", 1)


def test_child_completed_and_superseded_keys_are_namespaced():
    assert child_key("req-1", "funding") == "child:req-1:funding"
    assert completed_key("wf", "req-1", "funding") == "wf-completed:wf:req-1:funding"
    assert superseded_key("wf", "req-1") == "wf-superseded:wf:req-1"
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_keys.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.state.funding_workflow'`

- [ ] **Step 3: 최소 구현**

```python
# src/maestro/state/funding_workflow.py
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
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_keys.py -q`
Expected: PASS (10 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/funding_workflow.py tests/test_funding_workflow_keys.py
git commit -m "feat(3a-4): funding workflow id and duplicate key conventions"
```

---

### Task 2: head 조회 API

**Files:**
- Modify: `src/maestro/state/store.py` (`list_system_events_by_type` 근처, 2233행 뒤)
- Test: `tests/test_funding_workflow_head.py`

**Interfaces:**
- Consumes: Task 1의 `head_key`
- Produces:
  - `StateStore.load_funding_workflow_head(workflow_id: str) -> dict[str, Any] | None` — 가장 높은 `version`의 head payload. payload 키: `workflow_id`, `version`(int), `request_id`(str|None), `status`(str), `scope`(list)
  - `StateStore.list_funding_workflow_heads() -> list[dict[str, Any]]` — workflow_id별 최신 head payload 목록

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_funding_workflow_head.py
from maestro.state.funding_workflow import head_key
from maestro.state.store import StateStore


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def _write_head(store, workflow_id, version, request_id, status="pending"):
    store.save_system_events_atomic(
        "run-1",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": head_key(workflow_id, version),
                    "workflow_id": workflow_id,
                    "version": version,
                    "request_id": request_id,
                    "status": status,
                    "scope": ["core", "acct-1", "krw", "KRW"],
                },
            }
        ],
    )


def test_no_head_yet_reads_as_none(tmp_path):
    store = _store(tmp_path)
    assert store.load_funding_workflow_head("funding:x:2026-08") is None


def test_the_highest_version_is_the_head(tmp_path):
    store = _store(tmp_path)
    _write_head(store, "wf-a", 1, "req-1")
    _write_head(store, "wf-a", 2, "req-2")
    head = store.load_funding_workflow_head("wf-a")
    assert head["version"] == 2
    assert head["request_id"] == "req-2"


def test_heads_of_other_workflows_are_not_visible(tmp_path):
    store = _store(tmp_path)
    _write_head(store, "wf-a", 1, "req-1")
    _write_head(store, "wf-b", 1, "req-2")
    assert store.load_funding_workflow_head("wf-a")["request_id"] == "req-1"
    assert store.load_funding_workflow_head("wf-b")["request_id"] == "req-2"


def test_listing_gives_one_row_per_workflow(tmp_path):
    store = _store(tmp_path)
    _write_head(store, "wf-a", 1, "req-1")
    _write_head(store, "wf-a", 2, "req-2")
    _write_head(store, "wf-b", 1, "req-3")
    heads = {row["workflow_id"]: row for row in store.list_funding_workflow_heads()}
    assert heads["wf-a"]["version"] == 2
    assert heads["wf-b"]["version"] == 1
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_head.py -q`
Expected: FAIL — `AttributeError: 'StateStore' object has no attribute 'load_funding_workflow_head'`

- [ ] **Step 3: 최소 구현**

`src/maestro/state/store.py`, `list_system_events_by_type` 정의 뒤에 추가:

```python
    def load_funding_workflow_head(self, workflow_id: str) -> dict[str, Any] | None:
        """이 워크플로우의 현재 활성 요청. 없으면 None.

        head는 버전마다 새 행이므로 "최신"은 id가 아니라 version으로
        판정한다 — 다른 워크플로우의 head가 사이에 끼어들어도, 백필이
        옛 버전을 나중에 써도 결과가 달라지지 않는다.
        """
        best: dict[str, Any] | None = None
        for row in self.list_system_events_by_type("funding_workflow_head", limit=None):
            payload = row.get("payload") or {}
            if payload.get("workflow_id") != workflow_id:
                continue
            if best is None or int(payload.get("version") or 0) > int(best.get("version") or 0):
                best = dict(payload)
        return best

    def list_funding_workflow_heads(self) -> list[dict[str, Any]]:
        """워크플로우별 최신 head 목록. 수렴 sweep과 backfill이 쓴다."""
        latest: dict[str, dict[str, Any]] = {}
        for row in self.list_system_events_by_type("funding_workflow_head", limit=None):
            payload = row.get("payload") or {}
            workflow_id = payload.get("workflow_id")
            if not workflow_id:
                continue
            current = latest.get(str(workflow_id))
            if current is None or int(payload.get("version") or 0) > int(
                current.get("version") or 0
            ):
                latest[str(workflow_id)] = dict(payload)
        return list(latest.values())
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_head.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/store.py tests/test_funding_workflow_head.py
git commit -m "feat(3a-4): read the funding workflow head from the event log"
```

---

### Task 3: 요청 생성·교체를 head CAS 한 트랜잭션으로

**Files:**
- Modify: `src/maestro/state/funding_workflow.py` (`publish_contribution_request` 추가)
- Test: `tests/test_funding_workflow_head.py` (이어서)

**Interfaces:**
- Consumes: Task 1 키 함수, Task 2 `load_funding_workflow_head`, `StateStore.save_system_events_atomic`
- Produces:
  - `publish_contribution_request(store: StateStore, run_id: str, request: Mapping[str, Any], *, phase: str) -> dict[str, Any]` — 반환 dict: `{"committed": bool, "conflict": str | None, "workflow_id": str, "version": int}`

**설계 메모:** 신규 head 키를 `forbid_duplicate_keys`로도 선언한다. 같은 버전을 노린 경쟁 커밋이 이미 있으면 배치의 own-key partial overlap이 되어 `ValueError`로 죽는 대신 `precondition_present`로 보고된다 — CAS 패배는 예외가 아니라 결과값이다(`store.py:1142-1152`).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_funding_workflow_head.py 에 이어서
import pytest

from maestro.state.funding_workflow import publish_contribution_request


def _request(request_id, month_key="2026-08"):
    return {
        "request_id": request_id,
        "contribution_group_id": "core",
        "account_id": "acct-1",
        "execution_sleeve": "krw",
        "currency": "KRW",
        "month_key": month_key,
        "status": "pending",
        "strategy_ids": ["s1"],
    }


def test_the_first_request_becomes_head_v1(tmp_path):
    store = _store(tmp_path)
    result = publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    assert result["committed"] is True
    assert result["version"] == 1
    head = store.load_funding_workflow_head(result["workflow_id"])
    assert head["request_id"] == "req-1"


def test_a_replacement_request_supersedes_the_previous_one_atomically(tmp_path):
    store = _store(tmp_path)
    first = publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    second = publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")
    assert second["committed"] is True
    assert second["version"] == 2
    head = store.load_funding_workflow_head(first["workflow_id"])
    assert head["request_id"] == "req-2"
    superseded = [
        row["payload"]
        for row in store.list_system_events_by_type("funding_workflow_superseded", limit=None)
    ]
    assert [row["request_id"] for row in superseded] == ["req-1"]


def test_the_request_event_and_the_head_land_together_or_not_at_all(tmp_path):
    store = _store(tmp_path)
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    requests = store.list_system_events_by_type("contribution_funding_request", limit=None)
    heads = store.list_system_events_by_type("funding_workflow_head", limit=None)
    assert len(requests) == 1
    assert len(heads) == 1


def test_losing_the_race_for_a_version_is_reported_not_raised(tmp_path):
    store = _store(tmp_path)
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    # Another writer already took v2 while we were preparing our own v2.
    _write_head(store, workflow_id, 2, "req-other")
    result = publish_contribution_request(store, "run-3", _request("req-3"), phase="funding")
    assert result["committed"] is False
    assert result["conflict"] == "precondition_present"


def test_republishing_the_same_request_is_a_replay_not_a_new_version(tmp_path):
    store = _store(tmp_path)
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    again = publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    assert again["committed"] is False
    assert again["conflict"] == "already_committed"


def test_different_scopes_in_the_same_month_do_not_supersede_each_other(tmp_path):
    store = _store(tmp_path)
    left = dict(_request("req-1"), execution_sleeve="krw")
    right = dict(_request("req-2"), execution_sleeve="usd")
    a = publish_contribution_request(store, "run-1", left, phase="funding")
    b = publish_contribution_request(store, "run-1", right, phase="funding")
    assert a["workflow_id"] != b["workflow_id"]
    assert store.load_funding_workflow_head(a["workflow_id"])["request_id"] == "req-1"
    assert store.load_funding_workflow_head(b["workflow_id"])["request_id"] == "req-2"


def test_a_budget_request_uses_the_budget_event_type(tmp_path):
    store = _store(tmp_path)
    publish_contribution_request(store, "run-1", _request("req-1"), phase="budget")
    assert len(store.list_system_events_by_type("contribution_budget_request", limit=None)) == 1


def test_an_unknown_phase_is_refused(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="phase"):
        publish_contribution_request(store, "run-1", _request("req-1"), phase="rebate")
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_head.py -q`
Expected: FAIL — `ImportError: cannot import name 'publish_contribution_request'`

- [ ] **Step 3: 최소 구현**

`src/maestro/state/funding_workflow.py` 에 추가 (`TYPE_CHECKING`으로 순환 import 회피):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.state.store import StateStore

_REQUEST_EVENT = {
    "funding": "contribution_funding_request",
    "budget": "contribution_budget_request",
}


def _require_phase(phase: str) -> str:
    if phase not in PHASES:
        raise ValueError(f"unknown funding workflow phase: {phase}")
    return phase


def publish_contribution_request(
    store: "StateStore",
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
    version = int(head.get("version") or 0) + 1 if head else 1
    payload = dict(request)
    payload["funding_workflow_id"] = workflow_id
    payload["duplicate_key"] = f"{_REQUEST_EVENT[phase]}:{request_id}"

    events: list[dict[str, Any]] = [
        {"event_type": _REQUEST_EVENT[phase], "payload": payload}
    ]
    previous_request_id = str(head.get("request_id") or "") if head else ""
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
```

`__all__`에 `publish_contribution_request` 추가.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_head.py -q`
Expected: PASS (12 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/funding_workflow.py tests/test_funding_workflow_head.py
git commit -m "feat(3a-4): commit request, supersede and head as one transition"
```

---

### Task 4: orchestrator가 요청을 head와 함께 발행하도록 교체

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py:671-682`
- Test: `tests/test_funding_workflow_head.py` (이어서)

**Interfaces:**
- Consumes: Task 3 `publish_contribution_request`
- Produces: `_run_signal_locked`가 남기는 `contribution_funding_request` / `contribution_budget_request` 이벤트에 `funding_workflow_id` 필드가 붙고, 대응하는 `funding_workflow_head` v1이 항상 함께 존재한다.

- [ ] **Step 1: 실패하는 테스트 작성**

기존 시그널 픽스처를 재사용한다. `tests/test_contribution_funding_requests.py`의 헬퍼를 참고해 아래를 `tests/test_funding_workflow_head.py`에 추가:

```python
def test_a_signal_run_that_asks_for_funding_also_publishes_a_head(tmp_path):
    from tests.test_contribution_funding_requests import build_funding_orchestrator

    orchestrator = build_funding_orchestrator(tmp_path)
    summary = orchestrator.run_signal()
    assert summary.funding_requests_count == 1
    store = orchestrator.state_store
    request = store.list_system_events_by_type("contribution_funding_request", limit=None)[0]
    workflow_id = request["payload"]["funding_workflow_id"]
    head = store.load_funding_workflow_head(workflow_id)
    assert head["request_id"] == request["payload"]["request_id"]
    assert head["version"] == 1
```

`build_funding_orchestrator`가 없으면 그 파일의 기존 셋업 코드를 그 이름의 모듈 수준 함수로 추출하고, 기존 테스트가 그 함수를 쓰도록 바꾼다(동작 변경 없음).

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_head.py -q -k publishes_a_head`
Expected: FAIL — `KeyError: 'funding_workflow_id'`

- [ ] **Step 3: 최소 구현**

`orchestrator.py` 671-682행의 두 루프를 교체:

```python
        for request in funding_requests:
            self._publish_contribution_request(signal_run_id, request, phase="funding")
        for request in budget_requests:
            self._publish_contribution_request(signal_run_id, request, phase="budget")
```

같은 클래스에 추가:

```python
    def _publish_contribution_request(
        self,
        signal_run_id: str,
        request: Any,
        *,
        phase: str,
    ) -> None:
        """요청과 워크플로우 head를 한 트랜잭션으로 남긴다.

        CAS 패배(precondition_present)는 다른 run이 같은 scope/month의
        요청을 먼저 세웠다는 뜻이다. 이 run의 요청은 head가 되지 못하므로
        저장하지 않고 audit에만 남긴다 — 저장하면 head 없는 orphan이 된다.
        """
        payload = request.model_dump(mode="json")
        outcome = publish_contribution_request(
            self.state_store, signal_run_id, payload, phase=phase
        )
        if outcome["committed"]:
            self.audit.log(signal_run_id, f"contribution_{phase}_request", payload)
            return
        self.audit.log(
            signal_run_id,
            "funding_workflow_head_conflict",
            {
                "signal_run_id": signal_run_id,
                "workflow_id": outcome["workflow_id"],
                "request_id": payload.get("request_id"),
                "phase": phase,
                "conflict": outcome["conflict"],
            },
        )
```

import 추가: `from maestro.state.funding_workflow import publish_contribution_request`

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_head.py tests/test_contribution_funding_requests.py tests/test_contribution_budget_requests.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/orchestration/orchestrator.py tests/test_funding_workflow_head.py tests/test_contribution_funding_requests.py
git commit -m "feat(3a-4): publish contribution requests through the workflow head"
```

---

### Task 5: head precondition을 건 attempt claim

**Files:**
- Modify: `src/maestro/state/funding_workflow.py`
- Test: `tests/test_funding_workflow_transitions.py`

**Interfaces:**
- Consumes: Task 1~3
- Produces:
  - `claim_workflow_attempt(store: StateStore, run_id: str, *, workflow_id: str, request_id: str, phase: str, attempt: int = 1) -> dict[str, Any]` — 반환: `{"claimed": bool, "reason": str | None, "attempt": int, "head_version": int}`. `reason`은 `None` | `"no_head"` | `"not_head"` | `"head_moved"` | `"already_claimed"`.

**설계 메모:** claim은 head 조회 후 별도 쓰기가 아니라 **한 트랜잭션 안의 조건부 삽입**이다. `require_duplicate_keys=(head_key(wf, v),)`로 "그 버전이 존재함"을, `forbid_duplicate_keys=(head_key(wf, v + 1),)`로 "그 사이 head가 움직이지 않았음"을 함께 건다. 존재 검사만으로는 head가 단조 증가하므로 교체를 감지하지 못한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_funding_workflow_transitions.py
from maestro.state.funding_workflow import (
    claim_workflow_attempt,
    head_key,
    publish_contribution_request,
)
from maestro.state.store import StateStore


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def _request(request_id):
    return {
        "request_id": request_id,
        "contribution_group_id": "core",
        "account_id": "acct-1",
        "execution_sleeve": "krw",
        "currency": "KRW",
        "month_key": "2026-08",
        "status": "pending",
        "strategy_ids": ["s1"],
    }


def _published(store, request_id):
    return publish_contribution_request(
        store, "run-1", _request(request_id), phase="funding"
    )["workflow_id"]


def test_the_head_request_can_claim(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    result = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert result["claimed"] is True
    assert result["attempt"] == 1


def test_a_second_callback_for_the_same_attempt_is_refused(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    again = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert again["claimed"] is False
    assert again["reason"] == "already_claimed"


def test_a_superseded_request_cannot_claim(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")
    result = claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert result["claimed"] is False
    assert result["reason"] == "not_head"


def test_a_head_that_moves_between_read_and_write_loses_the_claim(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    # Simulate the TOCTOU: v2 lands after we read v1 as current.
    store.save_system_events_atomic(
        "run-9",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": head_key(workflow_id, 2),
                    "workflow_id": workflow_id,
                    "version": 2,
                    "request_id": "req-2",
                    "status": "pending",
                },
            }
        ],
    )
    result = claim_workflow_attempt(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        expected_version=1,
    )
    assert result["claimed"] is False
    assert result["reason"] == "head_moved"


def test_a_workflow_with_no_head_cannot_claim(tmp_path):
    store = _store(tmp_path)
    result = claim_workflow_attempt(
        store, "run-1", workflow_id="funding:x:2026-08", request_id="req-1", phase="funding"
    )
    assert result["claimed"] is False
    assert result["reason"] == "no_head"


def test_a_later_attempt_can_claim_after_the_first_one_stalled(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    resumed = claim_workflow_attempt(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=2,
    )
    assert resumed["claimed"] is True
    assert resumed["attempt"] == 2


def test_two_threads_racing_the_same_attempt_yield_exactly_one_claim(tmp_path):
    import threading

    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    results: list[bool] = []
    barrier = threading.Barrier(2)

    def claim() -> None:
        barrier.wait()
        results.append(
            claim_workflow_attempt(
                store,
                "run-1",
                workflow_id=workflow_id,
                request_id="req-1",
                phase="funding",
            )["claimed"]
        )

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, True]
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_transitions.py -q`
Expected: FAIL — `ImportError: cannot import name 'claim_workflow_attempt'`

- [ ] **Step 3: 최소 구현**

```python
def claim_workflow_attempt(
    store: "StateStore",
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
```

`__all__`에 `claim_workflow_attempt` 추가.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_transitions.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/funding_workflow.py tests/test_funding_workflow_transitions.py
git commit -m "feat(3a-4): claim a workflow attempt under a head CAS precondition"
```

---

### Task 6: child run lineage — get-or-create

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py:282-292` 및 `_run_signal_locked` 진입부
- Modify: `src/maestro/state/funding_workflow.py`
- Test: `tests/test_funding_workflow_transitions.py` (이어서)

**Interfaces:**
- Consumes: Task 1 `child_key`
- Produces:
  - `MaestroOrchestrator.run_signal(strategy_ids=None, *, contribution_override=False, source_request_id: str | None = None, source_workflow_id: str | None = None, source_phase: str | None = None) -> SignalRunSummary`
  - `load_workflow_child(store: StateStore, request_id: str, phase: str) -> str | None` — 이미 만들어진 child signal_run_id

**설계 메모:** attempt fencing은 이벤트 커밋을 거부할 뿐 `run_signal()`의 부작용(신규 package·승인 흐름)을 막지 못한다. 그래서 lineage 재조회와 child 생성을 **writer lock 안에서** 하고, child 기록을 `child:<request_id>:<phase>` 유일 키로 커밋해 DB 수준에서 최대 1개를 보장한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_funding_workflow_transitions.py 에 이어서
from maestro.state.funding_workflow import load_workflow_child


def test_a_signal_run_without_a_source_records_no_lineage(tmp_path):
    from tests.test_contribution_funding_requests import build_funding_orchestrator

    orchestrator = build_funding_orchestrator(tmp_path)
    orchestrator.run_signal()
    assert load_workflow_child(orchestrator.state_store, "req-1", "funding") is None


def test_the_same_source_request_never_creates_two_children(tmp_path):
    from tests.test_contribution_funding_requests import build_funding_orchestrator

    orchestrator = build_funding_orchestrator(tmp_path)
    first = orchestrator.run_signal(
        source_request_id="req-1", source_workflow_id="wf-a", source_phase="funding"
    )
    second = orchestrator.run_signal(
        source_request_id="req-1", source_workflow_id="wf-a", source_phase="funding"
    )
    assert second.signal_run_id == first.signal_run_id
    store = orchestrator.state_store
    assert load_workflow_child(store, "req-1", "funding") == first.signal_run_id
    packages = store.list_system_events_by_type("signal_package", limit=None)
    assert len(packages) == 1


def test_a_different_source_request_gets_its_own_child(tmp_path):
    from tests.test_contribution_funding_requests import build_funding_orchestrator

    orchestrator = build_funding_orchestrator(tmp_path)
    first = orchestrator.run_signal(
        source_request_id="req-1", source_workflow_id="wf-a", source_phase="funding"
    )
    second = orchestrator.run_signal(
        source_request_id="req-2", source_workflow_id="wf-a", source_phase="funding"
    )
    assert second.signal_run_id != first.signal_run_id
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_transitions.py -q -k lineage or child`
Expected: FAIL — `TypeError: run_signal() got an unexpected keyword argument 'source_request_id'`

- [ ] **Step 3: 최소 구현**

`funding_workflow.py`:

```python
def load_workflow_child(store: "StateStore", request_id: str, phase: str) -> str | None:
    """이 원천 요청이 이미 만든 child signal run. 추론적 연결은 하지 않는다."""
    key = child_key(request_id, phase)
    for row in store.list_system_events_by_type("funding_workflow_child_created", limit=None):
        payload = row.get("payload") or {}
        if payload.get("duplicate_key") == key:
            return str(payload.get("signal_run_id") or "") or None
    return None
```

`orchestrator.py`:

```python
    def run_signal(
        self,
        strategy_ids: list[str] | None = None,
        *,
        contribution_override: bool = False,
        source_request_id: str | None = None,
        source_workflow_id: str | None = None,
        source_phase: str | None = None,
    ) -> SignalRunSummary:
        with self.state_store.writer_lock("run_signal"):
            # lineage 조회와 child 생성은 같은 lock 경계 안에서만 의미가 있다.
            # lock 밖에서 "child 없음"을 관찰하면 지연된 이전 attempt와
            # 재개 attempt가 둘 다 없다고 보고 각자 package를 만든다.
            if source_request_id is not None:
                existing = load_workflow_child(
                    self.state_store, source_request_id, source_phase or "funding"
                )
                if existing is not None:
                    return self._reload_signal_summary(existing)
            summary = self._run_signal_locked(
                strategy_ids=strategy_ids,
                contribution_override=contribution_override,
            )
            if source_request_id is not None:
                self.state_store.save_system_events_atomic(
                    summary.signal_run_id,
                    [
                        {
                            "event_type": "funding_workflow_child_created",
                            "payload": {
                                "duplicate_key": child_key(
                                    source_request_id, source_phase or "funding"
                                ),
                                "workflow_id": source_workflow_id,
                                "request_id": source_request_id,
                                "phase": source_phase or "funding",
                                "signal_run_id": summary.signal_run_id,
                            },
                        }
                    ],
                )
            return summary
```

`_reload_signal_summary`는 저장된 package에서 요약을 복원한다:

```python
    def _reload_signal_summary(self, signal_run_id: str) -> SignalRunSummary:
        """이미 만들어진 child를 요약으로 되돌린다. 새 package를 만들지 않는다."""
        package = self.state_store.load_signal_package(signal_run_id) or {}
        return SignalRunSummary(
            signal_run_id=signal_run_id,
            status=str(package.get("status") or "completed"),
            orders_preview_count=int(package.get("orders_preview_count") or 0),
            funding_requests_count=int(package.get("funding_requests_count") or 0),
            budget_requests_count=int(package.get("budget_requests_count") or 0),
            action_required=bool(package.get("action_required")),
            no_order_reasons=list(package.get("no_order_reasons") or []),
        )
```

`SignalRunSummary`의 실제 필드 목록은 `orchestration/models.py`(또는 정의 위치)에서 확인해 그대로 맞춘다 — 필드가 더 있으면 package에서 같은 이름으로 읽어 채운다.

import 추가: `from maestro.state.funding_workflow import child_key, load_workflow_child, publish_contribution_request`

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_transitions.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/orchestration/orchestrator.py src/maestro/state/funding_workflow.py tests/test_funding_workflow_transitions.py
git commit -m "feat(3a-4): get-or-create the child signal run from its source request"
```

---

### Task 7: completed + legacy 종결 이벤트 dual-write

**Files:**
- Modify: `src/maestro/state/funding_workflow.py`
- Modify: `src/maestro/integrations/telegram/handlers.py:3878-3949`
- Test: `tests/test_funding_workflow_transitions.py` (이어서)

**Interfaces:**
- Consumes: Task 1 `completed_key`
- Produces:
  - `complete_workflow(store: StateStore, run_id: str, *, workflow_id: str, request_id: str, phase: str, attempt: int, legacy_payload: Mapping[str, Any]) -> dict[str, Any]` — 반환 `{"committed": bool, "conflict": str | None}`. legacy 이벤트 타입은 phase로 결정된다.

**설계 메모:** 구버전 `_load_pending_funding_request()`는 `contribution_funding_request_ack`만으로 종결을 판정한다. `completed`가 legacy ack를 **대체**하면 롤백 후 구버전이 완료된 요청을 pending으로 오판해 `run_signal()`과 현금흐름 처리를 재실행한다. 그래서 두 이벤트는 반드시 같은 배치다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_funding_workflow_transitions.py 에 이어서
from maestro.state.funding_workflow import complete_workflow


def test_completing_a_funding_workflow_also_writes_the_legacy_ack(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    result = complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed", "decided_by": "op"},
    )
    assert result["committed"] is True
    acks = store.list_system_events_by_type("contribution_funding_request_ack", limit=None)
    assert [row["payload"]["request_id"] for row in acks] == ["req-1"]


def test_the_completed_event_and_the_legacy_ack_land_together(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "confirmed", "decided_by": "op"},
    )
    completed = store.list_system_events_by_type("funding_workflow_completed", limit=None)
    acks = store.list_system_events_by_type("contribution_funding_request_ack", limit=None)
    assert len(completed) == len(acks) == 1


def test_a_budget_workflow_dual_writes_the_decision_event(tmp_path):
    store = _store(tmp_path)
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="budget"
    )["workflow_id"]
    complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="budget",
        attempt=1,
        legacy_payload={"request_id": "req-1", "status": "selected", "selected_budget": 500000.0},
    )
    decisions = store.list_system_events_by_type(
        "contribution_budget_request_decision", limit=None
    )
    assert decisions[0]["payload"]["selected_budget"] == 500000.0


def test_completing_twice_is_an_idempotent_replay(tmp_path):
    store = _store(tmp_path)
    workflow_id = _published(store, "req-1")
    payload = {"request_id": "req-1", "status": "confirmed", "decided_by": "op"}
    complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload=payload,
    )
    again = complete_workflow(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        legacy_payload=payload,
    )
    assert again["committed"] is False
    assert again["conflict"] == "already_committed"
    assert len(store.list_system_events_by_type("contribution_funding_request_ack", limit=None)) == 1
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_transitions.py -q -k complete`
Expected: FAIL — `ImportError: cannot import name 'complete_workflow'`

- [ ] **Step 3: 최소 구현**

```python
LEGACY_TERMINAL_EVENT = {
    "funding": "contribution_funding_request_ack",
    "budget": "contribution_budget_request_decision",
}
_LEGACY_TERMINAL_KEY_PREFIX = {
    "funding": "funding-ack",
    "budget": "budget-decision",
}


def complete_workflow(
    store: "StateStore",
    run_id: str,
    *,
    workflow_id: str,
    request_id: str,
    phase: str,
    attempt: int,
    legacy_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """전이를 종결하고, 같은 배치로 legacy 종결 이벤트를 남긴다.

    구버전 handler는 legacy 이벤트만 읽는다. completed가 그것을 대체하면
    롤백 후 구버전이 완료된 요청을 pending으로 오판해 run_signal과
    현금흐름 처리를 재실행한다. 그래서 두 이벤트는 한 트랜잭션이다.
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
```

`__all__`에 `complete_workflow`, `load_workflow_child` 추가.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_transitions.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/funding_workflow.py tests/test_funding_workflow_transitions.py
git commit -m "feat(3a-4): dual-write the terminal transition with its legacy event"
```

---

### Task 8: funding 확인 경로를 전이 3단계로

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py:3056-3116` (`_process_funding_callback`), `:3288-3385` (`_confirm_funding_request`), `:3927-3949` (`_save_funding_ack`)
- Test: `tests/test_funding_workflow_resume.py`

**Interfaces:**
- Consumes: Task 5 `claim_workflow_attempt`, Task 6 `run_signal(source_request_id=...)`, Task 7 `complete_workflow`
- Produces: `_confirm_funding_request(request, *, chat_id, user_id, username, attempt: int = 1) -> str` — claim 실패 시 `WorkflowClaimRefused`를 던진다.
  - `class WorkflowClaimRefused(RuntimeError)` in `maestro/state/funding_workflow.py`, 속성 `reason: str`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_funding_workflow_resume.py
import pytest

from maestro.state.funding_workflow import (
    WorkflowClaimRefused,
    load_workflow_child,
    publish_contribution_request,
)


def _request(request_id):
    return {
        "request_id": request_id,
        "contribution_group_id": "core",
        "account_id": "acct-1",
        "execution_sleeve": "krw",
        "currency": "KRW",
        "month_key": "2026-08",
        "status": "pending",
        "strategy_ids": ["s1"],
    }


def test_a_confirmed_funding_request_records_claim_child_and_completed(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )
    types = [
        row["event_type"]
        for row in store.list_system_events(limit=100)
        if row["event_type"].startswith("funding_workflow_")
    ]
    assert "funding_workflow_claim" in types
    assert "funding_workflow_child_created" in types
    assert "funding_workflow_completed" in types


def test_a_duplicate_callback_is_refused_before_any_side_effect(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )
    with pytest.raises(WorkflowClaimRefused):
        operator_bot._confirm_funding_request(
            _request("req-1"), chat_id=1, user_id=2, username="op"
        )
    assert len(store.list_system_events_by_type("signal_package", limit=None)) == 1


def test_a_superseded_request_callback_is_refused(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")
    with pytest.raises(WorkflowClaimRefused) as excinfo:
        operator_bot._confirm_funding_request(
            _request("req-1"), chat_id=1, user_id=2, username="op"
        )
    assert excinfo.value.reason == "not_head"


def test_a_crash_after_the_child_run_resumes_without_a_second_child(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    # attempt 1 gets as far as the child run, then the process dies.
    operator_bot._run_child_signal(_request("req-1"), workflow_id_of(store, "req-1"), attempt=1)
    child = load_workflow_child(store, "req-1", "funding")
    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op", attempt=2
    )
    assert load_workflow_child(store, "req-1", "funding") == child


def test_the_legacy_ack_is_written_so_a_rollback_sees_the_request_as_done(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )
    assert operator_bot._load_pending_funding_request("req-1") is None


def test_the_cash_flow_record_is_not_duplicated_on_resume(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    operator_bot._record_account_cash_flow_from_funding_request(
        _request("req-1"), user_id=2, username="op"
    )
    operator_bot._record_account_cash_flow_from_funding_request(
        _request("req-1"), user_id=2, username="op"
    )
    flows = store.list_system_events_by_type("account_cash_flow", limit=None)
    assert len({row["payload"].get("proposal_id") for row in flows}) == len(flows)
```

`operator_bot` 픽스처와 `workflow_id_of` 헬퍼는 `tests/test_telegram_approval_resume.py`가 쓰는 봇 셋업을 그대로 재사용한다(`conftest.py`로 추출하거나 그 파일에서 import).

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_resume.py -q`
Expected: FAIL — `ImportError: cannot import name 'WorkflowClaimRefused'`

- [ ] **Step 3: 최소 구현**

`funding_workflow.py`:

```python
class WorkflowClaimRefused(RuntimeError):
    """이 요청은 이 전이에 진입할 수 없다 — 이미 처리 중이거나 교체됐다."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"funding workflow claim refused: {reason}")
        self.reason = reason
```

`handlers.py` — `_confirm_funding_request` 앞부분을 claim으로 감싸고, `run_signal` 호출에 lineage를 넘기고, ack 저장을 `complete_workflow`로 바꾼다:

```python
    def _confirm_funding_request(
        self,
        request: dict[str, Any],
        *,
        chat_id: int,
        user_id: int,
        username: str | None,
        attempt: int = 1,
    ) -> str:
        if self.signal_config_path is None:
            raise ValueError("Funding confirmation requires telegram-operator --signal-config")
        workflow_id = workflow_id_from_request(request)
        request_id = str(request["request_id"])
        claim = claim_workflow_attempt(
            self.store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="funding",
            attempt=attempt,
        )
        if not claim["claimed"]:
            # 상태를 바꾸기 전에 막는다. 동시 중복 callback과 head 교체
            # 경쟁을 모두 부작용 이전에 차단하는 것이 claim의 목적이다.
            raise WorkflowClaimRefused(str(claim["reason"]))
        try:
            self._refresh_portfolio_from_broker_snapshot()
        except (RuntimeError, TimeoutError, ValueError):
            if self._has_readonly_broker_accounts():
                raise
        signal_config, signal_identity = load_config_with_identity(self.signal_config_path)
        strategy_ids = [str(item) for item in request.get("strategy_ids") or []]
        if not strategy_ids:
            raise ValueError("Funding request is missing strategy_ids")
        self._record_account_cash_flow_from_funding_request(
            request, user_id=user_id, username=username
        )
        self._record_strategy_cash_flow_from_funding_request(
            request, strategy_ids=strategy_ids, user_id=user_id, username=username
        )
        signal_summary = self._run_child_signal(request, workflow_id, attempt=attempt)
        ...
```

`_run_child_signal`을 추출:

```python
    def _run_child_signal(
        self,
        request: Mapping[str, Any],
        workflow_id: str,
        *,
        attempt: int,
    ) -> SignalRunSummary:
        """이 요청의 child run을 만들거나, 이미 있으면 그것을 돌려준다."""
        signal_config, signal_identity = load_config_with_identity(self.signal_config_path)
        return MaestroOrchestrator(
            signal_config,
            config_identity=signal_identity,
        ).run_signal(
            strategy_ids=[str(item) for item in request.get("strategy_ids") or []],
            contribution_override=True,
            source_request_id=str(request["request_id"]),
            source_workflow_id=workflow_id,
            source_phase="funding",
        )
```

`_save_funding_ack` 호출을 교체:

```python
        complete_workflow(
            self.store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="funding",
            attempt=attempt,
            legacy_payload={
                "request_id": request_id,
                "status": "confirmed",
                "decided_by": username or str(user_id),
                "new_signal_run_id": signal_summary.signal_run_id,
            },
        )
```

`_process_funding_callback`의 `except (RuntimeError, TimeoutError, TypeError, ValueError)` 앞에 전용 분기를 넣는다 (`WorkflowClaimRefused`는 `RuntimeError` 하위이므로 **먼저** 잡아야 한다):

```python
        except WorkflowClaimRefused:
            self._answer(callback, "이미 처리 중이거나 완료된 요청이에요.")
            self._record("/funding_complete", chat_id, user_id, username, "claim_refused")
            return True
```

취소 경로도 `complete_workflow(..., legacy_payload={"status": "canceled", ...})`로 바꾼다.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_resume.py tests/test_telegram_operator*.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/funding_workflow.py src/maestro/integrations/telegram/handlers.py tests/test_funding_workflow_resume.py
git commit -m "feat(3a-4): route funding confirmation through claim, child and completion"
```

---

### Task 9: budget 확인 경로에 같은 전이 적용

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py:3110-3158` (`_process_budget_callback`), `:3194-3260` (`_confirm_budget_request`), `:3878-3906` (`_save_budget_decision`)
- Test: `tests/test_funding_workflow_resume.py` (이어서)

**Interfaces:**
- Consumes: Task 5·6·7과 동일
- Produces: `_confirm_budget_request(request, *, selected_budget, chat_id, user_id, username, attempt: int = 1) -> str`

**설계 메모:** 현재 코드는 `contribution_budget_request_decision` 저장 즉시 요청을 pending에서 제외하므로, decision 직후 config load나 `run_signal()`이 실패하면 child run 없이 요청이 종결돼 월간 투자가 조용히 멈춘다. 개편 후 decision은 전이의 **입력값**일 뿐이며 `completed`가 없으면 미완이다. 그래서 decision 이벤트는 `complete_workflow`의 legacy dual-write 안에서만 쓰이고, 단독으로 먼저 저장하지 않는다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_funding_workflow_resume.py 에 이어서
def test_a_budget_decision_alone_does_not_close_the_workflow(operator_bot, monkeypatch):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="budget")

    def boom(*args, **kwargs):
        raise RuntimeError("config load failed")

    monkeypatch.setattr(operator_bot, "_run_child_signal", boom)
    with pytest.raises(RuntimeError, match="config load failed"):
        operator_bot._confirm_budget_request(
            _request("req-1"), selected_budget=500000.0, chat_id=1, user_id=2, username="op"
        )
    # The workflow must still be recoverable: no terminal event was written.
    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    assert (
        store.list_system_events_by_type("contribution_budget_request_decision", limit=None) == []
    )


def test_a_completed_budget_workflow_writes_the_legacy_decision(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="budget")
    operator_bot._confirm_budget_request(
        _request("req-1"), selected_budget=500000.0, chat_id=1, user_id=2, username="op"
    )
    decisions = store.list_system_events_by_type(
        "contribution_budget_request_decision", limit=None
    )
    assert decisions[0]["payload"]["selected_budget"] == 500000.0
    assert operator_bot._load_pending_budget_request("req-1") is None


def test_resuming_a_budget_workflow_reuses_the_stored_amount(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="budget")
    operator_bot._confirm_budget_request(
        _request("req-1"), selected_budget=500000.0, chat_id=1, user_id=2, username="op", attempt=1
    )
    claims = store.list_system_events_by_type("funding_workflow_claim", limit=None)
    assert claims[0]["payload"]["selected_budget"] == 500000.0
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_resume.py -q -k budget`
Expected: FAIL — decision이 여전히 먼저 저장된다

- [ ] **Step 3: 최소 구현**

`_confirm_budget_request`를 재배열: `validate_selected_budget` → claim(payload에 `selected_budget` 포함) → portfolio refresh → `_run_child_signal(..., source_phase="budget")` → `complete_workflow(legacy_payload={"status": "selected", "selected_budget": ..., ...})`. `_save_budget_decision` 단독 호출은 취소 경로만 남기되, 그것도 `complete_workflow`로 바꾼다.

claim payload에 금액을 싣기 위해 `claim_workflow_attempt`에 `extra: Mapping[str, Any] | None = None`를 더하고, 있으면 claim payload에 병합한다:

```python
    extra: Mapping[str, Any] | None = None,
    ...
    claim_payload = {
        "duplicate_key": claim_key(workflow_id, phase, request_id, attempt),
        "workflow_id": workflow_id,
        "request_id": request_id,
        "phase": phase,
        "attempt": attempt,
        "head_version": version,
    }
    if extra:
        # 재개가 원래 입력값을 그대로 쓸 수 있게 claim에 싣는다.
        # duplicate_key가 내용을 식별해야 하므로 결정적 값만 허용된다.
        claim_payload.update(dict(extra))
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_resume.py tests/test_contribution_budget_requests.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/funding_workflow.py src/maestro/integrations/telegram/handlers.py tests/test_funding_workflow_resume.py
git commit -m "feat(3a-4): make the budget decision an input, not a terminal state"
```

---

### Task 10: 중단 워크플로우 복구 카드와 [재개]

**Files:**
- Modify: `src/maestro/state/funding_workflow.py`
- Modify: `src/maestro/integrations/telegram/handlers.py` (callback 라우팅, poll sweep)
- Test: `tests/test_funding_workflow_resume.py` (이어서)

**Interfaces:**
- Consumes: Task 5·7
- Produces:
  - `list_incomplete_workflows(store: StateStore) -> list[dict[str, Any]]` — 각 항목 `{"workflow_id", "request_id", "phase", "attempt", "selected_budget"}`; claim은 있으나 `funding_workflow_completed`가 없는 건만
  - callback action `operator:wfresume:<phase>:<request_id>`

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_funding_workflow_resume.py 에 이어서
from maestro.state.funding_workflow import claim_workflow_attempt, list_incomplete_workflows


def test_a_claim_without_completion_shows_up_as_incomplete(operator_bot):
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert [row["request_id"] for row in list_incomplete_workflows(store)] == ["req-1"]


def test_a_completed_workflow_is_not_incomplete(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )
    assert list_incomplete_workflows(store) == []


def test_an_incomplete_workflow_is_never_resumed_automatically(operator_bot):
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    operator_bot._sweep_incomplete_workflows()
    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    assert list_incomplete_workflows(store) == [
        {
            "workflow_id": workflow_id,
            "request_id": "req-1",
            "phase": "funding",
            "attempt": 1,
            "selected_budget": None,
        }
    ]


def test_the_operator_resume_button_enters_exactly_once(operator_bot):
    import threading

    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    entered: list[bool] = []
    barrier = threading.Barrier(2)

    def press() -> None:
        barrier.wait()
        entered.append(
            claim_workflow_attempt(
                store,
                "run-1",
                workflow_id=workflow_id,
                request_id="req-1",
                phase="funding",
                attempt=2,
            )["claimed"]
        )

    threads = [threading.Thread(target=press) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(entered) == [False, True]
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_resume.py -q -k incomplete or resume_button`
Expected: FAIL — `ImportError: cannot import name 'list_incomplete_workflows'`

- [ ] **Step 3: 최소 구현**

```python
def list_incomplete_workflows(store: "StateStore") -> list[dict[str, Any]]:
    """claim은 있으나 completed가 없는 전이. 자동 재실행 대상이 아니라
    운영자 복구 카드의 입력이다."""
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
        current = latest.get(key)
        attempt = int(payload.get("attempt") or 0)
        if current is None or attempt > int(current["attempt"]):
            latest[key] = {
                "workflow_id": str(payload.get("workflow_id")),
                "request_id": key[0],
                "phase": key[1],
                "attempt": attempt,
                "selected_budget": payload.get("selected_budget"),
            }
    return sorted(latest.values(), key=lambda row: (row["phase"], row["request_id"]))
```

`handlers.py`에 sweep과 callback:

```python
    def _sweep_incomplete_workflows(self) -> None:
        """중단된 전이를 운영자에게 노출한다. 재실행하지 않는다.

        claim만 있는 상태를 자동으로 이어 달리면 이전 attempt의 지연된
        실행과 겹쳐 중복 부작용을 낸다. 진입은 [재개] 버튼 + attempt 증가로만.
        """
        for row in list_incomplete_workflows(self.store):
            key = f"funding-workflow-stalled:{row['request_id']}:{row['phase']}:a{row['attempt']}"
            if self.store.duplicate_key_exists(key):
                continue
            save_audited_system_event(
                self.store,
                self.audit,
                new_run_id(),
                "funding_workflow_stalled_notice",
                {**row, "duplicate_key": key},
            )
            self._broadcast(
                "\n".join(
                    [
                        "⚠️ 이전 작업이 중단된 상태예요",
                        f"요청: {row['request_id']} ({row['phase']})",
                        "아래 [재개]를 누르면 이어서 진행해요.",
                    ]
                ),
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "재개",
                                "callback_data": (
                                    f"operator:wfresume:{row['phase']}:{row['request_id']}"
                                ),
                            }
                        ]
                    ]
                },
            )
```

`_process_workflow_resume_callback`은 `list_incomplete_workflows`에서 해당 항목을 찾아 `attempt + 1`로 `_confirm_funding_request` / `_confirm_budget_request`를 호출하고, `WorkflowClaimRefused`는 "이미 재개됐어요"로 응답한다. 라우팅은 기존 `_process_funding_callback` 등록부 옆에 추가한다. `_broadcast`가 없으면 그 파일의 기존 브로드캐스트 헬퍼 이름을 그대로 쓴다.

poll 루프에서 `_sweep_pending_approvals` 옆에 `_sweep_incomplete_workflows()`를 호출한다.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_resume.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/funding_workflow.py src/maestro/integrations/telegram/handlers.py tests/test_funding_workflow_resume.py
git commit -m "feat(3a-4): surface stalled workflows for operator-approved resume"
```

---

### Task 11: 수렴 sweep (orphan 요청 / dangling head)

**Files:**
- Modify: `src/maestro/state/funding_workflow.py`
- Modify: `src/maestro/integrations/telegram/handlers.py` (poll 루프)
- Test: `tests/test_funding_workflow_resume.py` (이어서)

**Interfaces:**
- Consumes: Task 2 `list_funding_workflow_heads`, Task 3
- Produces:
  - `converge_workflow_invariants(store: StateStore, *, cutoff: int | None) -> dict[str, int]` — 반환 `{"orphans_superseded": int, "heads_rolled_back": int}`. `cutoff`가 `None`이면 아무것도 하지 않고 0을 돌려준다.
  - `load_migration_cutoff(store: StateStore) -> int | None` — `funding_workflow_migration_started` 이벤트의 `cutoff`. 3a-5가 이 이벤트를 쓴다. 지금은 항상 `None`이 정상이다.

**설계 메모:** 수렴은 **마이그레이션 cutoff 이후에 생성된 요청에만** 적용한다. 3a 이전에 생성된 요청은 head가 원래 없으므로 orphan이 아니다. 3a-5의 backfill이 아직 없는 지금은 cutoff가 없고, 따라서 sweep은 비활성이다 — 이것이 정상 동작이며 테스트로 고정한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_funding_workflow_resume.py 에 이어서
from maestro.state.funding_workflow import converge_workflow_invariants, load_migration_cutoff


def test_without_a_migration_cutoff_the_sweep_does_nothing(operator_bot):
    store = operator_bot.store
    store.save_system_event("run-1", "contribution_funding_request", dict(_request("legacy-1")))
    assert load_migration_cutoff(store) is None
    assert converge_workflow_invariants(store, cutoff=None) == {
        "orphans_superseded": 0,
        "heads_rolled_back": 0,
    }
    assert store.list_system_events_by_type("funding_workflow_superseded", limit=None) == []


def test_a_pending_request_after_the_cutoff_without_a_head_is_superseded(operator_bot):
    store = operator_bot.store
    cutoff = 0
    store.save_system_event("run-1", "contribution_funding_request", dict(_request("orphan-1")))
    result = converge_workflow_invariants(store, cutoff=cutoff)
    assert result["orphans_superseded"] == 1
    superseded = store.list_system_events_by_type("funding_workflow_superseded", limit=None)
    assert [row["payload"]["request_id"] for row in superseded] == ["orphan-1"]


def test_a_pending_request_before_the_cutoff_is_left_alone(operator_bot):
    store = operator_bot.store
    store.save_system_event("run-1", "contribution_funding_request", dict(_request("legacy-1")))
    rows = store.list_system_events_by_type("contribution_funding_request", limit=None)
    cutoff = int(rows[0]["id"])
    assert converge_workflow_invariants(store, cutoff=cutoff)["orphans_superseded"] == 0


def test_a_head_pointing_at_nothing_falls_back_to_the_previous_version(operator_bot):
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    store.save_system_events_atomic(
        "run-2",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": head_key(workflow_id, 2),
                    "workflow_id": workflow_id,
                    "version": 2,
                    "request_id": "ghost-1",
                    "phase": "funding",
                    "status": "pending",
                },
            }
        ],
    )
    result = converge_workflow_invariants(store, cutoff=0)
    assert result["heads_rolled_back"] == 1
    head = store.load_funding_workflow_head(workflow_id)
    assert head["request_id"] == "req-1"
    assert head["version"] == 3


def test_converging_twice_changes_nothing_the_second_time(operator_bot):
    store = operator_bot.store
    store.save_system_event("run-1", "contribution_funding_request", dict(_request("orphan-1")))
    converge_workflow_invariants(store, cutoff=0)
    assert converge_workflow_invariants(store, cutoff=0)["orphans_superseded"] == 0
```

`head_key`를 이 파일 import에 추가한다.

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_funding_workflow_resume.py -q -k converge or cutoff`
Expected: FAIL — `ImportError: cannot import name 'converge_workflow_invariants'`

- [ ] **Step 3: 최소 구현**

```python
def load_migration_cutoff(store: "StateStore") -> int | None:
    """3a 업그레이드 backfill이 고정한 경계. 3a-5가 기록한다.

    없으면 수렴 sweep은 켜지지 않는다 — 3a 이전에 만들어진 요청은 head가
    원래 없으므로 orphan이 아니고, 경계를 모른 채 쓸어담으면 정상 요청을
    supersede해 월간 투자를 지운다.
    """
    rows = store.list_system_events_by_type("funding_workflow_migration_started", limit=None)
    if not rows:
        return None
    return min(int((row.get("payload") or {}).get("cutoff") or 0) for row in rows)


def converge_workflow_invariants(store: "StateStore", *, cutoff: int | None) -> dict[str, int]:
    """head 미연결 요청과 실체 없는 head를 수렴시킨다."""
    if cutoff is None:
        return {"orphans_superseded": 0, "heads_rolled_back": 0}
    heads = {row["workflow_id"]: row for row in store.list_funding_workflow_heads()}
    live_request_ids = {str(row.get("request_id")) for row in heads.values()}
    orphans = 0
    for event_type, phase in (
        ("contribution_funding_request", "funding"),
        ("contribution_budget_request", "budget"),
    ):
        for row in store.list_system_events_by_type(event_type, limit=None):
            if int(row.get("id") or 0) <= cutoff:
                continue
            payload = row.get("payload") or {}
            if payload.get("status") != "pending":
                continue
            request_id = str(payload.get("request_id") or "")
            if not request_id or request_id in live_request_ids:
                continue
            workflow_id = payload.get("funding_workflow_id") or workflow_id_from_request(payload)
            outcome = store.save_system_events_atomic(
                str(row.get("run_id") or request_id),
                [
                    {
                        "event_type": "funding_workflow_superseded",
                        "payload": {
                            "duplicate_key": superseded_key(str(workflow_id), request_id),
                            "workflow_id": workflow_id,
                            "request_id": request_id,
                            "phase": phase,
                            "reason": "orphan_no_head",
                        },
                    }
                ],
            )
            if outcome["committed"]:
                orphans += 1
    rolled_back = 0
    for workflow_id, head in heads.items():
        request_id = str(head.get("request_id") or "")
        if _request_exists(store, request_id):
            continue
        previous = _previous_head_with_a_real_request(store, workflow_id, head)
        if previous is None:
            continue
        version = int(head.get("version") or 0) + 1
        outcome = store.save_system_events_atomic(
            workflow_id,
            [
                {
                    "event_type": "funding_workflow_head",
                    "payload": {
                        **previous,
                        "duplicate_key": head_key(workflow_id, version),
                        "version": version,
                        "reason": "dangling_head_rollback",
                    },
                }
            ],
            forbid_duplicate_keys=(head_key(workflow_id, version),),
        )
        if outcome["committed"]:
            rolled_back += 1
    return {"orphans_superseded": orphans, "heads_rolled_back": rolled_back}


def _request_exists(store: "StateStore", request_id: str) -> bool:
    for event_type in ("contribution_funding_request", "contribution_budget_request"):
        for row in store.list_system_events_by_type(event_type, limit=None):
            if str((row.get("payload") or {}).get("request_id") or "") == request_id:
                return True
    return False


def _previous_head_with_a_real_request(
    store: "StateStore", workflow_id: str, head: Mapping[str, Any]
) -> dict[str, Any] | None:
    candidates = [
        dict(row.get("payload") or {})
        for row in store.list_system_events_by_type("funding_workflow_head", limit=None)
        if (row.get("payload") or {}).get("workflow_id") == workflow_id
    ]
    candidates.sort(key=lambda row: int(row.get("version") or 0), reverse=True)
    for candidate in candidates:
        if int(candidate.get("version") or 0) >= int(head.get("version") or 0):
            continue
        if _request_exists(store, str(candidate.get("request_id") or "")):
            return candidate
    return None
```

poll 루프에 추가: `converge_workflow_invariants(self.store, cutoff=load_migration_cutoff(self.store))`.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_funding_workflow_resume.py -q`
Expected: PASS

- [ ] **Step 5: 전체 검증과 커밋**

```bash
pytest -q
ruff check src tests
git add -A
git commit -m "feat(3a-4): converge orphan requests and dangling heads after the cutoff"
```

---

## 검증

- `pytest -q` — 신규 4개 파일 포함 전부 통과. 기존 `tests/test_signal_approval_handoff.py`의 `test_daily_signal_approval_*` 5건은 root 소유 `/tmp` lock으로 인한 **선행 실패**이며 이 계획과 무관하다(`docs/superpowers/plans/2026-08-15-approval-dispatch-resume.md:226`).
- `ruff check src tests` — 통과.
- 스펙 승인 조건 대응:
  - "funding/budget 요청 교체 시 카드가 이어지고 중복 카드가 생기지 않는다" → Task 3·4
  - "같은 달 복수 funding scope 및 account_id=null 환경에서 scope별로 분리" → Task 1·3
  - "요청 전환 중 crash 주입해도 신규 요청은 하나, 구 요청 callback은 거절, 동시 중복 callback은 한 건" → Task 5·6·8
  - "claim-only 워크플로우가 [재개]로 정확히 한 번 재개되고, orphan/dangling head가 sweep에서 수렴" → Task 10·11
  - "완료된 요청이 있는 DB를 롤백해도 dual-write 덕분에 구버전이 pending으로 오판하지 않는다" → Task 7·8·9

운영 확인 (컷오버 후):

1. 스테이징 DB 사본에서 `list_incomplete_workflows`가 0건인지 확인한다. 0건이 아니면 배포 전에 먼저 종결한다.
2. 배포 후 첫 월초 사이클에서 funding 카드가 scope당 한 장씩 오는지, `funding_workflow_head` v1이 요청마다 기록되는지 확인한다.
3. `funding_workflow_head_conflict` audit 이벤트가 나오면 같은 scope/month에 두 run이 경쟁한 것이다 — 정상 동작이지만 빈도를 본다.

## 명시적으로 하지 않는 것

- **업그레이드 backfill과 전체 롤백 preflight** — 3a-5. Task 11의 `load_migration_cutoff`가 그 seam이며, 3a-5 이전에는 수렴 sweep이 켜지지 않는다. 이 상태로 배포해도 안전하다(아무것도 수렴시키지 않을 뿐이다).
- **월간 자금 카드 UI** — 3b. 이 단계는 상태 머신만 만들고, 카드 렌더는 기존 텍스트 메시지를 그대로 쓴다.
- **callback data 토큰 매핑(`ui:detail:...`)** — 3b. 지금은 callback이 전부 `request_id`만 담으므로 64바이트 제한에 걸리지 않는다.
- **재개 시 capacity 필터가 이미 배달된 그룹을 지우는 문제 (이월 5번)** — 3a-3 후속 웨이브. 별도 소규모 계획서.
- **현금흐름 기록 멱등화** — 이미 `duplicate_key=account-cash-flow:funding:<request_id>`로 되어 있다(`handlers.py:3424`). Task 8이 테스트로 고정만 한다.

> **SUPERSEDED (2026-08-24).** The canonical Phase 3a-5 plan is
> `docs/superpowers/plans/2026-08-24-upgrade-backfill-rollback-preflight-v2.md`.
> This document is retained for its historical design assumptions. Three of its
> tasks must NOT be implemented as written: the legacy-approval "no evidence =>
> canceled" resolution backfill (synthesizes history from absence), the rollback
> preflight's idempotent legacy-event repair (preflight is read-only), and the
> convergence-sweep stale-snapshot task (already solved at HEAD by atomic
> require/forbid preconditions). See the v2 plan's "Reconciliation" section.

# 3a-5: 업그레이드 backfill · 전체 롤백 preflight CLI 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 3a 이전에 쌓인 상태를 quiesce 장벽 아래에서 멱등 backfill해 새 불변식이 legacy 요청을 orphan으로 오판하거나 완료된 승인을 재집행하지 못하게 하고, 롤백 전 안전 검사를 4종 미완 상태 전부로 확장한 CLI와 운영 문서를 제공한다.

**Architecture:** 두 개의 CLI 명령이 축이다. `maestro upgrade-backfill`은 `migration_started`(immutable cutoff) → head 초기화 → legacy ack 판정 → `migration_completed` 순으로 진행하며 모든 쓰기가 결정적 duplicate_key로 멱등이라 어느 단계에서 죽어도 같은 cutoff로 재개된다. `maestro approval-rollback-preflight`는 기존 1종 검사를 4종으로 확장하고 dual-write 누락은 멱등 backfill 후 재검사한다. 판정 로직은 전부 `state/upgrade_backfill.py`에 있고 CLI는 얇은 껍데기다.

**Tech Stack:** Python 3.11+, SQLite(`sqlite3`, WAL, `BEGIN IMMEDIATE`), typer, pytest(+pytest-randomly), ruff, systemd(`systemctl is-active`)

**Spec:** `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md` — 「마이그레이션 순서」의 **단계 3a는 roll-forward-only**(715~752행)와 **3a 업제이드 backfill**(754~793행), 승인 조건(818~836행). 개정 10차·12차·13차가 이 범위를 정의한다.

## Global Constraints

- **선행 조건: 3a-4가 머지되어 있어야 한다.** 이 계획은 `state/funding_workflow.py`의 `head_key` / `superseded_key` / `completed_key` / `load_funding_workflow_head` / `converge_workflow_invariants` / `load_migration_cutoff`를 그대로 쓴다.
- **quiesce 장벽은 backfill과 preflight 양쪽 모두의 전제다.** 장벽 없이 실행하면 검사 통과가 무의미하다 — 검사 직후 callback·sweep·예약 run이 새 미완 상태를 만든다.
- **`migration_started`의 cutoff는 immutable하다.** 재기동 시 미완 마이그레이션이 발견되면 새 cutoff를 만들지 않고 기존 것으로 재개한다.
- **`migration_completed` 이전에는 어떤 프로세스도** 신규 스키마 write·수렴 sweep·funding/budget callback 처리를 시작하지 않는다.
- **수렴 sweep을 켜기 전에 3a-4가 남긴 stale-snapshot 결함을 먼저 닫아야 한다.** `converge_workflow_invariants`는 `heads`/`live_request_ids`/`accounted_*`를 함수 진입 시점에 한 번 스냅샷하는데, orphan 루프의 per-row `save_system_events_atomic`은 precondition을 하나도 선언하지 않는다(`funding_workflow.py` orphan 쓰기 지점; 바로 아래 head 롤백 루프는 자기 target key를 forbid한다 — orphan 루프만 무방비다). sweep 도중에 예약 signal run이 `publish_contribution_request`로 새 요청을 커밋하면, 스냅샷에 없는 그 요청이 **살아 있는 head인 채로** `orphan_no_head` supersede 마커를 뒤집어쓴다. 돈은 움직이지 않지만(claim은 마커가 아니라 head를 본다) 이벤트 로그가 거짓을 주장하고, 이후 sweep은 그 행을 처리 완료로 취급한다. 3a-4에서는 `cutoff is None`이라 도달 불가여서 의도적으로 유예했다 — **3a-5가 sweep을 켜는 순간 도달 가능해진다.** 둘 중 하나로 닫는다: (a) sweep을 quiesce 장벽 안에서만 돌린다, 또는 (b) candidate 행마다 head를 다시 읽고 `require_duplicate_keys`로 "이 요청이 head가 아님"을 트랜잭션 안에서 재검증한 뒤 쓴다. 함께: orphan 루프의 per-row `except ValueError` skip 건수를 반환 dict에 집계해 두었으므로(3a-4 최종 리뷰 반영), sweep을 켠 뒤 이 값이 0이 아니면 위 경합이 실제로 발생했다는 신호로 읽는다.
- **모호한 케이스는 자동 처리하지 않는다.** 같은 workflow에 pending 2건 이상, 증거 불충분 ack — 둘 다 운영자 검토로 격리한다.
- **모든 backfill 쓰기는 결정적 duplicate_key**(workflow_id·approval_id 기반)로 멱등이다. payload에 실행 시각·난수를 넣지 않는다 — 넣으면 재개가 `ValueError`로 죽는다(`store.py:1103-1120`).
- preflight는 읽기 전용이 원칙이다. 유일한 예외는 dual-write 누락의 legacy 이벤트 멱등 backfill이며, 이는 스펙이 명시적으로 지시한 것이다(741~742행).
- 테스트는 `pytest -q`, 린트는 `ruff check src tests`.

---

## File Structure

**Create:**
- `src/maestro/state/upgrade_backfill.py` — 마이그레이션 마커·cutoff, 3단계 backfill 판정과 쓰기, preflight 4종 검사. CLI가 호출하는 유일한 로직 소재지.
- `tests/test_upgrade_backfill_markers.py` — 마커/cutoff 불변성, 재개
- `tests/test_upgrade_backfill_heads.py` — legacy pending 요청 head v1, 모호 그룹 격리
- `tests/test_upgrade_backfill_acks.py` — legacy ack 증거 판정, 모호 격리
- `tests/test_rollback_preflight.py` — 4종 검사, dual-write backfill, quiesce 게이트
- `tests/fixtures/legacy_3a_state.sql` — 구버전 DB 스냅샷(스키마 + 대표 행). 업그레이드 테스트의 입력.
- `docs/rollback_and_upgrade_3a.md` — 운영 문서(quiesce unit 목록, 절차, 실패 시 판단)

**Modify:**
- `src/maestro/cli.py:1556-1608` — `_service_is_active`를 다중 unit 장벽으로 확장, `approval-rollback-preflight` 재작성, `upgrade-backfill` 신설
- `src/maestro/integrations/telegram/handlers.py` — funding/budget callback과 수렴 sweep에 `migration_completed` 게이트
- `docs/operator_runbook.md` — 새 운영 문서 링크 한 줄

---

### Task 1: quiesce 장벽 — writer unit 전부를 검사

**Files:**
- Modify: `src/maestro/cli.py:1556-1561`
- Test: `tests/test_rollback_preflight.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `WRITER_UNITS: tuple[str, ...]` in `maestro/cli.py`
  - `_active_writer_units(units: Sequence[str] = WRITER_UNITS) -> list[str]` — 아직 active인 unit 이름 목록

**설계 메모:** 현재 코드는 `maestro-telegram-operator.service` 하나만 본다. 스펙은 "writer를 되살릴 수 있는 유닛(`maestro-run-once` 포함)까지 빠짐없이 열거"를 요구한다(736~738행) — 타이머만 멈추면 실행 중인 인스턴스가 writer로 계속 남고, 타이머가 살아 있으면 검사와 배포 사이에 새 writer가 뜬다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_rollback_preflight.py
from maestro import cli


def test_the_barrier_covers_more_than_the_operator_service():
    assert "maestro-telegram-operator.service" in cli.WRITER_UNITS
    assert any(unit.startswith("maestro-run-once") for unit in cli.WRITER_UNITS)
    assert any(unit.endswith(".timer") for unit in cli.WRITER_UNITS)


def test_every_inactive_unit_means_an_empty_barrier_report(monkeypatch):
    monkeypatch.setattr(cli, "_service_is_active", lambda unit: False)
    assert cli._active_writer_units() == []


def test_a_single_live_unit_is_reported_by_name(monkeypatch):
    monkeypatch.setattr(
        cli, "_service_is_active", lambda unit: unit == "maestro-telegram-operator.service"
    )
    assert cli._active_writer_units() == ["maestro-telegram-operator.service"]


def test_the_barrier_reports_every_live_unit_not_just_the_first(monkeypatch):
    monkeypatch.setattr(cli, "_service_is_active", lambda unit: True)
    assert cli._active_writer_units() == list(cli.WRITER_UNITS)
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_rollback_preflight.py -q`
Expected: FAIL — `AttributeError: module 'maestro.cli' has no attribute 'WRITER_UNITS'`

- [ ] **Step 3: 최소 구현**

`src/maestro/cli.py`의 `_service_is_active` 뒤에 추가:

```python
# state store에 쓰는 모든 unit. 타이머까지 넣는 이유는, 서비스만 멈추면
# 검사와 구버전 배포 사이에 타이머가 새 writer를 띄워 장벽이 뚫리기
# 때문이다. maestro-run-once는 한 번 돌고 끝나지만 실행 중에는 writer다.
WRITER_UNITS: tuple[str, ...] = (
    "maestro-telegram-operator.service",
    "maestro-run-once.service",
    "maestro-signal-kr.timer",
    "maestro-signal-us.timer",
    "maestro-signal-kr.service",
    "maestro-signal-us.service",
)


def _active_writer_units(units: Sequence[str] = WRITER_UNITS) -> list[str]:
    """아직 살아 있는 writer unit. 빈 리스트여야 장벽이 선 것이다."""
    return [unit for unit in units if _service_is_active(unit)]
```

`WRITER_UNITS`의 실제 unit 이름은 `docs/vps_systemd.md`에 적힌 것과 대조해 맞춘다. `Sequence` import가 없으면 `from collections.abc import Sequence`를 추가한다.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_rollback_preflight.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/cli.py tests/test_rollback_preflight.py
git commit -m "feat(3a-5): enumerate every writer unit in the quiesce barrier"
```

---

### Task 2: 마이그레이션 마커와 immutable cutoff

**Files:**
- Create: `src/maestro/state/upgrade_backfill.py`
- Test: `tests/test_upgrade_backfill_markers.py`

**Interfaces:**
- Consumes: `StateStore.save_system_events_atomic`, `StateStore.list_system_events_by_type`, `maestro.state.funding_workflow.load_migration_cutoff`
- Produces:
  - `start_migration(store: StateStore) -> int` — cutoff를 돌려준다. 이미 started가 있으면 **그 cutoff를 그대로** 돌려주고 새로 만들지 않는다.
  - `complete_migration(store: StateStore, cutoff: int) -> bool` — 이미 completed면 `False`
  - `migration_is_complete(store: StateStore) -> bool`
  - `MIGRATION_STARTED = "funding_workflow_migration_started"`, `MIGRATION_COMPLETED = "funding_workflow_migration_completed"`
  - `StateStore.max_system_event_id() -> int` (store.py에 추가)

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_upgrade_backfill_markers.py
from maestro.state.funding_workflow import load_migration_cutoff
from maestro.state.store import StateStore
from maestro.state.upgrade_backfill import (
    complete_migration,
    migration_is_complete,
    start_migration,
)


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def test_the_cutoff_is_the_highest_event_id_at_the_moment_of_start(tmp_path):
    store = _store(tmp_path)
    store.save_system_event("run-1", "contribution_funding_request", {"request_id": "req-1"})
    expected = store.max_system_event_id()
    assert start_migration(store) == expected


def test_starting_twice_reuses_the_original_cutoff(tmp_path):
    store = _store(tmp_path)
    first = start_migration(store)
    store.save_system_event("run-2", "contribution_funding_request", {"request_id": "req-2"})
    assert start_migration(store) == first


def test_the_cutoff_is_readable_by_the_convergence_sweep(tmp_path):
    store = _store(tmp_path)
    cutoff = start_migration(store)
    assert load_migration_cutoff(store) == cutoff


def test_a_started_migration_is_not_complete(tmp_path):
    store = _store(tmp_path)
    start_migration(store)
    assert migration_is_complete(store) is False


def test_completing_flips_the_marker(tmp_path):
    store = _store(tmp_path)
    cutoff = start_migration(store)
    assert complete_migration(store, cutoff) is True
    assert migration_is_complete(store) is True


def test_completing_twice_is_idempotent(tmp_path):
    store = _store(tmp_path)
    cutoff = start_migration(store)
    complete_migration(store, cutoff)
    assert complete_migration(store, cutoff) is False
    events = store.list_system_events_by_type(
        "funding_workflow_migration_completed", limit=None
    )
    assert len(events) == 1


def test_an_empty_database_still_yields_a_usable_cutoff(tmp_path):
    store = _store(tmp_path)
    assert start_migration(store) == 0
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_upgrade_backfill_markers.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.state.upgrade_backfill'`

- [ ] **Step 3: 최소 구현**

`store.py`에 추가:

```python
    def max_system_event_id(self) -> int:
        """지금까지 기록된 마지막 system event의 id. 마이그레이션 경계다."""
        with self._connect() as conn:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM system_events").fetchone()
        return int(row[0])
```

`src/maestro/state/upgrade_backfill.py`:

```python
"""3a 업그레이드 backfill과 롤백 preflight의 판정 로직.

새 불변식은 3a 이전에 쌓인 상태를 모른다. backfill 없이 켜면 (a) 기존
pending funding/budget 요청은 head가 없어 수렴 sweep이 orphan으로 오판해
supersede하고, (b) 기존에 정상 완료된 승인 ack는 resolution_completed가
없어 sweep이 미완으로 오판해 재집행한다. 그래서 경계(cutoff)를 먼저
고정하고, 그 이전 상태만 판정한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from maestro.state.store import StateStore

MIGRATION_STARTED = "funding_workflow_migration_started"
MIGRATION_COMPLETED = "funding_workflow_migration_completed"


def start_migration(store: "StateStore") -> int:
    """마이그레이션 경계를 고정하고 돌려준다.

    cutoff는 immutable하다: 중단 후 재기동에서 새 경계를 만들면 첫
    실행이 이미 backfill한 행이 두 번째 실행에는 "경계 이후"로 보여
    판정이 뒤집힌다. 이미 started가 있으면 그 값을 그대로 쓴다.
    """
    existing = store.list_system_events_by_type(MIGRATION_STARTED, limit=None)
    if existing:
        return min(int((row.get("payload") or {}).get("cutoff") or 0) for row in existing)
    cutoff = store.max_system_event_id()
    store.save_system_events_atomic(
        "upgrade-backfill",
        [
            {
                "event_type": MIGRATION_STARTED,
                "payload": {"duplicate_key": "funding-workflow-migration:started", "cutoff": cutoff},
            }
        ],
    )
    return cutoff


def complete_migration(store: "StateStore", cutoff: int) -> bool:
    outcome = store.save_system_events_atomic(
        "upgrade-backfill",
        [
            {
                "event_type": MIGRATION_COMPLETED,
                "payload": {
                    "duplicate_key": "funding-workflow-migration:completed",
                    "cutoff": cutoff,
                },
            }
        ],
    )
    return bool(outcome["committed"])


def migration_is_complete(store: "StateStore") -> bool:
    return bool(store.list_system_events_by_type(MIGRATION_COMPLETED, limit=None))
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_upgrade_backfill_markers.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/upgrade_backfill.py src/maestro/state/store.py tests/test_upgrade_backfill_markers.py
git commit -m "feat(3a-5): pin an immutable migration cutoff behind start/complete markers"
```

---

### Task 3: legacy pending 요청의 v1 head 초기화

**Files:**
- Modify: `src/maestro/state/upgrade_backfill.py`
- Test: `tests/test_upgrade_backfill_heads.py`

**Interfaces:**
- Consumes: Task 2, `funding_workflow.workflow_id_from_request` / `head_key`, `StateStore.load_funding_workflow_head`
- Produces:
  - `backfill_legacy_heads(store: StateStore, cutoff: int) -> dict[str, int]` — `{"heads_created": int, "quarantined": int, "skipped": int}`
  - 격리 이벤트 `funding_workflow_migration_quarantine`, key `migration-quarantine:heads:<workflow_id>`

**설계 메모:** 같은 workflow에 pending이 2건 이상인 그룹은 **자동 supersede하지 않는다.** 어느 쪽이 활성인지 판단할 근거가 없고, 잘못 고르면 그 달 투자가 사라진다. 격리 이벤트만 남기고 head를 만들지 않으므로 그 workflow는 수렴 sweep 대상에서도 빠진다(head가 없으니 orphan 판정은 cutoff가 막는다).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_upgrade_backfill_heads.py
from maestro.state.funding_workflow import workflow_id_from_request
from maestro.state.store import StateStore
from maestro.state.upgrade_backfill import backfill_legacy_heads, start_migration


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def _legacy_request(store, request_id, *, sleeve="krw", status="pending", phase="funding"):
    payload = {
        "request_id": request_id,
        "contribution_group_id": "core",
        "account_id": "acct-1",
        "execution_sleeve": sleeve,
        "currency": "KRW",
        "month_key": "2026-08",
        "status": status,
        "strategy_ids": ["s1"],
    }
    store.save_system_event("run-legacy", f"contribution_{phase}_request", payload)
    return payload


def test_a_lone_legacy_pending_request_gets_head_v1(tmp_path):
    store = _store(tmp_path)
    request = _legacy_request(store, "req-1")
    cutoff = start_migration(store)
    assert backfill_legacy_heads(store, cutoff)["heads_created"] == 1
    head = store.load_funding_workflow_head(workflow_id_from_request(request))
    assert head["version"] == 1
    assert head["request_id"] == "req-1"


def test_an_already_acked_request_is_not_given_a_head(tmp_path):
    store = _store(tmp_path)
    request = _legacy_request(store, "req-1")
    store.save_system_event(
        "run-legacy", "contribution_funding_request_ack", {"request_id": "req-1"}
    )
    cutoff = start_migration(store)
    assert backfill_legacy_heads(store, cutoff)["heads_created"] == 0
    assert store.load_funding_workflow_head(workflow_id_from_request(request)) is None


def test_two_pending_requests_in_one_workflow_are_quarantined_not_superseded(tmp_path):
    store = _store(tmp_path)
    request = _legacy_request(store, "req-1")
    _legacy_request(store, "req-2")
    cutoff = start_migration(store)
    result = backfill_legacy_heads(store, cutoff)
    assert result["heads_created"] == 0
    assert result["quarantined"] == 1
    assert store.load_funding_workflow_head(workflow_id_from_request(request)) is None
    quarantines = store.list_system_events_by_type(
        "funding_workflow_migration_quarantine", limit=None
    )
    assert sorted(quarantines[0]["payload"]["request_ids"]) == ["req-1", "req-2"]


def test_different_scopes_each_get_their_own_head(tmp_path):
    store = _store(tmp_path)
    left = _legacy_request(store, "req-1", sleeve="krw")
    right = _legacy_request(store, "req-2", sleeve="usd")
    cutoff = start_migration(store)
    assert backfill_legacy_heads(store, cutoff)["heads_created"] == 2
    assert store.load_funding_workflow_head(workflow_id_from_request(left))["request_id"] == "req-1"
    assert (
        store.load_funding_workflow_head(workflow_id_from_request(right))["request_id"] == "req-2"
    )


def test_requests_created_after_the_cutoff_are_left_to_the_new_code(tmp_path):
    store = _store(tmp_path)
    cutoff = start_migration(store)
    request = _legacy_request(store, "req-late")
    assert backfill_legacy_heads(store, cutoff)["heads_created"] == 0
    assert store.load_funding_workflow_head(workflow_id_from_request(request)) is None


def test_running_the_backfill_twice_creates_nothing_new(tmp_path):
    store = _store(tmp_path)
    _legacy_request(store, "req-1")
    cutoff = start_migration(store)
    backfill_legacy_heads(store, cutoff)
    assert backfill_legacy_heads(store, cutoff)["heads_created"] == 0
    heads = store.list_system_events_by_type("funding_workflow_head", limit=None)
    assert len(heads) == 1


def test_a_budget_request_is_backfilled_too(tmp_path):
    store = _store(tmp_path)
    request = _legacy_request(store, "req-1", phase="budget")
    cutoff = start_migration(store)
    assert backfill_legacy_heads(store, cutoff)["heads_created"] == 1
    assert store.load_funding_workflow_head(workflow_id_from_request(request))["phase"] == "budget"
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_upgrade_backfill_heads.py -q`
Expected: FAIL — `ImportError: cannot import name 'backfill_legacy_heads'`

- [ ] **Step 3: 최소 구현**

```python
# 3a-4의 funding_workflow가 이미 이 매핑을 소유한다. 여기서 다시 정의하면
# 한쪽만 바뀌었을 때 backfill과 dual-write가 서로 다른 이벤트를 보게 된다.
from maestro.state.funding_workflow import LEGACY_TERMINAL_EVENT


def _legacy_pending_requests(store: "StateStore", cutoff: int) -> list[tuple[str, dict[str, Any]]]:
    """cutoff 이전에 만들어졌고 아직 종결되지 않은 요청."""
    pending: list[tuple[str, dict[str, Any]]] = []
    for phase, terminal in LEGACY_TERMINAL_EVENT.items():
        closed = {
            str((row.get("payload") or {}).get("request_id"))
            for row in store.list_system_events_by_type(terminal, limit=None)
        }
        for row in store.list_system_events_by_type(
            f"contribution_{phase}_request", limit=None
        ):
            if int(row.get("id") or 0) > cutoff:
                continue
            payload = dict(row.get("payload") or {})
            if payload.get("status") != "pending":
                continue
            if str(payload.get("request_id")) in closed:
                continue
            pending.append((phase, payload))
    return pending


def backfill_legacy_heads(store: "StateStore", cutoff: int) -> dict[str, int]:
    """cutoff 이전 pending 요청을 workflow별로 묶어 v1 head를 만든다.

    한 workflow에 pending이 둘 이상이면 어느 쪽이 활성인지 판단할 근거가
    없다. 자동 supersede는 그 달 투자를 지울 수 있으므로 격리만 한다 —
    head가 없는 workflow는 수렴 sweep도 건드리지 않는다.
    """
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for phase, payload in _legacy_pending_requests(store, cutoff):
        grouped.setdefault(workflow_id_from_request(payload), []).append((phase, payload))

    created = quarantined = skipped = 0
    for workflow_id, members in sorted(grouped.items()):
        if store.load_funding_workflow_head(workflow_id) is not None:
            skipped += 1
            continue
        if len(members) > 1:
            outcome = store.save_system_events_atomic(
                "upgrade-backfill",
                [
                    {
                        "event_type": "funding_workflow_migration_quarantine",
                        "payload": {
                            "duplicate_key": f"migration-quarantine:heads:{workflow_id}",
                            "workflow_id": workflow_id,
                            "reason": "multiple_pending_requests",
                            "request_ids": sorted(
                                str(payload["request_id"]) for _, payload in members
                            ),
                        },
                    }
                ],
            )
            if outcome["committed"]:
                quarantined += 1
            continue
        phase, payload = members[0]
        request_id = str(payload["request_id"])
        outcome = store.save_system_events_atomic(
            "upgrade-backfill",
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
                        "backfilled": True,
                        "scope": [
                            payload.get("contribution_group_id"),
                            payload.get("account_id"),
                            payload.get("execution_sleeve"),
                            payload.get("currency"),
                        ],
                    },
                }
            ],
        )
        if outcome["committed"]:
            created += 1
        else:
            skipped += 1
    return {"heads_created": created, "quarantined": quarantined, "skipped": skipped}
```

import 추가: `from maestro.state.funding_workflow import head_key, workflow_id_from_request`

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_upgrade_backfill_heads.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/upgrade_backfill.py tests/test_upgrade_backfill_heads.py
git commit -m "feat(3a-5): backfill v1 heads for legacy pending requests"
```

---

### Task 4: legacy 승인 ack의 완료 판정 backfill

**Files:**
- Modify: `src/maestro/state/upgrade_backfill.py`
- Test: `tests/test_upgrade_backfill_acks.py`

**Interfaces:**
- Consumes: Task 2, `StateStore.insert_approval_resolution`(4a가 만든 per-approval CAS), `StateStore.load_approval`
- Produces:
  - `backfill_legacy_approvals(store: StateStore, cutoff: int) -> dict[str, int]` — `{"resolved": int, "quarantined": int, "skipped": int}`
  - 격리 이벤트 `telegram_approval_migration_quarantine`, key `migration-quarantine:approval:<approval_id>`

**설계 메모:** cutoff 이전 ack 중 `schema_version`이 없는 것(3a 이전)만 대상이다. 완료 증거는 **`signal_approval_completed` 이벤트 + `approvals` 행** 둘 다다. 둘 다 있으면 `resolution_completed`를 backfill하고, 둘 다 없으면 집행이 없었음이 증명되므로 `canceled` 종결로 backfill한다. 하나만 있으면 모호하므로 **자동 재실행도 자동 종결도 하지 않고** 격리한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_upgrade_backfill_acks.py
from maestro.state.store import StateStore
from maestro.state.upgrade_backfill import backfill_legacy_approvals, start_migration


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def _legacy_ack(store, approval_id, *, schema_version=None):
    payload = {"approval_id": approval_id, "decision": "approved"}
    if schema_version is not None:
        payload["schema_version"] = schema_version
    store.save_system_event("run-legacy", "telegram_approval_ack", payload)


def _executed(store, approval_id, signal_run_id="sig-1"):
    store.save_approval(
        {
            "approval_id": approval_id,
            "signal_run_id": signal_run_id,
            "status": "approved",
        }
    )
    store.save_system_event(
        signal_run_id, "signal_approval_completed", {"signal_run_id": signal_run_id}
    )


def test_an_ack_with_full_execution_evidence_is_backfilled_as_completed(tmp_path):
    store = _store(tmp_path)
    _legacy_ack(store, "ap-1")
    _executed(store, "ap-1")
    cutoff = start_migration(store)
    assert backfill_legacy_approvals(store, cutoff)["resolved"] == 1
    assert store.approval_resolution_exists("ap-1") is True


def test_an_ack_with_no_execution_evidence_at_all_is_closed_as_canceled(tmp_path):
    store = _store(tmp_path)
    _legacy_ack(store, "ap-1")
    cutoff = start_migration(store)
    assert backfill_legacy_approvals(store, cutoff)["resolved"] == 1
    assert store.approval_resolution_exists("ap-1") is True
    completed = store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=None
    )
    assert completed[0]["payload"]["status"] == "canceled"


def test_partial_evidence_is_quarantined_not_guessed(tmp_path):
    store = _store(tmp_path)
    _legacy_ack(store, "ap-1")
    store.save_approval(
        {"approval_id": "ap-1", "signal_run_id": "sig-1", "status": "approved"}
    )
    cutoff = start_migration(store)
    result = backfill_legacy_approvals(store, cutoff)
    assert result["resolved"] == 0
    assert result["quarantined"] == 1
    assert store.approval_resolution_exists("ap-1") is False


def test_a_schema_version_2_ack_is_left_to_the_running_code(tmp_path):
    store = _store(tmp_path)
    _legacy_ack(store, "ap-1", schema_version=2)
    cutoff = start_migration(store)
    assert backfill_legacy_approvals(store, cutoff)["skipped"] == 1
    assert store.approval_resolution_exists("ap-1") is False


def test_an_ack_after_the_cutoff_is_not_touched(tmp_path):
    store = _store(tmp_path)
    cutoff = start_migration(store)
    _legacy_ack(store, "ap-late")
    assert backfill_legacy_approvals(store, cutoff)["resolved"] == 0


def test_an_already_resolved_approval_is_not_rewritten(tmp_path):
    store = _store(tmp_path)
    _legacy_ack(store, "ap-1")
    _executed(store, "ap-1")
    cutoff = start_migration(store)
    backfill_legacy_approvals(store, cutoff)
    assert backfill_legacy_approvals(store, cutoff)["resolved"] == 0
    completed = store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=None
    )
    assert len(completed) == 1


def test_quarantining_the_same_approval_twice_writes_one_event(tmp_path):
    store = _store(tmp_path)
    _legacy_ack(store, "ap-1")
    store.save_approval(
        {"approval_id": "ap-1", "signal_run_id": "sig-1", "status": "approved"}
    )
    cutoff = start_migration(store)
    backfill_legacy_approvals(store, cutoff)
    backfill_legacy_approvals(store, cutoff)
    quarantines = store.list_system_events_by_type(
        "telegram_approval_migration_quarantine", limit=None
    )
    assert len(quarantines) == 1
```

`store.save_approval` / `store.load_approval`의 실제 시그니처는 `store.py`에서 확인해 맞춘다.

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_upgrade_backfill_acks.py -q`
Expected: FAIL — `ImportError: cannot import name 'backfill_legacy_approvals'`

- [ ] **Step 3: 최소 구현**

```python
def backfill_legacy_approvals(store: "StateStore", cutoff: int) -> dict[str, int]:
    """cutoff 이전 legacy ack의 완료 여부를 증거로 판정한다.

    증거는 approvals 행과 signal_approval_completed 둘 다다. 둘 다 있으면
    집행이 끝난 것이고, 둘 다 없으면 집행이 시작조차 안 된 것이다. 하나만
    있으면 어느 쪽인지 알 수 없다 — 자동 재실행은 중복 주문을, 자동 종결은
    승인 유실을 낳으므로 격리해 운영자에게 넘긴다.
    """
    completed_runs = {
        str((row.get("payload") or {}).get("signal_run_id"))
        for row in store.list_system_events_by_type("signal_approval_completed", limit=None)
    }
    resolved = quarantined = skipped = 0
    for row in store.list_system_events_by_type("telegram_approval_ack", limit=None):
        if int(row.get("id") or 0) > cutoff:
            continue
        payload = row.get("payload") or {}
        if isinstance(payload.get("schema_version"), int):
            skipped += 1
            continue
        approval_id = str(payload.get("approval_id") or "")
        if not approval_id or store.approval_resolution_exists(approval_id):
            skipped += 1
            continue
        approval = store.load_approval(approval_id)
        has_row = approval is not None
        has_completion = has_row and str(approval.get("signal_run_id") or "") in completed_runs
        if has_row and has_completion:
            status = "executed"
        elif not has_row:
            status = "canceled"
        else:
            outcome = store.save_system_events_atomic(
                "upgrade-backfill",
                [
                    {
                        "event_type": "telegram_approval_migration_quarantine",
                        "payload": {
                            "duplicate_key": f"migration-quarantine:approval:{approval_id}",
                            "approval_id": approval_id,
                            "reason": "ambiguous_execution_evidence",
                            "has_approval_row": has_row,
                            "has_signal_completion": has_completion,
                        },
                    }
                ],
            )
            if outcome["committed"]:
                quarantined += 1
            continue
        _, created = store.insert_approval_resolution(
            "upgrade-backfill",
            {
                "approval_id": approval_id,
                "status": status,
                "duplicate_key": f"migration-resolution:{approval_id}",
                "backfilled": True,
            },
        )
        if created:
            resolved += 1
        else:
            skipped += 1
    return {"resolved": resolved, "quarantined": quarantined, "skipped": skipped}
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_upgrade_backfill_acks.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/upgrade_backfill.py tests/test_upgrade_backfill_acks.py
git commit -m "feat(3a-5): judge legacy approval acks by execution evidence"
```

---

### Task 5: `maestro upgrade-backfill` CLI

**Files:**
- Modify: `src/maestro/cli.py`
- Test: `tests/test_upgrade_backfill_markers.py` (이어서)

**Interfaces:**
- Consumes: Task 1~4
- Produces: CLI 명령 `upgrade-backfill`, 옵션 `--config`, `--require-quiesce/--no-require-quiesce`(기본 True)
  - 출력: `upgrade_backfill status=<safe|fail|quarantined> cutoff=<int> heads=<int> approvals=<int> quarantined=<int>`
  - exit 0: 완료. exit 1: 장벽 미충족 또는 격리 건 존재(완료 마커를 쓰지 않는다).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_upgrade_backfill_markers.py 에 이어서
from typer.testing import CliRunner

from maestro import cli
from maestro.state.upgrade_backfill import migration_is_complete

runner = CliRunner()


def test_the_backfill_refuses_to_run_while_a_writer_is_live(monkeypatch, tmp_path, config_file):
    monkeypatch.setattr(cli, "_service_is_active", lambda unit: True)
    result = runner.invoke(cli.app, ["upgrade-backfill", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "reason=writers_still_running" in result.stdout


def test_a_clean_database_completes_the_migration(monkeypatch, config_file):
    monkeypatch.setattr(cli, "_service_is_active", lambda unit: False)
    result = runner.invoke(cli.app, ["upgrade-backfill", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "status=safe" in result.stdout


def test_quarantined_cases_block_the_completed_marker(monkeypatch, config_file, store_of):
    monkeypatch.setattr(cli, "_service_is_active", lambda unit: False)
    store = store_of(config_file)
    store.save_system_event("run-legacy", "telegram_approval_ack", {"approval_id": "ap-1"})
    store.save_approval({"approval_id": "ap-1", "signal_run_id": "sig-1", "status": "approved"})
    result = runner.invoke(cli.app, ["upgrade-backfill", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "status=quarantined" in result.stdout
    assert migration_is_complete(store) is False


def test_rerunning_after_a_crash_reuses_the_same_cutoff(monkeypatch, config_file, store_of):
    monkeypatch.setattr(cli, "_service_is_active", lambda unit: False)
    store = store_of(config_file)
    runner.invoke(cli.app, ["upgrade-backfill", "--config", str(config_file)])
    first = [
        row["payload"]["cutoff"]
        for row in store.list_system_events_by_type(
            "funding_workflow_migration_started", limit=None
        )
    ]
    runner.invoke(cli.app, ["upgrade-backfill", "--config", str(config_file)])
    second = [
        row["payload"]["cutoff"]
        for row in store.list_system_events_by_type(
            "funding_workflow_migration_started", limit=None
        )
    ]
    assert first == second
```

`config_file`·`store_of` 픽스처는 `tests/test_cli_approval_settlement.py`가 쓰는 CLI 설정 셋업을 재사용한다(그 파일에서 import하거나 `conftest.py`로 추출).

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_upgrade_backfill_markers.py -q -k backfill`
Expected: FAIL — `Error: No such command 'upgrade-backfill'`

- [ ] **Step 3: 최소 구현**

```python
@app.command("upgrade-backfill")
def upgrade_backfill(
    config: Path | None = CONFIG_OPTION,
    require_quiesce: bool = typer.Option(
        True,
        "--require-quiesce/--no-require-quiesce",
        help="Fail unless every state store writer unit is stopped.",
    ),
) -> None:
    """3a 업그레이드 backfill. quiesce 장벽 아래에서만 실행한다.

    장벽 없이 돌리면 그 사이 구버전 writer가 head 없는 legacy 요청을 새로
    만들어(경계 오염) backfill이 끝난 뒤에도 불변식이 깨진 채 남는다.
    격리 건이 하나라도 있으면 completed 마커를 쓰지 않는다 — 마커가 없으면
    운영 코드가 신규 스키마 write와 sweep을 시작하지 않으므로, 미해결
    상태에서 시스템이 켜지는 일이 없다.
    """
    if require_quiesce:
        live = _active_writer_units()
        if live:
            typer.echo(
                "upgrade_backfill status=fail reason=writers_still_running "
                f"units={','.join(live)}"
            )
            raise typer.Exit(1)
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    cutoff = start_migration(store)
    heads = backfill_legacy_heads(store, cutoff)
    approvals = backfill_legacy_approvals(store, cutoff)
    quarantined = heads["quarantined"] + approvals["quarantined"]
    summary = (
        f"cutoff={cutoff} heads={heads['heads_created']} "
        f"approvals={approvals['resolved']} quarantined={quarantined}"
    )
    if quarantined:
        typer.echo(f"upgrade_backfill status=quarantined {summary}")
        raise typer.Exit(1)
    complete_migration(store, cutoff)
    typer.echo(f"upgrade_backfill status=safe {summary}")
```

import 추가: `from maestro.state.upgrade_backfill import (backfill_legacy_approvals, backfill_legacy_heads, complete_migration, start_migration)`

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_upgrade_backfill_markers.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/cli.py tests/test_upgrade_backfill_markers.py
git commit -m "feat(3a-5): add the upgrade-backfill command behind the quiesce barrier"
```

---

### Task 6: `migration_completed` 게이트

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py`
- Modify: `src/maestro/state/funding_workflow.py` — `converge_workflow_invariants`의 orphan 루프 (설계 메모 2)
- Test: `tests/test_upgrade_backfill_markers.py` (이어서)

**Interfaces:**
- Consumes: Task 2 `migration_is_complete`
- Produces: 마커가 없으면 funding/budget callback은 안내 문구로 거절되고 수렴 sweep은 진입하지 않는다.

**설계 메모:** 3a-4의 수렴 sweep은 이미 `load_migration_cutoff`가 `None`이면 비활성이다. 그런데 `started`만 있고 `completed`가 없는 **중간 상태**에서는 cutoff가 존재하므로 sweep이 켜진다 — backfill이 아직 head를 다 만들지 않은 상태에서 sweep이 돌면 legacy 요청을 orphan으로 오판한다. 그래서 게이트는 cutoff가 아니라 `completed` 마커여야 한다.

**설계 메모 2 (3a-4 최종 리뷰 F5):** 이 게이트를 통과시켜 sweep을 처음으로 실제 가동시키는 것이 바로 이 태스크다. 따라서 Global Constraints의 stale-snapshot 항목을 닫는 책임도 여기에 있다. `completed` 게이트만 달고 끝내면, 게이트가 열린 뒤 정상 운영 중에 sweep과 예약 signal run이 겹치는 순간 살아 있는 요청이 `orphan_no_head`로 마킹된다 — 게이트는 마이그레이션 중간 상태만 막을 뿐 정상 운영 중의 경합은 막지 못한다. 구현자는 (a) sweep 호출을 quiesce 장벽 안으로 옮기거나 (b) orphan 루프에서 candidate마다 head를 재조회하고 `require_duplicate_keys`로 트랜잭션 안에서 재검증하도록 `converge_workflow_invariants`를 고친 뒤, "sweep 진입 후·orphan 쓰기 전에 새 요청이 publish되면 그 요청은 supersede되지 않는다"를 확인하는 테스트를 추가한다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_upgrade_backfill_markers.py 에 이어서
def test_the_sweep_stays_off_between_started_and_completed(operator_bot):
    store = operator_bot.store
    store.save_system_event(
        "run-legacy",
        "contribution_funding_request",
        {
            "request_id": "req-1",
            "contribution_group_id": "core",
            "account_id": "acct-1",
            "execution_sleeve": "krw",
            "currency": "KRW",
            "month_key": "2026-08",
            "status": "pending",
        },
    )
    start_migration(store)
    operator_bot._converge_funding_workflows()
    assert store.list_system_events_by_type("funding_workflow_superseded", limit=None) == []


def test_the_sweep_runs_once_the_migration_is_complete(operator_bot):
    store = operator_bot.store
    cutoff = start_migration(store)
    complete_migration(store, cutoff)
    operator_bot._converge_funding_workflows()  # must not raise


def test_a_funding_callback_is_refused_before_the_completed_marker(operator_bot):
    store = operator_bot.store
    start_migration(store)
    handled = operator_bot._process_funding_callback(
        {"id": "cb-1", "message": {"chat": {"id": 1}, "message_id": 2}},
        "funding:complete:req-1",
        chat_id=1,
        user_id=2,
        username="op",
    )
    assert handled is True
    assert store.list_system_events_by_type("funding_workflow_claim", limit=None) == []
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_upgrade_backfill_markers.py -q -k marker or sweep or callback`
Expected: FAIL — sweep이 legacy 요청을 supersede한다

- [ ] **Step 3: 최소 구현**

`handlers.py`:

```python
    def _migration_is_pending(self) -> bool:
        """backfill이 시작됐지만 아직 끝나지 않았다.

        cutoff가 이미 있으므로 수렴 sweep은 스스로를 막지 못한다 —
        head가 아직 다 만들어지지 않은 상태에서 sweep이 돌면 legacy
        요청을 orphan으로 오판해 supersede한다. 게이트는 completed 마커다.
        """
        started = bool(
            self.store.list_system_events_by_type(MIGRATION_STARTED, limit=None)
        )
        return started and not migration_is_complete(self.store)

    def _converge_funding_workflows(self) -> None:
        if self._migration_is_pending():
            return
        converge_workflow_invariants(
            self.store, cutoff=load_migration_cutoff(self.store)
        )
```

`_process_funding_callback` / `_process_budget_callback` 진입부에 추가:

```python
        if self._migration_is_pending():
            self._answer(callback, "시스템 업그레이드 중이에요. 잠시 후 다시 눌러 주세요.")
            self._record("/funding", chat_id, user_id, username, "migration_pending")
            return True
```

3a-4 Task 11에서 poll 루프에 넣었던 `converge_workflow_invariants(...)` 직접 호출을 `self._converge_funding_workflows()`로 바꾼다.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_upgrade_backfill_markers.py tests/test_funding_workflow_resume.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/integrations/telegram/handlers.py tests/test_upgrade_backfill_markers.py
git commit -m "feat(3a-5): gate sweeps and callbacks on the migration completed marker"
```

---

### Task 7: 롤백 preflight를 4종 검사로 확장

**Files:**
- Modify: `src/maestro/state/upgrade_backfill.py`
- Modify: `src/maestro/cli.py:1564-1608`
- Test: `tests/test_rollback_preflight.py` (이어서)

**Interfaces:**
- Consumes: Task 1, 3a-4의 `list_incomplete_workflows` / `completed_key`
- Produces:
  - `rollback_preflight(store: StateStore) -> dict[str, list[str]]` — 키 4종: `incomplete_workflows`, `consumed_without_dispatch`, `acked_without_resolution`, `completed_without_legacy`. 값은 식별자 목록.
  - `backfill_missing_legacy_terminals(store: StateStore) -> int` — `completed_without_legacy` 건의 legacy 이벤트를 멱등 backfill하고 건수를 돌려준다.

**설계 메모:** 4종은 스펙 726~742행이 열거한 것 그대로다. 네 번째(dual-write 누락)만 검사 후 자동 복구가 지시되어 있으므로, CLI는 backfill → 재검사 순으로 돌고 그래도 남으면 unsafe다.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_rollback_preflight.py 에 이어서
from maestro.state.funding_workflow import claim_workflow_attempt, publish_contribution_request
from maestro.state.store import StateStore
from maestro.state.upgrade_backfill import backfill_missing_legacy_terminals, rollback_preflight


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


def test_a_clean_database_reports_nothing(tmp_path):
    store = _store(tmp_path)
    assert rollback_preflight(store) == {
        "incomplete_workflows": [],
        "consumed_without_dispatch": [],
        "acked_without_resolution": [],
        "completed_without_legacy": [],
    }


def test_a_claim_without_completion_is_detected(tmp_path):
    store = _store(tmp_path)
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert rollback_preflight(store)["incomplete_workflows"] == ["req-1"]


def test_a_consumed_package_without_dispatch_completion_is_detected(tmp_path):
    store = _store(tmp_path)
    store.save_system_event("sig-1", "signal_package", {"signal_run_id": "sig-1"})
    store.mark_signal_package_consumed("sig-1", "run-1")
    assert rollback_preflight(store)["consumed_without_dispatch"] == ["sig-1"]


def test_a_dispatched_package_is_not_reported(tmp_path):
    store = _store(tmp_path)
    store.save_system_event("sig-1", "signal_package", {"signal_run_id": "sig-1"})
    store.mark_signal_package_consumed("sig-1", "run-1")
    store.save_system_event("run-1", "signal_approval_pending", {"signal_run_id": "sig-1"})
    assert rollback_preflight(store)["consumed_without_dispatch"] == []


def test_a_versioned_ack_without_resolution_is_detected(tmp_path):
    store = _store(tmp_path)
    store.save_system_event(
        "run-1", "telegram_approval_ack", {"approval_id": "ap-1", "schema_version": 2}
    )
    assert rollback_preflight(store)["acked_without_resolution"] == ["ap-1"]


def test_a_completed_workflow_missing_its_legacy_event_is_detected(tmp_path):
    store = _store(tmp_path)
    store.save_system_event(
        "run-1",
        "funding_workflow_completed",
        {"workflow_id": "wf-a", "request_id": "req-1", "phase": "funding"},
    )
    assert rollback_preflight(store)["completed_without_legacy"] == ["req-1"]


def test_the_missing_legacy_event_is_backfilled_idempotently(tmp_path):
    store = _store(tmp_path)
    store.save_system_event(
        "run-1",
        "funding_workflow_completed",
        {"workflow_id": "wf-a", "request_id": "req-1", "phase": "funding"},
    )
    assert backfill_missing_legacy_terminals(store) == 1
    assert backfill_missing_legacy_terminals(store) == 0
    assert rollback_preflight(store)["completed_without_legacy"] == []
    acks = store.list_system_events_by_type("contribution_funding_request_ack", limit=None)
    assert acks[0]["payload"]["request_id"] == "req-1"
```

CLI 레벨:

```python
def test_the_preflight_refuses_while_writers_are_live(monkeypatch, config_file):
    monkeypatch.setattr(cli, "_service_is_active", lambda unit: True)
    result = runner.invoke(
        cli.app, ["approval-rollback-preflight", "--config", str(config_file), "--require-quiesce"]
    )
    assert result.exit_code == 1
    assert "reason=writers_still_running" in result.stdout


def test_the_preflight_names_every_failing_check(monkeypatch, config_file, store_of):
    monkeypatch.setattr(cli, "_service_is_active", lambda unit: False)
    store = store_of(config_file)
    store.save_system_event(
        "run-1", "telegram_approval_ack", {"approval_id": "ap-1", "schema_version": 2}
    )
    result = runner.invoke(
        cli.app, ["approval-rollback-preflight", "--config", str(config_file)]
    )
    assert result.exit_code == 1
    assert "check=acked_without_resolution" in result.stdout
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_rollback_preflight.py -q`
Expected: FAIL — `ImportError: cannot import name 'rollback_preflight'`

- [ ] **Step 3: 최소 구현**

```python
def rollback_preflight(store: "StateStore") -> dict[str, list[str]]:
    """구버전으로 되돌려도 안전한지 판정한다. 검사 4종은 서로 독립이다.

    quiesce 장벽 아래에서만 의미가 있다: 검사 직후 구버전 기동 전까지
    callback·sweep·예약 run이 새 미완 상태를 만들 수 있으므로, 정지 없이
    통과한 결과는 통과가 아니다.
    """
    incomplete = sorted(row["request_id"] for row in list_incomplete_workflows(store))

    dispatched = {
        str((row.get("payload") or {}).get("signal_run_id"))
        for row in store.list_system_events_by_type("signal_approval_pending", limit=None)
    }
    consumed = sorted(
        {
            str((row.get("payload") or {}).get("signal_run_id"))
            for row in store.list_system_events_by_type("signal_package_consumed", limit=None)
        }
        - dispatched
    )

    resolved = {
        str((row.get("payload") or {}).get("approval_id"))
        for row in store.list_system_events_by_type(
            "telegram_approval_resolution_completed", limit=None
        )
    }
    acked = sorted(
        {
            str((row.get("payload") or {}).get("approval_id"))
            for row in store.list_system_events_by_type("telegram_approval_ack", limit=None)
            if isinstance((row.get("payload") or {}).get("schema_version"), int)
        }
        - resolved
    )

    missing_legacy = sorted(
        request_id for request_id, _ in _completed_without_legacy_terminal(store)
    )
    return {
        "incomplete_workflows": incomplete,
        "consumed_without_dispatch": consumed,
        "acked_without_resolution": acked,
        "completed_without_legacy": missing_legacy,
    }


def _completed_without_legacy_terminal(store: "StateStore") -> list[tuple[str, str]]:
    """dual-write 규약을 어긴 종결. (request_id, phase) 목록."""
    legacy_by_phase = {
        phase: {
            str((row.get("payload") or {}).get("request_id"))
            for row in store.list_system_events_by_type(event_type, limit=None)
        }
        for phase, event_type in LEGACY_TERMINAL_EVENT.items()
    }
    broken: list[tuple[str, str]] = []
    for row in store.list_system_events_by_type("funding_workflow_completed", limit=None):
        payload = row.get("payload") or {}
        phase = str(payload.get("phase") or "funding")
        request_id = str(payload.get("request_id") or "")
        if request_id and request_id not in legacy_by_phase.get(phase, set()):
            broken.append((request_id, phase))
    return broken


def backfill_missing_legacy_terminals(store: "StateStore") -> int:
    """구버전이 읽을 종결 이벤트를 뒤늦게 채운다. 스펙이 지시한 유일한
    preflight 쓰기다 — 이것 없이 롤백하면 구버전이 완료된 요청을 pending
    으로 오판해 run_signal과 현금흐름 처리를 재실행한다."""
    written = 0
    for request_id, phase in _completed_without_legacy_terminal(store):
        outcome = store.save_system_events_atomic(
            "rollback-preflight",
            [
                {
                    "event_type": LEGACY_TERMINAL_EVENT[phase],
                    "payload": {
                        "duplicate_key": f"preflight-legacy-terminal:{phase}:{request_id}",
                        "request_id": request_id,
                        "status": "confirmed" if phase == "funding" else "selected",
                        "backfilled_by": "rollback_preflight",
                    },
                }
            ],
        )
        if outcome["committed"]:
            written += 1
    return written
```

import 추가: `from maestro.state.funding_workflow import list_incomplete_workflows`

`cli.py`의 `approval_rollback_preflight` 본문을 교체:

```python
    if require_quiesce:
        live = _active_writer_units()
        if live:
            typer.echo(
                "approval_rollback_preflight status=fail reason=writers_still_running "
                f"units={','.join(live)}"
            )
            raise typer.Exit(1)
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    backfill_missing_legacy_terminals(store)
    findings = rollback_preflight(store)
    failing = {check: ids for check, ids in findings.items() if ids}
    if not failing:
        typer.echo("approval_rollback_preflight status=safe unresolved=0")
        return
    total = 0
    for check, ids in sorted(failing.items()):
        total += len(ids)
        for identifier in ids:
            typer.echo(f"approval_rollback_preflight status=unsafe check={check} id={identifier}")
    typer.echo(f"approval_rollback_preflight status=unsafe unresolved={total}")
    raise typer.Exit(1)
```

`--require-quiesce`의 기본값을 `True`로 바꾼다 — 정지 없이 통과한 preflight는 통과가 아니다.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_rollback_preflight.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/maestro/state/upgrade_backfill.py src/maestro/cli.py tests/test_rollback_preflight.py
git commit -m "feat(3a-5): expand the rollback preflight to all four unfinished states"
```

---

### Task 8: 구버전 DB fixture 업그레이드 검증

**Files:**
- Create: `tests/fixtures/legacy_3a_state.sql`
- Create: `tests/test_upgrade_backfill_fixture.py`
- Test: 위 파일

**Interfaces:**
- Consumes: Task 2~5
- Produces: `load_legacy_fixture(tmp_path) -> StateStore` — fixture를 적용한 store

**설계 메모:** 스펙 승인 조건(819~825행)이 요구하는 것: 정상 pending 요청 유지, 완료된 승인 재집행 없음, 모호 케이스 격리, 그리고 **실패 주입** — 단계 사이 crash 후 같은 cutoff로 멱등 재개, `completed` 마커 이전의 신규 스키마 write 차단.

- [ ] **Step 1: fixture와 실패하는 테스트 작성**

`tests/fixtures/legacy_3a_state.sql`은 3a 이전 DB를 흉내낸다. 스키마는 `StateStore._init_db`가 만들어 주므로 fixture는 **행만** 넣는다:

```sql
-- 3a 이전 상태 스냅샷. head/claim/resolution_completed가 하나도 없다.
INSERT INTO system_events (run_id, event_type, payload) VALUES
  ('sig-legacy-1', 'contribution_funding_request',
   '{"request_id":"req-keep","contribution_group_id":"core","account_id":"acct-1","execution_sleeve":"krw","currency":"KRW","month_key":"2026-07","status":"pending","strategy_ids":["s1"]}'),
  ('sig-legacy-2', 'contribution_funding_request',
   '{"request_id":"req-dup-a","contribution_group_id":"core","account_id":"acct-2","execution_sleeve":"krw","currency":"KRW","month_key":"2026-07","status":"pending","strategy_ids":["s1"]}'),
  ('sig-legacy-2', 'contribution_funding_request',
   '{"request_id":"req-dup-b","contribution_group_id":"core","account_id":"acct-2","execution_sleeve":"krw","currency":"KRW","month_key":"2026-07","status":"pending","strategy_ids":["s1"]}'),
  ('sig-legacy-3', 'telegram_approval_ack', '{"approval_id":"ap-done","decision":"approved"}'),
  ('sig-legacy-3', 'signal_approval_completed', '{"signal_run_id":"sig-legacy-3"}'),
  ('sig-legacy-4', 'telegram_approval_ack', '{"approval_id":"ap-never","decision":"approved"}');

INSERT INTO approvals (approval_id, signal_run_id, status)
VALUES ('ap-done', 'sig-legacy-3', 'approved');
```

`approvals` 테이블의 실제 컬럼 목록은 `store.py`의 `CREATE TABLE approvals`에서 확인해 맞춘다.

```python
# tests/test_upgrade_backfill_fixture.py
from pathlib import Path

from maestro.state.funding_workflow import funding_workflow_id
from maestro.state.store import StateStore
from maestro.state.upgrade_backfill import (
    backfill_legacy_approvals,
    backfill_legacy_heads,
    complete_migration,
    migration_is_complete,
    start_migration,
)

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_3a_state.sql"


def load_legacy_fixture(tmp_path) -> StateStore:
    store = StateStore(tmp_path / "state.db")
    with store._connect() as conn:  # noqa: SLF001 - fixture setup needs raw SQL
        conn.executescript(FIXTURE.read_text(encoding="utf-8"))
    return store


def _run_backfill(store) -> tuple[int, dict, dict]:
    cutoff = start_migration(store)
    return cutoff, backfill_legacy_heads(store, cutoff), backfill_legacy_approvals(store, cutoff)


def test_a_lone_legacy_pending_request_survives_with_a_head(tmp_path):
    store = load_legacy_fixture(tmp_path)
    _run_backfill(store)
    workflow_id = funding_workflow_id(
        contribution_group_id="core",
        account_id="acct-1",
        execution_sleeve="krw",
        currency="KRW",
        month_key="2026-07",
    )
    head = store.load_funding_workflow_head(workflow_id)
    assert head["request_id"] == "req-keep"


def test_the_ambiguous_pending_pair_is_quarantined_and_keeps_no_head(tmp_path):
    store = load_legacy_fixture(tmp_path)
    _, heads, _ = _run_backfill(store)
    assert heads["quarantined"] == 1
    workflow_id = funding_workflow_id(
        contribution_group_id="core",
        account_id="acct-2",
        execution_sleeve="krw",
        currency="KRW",
        month_key="2026-07",
    )
    assert store.load_funding_workflow_head(workflow_id) is None


def test_a_completed_legacy_approval_is_not_re_executed(tmp_path):
    store = load_legacy_fixture(tmp_path)
    _run_backfill(store)
    assert store.approval_resolution_exists("ap-done") is True
    completed = [
        row["payload"]
        for row in store.list_system_events_by_type(
            "telegram_approval_resolution_completed", limit=None
        )
        if row["payload"]["approval_id"] == "ap-done"
    ]
    assert completed[0]["status"] == "executed"


def test_an_approval_with_no_execution_evidence_is_closed_as_canceled(tmp_path):
    store = load_legacy_fixture(tmp_path)
    _run_backfill(store)
    completed = [
        row["payload"]
        for row in store.list_system_events_by_type(
            "telegram_approval_resolution_completed", limit=None
        )
        if row["payload"]["approval_id"] == "ap-never"
    ]
    assert completed[0]["status"] == "canceled"


def test_a_crash_between_the_two_backfill_steps_resumes_on_the_same_cutoff(tmp_path):
    store = load_legacy_fixture(tmp_path)
    cutoff = start_migration(store)
    backfill_legacy_heads(store, cutoff)
    # crash here; a new process restarts and must not move the boundary.
    resumed = start_migration(store)
    assert resumed == cutoff
    backfill_legacy_approvals(store, resumed)
    heads = store.list_system_events_by_type("funding_workflow_head", limit=None)
    assert len(heads) == 1


def test_the_whole_backfill_is_idempotent_end_to_end(tmp_path):
    store = load_legacy_fixture(tmp_path)
    _run_backfill(store)
    before = len(store.list_system_events(limit=10_000))
    _run_backfill(store)
    assert len(store.list_system_events(limit=10_000)) == before


def test_the_migration_stays_incomplete_while_a_case_is_quarantined(tmp_path):
    store = load_legacy_fixture(tmp_path)
    cutoff, heads, approvals = _run_backfill(store)
    assert heads["quarantined"] + approvals["quarantined"] > 0
    # The CLI refuses to write the marker in this state (Task 5); assert the
    # invariant the running code actually depends on.
    assert migration_is_complete(store) is False
    complete_migration(store, cutoff)
    assert migration_is_complete(store) is True
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_upgrade_backfill_fixture.py -q`
Expected: FAIL — fixture 파일 없음 / `FileNotFoundError`

- [ ] **Step 3: fixture 정렬**

fixture의 컬럼·JSON을 실제 스키마에 맞을 때까지 조정한다. `store._connect()`가 `sqlite3.Connection`을 돌려주지 않으면 그 클래스가 제공하는 raw 커넥션 접근 방식을 쓴다.

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_upgrade_backfill_fixture.py -q`
Expected: PASS (7 tests)

- [ ] **Step 5: 커밋**

```bash
git add tests/fixtures/legacy_3a_state.sql tests/test_upgrade_backfill_fixture.py
git commit -m "test(3a-5): verify the upgrade against a pre-3a database snapshot"
```

---

### Task 9: 운영 문서

**Files:**
- Create: `docs/rollback_and_upgrade_3a.md`
- Modify: `docs/operator_runbook.md`
- Test: 없음 (문서)

**Interfaces:**
- Consumes: Task 1·5·7의 명령과 출력 형식
- Produces: 운영자가 따라갈 수 있는 절차 문서

- [ ] **Step 1: 문서 작성**

`docs/rollback_and_upgrade_3a.md`에 다음을 담는다:

- **quiesce unit 목록** — `WRITER_UNITS`와 정확히 같은 목록. 이 두 곳이 어긋나면 장벽이 뚫린다는 경고 포함.
- **업그레이드 절차** 5단계: (1) 모든 unit 정지 + `systemctl is-active`로 전부 inactive 확인, (2) `maestro upgrade-backfill --config <path>` 실행, (3) `status=quarantined`면 격리 이벤트(`funding_workflow_migration_quarantine`, `telegram_approval_migration_quarantine`)를 조회해 수동 종결 후 재실행, (4) `status=safe` 확인, (5) unit 재개.
- **롤백 절차** 5단계: (1) quiesce, (2) `maestro approval-rollback-preflight --config <path>` (기본이 `--require-quiesce`), (3) `status=unsafe`면 **롤백하지 않는다** — `check=` 별 대응 방법 표, (4) `status=safe` 확인 후 unit 정지 상태 그대로 구버전 배포, (5) 구버전 기동 확인 후 재개.
- **`check=` 대응 표**:
  - `incomplete_workflows` → 복구 카드 [재개]로 종결하거나, 집행 증거가 없으면 취소 종결
  - `consumed_without_dispatch` → 해당 signal run의 dispatch를 재개해 `signal_approval_pending`을 남긴다
  - `acked_without_resolution` → sweep이 재개하도록 두거나 `maestro approval-settle`로 종결
  - `completed_without_legacy` → preflight가 자동 backfill한다. 그래도 남으면 phase 판정이 깨진 것이므로 조사 대상
- **왜 절차 순서가 강제인지**: preflight를 정지 없이 실행하면 검사 직후 구버전 기동 전까지 callback·sweep·예약 run이 새 미완 상태를 만들어 통과가 무의미해진다.
- **roll-forward-only 원칙**: 3a 이후 버그 대응은 수정 배포로만. 롤백은 위 절차를 통과했을 때만.

`docs/operator_runbook.md`에 링크 한 줄을 추가한다.

- [ ] **Step 2: 검증**

```bash
grep -c "maestro upgrade-backfill" docs/rollback_and_upgrade_3a.md
grep -c "rollback_and_upgrade_3a" docs/operator_runbook.md
```
Expected: 둘 다 1 이상

- [ ] **Step 3: 전체 검증과 커밋**

```bash
pytest -q
ruff check src tests
git add docs/rollback_and_upgrade_3a.md docs/operator_runbook.md
git commit -m "docs(3a-5): document the quiesce-gated upgrade and rollback procedures"
```

---

## 검증

- `pytest -q` — 신규 5개 테스트 파일 포함 전부 통과. `tests/test_signal_approval_handoff.py`의 `test_daily_signal_approval_*` 5건은 root 소유 `/tmp` lock으로 인한 **선행 실패**이며 이 계획과 무관하다(`docs/superpowers/plans/2026-08-15-approval-dispatch-resume.md:226`).
- `ruff check src tests` — 통과.
- 스펙 승인 조건 대응:
  - "구버전 DB fixture로 업그레이드 backfill 검증: pending 유지, 완료된 승인 재집행 없음, 모호 케이스 격리" → Task 8
  - "실패 주입: 단계 사이 crash 후 같은 cutoff로 멱등 재개" → Task 8
  - "구·신 버전 동시 기동 시 completed 마커 이전의 신규 스키마 write·sweep 차단" → Task 6
  - "quiesce 없이 진행할 경우의 경계 오염 검출" → Task 1·5 (장벽 미충족 시 exit 1)
  - "preflight가 3종 미완 상태를 모두 검출하고 quiesce 아래에서만 유효하도록 절차화" → Task 7·9
  - "완료된 요청이 있는 DB를 롤백해도 dual-write 덕분에 구버전이 pending으로 오판하지 않는다" → 3a-4 Task 7 + 이 계획 Task 7의 `backfill_missing_legacy_terminals`

운영 확인 (컷오버 후):

1. 스테이징 DB 사본에 fixture 대신 **실제 운영 DB 스냅샷**을 넣고 `maestro upgrade-backfill --no-require-quiesce`를 돌려 격리 건수를 먼저 센다. 격리가 나오면 운영 정지 전에 처리 방법을 정한다 — 정지 시간이 격리 조사 시간만큼 늘어나는 것을 막는다.
2. 실제 업그레이드에서는 `--require-quiesce`(기본)로 돌리고 `status=safe`를 눈으로 확인한 뒤에만 unit을 재개한다.
3. 재개 후 첫 사이클에서 `funding_workflow_head`가 새 요청마다 기록되는지, `funding_workflow_superseded`가 legacy 요청에 붙지 않았는지 확인한다.

## 명시적으로 하지 않는 것

- **capacity 차단 경로로 고아가 된 승인 카드의 무력화 (이월 5번)** — 이 계획은 **탐지까지만** 한다. Task 7의 `consumed_without_dispatch`는 `signal_package_consumed`가 있고 `signal_approval_pending`이 없는 package를 잡는데, 재개 중 capacity가 전부를 막는 경로(`orchestrator.py:984-1004`)는 `signal_approval_completed`만 쓰고 `signal_approval_pending`은 쓰지 않으므로 **이 검사에 정확히 잡힌다.** 따라서 `2026-08-15-approval-dispatch-resume.md:296-299`가 "그 preflight에 잡히지 않는다"고 적은 것은 이 구현에는 해당하지 않으며, 그 문서의 해당 문단은 3a-5 머지 시 정정해야 한다. 다만 preflight가 unsafe로 보고하는 것과, 살아 있는 승인 카드를 원자적으로 **무력화**하는 것은 다른 일이다. 후자는 3a-3 후속 웨이브의 별도 계획서다.
- **legacy 요청의 자동 supersede** — 모호 그룹은 끝까지 운영자 판단으로 남긴다. 자동 선택 로직을 넣지 않는다.
- **월간 자금 카드 UI와 격리 카드 렌더** — 3b·4b. 이 단계의 격리는 system event와 CLI 출력으로만 노출된다.
- **quiesce 자동화(systemctl 정지·기동 스크립트)** — 문서화된 수동 절차로 둔다. 장벽을 자동으로 내리는 코드는 그 자체가 장벽을 뚫는 writer가 될 수 있다.
- **`save_approval` 멱등화와 자동 재개 게이트 완화 (이월 B)** — 이 계획이 intent 완전성 경계를 저장·검사 가능하게 만들지만, 게이트를 넓히는 것은 별건이다.

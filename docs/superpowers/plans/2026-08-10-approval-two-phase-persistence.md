# 승인 결정 2단계 영속화 (단계 3a-1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 운영자의 승인이 기록됐지만 주문 집행이 실패하면 승인이 영구 유실되는 결함을 없앤다 — ack를 종결이 아닌 "결정 기록"으로 재정의하고, `resolution_completed`가 있어야만 종결로 판정하며, 미완 승인은 기록된 결정으로 자동 재개한다.

**Architecture:** 기존 `telegram_approval_ack` 이벤트는 그대로 두되 payload에 `schema_version: 2`를 추가해 신·구 이벤트를 구분한다. 새 이벤트 `telegram_approval_resolution_completed`를 도입하고, "이 승인은 끝났는가" 판정을 **한 곳**(`_terminal_approval_ids`)으로 모아 `_pending_async_approval` / `_sweep_pending_approvals` / `_approvals`가 공유한다. schema_version이 없는 legacy ack는 종결로 간주하므로 DB 마이그레이션·cutoff 마커 없이 배포된다. 미완 승인의 재개는 sweep이 attempt 기반 duplicate_key로 한 건만 진입시키고, 이미 주문이 나간 승인은 자동 재개하지 않고 ⚠️ 카드로 운영자에게 넘긴다.

**Tech Stack:** Python 3.12, pydantic v2, SQLite(`StateStore`), pytest, ruff. 신규 의존성 없음.

## Global Constraints

- 스펙: `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md` 3a 항목 7 (승인 결정의 2단계 영속화). 본 계획은 그 항목만 구현한다.
- **접근 A 예외 범위**: 이 작업은 "비즈니스 로직 불변" 원칙의 명시적 예외다. 단, 예외는 승인 종결 판정과 재개 경로에 한정하며 주문 생성·리스크 게이트 로직은 건드리지 않는다.
- **roll-forward-only**: 3a 배포 이후 롤백은 quiesce 장벽 + preflight를 요구한다. 본 계획의 코드는 legacy ack를 종결로 인정하므로 **구버전으로 롤백해도 완료된 승인을 재실행하지 않는다** — 이 성질을 Task 7에서 테스트로 고정한다.
- 모든 사용자 노출 문구는 한글이며 `src/maestro/integrations/telegram/ui/catalog.py`에만 정의한다 (단계 1에서 세운 규칙).
- system event 중복 방지는 payload의 `duplicate_key` 필드로만 한다 (`StateStore`가 `system_events.duplicate_key` UNIQUE 인덱스로 강제).
- 검증 명령: `.venv/bin/python -m pytest tests/ -q` (기준선 **1251 passed, 9 skipped**), `.venv/bin/python -m ruff check src tests --output-format=concise`.
- 커밋 트레일러:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Ud76J4vJYANjQVUMNnFqEK
  ```

## 배경: 실제로 발생한 사고

2026-08-07 13:47:14 운영 DB에 남은 기록:

```
telegram_approval_ack                {"approval_id": "appr_508786c5...", "status": "approved", ...}
telegram_approval_resolution_failed  {"error_message": "Signal package stale broker snapshot:
                                      account_id=toss_brokerage age_seconds=1025 max_age_seconds=900"}
```

운영자가 $25,558 규모 승인을 눌렀고 ack가 저장됐지만, 스냅샷이 125초 만료돼 주문이 하나도 생성되지 않았다. `_pending_async_approval`이 ack 존재만으로 종결 판정하므로 재클릭도 "이미 처리됐거나 만료된 요청이에요"로 거절됐다. 그 달의 해외 리밸런싱이 조용히 누락됐다.

## 현재 코드의 사실관계 (구현 전 확인된 것)

- `src/maestro/integrations/telegram/handlers.py:1469` `_resolve_async_approval` — ack 저장(`duplicate_key = telegram-approval-ack:<id>`) → config 재로드 → `MaestroOrchestrator.resolve_pending_signal_approval()` 호출 → 예외 시 `telegram_approval_resolution_failed` 기록 후 **재raise**. ack는 이미 저장돼 있다.
- ack를 종결로 보는 곳이 **세 군데**로 흩어져 있다: `_pending_async_approval` (handlers.py:4068), `_sweep_pending_approvals` (handlers.py:1528), `_approvals` (handlers.py:3540). 셋 다 `list_system_events_by_type("telegram_approval_ack", limit=2000)`를 각자 호출한다.
- `src/maestro/orchestration/orchestrator.py:298` `resolve_pending_signal_approval` — `live_order_lock` 안에서 검증 → `save_approval` → 주문 실행 → `signal_approval_completed` 기록. `StateStore.save_approval` (`state/store.py:1138`)은 같은 approval_id면 `ValueError("Approval decision already exists")`를 던지므로 **재개 시 여기서 막힌다**.
- 실패 지점 분포: 운영 사고처럼 `_validate_signal_package_for_approval` / `_validate_signal_approval_preconditions`에서 나는 실패는 주문 생성 **이전**이라 부작용이 없다. 반면 `_execute_live_approval_orders` 도중 실패는 일부 주문이 브로커에 나간 뒤일 수 있다.
- 주문 집행 흔적은 `live_order_lifecycle` system event에 **order_id 기준**으로 남는다 (`state/store.py:1444` `_list_system_events_by_order_id`, private). approval_id로 직접 조회하는 API는 없다.

## 범위 결정 (명시적으로 하지 않는 것)

**부분 집행된 승인은 자동 재개하지 않는다.** envelope의 order_id 중 하나라도 `live_order_lifecycle` 기록이 있으면 sweep은 재개하지 않고 ⚠️ 확인 필요 카드로 운영자에게 라우팅한다. 주문 단위 멱등 재개(이미 나간 주문 재사용, 부분 체결 이어받기)는 dispatch·주문 계층 작업이며 단계 3a-3에서 다룬다. 이 계획이 자동으로 재개하는 것은 **부작용이 전혀 없는 실패**(검증 단계 실패, config 로드 실패, ack 직후 프로세스 종료)뿐이며, 운영 사고가 정확히 이 유형이다.

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `src/maestro/state/store.py` | `approval_exists` 활용한 get-or-create, order_id 기준 집행 흔적 공개 조회 | 수정 (메서드 2개 추가) |
| `src/maestro/orchestration/orchestrator.py` | `resolve_pending_signal_approval`의 재진입 허용 | 수정 (`save_approval` 분기) |
| `src/maestro/integrations/telegram/handlers.py` | ack에 schema_version, completed 기록, 종결 판정 단일화, sweep 재개 | 수정 (메서드 3개 추가, 3곳 치환) |
| `src/maestro/integrations/telegram/ui/catalog.py` | 재개·확인 필요 문구 | 수정 (상수 3개 추가) |
| `tests/test_telegram_approval_resume.py` | 2단계 영속화·재개·멱등·legacy 호환 | **신규** |
| `tests/test_telegram_operator_ui.py` | 기존 승인 콜백 회귀 | 수정 (판정 변경분) |

---

### Task 1: 종결 판정을 한 곳으로 모은다 (동작 변화 없음)

지금은 ack 조회가 세 군데에 복제돼 있어, 판정 규칙을 바꾸면 한 곳을 빠뜨리기 쉽다. 먼저 **동작을 바꾸지 않고** 헬퍼로 모은다.

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py` (`_pending_async_approval` 4068행, `_sweep_pending_approvals` 1528행, `_approvals` 3540행)
- Test: `tests/test_telegram_approval_resume.py` (신규)

**Interfaces:**
- Produces: `TelegramOperatorCommandRouter._terminal_approval_ids() -> set[str]` — 종결된 approval_id 집합. 이후 모든 태스크가 이 메서드 하나만 고친다.

- [ ] **Step 1: 테스트 헬퍼와 실패하는 테스트를 쓴다**

`tests/test_telegram_approval_resume.py`를 새로 만든다. `tests/test_telegram_operator_ui.py`는 config 픽스처(`_telegram_config_path`, `_telegram_signal_config_path`)와 `FakeTelegramClient`를 자체 정의해 쓰고 라우터 조립을 매 테스트에서 반복한다 (`tests/test_telegram_operator_ui.py:117-128`). 그 파일에서 import 하지 말고 — 테스트 파일 간 결합을 만들지 않는다 — 같은 구조의 헬퍼를 이 파일에 정의한다.

이후 모든 태스크의 테스트가 아래 헬퍼만 쓴다. 다른 이름을 새로 만들지 않는다.

```python
from datetime import UTC, datetime, timedelta

import pytest
import yaml

from maestro.approval.models import ApprovalRequest, PendingApprovalEnvelope
from maestro.audit.logger import AuditLogger
from maestro.config.loader import load_config
from maestro.integrations.telegram.handlers import TelegramOperatorCommandRouter
from maestro.state.store import StateStore

# config 픽스처는 tests/test_telegram_operator_ui.py의 _telegram_config_path와
# 같은 방식으로 만든다 (그 함수 본문을 이 파일에 복사해 온다).


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return {"result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, offset=None, timeout=0):
        return []

    def answer_callback_query(self, callback_query_id, text=""):
        return {"ok": True}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return {"ok": True}


class _StubRouter(TelegramOperatorCommandRouter):
    """resolve_pending_signal_approval만 대체한 라우터.

    실제 orchestrator를 띄우지 않고 집행 성공/실패를 제어한다.
    """

    def __init__(self, *args, resolve_error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolve_error = resolve_error
        self.resolved_decisions = []

    def _run_resolution(self, envelope, decision):
        self.resolved_decisions.append(decision)
        if self._resolve_error is not None:
            raise self._resolve_error
        return SignalApprovalSummary(
            signal_run_id=envelope.signal_run_id,
            run_id=envelope.run_id,
            orders_created=len(envelope.orders),
            orders_submitted=len(envelope.orders),
            approval_status=decision.status,
        )


def _router(tmp_path, *, resolve_error=None):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    router = _StubRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=FakeTelegramClient(),
        resolve_error=resolve_error,
    )
    return router, store


def _save_pending_envelope(store, *, approval_id, order_count=1):
    now = datetime.now(UTC)
    orders = [
        {
            "order_id": f"ord_{approval_id}_{index}",
            "symbol": "069500",
            "side": "buy",
            "quantity": 10,
            "notional": 712_000.0,
        }
        for index in range(order_count)
    ]
    envelope = PendingApprovalEnvelope(
        approval_id=approval_id,
        run_id=f"run_{approval_id}",
        signal_run_id=f"signal_{approval_id}",
        request=ApprovalRequest(
            approval_id=approval_id,
            run_id=f"run_{approval_id}",
            created_at=now,
            expires_at=now + timedelta(hours=1),
            channel="telegram",
            order_count=len(orders),
            estimated_notional=sum(order["notional"] for order in orders),
            proposed_orders=orders,
        ),
        orders=orders,
        message="카드 본문",
        source_strategy_ids=["tranquillo"],
        account_ids=["kis_ps"],
        reminder_seconds=[],
        created_at=now,
        expires_at=now + timedelta(hours=1),
        duplicate_key=f"telegram-approval-pending:{approval_id}",
    )
    store.save_system_event(
        envelope.run_id, "telegram_approval_pending", envelope.model_dump(mode="json")
    )
    return envelope


def _save_ack(store, *, approval_id, status, schema_version=None):
    payload = {
        "approval_id": approval_id,
        "signal_run_id": f"signal_{approval_id}",
        "status": status,
        "decided_by": "telegram:tester",
        "decided_at": datetime.now(UTC).isoformat(),
        "duplicate_key": f"telegram-approval-ack:{approval_id}",
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version
    store.save_system_event(f"run_{approval_id}", "telegram_approval_ack", payload)


def _save_completed(store, *, approval_id):
    store.save_system_event(
        f"run_{approval_id}",
        "telegram_approval_resolution_completed",
        {
            "approval_id": approval_id,
            "status": "approved",
            "attempt": 1,
            "duplicate_key": f"telegram-approval-completed:{approval_id}",
        },
    )


def _save_live_order_lifecycle(store, *, order_id):
    store.save_system_event(
        "run_x",
        "live_order_lifecycle",
        {"order_id": order_id, "final_status": "filled"},
    )


def _latest_payload(store, event_type):
    rows = store.list_system_events_by_type(event_type, limit=1)
    assert rows, f"no {event_type} event"
    return rows[0]["payload"]
```

`_StubRouter._run_resolution`은 아직 존재하지 않는 훅이다 — Task 2에서 `_resolve_async_approval`이 orchestrator를 직접 호출하던 부분을 이 이름의 메서드로 뽑아낸다. Task 1 시점에는 헬퍼만 정의하고, 첫 테스트는 `_terminal_approval_ids`만 검증한다.

```python
def test_terminal_approval_ids_collects_acked_approvals(tmp_path):
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved")

    assert router._terminal_approval_ids() == {"appr_1"}
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q`
Expected: FAIL — `AttributeError: 'TelegramOperatorCommandRouter' object has no attribute '_terminal_approval_ids'`

- [ ] **Step 3: 헬퍼를 만들고 세 곳을 치환한다**

```python
    def _terminal_approval_ids(self) -> set[str]:
        """더 이상 처리하지 않을 승인 집합. 종결 판정은 여기 한 곳에서만 한다."""
        return {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_ack",
                limit=2000,
            )
        }
```

`_pending_async_approval`의 첫 루프를 다음으로 바꾼다:

```python
        if approval_id in self._terminal_approval_ids():
            return None
```

`_sweep_pending_approvals`의 `acked = {...}` 블록과 `_approvals`의 `acked = {...}` 블록을 각각 `acked = self._terminal_approval_ids()`로 바꾼다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py tests/test_telegram_operator_ui.py tests/test_telegram_approval.py -q`
Expected: PASS (신규 1건 + 기존 전부)

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "refactor: centralize approval terminal-state lookup

$(printf 'Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/session_01Ud76J4vJYANjQVUMNnFqEK')"
```

---

### Task 2: ack에 schema_version을 붙이고 completed 이벤트를 기록한다

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py` (`_resolve_async_approval` 1469행)
- Test: `tests/test_telegram_approval_resume.py`

**Interfaces:**
- Consumes: Task 1의 `_terminal_approval_ids`
- Produces: 이벤트 `telegram_approval_resolution_completed`, payload 키 `{approval_id, signal_run_id, status, orders_submitted, orders_failed, resolved_at, attempt, duplicate_key}`, `duplicate_key = f"telegram-approval-completed:{approval_id}"`. ack payload에 `schema_version: 2` 추가.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_successful_resolution_records_completed_event(tmp_path):
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_1")

    router._resolve_async_approval(
        envelope,
        status="approved",
        decided_by="telegram:tester",
        reason="test",
    )

    ack = _latest_payload(store, "telegram_approval_ack")
    assert ack["schema_version"] == 2
    completed = _latest_payload(store, "telegram_approval_resolution_completed")
    assert completed["approval_id"] == "appr_1"
    assert completed["status"] == "approved"
    assert completed["attempt"] == 1
    assert completed["duplicate_key"] == "telegram-approval-completed:appr_1"


def test_failed_resolution_records_no_completed_event(tmp_path):
    router, store = _router(tmp_path, resolve_error=ValueError("stale broker snapshot"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")

    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope,
            status="approved",
            decided_by="telegram:tester",
            reason="test",
        )

    assert _latest_payload(store, "telegram_approval_ack")["schema_version"] == 2
    assert store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=10
    ) == []
```

`_router(tmp_path, resolve_error=...)` 헬퍼는 `MaestroOrchestrator.resolve_pending_signal_approval`을 monkeypatch로 대체해 예외를 던지게 만든다. 실제 orchestrator를 띄우지 않는다.

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q -k "completed_event"`
Expected: FAIL — `KeyError: 'schema_version'`

- [ ] **Step 3: 구현한다**

`_resolve_async_approval`의 ack payload에 `"schema_version": 2`를 추가하고, `resolve_pending_signal_approval` 호출을 감싼 `try` 블록의 성공 경로에 completed 기록을 넣는다. 반환값(`SignalApprovalSummary`)을 그대로 돌려주는 구조는 유지한다.

먼저 orchestrator 호출을 `_run_resolution` 훅으로 뽑아낸다 (테스트가 이 지점만 대체한다):

```python
    def _run_resolution(
        self,
        envelope: PendingApprovalEnvelope,
        decision: ApprovalDecision,
    ) -> SignalApprovalSummary:
        config = self.config
        identity = self.config_identity
        if self.approval_config_path is not None:
            config, identity = load_config_with_identity(self.approval_config_path)
        return MaestroOrchestrator(
            config,
            telegram_client=self.client,
            config_identity=identity,
        ).resolve_pending_signal_approval(envelope, decision)
```

`SignalApprovalSummary`는 handlers.py에 아직 import되지 않았다 (`from maestro.orchestration.orchestrator import MaestroOrchestrator`만 있음, handlers.py:90). 타입 힌트용으로 같은 줄에 추가한다.

그다음 `_resolve_async_approval`의 호출부를 바꾼다:

```python
        try:
            summary = self._run_resolution(envelope, decision)
        except Exception as exc:
            ...  # 기존 telegram_approval_resolution_failed 기록 후 raise (변경 없음)
            raise
        self._record_resolution_completed(envelope, decision, summary, attempt=attempt)
        return summary
```

`_resolve_async_approval`에 `attempt: int = 1` 키워드 인자를 추가하고(Task 4의 재개가 사용), 기록 메서드를 만든다:

```python
    def _record_resolution_completed(
        self,
        envelope: PendingApprovalEnvelope,
        decision: ApprovalDecision,
        summary: SignalApprovalSummary,
        *,
        attempt: int,
    ) -> None:
        duplicate_key = f"telegram-approval-completed:{envelope.approval_id}"
        if self.store.duplicate_key_exists(duplicate_key):
            return
        save_audited_system_event(
            self.store,
            self.audit,
            envelope.run_id,
            "telegram_approval_resolution_completed",
            {
                "approval_id": envelope.approval_id,
                "signal_run_id": envelope.signal_run_id,
                "status": decision.status,
                "orders_submitted": summary.orders_submitted,
                "orders_failed": summary.orders_failed,
                "resolved_at": utc_now().isoformat(),
                "attempt": attempt,
                "duplicate_key": duplicate_key,
            },
        )
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "feat: record approval resolution completion as a separate event"
```

---

### Task 3: 종결 판정을 completed 기준으로 바꾼다 (legacy ack는 종결로 인정)

이 태스크가 결함을 실제로 고치는 지점이다.

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py` (`_terminal_approval_ids`)
- Test: `tests/test_telegram_approval_resume.py`

**Interfaces:**
- Consumes: Task 2의 `schema_version`, `telegram_approval_resolution_completed`
- Produces: 판정 규칙 — `schema_version >= 2`인 ack는 대응 completed가 있어야 종결. schema_version이 없는 ack(3a 이전 기록)는 그 자체로 종결.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_acked_but_unresolved_approval_is_not_terminal(tmp_path):
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)

    assert router._terminal_approval_ids() == set()
    assert router._pending_async_approval("appr_1") is not None


def test_legacy_ack_without_schema_version_stays_terminal(tmp_path):
    # 3a 이전에 정상 완료된 승인이 재집행되면 안 된다.
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")  # schema_version 없음

    assert router._terminal_approval_ids() == {"appr_legacy"}
    assert router._pending_async_approval("appr_legacy") is None


def test_completed_approval_is_terminal(tmp_path):
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)
    _save_completed(store, approval_id="appr_1")

    assert router._terminal_approval_ids() == {"appr_1"}
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q -k "terminal"`
Expected: FAIL — `test_acked_but_unresolved_approval_is_not_terminal`에서 `{'appr_1'} != set()`

- [ ] **Step 3: 구현한다**

```python
    def _terminal_approval_ids(self) -> set[str]:
        """더 이상 처리하지 않을 승인 집합. 종결 판정은 여기 한 곳에서만 한다.

        ack는 운영자 의사의 기록일 뿐 종결이 아니다 — 주문 집행까지 끝난
        resolution_completed가 있어야 종결이다. 단 schema_version이 없는
        ack는 3a 이전 기록이라 completed가 존재할 수 없으므로 종결로 본다
        (없으면 이미 정상 완료된 과거 승인을 전부 재집행하게 된다).
        """
        completed = {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resolution_completed",
                limit=2000,
            )
        }
        terminal = set(completed)
        for row in self.store.list_system_events_by_type("telegram_approval_ack", limit=2000):
            payload = row["payload"]
            approval_id = str(payload.get("approval_id"))
            if not isinstance(payload.get("schema_version"), int):
                terminal.add(approval_id)
        return terminal
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS. 기존 테스트 중 "ack 저장 후 pending에서 빠진다"를 단언하던 것이 있으면, 그 테스트가 ack만 저장하는지 확인하고 **completed까지 저장하도록 fixture를 고친다** — 단언(승인은 한 번만 처리된다)은 바꾸지 않는다.

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "fix: treat an approval as settled only after resolution completes"
```

---

### Task 4: 미완 승인을 sweep이 기록된 결정으로 재개한다

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py` (`_sweep_pending_approvals` 1528행)
- Test: `tests/test_telegram_approval_resume.py`

**Interfaces:**
- Consumes: Task 3의 판정, Task 2의 `_resolve_async_approval(..., attempt=)`
- Produces: `_resume_unresolved_approvals() -> None` — `_sweep_pending_approvals` 시작부에서 호출. claim duplicate_key `telegram-approval-resume:<approval_id>:a<attempt>`.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_sweep_resumes_unresolved_approval_with_recorded_decision(tmp_path):
    router, store = _router(tmp_path, resolve_error=ValueError("stale broker snapshot"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )

    router._resolve_error = None  # 다음 시도는 성공한다
    router._sweep_pending_approvals()

    completed = _latest_payload(store, "telegram_approval_resolution_completed")
    assert completed["approval_id"] == "appr_1"
    assert completed["attempt"] == 2
    # 운영자 재클릭 없이 기록된 결정을 그대로 썼다
    assert router.resolved_decisions[-1].status == "approved"
    assert router.resolved_decisions[-1].decided_by == "telegram:tester"


def test_resume_enters_only_once_per_attempt(tmp_path):
    router, store = _router(tmp_path, resolve_error=ValueError("boom"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )

    router._sweep_pending_approvals()  # attempt 2 — 또 실패
    router._sweep_pending_approvals()  # attempt 3

    claims = store.list_system_events_by_type("telegram_approval_resume_claim", limit=10)
    keys = sorted(row["payload"]["duplicate_key"] for row in claims)
    assert keys == [
        "telegram-approval-resume:appr_1:a2",
        "telegram-approval-resume:appr_1:a3",
    ]


def test_repeated_resume_failures_stop_and_notify_operator(tmp_path):
    router, store = _router(tmp_path, resolve_error=ValueError("boom"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )

    for _ in range(6):
        router._sweep_pending_approvals()

    claims = store.list_system_events_by_type("telegram_approval_resume_claim", limit=20)
    assert len(claims) == 3  # attempt 2,3,4 까지만 (_MAX_RESUME_ATTEMPT = 4)
    assert any("확인이 필요" in message["text"] for message in router.client.sent_messages)
    # 확인 필요 알림은 승인당 1회만 나간다
    attention = store.list_system_events_by_type("telegram_approval_needs_attention", limit=10)
    assert len(attention) == 1


def test_partially_executed_approval_is_not_auto_resumed(tmp_path):
    router, store = _router(tmp_path, resolve_error=ValueError("broker timeout"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )
    _save_live_order_lifecycle(store, order_id=envelope.orders[0]["order_id"])

    router._sweep_pending_approvals()

    assert store.list_system_events_by_type("telegram_approval_resume_claim", limit=10) == []
    assert any(
        "확인이 필요" in message["text"] for message in router.client.sent_messages
    )
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q -k "resume or auto_resumed"`
Expected: FAIL — completed 이벤트 없음 / `telegram_approval_resume_claim` 이벤트 타입이 존재하지 않음

- [ ] **Step 3: StateStore에 집행 흔적 조회를 추가한다**

`src/maestro/state/store.py`에 공개 메서드를 추가한다 (`_list_system_events_by_order_id`는 private이라 handlers에서 쓰지 않는다):

```python
    def live_order_activity_exists(self, order_ids: Sequence[str]) -> bool:
        """주어진 주문 중 하나라도 집행 라이프사이클 기록이 있으면 True."""
        return any(
            self._list_system_events_by_order_id("live_order_lifecycle", order_id)
            for order_id in order_ids
        )
```

- [ ] **Step 4: 재개 로직을 구현한다**

`_sweep_pending_approvals` 본문 첫 줄에서 `self._resume_unresolved_approvals()`를 호출하고, 아래 메서드를 추가한다.

```python
    def _resume_unresolved_approvals(self) -> None:
        """결정은 기록됐지만 집행이 끝나지 않은 승인을 기록된 결정으로 재개한다."""
        completed = {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resolution_completed",
                limit=2000,
            )
        }
        envelopes = {
            str(row["payload"].get("approval_id")): row["payload"]
            for row in self.store.list_system_events_by_type(
                "telegram_approval_pending",
                limit=2000,
            )
        }
        for row in reversed(
            self.store.list_system_events_by_type("telegram_approval_ack", limit=2000)
        ):
            ack = row["payload"]
            approval_id = str(ack.get("approval_id"))
            if not isinstance(ack.get("schema_version"), int) or approval_id in completed:
                continue
            payload = envelopes.get(approval_id)
            if payload is None:
                continue
            envelope = PendingApprovalEnvelope.model_validate(payload)
            order_ids = [str(order.get("order_id")) for order in envelope.orders]
            if self.store.live_order_activity_exists(order_ids):
                # 일부 주문이 이미 브로커로 나갔다. 자동 재개는 중복 주문 위험이
                # 있으므로 운영자에게 넘긴다 (주문 단위 멱등 재개는 3a-3).
                self._notify_approval_needs_attention(envelope)
                continue
            attempt = self._next_resume_attempt(approval_id)
            if attempt > _MAX_RESUME_ATTEMPT:
                # 반복 실패는 자동 재시도로 풀리지 않는다. 매 poll마다 조용히
                # 실패를 쌓는 대신 운영자에게 넘긴다 (스펙 3a 항목 7).
                self._notify_approval_needs_attention(envelope)
                continue
            if not self._claim_resume(envelope, attempt):
                continue
            try:
                self._resolve_async_approval(
                    envelope,
                    status=str(ack.get("status")),
                    decided_by=str(ack.get("decided_by")),
                    reason="Resumed from recorded approval decision.",
                    attempt=attempt,
                )
            except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
                self._record_update_failure(None, exc)
```

`_resolve_async_approval`은 ack duplicate_key가 이미 존재하면 `ValueError("Approval request was already decided")`를 던지므로, **재개 호출에서는 ack 재기록을 건너뛰어야 한다.** `attempt > 1`이면 ack 저장 블록을 건너뛰도록 분기한다:

```python
        if attempt == 1:
            with self.store.writer_lock("telegram_approval_callback_claim"):
                if self.store.duplicate_key_exists(duplicate_key):
                    raise ValueError("Approval request was already decided")
                save_audited_system_event(... "telegram_approval_ack" ...)
```

보조 메서드 3개:

```python
모듈 상단 상수 영역(`TELEGRAM_EMERGENCY_COMMANDS` 근처)에 추가한다:

```python
#: 자동 재개 횟수 상한. 넘으면 ⚠️ 카드로 운영자에게 넘긴다.
_MAX_RESUME_ATTEMPT = 4
```

```python
    def _next_resume_attempt(self, approval_id: str) -> int:
        claims = [
            row
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resume_claim",
                limit=2000,
            )
            if str(row["payload"].get("approval_id")) == approval_id
        ]
        return len(claims) + 2  # 최초 콜백이 attempt 1이므로 첫 재개는 2

    def _claim_resume(self, envelope: PendingApprovalEnvelope, attempt: int) -> bool:
        """attempt 번호가 fencing token이다. 동시 sweep에서도 한 건만 진입한다."""
        duplicate_key = f"telegram-approval-resume:{envelope.approval_id}:a{attempt}"
        with self.store.writer_lock("telegram_approval_resume_claim"):
            if self.store.duplicate_key_exists(duplicate_key):
                return False
            save_audited_system_event(
                self.store,
                self.audit,
                envelope.run_id,
                "telegram_approval_resume_claim",
                {
                    "approval_id": envelope.approval_id,
                    "attempt": attempt,
                    "duplicate_key": duplicate_key,
                },
            )
        return True

    def _notify_approval_needs_attention(self, envelope: PendingApprovalEnvelope) -> None:
        duplicate_key = f"telegram-approval-attention:{envelope.approval_id}"
        if self.store.duplicate_key_exists(duplicate_key):
            return
        for chat_id in self.config.approval.telegram_allowed_chat_ids:
            self._send(chat_id, ui_catalog.APPROVAL_NEEDS_ATTENTION)
        save_audited_system_event(
            self.store,
            self.audit,
            envelope.run_id,
            "telegram_approval_needs_attention",
            {"approval_id": envelope.approval_id, "duplicate_key": duplicate_key},
        )
```

`src/maestro/integrations/telegram/ui/catalog.py`에 문구를 추가한다:

```python
APPROVAL_NEEDS_ATTENTION = (
    "⚠️ 확인이 필요해요 — 승인은 접수됐지만 주문 처리가 끝나지 않았어요.\n"
    "일부 주문이 이미 나갔을 수 있어 자동으로 다시 시도하지 않아요.\n"
    "/history에서 상태를 확인해 주세요."
)
```

- [ ] **Step 5: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add -A
git commit -m "feat: resume unresolved approvals from the recorded decision"
```

---

### Task 5: 재개가 승인 저장에서 막히지 않게 한다 (멱등화)

`resolve_pending_signal_approval`은 `save_approval`에서 `ValueError`를 던지므로, ack 이후 `save_approval`까지 성공하고 그 뒤에서 실패한 승인은 재개가 영원히 같은 예외로 실패한다.

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py:332` (`save_approval` 호출부)
- Test: `tests/test_telegram_approval_resume.py`

**Interfaces:**
- Consumes: `StateStore.approval_exists(approval_id) -> bool` (`state/store.py:1146`, 기존 API)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_resolution_can_reenter_after_approval_row_was_saved(tmp_path):
    orchestrator, store, envelope, decision = _approval_fixture(tmp_path)
    store.save_approval(envelope.run_id, envelope.approval_id, {"decision": {"status": "approved"}})

    summary = orchestrator.resolve_pending_signal_approval(envelope, decision)

    assert summary.approval_status == "approved"
```

`_approval_fixture`는 `tests/test_tranquillo_live_approval_workflow.py`가 orchestrator를 조립하는 방식을 따른다 (paper 모드, 임시 state DB).

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q -k "reenter"`
Expected: FAIL — `ValueError: Approval decision already exists: appr_...`

- [ ] **Step 3: 구현한다**

`orchestrator.py`의 `save_approval` 호출을 get-or-create로 바꾼다. audit 로그도 같은 조건으로 묶어 중복 감사 기록을 만들지 않는다.

```python
            # 재개 시 승인 행은 이미 있을 수 있다. 결정 내용은 approval_id에
            # 고정되므로 덮어쓰지 않고 건너뛴다 (기록된 결정이 원본이다).
            if not self.state_store.approval_exists(envelope.approval_id):
                self.state_store.save_approval(
                    envelope.run_id,
                    envelope.approval_id,
                    approval_payload,
                )
                self.audit.log(envelope.run_id, "approval_decision", approval_payload)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (1251 + 신규)

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "fix: allow approval resolution to re-enter after the row exists"
```

---

### Task 6: 운영 사고 시나리오를 종단 테스트로 고정한다

2026-08-07 사고를 그대로 재현해 회귀를 막는다.

**Files:**
- Test: `tests/test_telegram_approval_resume.py`

- [ ] **Step 1: 시나리오 테스트를 쓴다**

```python
def test_stale_snapshot_failure_is_recovered_on_next_poll(tmp_path):
    """2026-08-07 운영 사고: 승인 ack 직후 stale snapshot으로 집행 실패.
    구 코드에서는 재클릭도 거절돼 승인이 영구 유실됐다."""
    router, store = _router(
        tmp_path,
        resolve_error=ValueError(
            "Signal package stale broker snapshot: "
            "account_id=toss_brokerage age_seconds=1025 max_age_seconds=900"
        ),
    )
    envelope = _save_pending_envelope(store, approval_id="appr_1")

    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:u1wHK0B", reason="button"
        )

    # 실패 직후: 승인은 종결이 아니며 재개 대상이다
    assert router._terminal_approval_ids() == set()
    assert _latest_payload(store, "telegram_approval_resolution_failed")["approval_id"] == "appr_1"

    # 스냅샷이 갱신되면 다음 poll에서 자동 재개된다
    router._resolve_error = None
    router.poll_once()

    assert router._terminal_approval_ids() == {"appr_1"}
    completed = _latest_payload(store, "telegram_approval_resolution_completed")
    assert completed["status"] == "approved"
    assert completed["attempt"] == 2
```

- [ ] **Step 2: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q -k "stale_snapshot"`
Expected: PASS (Task 1~5가 이미 동작을 제공한다). 실패하면 Task 4의 sweep 진입 조건을 점검한다 — `poll_once`는 `_sweep_pending_approvals`를 try로 감싸므로 예외가 삼켜져 조용히 통과할 수 있다. 그 경우 `_record_update_failure` 호출 여부를 함께 단언한다.

- [ ] **Step 3: 커밋**

```bash
git add -A
git commit -m "test: cover the 2026-08-07 stale-snapshot approval loss scenario"
```

---

### Task 7: 롤백 안전성과 legacy 호환을 테스트로 고정한다

3a는 roll-forward-only지만, 이 계획의 변경분만큼은 롤백해도 안전해야 한다 (legacy ack 종결 인정 규칙 덕분). 그 성질을 명시적으로 고정한다.

**Files:**
- Test: `tests/test_telegram_approval_resume.py`
- Modify: `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md` (3a 항목 7에 구현 메모 추가)

- [ ] **Step 1: 테스트를 쓴다**

```python
def test_old_handler_semantics_still_settle_completed_approvals(tmp_path):
    """구버전은 ack만 보고 종결 판정한다. 새 코드가 남긴 ack에도
    status/decided_by가 그대로 있으므로 구버전이 재실행하지 않는다."""
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    router._resolve_async_approval(
        envelope, status="approved", decided_by="telegram:tester", reason="test"
    )

    legacy_acked = {
        str(row["payload"].get("approval_id"))
        for row in store.list_system_events_by_type("telegram_approval_ack", limit=2000)
    }
    assert legacy_acked == {"appr_1"}  # 구버전 판정 로직과 동일한 식


def test_mixed_legacy_and_new_events_are_classified_independently(tmp_path):
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")
    _save_pending_envelope(store, approval_id="appr_new")
    _save_ack(store, approval_id="appr_new", status="approved", schema_version=2)

    assert router._terminal_approval_ids() == {"appr_legacy"}
```

- [ ] **Step 2: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q`
Expected: PASS

- [ ] **Step 3: 스펙에 구현 메모를 남긴다**

3a 항목 7 아래에 다음을 추가한다:

```markdown
**구현 메모 (3a-1, 2026-08-10)**: 종결 판정은 `telegram_approval_ack.schema_version`
으로 신·구를 구분한다 — schema_version이 없는 ack(3a 이전)는 그 자체로 종결,
`schema_version >= 2`인 ack는 `telegram_approval_resolution_completed`가 있어야
종결이다. 별도의 cutoff 마커·DB 마이그레이션 없이 배포되며, 이 규칙 덕분에
3a-1 변경분만 단독 롤백해도 완료된 승인이 재집행되지 않는다. 부분 집행된
승인(주문에 `live_order_lifecycle` 기록이 있는 경우)은 자동 재개하지 않고
⚠️ 카드로 운영자에게 라우팅한다 — 주문 단위 멱등 재개는 3a-3의 범위다.
```

- [ ] **Step 4: 전체 검증 후 커밋**

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check src tests --output-format=concise
git add -A
git commit -m "test: pin rollback safety and legacy ack compatibility"
```

---

## 검증

- 각 태스크: 명시된 pytest 명령으로 RED 확인 → 구현 → GREEN 확인 → 커밋
- 최종: `.venv/bin/python -m pytest tests/ -q` (기준선 1251 passed / 9 skipped 이상), `.venv/bin/python -m ruff check src tests --output-format=concise` → All checks passed

## 배포 확인 (3a-1 승인 조건)

- 배포 전, 운영 DB에서 미완 승인이 없는지 확인한다 (있으면 배포 후 즉시 재개가 돌기 시작한다):
  ```bash
  .venv/bin/python -c "
  import sqlite3, json
  c = sqlite3.connect('/root/maestro-operator/var/symphony_state.db')
  acks = {json.loads(p)['approval_id'] for (p,) in c.execute(
      \"select payload from system_events where event_type='telegram_approval_ack'\")}
  done = {json.loads(p)['approval_id'] for (p,) in c.execute(
      \"select payload from system_events where event_type='signal_approval_completed'\") if 'approval_id' in json.loads(p)}
  print('ack:', len(acks))
  "
  ```
  기존 ack는 전부 schema_version이 없으므로 legacy 종결로 분류된다 — 재집행이 일어나지 않아야 정상이다.
- `systemctl restart maestro-telegram-operator.service` 후 로그에 `telegram_operator status=ok`가 이어지는지 확인.
- 다음 실제 승인 1건에서 `telegram_approval_ack`(schema_version=2)와 `telegram_approval_resolution_completed`가 **둘 다** 기록되는지 DB로 확인.
- 명령 메뉴는 이 단계에서 바뀌지 않으므로 `telegram-set-commands` 재실행은 불필요하다.

## 다음 단계

- **3a-2**: `StateStore.save_system_events_atomic` (다중 이벤트 + duplicate_key + precondition 원자 커밋)
- **3a-3**: 승인 dispatch idempotent resume (`dispatch_group_id` get-or-create, 채팅별 전송 intent) — 본 계획이 운영자에게 넘긴 "부분 집행된 승인"의 자동 처리도 여기서 다룬다
- **3a-4**: funding/budget workflow head·CAS·attempt claim·lineage·수렴 sweep
- **3a-5**: 업그레이드 backfill + 롤백 preflight CLI + 운영 문서

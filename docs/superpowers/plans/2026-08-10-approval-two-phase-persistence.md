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

**집행에 진입했을 수 있는 승인은 자동 재개하지 않는다.** 판정 기준은 **approvals 행의 존재**다 — `resolve_pending_signal_approval`은 검증 통과 직후 `save_approval()`로 행을 쓰고(`orchestrator.py:332`) **그 뒤에야** `_execute_live_approval_orders`에 진입하므로, 행이 없으면 브로커 호출이 한 번도 일어나지 않았음이 코드 순서로 증명된다.

`live_order_lifecycle` 기록 유무로 판정하면 **안 된다**: `LiveOrderLifecycleService.run`은 `submit_approved_order()`로 브로커에 먼저 제출하고(`execution/live_order_lifecycle.py:76`) `_persist_summary()`는 결과를 받은 뒤 저장한다(`:401`). 그 사이 중단되면 브로커에는 주문이 있는데 로컬 기록은 없어, "부작용 없음"으로 오판하고 같은 주문을 재제출하게 된다.

따라서 자동 재개 대상은 **approvals 행이 없는 실패**(검증 단계 실패, config 로드 실패, ack 직후 프로세스 종료)로 한정하며, 2026-08-07 운영 사고가 정확히 이 유형이다. 행이 있는 상태의 재개는 주문 단위 멱등성(제출 intent, 브로커 조회 기반 get-or-create)이 필요하므로 **3a-3**에서 다루고, 그전까지는 ⚠️ 확인 필요 알림으로 운영자에게 라우팅한다.

**legacy 승인은 자동 재집행하지 않는다.** 3a 이전에 쌓인 ack는 종결로 분류하되, 집행 증거가 없는 건은 일회성 격리 통보만 한다 (Task 4). 수일 지난 승인은 시세도 signal package도 낡아 자동 실행이 위험하다. 증거 기반 backfill과 모호 케이스 격리 카드는 **3a-5** 범위다.

## File Structure

| 파일 | 책임 | 변경 |
|---|---|---|
| `src/maestro/integrations/telegram/handlers.py` | ack에 schema_version, completed 기록, 종결 판정 단일화, sweep 재개·claim 회수·legacy 격리 | 수정 (메서드 7개 추가, 3곳 치환) |
| `src/maestro/integrations/telegram/ui/catalog.py` | 확인 필요 문구 | 수정 (상수 1개 추가) |
| `tests/test_telegram_approval_resume.py` | 2단계 영속화·재개·claim 회수·legacy 격리·롤백 안전성 | **신규** |
| `tests/test_telegram_operator_ui.py` | 기존 승인 콜백 회귀 | 수정 (판정 변경분) |

`src/maestro/state/store.py`와 `src/maestro/orchestration/orchestrator.py`는 **수정하지 않는다** — 필요한 것(`approval_exists`, `limit=None` 전건 조회)이 이미 있다.

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


def _save_stale_resume_claim(store, *, approval_id, attempt, age_seconds):
    claimed_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    store.save_system_event(
        f"run_{approval_id}",
        "telegram_approval_resume_claim",
        {
            "approval_id": approval_id,
            "run_id": f"run_{approval_id}",
            "attempt": attempt,
            "claimed_at": claimed_at.isoformat(),
            "duplicate_key": f"telegram-approval-resume:{approval_id}:a{attempt}",
        },
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
        """더 이상 처리하지 않을 승인 집합. 종결 판정은 여기 한 곳에서만 한다.

        정합성 판정이므로 조회 창을 두지 않는다 (limit=None). 기존 코드의
        limit=2000은 이벤트가 쌓이면 오래된 미완 승인을 조회 밖으로 밀어내
        재개 대상에서 조용히 사라지게 한다.
        """
        return {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_ack",
                limit=None,
            )
        }
```

`_pending_async_approval`의 첫 루프를 다음으로 바꾸고, 같은 메서드의 `telegram_approval_pending` 조회도 `limit=None`으로 바꾼다 (envelope을 못 찾으면 승인이 유실되므로 이것도 정합성 경로다):

```python
        if approval_id in self._terminal_approval_ids():
            return None
```

`_sweep_pending_approvals`의 `acked = {...}` 블록과 `_approvals`의 `acked = {...}` 블록을 각각 `acked = self._terminal_approval_ids()`로 바꾼다. `_approvals`의 `telegram_approval_pending` 조회는 화면 표시용(최근 5건)이므로 기존 limit을 유지한다.

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
                limit=None,
            )
        }
        terminal = set(completed)
        for row in self.store.list_system_events_by_type("telegram_approval_ack", limit=None):
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

### Task 4: legacy 미완 승인을 운영자에게 격리 통보

Task 3의 규칙은 legacy ack를 전부 종결로 분류한다. 그러면 3a 이전에 이미 유실된 승인(2026-08-07 사고 포함)이 **알림조차 없이 영구 봉인**된다. 자동 재집행은 하지 않되(수일 지난 승인은 시세도 signal package도 낡았다), 존재는 운영자에게 한 번 알린다.

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py`, `src/maestro/integrations/telegram/ui/catalog.py`
- Test: `tests/test_telegram_approval_resume.py`

**Interfaces:**
- Produces: `_notify_legacy_unresolved_approvals() -> None` (`_sweep_pending_approvals`에서 호출), `_notify_approval_needs_attention(envelope)` (Task 5의 재개 경로와 공유), `catalog.APPROVAL_NEEDS_ATTENTION`

**판정 기준은 집행 증거다.** `resolution_failed` 유무로 판정하면 안 된다 — `_resolve_async_approval`은 ack를 먼저 저장하고 config를 재로드하므로, ack 직후 프로세스 종료나 config 로드 실패는 `resolution_failed` **없이 ack만** 남긴다. 그런 건이야말로 브로커 제출 전에 중단됐다는 강한 증거를 가진다.

> legacy ack(`schema_version` 없음) 중 **approvals 행이 없고** 같은 `signal_run_id`의 `signal_approval_completed`도 없는 건을 격리 대상으로 본다.

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_legacy_ack_with_resolution_failure_is_isolated(tmp_path):
    """2026-08-07 형태: ack + resolution_failed, approvals 행 없음."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")
    store.save_system_event(
        "run_appr_legacy",
        "telegram_approval_resolution_failed",
        {"approval_id": "appr_legacy", "error_type": "ValueError"},
    )

    router._sweep_pending_approvals()

    assert any("확인이 필요" in message["text"] for message in router.client.sent_messages)
    assert len(store.list_system_events_by_type(
        "telegram_approval_needs_attention", limit=None
    )) == 1


def test_legacy_ack_only_crash_is_isolated(tmp_path):
    """ack 직후 프로세스 종료 / config 로드 실패: 후속 기록이 전혀 없다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")

    router._sweep_pending_approvals()

    assert any("확인이 필요" in message["text"] for message in router.client.sent_messages)


def test_legacy_ack_with_execution_evidence_is_not_isolated(tmp_path):
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")
    store.save_approval(envelope.run_id, "appr_legacy", {"decision": {"status": "approved"}})
    store.save_system_event(
        envelope.run_id,
        "signal_approval_completed",
        {"signal_run_id": envelope.signal_run_id, "approval_status": "approved"},
    )

    router._sweep_pending_approvals()

    assert store.list_system_events_by_type(
        "telegram_approval_needs_attention", limit=None
    ) == []
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q -k "legacy"`
Expected: FAIL — 알림이 나가지 않는다 (`telegram_approval_needs_attention` 이벤트 타입 없음)

- [ ] **Step 3: 구현한다**

`src/maestro/integrations/telegram/ui/catalog.py`:

```python
APPROVAL_NEEDS_ATTENTION = (
    "⚠️ 확인이 필요해요 — 승인은 접수됐지만 주문 처리가 끝나지 않았어요.\n"
    "일부 주문이 이미 나갔을 수 있어 자동으로 다시 시도하지 않아요.\n"
    "/history에서 상태를 확인해 주세요."
)
```

`handlers.py`:

```python
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

    def _notify_legacy_unresolved_approvals(self) -> None:
        """3a 이전 ack 중 집행 증거가 없는 건을 1회만 알린다. 자동 재집행은 하지 않는다."""
        completed_signal_runs = {
            str(row["payload"].get("signal_run_id"))
            for row in self.store.list_system_events_by_type(
                "signal_approval_completed",
                limit=None,
            )
        }
        envelopes = {
            str(row["payload"].get("approval_id")): row["payload"]
            for row in self.store.list_system_events_by_type(
                "telegram_approval_pending",
                limit=None,
            )
        }
        for row in self.store.list_system_events_by_type("telegram_approval_ack", limit=None):
            ack = row["payload"]
            if isinstance(ack.get("schema_version"), int):
                continue  # 신규 스키마는 재개 경로가 처리한다
            approval_id = str(ack.get("approval_id"))
            if self.store.approval_exists(approval_id):
                continue
            payload = envelopes.get(approval_id)
            if payload is None:
                continue
            envelope = PendingApprovalEnvelope.model_validate(payload)
            if envelope.signal_run_id in completed_signal_runs:
                continue
            self._notify_approval_needs_attention(envelope)
```

`_sweep_pending_approvals` 시작부에서 `self._notify_legacy_unresolved_approvals()`를 호출한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "feat: surface legacy approvals that never completed execution"
```

---

### Task 5: 미완 승인을 sweep이 기록된 결정으로 재개한다

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py` (`_sweep_pending_approvals` 1528행)
- Test: `tests/test_telegram_approval_resume.py`

**Interfaces:**
- Consumes: Task 3의 판정, Task 2의 `_resolve_async_approval(..., attempt=)`, Task 4의 `_notify_approval_needs_attention`
- Produces: `_resume_unresolved_approvals() -> None`, `_reclaim_abandoned_resume_claims() -> None`, `_next_resume_attempt(approval_id) -> int`, `_claim_resume(envelope, attempt) -> bool`, `_record_resume_finished(run_id, approval_id, attempt, outcome) -> None`. 이벤트 2종: `telegram_approval_resume_claim` (`duplicate_key = telegram-approval-resume:<id>:a<n>`), `telegram_approval_resume_finished` (`duplicate_key = telegram-approval-resume-finished:<id>:a<n>`).

**동시성 설계**: attempt 번호는 **종료 기록(`resume_finished`) 개수**에서 나온다. in-flight attempt가 있으면 다음 poll이 같은 번호를 계산해 claim duplicate_key와 충돌하므로, 기존 UNIQUE 인덱스만으로 approval당 단일 in-flight가 보장된다. claim 개수로 번호를 매기면 attempt 2가 실행 중인데 다른 프로세스가 attempt 3을 계산해 병행 진입할 수 있다 — 그렇게 하지 않는다.

**버려진 claim 회수**: claim 저장 직후·resolution 진입 전에 프로세스가 죽으면 finished도 approvals 행도 없다. 회수가 없으면 이후 모든 poll이 같은 attempt를 계산해 기존 claim과 충돌하고, 상한에도 attention 경로에도 닿지 못한 채 **영구 정지**한다. lease가 지난 미완 claim을 `outcome="abandoned"`로 종결시켜 회수한다.

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


def test_in_flight_attempt_blocks_a_second_entry(tmp_path):
    """종료 기록이 없는 attempt가 있으면 다음 진입은 같은 번호를 계산해 거절된다."""
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)
    _save_stale_resume_claim(store, approval_id="appr_1", attempt=2, age_seconds=10)

    assert router._next_resume_attempt("appr_1") == 2
    assert router._claim_resume(envelope, 2) is False

    router._sweep_pending_approvals()
    assert store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=None
    ) == []


def test_each_failed_attempt_records_a_finished_event(tmp_path):
    router, store = _router(tmp_path, resolve_error=ValueError("boom"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )

    router._sweep_pending_approvals()  # attempt 2 — 또 실패
    router._sweep_pending_approvals()  # attempt 3

    finished = store.list_system_events_by_type(
        "telegram_approval_resume_finished", limit=None
    )
    assert sorted(row["payload"]["attempt"] for row in finished) == [2, 3]
    assert {row["payload"]["outcome"] for row in finished} == {"failed"}


def test_claim_abandoned_before_resolution_is_reclaimed(tmp_path):
    """claim 직후 프로세스가 죽은 상황: lease 만료 후 재개가 이어져야 한다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)
    _save_stale_resume_claim(store, approval_id="appr_1", attempt=2, age_seconds=1000)

    router._sweep_pending_approvals()

    outcomes = [
        row["payload"]["outcome"]
        for row in store.list_system_events_by_type(
            "telegram_approval_resume_finished", limit=None
        )
    ]
    assert "abandoned" in outcomes
    completed = _latest_payload(store, "telegram_approval_resolution_completed")
    assert completed["attempt"] == 3


def test_resume_survives_more_than_2000_intervening_events(tmp_path):
    """정합성 판정이 조회 창 크기에 의존하지 않는다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)
    for index in range(2100):
        store.save_system_event(
            "run_noise",
            "telegram_approval_ack",
            {"approval_id": f"appr_noise_{index}", "status": "approved", "schema_version": 2},
        )

    assert "appr_1" not in router._terminal_approval_ids()
    router._sweep_pending_approvals()

    assert _latest_payload(
        store, "telegram_approval_resolution_completed"
    )["approval_id"] == "appr_1"


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


def test_approval_that_entered_execution_is_not_auto_resumed(tmp_path):
    """approvals 행이 있으면 브로커 제출이 일어났을 수 있다 — fail-closed."""
    router, store = _router(tmp_path, resolve_error=ValueError("broker timeout"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )
    store.save_approval(envelope.run_id, "appr_1", {"decision": {"status": "approved"}})

    router._sweep_pending_approvals()

    assert store.list_system_events_by_type(
        "telegram_approval_resume_claim", limit=None
    ) == []
    assert any(
        "확인이 필요" in message["text"] for message in router.client.sent_messages
    )
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q -k "resume or reclaim or in_flight"`
Expected: FAIL — completed 이벤트 없음 / `telegram_approval_resume_claim` 이벤트 타입이 존재하지 않음

- [ ] **Step 3: 재개 로직을 구현한다**

`_sweep_pending_approvals` 본문 첫 줄에서 `self._resume_unresolved_approvals()`를 호출하고, 아래 메서드를 추가한다.

```python
    def _resume_unresolved_approvals(self) -> None:
        """결정은 기록됐지만 집행이 끝나지 않은 승인을 기록된 결정으로 재개한다."""
        self._reclaim_abandoned_resume_claims()
        completed = {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resolution_completed",
                limit=None,
            )
        }
        envelopes = {
            str(row["payload"].get("approval_id")): row["payload"]
            for row in self.store.list_system_events_by_type(
                "telegram_approval_pending",
                limit=None,
            )
        }
        for row in reversed(
            self.store.list_system_events_by_type("telegram_approval_ack", limit=None)
        ):
            ack = row["payload"]
            approval_id = str(ack.get("approval_id"))
            if not isinstance(ack.get("schema_version"), int) or approval_id in completed:
                continue
            payload = envelopes.get(approval_id)
            if payload is None:
                continue
            envelope = PendingApprovalEnvelope.model_validate(payload)
            if self.store.approval_exists(approval_id):
                # approvals 행은 resolve_pending_signal_approval이 브로커 호출보다
                # 먼저 쓴다(orchestrator.py:332). 행이 있으면 집행에 진입했을 수
                # 있으므로 자동 재개하지 않는다 — 브로커 제출 직후·로컬 lifecycle
                # 기록 전에 중단된 창까지 fail-closed로 덮는다. (재개는 3a-3)
                self._notify_approval_needs_attention(envelope)
                continue
            attempt = self._next_resume_attempt(approval_id)
            if attempt > _MAX_RESUME_ATTEMPT:
                # 반복 실패는 자동 재시도로 풀리지 않는다. 매 poll마다 조용히
                # 실패를 쌓는 대신 운영자에게 넘긴다 (스펙 3a 항목 7).
                self._notify_approval_needs_attention(envelope)
                continue
            if not self._claim_resume(envelope, attempt):
                continue  # 같은 attempt가 in-flight다
            outcome = "failed"
            try:
                self._resolve_async_approval(
                    envelope,
                    status=str(ack.get("status")),
                    decided_by=str(ack.get("decided_by")),
                    reason="Resumed from recorded approval decision.",
                    attempt=attempt,
                )
                outcome = "completed"
            except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
                self._record_update_failure(None, exc)
            finally:
                # 종료 기록이 있어야 다음 attempt 번호가 진행된다.
                self._record_resume_finished(
                    run_id=envelope.run_id,
                    approval_id=approval_id,
                    attempt=attempt,
                    outcome=outcome,
                )

    def _reclaim_abandoned_resume_claims(self) -> None:
        """프로세스가 claim 직후 죽으면 그 attempt는 영원히 종료되지 않는다.
        lease가 지난 미완 claim을 abandoned로 종결해 재개가 이어지게 한다."""
        finished = {
            (str(row["payload"].get("approval_id")), int(row["payload"].get("attempt", 0)))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resume_finished",
                limit=None,
            )
        }
        now = utc_now()
        for row in self.store.list_system_events_by_type(
            "telegram_approval_resume_claim",
            limit=None,
        ):
            payload = row["payload"]
            approval_id = str(payload.get("approval_id"))
            attempt = int(payload.get("attempt", 0))
            if (approval_id, attempt) in finished:
                continue
            claimed_at = payload.get("claimed_at")
            if not claimed_at:
                continue
            age = (now - datetime.fromisoformat(str(claimed_at))).total_seconds()
            if age < _RESUME_LEASE_SECONDS:
                continue
            self._record_resume_finished(
                run_id=str(payload.get("run_id") or ""),
                approval_id=approval_id,
                attempt=attempt,
                outcome="abandoned",
            )
```

`_resolve_async_approval`은 ack duplicate_key가 이미 존재하면 `ValueError("Approval request was already decided")`를 던지므로, **재개 호출에서는 ack 재기록을 건너뛰어야 한다.** `attempt > 1`이면 ack 저장 블록을 건너뛰도록 분기한다:

```python
        if attempt == 1:
            with self.store.writer_lock("telegram_approval_callback_claim"):
                if self.store.duplicate_key_exists(duplicate_key):
                    raise ValueError("Approval request was already decided")
                save_audited_system_event(... "telegram_approval_ack" ...)
```

모듈 상단 상수 영역(`TELEGRAM_EMERGENCY_COMMANDS` 근처)에 추가한다:

```python
#: 자동 재개 횟수 상한. 넘으면 ⚠️ 알림으로 운영자에게 넘긴다.
_MAX_RESUME_ATTEMPT = 4
#: claim 후 이 시간이 지나도록 종료 기록이 없으면 버려진 시도로 보고 회수한다.
#: 운영자 봇 poll 간격(초 단위)과 resolution 소요(브로커 폴링 포함)를 고려한 값.
_RESUME_LEASE_SECONDS = 900
```

보조 메서드 3개:

```python
    def _next_resume_attempt(self, approval_id: str) -> int:
        """종료 기록 기준으로 번호를 매긴다. in-flight attempt가 있으면 같은
        번호가 다시 나오고, claim duplicate_key 충돌로 병행 진입이 막힌다."""
        finished = [
            row
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resume_finished",
                limit=None,
            )
            if str(row["payload"].get("approval_id")) == approval_id
        ]
        return len(finished) + 2  # 최초 콜백이 attempt 1이므로 첫 재개는 2

    def _claim_resume(self, envelope: PendingApprovalEnvelope, attempt: int) -> bool:
        """approval당 단일 in-flight를 duplicate_key UNIQUE 제약으로 보장한다."""
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
                    "run_id": envelope.run_id,
                    "attempt": attempt,
                    "claimed_at": utc_now().isoformat(),
                    "duplicate_key": duplicate_key,
                },
            )
        return True

    def _record_resume_finished(
        self,
        *,
        run_id: str,
        approval_id: str,
        attempt: int,
        outcome: str,
    ) -> None:
        """성공·실패·abandoned 세 경로가 함께 쓰는 종료 기록."""
        duplicate_key = f"telegram-approval-resume-finished:{approval_id}:a{attempt}"
        if self.store.duplicate_key_exists(duplicate_key):
            return
        save_audited_system_event(
            self.store,
            self.audit,
            run_id or f"run_{approval_id}",
            "telegram_approval_resume_finished",
            {
                "approval_id": approval_id,
                "attempt": attempt,
                "outcome": outcome,
                "finished_at": utc_now().isoformat(),
                "duplicate_key": duplicate_key,
            },
        )
```

`_notify_approval_needs_attention`과 `catalog.APPROVAL_NEEDS_ATTENTION`은 Task 4에서 이미 만들었으므로 그대로 재사용한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_resume.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add -A
git commit -m "feat: resume unresolved approvals from the recorded decision"
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

### Task 7: 롤백 위험 상태와 legacy 호환을 테스트로 고정한다

**롤백 안전성은 조건부다.** *완료된* 승인은 롤백해도 재집행되지 않는다 — legacy ack 종결 규칙이 있고, v2 ack에도 status/decided_by가 그대로 남기 때문이다. 그러나 **`schema_version=2` ack가 있고 `resolution_completed`가 없는 상태에서 롤백하면** 구버전이 ack만 보고 종결 처리해 승인된 주문이 유실된다. 이 상태를 테스트로 고정하고, 롤백 절차(배포 확인 절)가 quiesce 아래에서 이를 검사하도록 한다.

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
        for row in store.list_system_events_by_type("telegram_approval_ack", limit=None)
    }
    assert legacy_acked == {"appr_1"}  # 구버전 판정 로직과 동일한 식


def test_mixed_legacy_and_new_events_are_classified_independently(tmp_path):
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")
    _save_pending_envelope(store, approval_id="appr_new")
    _save_ack(store, approval_id="appr_new", status="approved", schema_version=2)

    assert router._terminal_approval_ids() == {"appr_legacy"}


def test_v2_ack_without_completed_is_rollback_unsafe(tmp_path):
    """이 상태에서 구버전으로 롤백하면 승인된 주문이 유실된다.
    배포 확인 절의 롤백 절차가 quiesce 아래에서 검사해야 하는 상태다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)

    # 구버전 판정식(ack만 조회)은 종결로 본다 = 롤백 시 유실
    legacy_terminal = {
        str(row["payload"].get("approval_id"))
        for row in store.list_system_events_by_type("telegram_approval_ack", limit=None)
    }
    assert legacy_terminal == {"appr_1"}
    assert router._terminal_approval_ids() == set()  # 신버전은 미완으로 본다
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
종결이다. 별도의 cutoff 마커·DB 마이그레이션 없이 배포된다.

자동 재개는 **approvals 행이 없는 승인**으로 한정한다 — `save_approval`은
모든 브로커 호출에 선행하므로(orchestrator.py:332) 행이 없으면 부작용이
없음이 증명된다. `live_order_lifecycle` 기록 유무로 판정하면 안 된다:
브로커 제출이 먼저이고 기록이 나중이라(execution/live_order_lifecycle.py:76,
:401) 그 사이 중단되면 중복 주문을 낸다. 행이 있는 상태의 재개(주문 단위
멱등성)는 3a-3 범위이며, 그전까지는 ⚠️ 알림으로 운영자에게 라우팅한다.

3a 이전 legacy ack는 자동 재집행하지 않되, 집행 증거(approvals 행 +
`signal_approval_completed`)가 없는 건은 일회성 격리 통보를 보낸다.

**롤백은 조건부로만 안전하다**: 완료된 승인은 롤백해도 재집행되지 않지만,
`schema_version=2` ack가 있고 `resolution_completed`가 없는 상태에서 롤백하면
구버전이 ack만 보고 종결 처리해 승인된 주문이 유실된다. 롤백은 quiesce →
검사 → 배포 → 재개 4단계 절차를 따르며, 검사에서 해당 상태가 하나라도
발견되면 롤백하지 않는다. 강제 CLI화는 3a-5다.
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

- 배포 직후 legacy 격리 알림이 몇 건 나갈 수 있다. 운영 DB의 기존 ack 6건 중 집행 증거가 없는 건(2026-08-07 사고 포함)이 대상이며, **자동 재집행은 일어나지 않는다**. 배포 전에 대상 건수를 미리 확인한다:
  ```bash
  .venv/bin/python -c "
  import sqlite3, json
  c = sqlite3.connect('/root/maestro-operator/var/symphony_state.db')
  def payloads(t):
      return [json.loads(p) for (p,) in
              c.execute('select payload from system_events where event_type=?', (t,))]
  acks = [p for p in payloads('telegram_approval_ack')
          if not isinstance(p.get('schema_version'), int)]
  approvals = {r[0] for r in c.execute('select approval_id from approvals')}
  print('legacy ack:', len(acks), '/ 격리 예상:',
        sorted(p['approval_id'] for p in acks if p['approval_id'] not in approvals))
  "
  ```
- `systemctl restart maestro-telegram-operator.service` 후 로그에 `telegram_operator status=ok`가 이어지는지 확인.
- 다음 실제 승인 1건에서 `telegram_approval_ack`(schema_version=2)와 `telegram_approval_resolution_completed`가 **둘 다** 기록되는지 DB로 확인.
- 명령 메뉴는 이 단계에서 바뀌지 않으므로 `telegram-set-commands` 재실행은 불필요하다.

### 롤백 절차 (순서 강제)

읽기 전용 검사만으로는 부족하다 — 서비스가 돌고 있으면 검사 직후 새 v2 ack가 기록되고 롤백되는 TOCTOU 창이 남는다. **writer를 먼저 멈추면** 검사 이후 새 상태가 생길 수 없다. 스펙이 3a 롤백에 요구하는 quiesce 순서와 같다.

```
1. quiesce   systemctl stop maestro-telegram-operator.service
             systemctl stop maestro-symphony-signal-kr.timer maestro-symphony-signal-us.timer
2. 검사      아래 쿼리 결과가 none이어야 한다.
             하나라도 있으면 롤백하지 않는다 — 해당 승인을 먼저 종결시킨다.
3. 구버전 배포  2와 3 사이에 어떤 unit도 재시작하지 않는다.
4. 재개      구버전 기동을 확인한 뒤에만 timer·service를 재시작한다.
```

```bash
.venv/bin/python -c "
import sqlite3, json
c = sqlite3.connect('/root/maestro-operator/var/symphony_state.db')
def payloads(t):
    return [json.loads(p) for (p,) in
            c.execute('select payload from system_events where event_type=?', (t,))]
acks_v2 = {p['approval_id'] for p in payloads('telegram_approval_ack')
           if isinstance(p.get('schema_version'), int)}
done = {p['approval_id'] for p in payloads('telegram_approval_resolution_completed')}
print('rollback-unsafe approvals:', sorted(acks_v2 - done) or 'none')
"
```

## 채택하지 않는 리뷰 권고 (근거 기록)

Codex 적대적 리뷰 1·2차에서 나왔으나 반영하지 않은 항목과 이유:

- **"주문 계층 멱등성(제출 intent·브로커 멱등 키)을 같은 배포에 포함"** — `approval_exists` fail-closed 조건이 중복 제출 경로를 이미 차단한다(행이 있으면 자동 재개하지 않는다). 제출 intent는 dispatch 계층 재설계와 함께 가야 하며 **3a-3** 범위다. 3a-1에 넣으면 계획이 두 배가 되고 검증 표면도 넓어진다.
- **"quiesced backfill을 3a-1 선행 조건으로"** — legacy를 자동 실행하지 않으므로 backfill 중 경계 오염이 발생할 여지가 없다. quiesce 장벽은 자동 재집행을 켜는 시점(**3a-5**)에 필요하다.
- **"DB compatibility marker로 구버전 시작 자체를 차단"** — **구현 불가능하다.** 구버전 코드에는 마커를 검사하는 로직이 없고, 이미 배포된 코드에 소급 적용할 수 없다. 강제력은 위 4단계 절차와 문서로만 확보되며, CLI화는 3a-5다.

## 다음 단계

- **3a-2**: `StateStore.save_system_events_atomic` (다중 이벤트 + duplicate_key + precondition 원자 커밋)
- **3a-3**: 승인 dispatch idempotent resume (`dispatch_group_id` get-or-create, 채팅별 전송 intent) — 본 계획이 운영자에게 넘긴 "부분 집행된 승인"의 자동 처리도 여기서 다룬다
- **3a-4**: funding/budget workflow head·CAS·attempt claim·lineage·수렴 sweep
- **3a-5**: 업그레이드 backfill + 롤백 preflight CLI + 운영 문서

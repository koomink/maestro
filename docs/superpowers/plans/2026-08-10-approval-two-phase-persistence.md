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
| `src/maestro/state/store.py` | `list_system_events_by_type`에 `since` 시간 경계 | 수정 (인자 1개 추가) |
| `src/maestro/integrations/telegram/handlers.py` | ack에 schema_version, completed 기록, 종결 판정 단일화, sweep 재개·claim 회수·legacy 격리 | 수정 (메서드 10개 추가, 3곳 치환) |
| `src/maestro/integrations/telegram/ui/catalog.py` | 확인 필요·브로커 대조 문구 | 수정 (상수 2개 추가) |
| `src/maestro/orchestration/orchestrator.py` | `signal_approval_completed`에 approval_id 기록 | 수정 (payload 1줄) |
| `src/maestro/cli.py` | 롤백 preflight 명령 | 수정 (명령 1개 추가) |
| `tests/test_state_store.py` | `since` 필터 | 수정 (테스트 1건 추가) |
| `tests/test_telegram_approval_resume.py` | 2단계 영속화·재개·claim 회수·legacy 격리·롤백 위험 상태 | **신규** |
| `tests/test_cli_approval_preflight.py` | 롤백 preflight | **신규** |
| `tests/test_telegram_operator_ui.py` | 기존 승인 콜백 회귀 | 수정 (판정 변경분) |

`save_approval`을 get-or-create로 바꾸지는 **않는다** — 자동 재개는 approvals 행이 없을 때만 일어나므로 불필요하고, "행 존재 = 집행 진입" 불변식을 약화시킨다.

---

### Task 0: 정합성 조회에 시간 경계를 도입한다

정합성 판정에 `limit=2000` 같은 **개수 창**을 쓰면 이벤트가 쌓일수록 오래된 미완 승인이 조회 밖으로 밀려 조용히 사라진다. 반대로 `limit=None` 전건 조회는 매 poll마다 전체 이력을 materialize·JSON 디코드하므로 시간이 지날수록 폴링이 무거워진다 (`poll_once`는 sweep을 `get_updates`보다 먼저 돌린다).

**개수가 아니라 시간으로 경계를 긋는다.** 승인에는 만료가 있으므로 충분히 오래된 이벤트는 정의상 재개 대상이 될 수 없고, 기존 인덱스 `idx_system_events_type_created`가 `(event_type, created_at)`이라 시간 하한은 인덱스를 그대로 탄다.

**Files:**
- Modify: `src/maestro/state/store.py` (`list_system_events_by_type` 1333행)
- Test: `tests/test_state_store.py`

**Interfaces:**
- Produces: `list_system_events_by_type(event_type, limit=10, *, since: datetime | None = None)` — 기존 호출부는 그대로 동작한다 (`since` 기본값 None).

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_list_system_events_by_type_filters_by_since(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_system_event("run_1", "telegram_approval_ack", {"approval_id": "old"})
    store.save_system_event("run_2", "telegram_approval_ack", {"approval_id": "new"})
    # 첫 이벤트만 과거로 밀어 넣는다
    with sqlite3.connect(str(tmp_path / "state.db")) as conn:
        conn.execute(
            "UPDATE system_events SET created_at = ? WHERE json_extract(payload, '$.approval_id') = 'old'",
            ((datetime.now(UTC) - timedelta(days=200)).isoformat(sep=" "),),
        )

    rows = store.list_system_events_by_type(
        "telegram_approval_ack",
        limit=None,
        since=datetime.now(UTC) - timedelta(days=90),
    )

    assert [row["payload"]["approval_id"] for row in rows] == ["new"]
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_state_store.py -q -k "since"`
Expected: FAIL — `TypeError: list_system_events_by_type() got an unexpected keyword argument 'since'`

- [ ] **Step 3: 구현한다**

```python
    def list_system_events_by_type(
        self,
        event_type: str,
        limit: int | None = 10,
        *,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM system_events WHERE event_type = ?"
        values: list[Any] = [event_type]
        if since is not None:
            # idx_system_events_type_created가 (event_type, created_at)이므로
            # 시간 하한은 인덱스를 그대로 탄다.
            # created_at은 SQLite DEFAULT CURRENT_TIMESTAMP가 쓰는
            # "YYYY-MM-DD HH:MM:SS" 문자열이다 (마이크로초·오프셋 없음,
            # list_system_events_in_range의 docstring 참조). 같은 포맷으로
            # 맞추지 않으면 초 단위로 같은 행이 >= 비교에서 탈락한다.
            sql += " AND created_at >= ?"
            values.append(since.strftime("%Y-%m-%d %H:%M:%S"))
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            values.append(limit)
        ...  # 이하 기존 본문 그대로
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_state_store.py -q`
Expected: PASS (기존 호출부는 `since=None`이라 동작이 바뀌지 않는다)

- [ ] **Step 5: handlers에 공용 헬퍼를 추가한다**

`handlers.py` 모듈 상단:

```python
#: 정합성 조회의 시간 경계. 승인 만료(config.approval.timeout_seconds)보다 훨씬
#: 길게 잡아, 만료된 지 오래된 이벤트만 조회에서 제외한다.
_CONSISTENCY_WINDOW_DAYS = 90
```

```python
    def _consistency_since(self) -> datetime:
        return utc_now() - timedelta(days=_CONSISTENCY_WINDOW_DAYS)
```

이후 태스크의 모든 정합성 조회는 `limit=2000`도 `limit=None`도 아닌 `since=self._consistency_since()`를 쓴다. 화면 표시용 조회(`_approvals`의 최근 5건 등)는 기존 limit을 유지한다.

- [ ] **Step 6: 커밋**

```bash
git add -A
git commit -m "feat: allow time-bounded system event queries"
```

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


def _save_pending_envelope(store, *, approval_id, order_count=1, signal_run_id=None):
    now = datetime.now(UTC)
    signal_run_id = signal_run_id or f"signal_{approval_id}"
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
        signal_run_id=signal_run_id,
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

        정합성 판정이므로 개수 창(limit=2000)을 쓰지 않는다 — 이벤트가 쌓이면
        오래된 미완 승인이 조회 밖으로 밀려 조용히 사라진다. 대신 Task 0의
        시간 경계를 쓴다 (인덱스를 타고, 성장에 무관하게 일정하다).
        """
        return {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_ack",
                limit=None,
            since=self._consistency_since(),
            )
        }
```

`_pending_async_approval`의 첫 루프를 다음으로 바꾸고, 같은 메서드의 `telegram_approval_pending` 조회도 `limit=None, since=self._consistency_since()`로 바꾼다 (envelope을 못 찾으면 승인이 유실되므로 이것도 정합성 경로다):

> **`limit=None`을 반드시 명시할 것.** `list_system_events_by_type(event_type, limit=10, *, since=None)`의 `limit` 기본값은 **10**이다. `since`만 넘기면 조용히 10건으로 잘려 정합성 판정이 깨진다 (Task 1 구현 중 발견).

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
            since=self._consistency_since(),
            )
        }
        terminal = set(completed)
        for row in self.store.list_system_events_by_type(
            "telegram_approval_ack", limit=None, since=self._consistency_since()
        ):
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
- Produces: `_notify_legacy_unresolved_approvals() -> None` (`_sweep_pending_approvals`에서 호출), `_notify_approval_needs_attention(envelope, *, partial: bool)` (Task 5의 재개 경로와 공유), `catalog.APPROVAL_NEEDS_ATTENTION` / `catalog.APPROVAL_NEEDS_RECONCILIATION`

**판정 기준은 "완료 증거의 부재" 하나다.** 두 가지를 혼동하지 않는다:

| 질문 | 술어 |
|---|---|
| 안전하게 자동 재개할 수 있는가? | approvals 행이 **없어야** 한다 (브로커 호출 이전) |
| 운영자에게 알려야 하는가? | `signal_approval_completed`가 **없으면** 알린다 |

`resolution_failed` 유무로 판정하면 안 된다 — `_resolve_async_approval`은 ack를 먼저 저장하고 config를 재로드하므로, ack 직후 프로세스 종료나 config 로드 실패는 `resolution_failed` **없이 ack만** 남긴다.

**approvals 행이 있는데 완료 기록이 없는 건을 알림에서 빼면 안 된다.** 그 상태야말로 브로커 제출 중·직후에 중단된 가장 위험한 경우이며(주문이 이미 나갔을 수 있다), 자동 재개 금지의 근거일 뿐 침묵의 근거가 아니다. 이때는 **브로커 대조가 필요하다**는 별도 문구로 알린다.

> legacy ack(`schema_version` 없음) 중 같은 `signal_run_id`의 `signal_approval_completed`가 없는 건을 전부 격리 대상으로 본다.
> approvals 행이 **있으면** "부분 집행 가능 — 브로커 대조 필요"(`APPROVAL_NEEDS_RECONCILIATION`),
> **없으면** "집행 전 중단"(`APPROVAL_NEEDS_ATTENTION`)으로 문구를 나눈다.

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


def test_legacy_ack_with_approvals_row_but_no_completion_needs_reconciliation(tmp_path):
    """가장 위험한 상태: 집행에 진입했으나 완료 기록이 없다.
    주문이 이미 브로커로 나갔을 수 있으므로 침묵하면 안 된다."""
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")
    store.save_approval(envelope.run_id, "appr_legacy", {"decision": {"status": "approved"}})

    router._sweep_pending_approvals()

    assert any("브로커" in message["text"] for message in router.client.sent_messages)
    assert len(store.list_system_events_by_type(
        "telegram_approval_needs_attention", limit=None
    )) == 1


def test_one_completed_group_does_not_hide_another_unresolved_group(tmp_path):
    """한 signal run이 여러 승인 그룹으로 나뉜 경우(다중 계좌·다중 전략).
    A 그룹이 완료됐다고 B 그룹의 유실이 가려지면 안 된다."""
    router, store = _router(tmp_path)
    signal_run_id = "signal_shared"
    envelope_a = _save_pending_envelope(
        store, approval_id="appr_a", signal_run_id=signal_run_id
    )
    _save_pending_envelope(store, approval_id="appr_b", signal_run_id=signal_run_id)
    _save_ack(store, approval_id="appr_a", status="approved")
    _save_ack(store, approval_id="appr_b", status="approved")
    # A만 완료 — 구 이벤트라 approval_id가 없다
    store.save_system_event(
        envelope_a.run_id,
        "signal_approval_completed",
        {"signal_run_id": signal_run_id, "approval_status": "approved"},
    )

    router._sweep_pending_approvals()

    # 그룹이 둘이라 어느 쪽 완료인지 모호하다 → 둘 다 알린다 (침묵보다 낫다)
    notified = {
        row["payload"]["approval_id"]
        for row in store.list_system_events_by_type(
            "telegram_approval_needs_attention", limit=None
        )
    }
    assert notified == {"appr_a", "appr_b"}


def test_completion_with_approval_id_matches_exactly(tmp_path):
    """신규 완료 이벤트는 approval_id가 있어 그룹 추론이 필요 없다."""
    router, store = _router(tmp_path)
    signal_run_id = "signal_shared"
    envelope_a = _save_pending_envelope(
        store, approval_id="appr_a", signal_run_id=signal_run_id
    )
    _save_pending_envelope(store, approval_id="appr_b", signal_run_id=signal_run_id)
    _save_ack(store, approval_id="appr_a", status="approved")
    _save_ack(store, approval_id="appr_b", status="approved")
    store.save_system_event(
        envelope_a.run_id,
        "signal_approval_completed",
        {
            "approval_id": "appr_a",
            "signal_run_id": signal_run_id,
            "approval_status": "approved",
        },
    )

    router._sweep_pending_approvals()

    notified = {
        row["payload"]["approval_id"]
        for row in store.list_system_events_by_type(
            "telegram_approval_needs_attention", limit=None
        )
    }
    assert notified == {"appr_b"}  # A는 완료가 증명됐다


def test_legacy_ack_with_completion_evidence_is_not_isolated(tmp_path):
    """완료 기록이 있으면 정상 종결이다 — 알리지 않는다."""
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
    "⚠️ 확인이 필요해요 — 승인은 접수됐지만 주문이 만들어지지 않았어요.\n"
    "주문이 나가기 전에 중단된 상태예요.\n"
    "/history에서 상태를 확인해 주세요."
)
APPROVAL_NEEDS_RECONCILIATION = (
    "⚠️ 확인이 필요해요 — 승인 후 주문 처리가 끝나지 않았어요.\n"
    "일부 주문이 이미 나갔을 수 있어 자동으로 다시 시도하지 않아요.\n"
    "증권사 앱에서 체결 내역을 확인해 주세요."
)
```

`handlers.py`:

```python
    def _notify_approval_needs_attention(
        self,
        envelope: PendingApprovalEnvelope,
        *,
        partial: bool,
    ) -> None:
        """partial=True면 브로커에 주문이 나갔을 수 있다는 뜻이다."""
        duplicate_key = f"telegram-approval-attention:{envelope.approval_id}"
        if self.store.duplicate_key_exists(duplicate_key):
            return
        text = (
            ui_catalog.APPROVAL_NEEDS_RECONCILIATION
            if partial
            else ui_catalog.APPROVAL_NEEDS_ATTENTION
        )
        for chat_id in self.config.approval.telegram_allowed_chat_ids:
            self._send(chat_id, text)
        save_audited_system_event(
            self.store,
            self.audit,
            envelope.run_id,
            "telegram_approval_needs_attention",
            {
                "approval_id": envelope.approval_id,
                "partial_execution_possible": partial,
                "duplicate_key": duplicate_key,
            },
        )

    def _notify_legacy_unresolved_approvals(self) -> None:
        """3a 이전 ack 중 완료 기록이 없는 건을 1회만 알린다. 자동 재집행은 하지 않는다.

        approvals 행 유무는 침묵의 근거가 아니라 문구를 가르는 기준이다 — 행이
        있는데 완료가 없으면 브로커 제출 중·직후에 중단된 가장 위험한 상태다.
        """
        completed = self._completed_legacy_approval_ids()
        envelopes = {
            str(row["payload"].get("approval_id")): row["payload"]
            for row in self.store.list_system_events_by_type(
                "telegram_approval_pending",
                limit=None,
            since=self._consistency_since(),
            )
        }
        for row in self.store.list_system_events_by_type(
            "telegram_approval_ack",
            limit=None,
            since=self._consistency_since(),
        ):
            ack = row["payload"]
            if isinstance(ack.get("schema_version"), int):
                continue  # 신규 스키마는 재개 경로가 처리한다
            approval_id = str(ack.get("approval_id"))
            payload = envelopes.get(approval_id)
            if payload is None or approval_id in completed:
                continue  # envelope 없음 또는 정상 종결
            self._notify_approval_needs_attention(
                PendingApprovalEnvelope.model_validate(payload),
                partial=self.store.approval_exists(approval_id),
            )

    def _completed_legacy_approval_ids(self) -> set[str]:
        """legacy 완료 판정. **signal_run_id만으로 판정하면 안 된다** — 하나의
        signal run이 여러 승인 그룹으로 나뉘고(orchestrator의 `_approval_order_groups`)
        그룹마다 별도 approval_id가 발급되므로, 한 그룹의 완료가 다른 그룹의
        유실을 가린다.

        신규 `signal_approval_completed`에는 approval_id가 있어 정확히 매칭된다.
        approval_id가 없는 구 이벤트는 그 signal run의 승인 그룹이 하나뿐일 때만
        완료로 인정하고, 둘 이상이면 **모호하므로 완료로 치지 않는다** — legacy는
        자동 재집행하지 않고 알림만 내므로, 모호하면 알리는 쪽이 안전하다.
        """
        groups: dict[str, list[str]] = defaultdict(list)
        for row in self.store.list_system_events_by_type(
            "telegram_approval_pending",
            limit=None,
            since=self._consistency_since(),
        ):
            payload = row["payload"]
            groups[str(payload.get("signal_run_id"))].append(str(payload.get("approval_id")))

        completed: set[str] = set()
        for row in self.store.list_system_events_by_type(
            "signal_approval_completed",
            limit=None,
            since=self._consistency_since(),
        ):
            payload = row["payload"]
            approval_id = payload.get("approval_id")
            if isinstance(approval_id, str) and approval_id:
                completed.add(approval_id)
                continue
            group = groups.get(str(payload.get("signal_run_id")), [])
            if len(group) == 1:
                completed.add(group[0])
        return completed
```

`handlers.py`는 `collections.abc.Mapping`만 import하고 있으므로 `from collections import defaultdict`를 추가한다.

**`signal_approval_completed`에 approval_id를 추가한다** (`orchestration/orchestrator.py:397` 부근, `resolve_pending_signal_approval`의 완료 이벤트 payload). 한 줄이지만 이후 모든 완료 판정이 signal_run_id 추론 없이 정확해진다:

```python
                {
                    "approval_id": envelope.approval_id,   # 추가
                    "signal_run_id": envelope.signal_run_id,
                    ...
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

**abandoned는 재시도 예산을 깎지 않는다**: attempt *번호*는 abandoned를 포함한 모든 종료 기록에서 증가해야 한다(안 그러면 같은 번호가 다시 나와 duplicate_key 교착). 그러나 `_MAX_RESUME_ATTEMPT` 판정은 **실제로 집행을 시도한 횟수**(`outcome in {"completed", "failed"}`)만 센다. 그렇지 않으면 lease 회수가 반복될 때 한 번도 실행되지 않은 채 예산이 소진되어 운영자 알림으로 빠진다.

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


def test_abandoned_attempts_do_not_consume_the_retry_budget(tmp_path):
    """lease 회수만 반복되면 실제 집행은 한 번도 없었으므로 예산을 깎지 않는다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)
    for attempt in range(2, 8):  # abandoned 6건 — _MAX_RESUME_ATTEMPT(4)를 넘는다
        router._record_resume_finished(
            run_id="run_appr_1", approval_id="appr_1", attempt=attempt, outcome="abandoned"
        )

    assert router._executed_resume_attempts("appr_1") == 0
    router._sweep_pending_approvals()

    # 예산이 남아 있으므로 실제 재개가 일어난다 (attention으로 빠지지 않는다)
    assert _latest_payload(
        store, "telegram_approval_resolution_completed"
    )["approval_id"] == "appr_1"
    assert store.list_system_events_by_type(
        "telegram_approval_needs_attention", limit=None
    ) == []


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
            since=self._consistency_since(),
            )
        }
        envelopes = {
            str(row["payload"].get("approval_id")): row["payload"]
            for row in self.store.list_system_events_by_type(
                "telegram_approval_pending",
                limit=None,
            since=self._consistency_since(),
            )
        }
        for row in reversed(
            self.store.list_system_events_by_type(
                "telegram_approval_ack", limit=None, since=self._consistency_since()
            )
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
                # 주문이 이미 나갔을 수 있으므로 브로커 대조 문구로 알린다.
                self._notify_approval_needs_attention(envelope, partial=True)
                continue
            if self._executed_resume_attempts(approval_id) >= _MAX_RESUME_ATTEMPT:
                # 반복 실패는 자동 재시도로 풀리지 않는다. 매 poll마다 조용히
                # 실패를 쌓는 대신 운영자에게 넘긴다 (스펙 3a 항목 7).
                # abandoned(한 번도 실행되지 않은 시도)는 이 예산을 깎지 않는다.
                self._notify_approval_needs_attention(envelope, partial=False)
                continue
            attempt = self._next_resume_attempt(approval_id)
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
            since=self._consistency_since(),
            )
        }
        now = utc_now()
        for row in self.store.list_system_events_by_type(
            "telegram_approval_resume_claim",
            limit=None,
            since=self._consistency_since(),
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
    def _resume_finished_events(self, approval_id: str) -> list[dict[str, Any]]:
        return [
            row["payload"]
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resume_finished",
                limit=None,
            since=self._consistency_since(),
            )
            if str(row["payload"].get("approval_id")) == approval_id
        ]

    def _next_resume_attempt(self, approval_id: str) -> int:
        """종료 기록 기준으로 번호를 매긴다. in-flight attempt가 있으면 같은
        번호가 다시 나오고, claim duplicate_key 충돌로 병행 진입이 막힌다.

        abandoned도 번호는 증가시킨다 — 안 그러면 회수된 claim과 같은 번호가
        다시 나와 duplicate_key 충돌로 영구 정지한다.
        """
        return len(self._resume_finished_events(approval_id)) + 2  # 최초 콜백이 attempt 1

    def _executed_resume_attempts(self, approval_id: str) -> int:
        """실제로 집행을 시도한 횟수. 재시도 예산은 이 값으로만 판정한다 —
        abandoned(한 번도 실행되지 않고 회수된 claim)는 세지 않는다."""
        return sum(
            1
            for payload in self._resume_finished_events(approval_id)
            if payload.get("outcome") in {"completed", "failed"}
        )

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

`_notify_approval_needs_attention(envelope, *, partial)`과 문구 상수 2개(`APPROVAL_NEEDS_ATTENTION` / `APPROVAL_NEEDS_RECONCILIATION`)는 Task 4에서 이미 만들었으므로 그대로 재사용한다. 재개 경로에서는 approvals 행이 있으면 `partial=True`(브로커 대조 필요), 재시도 예산 소진이면 `partial=False`로 호출한다.

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

### Task 8: 롤백 preflight를 CLI로 강제한다

Task 7은 위험 상태를 **테스트로 고정**할 뿐, 운영 중 긴급 롤백을 막지 못한다. 장애 한복판에서 여러 줄짜리 python 스니펫을 정확히 실행하기를 기대하는 것은 현실적이지 않다 — 그 순간이야말로 실수가 나는 순간이다. 3a-1이 만든 위험은 3a-1이 닫는다.

**범위는 3a-1이 도입한 불변식 하나로 한정한다**: `schema_version=2` ack가 있고 `telegram_approval_resolution_completed`가 없는 승인. funding/budget claim, dispatch 완료, legacy dual-write까지 검사하는 **전체 3a preflight는 3a-5**이며, 그 명령이 이 명령을 확장·흡수한다.

**Files:**
- Modify: `src/maestro/cli.py` (`clear-halt`가 쓰는 `_load_operator_config` / `_state_store` 패턴을 따른다, cli.py:1444)
- Test: `tests/test_cli_approval_preflight.py` (신규)

**Interfaces:**
- Produces: `maestro approval-rollback-preflight --config <path> [--require-quiesce]`
  - 미완 승인 0건 → `approval_rollback_preflight status=safe unresolved=0`, exit 0
  - 1건 이상 → 각 approval_id를 출력하고 exit **1**
  - `--require-quiesce`: `maestro-telegram-operator.service`가 active면 exit 1 (검사 자체가 무의미하므로)

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
def test_preflight_exits_zero_when_no_unresolved_approvals(tmp_path):
    config_path = _telegram_config_path(tmp_path)
    store = StateStore(load_config(config_path).state.sqlite_path, initial_cash=1000)
    store.save_system_event("run_1", "telegram_approval_ack", {
        "approval_id": "appr_1", "status": "approved", "schema_version": 2,
    })
    store.save_system_event("run_1", "telegram_approval_resolution_completed", {
        "approval_id": "appr_1", "status": "approved",
    })

    result = CliRunner().invoke(app, ["approval-rollback-preflight", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "status=safe" in result.stdout


def test_preflight_exits_nonzero_and_names_unresolved_approvals(tmp_path):
    config_path = _telegram_config_path(tmp_path)
    store = StateStore(load_config(config_path).state.sqlite_path, initial_cash=1000)
    store.save_system_event("run_1", "telegram_approval_ack", {
        "approval_id": "appr_unresolved", "status": "approved", "schema_version": 2,
    })

    result = CliRunner().invoke(app, ["approval-rollback-preflight", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "appr_unresolved" in result.stdout


def test_preflight_ignores_legacy_acks(tmp_path):
    """schema_version이 없는 ack는 구버전 의미론에서도 종결이라 롤백을 막지 않는다."""
    config_path = _telegram_config_path(tmp_path)
    store = StateStore(load_config(config_path).state.sqlite_path, initial_cash=1000)
    store.save_system_event("run_1", "telegram_approval_ack", {
        "approval_id": "appr_legacy", "status": "approved",
    })

    result = CliRunner().invoke(app, ["approval-rollback-preflight", "--config", str(config_path)])

    assert result.exit_code == 0
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_cli_approval_preflight.py -q`
Expected: FAIL — `No such command 'approval-rollback-preflight'` (exit_code 2)

- [ ] **Step 3: 구현한다**

`src/maestro/cli.py`:

```python
@app.command("approval-rollback-preflight")
def approval_rollback_preflight(
    config: Path | None = CONFIG_OPTION,
    require_quiesce: bool = typer.Option(
        False,
        "--require-quiesce",
        help="Fail if the telegram operator service is still running.",
    ),
) -> None:
    """롤백 전 안전 검사 (읽기 전용). 미완 승인이 있으면 exit 1.

    schema_version=2 ack가 있고 resolution_completed가 없는 승인은 구버전이
    ack만 보고 종결로 오판하므로, 이 상태에서 롤백하면 주문이 유실된다.
    """
    if require_quiesce and _service_is_active("maestro-telegram-operator.service"):
        typer.echo("approval_rollback_preflight status=fail reason=operator_still_running")
        raise typer.Exit(1)
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    acked = {
        str(row["payload"].get("approval_id"))
        for row in store.list_system_events_by_type("telegram_approval_ack", limit=None)
        if isinstance(row["payload"].get("schema_version"), int)
    }
    completed = {
        str(row["payload"].get("approval_id"))
        for row in store.list_system_events_by_type(
            "telegram_approval_resolution_completed", limit=None
        )
    }
    unresolved = sorted(acked - completed)
    if not unresolved:
        typer.echo("approval_rollback_preflight status=safe unresolved=0")
        return
    for approval_id in unresolved:
        typer.echo(f"approval_rollback_preflight status=unsafe approval_id={approval_id}")
    typer.echo(f"approval_rollback_preflight status=unsafe unresolved={len(unresolved)}")
    raise typer.Exit(1)


def _service_is_active(unit: str) -> bool:
    result = subprocess.run(  # noqa: S603 - 고정 인자, 사용자 입력 없음
        ["systemctl", "is-active", "--quiet", unit],
        check=False,
    )
    return result.returncode == 0
```

`subprocess`는 `cli.py:4`에 이미 import되어 있다 (추가 import 불필요).

`limit=None`을 쓰는 이유: 이 명령은 poll 루프가 아니라 **롤백 직전 1회** 실행되므로 전건 조회 비용이 문제되지 않고, 아무리 오래된 미완 승인도 놓치면 안 된다 (`_consistency_since` 창을 적용하지 않는다).

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_cli_approval_preflight.py -q`
Expected: PASS

- [ ] **Step 5: 배포 확인 절의 롤백 절차를 이 명령으로 바꾼다**

아래 "롤백 절차"의 3단계 인라인 python 스니펫을 다음으로 대체한다:

```bash
.venv/bin/maestro approval-rollback-preflight \
  --config /root/maestro-operator/symphony_approval.yaml \
  --require-quiesce || echo "롤백 중단 — 미완 승인을 먼저 종결시킬 것"
```

- [ ] **Step 6: 커밋**

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check src tests --output-format=concise
git add -A
git commit -m "feat: add a read-only rollback preflight for unresolved approvals"
```

---

## 검증

- 각 태스크: 명시된 pytest 명령으로 RED 확인 → 구현 → GREEN 확인 → 커밋
- 최종: `.venv/bin/python -m pytest tests/ -q` (기준선 1251 passed / 9 skipped 이상), `.venv/bin/python -m ruff check src tests --output-format=concise` → All checks passed

## 배포 확인 (3a-1 승인 조건)

- 배포 직후 legacy 격리 알림이 몇 건 나간다. 운영 DB의 기존 legacy ack 중 **집행 완료 증거가 없는 건**이 대상이며, **자동 재집행은 일어나지 않는다**. 배포 전에 대상 건수를 미리 확인한다.

  판정식은 구현(`_notify_legacy_unresolved_approvals` / `_completed_legacy_approval_ids`)과 반드시 같아야 한다: **pending envelope이 있고 + 완료 증거가 없으면** 알린다. `approvals` 행 유무는 알릴지 말지를 정하지 않고 **문구만 가른다** (행이 있으면 `APPROVAL_NEEDS_RECONCILIATION`, 없으면 `APPROVAL_NEEDS_ATTENTION`). 완료 증거는 `signal_approval_completed`이며, approval_id가 있으면 정확히 매칭하고 없는 구 이벤트는 그 signal run의 승인 그룹이 하나뿐일 때만 완료로 인정한다.

  ```bash
  .venv/bin/python -c "
  import sqlite3, json
  from collections import defaultdict
  c = sqlite3.connect('/root/maestro-operator/var/symphony_state.db')
  def payloads(t):
      return [json.loads(p) for (p,) in
              c.execute('select payload from system_events where event_type=?', (t,))]
  acks = [p for p in payloads('telegram_approval_ack')
          if not isinstance(p.get('schema_version'), int)]
  envelopes = {p['approval_id']: p for p in payloads('telegram_approval_pending')}
  groups = defaultdict(list)
  for p in envelopes.values():
      groups[str(p.get('signal_run_id'))].append(p['approval_id'])
  completed = set()
  for p in payloads('signal_approval_completed'):
      if isinstance(p.get('approval_id'), str) and p['approval_id']:
          completed.add(p['approval_id']); continue
      group = groups.get(str(p.get('signal_run_id')), [])
      if len(group) == 1:
          completed.add(group[0])   # 그룹이 둘 이상이면 모호 → 완료로 치지 않는다
  approvals = {r[0] for r in c.execute('select approval_id from approvals')}
  alerts = [p['approval_id'] for p in acks
            if p['approval_id'] in envelopes and p['approval_id'] not in completed]
  print('legacy ack:', len(acks), '/ 격리 예상:', len(alerts))
  for a in sorted(alerts):
      print('  ', a, 'RECONCILIATION' if a in approvals else 'ATTENTION')
  "
  ```

  **운영 DB 실측 결과: 2건이 나간다.** (이전 판정식 "approvals 행이 없는 legacy ack"는 1건으로 과소 예측했다.)
  - `2026-08-07` 사고 건 — approvals 행 없음 → `APPROVAL_NEEDS_ATTENTION`. 승인은 접수됐으나 stale broker snapshot으로 집행 전에 중단됐다.
  - `appr_37cd3742` (2026-07-28) — approvals 행 있음 → `APPROVAL_NEEDS_RECONCILIATION`. **본 브랜치가 새로 발견한 두 번째 유실이다.** QQQ 5주가 실제로 브로커에서 체결된 뒤 `AttributionValidationError`가 resolution을 중단시켰고, 그래서 완료 기록이 없다. 아무도 눈치채지 못한 상태였으며, 주문이 이미 나갔으므로 운영자는 증권사 체결 내역과 대조해야 한다.
- `systemctl restart maestro-telegram-operator.service` 후 로그에 `telegram_operator status=ok`가 이어지는지 확인.
- 다음 실제 승인 1건에서 `telegram_approval_ack`(schema_version=2)와 `telegram_approval_resolution_completed`가 **둘 다** 기록되는지 DB로 확인.
- 명령 메뉴는 이 단계에서 바뀌지 않으므로 `telegram-set-commands` 재실행은 불필요하다.

### 롤백 절차 (순서 강제)

읽기 전용 검사만으로는 부족하다 — 서비스가 돌고 있으면 검사 직후 새 v2 ack가 기록되고 롤백되는 TOCTOU 창이 남는다. **writer를 먼저 멈추면** 검사 이후 새 상태가 생길 수 없다. 스펙이 3a 롤백에 요구하는 quiesce 순서와 같다.

**타이머만 멈추는 것으로는 장벽이 닫히지 않는다.** (a) 이미 실행 중인 서비스 인스턴스는 타이머를 멈춰도 계속 돈다. (b) operator를 **되살릴 수 있는 유닛**이 있다 — `deploy/systemd/maestro-run-once.service:14`의 `ExecStopPost=-/bin/systemctl start maestro-telegram-operator.service`가 그렇다(현재 VPS에는 미설치지만, 설치된 환경에서는 preflight 이후 operator를 되살려 TOCTOU 창을 다시 연다). 그래서 정지 대상을 열거하고, 배포 직전에 **실제 정지 상태를 확인**하는 단계를 둔다.

```
1. quiesce   # 타이머와 서비스를 모두 멈춘다 (타이머만 멈추면 실행 중 인스턴스가 남는다)
   systemctl stop maestro-telegram-operator.service
   systemctl stop maestro-symphony-signal-kr.timer  maestro-symphony-signal-kr.service
   systemctl stop maestro-symphony-signal-us.timer  maestro-symphony-signal-us.service
   systemctl stop maestro-symphony-rebalance-kr.service maestro-symphony-rebalance-us.service
   systemctl stop maestro-run-once.timer maestro-run-once.service   # 설치돼 있으면 (operator를 되살린다)

2. 장벽 확인  # 전부 inactive여야 한다. 하나라도 active면 1로 돌아간다.
   systemctl is-active maestro-telegram-operator.service \
     maestro-symphony-signal-kr.service maestro-symphony-signal-us.service \
     maestro-symphony-rebalance-kr.service maestro-symphony-rebalance-us.service \
     maestro-run-once.service ; echo "---"
   # state store에 쓰는 유닛을 빠뜨리지 않았는지 전수 확인:
   systemctl list-units --all 'maestro-*' --no-pager | grep -v inactive

3. 검사      아래 preflight가 exit 0이어야 한다 (Task 8).
             exit 1이면 롤백하지 않는다 — 출력된 승인을 먼저 종결시킨다.

4. 구버전 배포  3과 4 사이에 어떤 unit도 재시작하지 않는다.

5. 재개      구버전 기동을 확인한 뒤에만 timer·service를 재시작한다.
```

```bash
.venv/bin/maestro approval-rollback-preflight \
  --config /root/maestro-operator/symphony_approval.yaml \
  --require-quiesce
# exit 0 → 롤백 가능 / exit 1 → 중단 (미완 승인 approval_id가 출력된다)
```

이 명령의 판정 로직은 읽기 전용이지만, 상태 저장소 연결 과정에서 다른 모든 CLI 명령과 마찬가지로 보류 중인 스키마 마이그레이션/백필이 적용될 수 있다(모두 additive-only·멱등적이며 구버전 코드도 기동 시 동일하게 실행하므로 롤백 안전성에는 영향이 없다).

## 채택하지 않는 리뷰 권고 (근거 기록)

Codex 적대적 리뷰 1·2차에서 나왔으나 반영하지 않은 항목과 이유:

- **"주문 계층 멱등성(제출 intent·브로커 멱등 키)을 같은 배포에 포함"** — `approval_exists` fail-closed 조건이 중복 제출 경로를 이미 차단한다(행이 있으면 자동 재개하지 않는다). 제출 intent는 dispatch 계층 재설계와 함께 가야 하며 **3a-3** 범위다. 3a-1에 넣으면 계획이 두 배가 되고 검증 표면도 넓어진다.
- **"quiesced backfill을 3a-1 선행 조건으로"** — legacy를 자동 실행하지 않으므로 backfill 중 경계 오염이 발생할 여지가 없다. quiesce 장벽은 자동 재집행을 켜는 시점(**3a-5**)에 필요하다.
- **"DB compatibility marker로 구버전 시작 자체를 차단"** / **"writer가 검사하는 durable maintenance fence"** — **구현 불가능하다.** 구버전 코드에는 마커·fence를 검사하는 로직이 없고, 이미 배포된 코드에 소급 적용할 수 없다. 강제력은 5단계 절차 + Task 8의 preflight로 확보한다.

> **입장 변경 (4차 리뷰)**: 3차 리뷰에서는 "절차 순서로 TOCTOU가 제거되므로 CLI는 3a-5"라고 판단했으나, 재지적을 받아들여 **Task 8로 3a-1 범위에 넣었다.** 절차 문서는 장애 한복판에서 지켜지지 않으며, 어차피 쿼리를 제공할 바에는 exit code로 롤백을 막는 명령이 거의 같은 비용으로 훨씬 안전하다. 단 범위는 3a-1이 만든 불변식 하나로 한정하고, 전체 3a preflight는 3a-5에서 이 명령을 확장한다.
- **"lease를 resolution 중 갱신하고 owner/token CAS로 회수"** (3차 리뷰) — 단일 프로세스 폴러에 과하다. `_sweep_pending_approvals`는 `poll_once` 안에서 순차 실행되므로, 같은 프로세스에서 resolution이 도는 동안 다른 sweep이 lease를 회수할 수 없다. "정상 resolution이 lease를 넘겨 회수당한다"는 시나리오는 프로세스가 둘일 때만 성립한다. 대신 실질적 위험(예산 소진)은 **abandoned를 재시도 예산에서 제외**하는 것으로 제거했다.
- **"인덱스 프로젝션·부하 테스트·폴링 예산 도입"** (3차 리뷰) — 규모에 비해 과하다. 운영 DB의 승인 이벤트는 몇 달치가 6~16건이고 증가율은 거래일당 1건(연 250행) 수준이다. 성장 우려와 개수 창의 정합성 결함을 **시간 경계(Task 0)** 하나로 함께 해소했으며, 이는 기존 `(event_type, created_at)` 인덱스를 그대로 탄다. 이벤트 보존 정책·부하 테스트가 필요해지면 그때 별도로 다룬다.

## 다음 단계

- **3a-2**: `StateStore.save_system_events_atomic` (다중 이벤트 + duplicate_key + precondition 원자 커밋)
- **3a-3**: 승인 dispatch idempotent resume (`dispatch_group_id` get-or-create, 채팅별 전송 intent) — 본 계획이 운영자에게 넘긴 "부분 집행된 승인"의 자동 처리도 여기서 다룬다

### 최종 수정 웨이브에서 의도적으로 넣지 않은 것 (3a-3 이월)

두 리뷰가 지적했고 타당하지만, 이번 배포 범위에 넣지 않기로 한 항목이다. 잊히지 않도록 여기 기록한다.

1. **주문 단위 제출 상태 판정 (3a-3).** 지금은 `approvals` 행이 있으면 무조건 자동 재개하지 않고 알림으로만 넘긴다. 그중에는 **브로커 제출이 한 번도 일어나지 않은** 상태(행은 썼지만 `_execute_live_approval_orders` 전에 중단)도 섞여 있고, 그건 원래 안전하게 재개할 수 있다.

   > **정정 (2026-08-11, 배포 후 조사).** 이 항목은 원래 "주문마다 제출 intent를 **먼저 기록해야** 구분이 가능하다 — 그 기록을 만드는 것이 3a-3"이라고 적혀 있었다. **틀렸다. 그 기록은 이미 존재한다.**
   >
   > - `execution/live_order_safety.py:255-275`가 **브로커 제출 직전에** `live_order_submit_intent`를 기록한다. 이벤트 행에 `order_id`가 들어가고 payload의 `duplicate_key`는 `intent:<결과키>` 형식이며, 중복 제출 차단에 이미 쓰이고 있다(같은 intent가 또 들어오면 "operator recovery is required"로 거부).
   > - `ops/workflow_recovery.py:197-213`이 이미 이 쌍을 소비한다: intent가 있는데 결과 키(`intent:` 접두어를 뗀 것)가 없으면 `live_order_intent_without_result` 블로커로 잡는다.
   >
   > 따라서 주문별 제출 상태는 **지금 당장 판정 가능**하다:
   > | 관찰 | 의미 |
   > |---|---|
   > | intent 없음 | 브로커에 나간 적 없음 → **안전하게 재개 가능** |
   > | intent 있음 + 결과 없음 | 제출됐고 결과 미확인 → 브로커 조회로 대조해야 함 |
   > | intent 있음 + 결과 있음 | 제출·결과 확정 |
   >
   > 실제로 2026-07-28 건(QQQ/SCZ/EEM)은 이 기준을 적용하면 `live_order_submit_intent`가 존재하므로 "제출됨"으로 정확히 분류된다.
   >
   > **3a-3의 실제 과제는 intent를 만드는 것이 아니라 승인 재개 경로가 그것을 읽게 하는 것이다.** 범위가 크게 줄고, 자동 재개를 "approvals 행 없음"에서 "**해당 승인의 어떤 주문에도 submit intent가 없음**"으로 안전하게 넓힐 수 있다.

   적대적 리뷰의 지적 자체는 유효했다: 3a-1 설계는 **중복 제출에 대해 fail-closed일 뿐, 그 상태를 스스로 복구하지는 못한다.** 이번 웨이브가 보장한 것은 복구가 아니라 **그 상태가 반드시 운영자에게 드러난다**는 것이다(F1/F2/F4).

2. **재개 시도 간 backoff·간격.** 지금은 간격이 없다. poll 주기상 자동 재개 예산 3회(attempt 2·3·4)가 **약 33초 안에 전부 소진된다.** 게다가 재개 경로는 브로커 스냅샷을 **다시 채택하지 않는다** — 2026-08-07을 일으킨 stale snapshot 실패는 시간이 지나도 저절로 낫지 않으므로, 이 실패 유형에 대해 자동 재개는 **단조적으로 회복 불가능**하다. 즉 세 번의 재시도는 스냅샷이 이미 유효한 경우(검증 실패·config 로드 실패·ack 직후 프로세스 종료)에만 값을 낸다.
   **이 계획의 전달 가치를 과장하지 말 것**: 3a-1이 실제로 없앤 것은 "ack가 곧 종결"이라는 오판이며, stale snapshot 자체의 자동 복구가 아니다. 그 유형은 예산 소진 후 ⚠️ 알림으로 운영자에게 간다. backoff와 스냅샷 재채택은 **3a-3**에서 함께 다룬다.

3. **2차 수정 웨이브(G1~G3)에서 기록만 하고 고치지 않은 것.** 넷 다 지적이 타당하지만, 실주문 코드에 지금 손댈 값어치보다 위험이 크다고 판단해 **3a-3으로 이월**한다.
   - **`_record_resolution_completed`의 새 raise 지점.** F5가 check-then-write를 `writer_lock` 안으로 옮기면서, **주문이 브로커에 도달한 뒤에** `TimeoutError`가 날 수 있는 지점이 하나 생겼다. 착지는 fail-closed다 — `approvals` 행이 이미 있으므로 다음 sweep이 ⚠️ 알림으로 운영자에게 넘긴다. 순이득이지만 hot path의 새 raise 지점이라는 사실은 남는다.
   - **리마인더 dedup 스캔의 `save_audited_system_event`가 자기 `try` 밖에 있다.** dedup이 빗나가면 `IntegrityError`가 sweep을 뚫고 나간다(F2와 같은 유형). 현재 이벤트 볼륨에서는 도달 불가.
   - **영구 전송 불가 채팅의 이벤트 증식.** 그런 채팅 하나가 미완 승인 하나당 poll(약 30초)마다 `telegram_command` error 이벤트를 한 건씩 쓴다. backoff도 상한도 없다.
   - **`str(ack.get("approval_id"))`가 손상된 ack에서 문자열 `"None"`을 만든다.** `telegram-approval-attention:None:<chat>` 같은 키가 생긴다.

4. **아웃박스에 포기 조건이 없다 (G1이 만든 것, 사용자 결정으로 이월).** `_deliver_resume_completion_notices`는 채팅별 키가 없는 `attempt > 1` 완료 건을 **매 poll 무제한으로 재시도**한다. 영구 도달 불가 채팅이 하나 있으면 비용이 "미완 승인 하나당"이 아니라 **"과거에 재개된 모든 승인 하나당, 약 30초마다, 영원히"**로 커지고, 그 집합은 줄어들지 않는다 — 위 3번의 이벤트 증식을 G1이 확대한 셈이다.
   **승인을 잃거나 주문을 중복시키지는 않는다** — 비용은 실패한 전송 시도와 `telegram_command` error 이벤트, 즉 운영 소음이다. 배포 시점 운영 DB의 대상 건수는 **0건**(`attempt > 1` 완료 이력 없음)이고 재개가 일어날 때마다 천천히 늘어난다.
   해법은 아웃박스 행에 **나이 또는 시도 상한**을 두고(예: N회 sweep 후 포기) attention 경로로 떨어뜨리는 것이다. 3a-3에서 3번 항목과 함께 다룬다.
- **3a-4**: funding/budget workflow head·CAS·attempt claim·lineage·수렴 sweep
- **3a-5**: 업그레이드 backfill + 롤백 preflight CLI + 운영 문서

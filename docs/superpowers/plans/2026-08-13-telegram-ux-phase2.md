# 라이프사이클 카드 매니저 (단계 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 승인·데일리 카드를 한 번 보내고 이후 상태 변화마다 그 메시지를 갱신하는 라이프사이클 매니저를 만들어, 연속 알림 나열을 카드 하나로 대체한다.

**Architecture:** `ui/lifecycle.py`가 유일한 상태 보유 컴포넌트다. 전달 복사본별 상태를 `telegram_ui_card` system event로 남기되 **키는 `(card_key, chat_id)`**이고, 전송은 항상 `intent → 텔레그램 호출 → result` 순서로 기록한다. poll 루프의 sweep이 원천 이벤트를 조회해 단계 변화 시 `edit_message_text`로 갱신한다. 렌더러(`cards.py`)는 순수 함수로 유지되고 네트워크·DB를 모른다.

**Tech Stack:** Python 3, SQLite state store, Telegram Bot API (`sendMessage`/`editMessageText`), Typer CLI, pytest, ruff

**Spec:** `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md` (단계 2 행, 「카드 체계 A」, 「데이터 흐름」, 「에러 처리」)

## Global Constraints

- 테스트 기준선: `.venv/bin/python -m pytest tests/ -q` → **1352 passed, 9 skipped**. 각 태스크는 이 수를 줄이지 않는다.
- 린트: `.venv/bin/python -m ruff check src tests --output-format=concise` → `All checks passed!`
- **모든 하중 테스트는 뮤테이션으로 비공허성을 증명한다.** 구현을 되돌려 테스트가 실패하는 것을 확인하고 복원한다.
- **상태 키는 `(card_key, chat_id)`.** `card_key` 단독으로 `message_id`를 들고 있는 자료구조를 만들지 않는다. 운영 chat이 현재 하나뿐이라는 사실은 이 계약을 접을 근거가 아니다.
- **전송 전 intent를 먼저 기록한다.** `sendMessage`/`editMessageText` 호출 앞에 intent, 응답 뒤에 result. intent만 있고 result 없는 상태가 조회 가능해야 한다.
- **모르는 것을 안다고 취급하지 않는다.** intent만 남은 복사본은 *미전달*이 아니라 **전달 여부 불명(ambiguous)**이다. 자동 재전송하지 않는다. 관측 가능성을 만든 뒤 그 위에서 곧바로 자동 조치를 하는 것은 이 프로젝트가 오늘 이미 한 번 저지른 실수다 — 락 스펙 3회차에서 "만료된 리스를 자동 회수"가 정확히 같은 모양이었고, 같은 이유로 철회했다.
- **`StateStore.list_system_events_by_type`는 `ORDER BY id DESC`다** (`store.py:1815`). 이벤트를 시간순으로 접으려면 반드시 뒤집어야 한다. 기본 `limit`은 10이므로 항상 명시한다.
- **`duplicate_key`에는 UNIQUE 인덱스가 있고**(`store.py:203`) `save_system_event`는 평범한 INSERT다. 같은 키를 두 번 쓰면 `IntegrityError`다. 재시도 가능한 이벤트의 키에는 시도별 고유값이 들어가야 한다.
- **의존성 방향은 `handlers.py → ui/` 단방향.** `ui/`는 handlers·orchestrator·execution을 임포트하지 않는다.
- **기존 알림 경로를 제거하지 않는다.** 개별 주문 알림·미체결 경고·halt·정산 불일치 알림은 카드와 **병행 유지**한다(스펙 「단계 2의 안전망」). 제거는 단계 5다.
- **UI 실패가 거래를 막지 않는다.** 단, 조용히 사라지지도 않는다 — 연속 3회 실패는 고정 템플릿 알림과 healthcheck degraded로 이어진다.
- 커밋 trailer:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Ud76J4vJYANjQVUMNnFqEK
  ```

## File Structure

| 파일 | 책임 |
|---|---|
| `src/maestro/integrations/telegram/ui/card_state.py` (신규) | `telegram_ui_card` 이벤트의 스키마와 순수 해석 함수 — 이벤트 목록 → 현재 전달 복사본 상태 |
| `src/maestro/integrations/telegram/ui/approval_stage.py` (신규) | 승인 payload → 카드 단계 판정 (순수 함수, 역행 금지 순위 포함) |
| `src/maestro/integrations/telegram/ui/lifecycle.py` (신규) | 유일한 상태 보유 컴포넌트. 전송(intent→send→result), sweep 갱신, 폴백 |
| `src/maestro/integrations/telegram/ui/cards.py` | 기존 승인 카드 + 신규 데일리 요약 카드 렌더러 (순수 함수) |
| `src/maestro/integrations/telegram/ui/catalog.py` | 신규 문구 (노옵, 폴백 템플릿) |
| `src/maestro/integrations/telegram/handlers.py` | sweep 등록, 승인 카드 전송을 lifecycle 경유로 전환 |

`card_state.py`를 `lifecycle.py`에서 분리하는 이유: 상태 해석은 순수 함수로 테스트할 수 있고, 네트워크·저장소를 가진 매니저와 섞이면 그 테스트가 불가능해진다.

---

### Task 1: 카드 상태 계약 — `(card_key, chat_id)`와 intent→result

**Files:**
- Create: `src/maestro/integrations/telegram/ui/card_state.py`
- Test: `tests/test_telegram_card_state.py` (신규)

**Interfaces:**
- Consumes: 없음
- Produces:
  - `CardStage` — `Literal["pending", "in_progress", "done", "attention"]`
  - `Delivery` — `Literal["confirmed", "failed", "unknown"]`
  - `CardCopy(card_key, chat_id, message_id: int | None, stage: str, render_hash: str, delivery: str, operation_id: str)` — NamedTuple
  - `new_operation_id() -> str`
  - `card_intent_event(card_key, chat_id, stage, render_hash, operation_id) -> dict[str, Any]`
  - `card_result_event(card_key, chat_id, stage, render_hash, operation_id, message_id) -> dict[str, Any]`
  - `card_failure_event(card_key, chat_id, stage, render_hash, operation_id, error: str) -> dict[str, Any]`
  - `resolve_card_copies(events: Sequence[Mapping[str, Any]]) -> dict[tuple[str, int], CardCopy]` — **events는 오래된 것부터**

**왜 `delivered: bool`이 아니라 `delivery` 3값인가.** boolean은 "전달됨/안 됨"만 표현하는데, 실제로는 세 번째 상태가 있다. 예외를 잡아 실패를 기록한 경우는 **전달되지 않았음을 안다**(`failed`). 프로세스가 호출 도중 죽어 intent만 남은 경우는 **모른다**(`unknown`). 이 둘을 boolean으로 뭉치면 후자를 전자처럼 재전송하게 되고, 원래 전송이 성공했다면 버튼 달린 카드가 두 장 생긴다. 두 번째 카드는 갱신되지 않으므로 영원히 "⏳ 승인 대기"로 남고, 진짜 카드가 "✅ 완료"일 때 운영자가 그 낡은 쪽을 보면 승인이 아직 안 됐다고 판단한다.

**왜 `operation_id`인가.** `duplicate_key`가 `(phase, card_key, chat_id, stage)`까지만이면 같은 단계의 두 번째 시도가 같은 키를 쓴다. UNIQUE 인덱스에 걸려 재시도 자체가 `IntegrityError`로 죽는다. 시도마다 고유 id를 부여하고 intent·result·failure가 그 id를 공유하게 하면, 재시도가 가능해지는 동시에 3a-3이 "어느 intent가 어느 결과와 짝인지"를 복원할 수 있다.

- [ ] **Step 1: Write the failing tests**

`tests/test_telegram_card_state.py`:

```python
from maestro.integrations.telegram.ui.card_state import (
    card_intent_event,
    card_result_event,
    resolve_card_copies,
)


def test_one_logical_card_keeps_a_separate_copy_per_chat():
    """The storage key is (card_key, chat_id), not card_key.

    With a single operator chat today the natural implementation collapses
    this, and then a second chat only ever sees the first render.
    """
    events = [
        card_result_event("approval:appr_1", 100, "pending", "h1", 5001),
        card_result_event("approval:appr_1", 200, "pending", "h1", 5002),
    ]

    copies = resolve_card_copies(events)

    assert set(copies) == {("approval:appr_1", 100), ("approval:appr_1", 200)}
    assert copies[("approval:appr_1", 100)].message_id == 5001
    assert copies[("approval:appr_1", 200)].message_id == 5002


def test_an_intent_without_a_result_is_unknown_not_undelivered():
    """The crash window must read as "we do not know", never as "not sent".

    If sendMessage succeeded and the process died before the result was
    written, the card exists in Telegram with no message_id here. Calling that
    undelivered invites a resend that duplicates a card nobody can update.
    """
    events = [card_intent_event("approval:appr_1", 100, "pending", "h1", "op1")]

    copy = resolve_card_copies(events)[("approval:appr_1", 100)]

    assert copy.delivery == "unknown"
    assert copy.message_id is None


def test_a_caught_error_records_a_known_failure():
    """Distinct from unknown: we hold the exception, so it never left."""
    events = [
        card_intent_event("approval:appr_1", 100, "pending", "h1", "op1"),
        card_failure_event("approval:appr_1", 100, "pending", "h1", "op1", "connection refused"),
    ]

    assert resolve_card_copies(events)[("approval:appr_1", 100)].delivery == "failed"


def test_a_result_supersedes_its_intent():
    events = [
        card_intent_event("approval:appr_1", 100, "pending", "h1", "op1"),
        card_result_event("approval:appr_1", 100, "pending", "h1", "op1", 5001),
    ]

    copy = resolve_card_copies(events)[("approval:appr_1", 100)]

    assert copy.delivery == "confirmed"
    assert copy.message_id == 5001


def test_a_retry_at_the_same_stage_gets_a_distinct_duplicate_key():
    """system_events has a UNIQUE index on duplicate_key (store.py:203).

    Without a per-attempt id the second attempt at one stage dies with
    IntegrityError, which would make retry impossible by construction.
    """
    first = card_intent_event("approval:appr_1", 100, "pending", "h1", "op1")
    second = card_intent_event("approval:appr_1", 100, "pending", "h1", "op2")

    assert first["duplicate_key"] != second["duplicate_key"]
    assert first["operation_id"] == "op1"


def test_a_later_stage_supersedes_an_earlier_one_for_that_chat_only():
    events = [
        card_result_event("approval:appr_1", 100, "pending", "h1", 5001),
        card_result_event("approval:appr_1", 200, "pending", "h1", 5002),
        card_result_event("approval:appr_1", 100, "done", "h2", 5001),
    ]

    copies = resolve_card_copies(events)

    assert copies[("approval:appr_1", 100)].stage == "done"
    assert copies[("approval:appr_1", 200)].stage == "pending"


def test_events_are_ordered_by_arrival_not_by_stage_name():
    """Out-of-order arrival is explicitly in scope (spec, card section A)."""
    events = [
        card_result_event("approval:appr_1", 100, "done", "h2", 5001),
        card_result_event("approval:appr_1", 100, "in_progress", "h3", 5001),
    ]

    assert resolve_card_copies(events)[("approval:appr_1", 100)].stage == "in_progress"


def test_intent_and_result_of_one_attempt_share_an_operation_id():
    """3a-3 has to pair an outcome with the intent that caused it."""
    intent = card_intent_event("approval:appr_1", 100, "pending", "h1", "op1")
    result = card_result_event("approval:appr_1", 100, "pending", "h1", "op1", 5001)

    assert intent["operation_id"] == result["operation_id"] == "op1"
    assert intent["duplicate_key"] == "telegram-ui-card:intent:approval:appr_1:100:pending:op1"
    assert result["duplicate_key"] == "telegram-ui-card:result:approval:appr_1:100:pending:op1"


def test_events_must_be_folded_oldest_first():
    """The store returns ORDER BY id DESC (store.py:1815).

    Feeding that straight in makes the oldest event win, so a delivered card
    reads back as unknown and the sweep sends it again. This pins the direction
    the resolver expects; lifecycle.py is responsible for reversing.
    """
    oldest_first = [
        card_intent_event("approval:appr_1", 100, "pending", "h1", "op1"),
        card_result_event("approval:appr_1", 100, "pending", "h1", "op1", 5001),
    ]

    assert resolve_card_copies(oldest_first)[("approval:appr_1", 100)].delivery == "confirmed"
    assert (
        resolve_card_copies(list(reversed(oldest_first)))[("approval:appr_1", 100)].delivery
        == "unknown"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_telegram_card_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.integrations.telegram.ui.card_state'`

- [ ] **Step 3: Implement the module**

`src/maestro/integrations/telegram/ui/card_state.py`:

```python
"""The card delivery record: one logical card, one copy per chat.

Pure functions over the event list. lifecycle.py owns the network and the
store; keeping the interpretation here means it can be tested without either.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Literal, NamedTuple

CardStage = Literal["pending", "in_progress", "done", "attention"]

EVENT_TYPE = "telegram_ui_card"


Delivery = Literal["confirmed", "failed", "unknown"]

_PHASE_DELIVERY: dict[str, Delivery] = {
    "intent": "unknown",
    "result": "confirmed",
    "failure": "failed",
}


class CardCopy(NamedTuple):
    card_key: str
    chat_id: int
    message_id: int | None
    stage: str
    render_hash: str
    delivery: str
    operation_id: str


def new_operation_id() -> str:
    return uuid.uuid4().hex[:16]


def _duplicate_key(
    phase: str, card_key: str, chat_id: int, stage: str, operation_id: str
) -> str:
    # operation_id is what makes a retry writable at all: system_events has a
    # UNIQUE index on duplicate_key (store.py:203).
    return f"telegram-ui-card:{phase}:{card_key}:{chat_id}:{stage}:{operation_id}"


def _event(
    phase: str,
    card_key: str,
    chat_id: int,
    stage: str,
    render_hash: str,
    operation_id: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "phase": phase,
        "card_key": card_key,
        "chat_id": chat_id,
        "stage": stage,
        "render_hash": render_hash,
        "operation_id": operation_id,
        "duplicate_key": _duplicate_key(phase, card_key, chat_id, stage, operation_id),
        **extra,
    }


def card_intent_event(
    card_key: str, chat_id: int, stage: str, render_hash: str, operation_id: str
) -> dict[str, Any]:
    """Written before the Telegram call, so a crash mid-send leaves a trace."""
    return _event("intent", card_key, chat_id, stage, render_hash, operation_id)


def card_result_event(
    card_key: str,
    chat_id: int,
    stage: str,
    render_hash: str,
    operation_id: str,
    message_id: int,
) -> dict[str, Any]:
    """Written after the call. message_id does not exist before it."""
    return _event(
        "result", card_key, chat_id, stage, render_hash, operation_id, message_id=message_id
    )


def card_failure_event(
    card_key: str,
    chat_id: int,
    stage: str,
    render_hash: str,
    operation_id: str,
    error: str,
) -> dict[str, Any]:
    """Written when the client raised: we hold the error, so it never landed.

    This is what separates a known failure from the unknown of a crash. Only a
    known failure may be retried automatically.
    """
    return _event(
        "failure", card_key, chat_id, stage, render_hash, operation_id, error=error
    )


def resolve_card_copies(
    events: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, int], CardCopy]:
    """Fold the event list into the current state of every delivery copy.

    **Events must arrive oldest first.** StateStore.list_system_events_by_type
    returns ORDER BY id DESC (store.py:1815), so callers reverse it; folding
    the store's order directly makes the oldest event win and a delivered card
    read back as unknown.

    Within that order the last event wins, in arrival order -- not in stage
    order. A status that arrives out of sequence still describes what we last
    knew, and inventing a stage ranking here would silently discard
    corrections.
    """
    copies: dict[tuple[str, int], CardCopy] = {}
    for event in events:
        card_key = str(event.get("card_key") or "")
        raw_chat_id = event.get("chat_id")
        if not card_key or not isinstance(raw_chat_id, int):
            continue
        key = (card_key, raw_chat_id)
        previous = copies.get(key)
        message_id = event.get("message_id")
        copies[key] = CardCopy(
            card_key=card_key,
            chat_id=raw_chat_id,
            message_id=(
                int(message_id)
                if isinstance(message_id, int)
                else (previous.message_id if previous else None)
            ),
            stage=str(event.get("stage") or ""),
            render_hash=str(event.get("render_hash") or ""),
            delivery=_PHASE_DELIVERY.get(str(event.get("phase") or ""), "unknown"),
            operation_id=str(event.get("operation_id") or ""),
        )
    return copies
```

import에 `uuid`와 `Literal`을 추가한다.

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_telegram_card_state.py -q`
Expected: 6 passed

- [ ] **Step 5: Mutation-verify the key**

`key = (card_key, raw_chat_id)` 를 `key = (card_key,)` 로 바꾸고(그리고 타입 오류를 피하기 위해 dict 타입 주석을 완화하고) `test_one_logical_card_keeps_a_separate_copy_per_chat` 과 `test_a_later_stage_supersedes_an_earlier_one_for_that_chat_only` 이 **둘 다** FAIL 하는지 확인한 뒤 복원한다. 하나만 실패하면 나머지 하나는 이 계약을 지키지 못한다.

- [ ] **Step 6: Commit**

```bash
git add src/maestro/integrations/telegram/ui/card_state.py tests/test_telegram_card_state.py
git commit -m "feat(telegram-ui): key card state by (card_key, chat_id) with send intent"
```

---

### Task 2: 전송 — intent → send → result

**Files:**
- Create: `src/maestro/integrations/telegram/ui/lifecycle.py`
- Test: `tests/test_telegram_card_lifecycle.py` (신규)

**Interfaces:**
- Consumes: Task 1의 `card_intent_event`, `card_result_event`, `resolve_card_copies`, `EVENT_TYPE`
- Produces:
  - `CardLifecycleManager(store, audit, client, *, chat_ids: Sequence[int])`
  - `.deliver(run_id: str, card_key: str, stage: str, rendered: RenderedCard) -> dict[str, Any]` — 반환 `{"sent": tuple[int, ...], "failed": tuple[int, ...]}`
  - `.render_hash(rendered: RenderedCard) -> str`

- [ ] **Step 1: Write the failing tests**

`tests/test_telegram_card_lifecycle.py`:

```python
import pytest

from maestro.integrations.telegram.ui.card_state import EVENT_TYPE, resolve_card_copies
from maestro.integrations.telegram.ui.cards import RenderedCard
from maestro.integrations.telegram.ui.lifecycle import CardLifecycleManager
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore

CARD = RenderedCard(text="📩 승인해 주세요", reply_markup=None)


class FakeClient:
    def __init__(self, *, fail_for: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()
        self.next_message_id = 5000

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.fail_for:
            raise RuntimeError(f"telegram refused chat {chat_id}")
        self.sent.append((chat_id, text))
        self.next_message_id += 1
        return {"message_id": self.next_message_id}


def _manager(tmp_path, client, chat_ids=(100, 200)):
    store = StateStore(str(tmp_path / "state.db"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    return store, CardLifecycleManager(store, audit, client, chat_ids=chat_ids)


def test_delivery_writes_intent_before_calling_telegram(tmp_path):
    """The intent must be durable before the side effect, not after it."""
    order: list[str] = []

    class RecordingClient(FakeClient):
        def send_message(self, chat_id, text, reply_markup=None):
            order.append("send")
            return super().send_message(chat_id, text, reply_markup)

    store, manager = _manager(tmp_path, RecordingClient(), chat_ids=(100,))
    original = store.save_system_event

    def spy(run_id, event_type, payload):
        if event_type == EVENT_TYPE:
            order.append(str(payload.get("phase")))
        return original(run_id, event_type, payload)

    store.save_system_event = spy

    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert order == ["intent", "send", "result"]


def test_a_crash_between_send_and_result_leaves_an_undelivered_intent(tmp_path):
    """Exactly the window the spec amendment exists to make visible."""

    class CrashingClient(FakeClient):
        def send_message(self, chat_id, text, reply_markup=None):
            super().send_message(chat_id, text, reply_markup)
            raise RuntimeError("process died after Telegram accepted it")

    store, manager = _manager(tmp_path, CrashingClient(), chat_ids=(100,))

    result = manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert result["failed"] == (100,)
    copy = _copies_from_store(store)[("approval:appr_1", 100)]
    # Telegram accepted it and then we died: not "failed", "unknown".
    assert copy.delivery == "unknown"
    assert copy.message_id is None


def test_every_chat_gets_its_own_copy(tmp_path):
    store, manager = _manager(tmp_path, FakeClient())

    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    copies = _copies_from_store(store)
    assert sorted(chat_id for _, chat_id in copies) == [100, 200]
    assert copies[("approval:appr_1", 100)].message_id != copies[
        ("approval:appr_1", 200)
    ].message_id


def test_one_failing_chat_does_not_block_the_others(tmp_path):
    """The reminder path already works this way; the card path must too."""
    store, manager = _manager(tmp_path, FakeClient(fail_for={100}))

    result = manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert result["sent"] == (200,)
    assert result["failed"] == (100,)
    copies = _copies_from_store(store)
    # A caught exception is a *known* failure, so this one is retryable.
    assert copies[("approval:appr_1", 100)].delivery == "failed"
    assert copies[("approval:appr_1", 200)].delivery == "confirmed"


def _copies_from_store(store):
    """Read back the way lifecycle.py does: newest-first, so reverse it."""
    rows = store.list_system_events_by_type(EVENT_TYPE, limit=2000)
    return resolve_card_copies([row["payload"] for row in reversed(rows)])


def test_the_render_hash_is_stable_for_equal_content(tmp_path):
    _, manager = _manager(tmp_path, FakeClient())
    same = RenderedCard(text=CARD.text, reply_markup=None)

    assert manager.render_hash(CARD) == manager.render_hash(same)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_telegram_card_lifecycle.py -v`
Expected: FAIL — `ModuleNotFoundError: ... 'lifecycle'`

- [ ] **Step 3: Implement the manager**

`src/maestro/integrations/telegram/ui/lifecycle.py`:

```python
"""The only stateful component in ui/.

cards.py renders and knows nothing else; this module owns the store and the
Telegram client. Every send records its intent first, so a process that dies
between the API call and the write leaves a visible undelivered copy instead
of a card nobody will ever update again.
"""

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from maestro.integrations.telegram.ui.card_state import (
    EVENT_TYPE,
    card_intent_event,
    card_result_event,
)
from maestro.integrations.telegram.ui.cards import RenderedCard


class CardLifecycleManager:
    def __init__(
        self,
        store: Any,
        audit: Any,
        client: Any,
        *,
        chat_ids: Sequence[int],
    ) -> None:
        self.store = store
        self.audit = audit
        self.client = client
        self.chat_ids = tuple(chat_ids)

    @staticmethod
    def render_hash(rendered: RenderedCard) -> str:
        payload = f"{rendered.text}\x00{rendered.reply_markup!r}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def deliver(
        self,
        run_id: str,
        card_key: str,
        stage: str,
        rendered: RenderedCard,
    ) -> dict[str, Any]:
        """Send this card to every chat, one delivery copy at a time."""
        render_hash = self.render_hash(rendered)
        sent: list[int] = []
        failed: list[int] = []
        for chat_id in self.chat_ids:
            operation_id = new_operation_id()
            self.store.save_system_event(
                run_id,
                EVENT_TYPE,
                card_intent_event(card_key, chat_id, stage, render_hash, operation_id),
            )
            try:
                response = self._send(chat_id, rendered)
            except Exception as exc:  # noqa: BLE001 - one chat must not stop the rest
                # We hold the exception, so the message did not land. Recording
                # that is what makes this attempt safe to retry later; without
                # it the copy would be indistinguishable from a crash and
                # would have to be left alone.
                self.store.save_system_event(
                    run_id,
                    EVENT_TYPE,
                    card_failure_event(
                        card_key, chat_id, stage, render_hash, operation_id, str(exc)
                    ),
                )
                failed.append(chat_id)
                continue
            message_id = self._message_id(response)
            if message_id is None:
                self.store.save_system_event(
                    run_id,
                    EVENT_TYPE,
                    card_failure_event(
                        card_key, chat_id, stage, render_hash, operation_id, "no message_id"
                    ),
                )
                failed.append(chat_id)
                continue
            self.store.save_system_event(
                run_id,
                EVENT_TYPE,
                card_result_event(
                    card_key, chat_id, stage, render_hash, operation_id, message_id
                ),
            )
            sent.append(chat_id)
        return {"sent": tuple(sent), "failed": tuple(failed)}

    def _send(self, chat_id: int, rendered: RenderedCard) -> Mapping[str, Any] | None:
        try:
            return self.client.send_message(
                chat_id, rendered.text, reply_markup=rendered.reply_markup
            )
        except TypeError:
            return self.client.send_message(chat_id, rendered.text)

    @staticmethod
    def _message_id(response: Mapping[str, Any] | None) -> int | None:
        if not isinstance(response, Mapping):
            return None
        value = response.get("message_id")
        return value if isinstance(value, int) else None
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_telegram_card_lifecycle.py -q && .venv/bin/python -m pytest tests/ -q`

- [ ] **Step 5: Mutation-verify the ordering**

intent 기록 줄을 `_send` 호출 **뒤로** 옮기고, `test_delivery_writes_intent_before_calling_telegram` 과 `test_a_crash_between_send_and_result_leaves_an_undelivered_intent` 이 **둘 다** FAIL 하는지 확인한 뒤 복원한다.

- [ ] **Step 6: Commit**

```bash
git add src/maestro/integrations/telegram/ui/lifecycle.py tests/test_telegram_card_lifecycle.py
git commit -m "feat(telegram-ui): deliver cards per chat, recording intent before the send"
```

---

### Task 3: sweep 갱신 — 단계 변화 시 edit, 동일하면 생략

**Files:**
- Modify: `src/maestro/integrations/telegram/ui/lifecycle.py`
- Test: `tests/test_telegram_card_lifecycle.py`

**Interfaces:**
- Consumes: Task 2의 `CardLifecycleManager`
- Produces: `.refresh(run_id: str, card_key: str, stage: str, rendered: RenderedCard) -> dict[str, Any]`

- [ ] **Step 1: Write the failing tests**

```python
def test_refresh_edits_the_existing_message_per_chat(tmp_path):
    client = FakeClient()
    store, manager = _manager(tmp_path, client)
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh("run_1", "approval:appr_1", "in_progress", progressed)

    assert sorted(result["edited"]) == [100, 200]
    assert [chat_id for chat_id, _, _ in client.edited] == [100, 200]


def test_an_unchanged_render_is_not_sent_again(tmp_path):
    """Telegram answers 'message is not modified' and it costs an API call."""
    client = FakeClient()
    _, manager = _manager(tmp_path, client)
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    result = manager.refresh("run_1", "approval:appr_1", "pending", CARD)

    assert result["edited"] == ()
    assert result["skipped"] == (100, 200)
    assert client.edited == []


def test_a_known_failed_copy_is_sent_again(tmp_path):
    """A caught exception means it never landed, so retrying is safe."""
    client = FakeClient(fail_for={100})
    store, manager = _manager(tmp_path, client)
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    client.fail_for = set()
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh("run_1", "approval:appr_1", "in_progress", progressed)

    assert 100 in result["sent"]
    assert 200 in result["edited"]


def test_an_ambiguous_copy_is_never_resent(tmp_path):
    """The crash window: Telegram may already hold this card.

    Resending would post a second card we have no message_id for, so it never
    updates -- it sits at the old stage forever while the real one moves on.
    For an approval card with buttons and a deadline, an operator reading the
    stale copy concludes the decision is still outstanding.
    """
    client = FakeClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    # Only an intent: exactly what a process death mid-send leaves behind.
    store.save_system_event(
        "run_1",
        EVENT_TYPE,
        card_intent_event("approval:appr_1", 100, "pending", "h1", "op-crashed"),
    )
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh("run_1", "approval:appr_1", "in_progress", progressed)

    assert client.sent == []
    assert client.edited == []
    assert result["ambiguous"] == (100,)


def test_a_delivered_card_is_not_resent_after_a_round_trip_through_the_store(tmp_path):
    """The integration failure the unit tests cannot see.

    resolve_card_copies folds oldest-first, but the store returns newest-first.
    Hand-built ascending lists in the unit tests hide that; only a real read
    back out of StateStore catches it. Getting this wrong makes every delivered
    card look unknown, and the sweep escalates all of them.
    """
    client = FakeClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    sent_after_first = len(client.sent)

    result = manager.refresh("run_1", "approval:appr_1", "pending", CARD)

    assert len(client.sent) == sent_after_first
    assert result["skipped"] == (100,)
    assert result["ambiguous"] == ()


def test_an_edit_failure_falls_back_to_a_new_message(tmp_path):
    """48h expiry or a deleted message must not strand the card."""
    client = FakeClient(edit_fails=True)
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh("run_1", "approval:appr_1", "in_progress", progressed)

    assert result["sent"] == (100,)
    copies = resolve_card_copies(
        [row["payload"] for row in store.list_system_events_by_type(EVENT_TYPE)]
    )
    assert copies[("approval:appr_1", 100)].message_id == client.next_message_id
```

`FakeClient` 를 확장한다:

```python
class FakeClient:
    def __init__(self, *, fail_for=None, edit_fails=False):
        self.sent = []
        self.edited = []
        self.fail_for = fail_for or set()
        self.edit_fails = edit_fails
        self.next_message_id = 5000

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.fail_for:
            raise RuntimeError(f"telegram refused chat {chat_id}")
        self.sent.append((chat_id, text))
        self.next_message_id += 1
        return {"message_id": self.next_message_id}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        if self.edit_fails:
            raise RuntimeError("message to edit not found")
        self.edited.append((chat_id, message_id, text))
        return {"message_id": message_id}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_telegram_card_lifecycle.py -k refresh -v`
Expected: FAIL — `AttributeError: 'CardLifecycleManager' object has no attribute 'refresh'`

- [ ] **Step 3: Implement refresh**

```python
    def refresh(
        self,
        run_id: str,
        card_key: str,
        stage: str,
        rendered: RenderedCard,
    ) -> dict[str, Any]:
        """Bring every delivery copy up to the current stage.

        A copy with no message_id was never delivered, so it is sent rather
        than edited -- that is the repair path for a send that failed or was
        interrupted after Telegram accepted it.
        """
        render_hash = self.render_hash(rendered)
        copies = self._copies(card_key)
        edited: list[int] = []
        skipped: list[int] = []
        sent: list[int] = []
        failed: list[int] = []
        ambiguous: list[int] = []
        for chat_id in self.chat_ids:
            copy = copies.get((card_key, chat_id))
            if (
                copy is not None
                and copy.delivery == "confirmed"
                and copy.render_hash == render_hash
            ):
                skipped.append(chat_id)
                continue
            if copy is not None and copy.delivery == "unknown":
                # We do not know whether Telegram already has this card, and
                # the Bot API gives us no way to ask. Sending again is how a
                # duplicate is created; staying silent is how one is avoided.
                # The operator is told through the plain-text escalation path
                # instead (Task 7), which does not add another button-bearing
                # card for the same approval.
                ambiguous.append(chat_id)
                self._escalate_ambiguous(run_id, card_key, chat_id, stage)
                continue
            if copy is None or copy.message_id is None:
                outcome = self._deliver_one(run_id, card_key, stage, rendered, chat_id)
                (sent if outcome else failed).append(chat_id)
                continue
            operation_id = new_operation_id()
            self.store.save_system_event(
                run_id,
                EVENT_TYPE,
                card_intent_event(card_key, chat_id, stage, render_hash, operation_id),
            )
            try:
                self.client.edit_message_text(
                    chat_id,
                    copy.message_id,
                    rendered.text,
                    reply_markup=rendered.reply_markup,
                )
            except Exception as exc:  # noqa: BLE001 - fall back to a fresh message
                # An edit that raised did not change the message, and we still
                # hold the original message_id, so this is a known failure --
                # not the ambiguous case. A fresh send is the documented
                # fallback for a 48h-expired or deleted message.
                self.store.save_system_event(
                    run_id,
                    EVENT_TYPE,
                    card_failure_event(
                        card_key, chat_id, stage, render_hash, operation_id, str(exc)
                    ),
                )
                outcome = self._deliver_one(run_id, card_key, stage, rendered, chat_id)
                (sent if outcome else failed).append(chat_id)
                continue
            self.store.save_system_event(
                run_id,
                EVENT_TYPE,
                card_result_event(
                    card_key, chat_id, stage, render_hash, operation_id, copy.message_id
                ),
            )
            edited.append(chat_id)
        return {
            "edited": tuple(edited),
            "skipped": tuple(skipped),
            "sent": tuple(sent),
            "failed": tuple(failed),
            "ambiguous": tuple(ambiguous),
        }

    def _copies(self, card_key: str):
        from maestro.integrations.telegram.ui.card_state import resolve_card_copies

        # list_system_events_by_type is ORDER BY id DESC (store.py:1815) and
        # resolve_card_copies folds oldest-first, so this must be reversed.
        # Feeding the store's order straight in makes a delivered card read
        # back as unknown, and the sweep then treats it as needing repair.
        rows = self.store.list_system_events_by_type(EVENT_TYPE, limit=2000)
        payloads = [row["payload"] for row in reversed(rows)]
        return {
            key: copy
            for key, copy in resolve_card_copies(payloads).items()
            if key[0] == card_key
        }
```

`deliver` 의 chat 루프 본문을 `_deliver_one(run_id, card_key, stage, rendered, chat_id) -> bool` 로 추출하고 `deliver` 와 `refresh` 가 공유하게 한다. 같은 코드를 두 벌 두지 않는다.

- [ ] **Step 4: Run everything**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests --output-format=concise`

- [ ] **Step 5: Mutation-verify the hash skip**

`copy.render_hash == render_hash` 를 `False` 로 바꾸고 `test_an_unchanged_render_is_not_sent_again` 이 FAIL 하는지 확인한 뒤 복원한다.

- [ ] **Step 6: Commit**

```bash
git add src/maestro/integrations/telegram/ui/lifecycle.py tests/test_telegram_card_lifecycle.py
git commit -m "feat(telegram-ui): refresh each copy in place, repairing undelivered ones"
```

---

### Task 4: 데일리 요약 카드 렌더러

**Files:**
- Modify: `src/maestro/integrations/telegram/ui/cards.py`, `src/maestro/integrations/telegram/ui/catalog.py`
- Test: `tests/test_telegram_cards.py` (기존 파일에 추가)

**Interfaces:**
- Consumes: 없음 (순수 함수)
- Produces: `render_daily_card(signal_run_id: str, groups: Sequence[Mapping[str, Any]]) -> RenderedCard`

- [ ] **Step 1: Write the failing tests**

```python
def test_the_daily_card_lists_every_approval_group_with_its_stage():
    card = render_daily_card(
        "signal_1",
        [
            {"label": "트랑필로", "stage": "done"},
            {"label": "크레센도", "stage": "pending"},
        ],
    )

    assert "트랑필로" in card.text
    assert "크레센도" in card.text
    assert "✅" in card.text
    assert "⏳" in card.text


def test_the_daily_card_carries_no_action_buttons():
    """Only the approval card is actionable; a parent with buttons would let a
    callback bind to the wrong group."""
    card = render_daily_card("signal_1", [{"label": "트랑필로", "stage": "pending"}])

    assert card.reply_markup is None


def test_a_mixed_outcome_is_rendered_without_collapsing_to_one_stage():
    card = render_daily_card(
        "signal_1",
        [
            {"label": "트랑필로", "stage": "done"},
            {"label": "크레센도", "stage": "attention"},
        ],
    )

    assert "✅" in card.text
    assert "⚠️" in card.text
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_telegram_cards.py -k daily -v`
Expected: FAIL — `ImportError: cannot import name 'render_daily_card'`

- [ ] **Step 3: Add the stage labels to catalog.py**

```python
CARD_STAGE_LABELS = {
    "pending": "⏳ 대기",
    "in_progress": "🔵 진행 중",
    "done": "✅ 완료",
    "attention": "⚠️ 확인 필요",
}

DAILY_CARD_TITLE = "📊 오늘의 투자 현황"
```

- [ ] **Step 4: Implement the renderer**

```python
def render_daily_card(
    signal_run_id: str,
    groups: Sequence[Mapping[str, Any]],
) -> RenderedCard:
    """Read-only parent card: one line per approval group.

    No buttons. Each group is approved independently against its own
    approval_id, so a button here would have nothing unambiguous to bind to.
    """
    lines = [DAILY_CARD_TITLE, ""]
    for group in groups:
        label = str(group.get("label") or "?")
        stage = CARD_STAGE_LABELS.get(str(group.get("stage") or ""), "⏳ 대기")
        lines.append(f"• {label} — {stage}")
    return RenderedCard(text=_clamp("\n".join(lines)), reply_markup=None)
```

- [ ] **Step 5: Run everything**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests --output-format=concise`

- [ ] **Step 6: Commit**

```bash
git add src/maestro/integrations/telegram/ui/ tests/test_telegram_cards.py
git commit -m "feat(telegram-ui): render the read-only daily summary card"
```

---

### Task 5: 승인 카드를 lifecycle 경유로 전환

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py:1409` 부근, `poll_once`
- Test: `tests/test_telegram_approval_card.py` (신규)

**Interfaces:**
- Consumes: Task 2·3의 `CardLifecycleManager`
- Produces: `TelegramCommandRouter._card_manager` 속성, `_sweep_lifecycle_cards()`

- [ ] **Step 1: Write the failing test**

```python
def test_an_approval_stage_change_edits_the_card_instead_of_sending_a_new_one(tmp_path):
    """Replaces the 'Maestro live order update' stream with one edited card."""
    router, store, client = _router_with_cards(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    router._sweep_lifecycle_cards()
    sent_before = len(client.sent)

    _record_orders_in_progress(store, envelope)
    router._sweep_lifecycle_cards()

    assert len(client.sent) == sent_before  # no new message
    assert client.edited, "the existing card should have been edited"
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_card.py -v`
Expected: FAIL — `AttributeError: ... '_sweep_lifecycle_cards'`

- [ ] **Step 3: Register the sweep**

`poll_once` 의 sweep 튜플에 추가한다. 기존 두 sweep과 같은 예외 격리를 그대로 받는다:

```python
        for sweep in (
            self._sweep_pending_approvals,
            self._sweep_recovery_notifications,
            self._sweep_lifecycle_cards,
        ):
```

- [ ] **Step 4: Implement the stage machine**

**이벤트 *유형*만 보면 안 된다. payload를 읽어야 한다.** 운영 DB에서 확인한 실제 페이로드가 그 이유를 보여준다 — 2026-08-12 US 런의 `signal_approval_completed`는 `approval_status='approved'`이면서 `orders_failed=1`이다. 유형만 보고 `done`으로 매핑하면 **절반만 집행되고 중단된 로테이션이 "✅ 완료"로 표시된다.**

`ui/approval_stage.py` (신규, 순수 함수)에 상태 머신을 둔다. handlers가 이벤트를 모아 넘기고, `ui/`는 판정만 한다:

```python
STAGE_RANK = {"pending": 0, "in_progress": 1, "done": 2, "attention": 3}


def approval_stage(
    ack: Mapping[str, Any] | None,
    completed: Mapping[str, Any] | None,
    unresolved_recovery: bool,
) -> str:
    """Decide one approval card's stage from payloads, not event types.

    attention outranks everything: a run that needs a human must never be
    displayed as finished, and the stage must not walk back to a calmer value
    on a later sweep.
    """
    if unresolved_recovery:
        return "attention"
    if completed is not None:
        failed = int(completed.get("orders_failed") or 0)
        status = str(completed.get("approval_status") or "")
        if failed > 0 or status not in {"approved", "not_required"}:
            return "attention"
        return "done"
    if ack is not None:
        status = str(ack.get("status") or "")
        if status == "approved":
            return "in_progress"
        if status in {"rejected", "expired"}:
            return "done"
        return "attention"
    return "pending"
```

**역행 금지**: sweep은 저장된 직전 단계와 비교해 `STAGE_RANK`가 낮아지는 전이를 무시한다. 순서가 뒤바뀐 이벤트 도착이 "⚠️ 확인 필요"를 "🔵 진행 중"으로 되돌리면 운영자가 개입을 놓친다.

**recovery 상관관계**: `live_order_recovery_required`에는 `approval_id`가 없다 — `order_id`뿐이다(운영 DB 확인). 승인과 잇는 경로는 `live_order_batch_lifecycle`의 `items[].request.approval_id` 또는 `live_order_submit_intent`의 request다. `unresolved_recovery`는 "이 승인의 order_id 중 recovery_required가 있고, 그 뒤에 해당 order의 terminal lifecycle이 없는 것"으로 판정한다. terminal이 왔으면 해제한다.

- [ ] **Step 4b: Test every transition**

```python
@pytest.mark.parametrize(
    "ack,completed,recovery,expected",
    [
        (None, None, False, "pending"),
        ({"status": "approved"}, None, False, "in_progress"),
        ({"status": "rejected"}, None, False, "done"),
        ({"status": "expired"}, None, False, "done"),
        # The 2026-08-12 shape: approved, but one order failed.
        (
            {"status": "approved"},
            {"approval_status": "approved", "orders_failed": 1},
            False,
            "attention",
        ),
        (
            {"status": "approved"},
            {"approval_status": "approved", "orders_failed": 0},
            False,
            "done",
        ),
        ({"status": "approved"}, {"approval_status": "approved", "orders_failed": 0}, True,
         "attention"),
    ],
)
def test_the_stage_machine_reads_payloads_not_event_types(ack, completed, recovery, expected):
    assert approval_stage(ack, completed, recovery) == expected


def test_the_stage_never_walks_back_to_a_calmer_value(tmp_path):
    router, store, client = _router_with_cards(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    _record_recovery_required(store, envelope)
    router._sweep_lifecycle_cards()

    _record_late_in_progress_event(store, envelope)
    router._sweep_lifecycle_cards()

    assert "⚠️" in client.edited[-1][2]
```

- [ ] **Step 4c: Wire the sweep**

`_sweep_lifecycle_cards` 는 활성 승인 봉투를 조회해 각 `approval:<approval_id>` 카드의 단계를 위 함수로 판정하고 `self._card_manager.refresh(...)` 를 호출한다. 승인 그룹이 2개 이상인 signal run에만 `daily:<signal_run_id>` 부모 카드를 추가로 갱신한다.

- [ ] **Step 5: Keep the legacy path**

`_sweep_pending_approvals` 의 기존 전송을 **삭제하지 않는다.** 스펙 「단계 2의 안전망」에 따라 병행 유지한다. 이를 테스트로 고정한다:

```python
def test_the_legacy_notification_path_still_runs(tmp_path):
    """Removing it is stage 5, after card delivery is proven in production."""
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")

    router._sweep_pending_approvals()

    assert client.sent, "the legacy path must still deliver"
```

- [ ] **Step 6: Run everything**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests --output-format=concise`

- [ ] **Step 7: Commit**

```bash
git add src/maestro/integrations/telegram/handlers.py tests/test_telegram_approval_card.py
git commit -m "feat(telegram-ui): drive the approval card from the lifecycle sweep"
```

---

### Task 6: 노옵 한 줄 알림

**Files:**
- Modify: `src/maestro/integrations/telegram/ui/catalog.py`, `src/maestro/cli.py`
- Test: `tests/test_telegram_cards.py`

**Interfaces:**
- Produces: `catalog.NO_ACTION_NOTICE`

- [ ] **Step 1: Write the failing test**

```python
def test_the_no_action_notice_is_one_line_and_names_no_internals():
    assert "\n" not in NO_ACTION_NOTICE
    assert "run_id" not in NO_ACTION_NOTICE
    assert "signal" not in NO_ACTION_NOTICE.lower()
```

- [ ] **Step 2: Run it**

Expected: FAIL — `ImportError: cannot import name 'NO_ACTION_NOTICE'`

- [ ] **Step 3: Add the string**

```python
NO_ACTION_NOTICE = "오늘은 매매할 것이 없어요."
```

- [ ] **Step 4: Use it where the daily run reports no action**

`cli.py` 의 `daily-signal-approval` 이 `status=no_action` 을 출력하는 분기에서 이 문구를 텔레그램으로 1회 전송한다. 카드가 아니라 한 줄 알림이므로 lifecycle을 거치지 않는다.

- [ ] **Step 5: Run everything and commit**

```bash
git add src/maestro/integrations/telegram/ui/catalog.py src/maestro/cli.py tests/
git commit -m "feat(telegram-ui): announce a no-action day in one line"
```

---

### Task 7: 연속 3회 실패 시 폴백 알림과 degraded 헬스체크

**Files:**
- Modify: `src/maestro/integrations/telegram/ui/lifecycle.py`, `src/maestro/integrations/telegram/ui/catalog.py`
- Test: `tests/test_telegram_card_lifecycle.py`

**Interfaces:**
- Produces: `catalog.CARD_FALLBACK_TEMPLATE`, `catalog.CARD_AMBIGUOUS_TEMPLATE`, `CardLifecycleManager.consecutive_failures(card_key, chat_id) -> int`, `CardLifecycleManager._escalate_ambiguous(run_id, card_key, chat_id, stage)`

> **Task 3 의존**: `_escalate_ambiguous` 는 Task 3의 `refresh` 가 호출하지만 정의는 여기다. Task 3을 먼저 구현할 경우 이 메서드를 no-op 스텁으로 두고 Task 7에서 채운다. 스텁 상태에서도 `test_an_ambiguous_copy_is_never_resent` 는 통과해야 한다 — 그 테스트가 검증하는 것은 "재전송하지 않는다"이지 "알린다"가 아니다.

**ambiguous 에스컬레이션이 폴백과 같은 경로인 이유.** 둘 다 "카드 렌더링을 신뢰할 수 없거나 카드로 말할 수 없는 상황"이고, 둘 다 **버튼 없는 고정 텍스트**여야 한다. ambiguous에 카드를 다시 보내면 같은 승인에 버튼 달린 카드가 두 장 생기는데, 그것이 바로 재전송을 막은 이유다. 알림은 상태를 알릴 뿐 조작 수단을 늘리지 않는다.

같은 `(card_key, chat_id, stage)`에 대한 ambiguous 통지는 **한 번만** 보낸다. 매 sweep마다 반복하면 2분 주기로 같은 문구가 쌓인다. 통지 발송을 `telegram_ui_card_ambiguous_notified` 이벤트로 기록해 중복을 억제한다.

- [ ] **Step 1: Write the failing tests**

```python
def test_three_consecutive_failures_send_a_plain_text_fallback(tmp_path):
    """The operator must not lose the thread because the renderer is broken.

    The fallback deliberately does not go through cards.py -- if rendering is
    what is failing, rendering the fallback would fail too.
    """
    client = FakeClient(fail_for={100})
    store, manager = _manager(tmp_path, client, chat_ids=(100,))

    for _ in range(3):
        manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    client.fail_for = set()
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert any("appr_1" in text and "확인" in text for _, text in client.sent)


def test_two_failures_do_not_trigger_the_fallback(tmp_path):
    client = FakeClient(fail_for={100})
    _, manager = _manager(tmp_path, client, chat_ids=(100,))

    for _ in range(2):
        manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert manager.consecutive_failures("approval:appr_1", 100) == 2
```

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — `AttributeError: ... 'consecutive_failures'`

- [ ] **Step 3: Implement**

실패 시 `telegram_ui_card_failure` 이벤트를 남기고, 성공 시 그 카운터를 리셋하는 이벤트를 남긴다. 3회 연속이면 `CARD_FALLBACK_TEMPLATE.format(card_key=..., stage=...)` 을 `cards.py` 를 거치지 않고 직접 전송한다. `telegram_ui` 헬스체크를 degraded로 전환한다.

- [ ] **Step 4: Run everything, mutation-verify (임계값을 5로 바꾸면 첫 테스트가 FAIL), commit**

```bash
git add src/maestro/integrations/telegram/ui/ tests/test_telegram_card_lifecycle.py
git commit -m "feat(telegram-ui): fall back to plain text after three failed renders"
```

---

## Self-Review

**Spec coverage**

| 스펙 요구 | 태스크 |
|---|---|
| `(card_key, chat_id, message_id, 단계, 렌더 해시)` 기록 | Task 1 |
| 전송 전 intent (스펙 개정 `23066f5`) | Task 1, 2 |
| 논리 카드 ↔ 전달 복사본 분리 | Task 1, 2 |
| 라이프사이클 카드 매니저 | Task 2, 3 |
| 해시 동일 시 edit 생략 | Task 3 |
| edit 실패 → 새 메시지 폴백 | Task 3 |
| 데일리 요약 카드 (읽기 전용, 버튼 없음) | Task 4 |
| 승인 그룹 1개면 부모 카드 생략 | Task 5 (sweep이 그룹 수로 분기) |
| 순서 뒤바뀐 이벤트 도착 | Task 1 (`test_events_are_ordered_by_arrival...`) |
| 혼합 상태 렌더 | Task 4 |
| 노옵 한 줄 | Task 6 |
| 기존 알림 병행 유지 | Task 5 Step 5 (테스트로 고정) |
| 연속 3회 실패 → 폴백 + degraded | Task 7 |
| 이벤트 fold 순서 (store는 DESC) | Task 1 (`test_events_must_be_folded_oldest_first`), Task 3 (store 왕복 통합 테스트) |
| 재시도 가능한 `duplicate_key` | Task 1 (`operation_id`) |
| ambiguous ≠ 미전달 | Task 1 (`delivery` 3값), Task 3 (재전송 금지), Task 7 (에스컬레이션) |
| payload 기반 단계 판정 + 역행 금지 | Task 5 Step 4·4b |

**남은 결정 — 실행자에게**

- Task 3의 `refresh` 는 Task 2의 chat 루프를 재사용해야 한다. `deliver` 와 `refresh` 가 각자 전송 코드를 갖게 되면 intent 기록 순서가 두 곳에서 갈라진다.
- Task 5에서 기존 테스트가 깨지면, **기존 알림 경로를 기대하는 단정은 유지한다.** 그 경로 제거는 단계 5다.
- `ui/` 는 handlers·orchestrator·execution을 임포트하지 않는다. Task 5에서 단계 판정 로직이 필요해지면 그 판정은 `handlers.py` 쪽에 두고 `ui/` 에는 렌더 데이터만 넘긴다.

**범위 밖 (확인용)**

월간 자금 카드(3b), 예외 마법사(4), 조회 카드·구 경로 제거(5), 그리고 intent만 있고 result 없는 카드의 **자가 치유**(3a-3). 단계 2는 그 상태를 *만들고 관측 가능하게* 할 뿐 고치지 않는다.

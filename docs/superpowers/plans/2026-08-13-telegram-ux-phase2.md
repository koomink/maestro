# 라이프사이클 카드 매니저 (단계 2) Implementation Plan

> **상태: 구현 완료 (2026-08-14).** 7개 태스크 전부와 두 차례 리뷰 반영까지
> `feat/telegram-ux-phase2`에 반영됐다. 계획과 달라진 두 가지는 아래 「구현에서
> 달라진 점」에 적었다. 이 문서는 이제 이력이며 남은 작업은 없다.
>
> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 승인·데일리 카드를 한 번 보내고 이후 상태 변화마다 그 메시지를 갱신하는 라이프사이클 매니저를 만들어, 연속 알림 나열을 카드 하나로 대체한다.

**Architecture:** `ui/lifecycle.py`가 유일한 상태 보유 컴포넌트다. 전달 복사본별 상태를 `telegram_ui_card` system event로 남기되 **키는 `(card_key, chat_id)`**이고, 전송은 항상 `intent → 텔레그램 호출 → result` 순서로 기록한다. poll 루프의 sweep이 원천 이벤트를 조회해 단계 변화 시 `edit_message_text`로 갱신한다. 렌더러(`cards.py`)는 순수 함수로 유지되고 네트워크·DB를 모른다.

**Tech Stack:** Python 3, SQLite state store, Telegram Bot API (`sendMessage`/`editMessageText`), Typer CLI, pytest, ruff

**Spec:** `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md` (단계 2 행, 「카드 체계 A」, 「데이터 흐름」, 「에러 처리」)

## Global Constraints

- 테스트 기준선: 계획 작성 시점 **1352 passed, 9 skipped**. 단계 2 착수 시점 1392,
  **완료 시점 1450 passed, 9 skipped** (2026-08-14). 각 태스크는 이 수를 줄이지 않는다.
- 린트: `.venv/bin/python -m ruff check src tests --output-format=concise` → `All checks passed!`
- **모든 하중 테스트는 뮤테이션으로 비공허성을 증명한다.** 구현을 되돌려 테스트가 실패하는 것을 확인하고 복원한다.
- **상태 키는 `(card_key, chat_id)`.** `card_key` 단독으로 `message_id`를 들고 있는 자료구조를 만들지 않는다. 운영 chat이 현재 하나뿐이라는 사실은 이 계약을 접을 근거가 아니다.
- **전송 전 intent를 먼저 기록한다.** `sendMessage`/`editMessageText` 호출 앞에 intent, 응답 뒤에 result. intent만 있고 result 없는 상태가 조회 가능해야 한다.
- **모르는 것을 안다고 취급하지 않는다.** intent만 남은 복사본은 *미전달*이 아니라 **전달 여부 불명(unknown)**이다. 자동 재전송하지 않는다. 관측 가능성을 만든 뒤 그 위에서 곧바로 자동 조치를 하는 것은 이 프로젝트가 오늘 이미 한 번 저지른 실수다 — 락 스펙 3회차에서 "만료된 리스를 자동 회수"가 정확히 같은 모양이었고, 같은 이유로 철회했다.
- **"예외를 잡았다"는 미전달의 증거가 아니다.** `TelegramBotClient._post`(`bot.py:148-170`)가 던지는 네 예외 중 미전달이 확정되는 것은 **텔레그램이 `ok: false`로 거절한 경우 하나뿐**이다. `TimeoutError`와 `URLError`는 수락 후 응답만 유실됐을 수 있고, 응답 파싱 실패(`ValueError`)는 오히려 전달됐을 가능성이 높다. 그리고 URLError와 `ok: false`는 **둘 다 `RuntimeError`**라 타입만으로 구분되지 않는다 — 전용 예외를 도입해야 한다(Task 2 Step 0).
- **현재 상태를 LIMIT 걸린 이벤트 조회로 재구성하지 않는다.** 이벤트 로그는 이력이고, 카드의 현재 상태는 `(card_key, chat_id)`로 직접 조회 가능한 투영 테이블에 둔다. 전역 상한으로 자르면 오래 대기한 승인 카드의 마지막 result가 범위 밖으로 밀려 "그런 카드는 없다"가 되고, 중복 전송으로 이어진다. 이벤트가 sweep마다 쌓이므로 **운영 기간이 길어지면 반드시 도달하는 상태**다.
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
| `src/maestro/integrations/telegram/ui/approval_stage.py` (신규) | 승인 payload → 진행 단계 + 주의 플래그 판정 (순수 함수) |
| `src/maestro/integrations/telegram/bot.py` | `TelegramApiRejected` 예외 — 명시적 거절과 전송 불명을 구분 가능하게 |
| `src/maestro/state/store.py` | `telegram_ui_card_state` 투영 테이블 + 이벤트·투영 원자 기록 API |
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

- [x] **Step 1: Write the failing tests**

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

- [x] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_telegram_card_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'maestro.integrations.telegram.ui.card_state'`

- [x] **Step 3: Implement the module**

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

- [x] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_telegram_card_state.py -q`
Expected: 6 passed

- [x] **Step 5: Mutation-verify the key**

`key = (card_key, raw_chat_id)` 를 `key = (card_key,)` 로 바꾸고(그리고 타입 오류를 피하기 위해 dict 타입 주석을 완화하고) `test_one_logical_card_keeps_a_separate_copy_per_chat` 과 `test_a_later_stage_supersedes_an_earlier_one_for_that_chat_only` 이 **둘 다** FAIL 하는지 확인한 뒤 복원한다. 하나만 실패하면 나머지 하나는 이 계약을 지키지 못한다.

- [x] **Step 6: Commit**

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

- [x] **Step 0: Make an explicit rejection distinguishable**

`_post`는 전송 실패(`URLError`)와 텔레그램의 명시적 거절(`ok: false`)을 **둘 다 `RuntimeError`로** 던진다. 이 상태에서 결과를 분류하려면 예외 메시지 문자열을 매칭해야 하는데, 그건 문구를 바꾸는 순간 조용히 깨진다.

`src/maestro/integrations/telegram/bot.py`:

```python
class TelegramApiRejected(RuntimeError):
    """Telegram answered ok=false: the message was definitively not delivered.

    Separate from transport failures on purpose. A timeout or a dropped
    connection may have happened after Telegram accepted the message, so those
    stay ambiguous; only an explicit rejection is safe to retry.
    """
```

`_post` 의 마지막 분기를 교체한다:

```python
        if not decoded.get("ok"):
            raise TelegramApiRejected(
                f"Telegram Bot API returned not ok for method: {method}"
            )
```

`TelegramApiRejected` 는 `RuntimeError` 하위이므로 기존에 `RuntimeError` 를 잡던 코드는 그대로 동작한다. 회귀 테스트:

```python
def test_an_ok_false_response_raises_a_distinguishable_rejection():
    """String-matching the message would break the moment the wording changes."""
    client = _client_returning({"ok": False, "description": "chat not found"})

    with pytest.raises(TelegramApiRejected):
        client.send_message(100, "hi")


def test_a_timeout_is_not_reported_as_a_rejection():
    client = _client_raising(TimeoutError("timed out"))

    with pytest.raises(TimeoutError):
        client.send_message(100, "hi")
```

- [x] **Step 1: Write the failing tests**

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
    def __init__(self, *, reject_for: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        # reject_for raises TelegramApiRejected -- a *known* non-delivery.
        # Transport ambiguity is modelled by the dedicated clients below.
        self.reject_for = reject_for or set()
        self.next_message_id = 5000

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.reject_for:
            raise TelegramApiRejected(f"telegram refused chat {chat_id}")
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


def test_a_timeout_after_telegram_accepted_stays_unknown(tmp_path):
    """The window the spec amendment exists to make visible.

    A timeout does not tell us the message failed -- Telegram may hold it and
    only the reply was lost. Recording a failure here is what would license a
    resend and duplicate an approval card.
    """

    class TimingOutClient(FakeClient):
        def send_message(self, chat_id, text, reply_markup=None):
            super().send_message(chat_id, text, reply_markup)
            raise TimeoutError("Telegram Bot API timed out for method: sendMessage")

    store, manager = _manager(tmp_path, TimingOutClient(), chat_ids=(100,))

    result = manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert result["unknown"] == (100,)
    assert result["failed"] == ()
    copy = _copies_from_store(store)[("approval:appr_1", 100)]
    assert copy.delivery == "unknown"
    assert copy.message_id is None


def test_an_explicit_rejection_is_recorded_as_a_failure(tmp_path):
    """ok=false is the one exception that proves nothing was delivered."""

    class RejectingClient(FakeClient):
        def send_message(self, chat_id, text, reply_markup=None):
            raise TelegramApiRejected("Telegram Bot API returned not ok")

    store, manager = _manager(tmp_path, RejectingClient(), chat_ids=(100,))

    result = manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert result["failed"] == (100,)
    assert _copies_from_store(store)[("approval:appr_1", 100)].delivery == "failed"


def test_an_ok_response_without_a_message_id_is_unknown(tmp_path):
    """We cannot address what we probably just created."""

    class NoIdClient(FakeClient):
        def send_message(self, chat_id, text, reply_markup=None):
            return {"ok": True}

    store, manager = _manager(tmp_path, NoIdClient(), chat_ids=(100,))

    assert manager.deliver("run_1", "approval:appr_1", "pending", CARD)["unknown"] == (100,)


def test_every_chat_gets_its_own_copy(tmp_path):
    store, manager = _manager(tmp_path, FakeClient())

    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    copies = _copies_from_store(store)
    assert sorted(chat_id for _, chat_id in copies) == [100, 200]
    assert copies[("approval:appr_1", 100)].message_id != copies[
        ("approval:appr_1", 200)
    ].message_id


def test_one_rejected_chat_does_not_block_the_others(tmp_path):
    """The reminder path already works this way; the card path must too."""
    store, manager = _manager(tmp_path, FakeClient(reject_for={100}))

    result = manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert result["sent"] == (200,)
    assert result["failed"] == (100,)
    copies = _copies_from_store(store)
    assert copies[("approval:appr_1", 100)].delivery == "failed"
    assert copies[("approval:appr_1", 200)].delivery == "confirmed"


def _copies_from_store(store, card_key="approval:appr_1"):
    """Read the projection, the same source lifecycle.py reads."""
    return store.load_card_copies(card_key)


def test_the_render_hash_is_stable_for_equal_content(tmp_path):
    _, manager = _manager(tmp_path, FakeClient())
    same = RenderedCard(text=CARD.text, reply_markup=None)

    assert manager.render_hash(CARD) == manager.render_hash(same)
```

- [x] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_telegram_card_lifecycle.py -v`
Expected: FAIL — `ModuleNotFoundError: ... 'lifecycle'`

- [x] **Step 3: Implement the manager**

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
        unknown: list[int] = []
        for chat_id in self.chat_ids:
            operation_id = new_operation_id()
            self.store.save_system_event(
                run_id,
                EVENT_TYPE,
                card_intent_event(card_key, chat_id, stage, render_hash, operation_id),
            )
            try:
                response = self._send(chat_id, rendered)
            except TelegramApiRejected as exc:
                # ok=false. Telegram looked at it and refused, so nothing was
                # delivered. This is the *only* exception that makes an attempt
                # safe to retry automatically.
                self.store.save_system_event(
                    run_id,
                    EVENT_TYPE,
                    card_failure_event(
                        card_key, chat_id, stage, render_hash, operation_id, str(exc)
                    ),
                )
                failed.append(chat_id)
                continue
            except Exception:  # noqa: BLE001 - one chat must not stop the rest
                # Timeout, dropped connection, unparseable body: any of these
                # can happen after Telegram accepted the message. Leaving the
                # intent without a result is what marks it unknown, and unknown
                # is never resent. Writing a failure event here is exactly the
                # misclassification that duplicates approval cards.
                unknown.append(chat_id)
                continue
            message_id = self._message_id(response)
            if message_id is None:
                # ok=true with no message_id means we cannot address the
                # message we probably just created. Unknown, not failed.
                unknown.append(chat_id)
                continue
            self.store.save_system_event(
                run_id,
                EVENT_TYPE,
                card_result_event(
                    card_key, chat_id, stage, render_hash, operation_id, message_id
                ),
            )
            sent.append(chat_id)
        return {
            "sent": tuple(sent),
            "failed": tuple(failed),
            "unknown": tuple(unknown),
        }

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

- [x] **Step 4: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_telegram_card_lifecycle.py -q && .venv/bin/python -m pytest tests/ -q`

- [x] **Step 5: Mutation-verify the ordering**

intent 기록 줄을 `_send` 호출 **뒤로** 옮기고, `test_delivery_writes_intent_before_calling_telegram` 과 `test_a_crash_between_send_and_result_leaves_an_undelivered_intent` 이 **둘 다** FAIL 하는지 확인한 뒤 복원한다.

- [x] **Step 6: Commit**

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

- [x] **Step 1: Write the failing tests**

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
    client = FakeClient(reject_for={100})
    store, manager = _manager(tmp_path, client)
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    client.reject_for = set()
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
    def __init__(self, *, reject_for=None, edit_fails=False):
        self.sent = []
        self.edited = []
        self.reject_for = reject_for or set()
        self.edit_fails = edit_fails
        self.next_message_id = 5000

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.reject_for:
            raise TelegramApiRejected(f"telegram refused chat {chat_id}")
        self.sent.append((chat_id, text))
        self.next_message_id += 1
        return {"message_id": self.next_message_id}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        if self.edit_fails:
            raise RuntimeError("message to edit not found")
        self.edited.append((chat_id, message_id, text))
        return {"message_id": message_id}
```

- [x] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_telegram_card_lifecycle.py -k refresh -v`
Expected: FAIL — `AttributeError: 'CardLifecycleManager' object has no attribute 'refresh'`

- [x] **Step 3: Implement refresh**

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
        # Read the projection, not the log. Reconstructing current state by
        # scanning the newest N events cannot be made correct: events accrue on
        # every sweep, so a card that has waited long enough will have its last
        # result pushed past any limit, read back as absent, and be sent again.
        return self.store.load_card_copies(card_key)
```

**투영 테이블.** `store.py` 에 추가한다:

```python
            conn.execute(
                "CREATE TABLE IF NOT EXISTS telegram_ui_card_state "
                "("
                "card_key TEXT NOT NULL, "
                "chat_id INTEGER NOT NULL, "
                "message_id INTEGER, "
                "stage TEXT NOT NULL, "
                "render_hash TEXT NOT NULL, "
                "delivery TEXT NOT NULL, "
                "operation_id TEXT NOT NULL, "
                "updated_at TEXT DEFAULT CURRENT_TIMESTAMP, "
                "PRIMARY KEY (card_key, chat_id)"
                ")"
            )
```

**이벤트와 투영은 한 트랜잭션에서 쓴다.** `writer_lock` 은 advisory flock이라 트랜잭션을 공유하지 않는다(락 스펙에서 확인한 사실). 둘이 갈라지면 투영이 낡은 채로 남아 카드가 중복된다.

```python
    def record_card_event(self, run_id: str, payload: dict[str, Any]) -> None:
        """Append the card event and update its projection atomically.

        The event log is the history 3a-3 will need; the projection is the
        current state the sweep reads. Writing them separately would let a
        crash leave the projection stale, which is the duplicate-card bug in
        another form.
        """
        payload_json = json.dumps(payload, default=str)
        with self.writer_lock("record_card_event"):
            with self._connect() as conn:
                conn.isolation_level = None
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO system_events (run_id, event_type, payload, duplicate_key) "
                    "VALUES (?, ?, ?, ?)",
                    (run_id, "telegram_ui_card", payload_json, payload.get("duplicate_key")),
                )
                conn.execute(
                    "INSERT INTO telegram_ui_card_state "
                    "(card_key, chat_id, message_id, stage, render_hash, delivery, "
                    "operation_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(card_key, chat_id) DO UPDATE SET "
                    "message_id=COALESCE(excluded.message_id, telegram_ui_card_state.message_id), "
                    "stage=excluded.stage, render_hash=excluded.render_hash, "
                    "delivery=excluded.delivery, operation_id=excluded.operation_id, "
                    "updated_at=CURRENT_TIMESTAMP",
                    (
                        payload["card_key"],
                        payload["chat_id"],
                        payload.get("message_id"),
                        payload["stage"],
                        payload["render_hash"],
                        _PHASE_DELIVERY.get(payload["phase"], "unknown"),
                        payload["operation_id"],
                    ),
                )

    def load_card_copies(self, card_key: str) -> dict[tuple[str, int], CardCopy]:
        """Current state of every delivery copy. No limit, no scan."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT card_key, chat_id, message_id, stage, render_hash, delivery, "
                "operation_id FROM telegram_ui_card_state WHERE card_key = ?",
                (card_key,),
            ).fetchall()
        return {(str(row[0]), int(row[1])): CardCopy(*row) for row in rows}
```

`lifecycle.py` 의 모든 `store.save_system_event(run_id, EVENT_TYPE, ...)` 호출을 `store.record_card_event(run_id, ...)` 로 바꾼다.

`resolve_card_copies` 는 삭제하지 않는다 — 3a-3이 시도 이력을 재구성할 때 쓰고, Task 1의 순수 테스트가 계속 그것을 검증한다.

- [x] **Step 3b: Pin that an old card is not resent**

```python
def test_a_card_older_than_any_event_window_is_still_found(tmp_path):
    """The projection is why this works; a scan of recent events would not.

    An approval can wait hours while sweeps append events for other cards.
    Reconstructing state from the newest N events would lose this card and
    post a duplicate.
    """
    client = FakeClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "approval:appr_old", "pending", CARD)
    for index in range(3000):
        manager.deliver("run_noise", f"daily:noise_{index}", "pending", CARD)
    sent_before = len(client.sent)

    result = manager.refresh("run_1", "approval:appr_old", "pending", CARD)

    assert result["skipped"] == (100,)
    assert len(client.sent) == sent_before
```

`deliver` 의 chat 루프 본문을 `_deliver_one(run_id, card_key, stage, rendered, chat_id) -> bool` 로 추출하고 `deliver` 와 `refresh` 가 공유하게 한다. 같은 코드를 두 벌 두지 않는다.

- [x] **Step 4: Run everything**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests --output-format=concise`

- [x] **Step 5: Mutation-verify the hash skip**

`copy.render_hash == render_hash` 를 `False` 로 바꾸고 `test_an_unchanged_render_is_not_sent_again` 이 FAIL 하는지 확인한 뒤 복원한다.

- [x] **Step 6: Commit**

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

- [x] **Step 1: Write the failing tests**

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

- [x] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_telegram_cards.py -k daily -v`
Expected: FAIL — `ImportError: cannot import name 'render_daily_card'`

- [x] **Step 3: Add the stage labels to catalog.py**

```python
CARD_STAGE_LABELS = {
    "pending": "⏳ 대기",
    "in_progress": "🔵 진행 중",
    "done": "✅ 완료",
    "attention": "⚠️ 확인 필요",
}

DAILY_CARD_TITLE = "📊 오늘의 투자 현황"
```

- [x] **Step 4: Implement the renderer**

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

- [x] **Step 5: Run everything**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests --output-format=concise`

- [x] **Step 6: Commit**

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

- [x] **Step 1: Write the failing test**

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

- [x] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_telegram_approval_card.py -v`
Expected: FAIL — `AttributeError: ... '_sweep_lifecycle_cards'`

- [x] **Step 3: Register the sweep**

`poll_once` 의 sweep 튜플에 추가한다. 기존 두 sweep과 같은 예외 격리를 그대로 받는다:

```python
        for sweep in (
            self._sweep_pending_approvals,
            self._sweep_recovery_notifications,
            self._sweep_lifecycle_cards,
        ):
```

- [x] **Step 4: Implement the stage machine**

**이벤트 *유형*만 보면 안 된다. payload를 읽어야 한다.** 운영 DB에서 확인한 실제 페이로드가 그 이유를 보여준다 — 2026-08-12 US 런의 `signal_approval_completed`는 `approval_status='approved'`이면서 `orders_failed=1`이다. 유형만 보고 `done`으로 매핑하면 **절반만 집행되고 중단된 로테이션이 "✅ 완료"로 표시된다.**

`ui/approval_stage.py` (신규, 순수 함수)에 상태 머신을 둔다. handlers가 이벤트를 모아 넘기고, `ui/`는 판정만 한다:

**두 축을 분리한다.** 초안은 `attention`을 진행 단계의 최상위 값으로 두고 역행을 금지했는데, 그러면 **복구가 끝나도 카드가 영원히 "⚠️ 확인 필요"에 갇힌다** — 같은 문단에서 "terminal이 오면 해제한다"고 써놓고 순위 규칙이 그 전이를 막는 자기모순이었다. 진행은 되돌릴 수 없지만 사고는 해소된다. 서로 다른 것이므로 따로 모델링한다.

```python
PROGRESS_RANK = {"pending": 0, "in_progress": 1, "done": 2}


def approval_progress(
    ack: Mapping[str, Any] | None,
    completed: Mapping[str, Any] | None,
) -> str:
    """How far this approval got. Monotonic -- never walks back."""
    if completed is not None:
        return "done"
    if ack is not None:
        status = str(ack.get("status") or "")
        if status == "approved":
            return "in_progress"
        if status in {"rejected", "expired"}:
            return "done"
    return "pending"


def approval_needs_attention(
    ack: Mapping[str, Any] | None,
    completed: Mapping[str, Any] | None,
    unresolved_recovery: bool,
) -> bool:
    """Whether a human still has to look. Clears when the incident clears."""
    if unresolved_recovery:
        return True
    if completed is not None:
        failed = int(completed.get("orders_failed") or 0)
        status = str(completed.get("approval_status") or "")
        if failed > 0 or status not in {"approved", "not_required"}:
            return True
    if ack is not None and str(ack.get("status") or "") not in {
        "approved",
        "rejected",
        "expired",
    }:
        return True
    return False


def card_stage(progress: str, needs_attention: bool) -> str:
    """What the card shows. Attention wins the display, not the history."""
    return "attention" if needs_attention else progress
```

**역행 금지는 진행 축에만 적용한다.** sweep은 저장된 직전 진행값과 비교해 `PROGRESS_RANK`가 낮아지는 전이를 무시한다. 순서가 뒤바뀐 이벤트가 "완료"를 "진행 중"으로 되돌리면 안 된다.

**주의 플래그는 매번 다시 계산한다.** 복구가 끝나면 그대로 내려간다. 그것이 이 축을 분리한 이유다.

한 가지 남는 판단: `orders_failed > 0` 은 사고가 *있었다*는 사실이라 저절로 사라지지 않는다. 이 경우 주의 플래그는 운영자가 예외 카드(단계 4)에서 종결하기 전까지 유지된다. 단계 2 범위에서는 종결 수단이 없으므로 **그대로 유지되는 것이 맞다** — 조용히 내려가면 실패한 로테이션이 완료로 보인다.

**recovery 상관관계**: `live_order_recovery_required`에는 `approval_id`가 없다 — `order_id`뿐이다(운영 DB 확인). 승인과 잇는 경로는 `live_order_batch_lifecycle`의 `items[].request.approval_id` 또는 `live_order_submit_intent`의 request다. `unresolved_recovery`는 "이 승인의 order_id 중 recovery_required가 있고, 그 뒤에 해당 order의 terminal lifecycle이 없는 것"으로 판정한다. terminal이 왔으면 해제한다.

- [x] **Step 4b: Test every transition**

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
def test_the_stage_reads_payloads_not_event_types(ack, completed, recovery, expected):
    progress = approval_progress(ack, completed)
    assert card_stage(progress, approval_needs_attention(ack, completed, recovery)) == expected


def test_a_resolved_recovery_releases_the_attention_flag():
    """The contradiction the first draft shipped: attention could never clear.

    Ranking attention above done and forbidding backward transitions meant a
    recovered incident stayed on the card forever, so a real new incident
    became indistinguishable from an old resolved one.
    """
    ack = {"status": "approved"}
    completed = {"approval_status": "approved", "orders_failed": 0}

    assert approval_needs_attention(ack, completed, unresolved_recovery=True) is True
    assert approval_needs_attention(ack, completed, unresolved_recovery=False) is False


def test_progress_never_walks_back():
    """Out-of-order arrival must not undo how far the run actually got."""
    assert PROGRESS_RANK["done"] > PROGRESS_RANK["in_progress"] > PROGRESS_RANK["pending"]


def test_a_failed_order_keeps_attention_even_after_recovery_clears():
    """orders_failed is a fact about the past; it does not resolve itself.

    Stage 2 has no way to close it out -- that is the exception wizard in
    stage 4 -- and letting it lapse would render a half-executed rotation as
    complete.
    """
    assert (
        approval_needs_attention(
            {"status": "approved"},
            {"approval_status": "approved", "orders_failed": 1},
            unresolved_recovery=False,
        )
        is True
    )
```

- [x] **Step 4c: Wire the sweep**

`_sweep_lifecycle_cards` 는 활성 승인 봉투를 조회해 각 `approval:<approval_id>` 카드의 단계를 위 함수로 판정하고 `self._card_manager.refresh(...)` 를 호출한다. 승인 그룹이 2개 이상인 signal run에만 `daily:<signal_run_id>` 부모 카드를 추가로 갱신한다.

- [x] **Step 5: Keep the legacy path**

`_sweep_pending_approvals` 의 기존 전송을 **삭제하지 않는다.** 스펙 「단계 2의 안전망」에 따라 병행 유지한다. 이를 테스트로 고정한다:

```python
def test_the_legacy_notification_path_still_runs(tmp_path):
    """Removing it is stage 5, after card delivery is proven in production."""
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")

    router._sweep_pending_approvals()

    assert client.sent, "the legacy path must still deliver"
```

- [x] **Step 6: Run everything**

Run: `.venv/bin/python -m pytest tests/ -q && .venv/bin/python -m ruff check src tests --output-format=concise`

- [x] **Step 7: Commit**

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

- [x] **Step 1: Write the failing test**

```python
def test_the_no_action_notice_is_one_line_and_names_no_internals():
    assert "\n" not in NO_ACTION_NOTICE
    assert "run_id" not in NO_ACTION_NOTICE
    assert "signal" not in NO_ACTION_NOTICE.lower()
```

- [x] **Step 2: Run it**

Expected: FAIL — `ImportError: cannot import name 'NO_ACTION_NOTICE'`

- [x] **Step 3: Add the string**

```python
NO_ACTION_NOTICE = "오늘은 매매할 것이 없어요."
```

- [x] **Step 4: Use it where the daily run reports no action**

`cli.py` 의 `daily-signal-approval` 이 `status=no_action` 을 출력하는 분기에서 이 문구를 텔레그램으로 1회 전송한다. 카드가 아니라 한 줄 알림이므로 lifecycle을 거치지 않는다.

- [x] **Step 5: Run everything and commit**

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

- [x] **Step 1: Write the failing tests**

```python
def test_three_consecutive_failures_send_a_plain_text_fallback(tmp_path):
    """The operator must not lose the thread because the renderer is broken.

    The fallback deliberately does not go through cards.py -- if rendering is
    what is failing, rendering the fallback would fail too.
    """
    client = FakeClient(reject_for={100})
    store, manager = _manager(tmp_path, client, chat_ids=(100,))

    for _ in range(3):
        manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    client.reject_for = set()
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert any("appr_1" in text and "확인" in text for _, text in client.sent)


def test_two_failures_do_not_trigger_the_fallback(tmp_path):
    client = FakeClient(reject_for={100})
    _, manager = _manager(tmp_path, client, chat_ids=(100,))

    for _ in range(2):
        manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert manager.consecutive_failures("approval:appr_1", 100) == 2
```

- [x] **Step 2: Run to verify failure**

Expected: FAIL — `AttributeError: ... 'consecutive_failures'`

- [x] **Step 3: Implement**

실패 시 `telegram_ui_card_failure` 이벤트를 남기고, 성공 시 그 카운터를 리셋하는 이벤트를 남긴다. 3회 연속이면 `CARD_FALLBACK_TEMPLATE.format(card_key=..., stage=...)` 을 `cards.py` 를 거치지 않고 직접 전송한다. `telegram_ui` 헬스체크를 degraded로 전환한다.

- [x] **Step 4: Run everything, mutation-verify (임계값을 5로 바꾸면 첫 테스트가 FAIL), commit**

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
| 예외별 전달 판정 (거절만 재시도) | Task 2 Step 0·3 |
| 카드 현재 상태를 상한 없이 조회 | Task 3 (`telegram_ui_card_state` 투영 + `record_card_event`) |
| 복구 완료 시 주의 해제 | Task 5 Step 4 (진행/주의 두 축 분리) |

**남은 결정 — 실행자에게**

- Task 3의 `refresh` 는 Task 2의 chat 루프를 재사용해야 한다. `deliver` 와 `refresh` 가 각자 전송 코드를 갖게 되면 intent 기록 순서가 두 곳에서 갈라진다.
- Task 5에서 기존 테스트가 깨지면, **기존 알림 경로를 기대하는 단정은 유지한다.** 그 경로 제거는 단계 5다.
- `ui/` 는 handlers·orchestrator·execution을 임포트하지 않는다. Task 5에서 단계 판정 로직이 필요해지면 그 판정은 `handlers.py` 쪽에 두고 `ui/` 에는 렌더 데이터만 넘긴다.

**범위 밖 (확인용)**

월간 자금 카드(3b), 예외 마법사(4), 조회 카드·구 경로 제거(5), 그리고 intent만 있고 result 없는 카드의 **자가 치유**(3a-3). 단계 2는 그 상태를 *만들고 관측 가능하게* 할 뿐 고치지 않는다.


---

## 구현에서 달라진 점 (2026-08-14)

계획을 그대로 따르지 않은 곳과 그 이유. 다음 단계가 이 결정 위에 쌓인다.

**승인 카드의 최초 전송은 sweep이 아니라 dispatch가 한다.** Task 5의 테스트는
sweep이 첫 전송까지 하는 모양이었지만, orchestrator의
`_dispatch_signal_approval_locked`가 이미 카드를 보내고 있었으므로 그대로 두면 한
승인에 버튼 달린 카드가 두 장 생긴다. dispatch를 `CardLifecycleManager.deliver`
경유로 바꿔 카드가 태어날 때부터 `message_id`를 갖게 하고, sweep은 갱신만 한다.

**`render_approval_stage_card`를 새로 만들었다.** `render_approval_card`는 제안
본문과 승인/거절 버튼만 그린다. pending에서는 기존 카드와 바이트 단위로 동일하고,
그 뒤로는 본문을 유지한 채 단계 헤더를 얹고 버튼을 뗀다.

**`PendingApprovalEnvelope.card_delivery_version`이 이관 경계다.** 0은 카드 이관
이전에 dispatch된 승인(sweep이 손대지 않는다), 1은 전송 **전에** intent가 기록됨을
보장하므로 투영이 비어 있으면 전송이 시작되지도 못했다는 뜻이고 sweep이 재전송한다.
이것이 없으면 pending 저장 직후 죽은 승인이 영원히 운영자에게 닿지 않는다.

**`telegram_ui_card_state`에는 마이그레이션이 필요하다.** 이 브랜치의 앞선 커밋이
`consecutive_failures` 없이 테이블을 만들었고, 운영 DB에도 그 상태로 존재했다.
`CREATE TABLE IF NOT EXISTS`는 컬럼을 더하지 않으므로 `PRAGMA table_info` +
`ALTER TABLE`을 추가했다 (`store.py`, 다른 테이블과 같은 규약).

**노옵 알림은 at-most-once다.** 전송 전에 duplicate_key로 자리를 잡으므로 중복은
없지만, 전송이 실패한 채팅은 그날 알림을 잃는다. 버튼 없는 한 줄 알림이라 중복보다
누락이 싸다는 판단이다. 남는 이벤트는 전송 완료가 아니라 `status: "claimed"`다.

**카드의 수신자는 최초 전송 **전에** 기록된다.** `refresh`가 매번 현재 설정을
읽으면, 허용 채팅을 하나 추가한 순간 과거의 모든 카드가 "복사본이 없다"로 보여
신규 전송된다 — 없는 복사본과 처음 보내는 카드는 구분되지 않는다. 그렇다고
복사본이 있는 채팅으로 되짚으면 이번엔 **전송 도중의 중단**을 못 읽는다: chat 100까지
기록하고 죽으면 chat 200은 "나중에 추가된 채팅"과 똑같이 보여 영영 전송되지 않는다.
그래서 `telegram_ui_card_audience`에 첫 API 호출 전에 남긴다 — `deliver`뿐 아니라
`record_render_failure`도 그렇다. 렌더가 처음부터 실패하는 카드는 refresh에 닿지
못하므로 그 경로가 수신자를 남기는 유일한 곳이다. 복사본으로 되짚는 경로는 이
기록이 생기기 전에 만들어진 카드용 fallback으로만 남아 있고, 그마저도 한 번 보면
그때의 답으로 고정된다. 읽을 때는 항상 현재 설정과 교집합을
취한다 — 뺀 채팅으로는 다시 성공할 수 없으므로 갱신은 실패만 쌓는다. 종결
판정(`_card_is_settled`)과 렌더 실패 기록도 같은 기준을 쓴다.

**종결된 signal run은 SQL에서 뺀다.** `_card_is_settled`는 렌더와 전송만 막았을 뿐
전체 스캔과 승인당 투영 조회는 그대로였고, 그 비용은 운영 기간에 비례해 callback
polling 지연으로 쌓인다. `telegram_ui_settled_run`이 terminal 인덱스다. 승인이
아니라 run 단위인 이유는 데일리 부모 카드가 그룹의 일부만 보면 안 되기 때문이다.
되살아나는 경로는 둘: 뒤늦게 붙은 승인은 기록된 `max_event_id`보다 큰 id로 스스로
빠져나오고, 복구·처리 실패는 `reopen_settled_signal_runs`가 표시를 지운다.
되살아날 때는 그 run의 봉투를 **전부** 다시 돌려준다 — 새 이벤트만 주면 그룹이
하나로 보여 부모 카드 조건(2개 이상)에 닿지 못한다. 되살릴 실패는 완료가 뒤따르지
않은 것뿐이다: 재개가 성공한 run까지 매 poll 표시를 지웠다 다시 쓰면 종결 인덱스가
없애려던 조회·쓰기가 그 run에는 영구히 남는다. 거절·만료는 완료 이벤트 없이도
done이므로 이 경로는 죽은 코드가 아니다 — 종결 뒤에 붙은 집행 실패가 실제로 있다.
표시는 카드가 done에 닿은 **다음** sweep에 남는다(그 pass의 판정은 갱신 전 상태로
내려지므로).

이력 조회도 같이 좁혔다. 인덱스만 두고 ack·completion·resolution failure를 전체
읽어 접으면 답은 같아도 비용은 그대로다. 그래서 (1) unsettled가 하나도 없으면
`list_unsettled_pending_approvals()` 직후 돌아가고, (2) 그 뒤의 조회는 열려 있는
approval_id로 SQL에서 한정하며, (3) 되살리기의 실패 판정은 아예 SQL 안에 있다 —
Python으로 하려면 전체 실패·완료를 역직렬화해야 하는데 그것이 없애려던 비용이다.
남는 것은 복구 미리보기(`WorkflowRecoveryService.preview()`)의 조회인데, 이것은
`_sweep_recovery_notifications`가 매 poll 이미 치르는 비용이고 blocker 판정을
SQL로 옮기면 그 로직이 두 벌이 된다. 되살릴 표시가 하나도 없으면 그마저 부르지
않는다.

**`system_events.approval_id` / `signal_run_id`는 생성 컬럼이다.** 스코프를 좁혀도
`json_extract`로 비교하면 SQLite는 그 event type의 모든 행을 훑는다 — 인덱스를
탈 수 없기 때문이다. 두 값을 `GENERATED ALWAYS AS (json_extract(...)) VIRTUAL`로
투영하고 `(event_type, 컬럼)` 인덱스를 걸었다. INSERT 경로마다 채우지 않은 이유가
중요하다: `system_events`에 넣는 자리는 일곱 군데인데 기존 `order_id` 투영을 채우는
곳은 셋뿐이고, 카드 sweep의 조회가 한 군데를 빠뜨리면 그 승인은 **느려지는 것이
아니라 조용히 사라진다**. 생성 컬럼은 payload와 어긋날 수 없다.
`PRAGMA table_info`는 VIRTUAL 컬럼을 보여주지 않으므로 마이그레이션 판단은
`table_xinfo`로 한다(아니면 두 번째로 열 때 "duplicate column name"으로 죽는다).

### 측정 (2026-08-14)

운영 기록은 18일에 승인 10건 — 연 200건 규모다. 열린 승인 1건 + 나머지 전부 종결
상태에서 `_sweep_lifecycle_cards()` 1회:

| 누적 승인 | 연수 | 인덱스 전 | 인덱스 후 |
|---|---|---|---|
| 10 (현재) | — | 4.1 ms | 4.8 ms |
| 200 | ~1년 | 5.1 ms | 5.4 ms |
| 2,000 | ~10년 | 16.0 ms | 10.2 ms |
| 10,000 | ~50년 | 57.6 ms | 34.5 ms |

승인별 조회(ack·completion·failure)는 상수 시간이 됐다 — 10,000건에서 29.2 ms →
0.5 ms. **남은 선형 항은 `list_unsettled_pending_approvals` 하나다.** 종결 여부를
알려면 pending 이벤트를 한 번씩은 봐야 하므로, 진짜 O(열린 승인)으로 만들려면
open-card 전용 projection이나 watermark 같은 **파생 상태를 하나 더** 두어야 한다.
지금은 두지 않기로 했다: poll 간격이 1초인데 10년 시점 비용이 10 ms(1%)이고, 이
단계에서 나온 결함 대부분이 파생 상태끼리 어긋나는 종류였다. 재검토 기준은
"poll당 50 ms" — 위 표대로면 누적 승인 15,000건, 대략 70년 뒤다.

**telegram_ui health는 현재 허용 채팅만 본다.** 설정에서 뺀 채팅의 복사본은 다시
성공할 기회가 없어 카운터가 0으로 돌아올 길이 없고, 그러면 자가 치유를 전제로 한
이 체크가 영구 warn이 된다. 다만 목록이 비어 있으면 좁히지 않고 전부 본다 —
목록은 설정이 비면 `MAESTRO_TELEGRAM_ALLOWED_CHAT_IDS`에서 오므로, 빈 목록은
"채팅이 없다"가 아니라 "환경을 읽지 않았다"일 수 있기 때문이다.

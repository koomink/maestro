# Telegram UX 개편 — 단계 1 (ui/ 모듈 + 승인 카드 + 자세히 토글 + 메뉴 5개) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Telegram 승인 메시지를 한글 카드로 교체하고, 자세히/접기 토글과 5개 한글 명령어 메뉴를 도입한다 (스펙 단계 1).

**Architecture:** `src/maestro/integrations/telegram/ui/` 신설 (catalog=문구, cards=순수 렌더러, format=한국어 포맷 유틸). handlers/orchestrator/bot은 텍스트 생성만 ui/로 위임하고 비즈니스 로직은 불변. `ui/`는 telegram 패키지 밖(handlers·orchestrator·execution)을 임포트하지 않는다 — 의존 방향은 `handlers → ui` 단방향.

**Tech Stack:** Python 3.11+, pydantic (기존 모델), pytest, zoneinfo. 신규 의존성 없음.

**Spec:** `docs/superpowers/specs/2026-08-09-telegram-ux-redesign-design.md` (13차 개정). 이 계획은 마이그레이션 표의 **단계 1만** 다룬다. 단계 2~5는 각각 별도 계획으로 작성한다.

## Global Constraints

- 첫 줄 = 결론: 카드 첫 줄만 읽어도 "무슨 일 + 내가 뭘 해야 하는지" 파악.
- 전부 한글. 기술 식별자(ID, 종목코드, ISO 시각)는 펼친 뷰에만. 티커·전략명 고유명사는 예외.
- 금액: 원화 `124만원`, `71.2만원`, 1만 미만 `8,500원`; 달러 `$1,240.50`. 펼친 뷰는 전체 자릿수.
- 시간: KST(Asia/Seoul) 기준 `밤 11시 30분`, `오전 9시 10분`. ISO는 펼친 뷰에만.
- 이모지 두 용도만: (a) 첫 줄 카드 유형 아이콘 1개(📩 등), (b) 상태 신호등(✅🔵⏳⚠️🛑🟢🔴).
- 에러도 일상 언어로. 예외 원문은 첫 화면에 노출하지 않는다.
- callback data 64바이트 이하. 기존 `operator:` prefix 체계 유지, UI 토글은 `operator:ui:` 하위.
- 기존 20개 명령어는 **동작 유지** (메뉴에서만 숨김). 기존 테스트가 깨지면 로직을 건드렸다는 경고 신호.
- `ui/cards.py`·`ui/format.py`·`ui/catalog.py`는 순수 함수/상수만: 네트워크·DB·전역 상태 금지.
- 커밋 메시지 끝에 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` 추가.
- 작업 브랜치: `feature/telegram-ux-redesign` (Maestro 저장소, cwd `/root/projects/Symphony/Maestro`).

---

### Task 1: `ui/format.py` — 한국어 포맷 유틸

**Files:**
- Create: `src/maestro/integrations/telegram/ui/__init__.py` (빈 파일)
- Create: `src/maestro/integrations/telegram/ui/format.py`
- Test: `tests/test_telegram_ui_format.py`

**Interfaces:**
- Consumes: 없음 (표준 라이브러리만).
- Produces:
  - `money_kr(value: float, currency: str | None) -> str` — 접힌 뷰 금액.
  - `money_full(value: float, currency: str | None) -> str` — 펼친 뷰 전체 자릿수.
  - `quantity_kr(value: float) -> str` — `10주`, `0.5주`.
  - `deadline_kr(dt: datetime, tz: ZoneInfo | None = None) -> str` — `밤 11시 30분` 형태 (기본 tz=Asia/Seoul).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_telegram_ui_format.py`:

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from maestro.integrations.telegram.ui.format import (
    deadline_kr,
    money_full,
    money_kr,
    quantity_kr,
)


def test_money_kr_krw_over_10k_uses_manwon():
    assert money_kr(1_240_000, "KRW") == "124만원"
    assert money_kr(712_000, "KRW") == "71.2만원"


def test_money_kr_krw_under_10k_uses_won():
    assert money_kr(8_500, "KRW") == "8,500원"


def test_money_kr_usd():
    assert money_kr(1_240.5, "USD") == "$1,240.50"


def test_money_kr_unknown_currency_falls_back():
    assert money_kr(100.0, "JPY") == "100.00 JPY"
    assert money_kr(100.0, None) == "100.00"


def test_money_full_keeps_all_digits():
    assert money_full(1_240_000, "KRW") == "1,240,000원"
    assert money_full(1_240.5, "USD") == "$1,240.50"


def test_quantity_kr():
    assert quantity_kr(10) == "10주"
    assert quantity_kr(28.0) == "28주"
    assert quantity_kr(0.5) == "0.5주"


def test_deadline_kr_buckets():
    # 23:30 KST = 14:30 UTC
    dt = datetime(2026, 8, 10, 14, 30, tzinfo=timezone.utc)
    assert deadline_kr(dt) == "밤 11시 30분"
    # 09:10 KST
    dt2 = datetime(2026, 8, 10, 0, 10, tzinfo=timezone.utc)
    assert deadline_kr(dt2) == "오전 9시 10분"
    # 분이 0이면 생략, 15:00 KST → 오후 3시
    dt3 = datetime(2026, 8, 10, 6, 0, tzinfo=timezone.utc)
    assert deadline_kr(dt3) == "오후 3시"


def test_deadline_kr_respects_explicit_tz():
    dt = datetime(2026, 8, 10, 14, 30, tzinfo=timezone.utc)
    assert deadline_kr(dt, tz=ZoneInfo("UTC")) == "오후 2시 30분"
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_telegram_ui_format.py -v`
Expected: FAIL — `ModuleNotFoundError: maestro.integrations.telegram.ui`

- [ ] **Step 3: 구현**

`src/maestro/integrations/telegram/ui/__init__.py`는 빈 파일로 생성. `src/maestro/integrations/telegram/ui/format.py`:

```python
"""한국어 표기 유틸. 순수 함수만 — 네트워크·DB·설정 접근 금지."""

from datetime import datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

# (시작시, 끝시) → 접두어. 12시간제 변환 전 24시간 기준.
_HOUR_LABELS = (
    (0, 5, "새벽"),
    (6, 11, "오전"),
    (12, 17, "오후"),
    (18, 20, "저녁"),
    (21, 23, "밤"),
)


def money_kr(value: float, currency: str | None) -> str:
    if currency == "KRW":
        if abs(value) >= 10_000:
            man = round(value / 10_000, 1)
            label = f"{man:,.1f}".rstrip("0").rstrip(".")
            return f"{label}만원"
        return f"{value:,.0f}원"
    if currency == "USD":
        return f"${value:,.2f}"
    suffix = f" {currency}" if currency else ""
    return f"{value:,.2f}{suffix}"


def money_full(value: float, currency: str | None) -> str:
    if currency == "KRW":
        return f"{value:,.0f}원"
    if currency == "USD":
        return f"${value:,.2f}"
    suffix = f" {currency}" if currency else ""
    return f"{value:,.2f}{suffix}"


def quantity_kr(value: float) -> str:
    label = f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{label}주"


def deadline_kr(dt: datetime, tz: ZoneInfo | None = None) -> str:
    local = dt.astimezone(tz or _KST)
    prefix = next(
        label for start, end, label in _HOUR_LABELS if start <= local.hour <= end
    )
    hour12 = local.hour % 12
    if hour12 == 0:
        hour12 = 12
    if local.minute:
        return f"{prefix} {hour12}시 {local.minute}분"
    return f"{prefix} {hour12}시"
```

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_telegram_ui_format.py -v`
Expected: PASS (8건 전부)

- [ ] **Step 5: Commit**

```bash
git add src/maestro/integrations/telegram/ui tests/test_telegram_ui_format.py
git commit -m "feat: add Korean format utilities for Telegram UI layer

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `ui/catalog.py` + `ui/cards.py` — 승인 카드 렌더러

**Files:**
- Create: `src/maestro/integrations/telegram/ui/catalog.py`
- Create: `src/maestro/integrations/telegram/ui/cards.py`
- Test: `tests/test_telegram_ui_cards.py`

**Interfaces:**
- Consumes: Task 1의 `money_kr`, `money_full`, `quantity_kr`, `deadline_kr`; `maestro.approval.models.ApprovalRequest`; `maestro.core.strategy_names.strategy_display_label` (둘 다 telegram 밖이지만 순수 모델/유틸이라 허용 — handlers/orchestrator/execution 임포트만 금지).
- Produces:
  - `RenderedCard` dataclass: `text: str`, `reply_markup: dict[str, Any]`.
  - `render_approval_card(request: ApprovalRequest, *, expanded: bool) -> RenderedCard`
  - `approval_decision_text(status: str, approval_id: str, orders_created: int) -> str` (status는 `"approved"` 또는 `"rejected"`)
  - `approval_reminder_text(minutes: int, card_text: str) -> str`
  - catalog 상수: `APPROVAL_TITLE`, `STALE_CALLBACK_TEXT`, `CALLBACK_FAILED_TEXT`, `ANSWER_APPROVED`, `ANSWER_REJECTED`
  - callback data 규약: 승인 `operator:appr:a:<approval_id>`, 거절 `operator:appr:r:<approval_id>` (기존 유지), 펼치기 `operator:ui:d:<approval_id>`, 접기 `operator:ui:f:<approval_id>` — approval_id는 `appr_<uuid4hex>`(37자)이므로 최장 `operator:appr:a:` 16+37=53바이트로 64바이트 제한 준수.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_telegram_ui_cards.py`:

```python
from datetime import datetime, timedelta, timezone

from maestro.approval.models import ApprovalRequest
from maestro.integrations.telegram.ui import catalog
from maestro.integrations.telegram.ui.cards import (
    approval_decision_text,
    approval_reminder_text,
    render_approval_card,
)


def _request() -> ApprovalRequest:
    expires = datetime(2026, 8, 10, 14, 30, tzinfo=timezone.utc)  # KST 밤 11시 30분
    return ApprovalRequest(
        approval_id="appr_0123456789abcdef0123456789abcdef",
        run_id="run_0123456789abcdef0123456789abcdef",
        created_at=expires - timedelta(hours=6),
        expires_at=expires,
        channel="telegram",
        source_strategy_ids=["tranquillo"],
        order_count=2,
        estimated_notional=1_240_000,
        proposed_orders=[
            {
                "symbol": "069500",
                "name": "KODEX 200",
                "side": "buy",
                "quantity": 10,
                "limit_price": 71_200,
                "notional": 712_000,
                "currency": "KRW",
                "exchange_code": "KRX",
                "broker_product": "kis_domestic_stock",
            },
            {
                "symbol": "360750",
                "name": "TIGER 미국S&P500",
                "side": "buy",
                "quantity": 28,
                "limit_price": 18_857,
                "notional": 528_000,
                "currency": "KRW",
                "exchange_code": "KRX",
                "broker_product": "kis_domestic_stock",
            },
        ],
        risk_violations=[],
    )


def test_collapsed_card_snapshot():
    card = render_approval_card(_request(), expanded=False)
    assert card.text == (
        "📩 투자 주문을 진행할까요?\n"
        "\n"
        "Tranquillo 전략 · 국내 주식 2종목 · 총 124만원\n"
        "\n"
        "• KODEX 200 10주 — 71.2만원\n"
        "• TIGER 미국S&P500 28주 — 52.8만원\n"
        "\n"
        "⏰ 밤 11시 30분까지 응답해 주세요."
    )


def test_collapsed_card_buttons():
    card = render_approval_card(_request(), expanded=False)
    rows = card.reply_markup["inline_keyboard"]
    approval_id = "appr_0123456789abcdef0123456789abcdef"
    assert rows[0][0] == {
        "text": "✅ 승인",
        "callback_data": f"operator:appr:a:{approval_id}",
    }
    assert rows[0][1] == {
        "text": "❌ 거절",
        "callback_data": f"operator:appr:r:{approval_id}",
    }
    assert rows[1][0] == {
        "text": "🔍 자세히",
        "callback_data": f"operator:ui:d:{approval_id}",
    }
    for row in rows:
        for button in row:
            assert len(button["callback_data"].encode()) <= 64


def test_expanded_card_has_identifiers_and_fold_button():
    card = render_approval_card(_request(), expanded=True)
    assert "appr_0123456789abcdef0123456789abcdef" in card.text
    assert "run_0123456789abcdef0123456789abcdef" in card.text
    assert "2026-08-10T14:30:00+00:00" in card.text
    assert "069500" in card.text
    assert "71,200원" in card.text  # 지정가 전체 자릿수
    rows = card.reply_markup["inline_keyboard"]
    assert rows[1][0]["text"] == "접기"
    assert rows[1][0]["callback_data"].startswith("operator:ui:f:")


def test_collapsed_card_hides_identifiers():
    card = render_approval_card(_request(), expanded=False)
    assert "appr_" not in card.text
    assert "run_" not in card.text


def test_risk_violations_summarized_in_collapsed_view():
    request = _request().model_copy(update={"risk_violations": ["max_notional exceeded"]})
    collapsed = render_approval_card(request, expanded=False)
    assert "⚠️ 위험 점검에서 확인할 내용이 1건 있어요." in collapsed.text
    assert "max_notional" not in collapsed.text
    expanded = render_approval_card(request, expanded=True)
    assert "max_notional exceeded" in expanded.text


def test_many_orders_are_truncated_in_collapsed_view():
    base = _request()
    order = dict(base.proposed_orders[0])
    request = base.model_copy(
        update={"proposed_orders": [dict(order) for _ in range(9)], "order_count": 9}
    )
    card = render_approval_card(request, expanded=False)
    assert card.text.count("•") == 7  # 6줄 + "외 3건" 줄
    assert "• 외 3건" in card.text


def test_decision_and_reminder_texts():
    assert approval_decision_text("approved", "appr_x", 2) == (
        "✅ 승인 완료 — 주문 2건을 접수했어요."
    )
    assert approval_decision_text("rejected", "appr_x", 0) == (
        "❌ 거절했어요 — 이번 제안은 실행되지 않아요."
    )
    reminder = approval_reminder_text(30, "카드 본문")
    assert reminder == "⏰ 아직 응답을 기다리고 있어요 (30분 경과)\n\n카드 본문"
    assert catalog.STALE_CALLBACK_TEXT == "이미 처리됐거나 만료된 요청이에요."
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_telegram_ui_cards.py -v`
Expected: FAIL — `ImportError` (catalog/cards 미존재)

- [ ] **Step 3: 구현 — catalog.py**

`src/maestro/integrations/telegram/ui/catalog.py`:

```python
"""모든 한글 문구·템플릿. 순수 데이터만 — 로직 금지.

문구 수정은 이 파일만 수정하면 끝난다.
"""

APPROVAL_TITLE = "📩 투자 주문을 진행할까요?"
APPROVAL_DEADLINE = "⏰ {deadline}까지 응답해 주세요."
APPROVAL_SUMMARY = "{strategy} 전략 · {market} {count}종목 · 총 {total}"
APPROVAL_RISK_SUMMARY = "⚠️ 위험 점검에서 확인할 내용이 {count}건 있어요."
APPROVAL_MORE_ORDERS = "• 외 {count}건"

DECISION_APPROVED = "✅ 승인 완료 — 주문 {count}건을 접수했어요."
DECISION_REJECTED = "❌ 거절했어요 — 이번 제안은 실행되지 않아요."
REMINDER = "⏰ 아직 응답을 기다리고 있어요 ({minutes}분 경과)"

STALE_CALLBACK_TEXT = "이미 처리됐거나 만료된 요청이에요."
CALLBACK_FAILED_TEXT = "처리하지 못했어요. 잠시 후 다시 시도해 주세요."
ANSWER_APPROVED = "승인했어요."
ANSWER_REJECTED = "거절했어요."

BUTTON_APPROVE = "✅ 승인"
BUTTON_REJECT = "❌ 거절"
BUTTON_DETAIL = "🔍 자세히"
BUTTON_FOLD = "접기"

MARKET_DOMESTIC = "국내 주식"
MARKET_OVERSEAS = "해외 주식"
MARKET_MIXED = "국내·해외 주식"

EXPANDED_HEADER = "자세한 내용"
```

- [ ] **Step 4: 구현 — cards.py**

`src/maestro/integrations/telegram/ui/cards.py`:

```python
"""카드 렌더러: 상태 데이터 → (text, reply_markup). 순수 함수만."""

from dataclasses import dataclass
from typing import Any

from maestro.approval.models import ApprovalRequest
from maestro.core.strategy_names import strategy_display_label
from maestro.integrations.telegram.ui import catalog
from maestro.integrations.telegram.ui.format import (
    deadline_kr,
    money_full,
    money_kr,
    quantity_kr,
)

_MAX_COLLAPSED_ORDER_LINES = 6
_CALLBACK_PREFIX = "operator:"


@dataclass(frozen=True)
class RenderedCard:
    text: str
    reply_markup: dict[str, Any]


def render_approval_card(request: ApprovalRequest, *, expanded: bool) -> RenderedCard:
    lines = [catalog.APPROVAL_TITLE, ""]
    lines.append(
        catalog.APPROVAL_SUMMARY.format(
            strategy=strategy_display_label(request.source_strategy_ids),
            market=_market_summary(request.proposed_orders),
            count=len(request.proposed_orders),
            total=_total_label(request),
        )
    )
    lines.append("")
    lines.extend(_order_lines(request.proposed_orders, expanded=expanded))
    if request.risk_violations:
        lines.append("")
        if expanded:
            lines.append("⚠️ 위험 점검 원문")
            lines.extend(f"- {item}" for item in request.risk_violations)
        else:
            lines.append(
                catalog.APPROVAL_RISK_SUMMARY.format(count=len(request.risk_violations))
            )
    lines.append("")
    lines.append(
        catalog.APPROVAL_DEADLINE.format(deadline=deadline_kr(request.expires_at))
    )
    if expanded:
        lines.append("")
        lines.append(catalog.EXPANDED_HEADER)
        lines.append(f"- 승인 ID: {request.approval_id}")
        lines.append(f"- 실행 ID: {request.run_id}")
        lines.append(f"- 마감(ISO): {request.expires_at.isoformat()}")
    return RenderedCard(
        text="\n".join(lines),
        reply_markup=approval_markup(request.approval_id, expanded=expanded),
    )


def approval_markup(approval_id: str, *, expanded: bool) -> dict[str, Any]:
    toggle = (
        {
            "text": catalog.BUTTON_FOLD,
            "callback_data": f"{_CALLBACK_PREFIX}ui:f:{approval_id}",
        }
        if expanded
        else {
            "text": catalog.BUTTON_DETAIL,
            "callback_data": f"{_CALLBACK_PREFIX}ui:d:{approval_id}",
        }
    )
    return {
        "inline_keyboard": [
            [
                {
                    "text": catalog.BUTTON_APPROVE,
                    "callback_data": f"{_CALLBACK_PREFIX}appr:a:{approval_id}",
                },
                {
                    "text": catalog.BUTTON_REJECT,
                    "callback_data": f"{_CALLBACK_PREFIX}appr:r:{approval_id}",
                },
            ],
            [toggle],
        ]
    }


def approval_decision_text(status: str, approval_id: str, orders_created: int) -> str:
    if status == "approved":
        return catalog.DECISION_APPROVED.format(count=orders_created)
    return catalog.DECISION_REJECTED


def approval_reminder_text(minutes: int, card_text: str) -> str:
    return f"{catalog.REMINDER.format(minutes=minutes)}\n\n{card_text}"


def _order_lines(orders: list[dict], *, expanded: bool) -> list[str]:
    lines: list[str] = []
    visible = orders if expanded else orders[:_MAX_COLLAPSED_ORDER_LINES]
    for order in visible:
        name = str(order.get("name") or order.get("symbol") or "unknown")
        quantity = _float_or_none(order.get("quantity"))
        notional = _float_or_none(order.get("notional"))
        currency = order.get("currency") if isinstance(order.get("currency"), str) else None
        quantity_label = f" {quantity_kr(quantity)}" if quantity is not None else ""
        amount = money_kr(notional, currency) if notional is not None else "-"
        lines.append(f"• {name}{quantity_label} — {amount}")
        if expanded:
            symbol = str(order.get("symbol") or "unknown")
            broker_symbol = order.get("broker_symbol")
            code = (
                f"{symbol} (브로커: {broker_symbol})"
                if isinstance(broker_symbol, str) and broker_symbol and broker_symbol != symbol
                else symbol
            )
            lines.append(f"  코드: {code}")
            price = _float_or_none(order.get("limit_price", order.get("price")))
            if price is not None:
                lines.append(f"  지정가: {money_full(price, currency)}")
            if notional is not None:
                lines.append(f"  금액: {money_full(notional, currency)}")
    hidden = len(orders) - len(visible)
    if hidden > 0:
        lines.append(catalog.APPROVAL_MORE_ORDERS.format(count=hidden))
    return lines


def _market_summary(orders: list[dict]) -> str:
    domestic = 0
    overseas = 0
    for order in orders:
        if (
            order.get("broker_product") == "kis_domestic_stock"
            or order.get("exchange_code") == "KRX"
        ):
            domestic += 1
        else:
            overseas += 1
    if domestic and overseas:
        return catalog.MARKET_MIXED
    if overseas:
        return catalog.MARKET_OVERSEAS
    return catalog.MARKET_DOMESTIC


def _total_label(request: ApprovalRequest) -> str:
    totals: dict[str | None, float] = {}
    for order in request.proposed_orders:
        notional = _float_or_none(order.get("notional"))
        if notional is None:
            continue
        currency = order.get("currency") if isinstance(order.get("currency"), str) else None
        totals[currency] = totals.get(currency, 0.0) + notional
    if not totals:
        return money_kr(request.estimated_notional, None)
    return ", ".join(
        money_kr(value, currency) for currency, value in sorted(
            totals.items(), key=lambda item: str(item[0])
        )
    )


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
```

- [ ] **Step 5: 통과 확인**

Run: `python -m pytest tests/test_telegram_ui_cards.py tests/test_telegram_ui_format.py -v`
Expected: PASS 전부. 스냅샷 문자열이 다르면 **테스트가 아니라 구현/카탈로그를 스냅샷에 맞춘다** (스냅샷이 스펙 예시 기반의 정답).

- [ ] **Step 6: Commit**

```bash
git add src/maestro/integrations/telegram/ui tests/test_telegram_ui_cards.py
git commit -m "feat: add Korean approval card renderer and message catalog

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: 승인 dispatch를 카드 렌더러로 교체

**Files:**
- Modify: `src/maestro/orchestration/orchestrator.py:994-1017` (`_dispatch_signal_approval_locked` 내부: `message = format_approval_request(request)` 와 `markup = _async_approval_markup(request.approval_id)` 호출부)
- Modify: `src/maestro/integrations/telegram/bot.py:178,216` (`TelegramApprovalNotifier.send_approval_request`, `TelegramApprovalService.request_decision`)
- Test: `tests/test_telegram_ui_cards.py` (렌더는 Task 2에서 검증됨) + 기존 `tests/test_approval_flow.py`, `tests/test_telegram_operator_ui.py` 회귀 확인

**Interfaces:**
- Consumes: Task 2의 `render_approval_card(request, expanded=False)`.
- Produces: `PendingApprovalEnvelope.message`에 접힌 카드 텍스트가 저장됨 (Task 4·5가 이 텍스트를 재사용). `formatter.format_approval_request`는 **삭제하지 않는다** (단계 5에서 제거).

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_telegram_operator_ui.py` 끝에 추가 (기존 헬퍼 `_telegram_config_path`, `FakeTelegramClient` 재사용 불필요 — orchestrator 단위가 아니라 함수 치환이므로 dispatch 산출물만 검증):

```python
def test_dispatch_uses_korean_approval_card(tmp_path, monkeypatch):
    # orchestrator가 만든 envelope.message가 한글 카드인지 검증한다.
    # 기존 test_approval_flow.py의 dispatch 경로를 재사용하기 어렵다면
    # 최소 계약으로: 렌더 함수가 dispatch 모듈에서 참조되는지 확인.
    from maestro.orchestration import orchestrator as orch

    assert orch.render_approval_card is not None
```

주의: 위 스모크 검증에 더해, 기존 dispatch 통합 테스트가 영어 문구를 단언하고 있으면 실패한다. 다음을 먼저 실행해 영향 범위를 확인하라:

Run: `rg -n "Maestro Approval|Tap Approve" tests/`

영어 문구를 단언하는 테스트는 **한글 카드 문구 단언으로 갱신**한다 (예: `"🔔 Maestro Approval" in text` → `text.startswith("📩 투자 주문을 진행할까요?")`). 비즈니스 로직 단언(승인 생성 수, 이벤트 기록)은 건드리지 않는다.

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_telegram_operator_ui.py::test_dispatch_uses_korean_approval_card -v`
Expected: FAIL — `AttributeError: render_approval_card`

- [ ] **Step 3: orchestrator.py 수정**

상단 import에 추가 (기존 `from maestro.integrations.telegram.formatter import format_approval_request` 근처):

```python
from maestro.integrations.telegram.ui.cards import render_approval_card
```

`_dispatch_signal_approval_locked` 내부 두 곳 수정 — 994행 부근:

```python
            # 변경 전: message = format_approval_request(request)
            card = render_approval_card(request, expanded=False)
            message = card.text
```

1016행 부근:

```python
            # 변경 전: markup = _async_approval_markup(request.approval_id)
            markup = card.reply_markup
```

`format_approval_request` import와 `_async_approval_markup` 함수(orchestrator.py:4692)는 다른 경로가 참조하는지 확인 후 미사용이면 orchestrator에서만 제거한다:

Run: `rg -n "_async_approval_markup|format_approval_request" src/maestro/orchestration/orchestrator.py`

- [ ] **Step 4: bot.py 수정**

`TelegramApprovalNotifier.send_approval_request` (178행)와 `TelegramApprovalService.request_decision` (216행)의 `format_approval_request(request)`를 다음으로 교체:

```python
        message = render_approval_card(request, expanded=False).text
```

import는 파일 상단에 `from maestro.integrations.telegram.ui.cards import render_approval_card` 추가. **주의**: sync 경로(`TelegramApprovalService`)는 자체 `_approval_reply_markup`을 유지한다 — 이 경로에는 `ui:d` callback을 처리할 라우터가 없으므로 자세히 버튼을 붙이지 않는다.

- [ ] **Step 5: 전체 회귀 확인**

Run: `python -m pytest tests/test_approval_flow.py tests/test_telegram_operator_ui.py tests/test_tranquillo_live_approval_workflow.py -v`
Expected: PASS. 영어 문구 단언 실패가 나오면 Step 1의 주의에 따라 해당 단언만 한글로 갱신 후 재실행.

- [ ] **Step 6: Commit**

```bash
git add src/maestro/orchestration/orchestrator.py src/maestro/integrations/telegram/bot.py tests/
git commit -m "feat: dispatch Korean approval cards from orchestrator and sync approval path

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `ui:d` / `ui:f` 자세히·접기 callback

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py:338-465` (`_process_callback`의 prefix 분기: `if action.startswith("appr:")` 앞에 `ui:` 분기 추가) 및 라우터에 `_process_ui_toggle` 메서드 추가
- Test: `tests/test_telegram_operator_ui.py`

**Interfaces:**
- Consumes: Task 2의 `render_approval_card`, `catalog.STALE_CALLBACK_TEXT`; 기존 `self._pending_async_approval(approval_id)` (handlers.py:3919, ack 존재·만료 시 None), 기존 `self._answer(callback, text)`, `self.client.edit_message_text`.
- Produces: callback data `operator:ui:d:<approval_id>` → 해당 메시지를 펼친 뷰로 edit, `operator:ui:f:<approval_id>` → 접힌 뷰로 edit. envelope가 없으면 `catalog.STALE_CALLBACK_TEXT`로 answer만 한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_telegram_operator_ui.py`에 추가 (기존 `_pending_approval_envelope`, `callback_update`, `FakeTelegramClient`, `_telegram_config_path` 헬퍼 재사용):

```python
def test_ui_detail_callback_expands_approval_card(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config, store=store, audit=audit, client=client
    )
    envelope = _pending_approval_envelope()
    store.save_system_event(
        new_run_id(), "telegram_approval_pending", envelope.model_dump(mode="json")
    )

    handled = router.process_update(
        callback_update(f"operator:ui:d:{envelope.approval_id}")
    )

    assert handled
    edited = client.edited_messages[-1]
    assert envelope.approval_id in edited["text"]  # 펼친 뷰에는 ID 노출
    fold_row = edited["reply_markup"]["inline_keyboard"][1][0]
    assert fold_row["callback_data"] == f"operator:ui:f:{envelope.approval_id}"

    handled = router.process_update(
        callback_update(f"operator:ui:f:{envelope.approval_id}", update_id=3)
    )
    assert handled
    folded = client.edited_messages[-1]
    assert envelope.approval_id not in folded["text"]  # 접힌 뷰에는 ID 숨김


def test_ui_detail_callback_for_stale_approval_answers_only(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config, store=store, audit=audit, client=client
    )

    handled = router.process_update(callback_update("operator:ui:d:appr_missing"))

    assert handled
    assert client.edited_messages == []
    assert client.answered_callbacks[-1]["text"] == "이미 처리됐거나 만료된 요청이에요."
```

주의: `FakeTelegramClient`에 `edit_message_text`가 없으면 (Step 2 실패 메시지로 확인) 클래스에 다음을 추가한다:

```python
    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.edited_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True}
```

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_telegram_operator_ui.py::test_ui_detail_callback_expands_approval_card -v`
Expected: FAIL — ui: prefix가 라우팅되지 않아 edit가 발생하지 않음

- [ ] **Step 3: handlers.py 구현**

import 추가:

```python
from maestro.integrations.telegram.ui import catalog as ui_catalog
from maestro.integrations.telegram.ui.cards import render_approval_card
```

`_process_callback`의 분기 목록에서 `if action.startswith("appr:"):` **앞에** 추가:

```python
        if action.startswith("ui:"):
            return self._process_ui_toggle(callback, action, chat_id, user_id, username)
```

라우터 클래스에 메서드 추가 (`_process_async_approval_callback` 근처):

```python
    def _process_ui_toggle(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        parts = action.split(":", 2)
        if len(parts) != 3 or parts[1] not in {"d", "f"}:
            self._answer(callback, ui_catalog.STALE_CALLBACK_TEXT)
            return True
        envelope = self._pending_async_approval(parts[2])
        if envelope is None:
            self._answer(callback, ui_catalog.STALE_CALLBACK_TEXT)
            self._record("/approval", chat_id, user_id, username, "stale_callback")
            return True
        card = render_approval_card(envelope.request, expanded=parts[1] == "d")
        message = callback.get("message")
        message_id = message.get("message_id") if isinstance(message, Mapping) else None
        if message_id is None:
            self._answer(callback, ui_catalog.STALE_CALLBACK_TEXT)
            return True
        self.client.edit_message_text(
            chat_id,
            int(message_id),
            card.text,
            reply_markup=card.reply_markup,
        )
        self._answer(callback, "")
        self._record("/approval", chat_id, user_id, username, "ui_toggle")
        return True
```

주의: `self._answer`와 `self.client.edit_message_text`의 실제 시그니처를 먼저 확인하고 (`rg -n "def _answer|def edit_message_text" src/maestro/integrations/telegram/`) 다르면 그에 맞춘다. edit 실패 예외는 기존 `_edit_callback_message`의 패턴을 따르되 이 토글에서는 실패 시 answer만 남긴다 (`except (RuntimeError, ValueError): self._answer(callback, ui_catalog.CALLBACK_FAILED_TEXT)`).

- [ ] **Step 4: 통과 확인**

Run: `python -m pytest tests/test_telegram_operator_ui.py -k "ui_detail" -v`
Expected: PASS (2건)

- [ ] **Step 5: Commit**

```bash
git add src/maestro/integrations/telegram/handlers.py tests/test_telegram_operator_ui.py
git commit -m "feat: add detail/fold toggle callback for approval cards

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 승인 결정 피드백·리마인더 한글화

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py:1320-1350` (`_process_async_approval_callback`의 answer/edit 문자열), `handlers.py:1452-1461` (sweep 리마인더 문자열)
- Test: `tests/test_telegram_operator_ui.py`

**Interfaces:**
- Consumes: Task 2의 `approval_decision_text`, `approval_reminder_text`, `catalog.ANSWER_APPROVED`, `catalog.ANSWER_REJECTED`, `catalog.CALLBACK_FAILED_TEXT`, `catalog.STALE_CALLBACK_TEXT`; Task 4의 `approval_markup`은 리마인더 재전송 markup에 사용 (`approval_markup(envelope.approval_id, expanded=False)` — 자세히 버튼 포함).
- Produces: 승인/거절 callback 후 카드가 한글 결과 텍스트로 edit되고, 리마인더 메시지가 한글로 발송됨.

- [ ] **Step 1: 실패하는 테스트 작성**

기존 async approval callback 테스트를 확인한다: `rg -n "Approval approved|appr:a:" tests/test_telegram_operator_ui.py`. 기존 테스트가 영어 문구를 단언하면 그 단언을 아래 새 문구로 바꾸고, 없으면 다음 테스트를 추가:

```python
def test_approval_callback_edits_card_with_korean_result(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config, store=store, audit=audit, client=client
    )
    envelope = _pending_approval_envelope()
    store.save_system_event(
        new_run_id(), "telegram_approval_pending", envelope.model_dump(mode="json")
    )

    handled = router.process_update(
        callback_update(f"operator:appr:r:{envelope.approval_id}")
    )

    assert handled
    assert client.answered_callbacks[-1]["text"] == "거절했어요."
    assert client.edited_messages[-1]["text"].startswith("❌ 거절했어요")
```

주의: 거절(`appr:r`)을 쓰는 이유 — 승인(`appr:a`)은 `resolve_pending_signal_approval`이 브로커/설정 의존을 타므로 이 단위 테스트에서 안정적이지 않다. 승인 경로 문구는 Step 3 수정 후 기존 통합 테스트(`test_tranquillo_live_approval_workflow.py`)의 단언 갱신으로 커버한다.

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_telegram_operator_ui.py::test_approval_callback_edits_card_with_korean_result -v`
Expected: FAIL — 현재 answer는 `"Approval rejected."`, edit는 `"Approval rejected\napproval_id: ..."`

- [ ] **Step 3: handlers.py 문자열 교체**

`_process_async_approval_callback` (1320행 부근)에서:

```python
        # 변경 전: self._answer(callback, "This approval request is no longer active.")
        self._answer(callback, ui_catalog.STALE_CALLBACK_TEXT)
```

(같은 함수 안 두 곳 모두), 그리고:

```python
        # 변경 전: self._answer(callback, f"Approval failed: {exc}")
        self._answer(callback, ui_catalog.CALLBACK_FAILED_TEXT)
```

```python
        # 변경 전: self._answer(callback, f"Approval {status}.")
        self._answer(
            callback,
            ui_catalog.ANSWER_APPROVED if status == "approved" else ui_catalog.ANSWER_REJECTED,
        )
        # 변경 전: self._edit_callback_message(callback, f"Approval {status}\n...")
        self._edit_callback_message(
            callback,
            approval_decision_text(status, envelope.approval_id, summary.orders_created),
        )
```

import에 `approval_decision_text`, `approval_reminder_text`, `approval_markup` 추가 (Task 4에서 추가한 import 줄 확장).

`_sweep_pending_approvals` (1452행 부근) 리마인더:

```python
                # 변경 전: f"Approval reminder ({reminder_seconds // 60}m)\n\n{envelope.message}"
                # 변경 전 markup: _async_approval_markup(envelope.approval_id)
                for chat_id in self.config.approval.telegram_allowed_chat_ids:
                    self._send(
                        chat_id,
                        approval_reminder_text(reminder_seconds // 60, envelope.message),
                        reply_markup=approval_markup(envelope.approval_id, expanded=False),
                    )
```

- [ ] **Step 4: 통과·회귀 확인**

Run: `python -m pytest tests/test_telegram_operator_ui.py tests/test_tranquillo_live_approval_workflow.py -v`
Expected: PASS. 영어 문구를 단언하던 기존 테스트는 새 한글 문구로 단언만 갱신.

- [ ] **Step 5: Commit**

```bash
git add src/maestro/integrations/telegram/handlers.py tests/
git commit -m "feat: localize approval decision feedback and reminders in Korean

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 명령어 메뉴 5개 등록 + 별칭 + 한글 /help

**Files:**
- Modify: `src/maestro/integrations/telegram/handlers.py:114-137` (`TELEGRAM_OPERATOR_COMMANDS` 유지, 신규 `TELEGRAM_UI_COMMANDS` 추가), `handlers.py:4597-4615` (`telegram_bot_commands`), `handlers.py:222-240` (message 핸들러 dict에 별칭 추가), `_help` 메서드 (handlers.py:2871 근처)
- Test: `tests/test_telegram_operator_ui.py`

**Interfaces:**
- Consumes: 기존 핸들러 메서드 `self._signal`, `self._health`, `self._orders`, `self._portfolio`.
- Produces:
  - `TELEGRAM_UI_COMMANDS: tuple[tuple[str, str], ...]` — 메뉴에 등록되는 5개.
  - `telegram_bot_commands(signal_config=None)` — **시그니처 유지**, 반환은 5개만 (per-strategy rebalance 명령은 메뉴에서 제외되지만 타이핑하면 여전히 동작). 호출부 `src/maestro/cli.py:1046`은 무변경.
  - `/today` `/system` `/history`는 기존 `/signal` `/health` `/orders` 핸들러의 별칭 (전체 한글 카드는 단계 5에서 교체).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_telegram_bot_commands_registers_only_five_korean_commands():
    commands = telegram_bot_commands()
    assert commands == [
        {"command": "today", "description": "오늘의 투자 현황"},
        {"command": "portfolio", "description": "내 자산"},
        {"command": "system", "description": "시스템 상태"},
        {"command": "history", "description": "지난 기록"},
        {"command": "help", "description": "도움말"},
    ]


def test_new_command_aliases_route_to_existing_handlers(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config, store=store, audit=audit, client=client
    )

    assert router.process_update(message_update("/system"))
    system_text = client.sent_messages[-1]["text"]
    assert router.process_update(message_update("/health", update_id=5))
    health_text = client.sent_messages[-1]["text"]
    # 별칭은 기존 핸들러와 동일 형식 (타임스탬프가 달라질 수 있어 첫 줄만 비교)
    assert system_text.splitlines()[0] == health_text.splitlines()[0]


def test_help_is_korean_and_lists_five_commands(tmp_path):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    router = TelegramOperatorCommandRouter(
        config=config, store=store, audit=audit, client=client
    )

    assert router.process_update(message_update("/help"))
    text = client.sent_messages[-1]["text"]
    assert "/today" in text and "/portfolio" in text and "/system" in text
    assert "/history" in text and "/help" in text
    assert "이런 알림이 올 수 있어요" in text
```

주의: `telegram_bot_commands`를 단언하는 기존 테스트(`tests/test_operator_deployment_wiring.py` 및 `test_telegram_operator_ui.py:2200` 부근)는 등록 결과와 함수 반환을 **같은 함수로 비교**하므로 계속 통과해야 한다. `rg -n "telegram_bot_commands" tests/`로 확인하고, 20개 명령 원문 목록을 하드코딩한 단언이 있으면 5개 목록으로 갱신한다.

- [ ] **Step 2: 실패 확인**

Run: `python -m pytest tests/test_telegram_operator_ui.py -k "five_korean or aliases or help_is_korean" -v`
Expected: FAIL — 20개 영어 명령 반환, `/system` 미라우팅, 영어 help

- [ ] **Step 3: 구현**

`handlers.py:137` (`TELEGRAM_OPERATOR_COMMANDS` 정의 직후)에 추가:

```python
TELEGRAM_UI_COMMANDS: tuple[tuple[str, str], ...] = (
    ("today", "오늘의 투자 현황"),
    ("portfolio", "내 자산"),
    ("system", "시스템 상태"),
    ("history", "지난 기록"),
    ("help", "도움말"),
)
```

`telegram_bot_commands` (4597행) 본문 교체:

```python
def telegram_bot_commands(signal_config: MaestroConfig | None = None) -> list[dict[str, str]]:
    # 메뉴에는 UI 명령 5개만 노출한다. 기존 20개와 per-strategy 명령은
    # 타이핑하면 여전히 동작한다 (signal_config 인자는 하위 호환용으로 유지).
    return [
        {"command": command, "description": description}
        for command, description in TELEGRAM_UI_COMMANDS
    ]
```

`process_update`의 핸들러 dict (222행 부근)에 별칭 3개 추가:

```python
                "signal": self._signal,
                "today": self._signal,
                "system": self._health,
                "history": self._orders,
```

`_help` 본문 교체 (기존 TELEGRAM_OPERATOR_COMMANDS 나열 제거):

```python
    def _help(self, chat_id: int) -> None:
        self._send(
            chat_id,
            "\n".join(
                [
                    "Maestro 명령어",
                    *[
                        f"/{command} - {description}"
                        for command, description in TELEGRAM_UI_COMMANDS
                    ],
                    "",
                    "이런 알림이 올 수 있어요:",
                    "- 📩 투자 승인 요청 (버튼으로 승인/거절)",
                    "- ⏰ 승인 응답 리마인더",
                    "- ⚠️ 확인이 필요한 상황 안내",
                ]
            ),
        )
```

- [ ] **Step 4: 통과·회귀 확인**

Run: `python -m pytest tests/test_telegram_operator_ui.py tests/test_operator_deployment_wiring.py -v`
Expected: PASS. 20개 목록을 하드코딩한 단언이 있으면 Step 1 주의에 따라 갱신.

- [ ] **Step 5: 전체 테스트**

Run: `python -m pytest tests/ -x -q`
Expected: PASS (기존 스위트 전체 — 실패가 있으면 문구 단언 갱신 여부를 먼저 의심)

- [ ] **Step 6: Commit**

```bash
git add src/maestro/integrations/telegram/handlers.py tests/
git commit -m "feat: register five Korean menu commands with aliases and Korean help

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## 배포 확인 (단계 1 승인 조건)

코드 머지 후 VPS 배포 시 확인할 것 (스펙 마이그레이션 표 기준):

- systemd 구성 변경 없음. `set_my_commands`는 기존 기동 경로(cli.py:1046)에서 자동 호출됨 — 봇 재시작 후 Telegram 메뉴에 5개 한글 명령만 보이는지 확인.
- 실제 승인 요청 1건에서: 접힌 카드 수신 → 자세히 → 접기 → 승인/거절 → 한글 결과 edit 확인.
- 기존 명령(`/status`, `/cash_drift` 등)을 타이핑하면 여전히 동작하는지 확인.

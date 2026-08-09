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

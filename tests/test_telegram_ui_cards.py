from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from maestro.approval.models import ApprovalRequest
from maestro.core.ids import new_budget_request_id, new_funding_request_id
from maestro.integrations.telegram.ui import catalog
from maestro.integrations.telegram.ui.cards import (
    TELEGRAM_TEXT_LIMIT,
    approval_decision_text,
    approval_detail_pages,
    approval_reminder_text,
    render_approval_card,
    render_approval_stage_card,
    render_funding_workflow_card,
    telegram_text_length,
)
from maestro.integrations.telegram.ui.format import money_kr
from maestro.integrations.telegram.ui.funding_workflow import (
    FundingWorkflowAttention,
    FundingWorkflowCardModel,
    FundingWorkflowCardStage,
    FundingWorkflowPhase,
    FundingWorkflowRequestRef,
)


def _request() -> ApprovalRequest:
    expires = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)  # KST 밤 11시 30분
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
        "Tranquillo 전략 · 국내 주식 2종목 · 매수 2건 · 총 124만원\n"
        "\n"
        "• 🟢 매수 KODEX 200 10주 — 71.2만원\n"
        "• 🟢 매수 TIGER 미국S&P500 28주 — 52.8만원\n"
        "\n"
        "⏰ 밤 11시 30분까지 응답해 주세요."
    )


def test_sell_orders_show_sell_direction():
    base = _request()
    sells = [dict(order, side="sell") for order in base.proposed_orders]
    card = render_approval_card(base.model_copy(update={"proposed_orders": sells}), expanded=False)
    assert "매도 2건" in card.text
    assert "• 🔴 매도 KODEX 200 10주 — 71.2만원" in card.text
    assert "매수" not in card.text


def test_mixed_direction_summary_lists_both_counts():
    base = _request()
    mixed = [base.proposed_orders[0], dict(base.proposed_orders[1], side="sell")]
    card = render_approval_card(base.model_copy(update={"proposed_orders": mixed}), expanded=False)
    assert "매수 1건 · 매도 1건" in card.text
    assert "• 🟢 매수 KODEX 200" in card.text
    assert "• 🔴 매도 TIGER 미국S&P500" in card.text


def test_unknown_direction_is_flagged_instead_of_assumed_buy():
    base = _request()
    unknown = [{k: v for k, v in base.proposed_orders[0].items() if k != "side"}]
    card = render_approval_card(
        base.model_copy(update={"proposed_orders": unknown}), expanded=False
    )
    assert "• ⚠️ 방향 미상 KODEX 200 10주 — 71.2만원" in card.text
    assert "매수" not in card.text


def test_expanded_card_shows_account_id_per_order():
    base = _request()
    orders = [
        dict(base.proposed_orders[0], account_id="kis_ps"),
        dict(base.proposed_orders[1], account_id="kis_isa"),
    ]
    card = render_approval_card(base.model_copy(update={"proposed_orders": orders}), expanded=True)
    assert "  계좌: kis_ps" in card.text
    assert "  계좌: kis_isa" in card.text
    collapsed = render_approval_card(
        base.model_copy(update={"proposed_orders": orders}), expanded=False
    )
    assert "  계좌: " not in collapsed.text  # 주문별 계좌 줄은 펼친 뷰에만


def test_collapsed_summary_shows_account_scope():
    base = _request()
    orders = [
        dict(base.proposed_orders[0], account_id="kis_ps"),
        dict(base.proposed_orders[1], account_id="kis_isa"),
    ]
    card = render_approval_card(base.model_copy(update={"proposed_orders": orders}), expanded=False)
    assert "· 계좌 kis_isa, kis_ps ·" in card.text


def test_collapsed_summary_shows_single_account():
    base = _request()
    orders = [dict(order, account_id="kis_ps") for order in base.proposed_orders]
    card = render_approval_card(base.model_copy(update={"proposed_orders": orders}), expanded=False)
    assert "· 계좌 kis_ps ·" in card.text


def test_collapsed_summary_counts_accounts_beyond_three():
    base = _request()
    template = base.proposed_orders[0]
    orders = [dict(template, account_id=f"kis_{index}") for index in range(5)]
    card = render_approval_card(base.model_copy(update={"proposed_orders": orders}), expanded=False)
    assert "· 계좌 kis_0, kis_1, kis_2 외 2곳 ·" in card.text


def test_collapsed_summary_omits_account_scope_when_unknown():
    card = render_approval_card(_request(), expanded=False)  # account_id 없는 주문
    assert "계좌" not in card.text


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


def test_risk_violation_reasons_are_visible_before_approving():
    request = _request().model_copy(
        update={"risk_violations": ["max_notional exceeded", "sector cap exceeded"]}
    )
    collapsed = render_approval_card(request, expanded=False)
    # 승인 버튼이 붙는 첫 화면에서 위험 사유 원문을 확인할 수 있어야 한다.
    assert "⚠️ 위험 점검에서 확인할 내용이 2건 있어요." in collapsed.text
    assert "- max_notional exceeded" in collapsed.text
    assert "- sector cap exceeded" in collapsed.text
    expanded = render_approval_card(request, expanded=True)
    assert "max_notional exceeded" in expanded.text


def test_collapsed_view_lists_first_risk_reasons_and_counts_the_rest():
    request = _request().model_copy(
        update={"risk_violations": [f"위반 사유 {index}" for index in range(5)]}
    )
    collapsed = render_approval_card(request, expanded=False)
    for index in range(3):
        assert f"- 위반 사유 {index}" in collapsed.text
    assert "- 위반 사유 3" not in collapsed.text
    assert "- 외 2건" in collapsed.text


def test_many_orders_are_truncated_in_collapsed_view():
    base = _request()
    order = dict(base.proposed_orders[0])
    request = base.model_copy(
        update={"proposed_orders": [dict(order) for _ in range(9)], "order_count": 9}
    )
    card = render_approval_card(request, expanded=False)
    assert card.text.count("•") == 7  # 6줄 + "외 3건" 줄
    assert "• 외 3건" in card.text


def _bulk_request(order_count: int, *, risk_violations: list[str] | None = None):
    base = _request()
    template = base.proposed_orders[0]
    orders = [
        dict(template, symbol=f"{index:06d}", name=f"국내 상장 ETF 종목 {index}")
        for index in range(order_count)
    ]
    return base.model_copy(
        update={
            "proposed_orders": orders,
            "order_count": order_count,
            "risk_violations": list(risk_violations or []),
        }
    )


def test_expanded_card_stays_within_telegram_limit_and_paginates():
    request = _bulk_request(120)
    first = render_approval_card(request, expanded=True)

    assert telegram_text_length(first.text) <= TELEGRAM_TEXT_LIMIT
    assert first.page_count > 1
    assert first.page == 0
    next_button = [
        button
        for row in first.reply_markup["inline_keyboard"]
        for button in row
        if button["callback_data"].startswith("operator:ui:p:")
    ]
    assert next_button, "여러 쪽이면 페이지 이동 버튼이 있어야 한다"
    for row in first.reply_markup["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode()) <= 64

    last = render_approval_card(request, expanded=True, page=first.page_count - 1)
    assert telegram_text_length(last.text) <= TELEGRAM_TEXT_LIMIT
    assert last.page == first.page_count - 1


def test_expanded_pages_cover_every_order_and_risk_reason():
    request = _bulk_request(120, risk_violations=[f"위반 사유 {index}" for index in range(20)])
    pages = approval_detail_pages(request)

    assert len(pages) > 1
    joined = "\n".join(pages)
    for index in range(120):
        assert f"{index:06d}" in joined
    for index in range(20):
        assert f"위반 사유 {index}" in joined
    for page in pages:
        assert telegram_text_length(page) <= TELEGRAM_TEXT_LIMIT


def test_oversized_single_block_is_split_across_pages_without_losing_text():
    violation = "".join(f"[{index:05d}]" for index in range(2000))  # 한 쪽보다 훨씬 긴 사유 1건
    long_name = "아주긴상품명" * 900
    base = _request()
    request = base.model_copy(
        update={
            "proposed_orders": [dict(base.proposed_orders[0], name=long_name)],
            "order_count": 1,
            "risk_violations": [violation],
        }
    )

    pages = approval_detail_pages(request)
    joined = "".join(pages)
    for page in pages:
        assert telegram_text_length(page) <= TELEGRAM_TEXT_LIMIT
    # 쪽 경계에서 문자열이 나뉠 수는 있어도 잘려 사라지면 안 된다.
    assert catalog.TRUNCATED_MARK not in joined
    assert joined.count("[") == 2000  # 위험 사유의 모든 조각이 남아 있다
    assert sum(page.count("아주긴") for page in pages) >= 899  # 긴 상품명도 보존


def test_page_out_of_range_clamps_to_last_page():
    request = _bulk_request(120)
    card = render_approval_card(request, expanded=True, page=999)
    assert card.page == card.page_count - 1


def test_collapsed_card_stays_within_telegram_limit():
    request = _bulk_request(400, risk_violations=["아주 긴 위험 사유 " * 400])
    card = render_approval_card(request, expanded=False)
    assert telegram_text_length(card.text) <= TELEGRAM_TEXT_LIMIT


def test_decision_and_reminder_texts():
    assert approval_decision_text("approved", "appr_x", orders_submitted=2, orders_failed=0) == (
        "✅ 승인 완료 — 주문 2건을 접수했어요."
    )
    assert approval_decision_text("rejected", "appr_x", orders_submitted=0, orders_failed=0) == (
        "❌ 거절했어요 — 이번 제안은 실행되지 않아요."
    )
    reminder = approval_reminder_text(30, "카드 본문")
    assert reminder == "⏰ 아직 응답을 기다리고 있어요 (30분 경과)\n\n카드 본문"
    assert catalog.STALE_CALLBACK_TEXT == "이미 처리됐거나 만료된 요청이에요."


def test_reminder_text_stays_within_telegram_limit():
    card_text = "가" * TELEGRAM_TEXT_LIMIT
    reminder = approval_reminder_text(30, card_text)
    assert telegram_text_length(reminder) <= TELEGRAM_TEXT_LIMIT
    assert reminder.startswith("⏰ 아직 응답을 기다리고 있어요 (30분 경과)")


def test_decision_text_does_not_claim_submission_when_nothing_was_submitted():
    text = approval_decision_text("approved", "appr_x", orders_submitted=0, orders_failed=0)
    assert text == "⚠️ 승인했지만 접수된 주문이 없어요. /history에서 확인해 주세요."


def test_decision_text_reports_partial_failure():
    text = approval_decision_text("approved", "appr_x", orders_submitted=1, orders_failed=2)
    assert text == "⚠️ 승인 완료 — 주문 1건 접수, 2건은 실패했어요. /history에서 확인해 주세요."


def test_decision_text_reports_total_failure():
    text = approval_decision_text("approved", "appr_x", orders_submitted=0, orders_failed=2)
    assert text == "⚠️ 승인했지만 주문 2건이 모두 실패했어요. /history에서 확인해 주세요."


def test_decision_text_for_expired_does_not_claim_the_operator_rejected():
    """G3: 만료된 승인이 재개되면 운영자는 보지도 못한 제안을 "거절했어요"라는
    통지로 받는다. 실행되지 않았다는 사실은 같지만 원인이 사실과 다르다."""
    text = approval_decision_text("expired", "appr_x", orders_submitted=0, orders_failed=0)
    assert text == catalog.DECISION_EXPIRED
    assert "거절" not in text


def test_the_pending_stage_card_is_byte_identical_to_the_plain_approval_card():
    """Stage 2 must not change what the operator sees before deciding.

    The dispatch path renders through the stage-aware function now, so any
    divergence here would silently redesign the approval card that production
    has been sending all along.
    """
    request = _request()

    assert render_approval_stage_card(request, "pending") == render_approval_card(
        request, expanded=False
    )


def test_a_decided_card_keeps_the_body_and_gains_a_stage_header():
    """One card carries both what was ordered and how far it got.

    Replacing the body with a bare result line is what the stream of separate
    notifications already did; the point of the card is that the two stay
    together.
    """
    card = render_approval_stage_card(_request(), "in_progress")

    assert card.text.startswith(catalog.CARD_STAGE_LABELS["in_progress"])
    assert "KODEX 200" in card.text
    assert catalog.APPROVAL_TITLE in card.text


def test_the_buttons_disappear_once_the_decision_is_made():
    """A decided approval that still offers 승인/거절 invites a second decision.

    The callback would be rejected as stale, but only after the operator has
    been told the thing is still theirs to decide.
    """
    for stage in ("in_progress", "done", "attention"):
        assert render_approval_stage_card(_request(), stage).reply_markup is None


def test_an_unknown_stage_falls_back_to_the_pending_card():
    """A stage we cannot name must not drop the buttons off a live approval."""
    card = render_approval_stage_card(_request(), "not_a_stage")

    assert card.reply_markup is not None


def test_a_stage_card_stays_within_the_telegram_limit():
    """The header is added on top of a body already clamped to the limit."""
    request = _request()
    request.proposed_orders = request.proposed_orders * 200

    card = render_approval_stage_card(request, "attention")

    assert telegram_text_length(card.text) <= TELEGRAM_TEXT_LIMIT


def _funding_workflow_card_model(
    stage: FundingWorkflowCardStage,
    *,
    phase: FundingWorkflowPhase = "funding",
    attention: FundingWorkflowAttention | None = None,
    financial_actions_allowed: bool | None = None,
    selected_budget: float | None = None,
    request_id: str = "req_1",
    request_payload: dict[str, Any] | None = None,
) -> FundingWorkflowCardModel:
    if financial_actions_allowed is None:
        financial_actions_allowed = (
            stage in ("funding_pending", "budget_pending") and attention is None
        )
    if request_payload is None:
        if phase == "funding":
            request_payload = {
                "request_id": request_id,
                "strategy_ids": ["tranquillo"],
                "contribution_group_id": "core",
                "account_id": "kis_ps",
                "execution_sleeve": "krw_contribution",
                "currency": "KRW",
                "available_cash": 500_000.0,
                "min_monthly_budget": 1_000_000.0,
                "required_shortfall": 500_000.0,
                "recommended_top_up": 500_000.0,
                "month_key": "2026-08",
            }
        else:
            request_payload = {
                "request_id": request_id,
                "strategy_ids": ["tranquillo"],
                "contribution_group_id": "core",
                "account_id": "kis_isa",
                "execution_sleeve": "krw_contribution",
                "currency": "KRW",
                "available_cash": 8_000_000.0,
                "min_monthly_budget": 1_660_000.0,
                "recommended_budget": 4_000_000.0,
                "selectable_max_budget": 8_000_000.0,
                "month_key": "2026-08",
            }
    return FundingWorkflowCardModel(
        workflow_id="wf_123",
        month_key="2026-08",
        scope=("core", "kis_ps" if phase == "funding" else "kis_isa", "krw_contribution", "KRW"),
        phase=phase,
        request_id=request_id,
        request=request_payload,
        stage=stage,
        attention=attention,
        financial_actions_allowed=financial_actions_allowed,
        terminal_intent="cancel"
        if "canceled" in stage
        else ("confirm" if "completed" in stage else None),
        selected_budget=selected_budget,
        predecessor_request_id=None,
        predecessor_completed=None,
        card_delivery_version=0,
        lineage=(
            FundingWorkflowRequestRef(request_id=request_id, phase=phase, lineage_distance=0),
        ),
    )


@pytest.mark.parametrize(
    ("stage", "phase", "expected_first_line", "actions_allowed"),
    [
        ("funding_pending", "funding", "📥 입금이 필요해요", True),
        ("funding_confirming", "funding", "⏳ 입금을 확인하고 있어요", False),
        ("funding_canceling", "funding", "⏳ 취소를 처리하고 있어요", False),
        ("budget_pending", "budget", "💰 이번 달 예산을 선택해 주세요", True),
        ("budget_applying", "budget", "⏳ 예산을 적용하고 있어요", False),
        ("budget_canceling", "budget", "⏳ 취소를 처리하고 있어요", False),
        ("funding_canceled", "funding", "🛑 이번 달 입금 요청을 취소했어요", False),
        ("budget_canceled", "budget", "🛑 이번 달 예산 선택을 취소했어요", False),
        ("budget_completed", "budget", "✅ 이번 달 예산을 확정했어요", False),
        ("funding_completed", "funding", "✅ 자금 확인을 마쳤어요", False),
    ],
)
def test_funding_workflow_card_snapshots_for_every_stage(
    stage: FundingWorkflowCardStage,
    phase: FundingWorkflowPhase,
    expected_first_line: str,
    actions_allowed: bool,
):
    selected_budget = 400_000.0 if stage == "budget_completed" else None
    model = _funding_workflow_card_model(
        stage=stage,
        phase=phase,
        financial_actions_allowed=actions_allowed,
        selected_budget=selected_budget,
    )
    rendered = render_funding_workflow_card(model)
    assert rendered.text.splitlines()[0] == expected_first_line
    if model.financial_actions_allowed:
        assert rendered.reply_markup is not None
    else:
        assert rendered.reply_markup is None

    if stage == "budget_completed":
        assert money_kr(400_000.0, "KRW") in rendered.text
    elif stage == "funding_completed":
        assert "선택 예산" not in rendered.text
        assert "예산 확정" not in rendered.text
        assert "예산 선택" not in rendered.text


def test_funding_workflow_card_predecessor_incomplete_snapshot():
    model = _funding_workflow_card_model(
        stage="budget_pending",
        phase="budget",
        attention="predecessor_incomplete",
        financial_actions_allowed=False,
    )
    rendered = render_funding_workflow_card(model)
    assert rendered.text.splitlines()[0] == "⚠️ 자금 확인을 마무리하고 있어요"
    assert rendered.reply_markup is None
    assert "예산" in rendered.text
    assert "선택할 수 없" in rendered.text or "마무리" in rendered.text


def test_funding_workflow_card_incomplete_transition_snapshot():
    model = _funding_workflow_card_model(
        stage="funding_pending",
        phase="funding",
        attention="incomplete_transition",
        financial_actions_allowed=False,
    )
    rendered = render_funding_workflow_card(model)
    assert rendered.text.splitlines()[0] == "📥 입금이 필요해요"
    assert "⚠️ 처리가 끝나지 않아 확인이 필요해요." in rendered.text
    assert rendered.reply_markup is None


def test_funding_workflow_card_button_authority_and_payloads():
    funding_id = new_funding_request_id()
    funding_model = _funding_workflow_card_model(
        stage="funding_pending",
        phase="funding",
        request_id=funding_id,
        financial_actions_allowed=True,
    )
    rendered_funding = render_funding_workflow_card(funding_model)
    assert rendered_funding.reply_markup is not None
    funding_rows = rendered_funding.reply_markup["inline_keyboard"]
    funding_callbacks = [b["callback_data"] for row in funding_rows for b in row]
    assert f"operator:funding:complete:{funding_id}" in funding_callbacks
    assert f"operator:funding:cancel:{funding_id}" in funding_callbacks
    for cb in funding_callbacks:
        assert "funding_workflow_id" not in cb
        assert "operator:wfresume" not in cb
        assert "operator:appr" not in cb
        assert "operator:ui:" not in cb
        assert len(cb.encode("utf-8")) <= 64

    budget_id = new_budget_request_id()
    budget_model = _funding_workflow_card_model(
        stage="budget_pending",
        phase="budget",
        request_id=budget_id,
        financial_actions_allowed=True,
    )
    rendered_budget = render_funding_workflow_card(budget_model)
    assert rendered_budget.reply_markup is not None
    budget_rows = rendered_budget.reply_markup["inline_keyboard"]
    budget_callbacks = [b["callback_data"] for row in budget_rows for b in row]
    assert f"operator:budget:sel:{budget_id}:m" in budget_callbacks
    assert f"operator:budget:sel:{budget_id}:r" in budget_callbacks
    assert f"operator:budget:sel:{budget_id}:f" in budget_callbacks
    assert f"operator:budget:cancel:{budget_id}" in budget_callbacks
    for cb in budget_callbacks:
        assert "funding_workflow_id" not in cb
        assert "operator:wfresume" not in cb
        assert "operator:appr" not in cb
        assert "operator:ui:" not in cb
        assert len(cb.encode("utf-8")) <= 64


def test_funding_workflow_card_attention_and_terminal_are_buttonless():
    terminal_stages: list[tuple[FundingWorkflowCardStage, FundingWorkflowPhase]] = [
        ("funding_canceled", "funding"),
        ("budget_canceled", "budget"),
        ("funding_completed", "funding"),
        ("budget_completed", "budget"),
    ]
    for stage, phase in terminal_stages:
        model = _funding_workflow_card_model(
            stage=stage,
            phase=phase,
            financial_actions_allowed=False,
            selected_budget=500_000.0 if stage == "budget_completed" else None,
        )
        assert render_funding_workflow_card(model).reply_markup is None

    for att in ("predecessor_incomplete", "incomplete_transition"):
        model = _funding_workflow_card_model(
            stage="budget_pending" if att == "predecessor_incomplete" else "funding_pending",
            phase="budget" if att == "predecessor_incomplete" else "funding",
            attention=att,  # type: ignore[arg-type]
            financial_actions_allowed=False,
        )
        assert render_funding_workflow_card(model).reply_markup is None


def test_funding_workflow_card_no_detail_or_fold_button():
    for stage, phase in [
        ("funding_pending", "funding"),
        ("budget_pending", "budget"),
        ("funding_confirming", "funding"),
        ("budget_applying", "budget"),
        ("budget_completed", "budget"),
    ]:
        model = _funding_workflow_card_model(
            stage=stage,  # type: ignore[arg-type]
            phase=phase,  # type: ignore[arg-type]
            selected_budget=500_000.0 if stage == "budget_completed" else None,
        )
        card = render_funding_workflow_card(model)
        assert "접기" not in card.text
        assert "자세히" not in card.text
        if card.reply_markup is not None:
            callbacks = [
                b["callback_data"] for row in card.reply_markup["inline_keyboard"] for b in row
            ]
            button_texts = [b["text"] for row in card.reply_markup["inline_keyboard"] for b in row]
            assert "접기" not in button_texts
            assert "자세히" not in button_texts
            for cb in callbacks:
                assert not cb.startswith("operator:ui:d:")
                assert not cb.startswith("operator:ui:f:")


def test_funding_workflow_card_stays_within_telegram_limit():
    long_account = "a" * 5000
    model = _funding_workflow_card_model(
        stage="funding_pending",
        phase="funding",
        request_payload={
            "request_id": "req_long",
            "account_id": long_account,
            "currency": "KRW",
        },
    )
    rendered = render_funding_workflow_card(model)
    assert telegram_text_length(rendered.text) <= TELEGRAM_TEXT_LIMIT

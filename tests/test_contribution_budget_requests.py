from datetime import datetime

from maestro.config.execution import ExecutionConfig
from maestro.core.ids import new_budget_request_id
from maestro.execution.budget_requests import (
    ContributionBudgetRequest,
    budget_request_reply_markup,
    build_contribution_budget_request,
    format_contribution_budget_request,
    selected_budget_from_request,
)
from maestro.state.models import PortfolioState


def test_budget_request_message_and_markup_include_presets():
    request = ContributionBudgetRequest(
        request_id="budget_req_1",
        source_signal_run_id="signal_1",
        strategy_ids=["tranquillo"],
        contribution_group_id="tranquillo",
        account_id="kis_isa",
        execution_sleeve="tranquillo_isa",
        currency="KRW",
        available_cash=8_000_000,
        min_monthly_budget=1_660_000,
        recommended_budget=4_000_000,
        selectable_max_budget=8_000_000,
        month_key="2026-06",
        created_at=datetime(2026, 6, 25),
        expires_at=datetime(2026, 6, 26),
    )

    message = format_contribution_budget_request(request)
    markup = budget_request_reply_markup(request)

    assert "Maestro budget request" in message
    assert "account_id: kis_isa" in message
    assert "minimum_required: 1,660,000 KRW" in message
    assert "recommended_budget: 4,000,000 KRW" in message
    assert "selectable_max_budget: 8,000,000 KRW" in message
    buttons = [button for row in markup["inline_keyboard"] for button in row]
    assert {button["text"] for button in buttons} == {
        "최소 1,660,000 KRW",
        "추천 4,000,000 KRW",
        "전액 8,000,000 KRW",
        "취소",
    }
    assert {
        button["callback_data"]
        for button in buttons
        if button["text"] != "취소"
    } == {
        "operator:budget:sel:budget_req_1:m",
        "operator:budget:sel:budget_req_1:r",
        "operator:budget:sel:budget_req_1:f",
    }


def test_budget_request_callback_data_fits_telegram_limit_for_real_ids():
    request = ContributionBudgetRequest(
        request_id=new_budget_request_id(),
        source_signal_run_id="signal_1",
        strategy_ids=["tranquillo"],
        account_id="kis_isa",
        execution_sleeve="tranquillo_isa",
        currency="KRW",
        available_cash=8_000_000,
        min_monthly_budget=1_660_000,
        recommended_budget=4_000_000,
        selectable_max_budget=8_000_000,
        month_key="2026-06",
        created_at=datetime(2026, 6, 25),
        expires_at=datetime(2026, 6, 26),
    )

    markup = budget_request_reply_markup(request)

    for row in markup["inline_keyboard"]:
        for button in row:
            assert len(button["callback_data"].encode("utf-8")) <= 64


def test_selected_budget_accepts_short_and_legacy_selection_keys():
    request = {
        "min_monthly_budget": 1_660_000.0,
        "recommended_budget": 4_000_000.0,
        "selectable_max_budget": 8_000_000.0,
    }

    assert selected_budget_from_request(request, "m") == 1_660_000.0
    assert selected_budget_from_request(request, "r") == 4_000_000.0
    assert selected_budget_from_request(request, "f") == 8_000_000.0
    assert selected_budget_from_request(request, "min") == 1_660_000.0
    assert selected_budget_from_request(request, "recommended") == 4_000_000.0
    assert selected_budget_from_request(request, "full") == 8_000_000.0


def test_missing_card_delivery_version_is_legacy_generation():
    request = ContributionBudgetRequest(
        request_id="budget_legacy",
        source_signal_run_id="signal_1",
        strategy_ids=["tranquillo"],
        account_id="kis_ps",
        execution_sleeve="tranquillo_ps",
        currency="KRW",
        available_cash=2_000_000.0,
        min_monthly_budget=1_000_000.0,
        recommended_budget=1_500_000.0,
        selectable_max_budget=2_000_000.0,
        month_key="2026-08",
        created_at=datetime(2026, 8, 1),
        expires_at=datetime(2026, 8, 2),
    )
    payload = request.model_dump(mode="json")
    payload.pop("card_delivery_version", None)

    restored = ContributionBudgetRequest.model_validate(payload)

    assert restored.card_delivery_version == 0


def test_build_contribution_budget_request_emits_delivery_version_one():
    config = ExecutionConfig(
        order_generation_mode="buy_only_contribution",
        contribution={
            "enabled": True,
            "currency": "KRW",
            "min_monthly_budget": 1_000_000.0,
            "monthly_budget": 1_500_000.0,
            "budget_request": {"enabled": True},
        },
    )
    state = PortfolioState(cash=2_000_000.0, cash_by_currency={"KRW": 2_000_000.0})
    budget_request = build_contribution_budget_request(
        source_signal_run_id="signal_1",
        strategy_ids=["tranquillo"],
        account_id="kis_ps",
        execution_sleeve="tranquillo_ps",
        execution_config=config,
        state=state,
        month_key="2026-08",
        created_at=datetime(2026, 8, 1),
        expires_after_seconds=86400,
    )
    assert budget_request is not None
    assert budget_request.card_delivery_version == 1


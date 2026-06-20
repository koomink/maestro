from datetime import datetime

from maestro.execution.budget_requests import (
    ContributionBudgetRequest,
    budget_request_reply_markup,
    format_contribution_budget_request,
)


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
        "operator:budget:select:budget_req_1:min",
        "operator:budget:select:budget_req_1:recommended",
        "operator:budget:select:budget_req_1:full",
    }

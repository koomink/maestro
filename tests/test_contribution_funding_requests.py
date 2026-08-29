from datetime import datetime

from maestro.config.execution import ExecutionConfig
from maestro.execution.funding_requests import (
    ContributionFundingRequest,
    build_contribution_funding_request,
    format_contribution_funding_request,
    funding_request_reply_markup,
)
from maestro.state.models import PortfolioState


def test_missing_card_delivery_version_is_legacy_generation():
    request = ContributionFundingRequest(
        request_id="fund_legacy",
        source_signal_run_id="signal_1",
        strategy_ids=["tranquillo"],
        account_id="kis_ps",
        execution_sleeve="tranquillo_ps",
        currency="KRW",
        available_cash=0.0,
        min_monthly_budget=1_000_000.0,
        required_shortfall=1_000_000.0,
        month_key="2026-08",
        created_at=datetime(2026, 8, 1),
        expires_at=datetime(2026, 8, 2),
    )
    payload = request.model_dump(mode="json")
    payload.pop("card_delivery_version", None)

    restored = ContributionFundingRequest.model_validate(payload)

    assert restored.card_delivery_version == 0


def test_build_contribution_funding_request_emits_delivery_version_one():
    config = ExecutionConfig(
        order_generation_mode="buy_only_contribution",
        contribution={
            "enabled": True,
            "currency": "KRW",
            "monthly_budget": 1_000_000.0,
            "min_monthly_budget": 1_000_000.0,
            "funding_request": {"enabled": True},
        },
    )
    state = PortfolioState(cash=0.0, cash_by_currency={"KRW": 0.0})
    funding_request = build_contribution_funding_request(
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
    assert funding_request is not None
    assert funding_request.card_delivery_version == 1


def test_funding_request_message_and_markup():
    request = ContributionFundingRequest(
        request_id="fund_req_1",
        source_signal_run_id="signal_1",
        strategy_ids=["tranquillo"],
        contribution_group_id="tranquillo",
        account_id="kis_ps",
        execution_sleeve="tranquillo_ps",
        currency="KRW",
        available_cash=500_000.0,
        min_monthly_budget=1_000_000.0,
        required_shortfall=500_000.0,
        recommended_top_up=500_000.0,
        month_key="2026-08",
        created_at=datetime(2026, 8, 1),
        expires_at=datetime(2026, 8, 2),
    )
    message = format_contribution_funding_request(request)
    markup = funding_request_reply_markup("fund_req_1")

    assert "Maestro funding request" in message
    assert "account_id: kis_ps" in message
    assert "shortfall: 500,000 KRW" in message
    buttons = [b for row in markup["inline_keyboard"] for b in row]
    assert {b["text"] for b in buttons} == {"입금 완료", "취소"}

from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from maestro.config.execution import ExecutionConfig
from maestro.core.ids import new_funding_request_id
from maestro.state.models import PortfolioState

FundingRequestStatus = Literal["pending", "confirmed", "canceled"]


class ContributionFundingRequest(BaseModel):
    request_id: str = Field(default_factory=new_funding_request_id)
    source_signal_run_id: str
    strategy_ids: list[str]
    account_id: str | None = None
    execution_sleeve: str | None = None
    currency: str
    available_cash: float
    min_monthly_budget: float
    required_shortfall: float
    month_key: str
    created_at: datetime
    expires_at: datetime
    status: FundingRequestStatus = "pending"


def build_contribution_funding_request(
    *,
    source_signal_run_id: str,
    strategy_ids: list[str],
    account_id: str | None,
    execution_sleeve: str | None,
    execution_config: ExecutionConfig,
    state: PortfolioState,
    month_key: str,
    created_at: datetime,
    expires_after_seconds: int,
) -> ContributionFundingRequest | None:
    contribution = execution_config.contribution
    if execution_config.order_generation_mode != "buy_only_contribution":
        return None
    if not contribution.enabled or not contribution.funding_request.enabled:
        return None
    available_cash = contribution_available_cash(execution_config, state)
    if available_cash >= contribution.min_monthly_budget:
        return None
    shortfall = contribution.min_monthly_budget - available_cash
    return ContributionFundingRequest(
        source_signal_run_id=source_signal_run_id,
        strategy_ids=list(strategy_ids),
        account_id=account_id,
        execution_sleeve=execution_sleeve,
        currency=contribution.currency.value,
        available_cash=available_cash,
        min_monthly_budget=contribution.min_monthly_budget,
        required_shortfall=shortfall,
        month_key=month_key,
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=expires_after_seconds),
    )


def contribution_available_cash(config: ExecutionConfig, state: PortfolioState) -> float:
    currency = config.contribution.currency.value
    cash = state.cash_by_currency.get(currency, 0.0) if state.cash_by_currency else state.cash
    return max(0.0, cash) * max(0.0, 1.0 - config.live_order_limits.fee_buffer_pct)


def format_contribution_funding_request(
    request: ContributionFundingRequest | dict[str, Any],
) -> str:
    if isinstance(request, ContributionFundingRequest):
        payload = request.model_dump(mode="json")
    else:
        payload = request
    currency = str(payload.get("currency") or "")
    return "\n".join(
        [
            "Maestro funding request",
            f"request_id: {payload.get('request_id')}",
            f"strategy: {', '.join(payload.get('strategy_ids') or []) or 'unknown'}",
            f"account_id: {payload.get('account_id') or 'n/a'}",
            f"execution_sleeve: {payload.get('execution_sleeve') or 'n/a'}",
            f"available_cash: {_money(payload.get('available_cash'), currency)}",
            f"minimum_required: {_money(payload.get('min_monthly_budget'), currency)}",
            f"shortfall: {_money(payload.get('required_shortfall'), currency)}",
            "이 금액 이상을 채워 넣어야 이번 달 최소 매수가 가능합니다.",
            "입금 완료 후 버튼을 누르면 Maestro가 현금을 다시 확인하고 새 signal을 생성합니다.",
            "이 버튼은 주문 승인이 아니며, 주문은 별도로 승인해야 합니다.",
        ]
    )


def funding_request_reply_markup(request_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "입금 완료",
                    "callback_data": f"operator:funding:complete:{request_id}",
                },
                {
                    "text": "취소",
                    "callback_data": f"operator:funding:cancel:{request_id}",
                },
            ]
        ]
    }


def _money(value: object, currency: str) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return f"- {currency}".rstrip()
    return f"{amount:,.0f} {currency}".rstrip()

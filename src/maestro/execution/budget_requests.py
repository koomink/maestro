from datetime import datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from maestro.config.execution import ExecutionConfig
from maestro.core.ids import new_budget_request_id
from maestro.core.strategy_names import strategy_display_name
from maestro.state.models import PortfolioState

BudgetRequestStatus = Literal["pending", "selected", "canceled"]


class ContributionBudgetRequest(BaseModel):
    request_id: str = Field(default_factory=new_budget_request_id)
    source_signal_run_id: str
    strategy_ids: list[str]
    contribution_group_id: str | None = None
    account_id: str | None = None
    execution_sleeve: str | None = None
    currency: str
    available_cash: float
    min_monthly_budget: float
    recommended_budget: float
    selectable_max_budget: float
    month_key: str
    created_at: datetime
    expires_at: datetime
    card_delivery_version: int = 0
    status: BudgetRequestStatus = "pending"


def build_contribution_budget_request(
    *,
    source_signal_run_id: str,
    strategy_ids: list[str],
    contribution_group_id: str | None = None,
    account_id: str | None,
    execution_sleeve: str | None,
    execution_config: ExecutionConfig,
    state: PortfolioState,
    month_key: str,
    created_at: datetime,
    expires_after_seconds: int,
) -> ContributionBudgetRequest | None:
    contribution = execution_config.contribution
    if execution_config.order_generation_mode != "buy_only_contribution":
        return None
    if not contribution.enabled or not contribution.budget_request.enabled:
        return None
    available_cash = contribution_available_cash(execution_config, state)
    if available_cash < contribution.min_monthly_budget:
        return None
    recommended = contribution.monthly_budget or contribution.min_monthly_budget
    recommended = min(max(recommended, contribution.min_monthly_budget), available_cash)
    return ContributionBudgetRequest(
        source_signal_run_id=source_signal_run_id,
        strategy_ids=list(strategy_ids),
        contribution_group_id=contribution_group_id,
        account_id=account_id,
        execution_sleeve=execution_sleeve,
        currency=contribution.currency.value,
        available_cash=available_cash,
        min_monthly_budget=contribution.min_monthly_budget,
        recommended_budget=recommended,
        selectable_max_budget=available_cash,
        month_key=month_key,
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=expires_after_seconds),
        card_delivery_version=1,
    )


def contribution_available_cash(config: ExecutionConfig, state: PortfolioState) -> float:
    currency = config.contribution.currency.value
    cash = state.cash_by_currency.get(currency, 0.0) if state.cash_by_currency else state.cash
    return max(0.0, cash) * max(0.0, 1.0 - config.live_order_limits.fee_buffer_pct)


# Telegram limits callback_data to 64 bytes, so buttons carry the short keys;
# the long keys stay accepted for updates from previously sent messages.
BUDGET_SELECTION_KEYS = {
    "m": "min",
    "r": "recommended",
    "f": "full",
    "min": "min",
    "recommended": "recommended",
    "full": "full",
}


def selected_budget_from_request(
    request: ContributionBudgetRequest | dict[str, Any],
    selection: str,
) -> float:
    payload = (
        request.model_dump(mode="json")
        if isinstance(request, ContributionBudgetRequest)
        else request
    )
    normalized = BUDGET_SELECTION_KEYS.get(selection)
    if normalized == "min":
        return float(payload["min_monthly_budget"])
    if normalized == "recommended":
        return float(payload["recommended_budget"])
    if normalized == "full":
        return float(payload["selectable_max_budget"])
    raise ValueError("Unknown budget selection")


def validate_selected_budget(request: dict[str, Any], amount: float) -> None:
    minimum = float(request.get("min_monthly_budget") or 0.0)
    maximum = float(request.get("selectable_max_budget") or 0.0)
    if amount < minimum or amount > maximum:
        raise ValueError(
            f"Budget amount out of range: {amount:,.0f}; "
            f"allowed {minimum:,.0f}-{maximum:,.0f}"
        )


def format_contribution_budget_request(
    request: ContributionBudgetRequest | dict[str, Any],
) -> str:
    payload = (
        request.model_dump(mode="json")
        if isinstance(request, ContributionBudgetRequest)
        else request
    )
    currency = str(payload.get("currency") or "")
    return "\n".join(
        [
            "Maestro budget request",
            f"request_id: {payload.get('request_id')}",
            f"strategy: {_strategy_label(payload.get('strategy_ids') or [])}",
            f"account_id: {payload.get('account_id') or 'n/a'}",
            f"execution_sleeve: {payload.get('execution_sleeve') or 'n/a'}",
            f"available_cash: {_money(payload.get('available_cash'), currency)}",
            f"minimum_required: {_money(payload.get('min_monthly_budget'), currency)}",
            f"recommended_budget: {_money(payload.get('recommended_budget'), currency)}",
            f"selectable_max_budget: {_money(payload.get('selectable_max_budget'), currency)}",
            "이번 달 매수에 사용할 예산을 선택하세요.",
            f"직접 입력: /budget {payload.get('request_id')} <amount>",
            "이 선택은 주문 승인이 아니며, 주문은 별도로 승인해야 합니다.",
        ]
    )


def budget_request_reply_markup(
    request: ContributionBudgetRequest | dict[str, Any],
) -> dict[str, Any]:
    payload = (
        request.model_dump(mode="json")
        if isinstance(request, ContributionBudgetRequest)
        else request
    )
    request_id = str(payload.get("request_id") or "")
    currency = str(payload.get("currency") or "")
    # "sel" plus single-character selection keys keep the callback_data within
    # Telegram's 64-byte limit for full-length request ids (budget_<32 hex>).
    return {
        "inline_keyboard": [
            [
                {
                    "text": "최소 " + _money(payload.get("min_monthly_budget"), currency),
                    "callback_data": f"operator:budget:sel:{request_id}:m",
                }
            ],
            [
                {
                    "text": "추천 " + _money(payload.get("recommended_budget"), currency),
                    "callback_data": f"operator:budget:sel:{request_id}:r",
                }
            ],
            [
                {
                    "text": "전액 " + _money(payload.get("selectable_max_budget"), currency),
                    "callback_data": f"operator:budget:sel:{request_id}:f",
                }
            ],
            [{"text": "취소", "callback_data": f"operator:budget:cancel:{request_id}"}],
        ]
    }


def _strategy_label(strategy_ids: list[object]) -> str:
    if not strategy_ids:
        return "unknown"
    return ", ".join(strategy_display_name(strategy_id) for strategy_id in strategy_ids)


def _money(value: object, currency: str) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return f"- {currency}".rstrip()
    return f"{amount:,.0f} {currency}".rstrip()


__all__ = [
    "BudgetRequestStatus",
    "ContributionBudgetRequest",
    "budget_request_reply_markup",
    "build_contribution_budget_request",
    "contribution_available_cash",
    "format_contribution_budget_request",
    "selected_budget_from_request",
    "validate_selected_budget",
]

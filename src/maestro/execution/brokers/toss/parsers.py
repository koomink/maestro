from datetime import datetime
from typing import Any

from maestro.core.clock import utc_now
from maestro.core.enums import Currency
from maestro.execution.brokers.readonly import (
    BrokerAccountSnapshot,
    BrokerBuyingPower,
    BrokerCashBalance,
    BrokerPosition,
    BrokerReadOnlySnapshot,
)


def toss_snapshot_from_payloads(
    *,
    account: dict[str, Any],
    holdings: dict[str, Any],
    buying_power: dict[str, Any],
    prices: list[dict[str, Any]] | None = None,
    fetched_at: datetime | None = None,
) -> BrokerReadOnlySnapshot:
    buying_power_model = _buying_power(buying_power)
    positions = [_position(item) for item in holdings.get("items", [])]
    current_prices = {
        str(item["symbol"]): _decimal(item.get("lastPrice"))
        for item in prices or []
        if item.get("symbol") and item.get("lastPrice") is not None
    }
    cash_by_currency = {buying_power_model.source_currency: buying_power_model.cash_buying_power}
    cash_balance = BrokerCashBalance(
        currency=buying_power_model.source_currency,
        cash=buying_power_model.cash_buying_power,
        withdrawable_cash=buying_power_model.cash_buying_power,
    )
    return BrokerReadOnlySnapshot(
        account=BrokerAccountSnapshot(
            account_id=str(account["accountNo"]),
            cash=buying_power_model.cash_buying_power,
            cash_by_currency=cash_by_currency,
            buying_power=buying_power_model.cash_buying_power,
            positions=positions,
            cash_balance=cash_balance,
            buying_power_detail=BrokerBuyingPower(
                cash_buying_power=buying_power_model.cash_buying_power,
                source="toss_openapi_readonly",
            ),
            daily_pnl_by_currency=_pnl_by_currency(holdings.get("dailyProfitLoss")),
            fetched_at=fetched_at or utc_now(),
            source="toss_openapi_readonly",
        ),
        current_prices=current_prices,
        order_fills=[],
        unfilled_orders=[],
    )


class _TossBuyingPower:
    def __init__(self, *, source_currency: str, cash_buying_power: float) -> None:
        self.source_currency = source_currency
        self.cash_buying_power = cash_buying_power


def _buying_power(payload: dict[str, Any]) -> _TossBuyingPower:
    return _TossBuyingPower(
        source_currency=str(payload.get("currency") or "KRW"),
        cash_buying_power=_decimal(payload.get("cashBuyingPower")),
    )


def _position(item: dict[str, Any]) -> BrokerPosition:
    return BrokerPosition(
        symbol=str(item["symbol"]),
        quantity=_decimal(item.get("quantity")),
        average_price=_decimal(item.get("averagePurchasePrice")),
        current_price=_decimal(item.get("lastPrice")),
        currency=str(item.get("currency")) if item.get("currency") is not None else None,
        name=str(item.get("name")) if item.get("name") is not None else None,
        unrealized_pnl=_optional_decimal((item.get("profitLoss") or {}).get("amount")),
    )


def _pnl_by_currency(payload: Any) -> dict[str, float] | None:
    """Normalize a Toss by-currency money object (e.g. holdings
    `dailyProfitLoss`: `{"krw": "-1000", "usd": "1.50"}`) into the
    `daily_pnl_by_currency` snapshot field the live daily-loss gate reads.

    Only keys that match a known `Currency` code are kept, so an unexpected
    payload shape degrades to None (gate falls back to per-position PnL)
    instead of feeding the gate currencies it cannot parse.
    """
    if not isinstance(payload, dict):
        return None
    known_currencies = {currency.value for currency in Currency}
    values = {
        str(currency).upper(): _decimal(value)
        for currency, value in payload.items()
        if value is not None and value != "" and str(currency).upper() in known_currencies
    }
    return values or None


def _decimal(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    return float(str(value).replace(",", ""))


def _optional_decimal(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return _decimal(value)


__all__ = ["toss_snapshot_from_payloads"]

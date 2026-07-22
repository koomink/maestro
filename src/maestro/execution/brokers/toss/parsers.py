from datetime import datetime
from typing import Any

from maestro.core.clock import utc_now
from maestro.core.enums import Currency
from maestro.execution.brokers.readonly import (
    BrokerAccountSnapshot,
    BrokerBuyingPower,
    BrokerCashBalance,
    BrokerOrderSummary,
    BrokerPosition,
    BrokerReadOnlySnapshot,
)


def toss_snapshot_from_payloads(
    *,
    account: dict[str, Any],
    holdings: dict[str, Any],
    buying_power: dict[str, Any] | None = None,
    buying_powers: list[dict[str, Any]] | None = None,
    open_orders: list[dict[str, Any]] | None = None,
    commissions: list[dict[str, Any]] | None = None,
    prices: list[dict[str, Any]] | None = None,
    fetched_at: datetime | None = None,
) -> BrokerReadOnlySnapshot:
    power_payloads = list(buying_powers or [])
    if buying_power is not None:
        power_payloads.insert(0, buying_power)
    if not power_payloads:
        raise ValueError("At least one Toss buying-power payload is required")
    power_models = [_buying_power(payload) for payload in power_payloads]
    available_by_currency = {
        model.source_currency: model.cash_buying_power for model in power_models
    }
    pending_orders = _pending_orders(open_orders or [], commissions or [])
    reserved_by_currency: dict[str, float] = {}
    for order in pending_orders:
        if order.currency and order.reserved_cash is not None:
            reserved_by_currency[order.currency] = (
                reserved_by_currency.get(order.currency, 0.0) + order.reserved_cash
            )
    cash_by_currency = {
        currency: available + reserved_by_currency.get(currency, 0.0)
        for currency, available in available_by_currency.items()
    }
    primary_currency = "KRW" if "KRW" in cash_by_currency else power_models[0].source_currency
    cash = cash_by_currency[primary_currency]
    available_cash = available_by_currency[primary_currency]
    reserved_cash = reserved_by_currency.get(primary_currency, 0.0)
    positions = [_position(item) for item in holdings.get("items", [])]
    current_prices = {
        str(item["symbol"]): _decimal(item.get("lastPrice"))
        for item in prices or []
        if item.get("symbol") and item.get("lastPrice") is not None
    }
    cash_balance = BrokerCashBalance(
        currency=primary_currency,
        cash=cash,
        available_cash=available_cash,
        reserved_cash=reserved_cash,
        available_cash_by_currency=available_by_currency,
        reserved_cash_by_currency=reserved_by_currency,
        cash_semantics="available_plus_open_buy_reservations",
    )
    return BrokerReadOnlySnapshot(
        account=BrokerAccountSnapshot(
            account_id=str(account["accountNo"]),
            cash=cash,
            cash_by_currency=cash_by_currency,
            buying_power=available_cash,
            positions=positions,
            cash_balance=cash_balance,
            buying_power_detail=BrokerBuyingPower(
                cash_buying_power=available_cash,
                source="toss_openapi_readonly",
            ),
            daily_pnl_by_currency=_pnl_by_currency(holdings.get("dailyProfitLoss")),
            fetched_at=fetched_at or utc_now(),
            source="toss_openapi_readonly",
        ),
        current_prices=current_prices,
        order_fills=[],
        unfilled_orders=pending_orders,
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


def _pending_orders(
    orders: list[dict[str, Any]],
    commissions: list[dict[str, Any]],
) -> list[BrokerOrderSummary]:
    rates = {
        str(item.get("marketCountry") or "").upper(): _decimal(item.get("commissionRate"))
        for item in commissions
        if item.get("marketCountry")
    }
    output: list[BrokerOrderSummary] = []
    for item in orders:
        execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
        quantity = _decimal(item.get("quantity"))
        filled_quantity = _decimal(execution.get("filledQuantity"))
        remaining_quantity = max(quantity - filled_quantity, 0.0)
        currency = str(item.get("currency") or "").upper() or None
        side = str(item.get("side") or "").lower()
        price = _optional_decimal(item.get("price"))
        reserved_principal = (
            remaining_quantity * price
            if side == "buy" and price is not None and remaining_quantity > 0
            else 0.0
        )
        country = "KR" if currency == "KRW" else "US" if currency == "USD" else ""
        commission_rate = rates.get(country, 0.0) / 100.0
        total_order_notional = quantity * price if price is not None else 0.0
        estimated_commission = reserved_principal * commission_rate
        if country == "US" and total_order_notional <= 10.0:
            estimated_commission = 0.0
        reserved_cash = reserved_principal + estimated_commission
        output.append(
            BrokerOrderSummary(
                order_id=str(item.get("orderId") or ""),
                symbol=str(item.get("symbol") or ""),
                side=side,
                quantity=quantity,
                status=str(item.get("status") or ""),
                submitted_at=_datetime(item.get("orderedAt")),
                filled_quantity=filled_quantity,
                average_fill_price=_optional_decimal(execution.get("averageFilledPrice")),
                raw_status=str(item.get("status") or "") or None,
                currency=currency,
                remaining_quantity=remaining_quantity,
                limit_price=price,
                reserved_cash=reserved_cash if reserved_cash > 0 else None,
                estimated_commission=(
                    estimated_commission if estimated_commission > 0 else None
                ),
            )
        )
    return output


def _datetime(value: Any) -> datetime:
    if value:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed
    return utc_now()


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

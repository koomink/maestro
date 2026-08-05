from collections.abc import Callable

from pydantic import BaseModel

from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide
from maestro.execution.base import OrderIntent
from maestro.execution.brokers.readonly import BrokerBuyingPower


class OrderCapacityBlock(BaseModel):
    order: OrderIntent
    requested_quantity: float
    requested_notional: float
    cash_buying_power: float | None = None
    max_buy_quantity: float | None = None
    reason: str
    error_type: str | None = None
    error_message: str | None = None
    checked_at: str


class OrderCapacityService:
    """Partition planned buys using authoritative, account-routed broker capacity."""

    def __init__(
        self,
        lookup: Callable[[OrderIntent], BrokerBuyingPower],
    ) -> None:
        self.lookup = lookup

    def partition(
        self,
        orders: list[OrderIntent],
    ) -> tuple[list[OrderIntent], list[OrderCapacityBlock]]:
        accepted: list[OrderIntent] = []
        blocked: list[OrderCapacityBlock] = []
        reserved_by_account: dict[str, float] = {}
        # A rotation's buy is funded by the sell filed alongside it, so a fully
        # invested account reports zero buying power right up until that sell
        # settles. Blocking the buy here leaves the book in cash for a whole
        # cycle; keep it and let the post-sell phase run the real check against
        # the broker's own balance.
        proceeds_by_account: dict[str, float] = {}
        for order in orders:
            if order.side == OrderSide.SELL:
                sell_key = order.account_id or "default"
                proceeds_by_account[sell_key] = (
                    proceeds_by_account.get(sell_key, 0.0) + order.notional
                )
        for order in orders:
            if order.side != OrderSide.BUY:
                accepted.append(order)
                continue
            account_key = order.account_id or "default"
            reserved = reserved_by_account.get(account_key, 0.0)
            proceeds = proceeds_by_account.get(account_key, 0.0)
            try:
                capacity = self.lookup(order)
            except Exception as exc:
                blocked.append(
                    OrderCapacityBlock(
                        order=order,
                        requested_quantity=order.quantity,
                        requested_notional=order.notional,
                        reason="broker_capacity_unavailable",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        checked_at=utc_now().isoformat(),
                    )
                )
                continue

            available_cash = max(0.0, capacity.cash_buying_power + proceeds - reserved)
            cash_quantity = available_cash / order.price if order.price > 0 else 0.0
            available_quantity = (
                cash_quantity
                if capacity.max_buy_quantity is None
                else min(capacity.max_buy_quantity, cash_quantity)
            )
            reason = None
            if order.notional > available_cash + 1e-9:
                reason = "cash_buying_power_exceeded"
            if (
                reason is None
                and capacity.max_buy_quantity is not None
                and order.quantity > capacity.max_buy_quantity + 1e-9
            ):
                reason = "max_buy_quantity_exceeded"
            if reason is not None:
                blocked.append(
                    OrderCapacityBlock(
                        order=order,
                        requested_quantity=order.quantity,
                        requested_notional=order.notional,
                        cash_buying_power=available_cash,
                        max_buy_quantity=available_quantity,
                        reason=reason,
                        checked_at=utc_now().isoformat(),
                    )
                )
                continue

            if proceeds > 0:
                order = order.model_copy(
                    update={"metadata": {**order.metadata, "sell_fill_pending": True}}
                )
            accepted.append(order)
            reserved_by_account[account_key] = reserved + order.notional
        return accepted, blocked


__all__ = ["OrderCapacityBlock", "OrderCapacityService"]

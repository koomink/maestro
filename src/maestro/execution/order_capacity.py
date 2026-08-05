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


def _capacity_scope(order: OrderIntent) -> tuple[str, str | None]:
    """Account and currency: the boundary broker cash is actually fungible across."""
    currency = order.currency.value if order.currency is not None else order.sleeve
    return (order.account_id or "default", currency)


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
        reserved_by_scope: dict[tuple[str, str | None], float] = {}
        # A rotation's buy is funded by the sell filed alongside it, so a fully
        # invested account reports zero buying power — and zero max buy quantity —
        # right up until that sell settles. Every dimension measured here is a
        # pre-fill reading, so none of them can judge such a buy. Defer the whole
        # check to the post-sell phase, which re-runs this partition against the
        # broker's own settled balance.
        #
        # Cash is fungible per account AND currency: a KRW sell cannot fund a USD
        # buy, and their notionals are not even comparable numbers.
        proceeds_by_scope: dict[tuple[str, str | None], float] = {}
        for order in orders:
            if order.side == OrderSide.SELL:
                sell_scope = _capacity_scope(order)
                proceeds_by_scope[sell_scope] = (
                    proceeds_by_scope.get(sell_scope, 0.0) + order.notional
                )
        for order in orders:
            if order.side != OrderSide.BUY:
                accepted.append(order)
                continue
            scope = _capacity_scope(order)
            reserved = reserved_by_scope.get(scope, 0.0)
            proceeds = proceeds_by_scope.get(scope, 0.0)
            if proceeds > 0:
                accepted.append(
                    order.model_copy(
                        update={"metadata": {**order.metadata, "sell_fill_pending": True}}
                    )
                )
                reserved_by_scope[scope] = reserved + order.notional
                continue
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

            available_cash = max(0.0, capacity.cash_buying_power - reserved)
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

            accepted.append(order)
            reserved_by_scope[scope] = reserved + order.notional
        return accepted, blocked


__all__ = ["OrderCapacityBlock", "OrderCapacityService"]

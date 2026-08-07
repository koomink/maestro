from collections.abc import Callable

from pydantic import BaseModel

from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide
from maestro.execution.base import OrderIntent
from maestro.execution.brokers.readonly import (
    BrokerBuyingPower,
    BuyingPowerCurrencyUnavailable,
)
from maestro.execution.order_builder import QUOTED_QUANTITY_TOLERANCE, floor_to_step


class OrderCapacityBlock(BaseModel):
    order: OrderIntent
    requested_quantity: float
    requested_notional: float
    cash_buying_power: float | None = None
    # Which currency's balance the figures above were actually read from, so a
    # blocked-order report can be checked against the account snapshot. Null
    # when no figure was obtained — the currency asked for is `requested_currency`
    # and what the broker did report is `available_currencies`.
    capacity_currency: str | None = None
    requested_currency: str | None = None
    available_currencies: list[str] | None = None
    max_buy_quantity: float | None = None
    reason: str
    error_type: str | None = None
    error_message: str | None = None
    checked_at: str


def _capacity_scope(order: OrderIntent) -> tuple[str, str | None, tuple[str, ...]]:
    """The unit that will actually execute together.

    Cash is fungible per account and currency, but orders are then split into
    approval groups by `source_strategy_ids` and each group runs its own
    sell-then-buy phases. A buy whose funding sell lands in a different group is
    executed as a buy-only cohort — no resize, no post-sell capacity ruling — so
    the deferral decision has to be made at the same granularity or it hands that
    buy through unchecked.
    """
    currency = order.currency.value if order.currency is not None else order.sleeve
    source_strategy_ids = order.metadata.get("source_strategy_ids") or []
    group = tuple(str(strategy_id) for strategy_id in source_strategy_ids if strategy_id)
    return (order.account_id or "default", currency, group)


class OrderCapacityService:
    """Partition planned buys using authoritative, account-routed broker capacity."""

    def __init__(
        self,
        lookup: Callable[[OrderIntent], BrokerBuyingPower],
        quantity_step: Callable[[OrderIntent], float] | None = None,
    ) -> None:
        self.lookup = lookup
        # Reported maximums are what the operator retries with, so a figure that
        # is not a whole tradable lot is not an offer they can accept. Callers
        # that know the instrument's step pass it; without one the raw cash
        # quantity stands.
        self.quantity_step = quantity_step

    def partition(
        self,
        orders: list[OrderIntent],
    ) -> tuple[list[OrderIntent], list[OrderCapacityBlock]]:
        accepted: list[OrderIntent] = []
        blocked: list[OrderCapacityBlock] = []
        reserved_by_scope: dict[tuple[str, str | None, tuple[str, ...]], float] = {}
        # A rotation's buy is funded by the sell filed alongside it, so a fully
        # invested account reports zero buying power — and zero max buy quantity —
        # right up until that sell settles. Every dimension measured here is a
        # pre-fill reading, so none of them can judge such a buy. Defer the whole
        # check to the post-sell phase, which re-runs this partition against the
        # broker's own settled balance.
        #
        # Cash is fungible per account AND currency: a KRW sell cannot fund a USD
        # buy, and their notionals are not even comparable numbers.
        proceeds_by_scope: dict[tuple[str, str | None, tuple[str, ...]], float] = {}
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
            except BuyingPowerCurrencyUnavailable as exc:
                # Distinct from an outage: the broker answered, just not about
                # the currency this order spends. Nothing to retry against.
                blocked.append(
                    OrderCapacityBlock(
                        order=order,
                        requested_quantity=order.quantity,
                        requested_notional=order.notional,
                        # No balance was read, so there is no capacity currency
                        # to name — only what was asked for and what existed.
                        requested_currency=exc.currency,
                        available_currencies=list(exc.available_currencies),
                        reason="buying_power_by_currency_unavailable",
                        error_type=type(exc).__name__,
                        error_message=str(exc),
                        checked_at=utc_now().isoformat(),
                    )
                )
                continue
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
            available_quantity = self._tradable_quantity(order, available_quantity)
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
                        capacity_currency=capacity.currency,
                        max_buy_quantity=available_quantity,
                        reason=reason,
                        checked_at=utc_now().isoformat(),
                    )
                )
                continue

            accepted.append(order)
            reserved_by_scope[scope] = reserved + order.notional
        return accepted, blocked

    def _tradable_quantity(self, order: OrderIntent, quantity: float) -> float:
        """Round a reported maximum down to something the broker would accept.

        The block/allow ruling above stays on the raw cash figure; this only
        shapes the number the operator is offered, so a partial share of a
        whole-lot instrument is not presented as a retry.
        """
        if self.quantity_step is None:
            return quantity
        return floor_to_step(
            max(0.0, quantity),
            self.quantity_step(order),
            tolerance=QUOTED_QUANTITY_TOLERANCE,
        )


__all__ = ["OrderCapacityBlock", "OrderCapacityService"]

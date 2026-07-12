from datetime import datetime

from maestro.config.execution import ExecutionConfig
from maestro.core.enums import OrderSide, OrderStatus
from maestro.core.symbols import is_cash_symbol
from maestro.execution.base import ExecutionResult, OrderIntent
from maestro.execution.order_builder import OrderBuilder
from maestro.portfolio.manager import PortfolioTarget
from maestro.state.models import PortfolioState


class PaperExecutionEngine:
    def __init__(
        self,
        *,
        config: ExecutionConfig | None = None,
        instruments=None,
        currency_sleeves=None,
    ) -> None:
        self.order_builder = OrderBuilder(
            config=config,
            instruments=instruments,
            currency_sleeves=currency_sleeves,
        )

    def propose_orders(
        self,
        current_state: PortfolioState,
        target: PortfolioTarget,
        prices: dict[str, float],
        *,
        as_of: datetime | None = None,
        contribution_already_executed: bool = False,
    ) -> list[OrderIntent]:
        return self.order_builder.build_orders(
            current_state,
            target,
            prices,
            as_of=as_of,
            contribution_already_executed=contribution_already_executed,
        )

    def contribution_month_key(self, as_of: datetime | None = None) -> str:
        return self.order_builder.contribution_month_key(as_of)

    def contribution_is_due(self, as_of: datetime | None = None) -> bool:
        return self.order_builder.contribution_is_due(as_of)

    def contribution_available_cash(self, current_state: PortfolioState) -> float:
        return self.order_builder.contribution_available_cash(current_state)

    def execute_orders(
        self,
        current_state: PortfolioState,
        orders: list[OrderIntent],
    ) -> tuple[list[ExecutionResult], PortfolioState]:
        next_state = current_state.model_copy(deep=True)
        for order in orders:
            signed_notional = order.notional if order.side == OrderSide.BUY else -order.notional
            signed_quantity = order.quantity if order.side == OrderSide.BUY else -order.quantity
            if order.currency and next_state.cash_by_currency:
                currency = order.currency.value
                next_state.cash_by_currency[currency] = (
                    next_state.cash_by_currency.get(currency, 0.0) - signed_notional
                )
            else:
                next_state.cash -= signed_notional
            next_state.positions[order.symbol] = (
                next_state.positions.get(order.symbol, 0.0) + signed_quantity
            )
            if is_cash_symbol(order.symbol):
                next_state.positions.pop(order.symbol, None)
        if next_state.cash_by_currency:
            next_state.cash = sum(next_state.cash_by_currency.values())
        results = [
            ExecutionResult(
                order_id=order.order_id,
                status=OrderStatus.FILLED,
                filled_quantity=order.quantity,
                fill_price=order.price,
                filled_notional=order.notional,
            )
            for order in orders
        ]
        return results, next_state

    def execute(
        self,
        current_state: PortfolioState,
        target: PortfolioTarget,
        prices: dict[str, float],
    ) -> tuple[list[OrderIntent], list[ExecutionResult], PortfolioState]:
        orders = self.propose_orders(current_state, target, prices)
        results, next_state = self.execute_orders(current_state, orders)
        return orders, results, next_state

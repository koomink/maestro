from maestro.core.enums import OrderSide
from maestro.core.ids import new_run_id
from maestro.execution.base import OrderIntent
from maestro.portfolio.manager import PortfolioTarget
from maestro.state.models import PortfolioState


class OrderBuilder:
    def build_orders(
        self,
        current_state: PortfolioState,
        target: PortfolioTarget,
        prices: dict[str, float],
    ) -> list[OrderIntent]:
        orders: list[OrderIntent] = []
        total_value = current_state.total_value(prices)
        for symbol, target_weight in target.allocations.items():
            if symbol == "CASH":
                continue
            current_qty = current_state.positions.get(symbol, 0.0)
            current_value = current_qty * prices[symbol]
            target_value = total_value * target_weight
            delta_value = target_value - current_value
            if abs(delta_value) < 0.01:
                continue
            side = OrderSide.BUY if delta_value > 0 else OrderSide.SELL
            orders.append(
                OrderIntent(
                    order_id=new_run_id(),
                    symbol=symbol,
                    side=side,
                    quantity=abs(delta_value) / prices[symbol],
                    price=prices[symbol],
                    notional=abs(delta_value),
                )
            )
        return orders

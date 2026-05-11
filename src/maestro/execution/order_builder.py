from maestro.core.enums import OrderSide
from maestro.core.exceptions import MissingPriceError
from maestro.core.ids import new_order_id
from maestro.core.instruments import TradableInstrument
from maestro.core.symbols import is_cash_symbol
from maestro.execution.base import OrderIntent
from maestro.portfolio.manager import PortfolioTarget
from maestro.state.models import PortfolioState


class OrderBuilder:
    def __init__(
        self,
        *,
        instruments: list[TradableInstrument] | None = None,
        currency_sleeves: dict | None = None,
    ) -> None:
        self.instruments = {instrument.symbol: instrument for instrument in instruments or []}
        self.currency_sleeves = currency_sleeves or {}

    def build_orders(
        self,
        current_state: PortfolioState,
        target: PortfolioTarget,
        prices: dict[str, float],
    ) -> list[OrderIntent]:
        if target.allocation_sleeves:
            return self._build_sleeve_orders(current_state, target, prices)
        orders: list[OrderIntent] = []
        total_value = current_state.total_value(prices)
        for symbol, target_weight in target.allocations.items():
            if is_cash_symbol(symbol):
                continue
            if symbol not in prices:
                raise MissingPriceError(f"Missing prices for symbols: {symbol}")
            current_qty = current_state.positions.get(symbol, 0.0)
            current_value = current_qty * prices[symbol]
            target_value = total_value * target_weight
            delta_value = target_value - current_value
            if abs(delta_value) < 0.01:
                continue
            side = OrderSide.BUY if delta_value > 0 else OrderSide.SELL
            orders.append(
                OrderIntent(
                    order_id=new_order_id(),
                    symbol=symbol,
                    side=side,
                    quantity=abs(delta_value) / prices[symbol],
                    price=prices[symbol],
                    notional=abs(delta_value),
                    currency=self.instruments.get(symbol).currency
                    if symbol in self.instruments
                    else None,
                    broker_product=self.instruments.get(symbol).broker_product
                    if symbol in self.instruments
                    else None,
                )
            )
        return orders

    def _build_sleeve_orders(
        self,
        current_state: PortfolioState,
        target: PortfolioTarget,
        prices: dict[str, float],
    ) -> list[OrderIntent]:
        orders: list[OrderIntent] = []
        for sleeve_name, allocations in target.allocation_sleeves.items():
            sleeve_config = self.currency_sleeves.get(sleeve_name)
            if sleeve_config is None:
                raise ValueError(f"Unknown allocation sleeve: {sleeve_name}")
            if hasattr(sleeve_config, "symbols"):
                symbols = list(sleeve_config.symbols)
                cash_symbol = sleeve_config.cash_symbol
            else:
                symbols = list(sleeve_config.get("symbols", []))
                cash_symbol = sleeve_config.get("cash_symbol", f"CASH_{sleeve_name}")
            total_value = current_state.sleeve_value(
                symbols=symbols,
                cash_symbol=cash_symbol,
                currency=sleeve_name,
                prices=prices,
            )
            for symbol, target_weight in allocations.items():
                if is_cash_symbol(symbol):
                    continue
                if symbol not in prices:
                    raise MissingPriceError(f"Missing prices for symbols: {symbol}")
                current_qty = current_state.positions.get(symbol, 0.0)
                current_value = current_qty * prices[symbol]
                target_value = total_value * target_weight
                delta_value = target_value - current_value
                if abs(delta_value) < 0.01:
                    continue
                side = OrderSide.BUY if delta_value > 0 else OrderSide.SELL
                instrument = self.instruments.get(symbol)
                quantity = self._order_quantity(symbol, abs(delta_value) / prices[symbol])
                if quantity <= 0:
                    continue
                notional = quantity * prices[symbol]
                orders.append(
                    OrderIntent(
                        order_id=new_order_id(),
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                        price=prices[symbol],
                        notional=notional,
                        currency=instrument.currency if instrument else None,
                        sleeve=sleeve_name,
                        broker_product=instrument.broker_product if instrument else None,
                    )
                )
        return orders

    def _order_quantity(self, symbol: str, raw_quantity: float) -> float:
        instrument = self.instruments.get(symbol)
        if instrument is None:
            return raw_quantity
        steps = int(raw_quantity / instrument.quantity_step)
        return steps * instrument.quantity_step

import calendar
import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from maestro.config.execution import ExecutionConfig
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
        config: ExecutionConfig | None = None,
        instruments: list[TradableInstrument] | None = None,
        currency_sleeves: dict | None = None,
    ) -> None:
        self.config = config or ExecutionConfig()
        self.instruments = {instrument.symbol: instrument for instrument in instruments or []}
        self.currency_sleeves = currency_sleeves or {}

    def build_orders(
        self,
        current_state: PortfolioState,
        target: PortfolioTarget,
        prices: dict[str, float],
        *,
        as_of: datetime | None = None,
        contribution_already_executed: bool = False,
    ) -> list[OrderIntent]:
        if self.config.order_generation_mode == "buy_only_contribution":
            return self._build_buy_only_contribution_orders(
                current_state,
                target,
                prices,
                as_of=as_of,
                contribution_already_executed=contribution_already_executed,
            )
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

    def _build_buy_only_contribution_orders(
        self,
        current_state: PortfolioState,
        target: PortfolioTarget,
        prices: dict[str, float],
        *,
        as_of: datetime | None,
        contribution_already_executed: bool,
    ) -> list[OrderIntent]:
        contribution = self.config.contribution
        if not contribution.enabled or contribution_already_executed:
            return []
        if not self._is_contribution_due(as_of):
            return []

        allocations = self._contribution_allocations(target)
        target_symbols = [symbol for symbol in allocations if not is_cash_symbol(symbol)]
        for symbol in target_symbols:
            if symbol not in prices:
                raise MissingPriceError(f"Missing prices for symbols: {symbol}")
        if not target_symbols:
            return []

        spend = self._contribution_spend(current_state)
        if spend <= 0 or spend < contribution.min_monthly_budget:
            return []

        budget_by_symbol = self._contribution_budget_by_symbol(
            current_state,
            allocations,
            target_symbols,
            prices,
            spend,
        )
        month_key = self.contribution_month_key(as_of)
        orders: list[OrderIntent] = []
        for symbol in target_symbols:
            budget = budget_by_symbol.get(symbol, 0.0)
            if budget <= 0:
                continue
            order_price = self._order_price(symbol, prices[symbol])
            if order_price <= 0:
                continue
            quantity = self._order_quantity(symbol, budget / order_price)
            instrument = self.instruments.get(symbol)
            min_quantity = instrument.min_order_quantity if instrument else 0.0
            notional = quantity * order_price
            min_notional = instrument.min_order_notional if instrument else 0.0
            if quantity <= 0 or quantity < min_quantity or notional < min_notional:
                continue
            orders.append(
                OrderIntent(
                    order_id=new_order_id(),
                    symbol=symbol,
                    side=OrderSide.BUY,
                    quantity=quantity,
                    price=order_price,
                    notional=notional,
                    currency=instrument.currency if instrument else contribution.currency,
                    sleeve=contribution.sleeve,
                    broker_product=instrument.broker_product if instrument else None,
                    metadata={
                        "order_generation_mode": "buy_only_contribution",
                        "contribution_month": month_key,
                        "contribution_sleeve": contribution.sleeve,
                    },
                )
            )
        return orders

    def _contribution_allocations(self, target: PortfolioTarget) -> dict[str, float]:
        contribution = self.config.contribution
        if target.allocation_sleeves:
            allocations = target.allocation_sleeves.get(contribution.sleeve)
            if allocations is None:
                raise ValueError(f"Unknown contribution sleeve: {contribution.sleeve}")
            return self._normalize_allocations(allocations)
        return self._normalize_allocations(target.allocations)

    def _normalize_allocations(self, allocations: dict[str, float]) -> dict[str, float]:
        investable = {
            symbol: weight
            for symbol, weight in allocations.items()
            if not is_cash_symbol(symbol) and weight > 0
        }
        total = sum(investable.values())
        if total <= 0:
            return {}
        return {symbol: weight / total for symbol, weight in investable.items()}

    def _contribution_spend(self, current_state: PortfolioState) -> float:
        contribution = self.config.contribution
        currency = contribution.currency.value
        cash = (
            current_state.cash_by_currency.get(currency, 0.0)
            if current_state.cash_by_currency
            else current_state.cash
        )
        available = cash * max(0.0, 1.0 - self.config.live_order_fee_buffer_pct)
        return min(contribution.monthly_budget, available)

    def _contribution_budget_by_symbol(
        self,
        current_state: PortfolioState,
        allocations: dict[str, float],
        target_symbols: list[str],
        prices: dict[str, float],
        spend: float,
    ) -> dict[str, float]:
        current_values = {
            symbol: current_state.positions.get(symbol, 0.0) * prices[symbol]
            for symbol in target_symbols
        }
        current_total = sum(current_values.values())
        if current_total <= 0:
            return {symbol: spend * allocations[symbol] for symbol in target_symbols}

        underweight_symbols = [
            symbol
            for symbol in target_symbols
            if current_values[symbol] / current_total < allocations[symbol] - 1e-9
        ]
        if not underweight_symbols:
            return {symbol: spend * allocations[symbol] for symbol in target_symbols}

        shortfalls = {
            symbol: max(0.0, allocations[symbol] * current_total - current_values[symbol])
            for symbol in underweight_symbols
        }
        shortfall_total = sum(shortfalls.values())
        if shortfall_total <= 0:
            weight_total = sum(allocations[symbol] for symbol in underweight_symbols)
            return {
                symbol: spend * allocations[symbol] / weight_total for symbol in underweight_symbols
            }
        return {
            symbol: spend * shortfalls[symbol] / shortfall_total for symbol in underweight_symbols
        }

    def _order_price(self, symbol: str, raw_price: float) -> float:
        instrument = self.instruments.get(symbol)
        if instrument is None:
            return raw_price
        ticks = math.floor(raw_price / instrument.price_tick)
        return ticks * instrument.price_tick

    def _is_contribution_due(self, as_of: datetime | None) -> bool:
        local_date = self._local_date(as_of)
        effective_date = self._effective_contribution_date(local_date.year, local_date.month)
        return local_date >= effective_date and self._is_trading_day(local_date)

    def contribution_month_key(self, as_of: datetime | None = None) -> str:
        local_date = self._local_date(as_of)
        return f"{local_date.year:04d}-{local_date.month:02d}"

    def _local_date(self, as_of: datetime | None) -> date:
        timestamp = as_of or datetime.now(ZoneInfo(self.config.market_session_timezone))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=ZoneInfo(self.config.market_session_timezone))
        return timestamp.astimezone(ZoneInfo(self.config.market_session_timezone)).date()

    def _effective_contribution_date(self, year: int, month: int) -> date:
        _, last_day = calendar.monthrange(year, month)
        candidate = date(year, month, min(self.config.contribution.buy_day, last_day))
        while not self._is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def _is_trading_day(self, candidate: date) -> bool:
        return (
            candidate.weekday() in self.config.market_session_weekdays
            and candidate.isoformat() not in self.config.market_session_holidays
        )

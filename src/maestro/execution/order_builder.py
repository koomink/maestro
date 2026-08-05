import calendar
from datetime import date, datetime, timedelta
from decimal import Decimal
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


def round_price_to_tick(raw_price: float, instrument: TradableInstrument | None) -> float:
    """Snap a price down to the instrument's tick.

    Brokers reject limit prices that are not tick multiples, and market quotes are
    not guaranteed to be: a USD ETF quote can carry four decimals against a 0.01
    tick. Anything that sets an order price must pass through here, including code
    that substitutes a broker quote after the order was built.
    """
    if instrument is None:
        return raw_price
    ticks = int(Decimal(str(raw_price)) / Decimal(str(instrument.price_tick)))
    return float(Decimal(ticks) * Decimal(str(instrument.price_tick)))


def floor_quantity_to_step(raw_quantity: float, instrument: TradableInstrument | None) -> float:
    """Round a quantity down to the instrument's tradable step.

    Rounding down rather than to nearest keeps an order inside whatever budget
    sized it: a rounded-up share is one the account may not be able to pay for.
    """
    if instrument is None:
        return raw_quantity
    steps = int(Decimal(str(raw_quantity)) / Decimal(str(instrument.quantity_step)))
    return float(Decimal(steps) * Decimal(str(instrument.quantity_step)))


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
        contribution_override: bool = False,
    ) -> list[OrderIntent]:
        if self.config.order_generation_mode == "buy_only_contribution":
            return self._build_buy_only_contribution_orders(
                current_state,
                target,
                prices,
                as_of=as_of,
                contribution_already_executed=contribution_already_executed,
                contribution_override=contribution_override,
            )
        if target.allocation_sleeves:
            return self._build_sleeve_orders(current_state, target, prices)
        orders: list[OrderIntent] = []
        total_value = current_state.total_value(prices)
        rebalance_symbols = list(
            dict.fromkeys(
                [
                    *target.allocations.keys(),
                    *self._exitable_positions(current_state),
                ]
            )
        )
        for symbol in rebalance_symbols:
            if is_cash_symbol(symbol):
                continue
            if symbol not in prices:
                raise MissingPriceError(f"Missing prices for symbols: {symbol}")
            current_qty = current_state.positions.get(symbol, 0.0)
            current_value = current_qty * prices[symbol]
            target_value = total_value * target.allocations.get(symbol, 0.0)
            delta_value = target_value - current_value
            if abs(delta_value) < 0.01:
                continue
            side = OrderSide.BUY if delta_value > 0 else OrderSide.SELL
            if self.config.order_posture == "disabled":
                instrument = self.instruments.get(symbol)
                orders.append(
                    OrderIntent(
                        order_id=new_order_id(),
                        symbol=symbol,
                        side=side,
                        quantity=abs(delta_value) / prices[symbol],
                        price=prices[symbol],
                        notional=abs(delta_value),
                        order_type=self.config.allowed_order_type,
                        currency=instrument.currency if instrument else None,
                        broker_product=instrument.broker_product if instrument else None,
                    )
                )
                continue
            order_price = self._order_price(symbol, prices[symbol])
            if order_price <= 0:
                continue
            quantity = self._order_quantity(symbol, abs(delta_value) / order_price)
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
                    side=side,
                    quantity=quantity,
                    price=order_price,
                    notional=notional,
                    order_type=self.config.allowed_order_type,
                    currency=instrument.currency if instrument else None,
                    broker_product=instrument.broker_product if instrument else None,
                )
            )
        return self._scale_buy_orders_to_cash(
            orders,
            self._cash_available(current_state),
        )

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
            rebalance_symbols = list(
                dict.fromkeys(
                    [
                        *allocations.keys(),
                        *(symbol for symbol in symbols if current_state.positions.get(symbol, 0.0)),
                    ]
                )
            )
            sleeve_orders: list[OrderIntent] = []
            for symbol in rebalance_symbols:
                if is_cash_symbol(symbol):
                    continue
                if symbol not in prices:
                    raise MissingPriceError(f"Missing prices for symbols: {symbol}")
                current_qty = current_state.positions.get(symbol, 0.0)
                current_value = current_qty * prices[symbol]
                target_value = total_value * allocations.get(symbol, 0.0)
                delta_value = target_value - current_value
                if abs(delta_value) < 0.01:
                    continue
                side = OrderSide.BUY if delta_value > 0 else OrderSide.SELL
                instrument = self.instruments.get(symbol)
                order_price = self._order_price(symbol, prices[symbol])
                if order_price <= 0:
                    continue
                quantity = self._order_quantity(symbol, abs(delta_value) / order_price)
                min_quantity = instrument.min_order_quantity if instrument else 0.0
                notional = quantity * order_price
                min_notional = instrument.min_order_notional if instrument else 0.0
                if quantity <= 0 or quantity < min_quantity or notional < min_notional:
                    continue
                sleeve_orders.append(
                    OrderIntent(
                        order_id=new_order_id(),
                        symbol=symbol,
                        side=side,
                        quantity=quantity,
                        price=order_price,
                        notional=notional,
                        order_type=self.config.allowed_order_type,
                        currency=instrument.currency if instrument else None,
                        sleeve=sleeve_name,
                        broker_product=instrument.broker_product if instrument else None,
                    )
                )
            orders.extend(
                self._scale_buy_orders_to_cash(
                    sleeve_orders,
                    self._cash_available(current_state, sleeve_name),
                )
            )
        return orders

    def _cash_available(
        self,
        current_state: PortfolioState,
        currency_sleeve: str | None = None,
    ) -> float:
        if current_state.cash_by_currency:
            if currency_sleeve is not None:
                cash = current_state.cash_by_currency.get(currency_sleeve, 0.0)
            else:
                cash = sum(current_state.cash_by_currency.values())
        else:
            cash = current_state.cash
        fee_multiplier = max(0.0, 1.0 - self.config.live_order_limits.fee_buffer_pct)
        return max(0.0, cash) * fee_multiplier

    def _exitable_positions(self, current_state: PortfolioState) -> list[str]:
        """Held symbols this builder may sell down when the target drops them.

        A rotation has to exit last month's winner, so holdings outside today's
        target belong in the rebalance set. It stops at the instrument universe:
        `portfolio.unknown_broker_position_policy=include_readonly` parks foreign
        broker holdings in the same state so they can be valued, and those are
        observed, not traded. An unconfigured builder has no universe to consult
        and treats every holding as its own.
        """
        return [
            symbol
            for symbol, quantity in current_state.positions.items()
            if quantity and (not self.instruments or symbol in self.instruments)
        ]

    def _sell_proceeds(self, orders: list[OrderIntent]) -> float:
        """Cash the sells in this batch release, net of the fee buffer.

        A fully invested rotation funds its buys entirely from the sells filed
        alongside them, so sizing buys against the pre-rebalance cash balance
        alone scales them to zero and leaves the book sitting in cash for a whole
        cycle. Callers pass one sleeve's orders at a time, which keeps proceeds
        from crossing a currency boundary.
        """
        proceeds = sum(order.notional for order in orders if order.side == OrderSide.SELL)
        fee_multiplier = max(0.0, 1.0 - self.config.live_order_limits.fee_buffer_pct)
        return proceeds * fee_multiplier

    def _scale_buy_orders_to_cash(
        self,
        orders: list[OrderIntent],
        cash_available: float,
    ) -> list[OrderIntent]:
        cash_available += self._sell_proceeds(orders)
        buy_notional = sum(order.notional for order in orders if order.side == OrderSide.BUY)
        if buy_notional <= cash_available + 1e-9:
            return orders
        if buy_notional <= 0:
            return orders

        scale = max(0.0, cash_available) / buy_notional
        scaled_orders: list[OrderIntent] = []
        scaled_buy_orders: list[OrderIntent] = []
        for order in orders:
            if order.side != OrderSide.BUY:
                scaled_orders.append(order)
                continue
            scaled = self._scaled_buy_order(order, scale)
            if scaled is None:
                continue
            scaled_orders.append(scaled)
            scaled_buy_orders.append(scaled)

        buy_notional_after = sum(order.notional for order in scaled_buy_orders)
        metadata = {
            "cash_scaled": True,
            "cash_available": cash_available,
            "buy_notional_before_scaling": buy_notional,
            "buy_notional_after_scaling": buy_notional_after,
            "fee_buffer_pct": self.config.live_order_limits.fee_buffer_pct,
        }
        return [
            order.model_copy(update={"metadata": {**order.metadata, **metadata}})
            if order.side == OrderSide.BUY
            else order
            for order in scaled_orders
        ]

    def _scaled_buy_order(self, order: OrderIntent, scale: float) -> OrderIntent | None:
        if order.price <= 0:
            return None
        quantity = self._order_quantity(symbol=order.symbol, raw_quantity=order.quantity * scale)
        instrument = self.instruments.get(order.symbol)
        min_quantity = instrument.min_order_quantity if instrument else 0.0
        notional = quantity * order.price
        min_notional = instrument.min_order_notional if instrument else 0.0
        if quantity <= 0 or quantity < min_quantity or notional < min_notional:
            return None
        return order.model_copy(update={"quantity": quantity, "notional": notional})

    def _order_quantity(self, symbol: str, raw_quantity: float) -> float:
        return floor_quantity_to_step(raw_quantity, self.instruments.get(symbol))

    def _build_buy_only_contribution_orders(
        self,
        current_state: PortfolioState,
        target: PortfolioTarget,
        prices: dict[str, float],
        *,
        as_of: datetime | None,
        contribution_already_executed: bool,
        contribution_override: bool = False,
    ) -> list[OrderIntent]:
        contribution = self.config.contribution
        if not contribution.enabled or contribution_already_executed:
            return []
        if not contribution_override and not self._is_contribution_due(as_of):
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
            min_notional = instrument.min_order_notional if instrument else 0.0
            quantities = self._split_contribution_quantity(symbol, quantity, order_price)
            quantities = [
                chunk_quantity
                for chunk_quantity in quantities
                if chunk_quantity >= min_quantity
                and chunk_quantity * order_price >= min_notional
            ]
            if not quantities:
                continue
            for chunk_index, chunk_quantity in enumerate(quantities, start=1):
                metadata = {
                    "order_generation_mode": "buy_only_contribution",
                    "contribution_month": month_key,
                    "contribution_sleeve": contribution.sleeve,
                }
                if len(quantities) > 1:
                    metadata.update(
                        {
                            "contribution_chunk_index": chunk_index,
                            "contribution_chunk_count": len(quantities),
                        }
                    )
                orders.append(
                    OrderIntent(
                        order_id=new_order_id(),
                        symbol=symbol,
                        side=OrderSide.BUY,
                        quantity=chunk_quantity,
                        price=order_price,
                        notional=chunk_quantity * order_price,
                        order_type=self.config.allowed_order_type,
                        currency=instrument.currency if instrument else contribution.currency,
                        sleeve=contribution.sleeve,
                        broker_product=instrument.broker_product if instrument else None,
                        metadata=metadata,
                    )
                )
        return orders

    def _split_contribution_quantity(
        self,
        symbol: str,
        quantity: float,
        order_price: float,
    ) -> list[float]:
        if quantity <= 0:
            return []
        instrument = self.instruments.get(symbol)
        currency = instrument.currency if instrument else self.config.contribution.currency
        max_notional = self.config.live_order_limits.max_order_notional_for(currency)
        if max_notional is None or max_notional <= 0 or quantity * order_price <= max_notional:
            return [quantity]

        max_chunk_quantity = self._order_quantity(symbol, max_notional / order_price)
        if max_chunk_quantity <= 0:
            return [quantity]
        chunks: list[float] = []
        remaining = quantity
        while remaining > 0:
            chunk = min(remaining, max_chunk_quantity)
            chunk = self._order_quantity(symbol, chunk)
            if chunk <= 0:
                break
            chunks.append(chunk)
            remaining = self._order_quantity(symbol, remaining - chunk)
        return chunks

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
        available = cash * max(0.0, 1.0 - self.config.live_order_limits.fee_buffer_pct)
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
        return round_price_to_tick(raw_price, self.instruments.get(symbol))

    def _is_contribution_due(self, as_of: datetime | None) -> bool:
        local_date = self._local_date(as_of)
        effective_date = self._effective_contribution_date(local_date.year, local_date.month)
        return local_date >= effective_date and self._is_trading_day(local_date)

    def contribution_month_key(self, as_of: datetime | None = None) -> str:
        local_date = self._local_date(as_of)
        return f"{local_date.year:04d}-{local_date.month:02d}"

    def contribution_is_due(self, as_of: datetime | None = None) -> bool:
        return self._is_contribution_due(as_of)

    def next_contribution_date(self, as_of: datetime | None = None) -> date:
        local_date = self._local_date(as_of)
        effective = self._effective_contribution_date(local_date.year, local_date.month)
        if local_date <= effective:
            return effective
        candidate = local_date
        while not self._is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def contribution_available_cash(self, current_state: PortfolioState) -> float:
        return self._contribution_spend(current_state)

    def _local_date(self, as_of: datetime | None) -> date:
        market_session = self.config.market_session
        timestamp = as_of or datetime.now(ZoneInfo(market_session.timezone))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=ZoneInfo(market_session.timezone))
        return timestamp.astimezone(ZoneInfo(market_session.timezone)).date()

    def _effective_contribution_date(self, year: int, month: int) -> date:
        _, last_day = calendar.monthrange(year, month)
        candidate = date(year, month, min(self.config.contribution.buy_day, last_day))
        while not self._is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def _is_trading_day(self, candidate: date) -> bool:
        return (
            candidate.weekday() in self.config.market_session.weekdays
            and candidate.isoformat() not in self.config.market_session.holidays
        )

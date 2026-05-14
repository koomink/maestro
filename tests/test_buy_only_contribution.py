from datetime import datetime
from zoneinfo import ZoneInfo

from maestro.config.execution import ExecutionConfig
from maestro.core.enums import OrderSide
from maestro.core.instruments import TradableInstrument
from maestro.execution.order_builder import OrderBuilder
from maestro.execution.paper import PaperExecutionEngine
from maestro.portfolio.manager import PortfolioTarget
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore

LEV = "TIGER_NASDAQ100_LEVERAGE"
DIV = "KODEX_US_DIVIDEND_DOWJONES"


def test_buy_only_contribution_never_emits_sells_when_asset_is_overweight():
    builder = _builder()
    state = PortfolioState(
        cash=3_000_000, cash_by_currency={"KRW": 3_000_000}, positions={LEV: 100}
    )
    target = _target()

    orders = builder.build_orders(
        state,
        target,
        prices={LEV: 100_000.0, DIV: 10_000.0},
        as_of=_dt(2026, 5, 15),
    )

    assert orders
    assert {order.side for order in orders} == {OrderSide.BUY}
    assert {order.symbol for order in orders} == {DIV}


def test_underweight_asset_receives_more_of_monthly_budget():
    builder = _builder()
    state = PortfolioState(
        cash=3_000_000,
        cash_by_currency={"KRW": 3_000_000},
        positions={LEV: 10, DIV: 100},
    )

    orders = builder.build_orders(
        state,
        _target(),
        prices={LEV: 100_000.0, DIV: 10_000.0},
        as_of=_dt(2026, 5, 15),
    )

    assert len(orders) == 1
    assert orders[0].symbol == LEV
    assert orders[0].notional == 3_000_000


def test_equal_weight_case_splits_contribution_by_target_weights():
    builder = _builder()
    state = PortfolioState(
        cash=3_000_000,
        cash_by_currency={"KRW": 3_000_000},
        positions={LEV: 18, DIV: 120},
    )

    orders = builder.build_orders(
        state,
        _target(),
        prices={LEV: 100_000.0, DIV: 10_000.0},
        as_of=_dt(2026, 5, 15),
    )

    notionals = {order.symbol: order.notional for order in orders}
    assert notionals == {LEV: 1_800_000.0, DIV: 1_200_000.0}


def test_share_and_tick_rounding_leave_residual_cash():
    builder = _builder(monthly_budget=1_000, min_monthly_budget=1)
    state = PortfolioState(cash=1_000, cash_by_currency={"KRW": 1_000}, positions={})

    orders = builder.build_orders(
        state,
        _target(),
        prices={LEV: 333.9, DIV: 200.4},
        as_of=_dt(2026, 5, 15),
    )

    assert {order.symbol: order.price for order in orders} == {LEV: 333, DIV: 200}
    assert sum(order.notional for order in orders) == 733
    assert sum(order.notional for order in orders) < 1_000


def test_non_trading_buy_day_executes_on_next_business_day():
    builder = _builder(buy_day=15)
    state = PortfolioState(cash=3_000_000, cash_by_currency={"KRW": 3_000_000}, positions={})

    sunday_orders = builder.build_orders(
        state,
        _target(),
        prices={LEV: 100_000.0, DIV: 10_000.0},
        as_of=_dt(2026, 2, 15),
    )
    monday_orders = builder.build_orders(
        state,
        _target(),
        prices={LEV: 100_000.0, DIV: 10_000.0},
        as_of=_dt(2026, 2, 16),
    )

    assert sunday_orders == []
    assert monday_orders


def test_duplicate_monthly_execution_does_not_create_another_contribution_order(tmp_path):
    config = _config()
    engine = PaperExecutionEngine(config=config, instruments=_instruments())
    state = PortfolioState(cash=3_000_000, cash_by_currency={"KRW": 3_000_000}, positions={})
    as_of = _dt(2026, 5, 15)
    store = StateStore(str(tmp_path / "state.db"), 3_000_000, {"KRW": 3_000_000})

    first_orders = engine.propose_orders(
        state,
        _target(),
        {LEV: 100_000.0, DIV: 10_000.0},
        as_of=as_of,
    )
    for order in first_orders:
        store.save_order("run-1", order.order_id, order.model_dump(mode="json"))
    already_executed = store.monthly_contribution_order_exists(
        engine.contribution_month_key(as_of),
        "KRW",
    )
    second_orders = engine.propose_orders(
        state,
        _target(),
        {LEV: 100_000.0, DIV: 10_000.0},
        as_of=as_of,
        contribution_already_executed=already_executed,
    )

    assert first_orders
    assert already_executed is True
    assert second_orders == []


def _builder(**contribution_overrides) -> OrderBuilder:
    return OrderBuilder(config=_config(**contribution_overrides), instruments=_instruments())


def _config(**contribution_overrides) -> ExecutionConfig:
    contribution = {
        "enabled": True,
        "currency": "KRW",
        "sleeve": "KRW",
        "monthly_budget": 3_000_000,
        "min_monthly_budget": 2_000_000,
        "max_monthly_budget": 4_000_000,
        "buy_day": 15,
        "non_trading_day_policy": "next_trading_day",
        "target_policy": "buy_only_toward_target",
    }
    contribution.update(contribution_overrides)
    return ExecutionConfig(
        engine="paper",
        order_generation_mode="buy_only_contribution",
        market_session_timezone="Asia/Seoul",
        market_session_weekdays=[0, 1, 2, 3, 4],
        contribution=contribution,
    )


def _target() -> PortfolioTarget:
    return PortfolioTarget(
        timestamp=_dt(2026, 5, 15),
        allocations={},
        allocation_sleeves={"KRW": {LEV: 0.60, DIV: 0.40}},
        source_strategy_ids=["ataraxia"],
    )


def _instruments() -> list[TradableInstrument]:
    return [
        TradableInstrument(
            symbol=LEV,
            asset_type="domestic_etf",
            region="KR",
            currency="KRW",
            broker="kis",
            broker_product="kis_domestic_stock",
            broker_symbol="418660",
            exchange_code="KRX",
            quantity_step=1,
            price_tick=1,
            min_order_quantity=1,
            min_order_notional=1,
        ),
        TradableInstrument(
            symbol=DIV,
            asset_type="domestic_etf",
            region="KR",
            currency="KRW",
            broker="kis",
            broker_product="kis_domestic_stock",
            broker_symbol="489250",
            exchange_code="KRX",
            quantity_step=1,
            price_tick=1,
            min_order_quantity=1,
            min_order_notional=1,
        ),
    ]


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 10, 0, tzinfo=ZoneInfo("Asia/Seoul"))

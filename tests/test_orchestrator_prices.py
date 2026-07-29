from datetime import UTC, datetime
from types import SimpleNamespace

from maestro.core.enums import OrderSide
from maestro.core.instruments import TradableInstrument
from maestro.execution.base import OrderIntent
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.sdk import DataBundle


def test_prices_from_bundle_supports_old_and_new_payload_shapes():
    bundle = DataBundle(
        requests=[],
        generated_at=datetime.now(UTC),
        source="test",
        data={
            "OLD": {"price": 10},
            "NEW": {"latest_price": {"price": 20}},
        },
    )
    orchestrator = object.__new__(MaestroOrchestrator)

    assert orchestrator._prices_from_bundle(bundle) == {"OLD": 10.0, "NEW": 20.0}


def test_apply_fx_prices_converts_only_usd_instruments_for_krw_portfolio():
    orchestrator = object.__new__(MaestroOrchestrator)
    orchestrator.config = SimpleNamespace(
        mode="paper",
        portfolio=SimpleNamespace(base_currency="KRW"),
        universe=SimpleNamespace(
            instruments=[
                SimpleNamespace(symbol="SPY", currency="USD"),
                SimpleNamespace(symbol="005930", currency="KRW"),
            ]
        ),
    )
    orchestrator.fx_service = SimpleNamespace(
        refresh_from_config=lambda: SimpleNamespace(rates={"USD/KRW": 1350.0})
    )
    prices = {"SPY": 100.0, "005930": 70000.0, "CASH_KRW": 1.0}

    result = orchestrator._apply_fx_prices("run_test", prices)

    assert result == {"SPY": 135000.0, "005930": 70000.0, "CASH_KRW": 1.0}
    assert prices == {"SPY": 100.0, "005930": 70000.0, "CASH_KRW": 1.0}


def test_apply_native_order_prices_snaps_substituted_broker_quotes_to_the_price_tick():
    """Substituting a broker quote must not undo the order builder's tick rounding.

    Regression: order prices come from _order_generation_prices, which prefers the
    broker's quote, and a USD ETF quote can carry four decimals against a 0.01 tick.
    The raw substitution discarded the rounding and the live execution gate rejected
    the entire signal run on price_tick — that is how the 2026-07-29 US signal run
    failed once the FX fix let the USD sleeve generate buys again.
    """
    orchestrator = object.__new__(MaestroOrchestrator)
    orchestrator.config = SimpleNamespace(
        universe=SimpleNamespace(
            instruments=[
                _instrument("BIL", price_tick=0.01),
                _instrument("SPY", price_tick=0.01),
            ]
        )
    )
    orders = [
        _order("BIL", quantity=3.0),
        _order("SPY", quantity=1.0),
        _order("UNLISTED", quantity=2.0),
    ]

    repriced = orchestrator._apply_native_order_prices(
        orders,
        {"BIL": 91.6378, "SPY": 739.39, "UNLISTED": 12.3456},
    )

    by_symbol = {order.symbol: order for order in repriced}
    assert by_symbol["BIL"].price == 91.63
    assert by_symbol["BIL"].notional == 3.0 * 91.63
    # An already-aligned quote is left exactly as it is.
    assert by_symbol["SPY"].price == 739.39
    # Nothing to snap to without an instrument, so the quote passes through.
    assert by_symbol["UNLISTED"].price == 12.3456


def _instrument(symbol: str, *, price_tick: float) -> TradableInstrument:
    return TradableInstrument(
        symbol=symbol,
        asset_type="etf",
        region="US",
        currency="USD",
        broker="kis",
        broker_product="kis_overseas_stock",
        broker_symbol=symbol,
        exchange_code="NASD",
        quantity_step=1,
        price_tick=price_tick,
        min_order_quantity=1,
        min_order_notional=1,
    )


def _order(symbol: str, *, quantity: float) -> OrderIntent:
    return OrderIntent(
        order_id=f"ord_{symbol.lower()}",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=quantity,
        price=1.0,
        notional=quantity,
    )


def test_apply_fx_prices_leaves_prices_unchanged_for_non_krw_portfolio():
    orchestrator = object.__new__(MaestroOrchestrator)
    orchestrator.config = SimpleNamespace(
        mode="paper",
        portfolio=SimpleNamespace(base_currency="USD"),
        universe=SimpleNamespace(instruments=[SimpleNamespace(symbol="SPY", currency="USD")]),
    )
    orchestrator.fx_service = SimpleNamespace(
        refresh_from_config=lambda: SimpleNamespace(rates={"USD/KRW": 1350.0})
    )
    prices = {"SPY": 100.0}

    assert orchestrator._apply_fx_prices("run_test", prices) == {"SPY": 100.0}


def test_apply_fx_prices_leaves_prices_unchanged_when_fx_refresh_fails():
    orchestrator = object.__new__(MaestroOrchestrator)
    orchestrator.config = SimpleNamespace(
        mode="paper",
        portfolio=SimpleNamespace(base_currency="KRW"),
        universe=SimpleNamespace(instruments=[SimpleNamespace(symbol="SPY", currency="USD")]),
    )

    def fail_refresh():
        raise RuntimeError("FX unavailable")

    orchestrator.fx_service = SimpleNamespace(refresh_from_config=fail_refresh)
    events = []
    orchestrator._record_event = lambda run_id, event_type, payload: events.append(
        (run_id, event_type, payload)
    )
    prices = {"SPY": 100.0}

    assert orchestrator._apply_fx_prices("run_test", prices) == {"SPY": 100.0}
    assert events[0][1] == "fx_conversion_warning"

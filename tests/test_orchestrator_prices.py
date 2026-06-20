from datetime import UTC, datetime
from types import SimpleNamespace

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

    result = orchestrator._apply_fx_prices(prices)

    assert result == {"SPY": 135000.0, "005930": 70000.0, "CASH_KRW": 1.0}


def test_apply_fx_prices_leaves_prices_unchanged_for_non_krw_portfolio():
    orchestrator = object.__new__(MaestroOrchestrator)
    orchestrator.config = SimpleNamespace(
        portfolio=SimpleNamespace(base_currency="USD"),
        universe=SimpleNamespace(
            instruments=[SimpleNamespace(symbol="SPY", currency="USD")]
        ),
    )
    orchestrator.fx_service = SimpleNamespace(
        refresh_from_config=lambda: SimpleNamespace(rates={"USD/KRW": 1350.0})
    )
    prices = {"SPY": 100.0}

    assert orchestrator._apply_fx_prices(prices) == {"SPY": 100.0}


def test_apply_fx_prices_leaves_prices_unchanged_when_fx_refresh_fails():
    orchestrator = object.__new__(MaestroOrchestrator)
    orchestrator.config = SimpleNamespace(
        portfolio=SimpleNamespace(base_currency="KRW"),
        universe=SimpleNamespace(
            instruments=[SimpleNamespace(symbol="SPY", currency="USD")]
        ),
    )

    def fail_refresh():
        raise RuntimeError("FX unavailable")

    orchestrator.fx_service = SimpleNamespace(refresh_from_config=fail_refresh)
    prices = {"SPY": 100.0}

    assert orchestrator._apply_fx_prices(prices) == {"SPY": 100.0}

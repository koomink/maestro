from types import SimpleNamespace

import pytest
import yaml

from maestro.config.loader import load_config
from maestro.core.enums import AssetType, BrokerProduct, Currency, MarketRegion, RunMode
from maestro.core.instruments import TradableInstrument
from maestro.orchestration.orchestrator import MaestroOrchestrator


class FakeFXService:
    def __init__(self, *, rates=None, error: Exception | None = None) -> None:
        self.rates = rates or {}
        self.error = error
        self.calls = 0

    def refresh_from_config(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return SimpleNamespace(rates=self.rates)


def test_live_approval_fx_refresh_exception_halts_and_records_event(tmp_path):
    orchestrator = _orchestrator(tmp_path, mode=RunMode.LIVE_APPROVAL)
    orchestrator.fx_service = FakeFXService(error=RuntimeError("FX unavailable"))

    with pytest.raises(
        RuntimeError,
        match="FX rate unavailable for USD->KRW conversion: refresh_failed",
    ):
        orchestrator._apply_fx_prices("run_fx", {"SPY": 100.0})

    events = orchestrator.state_store.list_system_events_by_type("fx_conversion_halt")
    assert len(events) == 1
    assert events[0]["run_id"] == "run_fx"
    assert events[0]["payload"] == {
        "reason": "refresh_failed",
        "error_type": "RuntimeError",
        "error_message": "FX unavailable",
        "symbols": ["SPY"],
    }


def test_live_approval_missing_usd_krw_rate_halts_and_records_event(tmp_path):
    orchestrator = _orchestrator(tmp_path, mode=RunMode.LIVE_APPROVAL)
    orchestrator.fx_service = FakeFXService(rates={"EUR/KRW": 1500.0})

    with pytest.raises(
        RuntimeError,
        match="FX rate unavailable for USD->KRW conversion: missing_rate",
    ):
        orchestrator._apply_fx_prices("run_fx", {"SPY": 100.0})

    events = orchestrator.state_store.list_system_events_by_type("fx_conversion_halt")
    assert len(events) == 1
    assert events[0]["payload"] == {
        "reason": "missing_rate",
        "error_type": "KeyError",
        "error_message": "USD/KRW",
        "symbols": ["SPY"],
    }


def test_paper_fx_refresh_exception_warns_and_leaves_prices_unchanged(tmp_path):
    orchestrator = _orchestrator(tmp_path, mode=RunMode.PAPER)
    orchestrator.fx_service = FakeFXService(error=RuntimeError("FX unavailable"))
    prices = {"SPY": 100.0}

    assert orchestrator._apply_fx_prices("run_fx", prices) == prices

    events = orchestrator.state_store.list_system_events_by_type("fx_conversion_warning")
    assert len(events) == 1
    assert events[0]["payload"] == {
        "reason": "refresh_failed",
        "error_type": "RuntimeError",
        "error_message": "FX unavailable",
        "symbols": ["SPY"],
    }


def test_no_usd_symbols_skips_fx_refresh(tmp_path):
    orchestrator = _orchestrator(tmp_path, mode=RunMode.LIVE_APPROVAL)
    fake_fx = FakeFXService(error=RuntimeError("FX unavailable"))
    orchestrator.fx_service = fake_fx

    assert orchestrator._apply_fx_prices("run_fx", {"005930": 70000.0}) == {"005930": 70000.0}
    assert fake_fx.calls == 0
    assert orchestrator.state_store.list_system_events_by_type("fx_conversion_halt") == []


def _orchestrator(tmp_path, *, mode: RunMode) -> MaestroOrchestrator:
    raw = yaml.safe_load(open("configs/paper.yaml", encoding="utf-8"))
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)
    config.mode = mode
    config.portfolio.base_currency = Currency.KRW
    config.universe.instruments = [
        _instrument("SPY", Currency.USD, MarketRegion.US),
        _instrument("005930", Currency.KRW, MarketRegion.KR),
    ]
    return MaestroOrchestrator(config)


def _instrument(
    symbol: str,
    currency: Currency,
    region: MarketRegion,
) -> TradableInstrument:
    broker_product = (
        BrokerProduct.KIS_OVERSEAS_STOCK
        if currency == Currency.USD
        else BrokerProduct.KIS_DOMESTIC_STOCK
    )
    return TradableInstrument(
        symbol=symbol,
        asset_type=AssetType.ETF,
        region=region,
        currency=currency,
        broker="kis",
        broker_product=broker_product,
        broker_symbol=symbol,
        quantity_step=1,
        price_tick=0.01,
        min_order_quantity=1,
        min_order_notional=1,
    )

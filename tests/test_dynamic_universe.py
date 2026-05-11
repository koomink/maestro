from pathlib import Path

import yaml

from maestro.config.loader import load_config
from maestro.config.models import UniversePolicyConfig
from maestro.core.enums import AssetType, BrokerProduct, Currency, ExchangeCode, MarketRegion
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.sdk import CandidateInstrumentRequest, DataRequest, StrategyManifest
from maestro.universe import DynamicUniverseService, InstrumentResolver


def test_sdk_dynamic_universe_contract_defaults_are_backward_compatible():
    manifest = StrategyManifest(
        strategy_id="dynamic",
        name="Dynamic",
        version="0.1.0",
        supported_modes=["paper"],
        supported_asset_types=["stock"],
    )
    data_request = DataRequest(symbol="AAPL", asset_type="stock", data_type="price")
    candidate = CandidateInstrumentRequest(symbol="AAPL", asset_type="stock")

    assert manifest.sdk_contract_version == "1.0"
    assert manifest.supports_dynamic_universe is False
    assert manifest.max_candidate_symbols is None
    assert manifest.allowed_data_types == []
    assert manifest.requires_llm is False
    assert manifest.supported_llm_providers == []
    assert manifest.required_env_vars == []
    assert manifest.estimated_runtime_seconds is None
    assert manifest.allow_direct_external_data_calls is False
    assert data_request.intended_use == "research"
    assert candidate.intended_use == "research"
    assert candidate.data_types == ["price"]


def test_dynamic_universe_approves_conservative_us_kis_overseas_candidate():
    request = CandidateInstrumentRequest(
        symbol="AAPL",
        asset_type=AssetType.STOCK,
        intended_use="tradable",
        currency=Currency.USD,
        region=MarketRegion.US,
        broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
        exchange_code=ExchangeCode.NASD,
    )
    service = DynamicUniverseService(
        UniversePolicyConfig(),
        InstrumentResolver([]),
        broker_checker=AlwaysTradable(),
        data_checker=AlwaysFresh(),
    )

    evaluation = service.evaluate(
        [request],
        operator_approved_symbols={"AAPL"},
    )[0]

    assert evaluation.status == "approved_tradable"
    assert evaluation.tradable is True
    assert evaluation.instrument is not None
    assert evaluation.instrument.broker_symbol == "AAPL"
    assert evaluation.instrument.price_tick == 0.01


def test_dynamic_universe_approval_can_be_temporary_or_persistent():
    request = _tradable("AAPL")
    service = DynamicUniverseService(
        UniversePolicyConfig(),
        InstrumentResolver([]),
        broker_checker=AlwaysTradable(),
        data_checker=AlwaysFresh(),
    )

    approval = service.approve_candidates(
        [request],
        operator_approved_symbols={"AAPL"},
        persistent=True,
    )

    assert approval.approved_symbols == ["AAPL"]
    assert approval.instruments[0].symbol == "AAPL"
    assert approval.persistent is True


def test_dynamic_universe_rejects_tradable_candidate_without_operator_approval():
    request = _tradable("AAPL")
    service = DynamicUniverseService(
        UniversePolicyConfig(),
        InstrumentResolver([]),
        broker_checker=AlwaysTradable(),
        data_checker=AlwaysFresh(),
    )

    evaluation = service.evaluate([request])[0]

    assert evaluation.status == "rejected"
    assert "operator_approval_required" in evaluation.reasons


def test_dynamic_universe_rejects_non_us_or_unsupported_exchange():
    request = CandidateInstrumentRequest(
        symbol="005930",
        asset_type=AssetType.STOCK,
        intended_use="tradable",
        currency=Currency.KRW,
        region=MarketRegion.KR,
        broker_product=BrokerProduct.KIS_DOMESTIC_STOCK,
        exchange_code=ExchangeCode.KRX,
    )
    service = DynamicUniverseService(
        UniversePolicyConfig(),
        InstrumentResolver([]),
        broker_checker=AlwaysTradable(),
        data_checker=AlwaysFresh(),
    )

    evaluation = service.evaluate([request], operator_approved_symbols={"005930"})[0]

    assert evaluation.status == "rejected"
    assert "region_not_allowed" in evaluation.reasons
    assert "currency_not_allowed" in evaluation.reasons
    assert "broker_product_not_allowed" in evaluation.reasons
    assert "exchange_not_allowed" in evaluation.reasons


def test_dynamic_universe_rejects_unresolved_tradable_candidate_without_exchange():
    request = CandidateInstrumentRequest(
        symbol="AAPL",
        asset_type=AssetType.STOCK,
        intended_use="tradable",
    )
    service = DynamicUniverseService(
        UniversePolicyConfig(),
        InstrumentResolver([]),
        broker_checker=AlwaysTradable(),
        data_checker=AlwaysFresh(),
    )

    evaluation = service.evaluate([request], operator_approved_symbols={"AAPL"})[0]

    assert evaluation.status == "unresolved"
    assert evaluation.reasons == ["instrument_unresolved"]


def test_dynamic_universe_rejects_when_checks_are_missing_or_fail():
    request = _tradable("AAPL")

    missing_checks = DynamicUniverseService(UniversePolicyConfig(), InstrumentResolver([]))
    missing = missing_checks.evaluate([request], operator_approved_symbols={"AAPL"})[0]
    assert "broker_tradability_check_unavailable" in missing.reasons
    assert "data_freshness_check_unavailable" in missing.reasons

    failing_checks = DynamicUniverseService(
        UniversePolicyConfig(),
        InstrumentResolver([]),
        broker_checker=NeverTradable(),
        data_checker=NeverFresh(),
    )
    failed = failing_checks.evaluate([request], operator_approved_symbols={"AAPL"})[0]
    assert "broker_untradable" in failed.reasons
    assert "stale_or_missing_data" in failed.reasons


def test_dynamic_universe_enforces_max_new_symbols_per_run():
    service = DynamicUniverseService(
        UniversePolicyConfig(max_new_symbols_per_run=1),
        InstrumentResolver([]),
        broker_checker=AlwaysTradable(),
        data_checker=AlwaysFresh(),
    )

    evaluations = service.evaluate(
        [_tradable("AAPL"), _tradable("MSFT")],
        operator_approved_symbols={"AAPL", "MSFT"},
    )

    assert evaluations[0].status == "approved_tradable"
    assert evaluations[1].status == "rejected"
    assert "max_new_symbols_per_run_exceeded" in evaluations[1].reasons


def test_dynamic_universe_keeps_research_candidates_out_of_tradable_set():
    request = CandidateInstrumentRequest(
        symbol="VIX",
        asset_type=AssetType.STOCK,
        intended_use="research",
        data_types=["macro"],
    )
    service = DynamicUniverseService(UniversePolicyConfig(), InstrumentResolver([]))

    evaluation = service.evaluate([request])[0]

    assert evaluation.status == "research_only"
    assert evaluation.tradable is False
    assert (
        service.approved_tradable_instruments(
            [request],
            operator_approved_symbols={"VIX"},
        )
        == []
    )


def test_run_once_records_dynamic_universe_evaluations(tmp_path, monkeypatch):
    import sample_static_allocation.strategy as strategy_module

    original_manifest = strategy_module.SampleStaticAllocationStrategy.manifest

    def manifest_with_dynamic_universe(self):
        manifest = original_manifest(self)
        return manifest.model_copy(
            update={
                "supports_dynamic_universe": True,
                "max_candidate_symbols": 1,
            }
        )

    def build_candidate_requests(self, context):
        del self, context
        return [
            CandidateInstrumentRequest(
                symbol="SPY",
                asset_type=AssetType.ETF,
                intended_use="research",
                reason="benchmark",
            )
        ]

    monkeypatch.setattr(
        strategy_module.SampleStaticAllocationStrategy,
        "manifest",
        manifest_with_dynamic_universe,
    )
    monkeypatch.setattr(
        strategy_module.SampleStaticAllocationStrategy,
        "build_candidate_requests",
        build_candidate_requests,
    )
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    orchestrator = MaestroOrchestrator(load_config(config_path))
    orchestrator.run_once()

    event = orchestrator.state_store.list_system_events_by_type("dynamic_universe_evaluation")[0]
    strategy_events = event["payload"]["strategies"]["sample_static_allocation"]
    assert strategy_events[0]["status"] == "research_only"
    assert event["payload"]["approved_symbols"] == []


def _tradable(symbol: str) -> CandidateInstrumentRequest:
    return CandidateInstrumentRequest(
        symbol=symbol,
        asset_type=AssetType.STOCK,
        intended_use="tradable",
        currency=Currency.USD,
        region=MarketRegion.US,
        broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
        exchange_code=ExchangeCode.NASD,
    )


class AlwaysTradable:
    def is_tradable(self, instrument) -> bool:
        del instrument
        return True


class NeverTradable:
    def is_tradable(self, instrument) -> bool:
        del instrument
        return False


class AlwaysFresh:
    def has_fresh_data(self, symbol: str, data_types: list[str]) -> bool:
        del symbol, data_types
        return True


class NeverFresh:
    def has_fresh_data(self, symbol: str, data_types: list[str]) -> bool:
        del symbol, data_types
        return False

from datetime import UTC, datetime
from pathlib import Path

from sample_static_allocation.strategy import SampleStaticAllocationStrategy

from maestro.core.enums import RunMode
from maestro.sdk import (
    BaseStrategyPlugin,
    DataBundle,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    StrategyRuntime,
    StrategySignalResult,
    TargetAllocationResult,
)


def test_sample_strategy_contract_and_sdk_boundary():
    strategy = SampleStaticAllocationStrategy()
    context = StrategyContext(
        cycle_id="test",
        timestamp=datetime.now(UTC),
        run_mode=RunMode.PAPER,
        strategy_id="sample_static_allocation",
        config={},
    )

    requests = strategy.build_data_requests(context)
    result = strategy.run(
        data_bundle=DataBundle(
            requests=requests,
            data={},
            generated_at=datetime.now(UTC),
            source="test",
        ),
        context=context,
    )

    assert isinstance(strategy, BaseStrategyPlugin)
    assert isinstance(result, TargetAllocationResult)
    assert sum(result.allocations.values()) == 1.0

    source = Path(
        "examples/sample_static_allocation/src/sample_static_allocation/strategy.py"
    ).read_text()
    assert "maestro.portfolio" not in source
    assert "maestro.risk" not in source
    assert "maestro.execution" not in source
    assert "maestro.state" not in source
    assert "maestro.datahub" not in source
    assert "maestro.orchestration" not in source
    assert "maestro.core" not in source
    assert "koreainvestment" not in source.lower()
    assert "telegram" not in source.lower()


def test_sample_strategy_can_emit_currency_sleeve_allocations():
    strategy = SampleStaticAllocationStrategy()
    context = StrategyContext(
        cycle_id="test",
        timestamp=datetime.now(UTC),
        run_mode=RunMode.LIVE_APPROVAL,
        strategy_id="sample_static_allocation",
        config={
            "allocation_sleeves": {
                "KRW": {"133690": 0.33, "005930": 0.33, "489250": 0.33},
                "USD": {"QLD": 0.5, "GOOG": 0.5},
            }
        },
    )

    requests = strategy.build_data_requests(context)
    result = strategy.run(
        data_bundle=DataBundle(
            requests=requests,
            data={},
            generated_at=datetime.now(UTC),
            source="test",
        ),
        context=context,
    )

    assert {request.symbol for request in requests} == {
        "133690",
        "005930",
        "489250",
        "QLD",
        "GOOG",
    }
    assert result.allocations == {}
    assert result.allocation_sleeves == {
        "KRW": {"133690": 0.33, "005930": 0.33, "489250": 0.33},
        "USD": {"QLD": 0.5, "GOOG": 0.5},
    }


def test_sdk_contract_supports_llm_signal_and_rich_data_request_fields():
    timestamp = datetime(2026, 1, 15, tzinfo=UTC)
    manifest = StrategyManifest(
        strategy_id="tradingagents",
        name="TradingAgents",
        version="0.2.4",
        supported_modes=["paper"],
        supported_asset_types=["stock"],
        result_type="strategy_signal",
        requires_llm=True,
        supported_llm_providers=["openai", "google"],
        required_env_vars=["OPENAI_API_KEY"],
        estimated_runtime_seconds=900,
        allowed_data_types=[
            "ohlcv",
            "technical_indicators",
            "fundamental",
            "financial_statements",
            "news",
            "insider_transactions",
        ],
    )
    request = DataRequest(
        symbol="NVDA",
        asset_type="stock",
        data_type="technical_indicators",
        intended_use="research",
        start=timestamp,
        end=timestamp,
        as_of=timestamp,
        indicator="rsi",
        limit=5,
        query="NVDA earnings",
        statement_type="income_statement",
        frequency="quarterly",
        provider_hint="yahoo",
        source_hint="primary",
        fields=["close", "volume"],
    )
    signal = StrategySignalResult(
        strategy_id="tradingagents",
        strategy_version="0.2.4",
        timestamp=timestamp,
        symbol="NVDA",
        action="buy",
        rating="Buy",
        confidence=0.82,
        price_target=195.0,
        time_horizon="3-6 months",
        position_sizing="30% target allocation",
        rationale="TradingAgents final portfolio manager decision.",
        metadata={"reports": {"market": "strong momentum"}},
    )
    allocation = TargetAllocationResult(
        strategy_id="tradingagents",
        strategy_version="0.2.4",
        timestamp=timestamp,
        allocations={"NVDA": 0.3, "CASH": 0.7},
        confidence=0.82,
        metadata={"rating": signal.rating, "raw_signal": signal.model_dump(mode="json")},
    )

    assert manifest.sdk_contract_version == "1.0"
    assert manifest.result_type == "strategy_signal"
    assert manifest.requires_llm is True
    assert request.indicator == "rsi"
    assert request.statement_type == "income_statement"
    assert signal.metadata["reports"]["market"] == "strong momentum"
    assert allocation.metadata["rating"] == "Buy"


def test_strategy_runtime_fetches_data_and_records_audit_payload():
    timestamp = datetime(2026, 1, 15, tzinfo=UTC)
    request = DataRequest(symbol="NVDA", asset_type="stock", data_type="price")

    def fetch_data(requests):
        return DataBundle(
            requests=requests,
            data={"NVDA": {"price": 100.0}},
            generated_at=timestamp,
            source="fixture",
        )

    runtime = StrategyRuntime(
        fetch_data,
        context=StrategyContext(
            cycle_id="test",
            timestamp=timestamp,
            run_mode=RunMode.PAPER,
            strategy_id="tradingagents",
            config={},
        ),
    )

    bundle = runtime.get_data(request)
    payload = runtime.audit_payload()

    assert bundle.data["NVDA"]["price"] == 100.0
    assert payload["requests"][0]["symbol"] == "NVDA"
    assert payload["bundles"][0]["source"] == "fixture"
    assert payload["errors"] == []


def test_base_strategy_plugin_runtime_default_delegates_to_run():
    class RuntimeDefaultStrategy(BaseStrategyPlugin):
        def manifest(self):
            return StrategyManifest(
                strategy_id="runtime_default",
                name="Runtime Default",
                version="0.1.0",
                supported_modes=["paper"],
                supported_asset_types=["stock"],
            )

        def build_data_requests(self, context):
            del context
            return []

        def run(self, data_bundle, context):
            del data_bundle
            return TargetAllocationResult(
                strategy_id=context.strategy_id,
                strategy_version="0.1.0",
                timestamp=context.timestamp,
                allocations={"CASH": 1.0},
                confidence=0.5,
            )

    timestamp = datetime(2026, 1, 15, tzinfo=UTC)
    context = StrategyContext(
        cycle_id="test",
        timestamp=timestamp,
        run_mode=RunMode.PAPER,
        strategy_id="runtime_default",
        config={},
    )
    runtime = StrategyRuntime(
        lambda requests: DataBundle(
            requests=requests,
            data={},
            generated_at=timestamp,
            source="fixture",
        ),
        context=context,
    )

    result = RuntimeDefaultStrategy().run_with_runtime(
        DataBundle(requests=[], data={}, generated_at=timestamp, source="fixture"),
        context,
        runtime,
    )

    assert result.allocations == {"CASH": 1.0}

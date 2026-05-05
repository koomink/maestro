from datetime import UTC, datetime
from pathlib import Path

from sample_static_allocation.strategy import SampleStaticAllocationStrategy

from maestro.core.enums import RunMode
from maestro.sdk import BaseStrategyPlugin, DataBundle, StrategyContext, TargetAllocationResult


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

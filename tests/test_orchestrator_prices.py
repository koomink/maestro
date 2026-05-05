from datetime import UTC, datetime

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

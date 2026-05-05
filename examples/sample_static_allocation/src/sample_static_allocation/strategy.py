from maestro.sdk import (
    BaseStrategyPlugin,
    DataBundle,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    TargetAllocationResult,
)


class SampleStaticAllocationStrategy(BaseStrategyPlugin):
    def manifest(self) -> StrategyManifest:
        return StrategyManifest(
            strategy_id="sample_static_allocation",
            name="Sample Static Allocation",
            version="0.1.0",
            description="Reference Virtuoso app that proposes a fixed target allocation.",
            supported_modes=["paper"],
            supported_asset_types=["cash", "domestic_etf"],
            result_type="target_allocation",
            requires_data=["price"],
        )

    def build_data_requests(self, context: StrategyContext) -> list[DataRequest]:
        allocations = self._allocations(context)
        return [
            DataRequest(symbol=symbol, asset_type=self._asset_type(symbol), data_type="price")
            for symbol in allocations
        ]

    def run(
        self, data_bundle: DataBundle, context: StrategyContext
    ) -> TargetAllocationResult:
        return TargetAllocationResult(
            strategy_id="sample_static_allocation",
            strategy_version=self.manifest().version,
            timestamp=context.timestamp,
            allocations=self._allocations(context),
            confidence=1.0,
            time_horizon="static",
            rationale="Reference fixed allocation for Maestro v0.1.",
        )

    def _allocations(self, context: StrategyContext) -> dict[str, float]:
        return context.config.get(
            "allocations",
            {
                "CASH": 0.5,
                "MOCK_ETF_A": 0.3,
                "MOCK_ETF_B": 0.2,
            },
        )

    def _asset_type(self, symbol: str) -> str:
        return "cash" if symbol == "CASH" else "domestic_etf"

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
            supported_modes=["paper", "live_approval"],
            supported_asset_types=["cash", "stock", "etf", "domestic_etf", "us_etf"],
            result_type="target_allocation",
            requires_data=["price"],
            can_run_live=True,
        )

    def build_data_requests(self, context: StrategyContext) -> list[DataRequest]:
        allocations = self._request_allocations(context)
        return [
            DataRequest(symbol=symbol, asset_type=self._asset_type(symbol), data_type="price")
            for symbol in allocations
        ]

    def run(self, data_bundle: DataBundle, context: StrategyContext) -> TargetAllocationResult:
        allocation_sleeves = self._allocation_sleeves(context)
        return TargetAllocationResult(
            strategy_id="sample_static_allocation",
            strategy_version=self.manifest().version,
            timestamp=context.timestamp,
            allocations={} if allocation_sleeves else self._allocations(context),
            allocation_sleeves=allocation_sleeves or None,
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

    def _allocation_sleeves(self, context: StrategyContext) -> dict[str, dict[str, float]]:
        return context.config.get("allocation_sleeves", {})

    def _request_allocations(self, context: StrategyContext) -> dict[str, float]:
        allocation_sleeves = self._allocation_sleeves(context)
        if not allocation_sleeves:
            return self._allocations(context)
        symbols: dict[str, float] = {}
        for allocations in allocation_sleeves.values():
            symbols.update(allocations)
        return symbols

    def _asset_type(self, symbol: str) -> str:
        if symbol == "CASH" or symbol.startswith("CASH_"):
            return "cash"
        return "etf"

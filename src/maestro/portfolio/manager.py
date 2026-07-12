from datetime import datetime

from pydantic import BaseModel, Field

from maestro.config.models import StrategyPluginConfig
from maestro.core.clock import utc_now
from maestro.sdk import TargetAllocationResult


class PortfolioTarget(BaseModel):
    timestamp: datetime
    allocations: dict[str, float]
    allocation_sleeves: dict[str, dict[str, float]] = Field(default_factory=dict)
    source_strategy_ids: list[str] = Field(default_factory=list)


class PortfolioManager:
    def __init__(self, strategy_configs: list[StrategyPluginConfig]) -> None:
        self.strategy_weights = {
            config.id: config.weight for config in strategy_configs if config.enabled
        }

    def build_target(self, results: list[TargetAllocationResult]) -> PortfolioTarget:
        sleeve_results = [result for result in results if result.allocation_sleeves]
        plain_results = [
            result for result in results if not result.allocation_sleeves and result.allocations
        ]
        if sleeve_results and plain_results:
            raise ValueError(
                "Cannot combine sleeve and non-sleeve strategy results in one portfolio target: "
                f"sleeves={[result.strategy_id for result in sleeve_results]} "
                f"plain={[result.strategy_id for result in plain_results]}"
            )
        if any(result.allocation_sleeves for result in results):
            return self._build_sleeve_target(results)
        combined: dict[str, float] = {}
        for result in results:
            strategy_weight = self.strategy_weights[result.strategy_id]
            for symbol, weight in result.allocations.items():
                combined[symbol] = combined.get(symbol, 0.0) + weight * strategy_weight

        total = sum(combined.values())
        if total > 1.0:
            combined = {symbol: weight / total for symbol, weight in combined.items()}
        elif total < 1.0:
            combined["CASH"] = combined.get("CASH", 0.0) + (1.0 - total)

        return PortfolioTarget(
            timestamp=utc_now(),
            allocations=combined,
            source_strategy_ids=[result.strategy_id for result in results],
        )

    def _build_sleeve_target(self, results: list[TargetAllocationResult]) -> PortfolioTarget:
        sleeves: dict[str, dict[str, float]] = {}
        for result in results:
            strategy_weight = self.strategy_weights[result.strategy_id]
            for sleeve, allocations in (result.allocation_sleeves or {}).items():
                sleeve_allocations = sleeves.setdefault(sleeve, {})
                for symbol, weight in allocations.items():
                    sleeve_allocations[symbol] = (
                        sleeve_allocations.get(symbol, 0.0) + weight * strategy_weight
                    )
        for sleeve, allocations in sleeves.items():
            total = sum(allocations.values())
            if total > 1.0:
                sleeves[sleeve] = {symbol: weight / total for symbol, weight in allocations.items()}
        return PortfolioTarget(
            timestamp=utc_now(),
            allocations={},
            allocation_sleeves=sleeves,
            source_strategy_ids=[result.strategy_id for result in results],
        )

from datetime import datetime

from pydantic import BaseModel, Field

from maestro.config.models import StrategyPluginConfig
from maestro.core.clock import utc_now
from maestro.sdk import TargetAllocationResult


class PortfolioTarget(BaseModel):
    timestamp: datetime
    allocations: dict[str, float]
    source_strategy_ids: list[str] = Field(default_factory=list)


class PortfolioManager:
    def __init__(self, strategy_configs: list[StrategyPluginConfig]) -> None:
        self.strategy_weights = {
            config.id: config.weight for config in strategy_configs if config.enabled
        }

    def build_target(self, results: list[TargetAllocationResult]) -> PortfolioTarget:
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

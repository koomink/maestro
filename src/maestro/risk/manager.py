from pydantic import BaseModel, Field

from maestro.config.models import RiskConfig
from maestro.portfolio.manager import PortfolioTarget


class RiskDecision(BaseModel):
    approved: bool
    target: PortfolioTarget
    modifications: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)


class RiskManager:
    def __init__(self, allowed_symbols: list[str], config: RiskConfig) -> None:
        self.allowed_symbols = set(allowed_symbols)
        self.config = config

    def check(self, target: PortfolioTarget) -> RiskDecision:
        allocations = dict(target.allocations)
        modifications: list[str] = []
        violations: list[str] = []

        for symbol, weight in list(allocations.items()):
            if symbol not in self.allowed_symbols:
                violations.append(f"{symbol} is outside allowed universe")
            if weight < 0:
                violations.append(f"{symbol} has negative allocation")

        if violations:
            return RiskDecision(
                approved=False,
                target=target,
                modifications=modifications,
                violations=violations,
            )

        for symbol, weight in list(allocations.items()):
            if symbol == "CASH":
                continue
            if weight > self.config.max_single_asset_weight:
                excess = weight - self.config.max_single_asset_weight
                allocations[symbol] = self.config.max_single_asset_weight
                allocations["CASH"] = allocations.get("CASH", 0.0) + excess
                modifications.append(
                    f"Capped {symbol} from {weight:.6f} to {self.config.max_single_asset_weight:.6f}"
                )

        cash = allocations.get("CASH", 0.0)
        if cash < self.config.min_cash_weight:
            needed = self.config.min_cash_weight - cash
            reducible = [
                symbol for symbol in allocations if symbol != "CASH" and allocations[symbol] > 0
            ]
            non_cash_total = sum(allocations[symbol] for symbol in reducible)
            if non_cash_total <= 0:
                violations.append("Cannot satisfy minimum cash weight")
            else:
                for symbol in reducible:
                    reduction = needed * (allocations[symbol] / non_cash_total)
                    allocations[symbol] -= reduction
                allocations["CASH"] = self.config.min_cash_weight
                modifications.append(f"Raised CASH to {self.config.min_cash_weight:.6f}")

        gross = sum(allocations.values())
        if gross > 1.000001:
            violations.append("Gross exposure exceeds 1.0")
        elif gross < 1.0:
            allocations["CASH"] = allocations.get("CASH", 0.0) + (1.0 - gross)

        adjusted = PortfolioTarget(
            timestamp=target.timestamp,
            allocations=allocations,
            source_strategy_ids=target.source_strategy_ids,
        )
        return RiskDecision(
            approved=not violations,
            target=adjusted,
            modifications=modifications,
            violations=violations,
        )

from pydantic import BaseModel, Field

from maestro.config.models import RiskConfig
from maestro.core.symbols import is_cash_symbol
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
        if target.allocation_sleeves:
            return self._check_sleeves(target)
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
            if is_cash_symbol(symbol):
                continue
            if weight > self.config.max_single_asset_weight:
                excess = weight - self.config.max_single_asset_weight
                allocations[symbol] = self.config.max_single_asset_weight
                allocations["CASH"] = allocations.get("CASH", 0.0) + excess
                max_weight = self.config.max_single_asset_weight
                modifications.append(f"Capped {symbol} from {weight:.6f} to {max_weight:.6f}")

        cash_symbol = self._cash_symbol(allocations)
        cash = allocations.get(cash_symbol, 0.0)
        if cash < self.config.min_cash_weight:
            needed = self.config.min_cash_weight - cash
            reducible = [
                symbol
                for symbol in allocations
                if not is_cash_symbol(symbol) and allocations[symbol] > 0
            ]
            non_cash_total = sum(allocations[symbol] for symbol in reducible)
            if non_cash_total <= 0:
                violations.append("Cannot satisfy minimum cash weight")
            else:
                for symbol in reducible:
                    reduction = needed * (allocations[symbol] / non_cash_total)
                    allocations[symbol] -= reduction
                allocations[cash_symbol] = self.config.min_cash_weight
                modifications.append(f"Raised {cash_symbol} to {self.config.min_cash_weight:.6f}")

        gross = sum(allocations.values())
        if gross > 1.000001:
            violations.append("Gross exposure exceeds 1.0")
        elif gross < 1.0:
            cash_symbol = self._cash_symbol(allocations)
            allocations[cash_symbol] = allocations.get(cash_symbol, 0.0) + (1.0 - gross)

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

    def _check_sleeves(self, target: PortfolioTarget) -> RiskDecision:
        adjusted_sleeves: dict[str, dict[str, float]] = {}
        modifications: list[str] = []
        violations: list[str] = []
        for sleeve, raw_allocations in target.allocation_sleeves.items():
            allocations = dict(raw_allocations)
            for symbol, weight in list(allocations.items()):
                if symbol not in self.allowed_symbols:
                    violations.append(f"{symbol} is outside allowed universe")
                if weight < 0:
                    violations.append(f"{symbol} has negative allocation")
            for symbol, weight in list(allocations.items()):
                if is_cash_symbol(symbol):
                    continue
                if weight > self.config.max_single_asset_weight:
                    excess = weight - self.config.max_single_asset_weight
                    allocations[symbol] = self.config.max_single_asset_weight
                    cash_symbol = self._cash_symbol_for_sleeve(sleeve, allocations)
                    allocations[cash_symbol] = allocations.get(cash_symbol, 0.0) + excess
                    modifications.append(
                        f"Capped {symbol} from {weight:.6f} "
                        f"to {self.config.max_single_asset_weight:.6f}"
                    )
            cash_symbol = self._cash_symbol_for_sleeve(sleeve, allocations)
            cash = allocations.get(cash_symbol, 0.0)
            if cash < self.config.min_cash_weight:
                needed = self.config.min_cash_weight - cash
                reducible = [
                    symbol
                    for symbol in allocations
                    if not is_cash_symbol(symbol) and allocations[symbol] > 0
                ]
                non_cash_total = sum(allocations[symbol] for symbol in reducible)
                if non_cash_total <= 0:
                    violations.append(f"Cannot satisfy minimum cash weight for {sleeve}")
                else:
                    for symbol in reducible:
                        reduction = needed * (allocations[symbol] / non_cash_total)
                        allocations[symbol] -= reduction
                    allocations[cash_symbol] = self.config.min_cash_weight
                    modifications.append(
                        f"Raised {cash_symbol} to {self.config.min_cash_weight:.6f}"
                    )
            gross = sum(allocations.values())
            if gross > 1.000001:
                violations.append(f"Gross exposure exceeds 1.0 for {sleeve}")
            elif gross < 1.0:
                cash_symbol = self._cash_symbol_for_sleeve(sleeve, allocations)
                allocations[cash_symbol] = allocations.get(cash_symbol, 0.0) + (1.0 - gross)
            adjusted_sleeves[sleeve] = allocations

        adjusted = PortfolioTarget(
            timestamp=target.timestamp,
            allocations={},
            allocation_sleeves=adjusted_sleeves,
            source_strategy_ids=target.source_strategy_ids,
        )
        return RiskDecision(
            approved=not violations,
            target=adjusted,
            modifications=modifications,
            violations=violations,
        )

    def _cash_symbol(self, allocations: dict[str, float]) -> str:
        for symbol in allocations:
            if is_cash_symbol(symbol):
                return symbol
        for symbol in self.allowed_symbols:
            if is_cash_symbol(symbol):
                return symbol
        return "CASH"

    def _cash_symbol_for_sleeve(self, sleeve: str, allocations: dict[str, float]) -> str:
        for symbol in allocations:
            if is_cash_symbol(symbol):
                return symbol
        candidate = f"CASH_{sleeve}"
        if candidate in self.allowed_symbols:
            return candidate
        return self._cash_symbol(allocations)

from pydantic import BaseModel, Field

from maestro.core.symbols import is_cash_symbol
from maestro.portfolio.manager import PortfolioTarget


class RiskDecision(BaseModel):
    approved: bool
    target: PortfolioTarget
    violations: list[str] = Field(default_factory=list)


class RiskManager:
    def __init__(
        self,
        allowed_symbols: list[str],
        *,
        max_position_weight: float | None = None,
    ) -> None:
        self.allowed_symbols = set(allowed_symbols)
        self.max_position_weight = max_position_weight

    def check(self, target: PortfolioTarget) -> RiskDecision:
        if target.allocation_sleeves:
            return self._check_sleeves(target)
        allocations = dict(target.allocations)
        violations: list[str] = []

        for symbol, weight in list(allocations.items()):
            if symbol not in self.allowed_symbols:
                violations.append(f"{symbol} is outside allowed universe")
            if weight < 0:
                violations.append(f"{symbol} has negative allocation")
            if (
                not is_cash_symbol(symbol)
                and self.max_position_weight is not None
                and weight > self.max_position_weight + 1e-9
            ):
                violations.append(
                    f"{symbol} weight {weight:.4f} exceeds max_position_weight "
                    f"{self.max_position_weight}"
                )

        if violations:
            return RiskDecision(
                approved=False,
                target=target,
                violations=violations,
            )

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
            violations=violations,
        )

    def _check_sleeves(self, target: PortfolioTarget) -> RiskDecision:
        adjusted_sleeves: dict[str, dict[str, float]] = {}
        violations: list[str] = []
        for sleeve, raw_allocations in target.allocation_sleeves.items():
            allocations = dict(raw_allocations)
            for symbol, weight in list(allocations.items()):
                if symbol not in self.allowed_symbols:
                    violations.append(f"{symbol} is outside allowed universe")
                if weight < 0:
                    violations.append(f"{symbol} has negative allocation")
                if (
                    not is_cash_symbol(symbol)
                    and self.max_position_weight is not None
                    and weight > self.max_position_weight + 1e-9
                ):
                    violations.append(
                        f"{symbol} weight {weight:.4f} exceeds max_position_weight "
                        f"{self.max_position_weight}"
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

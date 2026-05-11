from dataclasses import dataclass

from maestro.sdk import TargetAllocationResult


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]


class SignalValidator:
    def __init__(self, allowed_symbols: list[str], strategy_ids: set[str]) -> None:
        self.allowed_symbols = set(allowed_symbols)
        self.strategy_ids = strategy_ids
        self.research_only_symbols: set[str] = set()

    @classmethod
    def with_universe_boundaries(
        cls,
        *,
        tradable_symbols: set[str],
        research_only_symbols: set[str],
        strategy_ids: set[str],
    ) -> "SignalValidator":
        validator = cls(sorted(tradable_symbols), strategy_ids)
        validator.research_only_symbols = set(research_only_symbols)
        return validator

    def validate(self, result: TargetAllocationResult) -> ValidationResult:
        errors = []
        if result.strategy_id not in self.strategy_ids:
            errors.append(f"Unknown strategy_id: {result.strategy_id}")
        if not result.allocations and not result.allocation_sleeves:
            errors.append("allocations or allocation_sleeves must not be empty")
        total = sum(result.allocations.values())
        if total > 1.000001:
            errors.append("allocation sum must be 1.0 or less")
        for symbol, weight in result.allocations.items():
            if weight < 0:
                errors.append(f"allocation for {symbol} must be non-negative")
            if symbol in self.research_only_symbols:
                errors.append(f"allocation symbol {symbol} is research-only")
            elif symbol != "CASH" and symbol not in self.allowed_symbols:
                errors.append(f"allocation symbol {symbol} is not in allowed universe")
        for sleeve, allocations in (result.allocation_sleeves or {}).items():
            sleeve_total = sum(allocations.values())
            if sleeve_total > 1.000001:
                errors.append(f"allocation_sleeves[{sleeve}] sum must be 1.0 or less")
            for symbol, weight in allocations.items():
                if weight < 0:
                    errors.append(f"allocation_sleeves[{sleeve}] for {symbol} must be non-negative")
                if symbol in self.research_only_symbols:
                    errors.append(f"allocation symbol {symbol} is research-only")
                elif symbol != "CASH" and symbol not in self.allowed_symbols:
                    errors.append(f"allocation symbol {symbol} is not in allowed universe")
        return ValidationResult(ok=not errors, errors=errors)

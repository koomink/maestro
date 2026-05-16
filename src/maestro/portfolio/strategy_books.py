from typing import Any

from maestro.core.symbols import is_cash_symbol
from maestro.sdk import StrategyBookAllocation, TargetAllocationResult
from maestro.state.models import PortfolioState


def build_strategy_book_snapshots(
    *,
    results: list[TargetAllocationResult],
    strategy_weights: dict[str, float],
    state: PortfolioState,
    prices: dict[str, float],
) -> list[dict[str, Any]]:
    total_value = state.total_value(prices)
    snapshots: list[dict[str, Any]] = []
    for result in results:
        for book in _books_for_result(result):
            book_weight = strategy_weights.get(result.strategy_id, 1.0) * book.target_weight
            allocations = _allocations_with_cash(book.allocations)
            book_value = total_value * book_weight
            cash = sum(
                book_value * weight
                for symbol, weight in allocations.items()
                if is_cash_symbol(symbol)
            )
            positions = {
                symbol: (book_value * weight) / prices[symbol]
                for symbol, weight in allocations.items()
                if not is_cash_symbol(symbol) and symbol in prices
            }
            missing_prices = sorted(
                symbol
                for symbol in allocations
                if not is_cash_symbol(symbol) and symbol not in prices
            )
            snapshots.append(
                {
                    "strategy_id": result.strategy_id,
                    "book_id": book.book_id,
                    "label": book.label or book.book_id,
                    "target_weight": book_weight,
                    "book_value": book_value,
                    "cash": cash,
                    "positions": positions,
                    "allocations": allocations,
                    "missing_prices": missing_prices,
                    "rationale": book.rationale,
                    "metadata": book.metadata,
                }
            )
    return snapshots


def _books_for_result(result: TargetAllocationResult) -> list[StrategyBookAllocation]:
    if result.strategy_books:
        return result.strategy_books
    allocations = result.allocations or _flatten_sleeves(result.allocation_sleeves or {})
    return [
        StrategyBookAllocation(
            book_id=result.strategy_id,
            label=result.strategy_id,
            target_weight=1.0,
            allocations=allocations,
            rationale=result.rationale,
        )
    ]


def _flatten_sleeves(sleeves: dict[str, dict[str, float]]) -> dict[str, float]:
    flattened: dict[str, float] = {}
    for allocations in sleeves.values():
        for symbol, weight in allocations.items():
            flattened[symbol] = flattened.get(symbol, 0.0) + weight
    total = sum(flattened.values())
    if total > 1.0:
        return {symbol: weight / total for symbol, weight in flattened.items()}
    return flattened


def _allocations_with_cash(allocations: dict[str, float]) -> dict[str, float]:
    output = dict(allocations)
    total = sum(output.values())
    if total < 1.0:
        output["CASH"] = output.get("CASH", 0.0) + (1.0 - total)
    return output

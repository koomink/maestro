from dataclasses import dataclass

from maestro.core.symbols import is_cash_symbol
from maestro.portfolio.manager import PortfolioTarget
from maestro.state.models import PortfolioState


@dataclass(frozen=True)
class ExecutionScopeDraft:
    account_id: str | None
    execution_sleeve: str | None
    currency_sleeve: str | None
    target_weight: float
    target: PortfolioTarget
    attributed_positions: dict[str, float] | None = None


@dataclass(frozen=True)
class AllocatedExecutionScope:
    account_id: str | None
    execution_sleeve: str | None
    currency_sleeve: str | None
    target_weight: float
    current_value: float
    current_weight: float
    drift: float
    allocated_cash: float
    state: PortfolioState
    target: PortfolioTarget


def allocate_cash_rebalanced_scope_states(
    *,
    current_state: PortfolioState,
    scopes: list[ExecutionScopeDraft],
    prices: dict[str, float],
) -> list[AllocatedExecutionScope]:
    if not scopes:
        return []
    validate_unique_execution_scope_symbols(scopes)
    current_values = [
        _scope_position_value(current_state, scope, scope.target, prices)
        for scope in scopes
    ]
    cash = _cash_for_currency(current_state, scopes[0].currency_sleeve)
    account_value = _account_value(current_state, prices, cash)
    target_values = [account_value * scope.target_weight for scope in scopes]
    shortfalls = [
        max(0.0, target - current)
        for target, current in zip(target_values, current_values, strict=True)
    ]
    shortfall_total = sum(shortfalls)
    if cash <= 0:
        allocations = [0.0 for _ in scopes]
    elif shortfall_total > 0:
        allocatable_cash = min(cash, shortfall_total)
        allocations = [
            allocatable_cash * shortfall / shortfall_total for shortfall in shortfalls
        ]
    else:
        weight_total = sum(scope.target_weight for scope in scopes)
        allocations = [cash * scope.target_weight / weight_total for scope in scopes]

    output: list[AllocatedExecutionScope] = []
    for scope, current_value, allocated_cash in zip(
        scopes,
        current_values,
        allocations,
        strict=True,
    ):
        current_weight = current_value / account_value if account_value > 0 else 0.0
        output.append(
            AllocatedExecutionScope(
                account_id=scope.account_id,
                execution_sleeve=scope.execution_sleeve,
                currency_sleeve=scope.currency_sleeve,
                target_weight=scope.target_weight,
                current_value=current_value,
                current_weight=current_weight,
                drift=current_weight - scope.target_weight,
                allocated_cash=allocated_cash,
                state=_scope_state(current_state, scope, allocated_cash),
                target=scope.target,
            )
        )
    return output


def validate_unique_execution_scope_symbols(scopes: list[ExecutionScopeDraft]) -> None:
    symbols_by_account: dict[str | None, dict[str, str | None]] = {}
    for scope in scopes:
        owned = symbols_by_account.setdefault(scope.account_id, {})
        for symbol in _target_symbols(scope.target):
            previous = owned.get(symbol)
            if previous is not None and previous != scope.execution_sleeve:
                raise ValueError(
                    "execution_sleeves shared target symbols within account are not supported: "
                    f"account_id={scope.account_id} symbol={symbol} "
                    f"sleeves={previous},{scope.execution_sleeve}"
                )
            owned[symbol] = scope.execution_sleeve


def _scope_state(
    current_state: PortfolioState,
    scope: ExecutionScopeDraft,
    allocated_cash: float,
) -> PortfolioState:
    if scope.attributed_positions is not None:
        # Attributed positions are already scoped to this sleeve's bucket; keep
        # them all so holdings outside today's target can still be sold when the
        # strategy rotates.
        positions = {
            symbol: quantity
            for symbol, quantity in scope.attributed_positions.items()
            if quantity
        }
    else:
        symbols = set(_target_symbols(scope.target))
        positions = {
            symbol: quantity
            for symbol, quantity in current_state.positions.items()
            if symbol in symbols
        }
    cash_by_currency = (
        {scope.currency_sleeve: allocated_cash}
        if scope.currency_sleeve and current_state.cash_by_currency
        else {}
    )
    return PortfolioState(
        cash=allocated_cash,
        cash_by_currency=cash_by_currency,
        positions=positions,
    )


def _scope_position_value(
    current_state: PortfolioState,
    scope: ExecutionScopeDraft,
    target: PortfolioTarget,
    prices: dict[str, float],
) -> float:
    total = 0.0
    if scope.attributed_positions is not None:
        for symbol, quantity in scope.attributed_positions.items():
            if not quantity:
                continue
            if symbol not in prices:
                raise ValueError(
                    f"execution sleeve value requires a price for attributed position: {symbol}"
                )
            total += quantity * prices[symbol]
        return total
    for symbol in _target_symbols(target):
        total += current_state.positions.get(symbol, 0.0) * prices[symbol]
    return total


def _cash_for_currency(current_state: PortfolioState, currency_sleeve: str | None) -> float:
    if current_state.cash_by_currency and currency_sleeve:
        return current_state.cash_by_currency.get(currency_sleeve, 0.0)
    if current_state.cash_by_currency:
        return sum(current_state.cash_by_currency.values())
    return current_state.cash


def _account_value(
    current_state: PortfolioState,
    prices: dict[str, float],
    cash: float,
) -> float:
    total = cash
    for symbol, quantity in current_state.positions.items():
        if symbol not in prices:
            raise ValueError(
                f"execution sleeve capacity requires a price for account position: {symbol}"
            )
        total += quantity * prices[symbol]
    return total


def _target_symbols(target: PortfolioTarget) -> list[str]:
    symbols: list[str] = []
    if target.allocation_sleeves:
        for allocations in target.allocation_sleeves.values():
            symbols.extend(symbol for symbol in allocations if not is_cash_symbol(symbol))
    else:
        symbols.extend(symbol for symbol in target.allocations if not is_cash_symbol(symbol))
    return list(dict.fromkeys(symbols))

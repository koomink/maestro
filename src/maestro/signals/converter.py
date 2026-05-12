from maestro.config.strategy import SignalToAllocationConfig
from maestro.sdk import StrategyResult, StrategySignalResult, TargetAllocationResult


def normalize_strategy_result(
    result: StrategyResult,
    signal_to_allocation: SignalToAllocationConfig | None,
) -> TargetAllocationResult:
    if isinstance(result, TargetAllocationResult):
        return result
    if isinstance(result, StrategySignalResult):
        if signal_to_allocation is None:
            raise ValueError("strategy_signal result requires signal_to_allocation policy")
        return strategy_signal_to_target_allocation(result, signal_to_allocation)
    raise TypeError(f"Unsupported strategy result type: {type(result).__name__}")


def strategy_signal_to_target_allocation(
    signal: StrategySignalResult,
    policy: SignalToAllocationConfig,
) -> TargetAllocationResult:
    target_weight = getattr(policy.action_target_weights, signal.action)
    allocations = _allocations_for_signal(
        symbol=signal.symbol,
        cash_symbol=policy.cash_symbol,
        target_weight=target_weight,
    )
    metadata = dict(signal.metadata)
    metadata["source_signal"] = signal.model_dump(mode="json")
    return TargetAllocationResult(
        strategy_id=signal.strategy_id,
        strategy_version=signal.strategy_version,
        timestamp=signal.timestamp,
        allocations=allocations,
        confidence=signal.confidence,
        time_horizon=signal.time_horizon,
        rationale=signal.rationale,
        risk_flags=list(signal.risk_flags),
        metadata=metadata,
    )


def _allocations_for_signal(
    *,
    symbol: str,
    cash_symbol: str,
    target_weight: float,
) -> dict[str, float]:
    if target_weight <= 0.0:
        return {cash_symbol: 1.0}
    if symbol == cash_symbol:
        return {cash_symbol: 1.0}
    return {
        symbol: target_weight,
        cash_symbol: 1.0 - target_weight,
    }

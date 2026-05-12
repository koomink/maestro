from typing import Any, Literal

from pydantic import Field, field_validator

from maestro.config.base import StrictConfigModel
from maestro.core.enums import StrategyMode


class SignalActionTargetWeights(StrictConfigModel):
    buy: float = Field(ge=0.0, le=1.0)
    hold: float = Field(ge=0.0, le=1.0)
    sell: float = Field(ge=0.0, le=1.0)


class SignalToAllocationConfig(StrictConfigModel):
    type: Literal["single_symbol_action_map"]
    cash_symbol: str = "CASH"
    action_target_weights: SignalActionTargetWeights


class StrategyPluginConfig(StrictConfigModel):
    id: str
    enabled: bool = True
    mode: StrategyMode = StrategyMode.PAPER
    weight: float = Field(ge=0.0)
    entrypoint: str
    config: dict[str, Any] = Field(default_factory=dict)
    signal_to_allocation: SignalToAllocationConfig | None = None

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError("entrypoint must use 'module:ClassName' format")
        return value


__all__ = [
    "SignalActionTargetWeights",
    "SignalToAllocationConfig",
    "StrategyPluginConfig",
]

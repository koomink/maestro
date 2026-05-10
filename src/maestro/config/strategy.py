from typing import Any

from pydantic import Field, field_validator

from maestro.config.base import StrictConfigModel
from maestro.core.enums import StrategyMode


class StrategyPluginConfig(StrictConfigModel):
    id: str
    enabled: bool = True
    mode: StrategyMode = StrategyMode.PAPER
    weight: float = Field(ge=0.0)
    entrypoint: str
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError("entrypoint must use 'module:ClassName' format")
        return value


__all__ = ["StrategyPluginConfig"]

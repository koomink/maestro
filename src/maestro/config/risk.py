from pydantic import Field

from maestro.config.base import StrictConfigModel


class RiskConfig(StrictConfigModel):
    max_position_weight: float | None = Field(default=None, gt=0.0, le=1.0)


__all__ = ["RiskConfig"]

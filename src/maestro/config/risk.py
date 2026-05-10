from pydantic import Field

from maestro.config.base import StrictConfigModel


class RiskConfig(StrictConfigModel):
    max_single_asset_weight: float = Field(gt=0.0, le=1.0)
    min_cash_weight: float = Field(ge=0.0, le=1.0)


__all__ = ["RiskConfig"]

from pydantic import Field

from maestro.config.base import StrictConfigModel


class ReconciliationConfig(StrictConfigModel):
    cash_tolerance: float = Field(default=0.0, ge=0.0)
    position_quantity_tolerance: float = Field(default=0.0, ge=0.0)
    value_tolerance: float = Field(default=0.0, ge=0.0)
    max_age_seconds: int = Field(default=86400, gt=0)
    observation_snapshot_max_age_seconds: int = Field(default=1200, gt=0)
    signal_snapshot_max_age_seconds: int = Field(default=900, gt=0)
    approval_snapshot_max_age_seconds: int = Field(default=300, gt=0)


__all__ = ["ReconciliationConfig"]

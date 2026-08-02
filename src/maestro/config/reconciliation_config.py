from pydantic import Field

from maestro.config.base import StrictConfigModel


class BuyingPowerDriftConfig(StrictConfigModel):
    budget_by_currency: dict[str, float] = Field(default_factory=dict)
    nav_ratio_budget: float = Field(default=0.0005, ge=0.0)
    recent_fill_ratio_budget: float = Field(default=0.01, ge=0.0)
    settlement_grace_days: int = Field(default=3, ge=0)


class ReconciliationConfig(StrictConfigModel):
    # Legacy fallback for adapters that predate the explicit ledger contract.
    cash_tolerance: float = Field(default=0.0, ge=0.0)
    ledger_cash_tolerance_by_currency: dict[str, float] = Field(default_factory=dict)
    buying_power_drift: BuyingPowerDriftConfig = Field(default_factory=BuyingPowerDriftConfig)
    # Flat names remain accepted for backwards-compatible operator fragments.
    buying_power_drift_budget_by_currency: dict[str, float] = Field(default_factory=dict)
    buying_power_drift_nav_ratio_budget: float = Field(default=0.0005, ge=0.0)
    buying_power_drift_recent_fill_ratio_budget: float = Field(default=0.01, ge=0.0)
    buying_power_drift_settlement_grace_days: int = Field(default=3, ge=0)
    position_quantity_tolerance: float = Field(default=0.0, ge=0.0)
    value_tolerance: float = Field(default=0.0, ge=0.0)
    max_age_seconds: int = Field(default=86400, gt=0)
    observation_snapshot_max_age_seconds: int = Field(default=1200, gt=0)
    signal_snapshot_max_age_seconds: int = Field(default=900, gt=0)
    approval_snapshot_max_age_seconds: int = Field(default=300, gt=0)


__all__ = ["BuyingPowerDriftConfig", "ReconciliationConfig"]

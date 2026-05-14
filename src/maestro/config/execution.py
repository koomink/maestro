from typing import Literal

from pydantic import Field, field_validator, model_validator

from maestro.config.base import StrictConfigModel
from maestro.core.enums import Currency, OrderType


class ContributionConfig(StrictConfigModel):
    enabled: bool = False
    currency: Currency = Currency.KRW
    sleeve: str = "KRW"
    monthly_budget: float = Field(default=0.0, ge=0.0)
    min_monthly_budget: float = Field(default=0.0, ge=0.0)
    max_monthly_budget: float = Field(default=0.0, ge=0.0)
    buy_day: int = Field(default=1, ge=1, le=31)
    non_trading_day_policy: Literal["next_trading_day"] = "next_trading_day"
    target_policy: Literal["buy_only_toward_target"] = "buy_only_toward_target"

    @model_validator(mode="after")
    def validate_budget_range(self) -> "ContributionConfig":
        if not self.enabled:
            return self
        if self.monthly_budget <= 0:
            raise ValueError("monthly_budget must be positive when contribution is enabled")
        if self.min_monthly_budget > self.monthly_budget:
            raise ValueError("min_monthly_budget must be less than or equal to monthly_budget")
        if self.max_monthly_budget and self.monthly_budget > self.max_monthly_budget:
            raise ValueError("monthly_budget must be less than or equal to max_monthly_budget")
        if self.max_monthly_budget and self.min_monthly_budget > self.max_monthly_budget:
            raise ValueError("min_monthly_budget must be less than or equal to max_monthly_budget")
        return self


class ExecutionConfig(StrictConfigModel):
    engine: str = "paper"
    order_generation_mode: Literal["target_rebalance", "buy_only_contribution"] = "target_rebalance"
    contribution: ContributionConfig = Field(default_factory=ContributionConfig)
    live_order_enabled: bool = False
    live_order_dry_run: bool = False
    require_reconciliation_pass: bool = True
    max_live_order_notional: float = Field(default=0.0, ge=0.0)
    max_daily_live_notional: float = Field(default=0.0, ge=0.0)
    max_daily_live_order_count: int = Field(default=0, ge=0)
    daily_loss_limit: float | None = Field(default=None, gt=0.0)
    allowed_order_type: OrderType = OrderType.LIMIT
    order_status_poll_interval_seconds: float = Field(default=30.0, ge=0.0)
    order_status_max_polls: int = Field(default=20, gt=0)
    order_status_terminal_timeout_seconds: float = Field(default=1800.0, ge=0.0)
    require_market_session: bool = False
    market_session_timezone: str = "America/New_York"
    market_session_open: str = "09:30"
    market_session_close: str = "16:00"
    market_session_weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    market_session_holidays: list[str] = Field(default_factory=list)
    require_broker_quote_validation: bool = False
    max_broker_quote_deviation_pct: float = Field(default=0.05, ge=0.0)
    require_broker_risk_validation: bool = False
    live_order_fee_buffer_pct: float = Field(default=0.0, ge=0.0)
    heartbeat_max_age_seconds: int = Field(default=0, ge=0)
    scheduled_run_max_age_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_contribution_mode(self) -> "ExecutionConfig":
        if self.order_generation_mode == "buy_only_contribution" and not self.contribution.enabled:
            raise ValueError(
                "execution.contribution.enabled must be true for buy_only_contribution"
            )
        return self

    @field_validator("allowed_order_type")
    @classmethod
    def validate_allowed_order_type(cls, value: OrderType) -> OrderType:
        if value != OrderType.LIMIT:
            raise ValueError("allowed_order_type must be limit")
        return value

    @field_validator("market_session_open", "market_session_close")
    @classmethod
    def validate_market_session_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("market session time must use HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("market session time must use HH:MM")
        return value

    @field_validator("market_session_weekdays")
    @classmethod
    def validate_market_session_weekdays(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("market_session_weekdays must not be empty")
        invalid = [weekday for weekday in value if weekday < 0 or weekday > 6]
        if invalid:
            raise ValueError("market_session_weekdays must use values 0..6")
        return value


__all__ = ["ContributionConfig", "ExecutionConfig"]

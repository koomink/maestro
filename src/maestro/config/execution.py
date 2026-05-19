from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from maestro.config.base import StrictConfigModel
from maestro.core.enums import Currency, OrderType

OrderPosture = Literal["disabled", "dry_run", "armed"]


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


class MarketSessionConfig(StrictConfigModel):
    required: bool = False
    timezone: str = "America/New_York"
    open: str = "09:30"
    close: str = "16:00"
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    holidays: list[str] = Field(default_factory=list)

    @field_validator("open", "close")
    @classmethod
    def validate_market_session_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("market session time must use HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError("market session time must use HH:MM")
        return value

    @field_validator("weekdays")
    @classmethod
    def validate_market_session_weekdays(cls, value: list[int]) -> list[int]:
        if not value:
            raise ValueError("market_session.weekdays must not be empty")
        invalid = [weekday for weekday in value if weekday < 0 or weekday > 6]
        if invalid:
            raise ValueError("market_session.weekdays must use values 0..6")
        return value


class BrokerValidationConfig(StrictConfigModel):
    require_quote_validation: bool = False
    max_quote_deviation_pct: float = Field(default=0.05, ge=0.0)
    require_risk_validation: bool = False


class LiveOrderLimitsConfig(StrictConfigModel):
    max_order_notional: float = Field(default=0.0, ge=0.0)
    max_daily_notional: float = Field(default=0.0, ge=0.0)
    max_daily_order_count: int = Field(default=0, ge=0)
    daily_loss_limit: float | None = Field(default=None, gt=0.0)
    fee_buffer_pct: float = Field(default=0.0, ge=0.0)


class ExecutionConfig(StrictConfigModel):
    engine: str = "paper"
    order_posture: OrderPosture = "disabled"
    order_generation_mode: Literal["target_rebalance", "buy_only_contribution"] = "target_rebalance"
    contribution: ContributionConfig = Field(default_factory=ContributionConfig)
    market_session: MarketSessionConfig = Field(default_factory=MarketSessionConfig)
    broker_validation: BrokerValidationConfig = Field(default_factory=BrokerValidationConfig)
    live_order_limits: LiveOrderLimitsConfig = Field(default_factory=LiveOrderLimitsConfig)
    live_order_enabled: bool = False
    live_order_dry_run: bool = False
    require_reconciliation_pass: bool = True
    allowed_order_type: OrderType = OrderType.LIMIT
    order_status_poll_interval_seconds: float = Field(default=30.0, ge=0.0)
    order_status_max_polls: int = Field(default=20, gt=0)
    order_status_terminal_timeout_seconds: float = Field(default=1800.0, ge=0.0)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        _migrate_legacy_block(
            values,
            "market_session",
            {
                "require_market_session": "required",
                "market_session_timezone": "timezone",
                "market_session_open": "open",
                "market_session_close": "close",
                "market_session_weekdays": "weekdays",
                "market_session_holidays": "holidays",
            },
        )
        _migrate_legacy_block(
            values,
            "broker_validation",
            {
                "require_broker_quote_validation": "require_quote_validation",
                "max_broker_quote_deviation_pct": "max_quote_deviation_pct",
                "require_broker_risk_validation": "require_risk_validation",
            },
        )
        _migrate_legacy_block(
            values,
            "live_order_limits",
            {
                "max_live_order_notional": "max_order_notional",
                "max_daily_live_notional": "max_daily_notional",
                "max_daily_live_order_count": "max_daily_order_count",
                "daily_loss_limit": "daily_loss_limit",
                "live_order_fee_buffer_pct": "fee_buffer_pct",
            },
        )
        _migrate_order_posture(values)
        return values

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

    @property
    def require_market_session(self) -> bool:
        return self.market_session.required

    @property
    def market_session_timezone(self) -> str:
        return self.market_session.timezone

    @property
    def market_session_open(self) -> str:
        return self.market_session.open

    @property
    def market_session_close(self) -> str:
        return self.market_session.close

    @property
    def market_session_weekdays(self) -> list[int]:
        return self.market_session.weekdays

    @property
    def market_session_holidays(self) -> list[str]:
        return self.market_session.holidays

    @property
    def require_broker_quote_validation(self) -> bool:
        return self.broker_validation.require_quote_validation

    @property
    def max_broker_quote_deviation_pct(self) -> float:
        return self.broker_validation.max_quote_deviation_pct

    @property
    def require_broker_risk_validation(self) -> bool:
        return self.broker_validation.require_risk_validation

    @property
    def max_live_order_notional(self) -> float:
        return self.live_order_limits.max_order_notional

    @property
    def max_daily_live_notional(self) -> float:
        return self.live_order_limits.max_daily_notional

    @property
    def max_daily_live_order_count(self) -> int:
        return self.live_order_limits.max_daily_order_count

    @property
    def daily_loss_limit(self) -> float | None:
        return self.live_order_limits.daily_loss_limit

    @property
    def live_order_fee_buffer_pct(self) -> float:
        return self.live_order_limits.fee_buffer_pct


def _migrate_legacy_block(
    values: dict[str, Any],
    block_name: str,
    field_map: dict[str, str],
) -> None:
    legacy_keys = [key for key in field_map if key in values]
    if not legacy_keys:
        return
    if block_name in values:
        raise ValueError(
            f"execution.{block_name} cannot be mixed with legacy execution fields: "
            + ", ".join(legacy_keys)
        )
    values[block_name] = {field_map[key]: values.pop(key) for key in legacy_keys}


def _migrate_order_posture(values: dict[str, Any]) -> None:
    posture = values.get("order_posture")
    has_enabled = "live_order_enabled" in values
    has_dry_run = "live_order_dry_run" in values
    if posture is None:
        values["order_posture"] = _posture_from_legacy_flags(
            bool(values.get("live_order_enabled", False)),
            bool(values.get("live_order_dry_run", False)),
        )
        return

    expected_enabled, expected_dry_run = _flags_from_order_posture(str(posture))
    conflicts = []
    if has_enabled and bool(values["live_order_enabled"]) != expected_enabled:
        conflicts.append("live_order_enabled")
    if has_dry_run and bool(values["live_order_dry_run"]) != expected_dry_run:
        conflicts.append("live_order_dry_run")
    if conflicts:
        raise ValueError(
            "execution.order_posture conflicts with legacy execution fields: "
            + ", ".join(conflicts)
        )
    values["live_order_enabled"] = expected_enabled
    values["live_order_dry_run"] = expected_dry_run


def _posture_from_legacy_flags(live_order_enabled: bool, live_order_dry_run: bool) -> str:
    if live_order_dry_run:
        return "dry_run"
    if live_order_enabled:
        return "armed"
    return "disabled"


def _flags_from_order_posture(posture: str) -> tuple[bool, bool]:
    if posture == "disabled":
        return False, False
    if posture == "dry_run":
        return False, True
    if posture == "armed":
        return True, False
    return False, False


__all__ = [
    "BrokerValidationConfig",
    "ContributionConfig",
    "ExecutionConfig",
    "LiveOrderLimitsConfig",
    "MarketSessionConfig",
    "OrderPosture",
]

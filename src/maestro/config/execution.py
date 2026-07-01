from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from maestro.config.base import StrictConfigModel
from maestro.core.enums import Currency, OrderType

OrderPosture = Literal["disabled", "dry_run", "armed"]
OrderGenerationMode = Literal["target_rebalance", "buy_only_contribution"]


class ContributionFundingRequestConfig(StrictConfigModel):
    enabled: bool = False
    provider: Literal["telegram"] = "telegram"
    amount_policy: Literal["min_monthly_budget_shortfall"] = "min_monthly_budget_shortfall"


class ContributionBudgetRequestConfig(StrictConfigModel):
    enabled: bool = False
    provider: Literal["telegram"] = "telegram"


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
    funding_request: ContributionFundingRequestConfig = Field(
        default_factory=ContributionFundingRequestConfig
    )
    budget_request: ContributionBudgetRequestConfig = Field(
        default_factory=ContributionBudgetRequestConfig
    )

    @model_validator(mode="after")
    def validate_budget_range(self) -> "ContributionConfig":
        if not self.enabled:
            return self
        if self.monthly_budget <= 0:
            raise ValueError("monthly_budget must be positive when contribution is enabled")
        if self.min_monthly_budget > self.monthly_budget:
            raise ValueError("min_monthly_budget must be less than or equal to monthly_budget")
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
    max_order_notional_by_currency: dict[Currency, float] = Field(default_factory=dict)
    max_daily_notional: float = Field(default=0.0, ge=0.0)
    max_daily_notional_by_currency: dict[Currency, float] = Field(default_factory=dict)
    max_daily_order_count: int = Field(default=0, ge=0)
    daily_loss_limit: float | None = Field(default=None, gt=0.0)
    daily_loss_limit_by_currency: dict[Currency, float] = Field(default_factory=dict)
    fee_buffer_pct: float = Field(default=0.0, ge=0.0)

    @field_validator(
        "max_order_notional_by_currency",
        "max_daily_notional_by_currency",
    )
    @classmethod
    def validate_nonnegative_currency_limits(
        cls,
        value: dict[Currency, float],
    ) -> dict[Currency, float]:
        invalid = [currency.value for currency, limit in value.items() if limit < 0]
        if invalid:
            raise ValueError("currency-specific notional limits must be non-negative")
        return value

    @field_validator("daily_loss_limit_by_currency")
    @classmethod
    def validate_positive_daily_loss_limits(
        cls,
        value: dict[Currency, float],
    ) -> dict[Currency, float]:
        invalid = [currency.value for currency, limit in value.items() if limit <= 0]
        if invalid:
            raise ValueError("currency-specific daily loss limits must be positive")
        return value

    @model_validator(mode="before")
    @classmethod
    def reject_mixed_scalar_and_currency_limits(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        pairs = [
            ("max_order_notional", "max_order_notional_by_currency", 0.0),
            ("max_daily_notional", "max_daily_notional_by_currency", 0.0),
            ("daily_loss_limit", "daily_loss_limit_by_currency", None),
        ]
        for scalar_key, currency_key, empty_value in pairs:
            if scalar_key not in data or currency_key not in data:
                continue
            if not data.get(currency_key):
                continue
            scalar_value = data.get(scalar_key)
            if scalar_value in {empty_value, None}:
                continue
            raise ValueError(
                f"execution.live_order_limits cannot mix {scalar_key} "
                f"with {currency_key}"
            )
        return data

    def max_order_notional_for(self, currency: Currency | str | None) -> float | None:
        return _limit_for_currency(
            self.max_order_notional_by_currency,
            self.max_order_notional,
            currency,
        )

    def max_daily_notional_for(self, currency: Currency | str | None) -> float | None:
        return _limit_for_currency(
            self.max_daily_notional_by_currency,
            self.max_daily_notional,
            currency,
        )

    def daily_loss_limit_for(self, currency: Currency | str | None) -> float | None:
        if self.daily_loss_limit_by_currency:
            normalized = _normalize_currency(currency)
            if normalized is None:
                return None
            return self.daily_loss_limit_by_currency.get(normalized)
        return self.daily_loss_limit

    def has_daily_loss_limit(self) -> bool:
        return self.daily_loss_limit is not None or bool(self.daily_loss_limit_by_currency)


def _limit_for_currency(
    currency_limits: dict[Currency, float],
    scalar_limit: float,
    currency: Currency | str | None,
) -> float | None:
    if currency_limits:
        normalized = _normalize_currency(currency)
        if normalized is None:
            return None
        return currency_limits.get(normalized)
    return scalar_limit


def _normalize_currency(currency: Currency | str | None) -> Currency | None:
    if currency is None:
        return None
    if isinstance(currency, Currency):
        return currency
    return Currency(str(currency))



class ExecutionSleeveConfig(StrictConfigModel):
    currency_sleeve: str
    target_weight: float = Field(gt=0.0, le=1.0)
    order_generation_mode: OrderGenerationMode
    contribution: ContributionConfig | None = None

    @model_validator(mode="after")
    def validate_order_mode(self) -> "ExecutionSleeveConfig":
        if self.order_generation_mode == "buy_only_contribution":
            if self.contribution is None or not self.contribution.enabled:
                raise ValueError(
                    "execution_sleeves buy_only_contribution requires "
                    "contribution.enabled=true"
                )
        if (
            self.order_generation_mode != "buy_only_contribution"
            and self.contribution is not None
            and self.contribution.funding_request.enabled
        ):
            raise ValueError("funding_request requires buy_only_contribution")
        if (
            self.order_generation_mode != "buy_only_contribution"
            and self.contribution is not None
            and self.contribution.budget_request.enabled
        ):
            raise ValueError("budget_request requires buy_only_contribution")
        return self


class ExecutionSleevesConfig(StrictConfigModel):
    accounts: dict[str, dict[str, ExecutionSleeveConfig]] = Field(default_factory=dict)

    def account_sleeves(self, account_id: str | None) -> dict[str, ExecutionSleeveConfig]:
        if account_id is None:
            return {}
        return self.accounts.get(account_id, {})

    def sleeve(self, account_id: str | None, sleeve_id: str | None) -> ExecutionSleeveConfig | None:
        if account_id is None or sleeve_id is None:
            return None
        return self.accounts.get(account_id, {}).get(sleeve_id)

    def has_sleeves(self) -> bool:
        return bool(self.accounts)


class AccountStrategyTargetConfig(StrictConfigModel):
    target_weight: float = Field(gt=0.0, le=1.0)
    allowed_symbols: list[str] = Field(default_factory=list)


class ExecutionConfig(StrictConfigModel):
    proposal_engine: str = "paper"
    order_posture: OrderPosture = "disabled"
    order_generation_mode: OrderGenerationMode = "target_rebalance"
    contribution: ContributionConfig = Field(default_factory=ContributionConfig)
    market_session: MarketSessionConfig = Field(default_factory=MarketSessionConfig)
    market_sessions_by_exchange: dict[str, MarketSessionConfig] = Field(default_factory=dict)
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
        _migrate_proposal_engine(values)
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
        if (
            self.order_generation_mode != "buy_only_contribution"
            and self.contribution.funding_request.enabled
        ):
            raise ValueError("funding_request requires buy_only_contribution")
        if (
            self.order_generation_mode != "buy_only_contribution"
            and self.contribution.budget_request.enabled
        ):
            raise ValueError("budget_request requires buy_only_contribution")
        return self

    @field_validator("allowed_order_type")
    @classmethod
    def validate_allowed_order_type(cls, value: OrderType) -> OrderType:
        if value != OrderType.LIMIT:
            raise ValueError("allowed_order_type must be limit")
        return value

    @property
    def engine(self) -> str:
        return self.proposal_engine

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


def _migrate_proposal_engine(values: dict[str, Any]) -> None:
    if "engine" not in values:
        return
    engine = values.pop("engine")
    if "proposal_engine" in values and values["proposal_engine"] != engine:
        raise ValueError("execution.proposal_engine conflicts with legacy execution.engine")
    values["proposal_engine"] = engine


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
    "ContributionBudgetRequestConfig",
    "ContributionConfig",
    "AccountStrategyTargetConfig",
    "ExecutionConfig",
    "ExecutionSleeveConfig",
    "ExecutionSleevesConfig",
    "OrderGenerationMode",
    "LiveOrderLimitsConfig",
    "MarketSessionConfig",
    "OrderPosture",
]

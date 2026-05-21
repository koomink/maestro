from typing import Any

from pydantic import Field, model_validator

from maestro.config.approval import ApprovalConfig
from maestro.config.base import StrictConfigModel
from maestro.config.broker import KISConfig
from maestro.config.datahub import DataHubConfig, DataHubProviderConfig
from maestro.config.execution import (
    BrokerValidationConfig,
    ContributionConfig,
    ExecutionConfig,
    LiveOrderLimitsConfig,
    MarketSessionConfig,
)
from maestro.config.monitoring_config import MonitoringConfig
from maestro.config.portfolio import CurrencySleeveConfig, PortfolioConfig
from maestro.config.reconciliation_config import ReconciliationConfig
from maestro.config.risk import RiskConfig
from maestro.config.state_config import AuditConfig, StateConfig
from maestro.config.strategy import (
    SignalActionTargetWeights,
    SignalToAllocationConfig,
    StrategyPluginConfig,
)
from maestro.config.universe import UniverseConfig, UniversePolicyConfig
from maestro.core.enums import (
    AssetType,
    BrokerProduct,
    Currency,
    ExchangeCode,
    MarketRegion,
    ProfileStage,
    RunMode,
)
from maestro.core.instruments import TradableInstrument
from maestro.core.symbols import is_cash_symbol


class MaestroConfig(StrictConfigModel):
    mode: RunMode = RunMode.PAPER
    profile_stage: ProfileStage | None = Field(default=None, exclude=True)
    portfolio: PortfolioConfig
    strategies: list[StrategyPluginConfig]
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    datahub: DataHubConfig = Field(default_factory=DataHubConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    state: StateConfig
    audit: AuditConfig
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    kis: KISConfig = Field(default_factory=KISConfig)
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_monitoring_config(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        values = dict(data)
        execution = values.get("execution")
        if not isinstance(execution, dict):
            return values
        legacy_keys = [
            key
            for key in ("heartbeat_max_age_seconds", "scheduled_run_max_age_seconds")
            if key in execution
        ]
        if not legacy_keys:
            return values
        if "monitoring" in values:
            raise ValueError(
                "monitoring cannot be mixed with legacy execution monitoring fields: "
                + ", ".join(legacy_keys)
            )
        execution_values = dict(execution)
        values["execution"] = execution_values
        values["monitoring"] = {key: execution_values.pop(key) for key in legacy_keys}
        return values

    @model_validator(mode="after")
    def validate_universe_matches_portfolio(self) -> "MaestroConfig":
        configured_symbols = self.portfolio.configured_symbols()
        if self.universe.instruments or _all_cash_symbols(configured_symbols):
            self.universe.instruments = _add_default_cash_instruments(
                self.universe.instruments,
                configured_symbols,
                base_currency=self.portfolio.base_currency,
            )
        if not self.universe.instruments:
            if not self.portfolio.allowed_symbols:
                raise ValueError(
                    "portfolio.allowed_symbols is required when universe.instruments is empty"
                )
            return self
        universe_symbol_list = [instrument.symbol for instrument in self.universe.instruments]
        self.portfolio.allowed_symbols = self.portfolio.derive_allowed_symbols(universe_symbol_list)
        self.datahub.apply_universe_symbol_maps(self.universe.instruments)
        universe_symbols = set(universe_symbol_list)
        missing = [
            symbol for symbol in self.portfolio.allowed_symbols if symbol not in universe_symbols
        ]
        if missing:
            raise ValueError(
                "portfolio.allowed_symbols must be present in universe.instruments: "
                + ", ".join(missing)
            )
        if self.portfolio.allocation_mode == "currency_sleeves":
            if not self.portfolio.currency_sleeves:
                raise ValueError("currency_sleeves allocation mode requires currency_sleeves")
            sleeve_symbols = set()
            for currency, sleeve in self.portfolio.currency_sleeves.items():
                sleeve_symbols.add(sleeve.cash_symbol)
                sleeve_symbols.update(sleeve.symbols)
                if currency not in self.portfolio.cash_by_currency:
                    raise ValueError(
                        "cash_by_currency must include every currency_sleeves key: " + currency
                    )
            missing_sleeve_symbols = sorted(sleeve_symbols - set(self.portfolio.allowed_symbols))
            if missing_sleeve_symbols:
                raise ValueError(
                    "currency_sleeves symbols must be present in portfolio.allowed_symbols: "
                    + ", ".join(missing_sleeve_symbols)
                )
        return self

    @model_validator(mode="after")
    def validate_mode_contract(self) -> "MaestroConfig":
        enabled_strategies = [strategy.id for strategy in self.strategies if strategy.enabled]
        if self.mode == RunMode.PAPER and self.portfolio.initial_cash is None:
            raise ValueError("paper mode requires portfolio.initial_cash")
        if self.mode in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
            if self.portfolio.initial_cash is not None:
                raise ValueError(
                    f"{self.mode.value} mode uses broker snapshot cash; "
                    "remove portfolio.initial_cash"
                )
        if self.kis.provider == "kis" and self.kis.token_cache_path is None:
            self.kis.token_cache_path = "var/kis_access_token.json"
        if self.mode == RunMode.LIVE_READONLY:
            if enabled_strategies:
                raise ValueError(
                    "live_readonly mode does not run strategies; disable: "
                    + ", ".join(enabled_strategies)
                )
            if self.approval.enabled or self.approval.require_approval:
                raise ValueError("live_readonly mode must not require approval")
            if self.execution.order_posture != "disabled":
                raise ValueError("live_readonly mode requires execution.order_posture=disabled")
            if not self.kis.enabled:
                raise ValueError("live_readonly mode requires kis.enabled=true")
        if self.mode == RunMode.LIVE_APPROVAL:
            if not self.approval.enabled or not self.approval.require_approval:
                raise ValueError("live_approval mode requires approval.enabled=true")
            if not self.kis.enabled:
                raise ValueError("live_approval mode requires kis.enabled=true")
        if self.mode == RunMode.PAPER and self.execution.order_posture == "armed":
            raise ValueError("paper mode must not arm live order execution")
        expected_stage = self._derive_profile_stage()
        if self.profile_stage is not None and self.profile_stage != expected_stage:
            raise ValueError(
                f"profile_stage={self.profile_stage.value} conflicts with config; "
                f"expected {expected_stage.value}"
            )
        self.profile_stage = expected_stage
        return self

    def _derive_profile_stage(self) -> ProfileStage:
        if self.mode == RunMode.PAPER:
            if _uses_real_datahub(self.datahub):
                return ProfileStage.PAPER_REAL_DATA
            return ProfileStage.PAPER
        if self.mode == RunMode.LIVE_READONLY:
            return ProfileStage.LIVE_READONLY
        if self.kis.paper_trading:
            return ProfileStage.KIS_PAPER_TRADING
        if self.execution.order_posture == "armed":
            return ProfileStage.PRODUCTION_ARMED
        if self.execution.order_posture == "dry_run":
            return ProfileStage.LIVE_APPROVAL_DRY_RUN
        return ProfileStage.LIVE_APPROVAL_DISABLED


__all__ = [
    "ApprovalConfig",
    "BrokerValidationConfig",
    "ContributionConfig",
    "AuditConfig",
    "CurrencySleeveConfig",
    "DataHubConfig",
    "DataHubProviderConfig",
    "ExecutionConfig",
    "KISConfig",
    "LiveOrderLimitsConfig",
    "MaestroConfig",
    "MarketSessionConfig",
    "MonitoringConfig",
    "PortfolioConfig",
    "ProfileStage",
    "ReconciliationConfig",
    "RiskConfig",
    "SignalActionTargetWeights",
    "SignalToAllocationConfig",
    "StateConfig",
    "StrategyPluginConfig",
    "StrictConfigModel",
    "UniverseConfig",
    "UniversePolicyConfig",
]


def _add_default_cash_instruments(
    instruments: list[TradableInstrument],
    configured_symbols: list[str],
    *,
    base_currency: str,
) -> list[TradableInstrument]:
    known_symbols = {instrument.symbol for instrument in instruments}
    derived = list(instruments)
    for symbol in configured_symbols:
        if symbol in known_symbols or not is_cash_symbol(symbol):
            continue
        instrument = _default_cash_instrument(symbol, base_currency=base_currency)
        if instrument is None:
            continue
        derived.append(instrument)
        known_symbols.add(symbol)
    return derived


def _default_cash_instrument(
    symbol: str,
    *,
    base_currency: str,
) -> TradableInstrument | None:
    currency = _cash_currency(symbol, base_currency)
    if currency == Currency.KRW:
        return TradableInstrument(
            symbol=symbol,
            asset_type=AssetType.CASH,
            region=MarketRegion.KR,
            currency=Currency.KRW,
            broker="kis",
            broker_product=BrokerProduct.KIS_DOMESTIC_STOCK,
            broker_symbol=Currency.KRW.value,
            exchange_code=ExchangeCode.KRX,
            quantity_step=1.0,
            price_tick=1.0,
            min_order_quantity=1.0,
            min_order_notional=0.0,
        )
    if currency == Currency.USD:
        return TradableInstrument(
            symbol=symbol,
            asset_type=AssetType.CASH,
            region=MarketRegion.US,
            currency=Currency.USD,
            broker="kis",
            broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
            broker_symbol=Currency.USD.value,
            quantity_step=0.01,
            price_tick=0.01,
            min_order_quantity=0.01,
            min_order_notional=0.0,
        )
    return None


def _cash_currency(symbol: str, base_currency: str) -> Currency | None:
    if symbol == "CASH":
        currency_code = base_currency
    else:
        currency_code = symbol.removeprefix("CASH_")
    try:
        return Currency(currency_code)
    except ValueError:
        return None


def _all_cash_symbols(symbols: list[str]) -> bool:
    return bool(symbols) and all(is_cash_symbol(symbol) for symbol in symbols)


def _uses_real_datahub(config: DataHubConfig) -> bool:
    local_or_synthetic = {"mock", "csv"}
    return any(
        provider.enabled and provider.provider not in local_or_synthetic
        for provider in config.effective_providers()
    )

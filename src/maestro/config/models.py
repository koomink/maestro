from pydantic import Field, model_validator

from maestro.config.approval import ApprovalConfig
from maestro.config.base import StrictConfigModel
from maestro.config.broker import KISConfig
from maestro.config.datahub import DataHubConfig, DataHubProviderConfig
from maestro.config.execution import ContributionConfig, ExecutionConfig
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
from maestro.core.enums import RunMode


class MaestroConfig(StrictConfigModel):
    mode: RunMode = RunMode.PAPER
    portfolio: PortfolioConfig
    strategies: list[StrategyPluginConfig]
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    datahub: DataHubConfig = Field(default_factory=DataHubConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig
    state: StateConfig
    audit: AuditConfig
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    kis: KISConfig = Field(default_factory=KISConfig)
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)

    @model_validator(mode="after")
    def validate_universe_matches_portfolio(self) -> "MaestroConfig":
        if not self.universe.instruments:
            if not self.portfolio.allowed_symbols:
                raise ValueError(
                    "portfolio.allowed_symbols is required when universe.instruments is empty"
                )
            return self
        universe_symbol_list = [instrument.symbol for instrument in self.universe.instruments]
        self.portfolio.allowed_symbols = self.portfolio.derive_allowed_symbols(universe_symbol_list)
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
        if self.mode == RunMode.LIVE_READONLY:
            if enabled_strategies:
                raise ValueError(
                    "live_readonly mode does not run strategies; disable: "
                    + ", ".join(enabled_strategies)
                )
            if self.approval.enabled or self.approval.require_approval:
                raise ValueError("live_readonly mode must not require approval")
            if self.execution.live_order_enabled or self.execution.live_order_dry_run:
                raise ValueError("live_readonly mode must not enable live order execution")
            if not self.kis.enabled:
                raise ValueError("live_readonly mode requires kis.enabled=true")
        if self.mode == RunMode.LIVE_APPROVAL:
            if not self.approval.enabled or not self.approval.require_approval:
                raise ValueError("live_approval mode requires approval.enabled=true")
            if not self.kis.enabled:
                raise ValueError("live_approval mode requires kis.enabled=true")
        if self.mode == RunMode.PAPER and self.execution.live_order_enabled:
            raise ValueError("paper mode must not enable live order execution")
        return self


__all__ = [
    "ApprovalConfig",
    "ContributionConfig",
    "AuditConfig",
    "CurrencySleeveConfig",
    "DataHubConfig",
    "DataHubProviderConfig",
    "ExecutionConfig",
    "KISConfig",
    "MaestroConfig",
    "PortfolioConfig",
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

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
            return self
        universe_symbols = {instrument.symbol for instrument in self.universe.instruments}
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

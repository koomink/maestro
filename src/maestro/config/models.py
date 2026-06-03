from typing import Any

from pydantic import Field, model_validator

from maestro.config.approval import ApprovalConfig
from maestro.config.base import StrictConfigModel
from maestro.config.broker import BrokerAccountConfig, KISConfig
from maestro.config.datahub import DataHubConfig, DataHubProviderConfig
from maestro.config.execution import (
    BrokerValidationConfig,
    ContributionConfig,
    ExecutionConfig,
    ExecutionSleevesConfig,
    LiveOrderLimitsConfig,
    MarketSessionConfig,
)
from maestro.config.monitoring_config import MonitoringConfig
from maestro.config.multi_account_contributions import (
    MultiAccountContributionGroupConfig,
)
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
    app_fragment_paths: list[str] = Field(default_factory=list)
    app_fragment_recommendations: dict[str, Any] = Field(default_factory=dict, exclude=True)
    app_fragment_strategy_recommendations: dict[str, Any] = Field(
        default_factory=dict, exclude=True
    )
    strategy_account_map_path: str | None = None
    broker_accounts_path: str | None = None
    portfolio: PortfolioConfig
    strategies: list[StrategyPluginConfig]
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    datahub: DataHubConfig = Field(default_factory=DataHubConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    execution_sleeves: ExecutionSleevesConfig = Field(default_factory=ExecutionSleevesConfig)
    multi_account_contributions: dict[str, MultiAccountContributionGroupConfig] = Field(
        default_factory=dict
    )
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    state: StateConfig
    audit: AuditConfig
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    kis: KISConfig = Field(default_factory=KISConfig)
    accounts: list[BrokerAccountConfig] = Field(default_factory=list)
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
        self._derive_legacy_accounts()
        self._validate_account_mappings()
        self._validate_execution_sleeves()
        self._validate_multi_account_contributions()
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
            if self.approval.enabled or self.approval.require_approval:
                raise ValueError("live_readonly mode must not require approval")
            if self.execution.order_posture != "disabled":
                raise ValueError("live_readonly mode requires execution.order_posture=disabled")
            if not self._has_enabled_account():
                raise ValueError("live_readonly mode requires an enabled broker account")
        if self.mode == RunMode.LIVE_APPROVAL:
            if not self.approval.enabled or not self.approval.require_approval:
                raise ValueError("live_approval mode requires approval.enabled=true")
            if not self._has_enabled_account():
                raise ValueError("live_approval mode requires an enabled broker account")
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

    def _derive_legacy_accounts(self) -> None:
        if self.accounts or not self.kis.enabled:
            return
        default_account_id = "default_kis"
        self.accounts = [
            BrokerAccountConfig(
                id=default_account_id,
                broker="kis",
                environment="paper_trading" if self.kis.paper_trading else "real",
                enabled=True,
                provider=self.kis.provider,
                broker_product=self.kis.broker_product,
                broker_products=list(self.kis.broker_products),
                account_id=self.kis.account_id,
                account_id_env=self.kis.account_id_env,
                app_key_env=self.kis.app_key_env,
                app_secret_env=self.kis.app_secret_env,
                access_token_env=self.kis.access_token_env,
                approval_key_env=self.kis.approval_key_env,
                token_cache_path=self.kis.token_cache_path,
                base_url=self.kis.base_url,
                timeout_seconds=self.kis.timeout_seconds,
                quote_market_code=self.kis.quote_market_code,
            )
        ]
        self.strategies = [
            strategy.model_copy(update={"account_id": strategy.account_id or default_account_id})
            for strategy in self.strategies
        ]

    def _validate_account_mappings(self) -> None:
        account_ids = [account.id for account in self.accounts]
        duplicate_ids = sorted(
            {account_id for account_id in account_ids if account_ids.count(account_id) > 1}
        )
        if duplicate_ids:
            raise ValueError("accounts.id must be unique: " + ", ".join(duplicate_ids))
        enabled_accounts = {account.id for account in self.accounts if account.enabled}
        account_by_id = {account.id: account for account in self.accounts if account.enabled}
        postures_by_account: dict[str, set[str]] = {}
        for strategy in self.strategies:
            if not strategy.enabled:
                continue
            if (
                self.mode == RunMode.LIVE_APPROVAL
                and strategy.signal_enabled
                and not strategy.account_id
                and strategy.id not in self._multi_account_contribution_strategy_ids()
            ):
                raise ValueError(
                    f"live_approval strategy {strategy.id} requires strategy.account_id"
                )
            if not strategy.account_id:
                continue
            if strategy.account_id not in enabled_accounts:
                raise ValueError(
                    f"strategy {strategy.id} references unknown or disabled account_id: "
                    f"{strategy.account_id}"
                )
            account = account_by_id[strategy.account_id]
            if account.broker == "sandbox" and strategy.order_posture == "armed":
                raise ValueError(
                    f"strategy {strategy.id} uses sandbox account with order_posture=armed"
                )
            if strategy.signal_enabled:
                postures_by_account.setdefault(strategy.account_id, set()).add(
                    strategy.order_posture or self._effective_strategy_order_posture(strategy)
                )
        mixed_accounts = sorted(
            account_id for account_id, postures in postures_by_account.items() if len(postures) > 1
        )
        if mixed_accounts:
            raise ValueError(
                "strategies mapped to one account must not use mixed order_posture: "
                + ", ".join(mixed_accounts)
            )

    def _effective_strategy_order_posture(self, strategy: StrategyPluginConfig) -> str:
        posture = strategy.order_posture or self.execution.order_posture
        if self.execution.order_posture == "disabled":
            return "disabled"
        if self.execution.order_posture == "dry_run" and posture == "armed":
            return "dry_run"
        return posture

    def _validate_execution_sleeves(self) -> None:
        if not self.execution_sleeves.has_sleeves():
            return
        active_sleeves_by_account: dict[str, set[str]] = {}
        for strategy in self.strategies:
            if not strategy.enabled or not strategy.signal_enabled:
                continue
            if not strategy.account_id:
                continue
            if not strategy.execution_sleeve:
                raise ValueError(
                    f"strategy {strategy.id} requires execution_sleeve when "
                    "execution_sleeves are configured"
                )
            account_sleeves = self.execution_sleeves.account_sleeves(strategy.account_id)
            if strategy.execution_sleeve not in account_sleeves:
                raise ValueError(
                    f"strategy {strategy.id} references unknown execution_sleeve "
                    f"{strategy.execution_sleeve} for account_id {strategy.account_id}"
                )
            active_sleeves_by_account.setdefault(strategy.account_id, set()).add(
                strategy.execution_sleeve
            )
        for group in self.multi_account_contributions.values():
            for target in group.account_targets:
                active_sleeves_by_account.setdefault(target.account_id, set()).add(
                    target.execution_sleeve
                )
        for account_id, sleeve_ids in active_sleeves_by_account.items():
            total = sum(
                self.execution_sleeves.accounts[account_id][sleeve_id].target_weight
                for sleeve_id in sleeve_ids
            )
            if abs(total - 1.0) > 1e-6:
                raise ValueError(
                    "execution_sleeves target_weight for active sleeves must sum to 1.0 "
                    f"for account_id {account_id}: {total}"
                )

    def _validate_multi_account_contributions(self) -> None:
        if not self.multi_account_contributions:
            return
        strategy_ids = [strategy.id for strategy in self.strategies]
        strategy_id_set = set(strategy_ids)
        enabled_accounts = {account.id for account in self.accounts if account.enabled}
        portfolio_symbols = set(self.portfolio.allowed_symbols)
        grouped_strategy_ids: dict[str, str] = {}
        for group_id, group in self.multi_account_contributions.items():
            if group.strategy_id not in strategy_id_set:
                raise ValueError(
                    f"multi_account_contributions {group_id} references unknown "
                    f"strategy_id: {group.strategy_id}"
                )
            previous_group = grouped_strategy_ids.get(group.strategy_id)
            if previous_group is not None:
                raise ValueError(
                    "multi_account_contributions duplicate strategy_id: "
                    f"{group.strategy_id} in {previous_group}, {group_id}"
                )
            grouped_strategy_ids[group.strategy_id] = group_id
            for target in group.account_targets:
                if target.account_id not in enabled_accounts:
                    raise ValueError(
                        f"multi_account_contributions {group_id} references unknown "
                        f"or disabled account_id: {target.account_id}"
                    )
                sleeve = self.execution_sleeves.sleeve(
                    target.account_id,
                    target.execution_sleeve,
                )
                if sleeve is None:
                    raise ValueError(
                        f"multi_account_contributions {group_id} references unknown "
                        f"execution_sleeve {target.execution_sleeve} for account_id "
                        f"{target.account_id}"
                    )
                if sleeve.order_generation_mode != group.order_generation_mode:
                    raise ValueError(
                        f"multi_account_contributions {group_id} target "
                        f"{target.account_id}/{target.execution_sleeve} uses "
                        f"order_generation_mode {sleeve.order_generation_mode}"
                    )
                if sleeve.contribution is None or not sleeve.contribution.enabled:
                    raise ValueError(
                        f"multi_account_contributions {group_id} target "
                        f"{target.account_id}/{target.execution_sleeve} requires "
                        "contribution.enabled=true"
                    )
                unsupported_symbols = sorted(set(target.allowed_symbols) - portfolio_symbols)
                if unsupported_symbols:
                    raise ValueError(
                        f"multi_account_contributions {group_id} has unsupported "
                        "allowed_symbols: " + ", ".join(unsupported_symbols)
                    )

    def _multi_account_contribution_strategy_ids(self) -> set[str]:
        return {group.strategy_id for group in self.multi_account_contributions.values()}

    def execution_sleeve_for_strategy(self, strategy: StrategyPluginConfig):
        return self.execution_sleeves.sleeve(strategy.account_id, strategy.execution_sleeve)

    def effective_execution_config_for_strategy(
        self, strategy: StrategyPluginConfig
    ) -> ExecutionConfig:
        sleeve = self.execution_sleeve_for_strategy(strategy)
        if sleeve is None:
            return self.execution
        values = self.execution.model_dump(mode="python")
        values["order_generation_mode"] = sleeve.order_generation_mode
        if sleeve.contribution is not None:
            values["contribution"] = sleeve.contribution.model_dump(mode="python")
        return ExecutionConfig.model_validate(values)

    def effective_strategy_order_generation_mode(self, strategy: StrategyPluginConfig) -> str:
        group = self.multi_account_contribution_group_for_strategy(strategy.id)
        if group is not None:
            return group.order_generation_mode
        return self.effective_execution_config_for_strategy(strategy).order_generation_mode

    def multi_account_contribution_group_for_strategy(
        self,
        strategy_id: str,
    ) -> MultiAccountContributionGroupConfig | None:
        for group in self.multi_account_contributions.values():
            if group.strategy_id == strategy_id:
                return group
        return None

    def _has_enabled_account(self) -> bool:
        return any(account.enabled for account in self.accounts) or self.kis.enabled

    def _derive_profile_stage(self) -> ProfileStage:
        if self.mode == RunMode.PAPER:
            if _uses_real_datahub(self.datahub):
                return ProfileStage.PAPER_REAL_DATA
            return ProfileStage.PAPER
        if self.mode == RunMode.LIVE_READONLY:
            return ProfileStage.LIVE_READONLY
        if any(
            account.enabled and account.environment == "paper_trading" for account in self.accounts
        ):
            return ProfileStage.KIS_PAPER_TRADING
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
    "BrokerAccountConfig",
    "CurrencySleeveConfig",
    "DataHubConfig",
    "DataHubProviderConfig",
    "ExecutionConfig",
    "ExecutionSleevesConfig",
    "KISConfig",
    "LiveOrderLimitsConfig",
    "MaestroConfig",
    "MarketSessionConfig",
    "MonitoringConfig",
    "MultiAccountContributionGroupConfig",
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

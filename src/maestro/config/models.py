from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from maestro.core.enums import AssetType, OrderType, RunMode, StrategyMode


class StrictConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PortfolioConfig(StrictConfigModel):
    base_currency: str = "KRW"
    initial_cash: float = Field(gt=0)
    allowed_symbols: list[str]


class StrategyPluginConfig(StrictConfigModel):
    id: str
    enabled: bool = True
    mode: StrategyMode = StrategyMode.PAPER
    weight: float = Field(ge=0.0)
    entrypoint: str
    config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("entrypoint")
    @classmethod
    def validate_entrypoint(cls, value: str) -> str:
        if ":" not in value:
            raise ValueError("entrypoint must use 'module:ClassName' format")
        return value


class DataHubProviderConfig(StrictConfigModel):
    name: str
    provider: str
    priority: int = Field(default=100, ge=0)
    enabled: bool = True
    data_types: list[str] | None = None
    symbols: list[str] | None = None
    asset_types: list[AssetType] | None = None
    run_modes: list[RunMode] | None = None
    csv_path: str | None = None
    timeout_seconds: float = Field(default=10.0, gt=0)
    stale_after_seconds: int | None = Field(default=None, gt=0)
    symbol_map: dict[str, str] = Field(default_factory=dict)
    api_key_env: str | None = None
    feed_urls: list[str] = Field(default_factory=list)
    source_map: dict[str, str] = Field(default_factory=dict)
    sentiment_texts: list[str] = Field(default_factory=list)
    source_name: str | None = None


class DataHubConfig(StrictConfigModel):
    provider: str = "mock"
    csv_path: str | None = None
    providers: list[DataHubProviderConfig] = Field(default_factory=list)
    timeout_seconds: float = Field(default=10.0, gt=0)
    stale_after_seconds: int | None = Field(default=None, gt=0)
    symbol_map: dict[str, str] = Field(default_factory=dict)
    api_key_env: str | None = None
    feed_urls: list[str] = Field(default_factory=list)
    source_map: dict[str, str] = Field(default_factory=dict)
    sentiment_texts: list[str] = Field(default_factory=list)
    source_name: str | None = None


class ExecutionConfig(StrictConfigModel):
    engine: str = "paper"
    live_order_enabled: bool = False
    require_reconciliation_pass: bool = True
    max_live_order_notional: float = Field(default=0.0, ge=0.0)
    max_daily_live_notional: float = Field(default=0.0, ge=0.0)
    allowed_order_type: OrderType = OrderType.LIMIT
    order_status_poll_interval_seconds: float = Field(default=30.0, ge=0.0)
    order_status_max_polls: int = Field(default=20, gt=0)
    order_status_terminal_timeout_seconds: float = Field(default=1800.0, ge=0.0)

    @field_validator("allowed_order_type")
    @classmethod
    def validate_allowed_order_type(cls, value: OrderType) -> OrderType:
        if value != OrderType.LIMIT:
            raise ValueError("allowed_order_type must be limit")
        return value


class RiskConfig(StrictConfigModel):
    max_single_asset_weight: float = Field(gt=0.0, le=1.0)
    min_cash_weight: float = Field(ge=0.0, le=1.0)


class StateConfig(StrictConfigModel):
    sqlite_path: str


class AuditConfig(StrictConfigModel):
    jsonl_path: str


class ApprovalConfig(StrictConfigModel):
    enabled: bool = False
    provider: str = "console"
    require_approval: bool = False
    default_decision: str = "approved"
    timeout_seconds: int = Field(default=300, gt=0)
    whitelisted_user_ids: list[int] = Field(default_factory=list)
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_allowed_chat_ids: list[int] = Field(default_factory=list)
    telegram_poll_interval_seconds: float = Field(default=1.0, ge=0)

    @field_validator("default_decision")
    @classmethod
    def validate_default_decision(cls, value: str) -> str:
        if value not in {"approved", "rejected", "expired"}:
            raise ValueError("default_decision must be approved, rejected, or expired")
        return value


class KISConfig(StrictConfigModel):
    enabled: bool = False
    provider: str = "mock"
    account_id: str | None = None
    app_key_env: str = "KIS_APP_KEY"
    app_secret_env: str = "KIS_APP_SECRET"
    access_token_env: str = "KIS_ACCESS_TOKEN"
    token_cache_path: str | None = None
    base_url: str | None = None
    paper_trading: bool = False
    timeout_seconds: float = Field(default=10.0, gt=0)
    quote_market_code: str = "J"

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        if self.paper_trading:
            return "https://openapivts.koreainvestment.com:29443"
        return "https://openapi.koreainvestment.com:9443"


class ReconciliationConfig(StrictConfigModel):
    cash_tolerance: float = Field(default=0.0, ge=0.0)
    position_quantity_tolerance: float = Field(default=0.0, ge=0.0)
    value_tolerance: float = Field(default=0.0, ge=0.0)


class MaestroConfig(StrictConfigModel):
    mode: RunMode = RunMode.PAPER
    portfolio: PortfolioConfig
    strategies: list[StrategyPluginConfig]
    datahub: DataHubConfig = Field(default_factory=DataHubConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig
    state: StateConfig
    audit: AuditConfig
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)
    kis: KISConfig = Field(default_factory=KISConfig)
    reconciliation: ReconciliationConfig = Field(default_factory=ReconciliationConfig)

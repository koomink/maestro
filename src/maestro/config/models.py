from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from maestro.core.enums import RunMode, StrategyMode


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


class DataHubConfig(StrictConfigModel):
    provider: str = "mock"
    csv_path: str | None = None


class ExecutionConfig(StrictConfigModel):
    engine: str = "paper"


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
    base_url: str | None = None


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

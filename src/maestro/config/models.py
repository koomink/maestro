from typing import Any

from pydantic import BaseModel, Field, field_validator

from maestro.core.enums import RunMode, StrategyMode


class PortfolioConfig(BaseModel):
    base_currency: str = "KRW"
    initial_cash: float = Field(gt=0)
    allowed_symbols: list[str]


class StrategyPluginConfig(BaseModel):
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


class DataHubConfig(BaseModel):
    provider: str = "mock"
    csv_path: str | None = None


class ExecutionConfig(BaseModel):
    engine: str = "paper"


class RiskConfig(BaseModel):
    max_single_asset_weight: float = Field(gt=0.0, le=1.0)
    min_cash_weight: float = Field(ge=0.0, le=1.0)


class StateConfig(BaseModel):
    sqlite_path: str


class AuditConfig(BaseModel):
    jsonl_path: str


class ApprovalConfig(BaseModel):
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


class MaestroConfig(BaseModel):
    mode: RunMode = RunMode.PAPER
    portfolio: PortfolioConfig
    strategies: list[StrategyPluginConfig]
    datahub: DataHubConfig = Field(default_factory=DataHubConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    risk: RiskConfig
    state: StateConfig
    audit: AuditConfig
    approval: ApprovalConfig = Field(default_factory=ApprovalConfig)

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from maestro.core.enums import AssetType, RunMode, StrategyMode


class StrategyManifest(BaseModel):
    sdk_contract_version: str = "1.0"
    strategy_id: str
    name: str
    version: str
    description: str | None = None
    author: str | None = None
    supported_modes: list[StrategyMode]
    supported_asset_types: list[AssetType]
    result_type: Literal["target_allocation", "strategy_signal"] = "target_allocation"
    requires_data: list[str] = Field(default_factory=list)
    default_time_horizon: str | None = None
    can_run_live: bool = False
    can_use_leverage: bool = False
    can_short: bool = False
    supports_dynamic_universe: bool = False
    max_candidate_symbols: int | None = Field(default=None, ge=0)
    allowed_data_types: list[str] = Field(default_factory=list)
    requires_llm: bool = False
    supported_llm_providers: list[str] = Field(default_factory=list)
    required_env_vars: list[str] = Field(default_factory=list)
    estimated_runtime_seconds: int | None = Field(default=None, ge=0)
    allow_direct_external_data_calls: bool = False


class StrategyContext(BaseModel):
    cycle_id: str
    timestamp: datetime
    run_mode: RunMode
    strategy_id: str
    portfolio_state: dict[str, Any] | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class DataRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    data_type: str
    intended_use: Literal["research", "tradable"] = "research"
    timeframe: str | None = None
    lookback: int | None = None
    start: datetime | None = None
    end: datetime | None = None
    as_of: datetime | None = None
    indicator: str | None = None
    limit: int | None = Field(default=None, ge=0)
    query: str | None = None
    statement_type: Literal["balance_sheet", "cashflow", "cash_flow", "income_statement"] | None = (
        None
    )
    frequency: Literal["annual", "quarterly", "trailing"] | None = None
    provider_hint: str | None = None
    source_hint: str | None = None
    fields: list[str] = Field(default_factory=list)


class CandidateInstrumentRequest(BaseModel):
    symbol: str
    asset_type: AssetType
    intended_use: Literal["research", "tradable"] = "research"
    data_types: list[str] = Field(default_factory=lambda: ["price"])
    currency: str | None = None
    region: str | None = None
    broker_product: str | None = None
    exchange_code: str | None = None
    broker_symbol: str | None = None
    reason: str | None = None


class DataBundle(BaseModel):
    requests: list[DataRequest]
    data: dict[str, Any]
    generated_at: datetime
    source: str


class TargetAllocationResult(BaseModel):
    strategy_id: str
    strategy_version: str
    timestamp: datetime
    allocations: dict[str, float] = Field(default_factory=dict)
    allocation_sleeves: dict[str, dict[str, float]] | None = None
    strategy_books: list["StrategyBookAllocation"] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    time_horizon: str | None = None
    rationale: str | None = None
    risk_flags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StrategyBookAllocation(BaseModel):
    book_id: str
    label: str | None = None
    target_weight: float = Field(ge=0.0)
    allocations: dict[str, float] = Field(default_factory=dict)
    rationale: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StrategySignalResult(BaseModel):
    strategy_id: str
    strategy_version: str
    timestamp: datetime
    symbol: str
    action: Literal["buy", "hold", "sell"]
    rating: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    price_target: float | None = None
    stop_loss: float | None = None
    time_horizon: str | None = None
    position_sizing: str | None = None
    rationale: str | None = None
    risk_flags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


StrategyResult = TargetAllocationResult | StrategySignalResult

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from maestro.core.enums import AssetType, RunMode, StrategyMode


class StrategyManifest(BaseModel):
    strategy_id: str
    name: str
    version: str
    description: str | None = None
    author: str | None = None
    supported_modes: list[StrategyMode]
    supported_asset_types: list[AssetType]
    result_type: Literal["target_allocation"] = "target_allocation"
    requires_data: list[str] = Field(default_factory=list)
    default_time_horizon: str | None = None
    can_run_live: bool = False
    can_use_leverage: bool = False
    can_short: bool = False


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
    timeframe: str | None = None
    lookback: int | None = None
    fields: list[str] = Field(default_factory=list)


class DataBundle(BaseModel):
    requests: list[DataRequest]
    data: dict[str, Any]
    generated_at: datetime
    source: str


class TargetAllocationResult(BaseModel):
    strategy_id: str
    strategy_version: str
    timestamp: datetime
    allocations: dict[str, float]
    confidence: float = Field(ge=0.0, le=1.0)
    time_horizon: str | None = None
    rationale: str | None = None
    risk_flags: list[str] = Field(default_factory=list)

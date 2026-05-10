from pydantic import Field

from maestro.config.base import StrictConfigModel
from maestro.core.enums import AssetType, RunMode


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


__all__ = ["DataHubConfig", "DataHubProviderConfig"]

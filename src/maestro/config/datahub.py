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
    retry_max_attempts: int = Field(default=1, ge=1)
    retry_backoff_seconds: float = Field(default=0.0, ge=0.0)
    cache_ttl_seconds: int | None = Field(default=None, gt=0)
    min_request_interval_seconds: float = Field(default=0.0, ge=0.0)
    symbol_map: dict[str, str] = Field(default_factory=dict)
    api_key_env: str | None = None
    feed_urls: list[str] = Field(default_factory=list)
    source_map: dict[str, str] = Field(default_factory=dict)
    sentiment_texts: list[str] = Field(default_factory=list)
    source_name: str | None = None
    gdelt_base_url: str = "https://api.gdeltproject.org/api/v2/doc/doc"
    gdelt_timespan: str = "24h"
    gdelt_max_records: int = Field(default=25, ge=1, le=250)
    newsapi_base_url: str = "https://newsapi.org/v2/everything"
    newsapi_api_key_env: str = "NEWSAPI_API_KEY"
    newsapi_page_size: int = Field(default=25, ge=1, le=100)
    newsapi_sort_by: str = "publishedAt"
    newsapi_language: str | None = None
    newsapi_search_in: str | None = None
    newsapi_domains: list[str] = Field(default_factory=list)
    newsapi_exclude_domains: list[str] = Field(default_factory=list)
    newsapi_sources: list[str] = Field(default_factory=list)


class DataHubConfig(StrictConfigModel):
    provider: str = "mock"
    csv_path: str | None = None
    providers: list[DataHubProviderConfig] = Field(default_factory=list)
    timeout_seconds: float = Field(default=10.0, gt=0)
    stale_after_seconds: int | None = Field(default=None, gt=0)
    retry_max_attempts: int = Field(default=1, ge=1)
    retry_backoff_seconds: float = Field(default=0.0, ge=0.0)
    cache_ttl_seconds: int | None = Field(default=None, gt=0)
    min_request_interval_seconds: float = Field(default=0.0, ge=0.0)
    symbol_map: dict[str, str] = Field(default_factory=dict)
    api_key_env: str | None = None
    feed_urls: list[str] = Field(default_factory=list)
    source_map: dict[str, str] = Field(default_factory=dict)
    sentiment_texts: list[str] = Field(default_factory=list)
    source_name: str | None = None
    gdelt_base_url: str = "https://api.gdeltproject.org/api/v2/doc/doc"
    gdelt_timespan: str = "24h"
    gdelt_max_records: int = Field(default=25, ge=1, le=250)
    newsapi_base_url: str = "https://newsapi.org/v2/everything"
    newsapi_api_key_env: str = "NEWSAPI_API_KEY"
    newsapi_page_size: int = Field(default=25, ge=1, le=100)
    newsapi_sort_by: str = "publishedAt"
    newsapi_language: str | None = None
    newsapi_search_in: str | None = None
    newsapi_domains: list[str] = Field(default_factory=list)
    newsapi_exclude_domains: list[str] = Field(default_factory=list)
    newsapi_sources: list[str] = Field(default_factory=list)


__all__ = ["DataHubConfig", "DataHubProviderConfig"]

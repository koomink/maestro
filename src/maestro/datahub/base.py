from abc import ABC, abstractmethod
from typing import Any

from maestro.config.models import DataHubConfig, DataHubProviderConfig
from maestro.sdk import DataBundle, DataRequest

YAHOO_DATA_TYPES = {"price", "ohlcv", "fundamental", "financial_statements"}
TECHNICAL_DATA_TYPES = {"technical_indicators"}


class BaseDataProvider(ABC):
    @abstractmethod
    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        raise NotImplementedError


def build_data_provider(config: DataHubConfig) -> BaseDataProvider:
    if config.providers:
        from maestro.datahub.registry import DataHubRegistry
        from maestro.datahub.router import DataHubRouter

        registry = DataHubRegistry()
        for provider_config in config.providers:
            if provider_config.enabled:
                _register_configured_provider(registry, provider_config)
        return DataHubRouter(registry)

    if config.provider == "mock":
        from maestro.datahub.mock_provider import MockDataHub
        from maestro.datahub.registry import DataHubRegistry
        from maestro.datahub.router import DataHubRouter

        registry = DataHubRegistry()
        registry.register("mock", MockDataHub(), {"price", "ohlcv"})
        return DataHubRouter(registry)
    if config.provider == "csv":
        from maestro.datahub.csv_provider import CSVDataProvider
        from maestro.datahub.registry import DataHubRegistry
        from maestro.datahub.router import DataHubRouter

        if not config.csv_path:
            raise ValueError("datahub.csv_path is required when provider is 'csv'")
        registry = DataHubRegistry()
        registry.register("csv", CSVDataProvider(config.csv_path), {"price", "ohlcv"})
        return DataHubRouter(registry)
    if config.provider in {"yahoo", "yfinance"}:
        from maestro.datahub.registry import DataHubRegistry
        from maestro.datahub.router import DataHubRouter
        from maestro.datahub.technical_provider import TechnicalIndicatorProvider
        from maestro.datahub.yahoo_provider import YahooDataProvider

        registry = DataHubRegistry()
        yahoo_provider = YahooDataProvider(
            timeout_seconds=config.timeout_seconds,
            stale_after_seconds=config.stale_after_seconds,
            symbol_map=config.symbol_map,
        )
        registry.register(
            "yahoo",
            yahoo_provider,
            YAHOO_DATA_TYPES,
        )
        registry.register(
            "technical",
            TechnicalIndicatorProvider(ohlcv_provider=yahoo_provider),
            TECHNICAL_DATA_TYPES,
        )
        return DataHubRouter(registry)
    if config.provider == "fred":
        from maestro.datahub.fred_provider import FREDDataProvider
        from maestro.datahub.registry import DataHubRegistry
        from maestro.datahub.router import DataHubRouter

        registry = DataHubRegistry()
        registry.register(
            "fred",
            FREDDataProvider(
                api_key_env=config.api_key_env,
                timeout_seconds=config.timeout_seconds,
                stale_after_seconds=config.stale_after_seconds,
                symbol_map=config.symbol_map,
            ),
            {"macro"},
        )
        return DataHubRouter(registry)
    if config.provider == "rss":
        from maestro.datahub.registry import DataHubRegistry
        from maestro.datahub.router import DataHubRouter
        from maestro.datahub.rss_provider import RSSNewsProvider

        registry = DataHubRegistry()
        registry.register(
            "rss",
            RSSNewsProvider(
                feed_urls=config.feed_urls,
                timeout_seconds=config.timeout_seconds,
                stale_after_seconds=config.stale_after_seconds,
                symbol_map=config.symbol_map,
                source_map=config.source_map,
            ),
            {"news"},
        )
        return DataHubRouter(registry)
    if config.provider == "sentiment":
        from maestro.datahub.registry import DataHubRegistry
        from maestro.datahub.router import DataHubRouter
        from maestro.datahub.sentiment_provider import RuleBasedSentimentProvider

        registry = DataHubRegistry()
        registry.register(
            "sentiment",
            RuleBasedSentimentProvider(
                texts=config.sentiment_texts,
                timeout_seconds=config.timeout_seconds,
                stale_after_seconds=config.stale_after_seconds,
                symbol_map=config.symbol_map,
                source_name=config.source_name,
            ),
            {"sentiment"},
        )
        return DataHubRouter(registry)
    raise ValueError(f"Unsupported datahub provider: {config.provider}")


def _register_configured_provider(registry: Any, config: DataHubProviderConfig) -> None:
    data_types = set(config.data_types or ["price", "ohlcv"])
    if config.provider == "mock":
        from maestro.datahub.mock_provider import MockDataHub

        registry.register(
            config.name,
            MockDataHub(),
            data_types,
            priority=config.priority,
            symbols=set(config.symbols) if config.symbols is not None else None,
            asset_types=set(config.asset_types) if config.asset_types is not None else None,
            run_modes=set(config.run_modes) if config.run_modes is not None else None,
        )
        return

    if config.provider == "csv":
        from maestro.datahub.csv_provider import CSVDataProvider

        csv_path = config.csv_path
        if not csv_path:
            raise ValueError("datahub.providers[].csv_path is required when provider is 'csv'")
        registry.register(
            config.name,
            CSVDataProvider(csv_path),
            data_types,
            priority=config.priority,
            symbols=set(config.symbols) if config.symbols is not None else None,
            asset_types=set(config.asset_types) if config.asset_types is not None else None,
            run_modes=set(config.run_modes) if config.run_modes is not None else None,
        )
        return

    if config.provider in {"yahoo", "yfinance"}:
        from maestro.datahub.technical_provider import TechnicalIndicatorProvider
        from maestro.datahub.yahoo_provider import YahooDataProvider

        supported = _validate_yahoo_family_data_types(data_types)
        yahoo_data_types = supported & YAHOO_DATA_TYPES
        technical_data_types = supported & TECHNICAL_DATA_TYPES
        yahoo_provider = YahooDataProvider(
            timeout_seconds=config.timeout_seconds,
            stale_after_seconds=config.stale_after_seconds,
            symbol_map=config.symbol_map,
        )
        if yahoo_data_types:
            registry.register(
                config.name,
                yahoo_provider,
                yahoo_data_types,
                priority=config.priority,
                symbols=set(config.symbols) if config.symbols is not None else None,
                asset_types=set(config.asset_types) if config.asset_types is not None else None,
                run_modes=set(config.run_modes) if config.run_modes is not None else None,
            )
        if technical_data_types:
            registry.register(
                f"{config.name}_technical",
                TechnicalIndicatorProvider(ohlcv_provider=yahoo_provider),
                technical_data_types,
                priority=config.priority,
                symbols=set(config.symbols) if config.symbols is not None else None,
                asset_types=set(config.asset_types) if config.asset_types is not None else None,
                run_modes=set(config.run_modes) if config.run_modes is not None else None,
            )
        return

    if config.provider == "fred":
        from maestro.datahub.fred_provider import FREDDataProvider

        fred_data_types = set(config.data_types or ["macro"])
        registry.register(
            config.name,
            FREDDataProvider(
                api_key_env=config.api_key_env,
                timeout_seconds=config.timeout_seconds,
                stale_after_seconds=config.stale_after_seconds,
                symbol_map=config.symbol_map,
            ),
            _validate_fred_data_types(fred_data_types),
            priority=config.priority,
            symbols=set(config.symbols) if config.symbols is not None else None,
            asset_types=set(config.asset_types) if config.asset_types is not None else None,
            run_modes=set(config.run_modes) if config.run_modes is not None else None,
        )
        return

    if config.provider == "rss":
        from maestro.datahub.rss_provider import RSSNewsProvider

        rss_data_types = set(config.data_types or ["news"])
        registry.register(
            config.name,
            RSSNewsProvider(
                feed_urls=config.feed_urls,
                timeout_seconds=config.timeout_seconds,
                stale_after_seconds=config.stale_after_seconds,
                symbol_map=config.symbol_map,
                source_map=config.source_map,
            ),
            _validate_rss_data_types(rss_data_types),
            priority=config.priority,
            symbols=set(config.symbols) if config.symbols is not None else None,
            asset_types=set(config.asset_types) if config.asset_types is not None else None,
            run_modes=set(config.run_modes) if config.run_modes is not None else None,
        )
        return

    if config.provider == "sentiment":
        from maestro.datahub.sentiment_provider import RuleBasedSentimentProvider

        sentiment_data_types = set(config.data_types or ["sentiment"])
        registry.register(
            config.name,
            RuleBasedSentimentProvider(
                texts=config.sentiment_texts,
                timeout_seconds=config.timeout_seconds,
                stale_after_seconds=config.stale_after_seconds,
                symbol_map=config.symbol_map,
                source_name=config.source_name,
            ),
            _validate_sentiment_data_types(sentiment_data_types),
            priority=config.priority,
            symbols=set(config.symbols) if config.symbols is not None else None,
            asset_types=set(config.asset_types) if config.asset_types is not None else None,
            run_modes=set(config.run_modes) if config.run_modes is not None else None,
        )
        return

    raise ValueError(f"Unsupported datahub provider: {config.provider}")


def _validate_yahoo_family_data_types(data_types: set[str]) -> set[str]:
    unsupported = data_types - YAHOO_DATA_TYPES - TECHNICAL_DATA_TYPES
    if unsupported:
        raise ValueError(
            "Yahoo provider supports only price, ohlcv, fundamental, "
            "financial_statements, and technical_indicators: "
            f"{sorted(unsupported)}"
        )
    return data_types


def _validate_fred_data_types(data_types: set[str]) -> set[str]:
    unsupported = data_types - {"macro"}
    if unsupported:
        raise ValueError(f"FRED provider supports only macro: {sorted(unsupported)}")
    return data_types


def _validate_rss_data_types(data_types: set[str]) -> set[str]:
    unsupported = data_types - {"news"}
    if unsupported:
        raise ValueError(f"RSS provider supports only news: {sorted(unsupported)}")
    return data_types


def _validate_sentiment_data_types(data_types: set[str]) -> set[str]:
    unsupported = data_types - {"sentiment"}
    if unsupported:
        raise ValueError(f"Sentiment provider supports only sentiment: {sorted(unsupported)}")
    return data_types

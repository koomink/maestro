from abc import ABC, abstractmethod

from maestro.config.models import DataHubConfig
from maestro.sdk import DataBundle, DataRequest


class BaseDataProvider(ABC):
    @abstractmethod
    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        raise NotImplementedError


def build_data_provider(config: DataHubConfig) -> BaseDataProvider:
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
    raise ValueError(f"Unsupported datahub provider: {config.provider}")

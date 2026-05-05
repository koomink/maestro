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

        return MockDataHub()
    if config.provider == "csv":
        from maestro.datahub.csv_provider import CSVDataProvider

        if not config.csv_path:
            raise ValueError("datahub.csv_path is required when provider is 'csv'")
        return CSVDataProvider(config.csv_path)
    raise ValueError(f"Unsupported datahub provider: {config.provider}")

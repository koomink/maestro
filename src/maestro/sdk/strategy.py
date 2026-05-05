from abc import ABC, abstractmethod

from maestro.sdk.schemas import (
    DataBundle,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    TargetAllocationResult,
)


class BaseStrategyPlugin(ABC):
    @abstractmethod
    def manifest(self) -> StrategyManifest:
        raise NotImplementedError

    @abstractmethod
    def build_data_requests(self, context: StrategyContext) -> list[DataRequest]:
        raise NotImplementedError

    @abstractmethod
    def run(
        self, data_bundle: DataBundle, context: StrategyContext
    ) -> TargetAllocationResult:
        raise NotImplementedError

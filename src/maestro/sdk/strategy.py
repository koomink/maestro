from abc import ABC, abstractmethod

from maestro.sdk.schemas import (
    CandidateInstrumentRequest,
    DataBundle,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    StrategyResult,
)


class BaseStrategyPlugin(ABC):
    @abstractmethod
    def manifest(self) -> StrategyManifest:
        raise NotImplementedError

    @abstractmethod
    def build_data_requests(self, context: StrategyContext) -> list[DataRequest]:
        raise NotImplementedError

    def build_candidate_requests(
        self,
        context: StrategyContext,
    ) -> list[CandidateInstrumentRequest]:
        del context
        return []

    @abstractmethod
    def run(self, data_bundle: DataBundle, context: StrategyContext) -> StrategyResult:
        raise NotImplementedError

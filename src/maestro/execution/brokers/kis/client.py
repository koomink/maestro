from abc import ABC, abstractmethod

from maestro.execution.brokers.kis.models import (
    KISAccountSnapshot,
    KISOrderSummary,
)


class KISReadOnlyClient(ABC):
    @abstractmethod
    def get_account_snapshot(self) -> KISAccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        raise NotImplementedError

    @abstractmethod
    def get_daily_orders(self) -> list[KISOrderSummary]:
        raise NotImplementedError

    @abstractmethod
    def get_unfilled_orders(self) -> list[KISOrderSummary]:
        raise NotImplementedError

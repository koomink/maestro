from abc import ABC, abstractmethod

from maestro.execution.brokers.kis.models import (
    KISAccountSnapshot,
    KISBuyingPower,
    KISOrderSummary,
    KISPosition,
)


class KISReadOnlyClient(ABC):
    @abstractmethod
    def get_account_snapshot(self) -> KISAccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> list[KISPosition]:
        raise NotImplementedError

    @abstractmethod
    def get_buying_power(
        self,
        symbol: str | None = None,
        order_price: float | None = None,
    ) -> KISBuyingPower:
        raise NotImplementedError

    @abstractmethod
    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        raise NotImplementedError

    @abstractmethod
    def get_order_fills(self) -> list[KISOrderSummary]:
        raise NotImplementedError

    @abstractmethod
    def get_unfilled_orders(self) -> list[KISOrderSummary]:
        raise NotImplementedError

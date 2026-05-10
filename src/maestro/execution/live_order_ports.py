from abc import ABC, abstractmethod

from maestro.execution.live_order_models import (
    BrokerOrderId,
    LiveOrderCancelRequest,
    LiveOrderCancelResult,
    LiveOrderLifecycleNotification,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusSnapshot,
)
from maestro.execution.reconciliation import ReconciliationResult


class LiveOrderClient(ABC):
    @abstractmethod
    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        raise NotImplementedError


class LiveOrderStatusClient(ABC):
    @abstractmethod
    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        raise NotImplementedError


class LiveOrderCancelClient(ABC):
    @abstractmethod
    def cancel_order(self, request: LiveOrderCancelRequest) -> LiveOrderCancelResult:
        raise NotImplementedError


class BrokerReconciliationRunner(ABC):
    @abstractmethod
    def reconcile_latest(self) -> ReconciliationResult:
        raise NotImplementedError


class LiveOrderNotificationClient(ABC):
    @abstractmethod
    def notify(self, event: LiveOrderLifecycleNotification) -> None:
        raise NotImplementedError


__all__ = [
    "BrokerReconciliationRunner",
    "LiveOrderCancelClient",
    "LiveOrderClient",
    "LiveOrderNotificationClient",
    "LiveOrderStatusClient",
]

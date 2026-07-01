from maestro.core.enums import OrderStatus
from maestro.execution.live_order_models import BrokerOrderId, LiveOrderStatusSnapshot
from maestro.execution.live_order_ports import LiveOrderStatusClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore


class LiveOrderStatusService:
    def __init__(
        self,
        state_store: StateStore,
        audit_logger: AuditLogger,
        status_client: LiveOrderStatusClient,
    ) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.status_client = status_client

    def poll_order_status(
        self,
        run_id: str,
        broker_order_id: BrokerOrderId,
    ) -> LiveOrderStatusSnapshot:
        snapshot = self.status_client.get_order_status(broker_order_id)
        if snapshot.status == OrderStatus.UNKNOWN:
            snapshot = snapshot.model_copy(
                update={
                    "status": OrderStatus.HALTED,
                    "message": "Live order halted because broker returned an unknown order state.",
                }
            )
            save_audited_system_event(
                self.state_store,
                self.audit_logger,
                run_id,
                SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED,
                {
                    "reason": "unknown_broker_order_status",
                    "order_id": broker_order_id.order_id,
                    "broker_order_id": broker_order_id.broker_order_id,
                    "request": {"order_id": broker_order_id.order_id},
                    "result": {
                        "broker_order": broker_order_id.model_dump(mode="json"),
                        "message": snapshot.message,
                        "status": snapshot.status.value,
                        "raw_status": snapshot.raw_status,
                        "raw": snapshot.raw,
                    },
                },
            )
        payload = snapshot.model_dump(mode="json")
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            run_id,
            SystemEventType.LIVE_ORDER_STATUS,
            payload,
        )
        return snapshot


__all__ = ["LiveOrderStatusService"]

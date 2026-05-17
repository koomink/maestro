from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import BrokerProduct, Currency, OrderStatus, OrderType
from maestro.core.instruments import TradableInstrument
from maestro.execution.live_order_models import LiveOrderRequest, LiveOrderResult
from maestro.execution.live_order_ports import LiveOrderClient, LiveOrderPreSubmitValidator
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore


class LiveOrderSafetyService:
    def __init__(
        self,
        config: ExecutionConfig,
        state_store: StateStore,
        audit_logger: AuditLogger,
        broker_client: LiveOrderClient,
        instruments: list[TradableInstrument] | None = None,
        broker_product: BrokerProduct | None = None,
        broker_products: list[BrokerProduct] | None = None,
        base_currency: Currency | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.broker_client = broker_client
        self.instruments = {instrument.symbol: instrument for instrument in instruments or []}
        self.broker_product = broker_product
        self.broker_products = broker_products or ([broker_product] if broker_product else [])
        self.base_currency = base_currency

    def submit_approved_order(
        self,
        request: LiveOrderRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderResult:
        self._validate_safety_contract(request, approval_decision)
        if isinstance(self.broker_client, LiveOrderPreSubmitValidator):
            self.broker_client.validate_pre_submit_order(request)
        try:
            result = self.broker_client.submit_limit_order(request)
        except Exception as exc:
            halted = LiveOrderResult(
                order_id=request.order_id,
                status=OrderStatus.HALTED,
                message=(
                    "Live order submission outcome is ambiguous; operator recovery "
                    "is required before another live order."
                ),
                raw={
                    "recovery_required": True,
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
            )
            self._persist(SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED, request, halted)
            return halted
        if result.status == OrderStatus.UNKNOWN:
            halted = LiveOrderResult(
                order_id=request.order_id,
                status=OrderStatus.HALTED,
                broker_order=result.broker_order,
                message="Live order halted because broker returned an unknown order state.",
                raw=result.raw,
            )
            self._persist(SystemEventType.LIVE_ORDER_HALT, request, halted)
            return halted
        if result.broker_order is not None and self._broker_order_seen(
            result.broker_order.broker_order_id
        ):
            halted = result.model_copy(
                update={
                    "status": OrderStatus.HALTED,
                    "message": (
                        "Live order submission returned a duplicate broker order ID; "
                        "operator recovery is required before another live order."
                    ),
                    "raw": {
                        **result.raw,
                        "recovery_required": True,
                        "duplicate_broker_order_id": result.broker_order.broker_order_id,
                    },
                }
            )
            self._persist(SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED, request, halted)
            return halted
        self._persist(SystemEventType.LIVE_ORDER_RESULT, request, result)
        return result

    def _validate_safety_contract(
        self,
        request: LiveOrderRequest,
        approval_decision: ApprovalDecision,
    ) -> None:
        if not self.config.live_order_enabled:
            raise ValueError("Live order submission is disabled")
        if approval_decision.approval_id != request.approval_id:
            raise ValueError("Approval decision does not match the live order request")
        if approval_decision.run_id != request.run_id:
            raise ValueError("Approval decision run_id does not match the live order request")
        if approval_decision.status != "approved":
            raise ValueError("Telegram approval is required before live order submission")
        if not approval_decision.decided_by.startswith("telegram:"):
            raise ValueError("Telegram approval is required before live order submission")
        if request.order_type != OrderType.LIMIT:
            raise ValueError("Live order submission is limit-order only")
        if self.config.allowed_order_type != OrderType.LIMIT:
            raise ValueError("Configured live order type must be limit")
        limits = self.config.live_order_limits
        if request.notional > limits.max_order_notional:
            raise ValueError("Live order notional exceeds per-order cap")
        if self._daily_live_notional() + request.notional > limits.max_daily_notional:
            raise ValueError("Live order notional exceeds daily cap")
        if limits.max_daily_order_count > 0:
            if self._daily_live_order_count() + 1 > limits.max_daily_order_count:
                raise ValueError("Live order count exceeds daily cap")
        self._validate_instrument_contract(request)
        if self._is_duplicate(request):
            raise ValueError("Duplicate live order request rejected")
        if self.config.require_reconciliation_pass:
            latest = self.state_store.load_latest_system_event(
                SystemEventType.BROKER_RECONCILIATION
            )
            if latest is None or latest["payload"].get("passed") is not True:
                raise ValueError(
                    "Latest broker reconciliation must pass before live order submission"
                )

    def _validate_instrument_contract(self, request: LiveOrderRequest) -> None:
        if not self.instruments:
            return
        instrument = self.instruments.get(request.symbol)
        if instrument is None:
            raise ValueError(f"Live order symbol is not in universe: {request.symbol}")
        if request.currency is not None and instrument.currency != request.currency:
            raise ValueError("Live order currency does not match request currency")
        if self.base_currency is not None and len(self.broker_products) <= 1:
            if instrument.currency != self.base_currency:
                raise ValueError("Live order currency does not match portfolio base currency")
        if self.broker_products and instrument.broker_product not in self.broker_products:
            raise ValueError("Live order broker product is not enabled for KIS")
        if (
            self.broker_product is not None
            and not self.broker_products
            and instrument.broker_product != self.broker_product
        ):
            raise ValueError("Live order broker product does not match KIS adapter product")
        if request.quantity < instrument.min_order_quantity:
            raise ValueError("Live order quantity is below instrument minimum")
        if request.notional < instrument.min_order_notional:
            raise ValueError("Live order notional is below instrument minimum")
        if not _is_step_multiple(request.quantity, instrument.quantity_step):
            raise ValueError("Live order quantity does not match instrument quantity_step")
        if not _is_step_multiple(request.limit_price, instrument.price_tick):
            raise ValueError("Live order limit price does not match instrument price_tick")

    def _persist(
        self,
        event_type: SystemEventType | str,
        request: LiveOrderRequest,
        result: LiveOrderResult,
    ) -> None:
        payload = {
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "duplicate_key": _duplicate_key(request),
            "notional": request.notional,
            "submitted_date": _live_order_date(),
        }
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            request.run_id,
            event_type,
            payload,
        )

    def _daily_live_notional(self) -> float:
        today = _live_order_date()
        total = 0.0
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_RESULT, limit=1000
        ):
            payload = row["payload"]
            if payload.get("submitted_date") == today:
                total += float(payload.get("notional", 0.0))
        return total

    def _daily_live_order_count(self) -> int:
        today = _live_order_date()
        count = 0
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_RESULT, limit=1000
        ):
            if row["payload"].get("submitted_date") == today:
                count += 1
        return count

    def _is_duplicate(self, request: LiveOrderRequest) -> bool:
        key = _duplicate_key(request)
        for event_type in (
            SystemEventType.LIVE_ORDER_RESULT,
            SystemEventType.LIVE_ORDER_HALT,
            SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED,
        ):
            for row in self.state_store.list_system_events_by_type(event_type, limit=1000):
                if row["payload"].get("duplicate_key") == key:
                    return True
        return False

    def _broker_order_seen(self, broker_order_id: str) -> bool:
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_RESULT, limit=1000
        ):
            broker_order = row["payload"].get("result", {}).get("broker_order") or {}
            if broker_order.get("broker_order_id") == broker_order_id:
                return True
        return False


def _duplicate_key(request: LiveOrderRequest) -> str:
    if request.duplicate_key:
        return request.duplicate_key
    submitted_minute = utc_now().strftime("%Y%m%d%H%M")
    return (
        f"{request.approval_id}:{request.symbol}:{request.side}:"
        f"{request.quantity}:{request.limit_price}:{submitted_minute}"
    )


def _live_order_date() -> str:
    return utc_now().date().isoformat()


def _is_step_multiple(value: float, step: float) -> bool:
    scaled = value / step
    return abs(scaled - round(scaled)) < 1e-9


__all__ = ["LiveOrderSafetyService"]

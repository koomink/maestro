from collections.abc import Callable

from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import BrokerProduct, Currency, OrderStatus, OrderType
from maestro.core.instruments import TradableInstrument
from maestro.core.trading_day import trading_day_bounds_utc_str
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
        broker_products: list[BrokerProduct] | None = None,
        base_currency: Currency | None = None,
        account_attribution_validator: Callable[[LiveOrderRequest], None] | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.broker_client = broker_client
        self.instruments = {instrument.symbol: instrument for instrument in instruments or []}
        self.broker_products = broker_products or []
        self.base_currency = base_currency
        self.account_attribution_validator = account_attribution_validator

    def submit_approved_order(
        self,
        request: LiveOrderRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderResult:
        # Serialize the whole check -> submit -> persist sequence so daily-cap and
        # duplicate checks are atomic with respect to concurrent live submissions.
        with self.state_store.live_order_lock("submit_approved_order"):
            return self._submit_approved_order_locked(request, approval_decision)

    def _submit_approved_order_locked(
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
                signal_run_id=request.signal_run_id,
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
                signal_run_id=request.signal_run_id,
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
        if request.signal_run_id and result.signal_run_id is None:
            result = result.model_copy(update={"signal_run_id": request.signal_run_id})
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
        order_limit_currency = (
            self._request_currency(request) if limits.max_order_notional_by_currency else None
        )
        max_order_notional = limits.max_order_notional_for(order_limit_currency)
        if max_order_notional is None:
            raise ValueError("Live order currency is missing a per-order cap")
        if request.notional > max_order_notional:
            raise ValueError("Live order notional exceeds per-order cap")
        daily_limit_currency = (
            self._request_currency(request) if limits.max_daily_notional_by_currency else None
        )
        max_daily_notional = limits.max_daily_notional_for(daily_limit_currency)
        if max_daily_notional is None:
            raise ValueError("Live order currency is missing a daily cap")
        if self._daily_live_notional(daily_limit_currency) + request.notional > max_daily_notional:
            raise ValueError("Live order notional exceeds daily cap")
        if limits.max_daily_order_count > 0:
            if self._daily_live_order_count() + 1 > limits.max_daily_order_count:
                raise ValueError("Live order count exceeds daily cap")
        self._validate_instrument_contract(request)
        if self.account_attribution_validator is not None:
            self.account_attribution_validator(request)
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
            "signal_run_id": request.signal_run_id,
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

    def _today_utc_bounds(self) -> tuple[str, str]:
        return trading_day_bounds_utc_str(self.config.market_session.timezone)

    def _daily_live_results(self) -> list[dict]:
        start_utc, end_utc = self._today_utc_bounds()
        return self.state_store.list_system_events_in_range(
            SystemEventType.LIVE_ORDER_RESULT,
            start_utc=start_utc,
            end_utc=end_utc,
        )

    def _daily_live_notional(self, currency: Currency | None = None) -> float:
        total = 0.0
        for row in self._daily_live_results():
            payload = row["payload"]
            if currency is None or self._payload_currency(payload) == currency:
                total += float(payload.get("notional", 0.0))
        return total

    def _request_currency(self, request: LiveOrderRequest) -> Currency:
        if request.currency is not None:
            return request.currency
        instrument = self.instruments.get(request.symbol)
        if instrument is not None:
            return instrument.currency
        if self.base_currency is not None:
            return self.base_currency
        raise ValueError("Live order currency is required for currency-specific limits")

    def _payload_currency(self, payload: dict) -> Currency:
        request = payload.get("request")
        if isinstance(request, dict) and request.get("currency"):
            return Currency(str(request["currency"]))
        if payload.get("currency"):
            return Currency(str(payload["currency"]))
        if self.base_currency is not None:
            return self.base_currency
        return Currency.KRW

    def _daily_live_order_count(self) -> int:
        return len(self._daily_live_results())

    def _is_duplicate(self, request: LiveOrderRequest) -> bool:
        return self.state_store.duplicate_key_exists(_duplicate_key(request))

    def _broker_order_seen(self, broker_order_id: str) -> bool:
        return self.state_store.broker_order_id_seen(broker_order_id)


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

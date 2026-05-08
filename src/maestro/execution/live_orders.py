from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, model_validator

from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus, OrderType
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


class BrokerOrderId(BaseModel):
    broker: str
    broker_order_id: str
    broker_order_org_no: str | None = None
    order_id: str
    submitted_at: str


class LiveOrderRequest(BaseModel):
    order_id: str
    symbol: str
    side: OrderSide
    quantity: float = Field(gt=0)
    limit_price: float = Field(gt=0)
    order_type: OrderType = OrderType.LIMIT
    approval_id: str
    run_id: str
    duplicate_key: str | None = None

    @property
    def notional(self) -> float:
        return self.quantity * self.limit_price

    @model_validator(mode="after")
    def validate_limit_order(self) -> "LiveOrderRequest":
        if self.order_type != OrderType.LIMIT:
            raise ValueError("Live orders must be limit orders")
        return self


class LiveOrderResult(BaseModel):
    order_id: str
    status: OrderStatus
    broker_order: BrokerOrderId | None = None
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    message: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def live_notional(self) -> float:
        if self.average_fill_price is None:
            return 0.0
        return self.filled_quantity * self.average_fill_price


class FillEvent(BaseModel):
    broker_order_id: str
    symbol: str
    quantity: float
    price: float
    filled_at: str
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def notional(self) -> float:
        return self.quantity * self.price


class PartialFillSummary(BaseModel):
    ordered_quantity: float
    filled_quantity: float
    remaining_quantity: float
    average_fill_price: float | None = None
    fill_count: int = 0

    @property
    def filled_notional(self) -> float:
        if self.average_fill_price is None:
            return 0.0
        return self.filled_quantity * self.average_fill_price


class LiveOrderStatusSnapshot(BaseModel):
    broker_order: BrokerOrderId
    status: OrderStatus
    checked_at: str
    symbol: str | None = None
    side: OrderSide | None = None
    partial_fill: PartialFillSummary
    fills: list[FillEvent] = Field(default_factory=list)
    raw_status: str | None = None
    message: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class AppliedFill(BaseModel):
    broker_order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: float
    notional: float
    cumulative_filled_quantity: float
    cumulative_filled_notional: float
    status_checked_at: str


class SkippedFill(BaseModel):
    broker_order_id: str
    status_checked_at: str
    reason: str
    status: OrderStatus


class FillReconciliationResult(BaseModel):
    run_id: str
    checked_at: str
    applied_fills: list[AppliedFill] = Field(default_factory=list)
    skipped_fills: list[SkippedFill] = Field(default_factory=list)
    portfolio_updated: bool = False
    cash: float
    positions: dict[str, float]


class LiveOrderCancelRequest(BaseModel):
    run_id: str
    approval_id: str
    broker_order: BrokerOrderId
    reason: str | None = None


class LiveOrderCancelResult(BaseModel):
    broker_order: BrokerOrderId
    status: OrderStatus
    canceled_quantity: float
    message: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


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
        payload = snapshot.model_dump(mode="json")
        self.state_store.save_system_event(run_id, "live_order_status", payload)
        self.audit_logger.log(run_id, "live_order_status", payload)
        return snapshot


class LiveOrderCancellationService:
    def __init__(
        self,
        state_store: StateStore,
        audit_logger: AuditLogger,
        cancel_client: LiveOrderCancelClient,
    ) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.cancel_client = cancel_client

    def cancel_order(
        self,
        request: LiveOrderCancelRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderCancelResult:
        latest_status = self._validate_cancellation_policy(request, approval_decision)
        result = self.cancel_client.cancel_order(request)
        payload = {
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "latest_status": latest_status.model_dump(mode="json"),
        }
        self.state_store.save_system_event(request.run_id, "live_order_cancel", payload)
        self.audit_logger.log(request.run_id, "live_order_cancel", payload)
        return result

    def _validate_cancellation_policy(
        self,
        request: LiveOrderCancelRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderStatusSnapshot:
        if approval_decision.approval_id != request.approval_id:
            raise ValueError("Approval decision does not match the cancel request")
        if approval_decision.run_id != request.run_id:
            raise ValueError("Approval decision run_id does not match the cancel request")
        if approval_decision.status != "approved":
            raise ValueError("Telegram approval is required before live order cancellation")
        if not approval_decision.decided_by.startswith("telegram:"):
            raise ValueError("Telegram approval is required before live order cancellation")
        if self._is_duplicate_cancel(request.broker_order.broker_order_id):
            raise ValueError("Duplicate live order cancellation rejected")
        latest_reconciliation = self.state_store.load_latest_system_event("broker_reconciliation")
        if (
            latest_reconciliation is None
            or latest_reconciliation["payload"].get("passed") is not True
        ):
            raise ValueError(
                "Latest broker reconciliation must pass before live order cancellation"
            )

        latest_status = self._load_latest_status(request.broker_order.broker_order_id)
        if latest_status is None:
            raise ValueError("Latest live order status is required before cancellation")
        if latest_status.status == OrderStatus.UNKNOWN:
            raise ValueError("Unknown broker state blocks live order cancellation")
        if latest_status.status == OrderStatus.HALTED:
            raise ValueError("Halted live order state blocks cancellation")
        if latest_status.status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
            raise ValueError(
                f"Live order cancellation is forbidden for status={latest_status.status}"
            )
        if latest_status.partial_fill.remaining_quantity <= 0:
            raise ValueError("Live order cancellation requires a remaining open quantity")
        if latest_status.status == OrderStatus.PARTIALLY_FILLED:
            latest_fill_reconciliation = self.state_store.load_latest_system_event(
                "fill_reconciliation"
            )
            if latest_fill_reconciliation is None:
                raise ValueError(
                    "Latest fill reconciliation is required before partial-fill cancellation"
                )
        return latest_status

    def _load_latest_status(self, broker_order_id: str) -> LiveOrderStatusSnapshot | None:
        rows = self.state_store.list_system_events_by_type("live_order_status", limit=1000)
        for row in rows:
            snapshot = LiveOrderStatusSnapshot.model_validate(row["payload"])
            if snapshot.broker_order.broker_order_id == broker_order_id:
                return snapshot
        return None

    def _is_duplicate_cancel(self, broker_order_id: str) -> bool:
        rows = self.state_store.list_system_events_by_type("live_order_cancel", limit=1000)
        for row in rows:
            broker_order = row["payload"].get("request", {}).get("broker_order", {})
            if broker_order.get("broker_order_id") == broker_order_id:
                return True
        return False


class PartialFillReconciliationService:
    def __init__(
        self,
        state_store: StateStore,
        audit_logger: AuditLogger,
    ) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger

    def reconcile_latest(self, run_id: str, *, limit: int = 1000) -> FillReconciliationResult:
        current_state = self.state_store.load_latest_portfolio_state()
        next_state = current_state.model_copy(deep=True)
        applied_by_order = self._load_applied_watermarks()
        applied_fills: list[AppliedFill] = []
        skipped_fills: list[SkippedFill] = []

        rows = self.state_store.list_system_events_by_type("live_order_status", limit=limit)
        for row in reversed(rows):
            snapshot = LiveOrderStatusSnapshot.model_validate(row["payload"])
            applied_fill, skipped_fill = _fill_delta(snapshot, applied_by_order)
            if skipped_fill is not None:
                skipped_fills.append(skipped_fill)
                continue
            if applied_fill is None:
                continue
            _apply_fill(next_state, applied_fill)
            applied_by_order[applied_fill.broker_order_id] = (
                applied_fill.cumulative_filled_quantity,
                applied_fill.cumulative_filled_notional,
            )
            applied_fills.append(applied_fill)

        if applied_fills:
            self.state_store.save_portfolio_snapshot(run_id, next_state)

        result = FillReconciliationResult(
            run_id=run_id,
            checked_at=utc_now().isoformat(),
            applied_fills=applied_fills,
            skipped_fills=skipped_fills,
            portfolio_updated=bool(applied_fills),
            cash=next_state.cash,
            positions=next_state.positions,
        )
        payload = result.model_dump(mode="json")
        self.state_store.save_system_event(run_id, "fill_reconciliation", payload)
        self.audit_logger.log(run_id, "fill_reconciliation", payload)
        return result

    def _load_applied_watermarks(self) -> dict[str, tuple[float, float]]:
        applied: dict[str, tuple[float, float]] = {}
        rows = self.state_store.list_system_events_by_type("fill_reconciliation", limit=1000)
        for row in reversed(rows):
            for item in row["payload"].get("applied_fills", []):
                broker_order_id = str(item.get("broker_order_id") or "")
                if not broker_order_id:
                    continue
                applied[broker_order_id] = (
                    float(item.get("cumulative_filled_quantity", 0.0)),
                    float(item.get("cumulative_filled_notional", 0.0)),
                )
        return applied


class LiveOrderSafetyService:
    def __init__(
        self,
        config: ExecutionConfig,
        state_store: StateStore,
        audit_logger: AuditLogger,
        broker_client: LiveOrderClient,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.broker_client = broker_client

    def submit_approved_order(
        self,
        request: LiveOrderRequest,
        approval_decision: ApprovalDecision,
    ) -> LiveOrderResult:
        self._validate_safety_contract(request, approval_decision)
        result = self.broker_client.submit_limit_order(request)
        if result.status == OrderStatus.UNKNOWN:
            halted = LiveOrderResult(
                order_id=request.order_id,
                status=OrderStatus.HALTED,
                broker_order=result.broker_order,
                message="Live order halted because broker returned an unknown order state.",
                raw=result.raw,
            )
            self._persist("live_order_halt", request, halted)
            return halted
        self._persist("live_order_result", request, result)
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
        if request.notional > self.config.max_live_order_notional:
            raise ValueError("Live order notional exceeds per-order cap")
        if self._daily_live_notional() + request.notional > self.config.max_daily_live_notional:
            raise ValueError("Live order notional exceeds daily cap")
        if self._is_duplicate(request):
            raise ValueError("Duplicate live order request rejected")
        if self.config.require_reconciliation_pass:
            latest = self.state_store.load_latest_system_event("broker_reconciliation")
            if latest is None or latest["payload"].get("passed") is not True:
                raise ValueError(
                    "Latest broker reconciliation must pass before live order submission"
                )

    def _persist(
        self,
        event_type: str,
        request: LiveOrderRequest,
        result: LiveOrderResult,
    ) -> None:
        payload = {
            "request": request.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "duplicate_key": _duplicate_key(request),
            "notional": request.notional,
            "submitted_date": date.today().isoformat(),
        }
        self.state_store.save_system_event(request.run_id, event_type, payload)
        self.audit_logger.log(request.run_id, event_type, payload)

    def _daily_live_notional(self) -> float:
        today = date.today().isoformat()
        total = 0.0
        for row in self.state_store.list_system_events_by_type("live_order_result", limit=1000):
            payload = row["payload"]
            if payload.get("submitted_date") == today:
                total += float(payload.get("notional", 0.0))
        return total

    def _is_duplicate(self, request: LiveOrderRequest) -> bool:
        key = _duplicate_key(request)
        for event_type in ("live_order_result", "live_order_halt"):
            for row in self.state_store.list_system_events_by_type(event_type, limit=1000):
                if row["payload"].get("duplicate_key") == key:
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


def _fill_delta(
    snapshot: LiveOrderStatusSnapshot,
    applied_by_order: dict[str, tuple[float, float]],
) -> tuple[AppliedFill | None, SkippedFill | None]:
    broker_order_id = snapshot.broker_order.broker_order_id
    if snapshot.status == OrderStatus.UNKNOWN:
        return None, _skipped(snapshot, "unknown_broker_state")
    if snapshot.status not in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
        return None, _skipped(snapshot, "order_status_not_fill_reconcilable")
    if snapshot.symbol is None:
        return None, _skipped(snapshot, "missing_symbol")
    if snapshot.side is None:
        return None, _skipped(snapshot, "missing_side")

    total_quantity = snapshot.partial_fill.filled_quantity
    total_notional = snapshot.partial_fill.filled_notional
    applied_quantity, applied_notional = applied_by_order.get(broker_order_id, (0.0, 0.0))
    delta_quantity = total_quantity - applied_quantity
    delta_notional = total_notional - applied_notional

    if delta_quantity <= 0 or delta_notional < 0:
        return None, _skipped(snapshot, "duplicate_or_no_new_fill")

    return (
        AppliedFill(
            broker_order_id=broker_order_id,
            symbol=snapshot.symbol,
            side=snapshot.side,
            quantity=delta_quantity,
            price=delta_notional / delta_quantity,
            notional=delta_notional,
            cumulative_filled_quantity=total_quantity,
            cumulative_filled_notional=total_notional,
            status_checked_at=snapshot.checked_at,
        ),
        None,
    )


def _skipped(snapshot: LiveOrderStatusSnapshot, reason: str) -> SkippedFill:
    return SkippedFill(
        broker_order_id=snapshot.broker_order.broker_order_id,
        status_checked_at=snapshot.checked_at,
        reason=reason,
        status=snapshot.status,
    )


def _apply_fill(state: PortfolioState, fill: AppliedFill) -> None:
    signed_quantity = fill.quantity if fill.side == OrderSide.BUY else -fill.quantity
    signed_notional = fill.notional if fill.side == OrderSide.BUY else -fill.notional
    state.cash -= signed_notional
    next_quantity = state.positions.get(fill.symbol, 0.0) + signed_quantity
    if abs(next_quantity) < 1e-12:
        state.positions.pop(fill.symbol, None)
    else:
        state.positions[fill.symbol] = next_quantity

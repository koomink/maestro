from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from pydantic import BaseModel, Field, model_validator

from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus, OrderType
from maestro.monitoring.audit_logger import AuditLogger
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


class LiveOrderClient(ABC):
    @abstractmethod
    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        raise NotImplementedError


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

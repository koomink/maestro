from typing import Any

from pydantic import BaseModel, Field, model_validator

from maestro.core.enums import BrokerProduct, Currency, OrderSide, OrderStatus, OrderType


class BrokerOrderId(BaseModel):
    broker: str
    broker_order_id: str
    broker_order_org_no: str | None = None
    order_id: str
    submitted_at: str
    account_id: str | None = None
    broker_product: BrokerProduct | None = None
    parent_broker_order_id: str | None = None


class BrokerOrderRequest(BaseModel):
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float | None = Field(default=None, gt=0)
    order_amount: float | None = Field(default=None, gt=0)
    limit_price: float | None = Field(default=None, gt=0)
    time_in_force: str = "DAY"
    currency: Currency | None = None
    account_id: str | None = None

    @model_validator(mode="after")
    def validate_order_shape(self) -> "BrokerOrderRequest":
        if (self.quantity is None) == (self.order_amount is None):
            raise ValueError("Exactly one of quantity or order_amount is required")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("Limit orders require limit_price")
        if self.order_type == OrderType.MARKET and self.limit_price is not None:
            raise ValueError("Market orders must not include limit_price")
        return self


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
    currency: Currency | None = None
    sleeve: str | None = None
    # The currency sleeve above ("USD"/"KRW") is not the attribution bucket. Fills
    # are attributed by execution sleeve ("crescendo_us"), so carry it separately.
    execution_sleeve: str | None = None
    account_id: str | None = None
    broker_product: BrokerProduct | None = None
    signal_run_id: str | None = None

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
    signal_run_id: str | None = None

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
    currency: str | None = None
    cumulative_filled_amount: float | None = None
    cumulative_commission: float | None = None
    cumulative_tax: float | None = None
    settlement_date: str | None = None
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
    commission: float = 0.0
    tax: float = 0.0
    cumulative_commission: float = 0.0
    cumulative_tax: float = 0.0


class SettlementCashAdjustment(BaseModel):
    account_id: str
    currency: str
    amount: float
    transaction_costs_before: float
    transaction_costs_after: float
    broker_snapshot_id: int | None = None
    broker_order_id: str | None = None
    source: str = "broker_snapshot"


class SkippedFill(BaseModel):
    broker_order_id: str
    status_checked_at: str
    reason: str
    status: OrderStatus


class FillReconciliationResult(BaseModel):
    run_id: str
    checked_at: str
    applied_fills: list[AppliedFill] = Field(default_factory=list)
    settlement_cash_adjustments: list[SettlementCashAdjustment] = Field(default_factory=list)
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


class LiveOrderModifyRequest(BaseModel):
    run_id: str
    approval_id: str
    broker_order: BrokerOrderId
    symbol: str
    limit_price: float = Field(gt=0)
    quantity: float | None = Field(default=None, gt=0)
    currency: Currency | None = None
    reason: str | None = None


class LiveOrderModifyResult(BaseModel):
    broker_order: BrokerOrderId
    status: OrderStatus
    previous_broker_order: BrokerOrderId
    message: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class LiveOrderWorkflowResult(BaseModel):
    run_id: str
    workflow_status: OrderStatus
    order_id: str
    broker_order_id: str | None = None
    submitted_order: LiveOrderResult | None = None
    status_snapshot: LiveOrderStatusSnapshot | None = None
    fill_reconciliation: FillReconciliationResult | None = None
    broker_reconciliation: dict[str, Any] | None = None
    applied_fills: list[AppliedFill] = Field(default_factory=list)
    halt_reason: str | None = None
    failed_reason: str | None = None
    checked_at: str


class LiveOrderLifecycleNotification(BaseModel):
    run_id: str
    order_id: str
    status: OrderStatus
    message: str
    broker_order_id: str | None = None


class LiveOrderLifecycleResult(BaseModel):
    run_id: str
    order_id: str
    final_status: OrderStatus
    broker_order_id: str | None = None
    signal_run_id: str | None = None
    submitted_order: LiveOrderResult | None = None
    poll_count: int = 0
    status_snapshots: list[LiveOrderStatusSnapshot] = Field(default_factory=list)
    applied_fills: list[AppliedFill] = Field(default_factory=list)
    fill_reconciliations: list[FillReconciliationResult] = Field(default_factory=list)
    broker_reconciliations: list[dict[str, Any]] = Field(default_factory=list)
    notifications_sent: list[LiveOrderLifecycleNotification] = Field(default_factory=list)
    max_polls_reached: bool = False
    halt_reason: str | None = None
    failed_reason: str | None = None
    checked_at: str


class LiveOrderBatchItemResult(BaseModel):
    request: LiveOrderRequest
    lifecycle: LiveOrderLifecycleResult


class LiveOrderBatchLifecycleResult(BaseModel):
    run_id: str
    items: list[LiveOrderBatchItemResult] = Field(default_factory=list)
    poll_rounds: int = 0
    max_polls_reached: bool = False
    orders_planned: int = 0
    orders_submitted: int = 0
    orders_accepted: int = 0
    orders_filled: int = 0
    orders_failed: int = 0
    submission_duration_seconds: float = 0.0
    polling_duration_seconds: float = 0.0
    reconciliation_duration_seconds: float = 0.0
    total_duration_seconds: float = 0.0
    checked_at: str


class LiveOrderRecoveryCandidate(BaseModel):
    source_order_id: str
    order: dict[str, Any]
    source_type: str
    reason: str
    signal_run_id: str | None = None
    created_at: str
    status: str = "pending"


__all__ = [
    "AppliedFill",
    "BrokerOrderRequest",
    "BrokerOrderId",
    "FillEvent",
    "FillReconciliationResult",
    "LiveOrderCancelRequest",
    "LiveOrderCancelResult",
    "LiveOrderLifecycleNotification",
    "LiveOrderLifecycleResult",
    "LiveOrderBatchItemResult",
    "LiveOrderBatchLifecycleResult",
    "LiveOrderRecoveryCandidate",
    "LiveOrderModifyRequest",
    "LiveOrderModifyResult",
    "LiveOrderRequest",
    "LiveOrderResult",
    "LiveOrderStatusSnapshot",
    "LiveOrderWorkflowResult",
    "PartialFillSummary",
    "SkippedFill",
]

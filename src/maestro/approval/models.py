from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ApprovalStatus = Literal["approved", "rejected", "expired"]


class ApprovalRequest(BaseModel):
    approval_id: str
    run_id: str
    profile_name: str | None = None
    created_at: datetime
    expires_at: datetime
    channel: str
    source_strategy_ids: list[str] = Field(default_factory=list)
    order_count: int
    estimated_notional: float
    proposed_orders: list[dict]
    risk_violations: list[str] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    approval_id: str
    run_id: str
    status: ApprovalStatus
    decided_at: datetime
    decided_by: str
    reason: str | None = None


class PendingApprovalEnvelope(BaseModel):
    approval_id: str
    run_id: str
    signal_run_id: str
    request: ApprovalRequest
    orders: list[dict[str, Any]]
    message: str
    source_strategy_ids: list[str] = Field(default_factory=list)
    account_ids: list[str] = Field(default_factory=list)
    status: Literal["pending"] = "pending"
    reminder_seconds: list[int] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime
    duplicate_key: str


class ApprovalDispatchResult(BaseModel):
    signal_run_id: str
    run_id: str
    orders_planned: int
    orders_capacity_blocked: int = 0
    approvals_pending: int = 0
    approval_status: str

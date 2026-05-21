from datetime import datetime
from typing import Literal

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

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
    #: 이 승인의 카드를 lifecycle이 소유하는가. 0은 카드 이관 이전에 기록된
    #: envelope다 — 카드가 직접 전송돼 message_id가 남지 않았으므로 sweep이
    #: 손대면 두 번째 카드가 생긴다. 1이면 dispatch가 전송 **전에** intent를
    #: 남기므로, 투영이 비어 있다는 것은 곧 전송이 시작되지도 못했다는 뜻이고
    #: sweep이 안전하게 최초 전송을 대신할 수 있다. 이 구분이 없으면 pending
    #: 저장 직후 죽은 승인이 영원히 운영자에게 닿지 않는다.
    card_delivery_version: int = 0


class ApprovalDispatchResult(BaseModel):
    signal_run_id: str
    run_id: str
    orders_planned: int
    orders_capacity_blocked: int = 0
    approvals_pending: int = 0
    approval_status: str

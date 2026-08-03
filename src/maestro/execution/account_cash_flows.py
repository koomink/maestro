from dataclasses import dataclass
from typing import Any

from maestro.core.ids import new_run_id
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import (
    ACCOUNT_CASH_FLOW_CLASSES,
    EXTERNAL_TRANSFER,
    SystemEventType,
)
from maestro.state.store import StateStore


@dataclass(frozen=True)
class AccountCashFlowRecord:
    run_id: str
    created: bool


def account_cash_flow_leg_duplicate_key(
    transfer_id: str,
    account_id: str,
    currency: str,
    flow_type: str,
) -> str:
    """Idempotency key for one leg of a linked transfer.

    A transfer has two sides -- money leaving one account or currency and
    arriving in another -- and both have to be recorded for the pair to be
    recognised as internal.  Keying on the transfer alone means the second
    leg collides with the first and is silently dropped, leaving a lone
    outbound leg that reads as money leaving the portfolio entirely.
    """
    return (
        f"account-cash-flow:transfer:{transfer_id}:"
        f"{account_id}:{str(currency).upper()}:{str(flow_type).lower()}"
    )


class AccountCashFlowService:
    """Apply operator- or broker-verified external cash flows exactly once."""

    def __init__(self, store: StateStore, audit: AuditLogger) -> None:
        self.store = store
        self.audit = audit

    def record(
        self,
        *,
        account_id: str,
        amount: float,
        currency: str,
        flow_type: str,
        effective_at: str,
        source: str,
        reason: str | None = None,
        decided_by: str | None = None,
        transfer_id: str | None = None,
        proposal_id: str | None = None,
        evidence: dict[str, Any] | None = None,
        verification: str | None = None,
        duplicate_key: str | None = None,
        flow_class: str = EXTERNAL_TRANSFER,
    ) -> AccountCashFlowRecord:
        normalized_type = flow_type.strip().lower()
        if normalized_type not in {"deposit", "withdrawal"}:
            raise ValueError("flow_type must be deposit or withdrawal")
        normalized_class = flow_class.strip().lower()
        if normalized_class not in ACCOUNT_CASH_FLOW_CLASSES:
            raise ValueError(f"flow_class must be one of {sorted(ACCOUNT_CASH_FLOW_CLASSES)}")
        normalized_currency = currency.strip().upper()
        signed_amount = abs(float(amount))
        if normalized_type == "withdrawal":
            signed_amount = -signed_amount
        if signed_amount == 0:
            raise ValueError("cash-flow amount must be non-zero")

        run_id = new_run_id()
        payload = {
            "account_id": account_id,
            "amount": signed_amount,
            "currency": normalized_currency,
            "flow_type": normalized_type,
            "flow_class": normalized_class,
            "effective_at": effective_at,
            "source": source,
            "reason": reason,
            "transfer_id": transfer_id,
            "proposal_id": proposal_id,
            "decided_by": decided_by,
            "verification": verification,
            "evidence": evidence or {},
            "duplicate_key": duplicate_key,
        }
        # The ledger row and the event are written together; the store reports
        # back whether this call was the one that created them.
        result = self.store.apply_account_cash_flows(
            run_id,
            [
                {
                    "account_id": account_id,
                    "amount": signed_amount,
                    "currency": normalized_currency,
                    "event_payload": payload,
                }
            ],
        )
        if not result["ledger_established"]:
            raise ValueError(
                "account ledger is not established; run `maestro ledger open-baseline` first"
            )
        if not result["created"]:
            return AccountCashFlowRecord(run_id=str(result["run_id"]), created=False)
        self.audit.log(run_id, str(SystemEventType.ACCOUNT_CASH_FLOW), payload)
        return AccountCashFlowRecord(run_id=run_id, created=True)

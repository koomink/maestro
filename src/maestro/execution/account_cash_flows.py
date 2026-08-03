from dataclasses import dataclass
from typing import Any

from maestro.core.ids import new_run_id
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore


@dataclass(frozen=True)
class AccountCashFlowRecord:
    run_id: str
    created: bool


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
    ) -> AccountCashFlowRecord:
        normalized_type = flow_type.strip().lower()
        if normalized_type not in {"deposit", "withdrawal"}:
            raise ValueError("flow_type must be deposit or withdrawal")
        normalized_currency = currency.strip().upper()
        signed_amount = abs(float(amount))
        if normalized_type == "withdrawal":
            signed_amount = -signed_amount
        if signed_amount == 0:
            raise ValueError("cash-flow amount must be non-zero")

        with self.store.writer_lock("account_cash_flow"):
            if duplicate_key and self.store.duplicate_key_exists(duplicate_key):
                existing = next(
                    (
                        row
                        for row in self.store.list_system_events_by_type(
                            SystemEventType.ACCOUNT_CASH_FLOW,
                            limit=10_000,
                        )
                        if (row.get("payload") or {}).get("duplicate_key") == duplicate_key
                    ),
                    None,
                )
                return AccountCashFlowRecord(
                    run_id=str((existing or {}).get("run_id") or ""),
                    created=False,
                )
            run_id = new_run_id()
            if not self.store.apply_account_cash_flow(
                run_id,
                account_id,
                signed_amount,
                normalized_currency,
            ):
                raise ValueError(
                    "account ledger is not established; run `maestro ledger open-baseline` first"
                )
            payload = {
                "account_id": account_id,
                "amount": signed_amount,
                "currency": normalized_currency,
                "flow_type": normalized_type,
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
            save_audited_system_event(
                self.store,
                self.audit,
                run_id,
                SystemEventType.ACCOUNT_CASH_FLOW,
                payload,
            )
        return AccountCashFlowRecord(run_id=run_id, created=True)

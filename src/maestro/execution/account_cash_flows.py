from dataclasses import dataclass
from typing import Any

from maestro.core.ids import new_run_id
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import (
    ACCOUNT_CASH_FLOW_CLASSES,
    COST,
    EXTERNAL_TRANSFER,
    FX_CONVERSION,
    SystemEventType,
)
from maestro.state.store import StateStore

# Smallest amount a currency can express, used as the floor on rounding
# tolerance so a check does not fail on sub-unit noise.
_MINOR_UNITS = {"KRW": 1.0, "JPY": 1.0}


def _minor_unit(currency: str) -> float:
    return _MINOR_UNITS.get(currency.upper(), 0.01)


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

    def record_currency_conversion(
        self,
        *,
        account_id: str,
        from_currency: str,
        from_amount: float,
        to_currency: str,
        to_amount: float,
        transfer_id: str,
        effective_at: str,
        source: str,
        fee: float = 0.0,
        rate: float | None = None,
        reason: str | None = None,
        decided_by: str | None = None,
    ) -> AccountCashFlowRecord:
        """Record a currency conversion as linked legs plus its cost.

        ``to_amount`` is what actually arrived and ``fee`` is the spread or
        commission that did not.  They are booked apart because a currency
        sleeve neutralises the conversion itself: if the spread were folded into
        the converted amount, both sleeves would neutralise their whole leg and
        a real cost would vanish from every return.  Booking the fair amount as
        the conversion and the difference as a cost leaves the loss where it was
        incurred.

        ``rate`` is optional and expressed as units of ``to_currency`` per unit
        of ``from_currency``.  When given it is checked against the amounts, so
        a mistyped figure is refused rather than written to the ledger.
        """
        normalized_from = from_currency.strip().upper()
        normalized_to = to_currency.strip().upper()
        if normalized_from == normalized_to:
            raise ValueError("a conversion needs two different currencies")
        if from_amount <= 0 or to_amount <= 0:
            raise ValueError("conversion amounts must be positive")
        if fee < 0:
            raise ValueError("conversion fee cannot be negative")
        if not transfer_id:
            raise ValueError("a conversion needs a transfer_id so a retry is safe")
        converted_amount = to_amount + fee
        if rate is not None:
            expected = from_amount * rate
            tolerance = max(abs(expected) * 1e-6, _minor_unit(normalized_to))
            if abs(expected - converted_amount) > tolerance:
                raise ValueError(
                    f"conversion does not add up: {from_amount} {normalized_from} at "
                    f"{rate} is {expected} {normalized_to}, but to_amount plus fee is "
                    f"{converted_amount} {normalized_to}"
                )

        def leg(currency: str, amount: float, flow_type: str, flow_class: str) -> dict[str, Any]:
            signed = abs(amount) if flow_type == "deposit" else -abs(amount)
            payload = {
                "account_id": account_id,
                "amount": signed,
                "currency": currency,
                "flow_type": flow_type,
                "flow_class": flow_class,
                "effective_at": effective_at,
                "source": source,
                "reason": reason,
                "transfer_id": transfer_id,
                "decided_by": decided_by,
                "verification": "operator_verified",
                "evidence": {
                    "kind": "operator_currency_conversion",
                    "from_currency": normalized_from,
                    "from_amount": from_amount,
                    "to_currency": normalized_to,
                    "to_amount": to_amount,
                    "fee": fee,
                    "rate": rate,
                },
                "duplicate_key": account_cash_flow_leg_duplicate_key(
                    transfer_id, account_id, currency, flow_type
                ),
            }
            return {
                "account_id": account_id,
                "amount": signed,
                "currency": currency,
                "event_payload": payload,
            }

        # Every leg carries the same effective_at: legs that disagree on when
        # they happened land in different performance periods, which reopens the
        # gap that recording them together was meant to close.
        legs = [
            leg(normalized_from, from_amount, "withdrawal", FX_CONVERSION),
            leg(normalized_to, converted_amount, "deposit", FX_CONVERSION),
        ]
        if fee > 0:
            legs.append(leg(normalized_to, fee, "withdrawal", COST))
        return self._apply(legs)

    def _apply(self, legs: list[dict[str, Any]]) -> AccountCashFlowRecord:
        run_id = new_run_id()
        result = self.store.apply_account_cash_flows(run_id, legs)
        if not result["ledger_established"]:
            raise ValueError(
                "account ledger is not established; run `maestro ledger open-baseline` first"
            )
        if not result["created"]:
            return AccountCashFlowRecord(run_id=str(result["run_id"]), created=False)
        for leg in legs:
            self.audit.log(
                run_id,
                str(SystemEventType.ACCOUNT_CASH_FLOW),
                leg["event_payload"],
            )
        return AccountCashFlowRecord(run_id=run_id, created=True)

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
        return self._apply(
            [
                {
                    "account_id": account_id,
                    "amount": signed_amount,
                    "currency": normalized_currency,
                    "event_payload": payload,
                }
            ]
        )

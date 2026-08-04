import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from maestro.state.store import StateStore

_MINIMUM_CHANGE = {"KRW": 1_000.0, "USD": 1.0}
_STABILITY_TOLERANCE = {"KRW": 1.0, "USD": 0.01}
_BLOCKING_EVENT_TYPES = {
    "live_order_result",
    "live_order_lifecycle",
    "live_order_status",
    "live_order_recovery_required",
    "live_order_recovery_completed",
}
# Korean equities settle T+2 and US equities T+1, so a fill up to this many days
# old can still be moving cash today.  Activity inside the observation window is
# already excluded; this covers a fill that happened before it.
_SETTLEMENT_HORIZON_DAYS = 3.0

# Where an account's cash figure comes from.  Toss reports only buying power, a
# proxy that is not settled cash; other brokers report actual deposits.  The
# safety checks are identical either way -- what differs is which number to read.
PROXY_CASH = "proxy"
BROKER_REPORTED_CASH = "broker_reported"


@dataclass(frozen=True)
class CashFlowCandidate:
    fingerprint: str
    account_id: str
    currency: str
    amount: float
    flow_type: str
    cash_basis: str
    baseline_snapshot_id: int
    first_changed_snapshot_id: int
    latest_snapshot_id: int
    stable_snapshot_ids: tuple[int, ...]
    effective_at: str

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": (
                "stable_toss_buying_power_change"
                if self.cash_basis == PROXY_CASH
                else "stable_broker_reported_cash_change"
            ),
            "cash_basis": self.cash_basis,
            "baseline_snapshot_id": self.baseline_snapshot_id,
            "first_changed_snapshot_id": self.first_changed_snapshot_id,
            "latest_snapshot_id": self.latest_snapshot_id,
            "stable_snapshot_ids": list(self.stable_snapshot_ids),
            "positions_unchanged": True,
            "orders_unchanged": True,
            "fills_unchanged": True,
            "blocking_lifecycle_events": False,
            "settled_beyond_horizon_days": _SETTLEMENT_HORIZON_DAYS,
        }


@dataclass(frozen=True)
class FxConversionCandidate:
    fingerprint: str
    account_id: str
    from_currency: str
    from_amount: float
    to_currency: str
    to_amount: float
    cash_basis: str
    baseline_snapshot_id: int
    first_changed_snapshot_id: int
    latest_snapshot_id: int
    stable_snapshot_ids: tuple[int, ...]
    effective_at: str

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": "stable_toss_fx_conversion_change",
            "cash_basis": self.cash_basis,
            "baseline_snapshot_id": self.baseline_snapshot_id,
            "first_changed_snapshot_id": self.first_changed_snapshot_id,
            "latest_snapshot_id": self.latest_snapshot_id,
            "stable_snapshot_ids": list(self.stable_snapshot_ids),
            "positions_unchanged": True,
            "orders_unchanged": True,
            "fills_unchanged": True,
            "paired_opposite_currency_moves": True,
            "operator_confirmation_required": True,
        }


class CashFlowCandidateDetector:
    """Offers an unexplained cash change for operator confirmation.

    The new level has to hold across consecutive snapshots and positions, orders,
    and fills must be unchanged across the window. Single-currency flows also
    reject nearby lifecycle/settlement activity. Paired opposite Toss movements
    are offered as one conversion candidate for explicit operator confirmation;
    neither currency is ever offered as an independent deposit or withdrawal.
    """

    def __init__(self, store: StateStore) -> None:
        self.store = store

    def detect(
        self, account_id: str
    ) -> CashFlowCandidate | FxConversionCandidate | None:
        rows = self.store.list_broker_account_snapshots(limit=100, account_id=account_id)
        if len(rows) < 4:
            return None
        latest_account = _account(rows[0])
        basis = _cash_basis(latest_account)
        if basis is None:
            return None
        candidates: list[CashFlowCandidate] = []
        for currency in sorted(_cash_by_basis(latest_account, basis)):
            candidate = self._detect_currency(
                rows,
                account_id,
                currency,
                basis,
                check_blocking_events=False,
            )
            if candidate is not None:
                candidates.append(candidate)
        if not candidates:
            return None
        conversion = _fx_conversion_candidate(candidates, basis)
        if conversion is not None:
            return conversion

        latest_timestamp = _timestamp(rows[0].get("created_at"))
        if latest_timestamp is not None and _settling_fill_nearby(rows, latest_timestamp):
            # A fill from before the window can still be moving one currency's
            # cash. Paired opposite-currency moves above are offered only for
            # explicit operator confirmation as a conversion.
            return None

        strict_candidates = []
        for currency in sorted(_cash_by_basis(latest_account, basis)):
            candidate = self._detect_currency(
                rows,
                account_id,
                currency,
                basis,
                check_blocking_events=True,
            )
            if candidate is not None:
                strict_candidates.append(candidate)
        if not strict_candidates:
            return None
        if len({candidate.flow_type for candidate in strict_candidates}) > 1:
            return None
        return strict_candidates[0]

    def _detect_currency(
        self,
        rows: list[dict[str, Any]],
        account_id: str,
        currency: str,
        basis: str,
        *,
        check_blocking_events: bool,
    ) -> CashFlowCandidate | None:
        tolerance = _STABILITY_TOLERANCE.get(currency, 0.01)
        latest_value = _cash_by_basis(_account(rows[0]), basis).get(currency)
        if latest_value is None:
            return None
        stable: list[dict[str, Any]] = []
        baseline: dict[str, Any] | None = None
        for row in rows:
            value = _cash_by_basis(_account(row), basis).get(currency)
            if value is None:
                return None
            if abs(value - latest_value) <= tolerance:
                stable.append(row)
                continue
            baseline = row
            break
        if len(stable) < 3 or baseline is None:
            return None
        baseline_value = _cash_by_basis(_account(baseline), basis).get(currency)
        if baseline_value is None:
            return None
        delta = latest_value - baseline_value
        if abs(delta) < _MINIMUM_CHANGE.get(currency, 1.0):
            return None

        window = [*stable, baseline]
        signature = _activity_signature(baseline)
        if any(_activity_signature(row) != signature for row in stable):
            return None
        if check_blocking_events and self._has_blocking_events(
            account_id, baseline, stable[0]
        ):
            return None

        first_changed = stable[-1]
        fingerprint_payload = {
            "account_id": account_id,
            "currency": currency,
            "baseline_snapshot_id": int(baseline["id"]),
            "first_changed_snapshot_id": int(first_changed["id"]),
            "amount": round(delta, 8),
        }
        fingerprint = hashlib.sha256(
            json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
        ).hexdigest()[:24]
        del window
        return CashFlowCandidate(
            fingerprint=fingerprint,
            account_id=account_id,
            currency=currency,
            amount=abs(delta),
            flow_type="deposit" if delta > 0 else "withdrawal",
            baseline_snapshot_id=int(baseline["id"]),
            first_changed_snapshot_id=int(first_changed["id"]),
            latest_snapshot_id=int(stable[0]["id"]),
            stable_snapshot_ids=tuple(int(row["id"]) for row in reversed(stable)),
            effective_at=str(first_changed.get("created_at") or ""),
            cash_basis=basis,
        )

    def _has_blocking_events(
        self,
        account_id: str,
        baseline: Mapping[str, Any],
        latest: Mapping[str, Any],
    ) -> bool:
        start = _timestamp(baseline.get("created_at"))
        end = _timestamp(latest.get("created_at"))
        for row in self.store.list_system_events(limit=2000):
            created_at = _timestamp(row.get("created_at"))
            if created_at is None or start is None or end is None:
                continue
            if not (start <= created_at <= end):
                continue
            event_type = str(row.get("event_type") or "")
            payload = row.get("payload") or {}
            event_account_id = str(payload.get("account_id") or "")
            if event_account_id and event_account_id != account_id:
                continue
            if event_type in _BLOCKING_EVENT_TYPES:
                return True
            if event_type == "fill_reconciliation" and (
                payload.get("applied_fills") or payload.get("portfolio_updated")
            ):
                return True
        return False


def _fx_conversion_candidate(
    candidates: list[CashFlowCandidate],
    basis: str,
) -> FxConversionCandidate | None:
    if basis != PROXY_CASH or len(candidates) != 2:
        return None
    withdrawals = [candidate for candidate in candidates if candidate.flow_type == "withdrawal"]
    deposits = [candidate for candidate in candidates if candidate.flow_type == "deposit"]
    if len(withdrawals) != 1 or len(deposits) != 1:
        return None
    outbound = withdrawals[0]
    inbound = deposits[0]
    if (
        outbound.account_id != inbound.account_id
        or outbound.baseline_snapshot_id != inbound.baseline_snapshot_id
        or outbound.first_changed_snapshot_id != inbound.first_changed_snapshot_id
        or outbound.latest_snapshot_id != inbound.latest_snapshot_id
        or outbound.stable_snapshot_ids != inbound.stable_snapshot_ids
    ):
        return None
    fingerprint_payload = {
        "account_id": outbound.account_id,
        "baseline_snapshot_id": outbound.baseline_snapshot_id,
        "first_changed_snapshot_id": outbound.first_changed_snapshot_id,
        "from_currency": outbound.currency,
        "from_amount": round(outbound.amount, 8),
        "to_currency": inbound.currency,
        "to_amount": round(inbound.amount, 8),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    return FxConversionCandidate(
        fingerprint=fingerprint,
        account_id=outbound.account_id,
        from_currency=outbound.currency,
        from_amount=outbound.amount,
        to_currency=inbound.currency,
        to_amount=inbound.amount,
        cash_basis=basis,
        baseline_snapshot_id=outbound.baseline_snapshot_id,
        first_changed_snapshot_id=outbound.first_changed_snapshot_id,
        latest_snapshot_id=outbound.latest_snapshot_id,
        stable_snapshot_ids=outbound.stable_snapshot_ids,
        effective_at=outbound.effective_at,
    )

def _snapshot_account_id(row: Mapping[str, Any]) -> str:
    payload = row.get("payload") or {}
    account = payload.get("account") if isinstance(payload, Mapping) else {}
    return str(
        (payload.get("account_id") if isinstance(payload, Mapping) else None)
        or (account.get("account_id") if isinstance(account, Mapping) else None)
        or row.get("account_id")
        or ""
    )


def _account(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload") or {}
    account = payload.get("account") if isinstance(payload, Mapping) else {}
    return account if isinstance(account, Mapping) else {}


def _cash_basis(account: Mapping[str, Any]) -> str | None:
    """Which cash figure this account's changes should be read from.

    Toss publishes buying power and no settled-cash endpoint, so its ledger
    cash is deliberately absent; anything else reports actual deposits.
    """
    source = str(account.get("source") or "")
    if (
        source.startswith("toss_")
        and "ledger_cash_by_currency" in account
        and account.get("ledger_cash_by_currency") is None
    ):
        return PROXY_CASH
    if source:
        return BROKER_REPORTED_CASH
    return None


def _cash_by_basis(account: Mapping[str, Any], basis: str) -> dict[str, float]:
    if basis == PROXY_CASH:
        return _buying_power_by_currency(account)
    values = account.get("cash_by_currency")
    if isinstance(values, Mapping) and values:
        return {str(key).upper(): float(value) for key, value in values.items()}
    cash = account.get("cash")
    if cash is None:
        return {}
    cash_balance = account.get("cash_balance")
    currency = str(account.get("currency") or "UNKNOWN")
    if isinstance(cash_balance, Mapping):
        currency = str(cash_balance.get("currency") or currency)
    return {currency.upper(): float(cash)}


def _settling_fill_nearby(
    rows: list[dict[str, Any]],
    latest_timestamp: datetime,
) -> bool:
    """Whether any observed fill is recent enough to still be settling."""
    for row in rows:
        payload = row.get("payload") or {}
        fills = payload.get("order_fills") if isinstance(payload, Mapping) else None
        for fill in fills or []:
            if not isinstance(fill, Mapping):
                continue
            submitted = _timestamp(fill.get("submitted_at"))
            if submitted is None:
                continue
            elapsed_days = (latest_timestamp - submitted).total_seconds() / 86400.0
            # Anything not yet past the horizon blocks, including a fill dated
            # after the snapshot: snapshot timestamps are second-granular, so a
            # fill recorded moments earlier can still read as being ahead of it.
            if elapsed_days <= _SETTLEMENT_HORIZON_DAYS:
                return True
    return False


def _buying_power_by_currency(account: Mapping[str, Any]) -> dict[str, float]:
    values = account.get("buying_power_by_currency") or {}
    if isinstance(values, Mapping) and values:
        return {str(key).upper(): float(value) for key, value in values.items()}
    cash_balance = account.get("cash_balance") or {}
    available = (
        cash_balance.get("available_cash_by_currency")
        if isinstance(cash_balance, Mapping)
        else {}
    )
    if isinstance(available, Mapping):
        return {str(key).upper(): float(value) for key, value in available.items()}
    return {}


def _activity_signature(row: Mapping[str, Any]) -> str:
    payload = row.get("payload") or {}
    account = _account(row)
    comparable = {
        "positions": _normalized_positions(account.get("positions") or []),
        "unfilled_orders": _normalized_orders(
            payload.get("unfilled_orders") if isinstance(payload, Mapping) else []
        ),
        "order_fills": _normalized_orders(
            payload.get("order_fills") if isinstance(payload, Mapping) else []
        ),
    }
    return json.dumps(comparable, sort_keys=True, separators=(",", ":"), default=str)


def _normalized_positions(rows: object) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    output = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        output.append(
            {
                "symbol": str(row.get("symbol") or ""),
                "quantity": float(row.get("quantity") or 0.0),
                "average_price": float(row.get("average_price") or 0.0),
                "currency": str(row.get("currency") or "").upper(),
            }
        )
    return sorted(output, key=lambda row: (row["symbol"], row["currency"]))


def _normalized_orders(rows: object) -> list[dict[str, Any]]:
    if not isinstance(rows, list):
        return []
    output = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        output.append(
            {
                "order_id": str(row.get("order_id") or ""),
                "symbol": str(row.get("symbol") or ""),
                "side": str(row.get("side") or "").lower(),
                "status": str(row.get("status") or "").upper(),
                "quantity": float(row.get("quantity") or 0.0),
                "filled_quantity": float(row.get("filled_quantity") or 0.0),
                "remaining_quantity": float(row.get("remaining_quantity") or 0.0),
                "average_fill_price": float(row.get("average_fill_price") or 0.0),
                "cumulative_commission": float(
                    row.get("cumulative_commission") or 0.0
                ),
                "cumulative_tax": float(row.get("cumulative_tax") or 0.0),
            }
        )
    return sorted(output, key=lambda row: row["order_id"])


def _timestamp(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Snapshot rows carry naive UTC from SQLite while event payloads carry an
    # offset; comparing the two raises unless they are normalised first.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

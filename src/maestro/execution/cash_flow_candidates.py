import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
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


@dataclass(frozen=True)
class TossCashFlowCandidate:
    fingerprint: str
    account_id: str
    currency: str
    amount: float
    flow_type: str
    baseline_snapshot_id: int
    first_changed_snapshot_id: int
    latest_snapshot_id: int
    stable_snapshot_ids: tuple[int, ...]
    effective_at: str

    def evidence(self) -> dict[str, Any]:
        return {
            "kind": "stable_toss_buying_power_change",
            "baseline_snapshot_id": self.baseline_snapshot_id,
            "first_changed_snapshot_id": self.first_changed_snapshot_id,
            "latest_snapshot_id": self.latest_snapshot_id,
            "stable_snapshot_ids": list(self.stable_snapshot_ids),
            "positions_unchanged": True,
            "orders_unchanged": True,
            "fills_unchanged": True,
            "blocking_lifecycle_events": False,
        }


class TossCashFlowCandidateDetector:
    def __init__(self, store: StateStore) -> None:
        self.store = store

    def detect(self, account_id: str) -> TossCashFlowCandidate | None:
        rows = [
            row
            for row in self.store.list_broker_account_snapshots(limit=100)
            if _snapshot_account_id(row) == account_id
        ]
        if len(rows) < 4:
            return None
        latest_account = _account(rows[0])
        if not (
            str(latest_account.get("source") or "").startswith("toss_")
            and "ledger_cash_by_currency" in latest_account
            and latest_account.get("ledger_cash_by_currency") is None
        ):
            return None

        currencies = sorted(_buying_power_by_currency(latest_account))
        for currency in currencies:
            candidate = self._detect_currency(rows, account_id, currency)
            if candidate is not None:
                return candidate
        return None

    def _detect_currency(
        self,
        rows: list[dict[str, Any]],
        account_id: str,
        currency: str,
    ) -> TossCashFlowCandidate | None:
        tolerance = _STABILITY_TOLERANCE.get(currency, 0.01)
        latest_value = _buying_power_by_currency(_account(rows[0])).get(currency)
        if latest_value is None:
            return None
        stable: list[dict[str, Any]] = []
        baseline: dict[str, Any] | None = None
        for row in rows:
            value = _buying_power_by_currency(_account(row)).get(currency)
            if value is None:
                return None
            if abs(value - latest_value) <= tolerance:
                stable.append(row)
                continue
            baseline = row
            break
        if len(stable) < 3 or baseline is None:
            return None
        baseline_value = _buying_power_by_currency(_account(baseline)).get(currency)
        if baseline_value is None:
            return None
        delta = latest_value - baseline_value
        if abs(delta) < _MINIMUM_CHANGE.get(currency, 1.0):
            return None

        window = [*stable, baseline]
        signature = _activity_signature(baseline)
        if any(_activity_signature(row) != signature for row in stable):
            return None
        if self._has_blocking_events(account_id, baseline, stable[0]):
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
        return TossCashFlowCandidate(
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
        "positions": account.get("positions") or [],
        "unfilled_orders": payload.get("unfilled_orders") if isinstance(payload, Mapping) else [],
        "order_fills": payload.get("order_fills") if isinstance(payload, Mapping) else [],
    }
    return json.dumps(comparable, sort_keys=True, separators=(",", ":"), default=str)


def _timestamp(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None

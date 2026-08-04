from collections import defaultdict
from typing import Any, Literal

from pydantic import BaseModel, Field

from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import save_audited_system_event
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore

MANUAL_BUCKET_ID = "manual"
ATTRIBUTION_RECONCILIATION_EVENT = "account_attribution_reconciliation"
ATTRIBUTION_ADOPTED_EVENT = "account_attribution_adopted"


class AttributionValidationError(ValueError):
    pass


class AttributionPosition(BaseModel):
    account_id: str
    symbol: str
    bucket_id: str
    quantity: float = Field(ge=0.0)
    source: str = "ledger"
    confidence: Literal["high", "medium", "low"] = "high"
    broker_snapshot_id: int | None = None
    version: int = Field(default=1, ge=1)
    approved: bool = False


class AccountAttributionReconciliationService:
    def __init__(self, state_store: StateStore, audit_logger: AuditLogger) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger

    def reconcile_broker_snapshot(
        self,
        *,
        run_id: str,
        account_id: str,
        broker_snapshot_id: int,
        broker_positions: dict[str, float],
        strategy_symbols_by_bucket: dict[str, set[str]],
    ) -> list[AttributionPosition]:
        previous = self._latest_positions(account_id)
        if previous is None:
            positions = build_auto_baseline(
                account_id=account_id,
                broker_positions=broker_positions,
                strategy_symbols_by_bucket=strategy_symbols_by_bucket,
            )
            approved = False
            version = 1
            status = "baseline_pending_approval"
            events: list[dict[str, object]] = []
        else:
            reconciled, events = apply_broker_snapshot_delta(
                previous=previous,
                broker_positions=broker_positions,
            )
            approved = all(position.approved for position in previous)
            version = max(position.version for position in previous) + 1
            status = "reconciled" if approved else "baseline_pending_approval"
            positions = reconciled
        positions = [
            position.model_copy(
                update={
                    "broker_snapshot_id": broker_snapshot_id,
                    "version": version,
                    "approved": approved,
                }
            )
            for position in positions
        ]
        self.state_store.save_account_attribution_snapshot(run_id, positions)
        self._save_state_event(
            run_id=run_id,
            event_type=ATTRIBUTION_RECONCILIATION_EVENT,
            account_id=account_id,
            broker_snapshot_id=broker_snapshot_id,
            version=version,
            approved=approved,
            status=status,
            positions=positions,
            changes=events,
        )
        return positions

    def adopt_latest(
        self,
        *,
        run_id: str,
        account_id: str,
        reason: str,
        adopted_by: str,
    ) -> list[AttributionPosition]:
        previous = self._latest_positions(account_id)
        if previous is None:
            raise AttributionValidationError(
                f"account attribution baseline is missing for account_id={account_id}"
            )
        if previous and all(position.approved for position in previous):
            raise AttributionValidationError(
                f"account attribution is already adopted for account_id={account_id}"
            )
        version = max(position.version for position in previous) + 1
        positions = [
            position.model_copy(
                update={
                    "source": "operator_adopted",
                    "confidence": "high",
                    "version": version,
                    "approved": True,
                }
            )
            for position in previous
        ]
        self.state_store.save_account_attribution_snapshot(run_id, positions)
        self._save_state_event(
            run_id=run_id,
            event_type=ATTRIBUTION_ADOPTED_EVENT,
            account_id=account_id,
            broker_snapshot_id=positions[0].broker_snapshot_id if positions else None,
            version=version,
            approved=True,
            status="adopted",
            positions=positions,
            changes=[],
            reason=reason,
            adopted_by=adopted_by,
        )
        return positions

    def require_ready(
        self,
        *,
        account_id: str,
        broker_snapshot_id: int,
        broker_positions: dict[str, float],
    ) -> list[AttributionPosition]:
        positions = self._latest_positions(account_id)
        if positions is None:
            raise AttributionValidationError(
                f"account attribution is missing for account_id={account_id}"
            )
        if not all(position.approved for position in positions):
            raise AttributionValidationError(
                f"account attribution is not adopted for account_id={account_id}"
            )
        attribution_snapshot_ids = {position.broker_snapshot_id for position in positions}
        if attribution_snapshot_ids != {broker_snapshot_id}:
            raise AttributionValidationError(
                "account attribution broker snapshot does not match latest broker snapshot: "
                f"account_id={account_id}"
            )
        attributed: defaultdict[str, float] = defaultdict(float)
        for position in positions:
            attributed[position.symbol] += position.quantity
        symbols = sorted(set(attributed) | set(broker_positions))
        mismatches = [
            symbol
            for symbol in symbols
            if abs(attributed[symbol] - float(broker_positions.get(symbol, 0.0))) > 1e-9
        ]
        if mismatches:
            raise AttributionValidationError(
                "account attribution quantity mismatch: " + ", ".join(mismatches)
            )
        return positions

    def apply_maestro_fill(
        self,
        *,
        run_id: str,
        account_id: str,
        bucket_id: str,
        symbol: str,
        side: str,
        quantity: float,
        fill_key: str,
    ) -> list[AttributionPosition]:
        previous = self._latest_positions(account_id)
        if previous is None:
            raise AttributionValidationError(
                f"account attribution is missing for account_id={account_id}"
            )
        if self._fill_key_applied(account_id, fill_key):
            return previous
        positions = _positions_by_key(previous)
        strategy_key = (account_id, symbol, bucket_id)
        changes: list[dict[str, object]] = []
        if side == "buy":
            manual_key = (account_id, symbol, MANUAL_BUCKET_ID)
            reclassified = min(positions.get(manual_key, 0.0), quantity)
            if reclassified > 0:
                positions[manual_key] -= reclassified
                positions[strategy_key] += reclassified
                changes.append(
                    _event(
                        "maestro_fill_reclassified",
                        account_id=account_id,
                        symbol=symbol,
                        bucket_id=bucket_id,
                        quantity=reclassified,
                    )
                )
            remaining = quantity - reclassified
            if remaining > 1e-12:
                positions[strategy_key] += remaining
                changes.append(
                    _event(
                        "maestro_strategy_buy",
                        account_id=account_id,
                        symbol=symbol,
                        bucket_id=bucket_id,
                        quantity=remaining,
                    )
                )
        elif side == "sell":
            available = positions.get(strategy_key, 0.0)
            if quantity > available + 1e-9:
                raise AttributionValidationError(
                    "Maestro strategy sell exceeds attributed quantity: "
                    f"account_id={account_id} bucket_id={bucket_id} symbol={symbol}"
                )
            positions[strategy_key] -= quantity
            changes.append(
                _event(
                    "maestro_strategy_sell",
                    account_id=account_id,
                    symbol=symbol,
                    bucket_id=bucket_id,
                    quantity=quantity,
                )
            )
        else:
            raise ValueError(f"Unsupported Maestro fill side: {side}")
        latest_payload = self._latest_attribution_payload(account_id) or {}
        version = max(
            (position.version for position in previous),
            default=int(latest_payload.get("version") or 0),
        ) + 1
        broker_snapshot_id = (
            previous[0].broker_snapshot_id
            if previous
            else latest_payload.get("broker_snapshot_id")
        )
        approved = all(position.approved for position in previous)
        next_positions = [
            position.model_copy(
                update={
                    "source": "maestro_fill",
                    "confidence": "high",
                    "broker_snapshot_id": broker_snapshot_id,
                    "version": version,
                    "approved": approved,
                }
            )
            for position in _sorted_positions(positions)
        ]
        self.state_store.save_account_attribution_snapshot(run_id, next_positions)
        self._save_state_event(
            run_id=run_id,
            event_type=ATTRIBUTION_RECONCILIATION_EVENT,
            account_id=account_id,
            broker_snapshot_id=broker_snapshot_id,
            version=version,
            approved=approved,
            status="maestro_fill_applied",
            positions=next_positions,
            changes=changes,
            fill_key=fill_key,
        )
        return next_positions

    def reclassify_position(
        self,
        *,
        run_id: str,
        account_id: str,
        symbol: str,
        from_bucket_id: str,
        to_bucket_id: str,
        quantity: float,
        reason: str,
        reclassified_by: str,
    ) -> list[AttributionPosition]:
        previous = self._latest_positions(account_id)
        if previous is None:
            raise AttributionValidationError(
                f"account attribution is missing for account_id={account_id}"
            )
        if not all(position.approved for position in previous):
            raise AttributionValidationError(
                f"account attribution is not adopted for account_id={account_id}"
            )
        if from_bucket_id == to_bucket_id:
            raise AttributionValidationError("attribution buckets must be different")
        if quantity <= 0:
            raise AttributionValidationError("reclassification quantity must be positive")

        positions = _positions_by_key(previous)
        from_key = (account_id, symbol, from_bucket_id)
        to_key = (account_id, symbol, to_bucket_id)
        available = positions.get(from_key, 0.0)
        if quantity > available + 1e-9:
            raise AttributionValidationError(
                "attribution reclassification exceeds source quantity: "
                f"account_id={account_id} symbol={symbol} "
                f"bucket_id={from_bucket_id} available={available:g}"
            )
        positions[from_key] -= quantity
        positions[to_key] += quantity

        version = max(position.version for position in previous) + 1
        broker_snapshot_id = previous[0].broker_snapshot_id if previous else None
        next_positions = [
            position.model_copy(
                update={
                    "source": "operator_reclassified",
                    "confidence": "high",
                    "broker_snapshot_id": broker_snapshot_id,
                    "version": version,
                    "approved": True,
                }
            )
            for position in _sorted_positions(positions)
        ]
        change = _event(
            "operator_reclassified",
            account_id=account_id,
            symbol=symbol,
            bucket_id=to_bucket_id,
            quantity=quantity,
        )
        change["from_bucket_id"] = from_bucket_id
        change["to_bucket_id"] = to_bucket_id
        self.state_store.save_account_attribution_snapshot(run_id, next_positions)
        self._save_state_event(
            run_id=run_id,
            event_type=ATTRIBUTION_RECONCILIATION_EVENT,
            account_id=account_id,
            broker_snapshot_id=broker_snapshot_id,
            version=version,
            approved=True,
            status="operator_reclassified",
            positions=next_positions,
            changes=[change],
            reason=reason,
            reclassified_by=reclassified_by,
        )
        return next_positions

    def restore_pending_maestro_sell(
        self,
        *,
        run_id: str,
        account_id: str,
        symbol: str,
        bucket_id: str,
        quantity: float,
        reason: str,
        restored_by: str,
    ) -> list[AttributionPosition]:
        """Reverse a broker-delta reduction before replaying its Maestro sell fill.

        A broker snapshot can observe a sell before fill reconciliation does. The
        attribution delta then records an explicit strategy-reduction warning and
        removes the quantity. This audited repair restores only warning-backed
        quantity so the normal fill path can consume it exactly once.
        """
        previous = self._latest_positions(account_id)
        if previous is None:
            raise AttributionValidationError(
                f"account attribution is missing for account_id={account_id}"
            )
        if not all(position.approved for position in previous):
            raise AttributionValidationError(
                f"account attribution is not adopted for account_id={account_id}"
            )
        if quantity <= 0:
            raise AttributionValidationError("restoration quantity must be positive")
        available = self._unrestored_strategy_reduction(
            account_id=account_id,
            symbol=symbol,
            bucket_id=bucket_id,
        )
        if quantity > available + 1e-9:
            raise AttributionValidationError(
                "restoration exceeds warning-backed strategy reduction: "
                f"account_id={account_id} symbol={symbol} bucket_id={bucket_id} "
                f"available={available:g}"
            )

        positions = _positions_by_key(previous)
        positions[(account_id, symbol, bucket_id)] += quantity
        latest_payload = self._latest_attribution_payload(account_id) or {}
        version = max(
            (position.version for position in previous),
            default=int(latest_payload.get("version") or 0),
        ) + 1
        broker_snapshot_id = (
            previous[0].broker_snapshot_id
            if previous
            else latest_payload.get("broker_snapshot_id")
        )
        next_positions = [
            position.model_copy(
                update={
                    "source": "operator_recovery",
                    "confidence": "high",
                    "broker_snapshot_id": broker_snapshot_id,
                    "version": version,
                    "approved": True,
                }
            )
            for position in _sorted_positions(positions)
        ]
        change = _event(
            "pending_maestro_sell_restored",
            account_id=account_id,
            symbol=symbol,
            bucket_id=bucket_id,
            quantity=quantity,
        )
        self.state_store.save_account_attribution_snapshot(run_id, next_positions)
        self._save_state_event(
            run_id=run_id,
            event_type=ATTRIBUTION_RECONCILIATION_EVENT,
            account_id=account_id,
            broker_snapshot_id=broker_snapshot_id,
            version=version,
            approved=True,
            status="pending_maestro_sell_restored",
            positions=next_positions,
            changes=[change],
            reason=reason,
            restored_by=restored_by,
        )
        return next_positions

    def has_attribution(self, account_id: str) -> bool:
        return self._latest_positions(account_id) is not None

    def _fill_key_applied(self, account_id: str, fill_key: str) -> bool:
        for row in self.state_store.list_system_events_by_type(
            ATTRIBUTION_RECONCILIATION_EVENT,
            limit=2000,
        ):
            payload = row["payload"]
            if (
                payload.get("account_id") == account_id
                and payload.get("fill_key") == fill_key
            ):
                return True
        return False

    def _unrestored_strategy_reduction(
        self,
        *,
        account_id: str,
        symbol: str,
        bucket_id: str,
    ) -> float:
        warned = 0.0
        restored = 0.0
        for row in self.state_store.list_system_events_by_type(
            ATTRIBUTION_RECONCILIATION_EVENT,
            limit=5000,
        ):
            payload = row.get("payload") or {}
            if payload.get("account_id") != account_id:
                continue
            for change in payload.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                if (
                    change.get("symbol") != symbol
                    or change.get("bucket_id") != bucket_id
                ):
                    continue
                event_type = change.get("event_type")
                quantity = max(float(change.get("quantity") or 0.0), 0.0)
                if event_type == "external_strategy_reduction_warning":
                    warned += quantity
                elif event_type == "pending_maestro_sell_restored":
                    restored += quantity
        return max(warned - restored, 0.0)

    def _latest_positions(self, account_id: str) -> list[AttributionPosition] | None:
        payload = self._latest_attribution_payload(account_id)
        if payload is None:
            return None
        return [
            AttributionPosition.model_validate(position)
            for position in payload.get("positions", [])
        ]

    def _latest_attribution_payload(self, account_id: str) -> dict[str, Any] | None:
        event_types = {ATTRIBUTION_ADOPTED_EVENT, ATTRIBUTION_RECONCILIATION_EVENT}
        for row in self.state_store.list_system_events(limit=2000):
            if row.get("event_type") not in event_types:
                continue
            payload = row["payload"]
            if payload.get("account_id") != account_id:
                continue
            return payload
        return None

    def _save_state_event(
        self,
        *,
        run_id: str,
        event_type: str,
        account_id: str,
        broker_snapshot_id: int | None,
        version: int,
        approved: bool,
        status: str,
        positions: list[AttributionPosition],
        changes: list[dict[str, object]],
        **extra: Any,
    ) -> None:
        payload = {
            "account_id": account_id,
            "broker_snapshot_id": broker_snapshot_id,
            "version": version,
            "approved": approved,
            "status": status,
            "positions": [position.model_dump(mode="json") for position in positions],
            "changes": changes,
            **extra,
        }
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            run_id,
            event_type,
            payload,
        )


def build_auto_baseline(
    *,
    account_id: str,
    broker_positions: dict[str, float],
    strategy_symbols_by_bucket: dict[str, set[str]],
) -> list[AttributionPosition]:
    positions: list[AttributionPosition] = []
    for symbol, quantity in sorted(broker_positions.items()):
        if quantity <= 0:
            continue
        matching_buckets = sorted(
            bucket_id
            for bucket_id, symbols in strategy_symbols_by_bucket.items()
            if symbol in symbols
        )
        bucket_id = matching_buckets[0] if len(matching_buckets) == 1 else MANUAL_BUCKET_ID
        positions.append(
            AttributionPosition(
                account_id=account_id,
                symbol=symbol,
                bucket_id=bucket_id,
                quantity=quantity,
                source="auto_baseline",
                confidence="medium",
            )
        )
    return positions


def apply_broker_snapshot_delta(
    *,
    previous: list[AttributionPosition],
    broker_positions: dict[str, float],
) -> tuple[list[AttributionPosition], list[dict[str, object]]]:
    positions = _positions_by_key(previous)
    events: list[dict[str, object]] = []
    symbols = sorted(set(broker_positions) | {position.symbol for position in previous})
    account_id = _single_account_id(previous)
    for symbol in symbols:
        expected = sum(
            quantity
            for (position_account_id, position_symbol, _), quantity in positions.items()
            if position_account_id == account_id and position_symbol == symbol
        )
        actual = max(0.0, float(broker_positions.get(symbol, 0.0)))
        delta = actual - expected
        if abs(delta) < 1e-12:
            continue
        if delta > 0:
            key = (account_id, symbol, MANUAL_BUCKET_ID)
            positions[key] += delta
            events.append(
                _event(
                    "external_manual_buy",
                    account_id=account_id,
                    symbol=symbol,
                    bucket_id=MANUAL_BUCKET_ID,
                    quantity=delta,
                )
            )
            continue
        remaining = -delta
        manual_key = (account_id, symbol, MANUAL_BUCKET_ID)
        manual_reduction = min(positions.get(manual_key, 0.0), remaining)
        if manual_reduction > 0:
            positions[manual_key] -= manual_reduction
            remaining -= manual_reduction
            events.append(
                _event(
                    "external_manual_sell",
                    account_id=account_id,
                    symbol=symbol,
                    bucket_id=MANUAL_BUCKET_ID,
                    quantity=manual_reduction,
                )
            )
        for key in sorted(_strategy_keys_for_symbol(positions, account_id, symbol)):
            if remaining <= 1e-12:
                break
            strategy_reduction = min(positions[key], remaining)
            if strategy_reduction <= 0:
                continue
            positions[key] -= strategy_reduction
            remaining -= strategy_reduction
            events.append(
                _event(
                    "external_strategy_reduction_warning",
                    account_id=account_id,
                    symbol=symbol,
                    bucket_id=key[2],
                    quantity=strategy_reduction,
                )
            )
    return _sorted_positions(positions), events


def bucket_portfolio_state(
    *,
    positions: list[AttributionPosition],
    account_id: str,
    bucket_id: str,
    cash: float = 0.0,
    cash_by_currency: dict[str, float] | None = None,
) -> PortfolioState:
    bucket_positions: dict[str, float] = {}
    for position in positions:
        if position.account_id != account_id or position.bucket_id != bucket_id:
            continue
        bucket_positions[position.symbol] = (
            bucket_positions.get(position.symbol, 0.0) + position.quantity
        )
    return PortfolioState(
        cash=cash,
        cash_by_currency=dict(cash_by_currency or {}),
        positions=bucket_positions,
    )


def _positions_by_key(
    positions: list[AttributionPosition],
) -> defaultdict[tuple[str, str, str], float]:
    output: defaultdict[tuple[str, str, str], float] = defaultdict(float)
    for position in positions:
        output[(position.account_id, position.symbol, position.bucket_id)] += position.quantity
    return output


def _single_account_id(positions: list[AttributionPosition]) -> str:
    account_ids = sorted({position.account_id for position in positions})
    if len(account_ids) > 1:
        raise ValueError("broker snapshot attribution reconciliation supports one account")
    if not account_ids:
        raise ValueError("previous attribution positions are required")
    return account_ids[0]


def _strategy_keys_for_symbol(
    positions: dict[tuple[str, str, str], float],
    account_id: str,
    symbol: str,
) -> list[tuple[str, str, str]]:
    return [
        key
        for key, quantity in positions.items()
        if key[0] == account_id
        and key[1] == symbol
        and key[2] != MANUAL_BUCKET_ID
        and quantity > 0
    ]


def _sorted_positions(
    positions: dict[tuple[str, str, str], float],
) -> list[AttributionPosition]:
    output = []
    for (account_id, symbol, bucket_id), quantity in sorted(positions.items()):
        if quantity <= 1e-12:
            continue
        output.append(
            AttributionPosition(
                account_id=account_id,
                symbol=symbol,
                bucket_id=bucket_id,
                quantity=quantity,
            )
        )
    return output


def _event(
    event_type: str,
    *,
    account_id: str,
    symbol: str,
    bucket_id: str,
    quantity: float,
) -> dict[str, object]:
    return {
        "event_type": event_type,
        "account_id": account_id,
        "symbol": symbol,
        "bucket_id": bucket_id,
        "quantity": quantity,
    }


__all__ = [
    "ATTRIBUTION_ADOPTED_EVENT",
    "ATTRIBUTION_RECONCILIATION_EVENT",
    "AccountAttributionReconciliationService",
    "AttributionPosition",
    "AttributionValidationError",
    "MANUAL_BUCKET_ID",
    "apply_broker_snapshot_delta",
    "build_auto_baseline",
    "bucket_portfolio_state",
]

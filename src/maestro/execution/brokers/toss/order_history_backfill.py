from datetime import UTC, date, datetime
from typing import Any

from maestro.core.ids import new_run_id
from maestro.execution.brokers.readonly import BrokerOrderSummary
from maestro.execution.brokers.toss.readonly_client import TossReadOnlyClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore


class TossOrderHistoryBackfillService:
    """Backfill Toss fills/costs into the cash ledger with watermarks."""

    def __init__(
        self,
        client: TossReadOnlyClient,
        state_store: StateStore,
        audit_logger: AuditLogger,
    ) -> None:
        self.client = client
        self.state_store = state_store
        self.audit_logger = audit_logger

    def backfill(
        self,
        account_id: str,
        *,
        from_date: date,
        to_date: date | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        run_id = run_id or new_run_id()
        to_date = to_date or date.today()
        if to_date < from_date:
            raise ValueError("to_date must be on or after from_date")
        orders: list[BrokerOrderSummary] = []
        for status in ("OPEN", "CLOSED"):
            orders.extend(
                self.client.list_orders(
                    status=status,
                    from_date=from_date,
                    to_date=to_date,
                )
            )
        # A broker can return an order in both lifecycle views during a
        # transition; the broker order id is the idempotency key.
        unique_orders: dict[str, BrokerOrderSummary] = {
            order.order_id: order for order in orders if order.order_id
        }
        applied = 0
        fills = 0
        costs = 0
        missing_ledger = 0
        cash_baseline_at, position_adopted_at = self._ledger_cutoffs(account_id)
        for order in unique_orders.values():
            filled_quantity = max(float(order.filled_quantity), 0.0)
            average_price = float(order.average_fill_price or order.limit_price or 0.0)
            cumulative_notional = filled_quantity * average_price
            maestro_submitted = bool(
                self.state_store.list_system_events_by_broker_order_id(
                    order.order_id,
                    str(SystemEventType.LIVE_ORDER_RESULT),
                )
            )
            submitted_at = order.submitted_at
            if submitted_at.tzinfo is None:
                submitted_at = submitted_at.replace(tzinfo=UTC)
            principal_in_cash_baseline = (
                cash_baseline_at is not None
                and submitted_at <= cash_baseline_at
            )
            cost_in_cash_baseline = principal_in_cash_baseline
            quantity_in_adopted_positions = (
                not maestro_submitted
                and position_adopted_at is not None
                and submitted_at <= position_adopted_at
            )
            result = self.state_store.apply_broker_order_history_delta(
                run_id,
                account_id=account_id,
                broker_order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                currency=order.currency or "KRW",
                # Live-order status/fill reconciliation owns principal for
                # Maestro-submitted orders. History may still supply costs;
                # keeping quantity/notional at zero prevents a later live
                # status from being suppressed by an early backfill.
                cumulative_quantity=(
                    0.0
                    if maestro_submitted
                    else filled_quantity
                ),
                cumulative_notional=(
                    0.0
                    if maestro_submitted
                    else cumulative_notional
                ),
                cumulative_commission=order.cumulative_commission,
                cumulative_tax=order.cumulative_tax,
                quantity_in_baseline=quantity_in_adopted_positions,
                principal_in_baseline=principal_in_cash_baseline,
                costs_in_baseline=cost_in_cash_baseline,
            )
            if filled_quantity > 0:
                fills += 1
            if order.cumulative_commission is not None or order.cumulative_tax is not None:
                costs += 1
            if result["applied"]:
                applied += 1
            elif not self.state_store.load_latest_account_portfolio_state(account_id):
                missing_ledger += 1
            item_payload = {
                "account_id": account_id,
                "broker_order_id": order.order_id,
                "symbol": order.symbol,
                "side": order.side,
                "status": order.status,
                "filled_quantity": filled_quantity,
                "cumulative_notional": cumulative_notional,
                "cumulative_commission": order.cumulative_commission,
                "cumulative_tax": order.cumulative_tax,
                "history_mode": "maestro_cost_only" if maestro_submitted else "external_fill",
                "principal_in_cash_baseline": principal_in_cash_baseline,
                "cost_in_cash_baseline": cost_in_cash_baseline,
                "quantity_in_adopted_positions": quantity_in_adopted_positions,
                "attribution_mode": "broker_snapshot_delta",
                "submitted_at": order.submitted_at.isoformat(),
                "source": "toss_order_history",
                "duplicate_key": (
                    f"toss-order-history:{account_id}:{order.order_id}:"
                    f"{filled_quantity}:{order.cumulative_commission or 0}:"
                    f"{order.cumulative_tax or 0}"
                ),
            }
            if not self.state_store.duplicate_key_exists(item_payload["duplicate_key"]):
                save_audited_system_event(
                    self.state_store,
                    self.audit_logger,
                    run_id,
                    "broker_order_history_item",
                    item_payload,
                )
        payload = {
            "account_id": account_id,
            "from_date": from_date.isoformat(),
            "to_date": to_date.isoformat(),
            "orders_checked": len(unique_orders),
            "fills_seen": fills,
            "costs_seen": costs,
            "applied_count": applied,
            "missing_ledger_count": missing_ledger,
            "broker_order_ids": sorted(unique_orders),
            "open_order_ids": sorted(
                order.order_id for order in unique_orders.values() if order.status == "OPEN"
            ),
            "checked_at": date.today().isoformat(),
        }
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            run_id,
            SystemEventType.BROKER_ORDER_HISTORY_BACKFILL,
            payload,
        )
        return payload

    def _ledger_cutoffs(
        self,
        account_id: str,
    ) -> tuple[datetime | None, datetime | None]:
        cash_baseline_at: datetime | None = None
        position_adopted_at: datetime | None = None
        for event_type in (
            SystemEventType.LEDGER_OPENING_BASELINE,
            SystemEventType.BROKER_SNAPSHOT_ADOPTED,
        ):
            for row in self.state_store.list_system_events_by_type(event_type, limit=2000):
                payload = row.get("payload") or {}
                if str(payload.get("account_id") or "") != account_id:
                    continue
                if (
                    event_type == SystemEventType.BROKER_SNAPSHOT_ADOPTED
                    and payload.get("broker_snapshot_id") is None
                ):
                    # Signal preflight records broker evidence with this event
                    # type but no longer persists ledger positions.
                    continue
                timestamp = _as_utc(payload.get("effective_at") or row.get("created_at"))
                if timestamp is None:
                    continue
                position_adopted_at = max(position_adopted_at or timestamp, timestamp)
                legacy_cash_adoption = (
                    event_type == SystemEventType.BROKER_SNAPSHOT_ADOPTED
                    and "include_cash" not in payload
                    and ("cash" in payload or "cash_by_currency" in payload)
                )
                if (
                    event_type == SystemEventType.LEDGER_OPENING_BASELINE
                    or payload.get("include_cash") is True
                    or legacy_cash_adoption
                ):
                    cash_baseline_at = max(cash_baseline_at or timestamp, timestamp)
        return cash_baseline_at, position_adopted_at


def _as_utc(value: Any) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


__all__ = ["TossOrderHistoryBackfillService"]

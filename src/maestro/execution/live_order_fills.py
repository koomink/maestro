from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.live_order_models import (
    AppliedFill,
    FillReconciliationResult,
    LiveOrderStatusSnapshot,
    SkippedFill,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.portfolio.account_attribution import AccountAttributionReconciliationService
from maestro.state.events import SystemEventType
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


class PartialFillReconciliationService:
    def __init__(
        self,
        state_store: StateStore,
        audit_logger: AuditLogger,
    ) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger

    def reconcile_latest(self, run_id: str, *, limit: int = 1000) -> FillReconciliationResult:
        with self.state_store.writer_lock("fill_reconciliation"):
            with self.state_store.live_order_lock("fill_reconciliation"):
                current_state = self.state_store.load_latest_portfolio_state()
                next_state = current_state.model_copy(deep=True)
                applied_by_order = self._load_applied_watermarks()
                applied_fills: list[AppliedFill] = []
                skipped_fills: list[SkippedFill] = []

                rows = self.state_store.list_system_events_by_type(
                    SystemEventType.LIVE_ORDER_STATUS, limit=limit
                )
                for row in reversed(rows):
                    snapshot = LiveOrderStatusSnapshot.model_validate(row["payload"])
                    applied_fill, skipped_fill = _fill_delta(snapshot, applied_by_order)
                    if skipped_fill is not None:
                        skipped_fills.append(skipped_fill)
                        continue
                    if applied_fill is None:
                        continue
                    _apply_fill(next_state, applied_fill)
                    attribution_context = self._attribution_context(applied_fill.broker_order_id)
                    if attribution_context is not None:
                        account_id, bucket_id = attribution_context
                        AccountAttributionReconciliationService(
                            self.state_store,
                            self.audit_logger,
                        ).apply_maestro_fill(
                            run_id=run_id,
                            account_id=account_id,
                            bucket_id=bucket_id,
                            symbol=applied_fill.symbol,
                            side=applied_fill.side.value,
                            quantity=applied_fill.quantity,
                            fill_key=(
                                f"{applied_fill.broker_order_id}:"
                                f"{applied_fill.cumulative_filled_quantity}"
                            ),
                        )
                    applied_by_order[applied_fill.broker_order_id] = (
                        applied_fill.cumulative_filled_quantity,
                        applied_fill.cumulative_filled_notional,
                    )
                    applied_fills.append(applied_fill)

                result = FillReconciliationResult(
                    run_id=run_id,
                    checked_at=utc_now().isoformat(),
                    applied_fills=applied_fills,
                    skipped_fills=skipped_fills,
                    portfolio_updated=bool(applied_fills),
                    cash=next_state.cash,
                    positions=next_state.positions,
                )
                payload = result.model_dump(mode="json")
                watermarks = {
                    fill.broker_order_id: (
                        fill.cumulative_filled_quantity,
                        fill.cumulative_filled_notional,
                    )
                    for fill in applied_fills
                }
                self.state_store.apply_fill_reconciliation(
                    run_id,
                    next_state,
                    watermarks,
                    payload,
                )

        self.audit_logger.log(run_id, str(SystemEventType.FILL_RECONCILIATION), payload)
        return result

    def _attribution_context(self, broker_order_id: str) -> tuple[str, str] | None:
        for row in self.state_store.list_system_events_by_broker_order_id(
            broker_order_id,
            str(SystemEventType.LIVE_ORDER_RESULT),
        ):
            payload = row["payload"]
            request = payload.get("request", {})
            account_id = str(request.get("account_id") or "")
            bucket_id = str(request.get("sleeve") or "")
            if account_id and bucket_id:
                attribution = AccountAttributionReconciliationService(
                    self.state_store,
                    self.audit_logger,
                )
                if attribution.has_attribution(account_id):
                    return account_id, bucket_id
        return None

    def _load_applied_watermarks(self) -> dict[str, tuple[float, float]]:
        self.state_store.seed_fill_watermarks_from_events()
        return self.state_store.load_fill_watermarks()


def _fill_delta(
    snapshot: LiveOrderStatusSnapshot,
    applied_by_order: dict[str, tuple[float, float]],
) -> tuple[AppliedFill | None, SkippedFill | None]:
    broker_order_id = snapshot.broker_order.broker_order_id
    if snapshot.status == OrderStatus.UNKNOWN:
        return None, _skipped(snapshot, "unknown_broker_state")
    if snapshot.status not in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
        return None, _skipped(snapshot, "order_status_not_fill_reconcilable")
    if snapshot.symbol is None:
        return None, _skipped(snapshot, "missing_symbol")
    if snapshot.side is None:
        return None, _skipped(snapshot, "missing_side")

    total_quantity = snapshot.partial_fill.filled_quantity
    total_notional = snapshot.partial_fill.filled_notional
    applied_quantity, applied_notional = applied_by_order.get(broker_order_id, (0.0, 0.0))
    delta_quantity = total_quantity - applied_quantity
    delta_notional = total_notional - applied_notional

    if delta_quantity <= 0 or delta_notional < 0:
        return None, _skipped(snapshot, "duplicate_or_no_new_fill")

    return (
        AppliedFill(
            broker_order_id=broker_order_id,
            symbol=snapshot.symbol,
            side=snapshot.side,
            quantity=delta_quantity,
            price=delta_notional / delta_quantity,
            notional=delta_notional,
            cumulative_filled_quantity=total_quantity,
            cumulative_filled_notional=total_notional,
            status_checked_at=snapshot.checked_at,
        ),
        None,
    )


def _skipped(snapshot: LiveOrderStatusSnapshot, reason: str) -> SkippedFill:
    return SkippedFill(
        broker_order_id=snapshot.broker_order.broker_order_id,
        status_checked_at=snapshot.checked_at,
        reason=reason,
        status=snapshot.status,
    )


def _apply_fill(state: PortfolioState, fill: AppliedFill) -> None:
    signed_quantity = fill.quantity if fill.side == OrderSide.BUY else -fill.quantity
    signed_notional = fill.notional if fill.side == OrderSide.BUY else -fill.notional
    state.cash -= signed_notional
    next_quantity = state.positions.get(fill.symbol, 0.0) + signed_quantity
    if abs(next_quantity) < 1e-12:
        state.positions.pop(fill.symbol, None)
    else:
        state.positions[fill.symbol] = next_quantity


__all__ = ["PartialFillReconciliationService"]

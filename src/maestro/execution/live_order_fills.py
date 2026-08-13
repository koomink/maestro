from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.live_order_models import (
    AppliedFill,
    FillReconciliationResult,
    LiveOrderStatusSnapshot,
    SettlementCashAdjustment,
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
        *,
        account_snapshot_refresher: Callable[[str], None] | None = None,
    ) -> None:
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.account_snapshot_refresher = account_snapshot_refresher

    def reconcile_latest(self, run_id: str, *, limit: int = 1000) -> FillReconciliationResult:
        # live_order_lock outermost, then writer_lock — the same order every
        # other live-order path uses (resolve_pending_signal_approval,
        # submit_approved_order, workflow_recovery). The inverse deadlocked the
        # 2026-08-11/08-12 US rotations against the resume-order-tracking timer.
        with self.state_store.live_order_lock("fill_reconciliation"):
            with self.state_store.writer_lock("fill_reconciliation"):
                current_state = self.state_store.load_latest_portfolio_state()
                next_state = current_state.model_copy(deep=True)
                applied_by_order = self._load_applied_watermarks()
                applied_costs_by_order = self.state_store.load_fill_cost_watermarks()
                applied_fills: list[AppliedFill] = []
                skipped_fills: list[SkippedFill] = []
                execution_cost_adjustments: list[SettlementCashAdjustment] = []
                updated_cost_watermarks: dict[str, tuple[float, float]] = {}
                account_states: dict[str, PortfolioState] = {}
                account_costs_before: dict[str, tuple[str | None, float]] = {}
                account_currencies: dict[str, str] = {}

                rows = self.state_store.list_system_events_by_type(
                    SystemEventType.LIVE_ORDER_STATUS, limit=limit
                )
                for row in reversed(rows):
                    snapshot = LiveOrderStatusSnapshot.model_validate(row["payload"])
                    cost_delta = _execution_cost_delta(snapshot, applied_costs_by_order)
                    applied_fill, skipped_fill = _fill_delta(
                        snapshot,
                        applied_by_order,
                        cost_delta=cost_delta,
                    )
                    if skipped_fill is not None:
                        skipped_fills.append(skipped_fill)
                    if applied_fill is None and cost_delta is None:
                        continue
                    broker_order_id = snapshot.broker_order.broker_order_id
                    order_context = self._order_context(broker_order_id)
                    currency = order_context.currency if order_context is not None else None
                    if applied_fill is not None:
                        _apply_fill(next_state, applied_fill, currency=currency)
                    if order_context is not None:
                        account_id = order_context.account_id
                        account_state = account_states.get(account_id)
                        if account_state is None:
                            stored_state = self.state_store.load_latest_account_portfolio_state(
                                account_id
                            )
                            if stored_state is not None:
                                account_state = stored_state.model_copy(deep=True)
                                account_states[account_id] = account_state
                                account_costs_before[account_id] = _transaction_cost_state(
                                    _latest_snapshot_for_account(self.state_store, account_id)
                                )
                        if account_state is not None and applied_fill is not None:
                            _apply_fill(
                                account_state,
                                applied_fill,
                                currency=snapshot.currency or order_context.currency,
                            )
                        if account_state is not None and cost_delta is not None:
                            commission_delta, tax_delta, cumulative_commission, cumulative_tax = (
                                cost_delta
                            )
                            total_cost_delta = commission_delta + tax_delta
                            cost_currency = snapshot.currency or order_context.currency
                            if abs(total_cost_delta) > 1e-12:
                                _apply_cash_adjustment(
                                    account_state,
                                    -total_cost_delta,
                                    currency=cost_currency,
                                )
                                _apply_cash_adjustment(
                                    next_state,
                                    -total_cost_delta,
                                    currency=cost_currency,
                                )
                                execution_cost_adjustments.append(
                                    SettlementCashAdjustment(
                                        account_id=account_id,
                                        currency=cost_currency,
                                        amount=-total_cost_delta,
                                        transaction_costs_before=(
                                            cumulative_commission
                                            + cumulative_tax
                                            - total_cost_delta
                                        ),
                                        transaction_costs_after=(
                                            cumulative_commission + cumulative_tax
                                        ),
                                        broker_order_id=broker_order_id,
                                        source="toss_order_execution",
                                    )
                                )
                            applied_costs_by_order[broker_order_id] = (
                                cumulative_commission,
                                cumulative_tax,
                            )
                            updated_cost_watermarks[broker_order_id] = (
                                cumulative_commission,
                                cumulative_tax,
                            )
                        account_currencies[account_id] = (
                            snapshot.currency or order_context.currency
                        )
                    if (
                        applied_fill is not None
                        and order_context is not None
                        and order_context.attribution_bucket_unresolved
                    ):
                        skipped_fills.append(_skipped(snapshot, "attribution_bucket_unresolved"))
                    if (
                        applied_fill is not None
                        and order_context is not None
                        and order_context.bucket_id
                    ):
                        AccountAttributionReconciliationService(
                            self.state_store,
                            self.audit_logger,
                        ).apply_maestro_fill(
                            run_id=run_id,
                            account_id=order_context.account_id,
                            bucket_id=order_context.bucket_id,
                            symbol=applied_fill.symbol,
                            side=applied_fill.side.value,
                            quantity=applied_fill.quantity,
                            fill_key=(
                                f"{applied_fill.broker_order_id}:"
                                f"{applied_fill.cumulative_filled_quantity}"
                            ),
                        )
                    if applied_fill is not None:
                        applied_by_order[applied_fill.broker_order_id] = (
                            applied_fill.cumulative_filled_quantity,
                            applied_fill.cumulative_filled_notional,
                        )
                        applied_fills.append(applied_fill)

                settlement_adjustments = execution_cost_adjustments + self._apply_settlement_costs(
                    next_state,
                    account_states,
                    account_costs_before,
                    account_currencies,
                )

                result = FillReconciliationResult(
                    run_id=run_id,
                    checked_at=utc_now().isoformat(),
                    applied_fills=applied_fills,
                    settlement_cash_adjustments=settlement_adjustments,
                    skipped_fills=skipped_fills,
                    portfolio_updated=bool(applied_fills or settlement_adjustments),
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
                for broker_order_id in updated_cost_watermarks:
                    watermarks.setdefault(
                        broker_order_id,
                        applied_by_order.get(broker_order_id, (0.0, 0.0)),
                    )
                persisted_cost_watermarks = {
                    broker_order_id: applied_costs_by_order.get(
                        broker_order_id,
                        (0.0, 0.0),
                    )
                    for broker_order_id in watermarks
                }
                self.state_store.apply_fill_reconciliation(
                    run_id,
                    next_state,
                    watermarks,
                    payload,
                    account_states=account_states,
                    cost_watermarks=persisted_cost_watermarks,
                )

        self.audit_logger.log(run_id, str(SystemEventType.FILL_RECONCILIATION), payload)
        return result

    def _order_context(self, broker_order_id: str) -> "_OrderContext | None":
        for row in self.state_store.list_system_events_by_broker_order_id(
            broker_order_id,
            str(SystemEventType.LIVE_ORDER_RESULT),
        ):
            payload = row["payload"]
            request = payload.get("request", {})
            account_id = str(request.get("account_id") or "")
            # Attribution buckets are execution sleeves ("crescendo_us"), not the
            # currency sleeve that request["sleeve"] carries ("USD").
            bucket_id = str(request.get("execution_sleeve") or "")
            currency = str(request.get("currency") or "KRW")
            if account_id:
                attribution = AccountAttributionReconciliationService(
                    self.state_store,
                    self.audit_logger,
                )
                has_attribution = attribution.has_attribution(account_id)
                return _OrderContext(
                    account_id=account_id,
                    bucket_id=bucket_id if bucket_id and has_attribution else None,
                    currency=currency,
                    # An account keeping an attribution ledger must tell us which
                    # bucket the fill belongs to. Missing it means the ledger will
                    # silently drift, so surface it instead of skipping quietly.
                    attribution_bucket_unresolved=has_attribution and not bucket_id,
                )
        return None

    def _apply_settlement_costs(
        self,
        aggregate_state: PortfolioState,
        account_states: dict[str, PortfolioState],
        costs_before: dict[str, tuple[str | None, float]],
        currencies: dict[str, str],
    ) -> list[SettlementCashAdjustment]:
        if self.account_snapshot_refresher is None:
            return []
        adjustments: list[SettlementCashAdjustment] = []
        for account_id, account_state in account_states.items():
            self.account_snapshot_refresher(account_id)
            snapshot = _latest_snapshot_for_account(self.state_store, account_id)
            if snapshot is None or not _positions_match(snapshot, account_state):
                continue
            before_date, before_costs = costs_before.get(account_id, (None, 0.0))
            after_date, costs_after = _transaction_cost_state(snapshot)
            baseline_costs = before_costs if before_date == after_date else 0.0
            costs_delta = costs_after - baseline_costs
            if costs_delta <= 0:
                continue
            currency = currencies.get(account_id, "KRW")
            _apply_cash_adjustment(account_state, -costs_delta, currency=currency)
            _apply_cash_adjustment(aggregate_state, -costs_delta, currency=currency)
            adjustments.append(
                SettlementCashAdjustment(
                    account_id=account_id,
                    currency=currency,
                    amount=-costs_delta,
                    transaction_costs_before=baseline_costs,
                    transaction_costs_after=costs_after,
                    broker_snapshot_id=int(snapshot["id"]),
                )
            )
        return adjustments

    def _load_applied_watermarks(self) -> dict[str, tuple[float, float]]:
        self.state_store.seed_fill_watermarks_from_events()
        return self.state_store.load_fill_watermarks()


def _fill_delta(
    snapshot: LiveOrderStatusSnapshot,
    applied_by_order: dict[str, tuple[float, float]],
    *,
    cost_delta: tuple[float, float, float, float] | None = None,
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
    total_notional = (
        snapshot.cumulative_filled_amount
        if snapshot.cumulative_filled_amount is not None
        else snapshot.partial_fill.filled_notional
    )
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
            commission=cost_delta[0] if cost_delta is not None else 0.0,
            tax=cost_delta[1] if cost_delta is not None else 0.0,
            cumulative_commission=cost_delta[2] if cost_delta is not None else 0.0,
            cumulative_tax=cost_delta[3] if cost_delta is not None else 0.0,
        ),
        None,
    )


def _execution_cost_delta(
    snapshot: LiveOrderStatusSnapshot,
    applied_costs_by_order: dict[str, tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    if snapshot.broker_order.broker != "toss":
        return None
    if snapshot.cumulative_commission is None and snapshot.cumulative_tax is None:
        return None
    cumulative_commission = float(snapshot.cumulative_commission or 0.0)
    cumulative_tax = float(snapshot.cumulative_tax or 0.0)
    if cumulative_commission < 0 or cumulative_tax < 0:
        return None
    previous_commission, previous_tax = applied_costs_by_order.get(
        snapshot.broker_order.broker_order_id,
        (0.0, 0.0),
    )
    if (
        cumulative_commission < previous_commission
        or cumulative_tax < previous_tax
    ):
        return None
    if (
        abs(cumulative_commission - previous_commission) < 1e-12
        and abs(cumulative_tax - previous_tax) < 1e-12
    ):
        return None
    return (
        cumulative_commission - previous_commission,
        cumulative_tax - previous_tax,
        cumulative_commission,
        cumulative_tax,
    )


def _skipped(snapshot: LiveOrderStatusSnapshot, reason: str) -> SkippedFill:
    return SkippedFill(
        broker_order_id=snapshot.broker_order.broker_order_id,
        status_checked_at=snapshot.checked_at,
        reason=reason,
        status=snapshot.status,
    )


@dataclass(frozen=True)
class _OrderContext:
    account_id: str
    bucket_id: str | None
    currency: str
    attribution_bucket_unresolved: bool = False


def _apply_fill(
    state: PortfolioState,
    fill: AppliedFill,
    *,
    currency: str | None = None,
) -> None:
    signed_quantity = fill.quantity if fill.side == OrderSide.BUY else -fill.quantity
    signed_notional = fill.notional if fill.side == OrderSide.BUY else -fill.notional
    _apply_cash_adjustment(state, -signed_notional, currency=currency)
    next_quantity = state.positions.get(fill.symbol, 0.0) + signed_quantity
    if abs(next_quantity) < 1e-12:
        state.positions.pop(fill.symbol, None)
    else:
        state.positions[fill.symbol] = next_quantity


def _apply_cash_adjustment(
    state: PortfolioState,
    amount: float,
    *,
    currency: str | None,
) -> None:
    if state.cash_by_currency and currency:
        state.cash_by_currency[currency] = state.cash_by_currency.get(currency, 0.0) + amount
        state.cash = float(
            state.cash_by_currency.get("KRW", sum(state.cash_by_currency.values()))
        )
        return
    state.cash += amount


def _latest_snapshot_for_account(store: StateStore, account_id: str) -> dict | None:
    for row in store.list_broker_account_snapshots(limit=1000):
        payload = row.get("payload") or {}
        logical_account_id = str(payload.get("account_id") or row.get("account_id") or "")
        if logical_account_id == account_id:
            return row
    return None


def _transaction_cost_state(snapshot: dict | None) -> tuple[str | None, float]:
    if snapshot is None:
        return None, 0.0
    account = (snapshot.get("payload") or {}).get("account") or {}
    cash_balance = account.get("cash_balance") or {}
    fetched_at = account.get("fetched_at") or snapshot.get("created_at")
    trading_date = None
    if fetched_at:
        parsed = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        trading_date = parsed.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
    return trading_date, float(cash_balance.get("transaction_costs_today") or 0.0)


def _positions_match(snapshot: dict, state: PortfolioState) -> bool:
    account = (snapshot.get("payload") or {}).get("account") or {}
    broker_positions = {
        str(position.get("symbol") or ""): float(position.get("quantity") or 0.0)
        for position in account.get("positions", [])
        if position.get("symbol")
    }
    symbols = set(broker_positions) | set(state.positions)
    return all(
        abs(broker_positions.get(symbol, 0.0) - state.positions.get(symbol, 0.0)) < 1e-9
        for symbol in symbols
    )


__all__ = ["PartialFillReconciliationService"]

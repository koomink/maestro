from collections.abc import Callable
from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, RunMode
from maestro.execution.base import OrderIntent
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore


class LiveExecutionGateService:
    def __init__(
        self,
        config: MaestroConfig,
        state_store: StateStore,
        audit: AuditLogger,
        *,
        now_fn: Callable[[], datetime] = utc_now,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit = audit
        self._now = now_fn

    def evaluate(
        self,
        run_id: str,
        orders: list[OrderIntent],
        data_quality_issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.config.mode != RunMode.LIVE_APPROVAL:
            return []
        blocks = []
        if data_quality_issues:
            payload = {"issues": data_quality_issues, "mode": self.config.mode.value}
            self._record_event(run_id, SystemEventType.STALE_DATA_HALT, payload)
            blocks.append({"event_type": SystemEventType.STALE_DATA_HALT.value, **payload})
        if not orders:
            return blocks

        recovery_block = self._live_recovery_block()
        if recovery_block is not None:
            self._record_event(run_id, SystemEventType.LIVE_ORDER_RECOVERY_HALT, recovery_block)
            blocks.append(
                {"event_type": SystemEventType.LIVE_ORDER_RECOVERY_HALT.value, **recovery_block}
            )

        market_session_block = self._market_session_block()
        if market_session_block is not None:
            self._record_event(run_id, SystemEventType.MARKET_SESSION_HALT, market_session_block)
            blocks.append(
                {"event_type": SystemEventType.MARKET_SESSION_HALT.value, **market_session_block}
            )

        reconciliation_block = self._reconciliation_block()
        if reconciliation_block is not None:
            self._record_event(
                run_id,
                SystemEventType.BROKER_RECONCILIATION_HALT,
                reconciliation_block,
            )
            blocks.append(
                {
                    "event_type": SystemEventType.BROKER_RECONCILIATION_HALT.value,
                    **reconciliation_block,
                }
            )

        limit_block = self._daily_limit_block(orders)
        if limit_block is not None:
            self._record_event(run_id, SystemEventType.LIVE_ORDER_LIMIT_HALT, limit_block)
            blocks.append(
                {"event_type": SystemEventType.LIVE_ORDER_LIMIT_HALT.value, **limit_block}
            )

        instrument_block = self._instrument_validation_block(orders)
        if instrument_block is not None:
            self._record_event(
                run_id,
                SystemEventType.INSTRUMENT_VALIDATION_HALT,
                instrument_block,
            )
            blocks.append(
                {
                    "event_type": SystemEventType.INSTRUMENT_VALIDATION_HALT.value,
                    **instrument_block,
                }
            )

        broker_quote_block = self._broker_quote_validation_block(orders)
        if broker_quote_block is not None:
            self._record_event(
                run_id,
                SystemEventType.BROKER_QUOTE_VALIDATION_HALT,
                broker_quote_block,
            )
            blocks.append(
                {
                    "event_type": SystemEventType.BROKER_QUOTE_VALIDATION_HALT.value,
                    **broker_quote_block,
                }
            )

        broker_risk_block = self._broker_risk_block(orders)
        if broker_risk_block is not None:
            self._record_event(run_id, SystemEventType.BROKER_RISK_HALT, broker_risk_block)
            blocks.append(
                {"event_type": SystemEventType.BROKER_RISK_HALT.value, **broker_risk_block}
            )
        return blocks

    def _market_session_block(self) -> dict[str, Any] | None:
        market_session = self.config.execution.market_session
        if not market_session.required:
            return None
        try:
            timezone = ZoneInfo(market_session.timezone)
        except Exception:
            return {
                "reason": "invalid_market_session_timezone",
                "timezone": market_session.timezone,
            }
        local_now = self._now().astimezone(timezone)
        local_date = local_now.date().isoformat()
        local_time = local_now.time()
        open_time = _parse_hhmm(market_session.open)
        close_time = _parse_hhmm(market_session.close)
        payload = {
            "checked_at": self._now().isoformat(),
            "local_time": local_now.isoformat(),
            "timezone": market_session.timezone,
            "open": market_session.open,
            "close": market_session.close,
        }
        if local_now.weekday() not in market_session.weekdays:
            return {"reason": "market_weekday_closed", **payload}
        if local_date in market_session.holidays:
            return {"reason": "market_holiday_closed", "date": local_date, **payload}
        if not (open_time <= local_time < close_time):
            return {"reason": "outside_market_session", **payload}
        return None

    def _live_recovery_block(self) -> dict[str, Any] | None:
        latest_completion = self.state_store.load_latest_system_event(
            SystemEventType.LIVE_ORDER_RECOVERY_COMPLETED
        )
        completed_after_event_id = int(latest_completion["id"]) if latest_completion else 0
        latest_required = self.state_store.load_latest_system_event(
            SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED
        )
        if latest_required is not None and int(latest_required["id"]) > completed_after_event_id:
            payload = latest_required["payload"]
            request = payload.get("request", {})
            result = payload.get("result", {})
            return {
                "reason": "live_order_recovery_required",
                "recovery_event_id": latest_required["id"],
                "order_id": request.get("order_id"),
                "broker_order_id": (result.get("broker_order") or {}).get("broker_order_id"),
                "message": result.get("message"),
            }

        lifecycle_order_ids = {
            str(row["payload"].get("order_id"))
            for row in self.state_store.list_system_events_by_type(
                SystemEventType.LIVE_ORDER_LIFECYCLE, limit=1000
            )
        }
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_RESULT, limit=1000
        ):
            if int(row["id"]) <= completed_after_event_id:
                continue
            request = row["payload"].get("request", {})
            order_id = str(request.get("order_id") or "")
            if order_id and order_id not in lifecycle_order_ids:
                result = row["payload"].get("result", {})
                return {
                    "reason": "live_order_lifecycle_incomplete",
                    "live_order_result_event_id": row["id"],
                    "order_id": order_id,
                    "broker_order_id": (result.get("broker_order") or {}).get("broker_order_id"),
                }
        return None

    def _reconciliation_block(self) -> dict[str, Any] | None:
        if not self.config.execution.require_reconciliation_pass:
            return None
        latest = self.state_store.load_latest_system_event(SystemEventType.BROKER_RECONCILIATION)
        if latest is None:
            return {"reason": "missing_reconciliation"}
        if latest["payload"].get("passed") is not True:
            return {
                "reason": "failed_reconciliation",
                "reconciliation": latest["payload"],
            }
        created_at = _parse_store_created_at(latest["created_at"])
        age_seconds = (self._now() - created_at).total_seconds()
        if age_seconds > self.config.reconciliation.max_age_seconds:
            return {
                "reason": "stale_reconciliation",
                "created_at": latest["created_at"],
                "age_seconds": age_seconds,
                "max_age_seconds": self.config.reconciliation.max_age_seconds,
            }
        return None

    def _daily_limit_block(self, orders: list[OrderIntent]) -> dict[str, Any] | None:
        today = self._now().date().isoformat()
        existing_notional = 0.0
        existing_count = 0
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_RESULT, limit=1000
        ):
            payload = row["payload"]
            if payload.get("submitted_date") == today:
                existing_count += 1
                existing_notional += float(payload.get("notional", 0.0))
        proposed_notional = sum(order.notional for order in orders)
        proposed_count = len(orders)
        limits = self.config.execution.live_order_limits
        if existing_notional + proposed_notional > limits.max_daily_notional:
            return {
                "reason": "daily_notional_exceeded",
                "existing_notional": existing_notional,
                "proposed_notional": proposed_notional,
                "max_daily_live_notional": limits.max_daily_notional,
            }
        max_count = limits.max_daily_order_count
        if max_count > 0 and existing_count + proposed_count > max_count:
            return {
                "reason": "daily_order_count_exceeded",
                "existing_count": existing_count,
                "proposed_count": proposed_count,
                "max_daily_live_order_count": max_count,
            }
        return None

    def _instrument_validation_block(self, orders: list[OrderIntent]) -> dict[str, Any] | None:
        instruments = {
            instrument.symbol: instrument for instrument in self.config.universe.instruments
        }
        if not instruments:
            return None
        for order in orders:
            instrument = instruments.get(order.symbol)
            if instrument is None:
                return {"reason": "missing_instrument", "symbol": order.symbol}
            if (
                self.config.portfolio.allocation_mode != "currency_sleeves"
                and instrument.currency.value != self.config.portfolio.base_currency
            ):
                return {"reason": "currency_mismatch", "symbol": order.symbol}
            if instrument.broker_product not in self.config.kis.effective_broker_products():
                return {"reason": "broker_product_mismatch", "symbol": order.symbol}
            if order.quantity < instrument.min_order_quantity:
                return {"reason": "min_order_quantity", "symbol": order.symbol}
            if order.notional < instrument.min_order_notional:
                return {"reason": "min_order_notional", "symbol": order.symbol}
            if not _is_step_multiple(order.quantity, instrument.quantity_step):
                return {"reason": "quantity_step", "symbol": order.symbol}
            if not _is_step_multiple(order.price, instrument.price_tick):
                return {"reason": "price_tick", "symbol": order.symbol}
        return None

    def _broker_quote_validation_block(self, orders: list[OrderIntent]) -> dict[str, Any] | None:
        broker_validation = self.config.execution.broker_validation
        if not broker_validation.require_quote_validation:
            return None
        latest = self.state_store.load_latest_broker_account_snapshot()
        if latest is None:
            return {"reason": "missing_broker_snapshot"}
        current_prices = latest["payload"].get("current_prices", {})
        for order in orders:
            quote = current_prices.get(order.symbol)
            if quote is None:
                return {"reason": "missing_broker_quote", "symbol": order.symbol}
            quote_value = float(quote)
            if quote_value <= 0:
                return {
                    "reason": "invalid_broker_quote",
                    "symbol": order.symbol,
                    "broker_quote": quote_value,
                }
            deviation = abs(order.price - quote_value) / quote_value
            if deviation > broker_validation.max_quote_deviation_pct:
                return {
                    "reason": "broker_quote_deviation_exceeded",
                    "symbol": order.symbol,
                    "order_price": order.price,
                    "broker_quote": quote_value,
                    "deviation_pct": deviation,
                    "max_deviation_pct": broker_validation.max_quote_deviation_pct,
                }
        return None

    def _broker_risk_block(self, orders: list[OrderIntent]) -> dict[str, Any] | None:
        execution = self.config.execution
        broker_validation = execution.broker_validation
        limits = execution.live_order_limits
        if not broker_validation.require_risk_validation and limits.daily_loss_limit is None:
            return None
        latest = self.state_store.load_latest_broker_account_snapshot()
        if latest is None:
            return {"reason": "missing_broker_snapshot"}

        snapshot = latest["payload"]
        account = snapshot.get("account", {})
        issues = []
        if broker_validation.require_risk_validation:
            issues.extend(self._broker_reconciliation_risk_issues(latest))
            issues.extend(self._pending_broker_order_issues(snapshot))
            issues.extend(self._cash_and_exposure_risk_issues(orders, snapshot))
        daily_loss_issue = self._daily_loss_risk_issue(snapshot)
        if daily_loss_issue is not None:
            issues.append(daily_loss_issue)
        if not issues:
            return None
        return {
            "reason": "broker_risk_failed",
            "broker_snapshot_id": latest.get("id"),
            "account_id": account.get("account_id"),
            "issues": issues,
        }

    def _broker_reconciliation_risk_issues(
        self, latest_snapshot: dict[str, Any]
    ) -> list[dict[str, Any]]:
        latest_reconciliation = self.state_store.load_latest_system_event(
            SystemEventType.BROKER_RECONCILIATION
        )
        if latest_reconciliation is None:
            return [{"reason": "missing_reconciliation"}]
        payload = latest_reconciliation["payload"]
        issues = []
        if payload.get("passed") is not True:
            issues.append({"reason": "failed_reconciliation"})
        if payload.get("issues"):
            issues.append(
                {
                    "reason": "broker_reconciliation_has_issues",
                    "issue_count": len(payload["issues"]),
                }
            )
        reconciled_snapshot_id = payload.get("broker_snapshot_id")
        if reconciled_snapshot_id is None:
            issues.append({"reason": "broker_reconciliation_snapshot_unknown"})
        elif int(reconciled_snapshot_id) != int(latest_snapshot["id"]):
            issues.append(
                {
                    "reason": "broker_snapshot_not_reconciled",
                    "latest_broker_snapshot_id": latest_snapshot["id"],
                    "reconciled_broker_snapshot_id": reconciled_snapshot_id,
                }
            )
        return issues

    def _pending_broker_order_issues(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        pending_orders = snapshot.get("unfilled_orders", [])
        if not pending_orders:
            return []
        return [
            {
                "reason": "pending_broker_orders",
                "pending_order_count": len(pending_orders),
            }
        ]

    def _cash_and_exposure_risk_issues(
        self,
        orders: list[OrderIntent],
        snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        account = snapshot.get("account", {})
        current_prices = snapshot.get("current_prices", {})
        cash = float(account.get("cash", 0.0))
        buying_power = float(account.get("buying_power", 0.0))
        buy_notional = sum(order.notional for order in orders if order.side == OrderSide.BUY)
        sell_notional = sum(order.notional for order in orders if order.side == OrderSide.SELL)
        fee_buffer = (
            buy_notional + sell_notional
        ) * self.config.execution.live_order_limits.fee_buffer_pct
        required_buying_power = buy_notional + fee_buffer
        cash_after_orders = cash - buy_notional + sell_notional - fee_buffer
        positions = _broker_position_quantities(account)
        prices = _broker_position_prices(account, current_prices)
        issues = []

        if required_buying_power > buying_power:
            issues.append(
                {
                    "reason": "buying_power_exceeded",
                    "required_buying_power": required_buying_power,
                    "buying_power": buying_power,
                }
            )
        if cash_after_orders < 0:
            issues.append(
                {
                    "reason": "cash_exceeded",
                    "cash_after_orders": cash_after_orders,
                    "cash": cash,
                }
            )
        for order in orders:
            prices[order.symbol] = _float_or_default(current_prices.get(order.symbol), order.price)
            signed_quantity = order.quantity if order.side == OrderSide.BUY else -order.quantity
            positions[order.symbol] = positions.get(order.symbol, 0.0) + signed_quantity
            if positions[order.symbol] < -1e-9:
                issues.append(
                    {
                        "reason": "short_position_after_order",
                        "symbol": order.symbol,
                        "quantity_after_orders": positions[order.symbol],
                    }
                )

        position_values = {
            symbol: max(quantity, 0.0) * prices.get(symbol, 0.0)
            for symbol, quantity in positions.items()
            if abs(quantity) > 1e-12
        }
        total_value = cash_after_orders + sum(position_values.values())
        if total_value <= 0:
            issues.append({"reason": "nonpositive_broker_equity_after_orders"})
            return issues

        cash_weight = cash_after_orders / total_value
        if cash_weight < self.config.risk.min_cash_weight:
            issues.append(
                {
                    "reason": "cash_reserve_breached",
                    "cash_weight_after_orders": cash_weight,
                    "min_cash_weight": self.config.risk.min_cash_weight,
                }
            )
        exposure_weight = sum(position_values.values()) / total_value
        max_exposure_weight = 1.0 - self.config.risk.min_cash_weight
        if exposure_weight > max_exposure_weight + 1e-9:
            issues.append(
                {
                    "reason": "portfolio_exposure_exceeded",
                    "exposure_weight_after_orders": exposure_weight,
                    "max_exposure_weight": max_exposure_weight,
                }
            )
        for symbol, value in sorted(position_values.items()):
            weight = value / total_value
            if weight > self.config.risk.max_single_asset_weight + 1e-9:
                issues.append(
                    {
                        "reason": "symbol_exposure_exceeded",
                        "symbol": symbol,
                        "weight_after_orders": weight,
                        "max_single_asset_weight": self.config.risk.max_single_asset_weight,
                    }
                )
        return issues

    def _daily_loss_risk_issue(self, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        daily_loss_limit = self.config.execution.live_order_limits.daily_loss_limit
        if daily_loss_limit is None:
            return None
        normalized_pnl = _normalized_broker_pnl(snapshot)
        if normalized_pnl is None:
            return {
                "reason": "broker_pnl_unavailable",
                "daily_loss_limit": daily_loss_limit,
            }
        pnl_value, pnl_source = normalized_pnl
        if pnl_value <= -daily_loss_limit:
            return {
                "reason": "daily_loss_limit_exceeded",
                "broker_pnl": pnl_value,
                "pnl_source": pnl_source,
                "daily_loss_limit": daily_loss_limit,
            }
        return None

    def _record_event(
        self,
        run_id: str,
        event_type: SystemEventType,
        payload: dict[str, Any],
    ) -> None:
        save_audited_system_event(self.state_store, self.audit, run_id, event_type, payload)


def _parse_hhmm(value: str) -> time:
    parsed = datetime.strptime(value, "%H:%M")
    return time(hour=parsed.hour, minute=parsed.minute)


def _parse_store_created_at(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _is_step_multiple(value: float, step: float) -> bool:
    scaled = value / step
    return abs(scaled - round(scaled)) < 1e-9


def _broker_position_quantities(account: dict[str, Any]) -> dict[str, float]:
    quantities: dict[str, float] = {}
    for position in account.get("positions", []):
        symbol = str(position.get("symbol") or "")
        if not symbol:
            continue
        quantities[symbol] = quantities.get(symbol, 0.0) + float(position.get("quantity", 0.0))
    return quantities


def _broker_position_prices(
    account: dict[str, Any],
    current_prices: dict[str, Any],
) -> dict[str, float]:
    prices = {symbol: float(price) for symbol, price in current_prices.items()}
    for position in account.get("positions", []):
        symbol = str(position.get("symbol") or "")
        if not symbol or symbol in prices:
            continue
        prices[symbol] = _float_or_default(position.get("current_price"), 0.0)
    return prices


def _normalized_broker_pnl(snapshot: dict[str, Any]) -> tuple[float, str] | None:
    account = snapshot.get("account", {})
    for key in ("daily_pnl", "today_pnl", "realized_pnl", "pnl"):
        if key in account and account[key] is not None:
            return float(account[key]), f"account.{key}"
    position_pnls = [
        float(position["unrealized_pnl"])
        for position in account.get("positions", [])
        if position.get("unrealized_pnl") is not None
    ]
    if position_pnls:
        return sum(position_pnls), "account.positions.unrealized_pnl"
    if "daily_pnl" in snapshot and snapshot["daily_pnl"] is not None:
        return float(snapshot["daily_pnl"]), "snapshot.daily_pnl"
    return None


def _float_or_default(value: Any, default: float) -> float:
    if value is None:
        return default
    return float(value)

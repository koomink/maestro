from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.enums import Currency, OrderSide, OrderType, RunMode
from maestro.core.trading_day import trading_day_bounds_utc_str
from maestro.execution.base import OrderIntent
from maestro.execution.broker_router import BrokerAccountRouter
from maestro.monitoring.audit_logger import AuditLogger
from maestro.ops.workflow_recovery import WorkflowRecoveryService
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

        market_session_block = self._market_session_block(orders)
        if market_session_block is not None:
            self._record_event(run_id, SystemEventType.MARKET_SESSION_HALT, market_session_block)
            blocks.append(
                {"event_type": SystemEventType.MARKET_SESSION_HALT.value, **market_session_block}
            )

        reconciliation_block = self._reconciliation_block(orders)
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

    def _market_session_block(self, orders: list[OrderIntent]) -> dict[str, Any] | None:
        market_session = self.config.execution.market_session
        if not market_session.required:
            return None
        instruments = {
            instrument.symbol: instrument for instrument in self.config.universe.instruments
        }
        sessions_by_exchange = self.config.execution.market_sessions_by_exchange
        if sessions_by_exchange:
            checked_sessions: set[str] = set()
            for order in orders:
                instrument = instruments.get(order.symbol)
                exchange_code = str(instrument.exchange_code) if instrument else None
                session = sessions_by_exchange.get(exchange_code, market_session)
                session_key = exchange_code or "default"
                if session_key in checked_sessions:
                    continue
                checked_sessions.add(session_key)
                block = self._market_session_block_for_session(session, exchange_code=exchange_code)
                if block is not None:
                    return block
            return None
        return self._market_session_block_for_session(market_session, exchange_code=None)

    def _market_session_block_for_session(
        self,
        market_session,
        *,
        exchange_code: str | None,
    ) -> dict[str, Any] | None:
        try:
            timezone = ZoneInfo(market_session.timezone)
        except Exception:
            return {
                "reason": "invalid_market_session_timezone",
                "timezone": market_session.timezone,
                **({"exchange_code": exchange_code} if exchange_code else {}),
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
            **({"exchange_code": exchange_code} if exchange_code else {}),
        }
        if open_time > close_time:
            if not (local_time >= open_time or local_time < close_time):
                return {"reason": "outside_market_session", **payload}
            session_start_date = (
                local_now.date()
                if local_time >= open_time
                else local_now.date() - timedelta(days=1)
            )
            session_start_date_text = session_start_date.isoformat()
            if session_start_date.weekday() not in market_session.weekdays:
                return {"reason": "market_weekday_closed", **payload}
            if session_start_date_text in market_session.holidays:
                return {
                    "reason": "market_holiday_closed",
                    "date": session_start_date_text,
                    **payload,
                }
            return None
        if local_now.weekday() not in market_session.weekdays:
            return {"reason": "market_weekday_closed", **payload}
        if local_date in market_session.holidays:
            return {"reason": "market_holiday_closed", "date": local_date, **payload}
        if not (open_time <= local_time < close_time):
            return {"reason": "outside_market_session", **payload}
        return None

    def _live_recovery_block(self) -> dict[str, Any] | None:
        preview = WorkflowRecoveryService(
            self.config,
            self.state_store,
            self.audit,
            now_fn=self._now,
        ).preview()
        if not preview.blockers:
            return None
        blocker = preview.blockers[0]
        return {
            "reason": blocker.reason,
            "recovery_event_id": blocker.event_id,
            "recovery_fingerprint": preview.fingerprint,
            "order_id": blocker.order_id,
            "broker_order_id": blocker.broker_order_id,
            "pending_recovery_count": len(preview.blockers),
        }

    def _reconciliation_block(
        self,
        orders: list[OrderIntent] | None = None,
    ) -> dict[str, Any] | None:
        if not self.config.execution.require_reconciliation_pass:
            return None
        required_account_ids = {
            str(order.account_id) for order in orders or [] if order.account_id is not None
        }
        if required_account_ids:
            latest_by_account: dict[str, dict[str, Any]] = {}
            events = self.state_store.list_system_events_by_type(
                SystemEventType.BROKER_RECONCILIATION,
                limit=max(100, len(required_account_ids) * 20),
            )
            for event in events:
                for result in event["payload"].get("account_results") or []:
                    account_id = str(result.get("account_id") or "")
                    if account_id in required_account_ids and account_id not in latest_by_account:
                        latest_by_account[account_id] = {
                            "event": event,
                            "result": result,
                        }
            missing = sorted(required_account_ids - set(latest_by_account))
            if missing:
                # Legacy single-account reconciliation events predate
                # account_results. Preserve them only for a single required
                # account; multi-account execution must have scoped evidence.
                if len(required_account_ids) == 1 and events:
                    latest = events[0]
                    if latest["payload"].get("passed") is True:
                        created_at = _parse_store_created_at(latest["created_at"])
                        age_seconds = (self._now() - created_at).total_seconds()
                        if age_seconds <= self.config.reconciliation.max_age_seconds:
                            return None
                    if latest["payload"].get("passed") is False:
                        return {
                            "reason": "failed_reconciliation",
                            "account_id": next(iter(required_account_ids)),
                            "reconciliation": latest["payload"],
                        }
                return {"reason": "missing_reconciliation", "account_ids": missing}
            for account_id in sorted(required_account_ids):
                item = latest_by_account[account_id]
                if item["result"].get("passed") is not True:
                    return {
                        "reason": "failed_reconciliation",
                        "account_id": account_id,
                        "reconciliation": item["result"],
                    }
                drift_levels = {
                    str(observation.get("drift_level") or "")
                    for observation in item["result"].get("observations") or []
                }
                if drift_levels & {"L3"}:
                    return {
                        "reason": "buying_power_drift_requires_review",
                        "account_id": account_id,
                        "drift_levels": sorted(drift_levels & {"L3"}),
                        "reconciliation": item["result"],
                    }
                created_at = _parse_store_created_at(item["event"]["created_at"])
                age_seconds = (self._now() - created_at).total_seconds()
                if age_seconds > self.config.reconciliation.max_age_seconds:
                    return {
                        "reason": "stale_reconciliation",
                        "account_id": account_id,
                        "created_at": item["event"]["created_at"],
                        "age_seconds": age_seconds,
                        "max_age_seconds": self.config.reconciliation.max_age_seconds,
                    }
            return None
        latest = self.state_store.load_latest_system_event(SystemEventType.BROKER_RECONCILIATION)
        if latest is None:
            return {"reason": "missing_reconciliation"}
        if latest["payload"].get("passed") is not True:
            return {
                "reason": "failed_reconciliation",
                "reconciliation": latest["payload"],
            }
        drift_levels = {
            str(observation.get("drift_level") or "")
            for observation in latest["payload"].get("observations") or []
        }
        if drift_levels & {"L3"}:
            return {
                "reason": "buying_power_drift_requires_review",
                "drift_levels": sorted(drift_levels & {"L3"}),
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
        start_utc, end_utc = trading_day_bounds_utc_str(
            self.config.execution.market_session.timezone, at=self._now()
        )
        existing_count = 0
        existing_notional_by_currency: dict[Currency, float] = {}
        for event_type in (
            SystemEventType.LIVE_ORDER_RESULT,
            SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED,
            SystemEventType.LIVE_ORDER_HALT,
        ):
            for row in self.state_store.list_system_events_in_range(
                event_type,
                start_utc=start_utc,
                end_utc=end_utc,
            ):
                payload = row["payload"]
                if (
                    event_type == SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED
                    and "submitted_date" not in payload
                ):
                    continue
                existing_count += 1
                currency = self._event_currency(payload)
                existing_notional_by_currency[currency] = existing_notional_by_currency.get(
                    currency, 0.0
                ) + float(payload.get("notional", 0.0))

        proposed_notional_by_currency: dict[Currency, float] = {}
        limits = self.config.execution.live_order_limits
        for order in orders:
            currency = self._order_currency(order)
            max_order_notional = limits.max_order_notional_for(currency)
            if max_order_notional is None:
                return {
                    "reason": "max_order_notional_limit_missing",
                    "currency": currency.value,
                    "symbol": order.symbol,
                }
            if order.notional > max_order_notional:
                return {
                    "reason": "max_order_notional_exceeded",
                    "currency": currency.value,
                    "symbol": order.symbol,
                    "order_notional": order.notional,
                    "max_live_order_notional": max_order_notional,
                }
            proposed_notional_by_currency[currency] = (
                proposed_notional_by_currency.get(currency, 0.0) + order.notional
            )

        for currency, proposed_notional in proposed_notional_by_currency.items():
            max_daily_notional = limits.max_daily_notional_for(currency)
            if max_daily_notional is None:
                return {
                    "reason": "daily_notional_limit_missing",
                    "currency": currency.value,
                }
            existing_notional = existing_notional_by_currency.get(currency, 0.0)
            if existing_notional + proposed_notional > max_daily_notional:
                return {
                    "reason": "daily_notional_exceeded",
                    "currency": currency.value,
                    "existing_notional": existing_notional,
                    "proposed_notional": proposed_notional,
                    "max_daily_live_notional": max_daily_notional,
                }
        proposed_count = len(orders)
        max_count = limits.max_daily_order_count
        if max_count > 0 and existing_count + proposed_count > max_count:
            return {
                "reason": "daily_order_count_exceeded",
                "existing_count": existing_count,
                "proposed_count": proposed_count,
                "max_daily_live_order_count": max_count,
            }
        return None

    def _order_currency(self, order: OrderIntent) -> Currency:
        if order.currency is not None:
            return order.currency
        instruments = {
            instrument.symbol: instrument for instrument in self.config.universe.instruments
        }
        instrument = instruments.get(order.symbol)
        if instrument is not None:
            return instrument.currency
        return Currency(self.config.portfolio.base_currency)

    def _event_currency(self, payload: dict[str, Any]) -> Currency:
        request = payload.get("request")
        if isinstance(request, dict) and request.get("currency"):
            return Currency(str(request["currency"]))
        if payload.get("currency"):
            return Currency(str(payload["currency"]))
        return Currency(self.config.portfolio.base_currency)

    def _instrument_validation_block(self, orders: list[OrderIntent]) -> dict[str, Any] | None:
        instruments = {
            instrument.symbol: instrument for instrument in self.config.universe.instruments
        }
        account_router = BrokerAccountRouter(self.config)
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
            broker_products = _broker_products_for_order(account_router, order)
            if broker_products is not None and instrument.broker_product not in broker_products:
                return {"reason": "broker_product_mismatch", "symbol": order.symbol}
            broker = _broker_for_order(account_router, order)
            if broker == "toss" and order.order_type != OrderType.LIMIT:
                return {
                    "reason": "unsupported_order_type",
                    "symbol": order.symbol,
                    "account_id": order.account_id,
                    "broker": "toss",
                    "order_type": order.order_type.value,
                }
            if broker == "toss" and not _is_step_multiple(
                order.quantity,
                1.0,
            ):
                return {
                    "reason": "integer_quantity_required",
                    "symbol": order.symbol,
                    "account_id": order.account_id,
                    "broker": "toss",
                    "quantity": order.quantity,
                }
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
        # Quotes must come from the snapshot of the account each order trades
        # in: the globally-latest snapshot belongs to whichever account was
        # fetched last and does not carry quotes for other accounts' symbols.
        snapshots_by_account = _latest_broker_snapshots_by_account(self.state_store, self.config)
        if not snapshots_by_account:
            return {"reason": "missing_broker_snapshot"}
        account_router = BrokerAccountRouter(self.config)
        for account_id, account_orders in _orders_by_account(orders).items():
            latest = _snapshot_for_account(snapshots_by_account, account_router, account_id)
            if latest is None:
                return {"reason": "missing_broker_snapshot", "account_id": account_id}
            current_prices = latest["payload"].get("current_prices", {})
            for order in account_orders:
                quote = current_prices.get(order.symbol)
                if quote is None:
                    return {
                        "reason": "missing_broker_quote",
                        "symbol": order.symbol,
                        "account_id": account_id,
                    }
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
        if not broker_validation.require_risk_validation and not limits.has_daily_loss_limit():
            return None
        snapshots_by_account = _latest_broker_snapshots_by_account(self.state_store, self.config)
        if not snapshots_by_account:
            return {"reason": "missing_broker_snapshot"}

        issues = []
        account_router = BrokerAccountRouter(self.config)
        if broker_validation.require_risk_validation:
            for account_id, account_orders in _orders_by_account(orders).items():
                latest = _snapshot_for_account(snapshots_by_account, account_router, account_id)
                if latest is None:
                    issues.append(
                        {
                            "reason": "missing_broker_snapshot",
                            "account_id": account_id,
                        }
                    )
                    continue
                snapshot = latest["payload"]
                issues.extend(self._broker_reconciliation_risk_issues(latest))
                issues.extend(self._pending_broker_order_issues(snapshot))
                issues.extend(self._cash_and_exposure_risk_issues(account_orders, snapshot))
        seen_snapshot_ids: set[int] = set()
        for latest in snapshots_by_account.values():
            snapshot_id = int(latest["id"])
            if snapshot_id in seen_snapshot_ids:
                continue
            seen_snapshot_ids.add(snapshot_id)
            issues.extend(self._daily_loss_risk_issues(latest["payload"]))
        if not issues:
            return None
        latest_ids = sorted({int(snapshot["id"]) for snapshot in snapshots_by_account.values()})
        return {
            "reason": "broker_risk_failed",
            "broker_snapshot_ids": latest_ids,
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
        # Multi-account reconciliation records the max snapshot id at the top
        # level and one id per account in account_results; an account snapshot
        # counts as reconciled when it appears in either place.
        reconciled_ids: set[int] = set()
        reconciled_snapshot_id = payload.get("broker_snapshot_id")
        if reconciled_snapshot_id is not None:
            reconciled_ids.add(int(reconciled_snapshot_id))
        for entry in payload.get("account_results") or []:
            entry_snapshot_id = entry.get("broker_snapshot_id")
            if entry_snapshot_id is not None:
                reconciled_ids.add(int(entry_snapshot_id))
        if not reconciled_ids:
            issues.append({"reason": "broker_reconciliation_snapshot_unknown"})
        elif int(latest_snapshot["id"]) not in reconciled_ids:
            issues.append(
                {
                    "reason": "broker_snapshot_not_reconciled",
                    "latest_broker_snapshot_id": latest_snapshot["id"],
                    "reconciled_broker_snapshot_id": reconciled_snapshot_id,
                    "reconciled_broker_snapshot_ids": sorted(reconciled_ids),
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

    def _buying_power_issues(
        self,
        orders: list[OrderIntent],
        account: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Check buying power and cash one currency at a time.

        Sell proceeds fund the buys the batch submits after them, so the check
        nets them — but only within a currency. A KRW sell raises no dollars, and
        summing native notionals across currencies lets a large KRW sale offset a
        USD buy the account cannot pay for.
        """
        fee_buffer_pct = self.config.execution.live_order_limits.fee_buffer_pct
        by_currency: dict[str, dict[str, float]] = {}
        for order in orders:
            currency = self._order_currency(order).value
            totals = by_currency.setdefault(currency, {"buy": 0.0, "sell": 0.0})
            totals["buy" if order.side == OrderSide.BUY else "sell"] += order.notional
        buying_power_by_currency = account.get("buying_power_by_currency") or {}
        cash_by_currency = account.get("cash_by_currency") or {}
        ledgerless_cash = (
            str(account.get("source") or "").startswith("toss_")
            and "ledger_cash_by_currency" in account
            and account.get("ledger_cash_by_currency") is None
        )
        issues: list[dict[str, Any]] = []
        for currency, totals in sorted(by_currency.items()):
            fee_buffer = (totals["buy"] + totals["sell"]) * fee_buffer_pct
            required = max(0.0, totals["buy"] - totals["sell"]) + fee_buffer
            # One aggregate number does not say which currency it is denominated
            # in, so a single-currency batch cannot claim it either. Every real
            # adapter — KIS domestic, KIS overseas, Toss — reports the breakdown;
            # without it there is no honest split, so fail closed.
            if currency not in buying_power_by_currency:
                issues.append(
                    {
                        "reason": "buying_power_by_currency_unavailable",
                        "currency": currency,
                        "required_buying_power": required,
                    }
                )
            elif required > float(buying_power_by_currency[currency]):
                issues.append(
                    {
                        "reason": "buying_power_exceeded",
                        "currency": currency,
                        "required_buying_power": required,
                        "buying_power": float(buying_power_by_currency[currency]),
                    }
                )
            if ledgerless_cash:
                continue
            if currency not in cash_by_currency:
                issues.append(
                    {
                        "reason": "cash_by_currency_unavailable",
                        "currency": currency,
                    }
                )
                continue
            cash_after = (
                float(cash_by_currency[currency]) - totals["buy"] + totals["sell"] - fee_buffer
            )
            if cash_after < 0:
                issues.append(
                    {
                        "reason": "cash_exceeded",
                        "currency": currency,
                        "cash_after_orders": cash_after,
                        "cash": float(cash_by_currency[currency]),
                    }
                )
        return issues

    def _cash_and_exposure_risk_issues(
        self,
        orders: list[OrderIntent],
        snapshot: dict[str, Any],
    ) -> list[dict[str, Any]]:
        account = snapshot.get("account", {})
        current_prices = snapshot.get("current_prices", {})
        positions = _broker_position_quantities(account)
        prices = _broker_position_prices(account, current_prices)
        issues = []

        # Buying power and cash are both judged per currency; a KRW sale funds
        # nothing denominated in dollars.
        issues.extend(self._buying_power_issues(orders, account))
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
        # Equity is a whole-account figure, so it uses the account-level cash the
        # broker reports rather than any single currency's balance.
        cash_after_orders = (
            float(account.get("cash", 0.0))
            - sum(order.notional for order in orders if order.side == OrderSide.BUY)
            + sum(order.notional for order in orders if order.side == OrderSide.SELL)
        )
        total_value = cash_after_orders + sum(position_values.values())
        if total_value <= 0:
            issues.append({"reason": "nonpositive_broker_equity_after_orders"})
            return issues
        return issues

    def _daily_loss_risk_issues(self, snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        limits = self.config.execution.live_order_limits
        if limits.daily_loss_limit_by_currency:
            pnl_by_currency = _normalized_broker_pnl_by_currency(snapshot)
            if not pnl_by_currency:
                return [
                    {
                        "reason": "broker_pnl_unavailable",
                        "daily_loss_limit_by_currency": {
                            currency.value: limit
                            for currency, limit in limits.daily_loss_limit_by_currency.items()
                        },
                    }
                ]
            issues = []
            for currency, (pnl_value, pnl_source) in pnl_by_currency.items():
                daily_loss_limit = limits.daily_loss_limit_for(currency)
                if daily_loss_limit is None:
                    if pnl_value < 0:
                        issues.append(
                            {
                                "reason": "daily_loss_limit_missing",
                                "currency": currency.value,
                                "broker_pnl": pnl_value,
                                "pnl_source": pnl_source,
                            }
                        )
                    continue
                if pnl_value <= -daily_loss_limit:
                    issues.append(
                        {
                            "reason": "daily_loss_limit_exceeded",
                            "currency": currency.value,
                            "broker_pnl": pnl_value,
                            "pnl_source": pnl_source,
                            "daily_loss_limit": daily_loss_limit,
                        }
                    )
            return issues

        daily_loss_limit = limits.daily_loss_limit
        if daily_loss_limit is None:
            return []
        normalized_pnl = _normalized_broker_pnl(snapshot)
        if normalized_pnl is None:
            return [
                {
                    "reason": "broker_pnl_unavailable",
                    "daily_loss_limit": daily_loss_limit,
                }
            ]
        pnl_value, pnl_source = normalized_pnl
        if pnl_value <= -daily_loss_limit:
            return [
                {
                    "reason": "daily_loss_limit_exceeded",
                    "broker_pnl": pnl_value,
                    "pnl_source": pnl_source,
                    "daily_loss_limit": daily_loss_limit,
                }
            ]
        return []

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


def _normalized_broker_pnl_by_currency(
    snapshot: dict[str, Any],
) -> dict[Currency, tuple[float, str]]:
    account = snapshot.get("account", {})
    for key in ("daily_pnl_by_currency", "today_pnl_by_currency", "realized_pnl_by_currency"):
        raw = account.get(key) or snapshot.get(key)
        if isinstance(raw, dict) and raw:
            return {
                Currency(str(currency)): (float(value), f"account.{key}")
                for currency, value in raw.items()
                if value is not None
            }
    position_pnls: dict[Currency, float] = {}
    for position in account.get("positions", []):
        if position.get("unrealized_pnl") is None:
            continue
        currency = position.get("currency")
        if currency is None:
            continue
        normalized = Currency(str(currency))
        position_pnls[normalized] = position_pnls.get(normalized, 0.0) + float(
            position["unrealized_pnl"]
        )
    if position_pnls:
        return {
            currency: (value, "account.positions.unrealized_pnl")
            for currency, value in position_pnls.items()
        }
    scalar = _normalized_broker_pnl(snapshot)
    if scalar is None:
        return {}
    inferred_currency = _infer_single_broker_currency(account)
    if inferred_currency is None:
        return {}
    return {inferred_currency: scalar}


def _infer_single_broker_currency(account: dict[str, Any]) -> Currency | None:
    cash_by_currency = account.get("cash_by_currency")
    if isinstance(cash_by_currency, dict) and len(cash_by_currency) == 1:
        return Currency(str(next(iter(cash_by_currency.keys()))))
    cash_balance = account.get("cash_balance")
    if isinstance(cash_balance, dict) and cash_balance.get("currency"):
        return Currency(str(cash_balance["currency"]))
    positions = account.get("positions")
    if isinstance(positions, list):
        currencies = {
            str(position.get("currency")) for position in positions if position.get("currency")
        }
        if len(currencies) == 1:
            return Currency(next(iter(currencies)))
    return None


def _broker_products_for_order(
    account_router: BrokerAccountRouter,
    order: OrderIntent,
) -> set[Any] | None:
    account = account_router.account(order.account_id)
    if account is None:
        return set(account_router.config.kis.effective_broker_products())
    if account.broker != "kis":
        return None
    return set(account.effective_broker_products())


def _broker_for_order(
    account_router: BrokerAccountRouter,
    order: OrderIntent,
) -> str | None:
    account = account_router.account(order.account_id)
    return account.broker if account is not None else None


def _orders_by_account(orders: list[OrderIntent]) -> dict[str | None, list[OrderIntent]]:
    grouped: dict[str | None, list[OrderIntent]] = {}
    for order in orders:
        grouped.setdefault(order.account_id, []).append(order)
    return grouped


def _disabled_account_native_keys(config: MaestroConfig | None) -> set[str]:
    """Native identifiers (config `id` and, when set, the literal
    `account_id`) of accounts that are explicitly disabled in the config.

    Broker snapshots are never deleted, so a disabled/retired account (e.g. a
    mock/paper account retired after setup) keeps its last snapshot in the
    state DB forever. Without this filter, `_latest_broker_snapshots_by_
    account` would keep including that stale snapshot in portfolio-wide risk
    checks — most importantly `_daily_loss_risk_issues`, which iterates every
    distinct snapshot and can block ALL live orders (not just ones for that
    account) if a disabled account's leftover/missing PnL data looks like it
    breached the daily loss limit.

    This is a DENY-list (only positively-identified disabled accounts are
    excluded) rather than an allow-list, which matters in practice: many
    configs use `account_id_env` instead of a literal `account_id`, so a
    snapshot's raw account_id can't always be matched back to a specific
    enabled account. An allow-list would wrongly exclude those as
    unmatched; a deny-list only ever excludes a snapshot confirmed to
    belong to an account we positively know is disabled, so ambiguous or
    unmatched snapshots (e.g. a single-account test fixture using an ad hoc
    id) safely pass through exactly as they did before this filter existed.
    This mirrors the equivalent dashboard read-model filter (see
    `maestro.dashboard.read_models._disabled_native_account_ids`),
    duplicated here rather than imported to keep the live order gate
    decoupled from the dashboard module.
    """
    if config is None:
        return set()
    disabled: set[str] = set()
    for account in getattr(config, "accounts", None) or []:
        if getattr(account, "enabled", False):
            continue
        disabled.add(account.id)
        literal_account_id = getattr(account, "account_id", None)
        if literal_account_id:
            disabled.add(str(literal_account_id))
    return disabled


def _latest_broker_snapshots_by_account(
    state_store: StateStore,
    config: MaestroConfig | None = None,
) -> dict[str | None, dict[str, Any]]:
    disabled_keys = _disabled_account_native_keys(config)
    snapshots: dict[str | None, dict[str, Any]] = {}
    for row in state_store.list_broker_account_snapshots(limit=1000):
        keys = _broker_snapshot_account_keys(row)
        if keys & disabled_keys:
            continue
        for key in keys:
            snapshots.setdefault(key, row)
        snapshots.setdefault(None, row)
    return snapshots


def _broker_snapshot_account_keys(row: dict[str, Any]) -> set[str]:
    payload = row.get("payload", {})
    account = payload.get("account", {}) if isinstance(payload, dict) else {}
    keys = {
        str(value)
        for value in (
            row.get("account_id"),
            payload.get("account_id") if isinstance(payload, dict) else None,
            account.get("account_id") if isinstance(account, dict) else None,
        )
        if value
    }
    return keys


def _snapshot_for_account(
    snapshots_by_account: dict[str | None, dict[str, Any]],
    account_router: BrokerAccountRouter,
    account_id: str | None,
) -> dict[str, Any] | None:
    if account_id in snapshots_by_account:
        return snapshots_by_account[account_id]
    account = account_router.account(account_id)
    if account is not None and account.account_id in snapshots_by_account:
        return snapshots_by_account[account.account_id]
    if account_id is not None:
        return None
    return snapshots_by_account.get(None)


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

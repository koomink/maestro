import os
import subprocess
from collections.abc import Mapping
from datetime import datetime, timedelta
from math import floor, isfinite
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from maestro.approval.models import ApprovalDecision, PendingApprovalEnvelope
from maestro.config.identity import ConfigIdentity
from maestro.config.loader import load_config, load_config_with_identity
from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus, OrderType, RunMode, SafetyState
from maestro.core.ids import new_order_id, new_run_id
from maestro.core.strategy_names import (
    strategy_command_slug as _telegram_strategy_command_slug,
)
from maestro.core.strategy_names import (
    strategy_display_name as _telegram_strategy_display_name,
)
from maestro.core.time_display import format_operator_time, operator_timezone
from maestro.dashboard.actions import generate_strategy_signal
from maestro.dashboard.read_models import (
    build_approvals_table,
    build_broker_account_overview,
    build_broker_account_summary,
    build_health_summary,
    build_latest_signal_package_card,
    build_orders_table,
    build_overview,
    build_safety_state_card,
    build_strategy_runs_table,
)
from maestro.execution.base import OrderIntent
from maestro.execution.broker_router import BrokerAccountRouter
from maestro.execution.broker_state import portfolio_state_from_broker_account
from maestro.execution.brokers.readonly_factory import (
    broker_readonly_accounts,
    build_broker_readonly_service,
    build_broker_readonly_services,
)
from maestro.execution.budget_requests import (
    BUDGET_SELECTION_KEYS,
    budget_request_reply_markup,
    format_contribution_budget_request,
    selected_budget_from_request,
    validate_selected_budget,
)
from maestro.execution.funding_requests import (
    format_contribution_funding_request,
    funding_request_reply_markup,
)
from maestro.execution.live_order_factory import build_live_approval_dependencies
from maestro.execution.live_order_models import (
    LiveOrderModifyRequest,
    LiveOrderRecoveryCandidate,
    LiveOrderRequest,
    LiveOrderStatusSnapshot,
)
from maestro.execution.order_capacity import OrderCapacityService
from maestro.integrations.telegram.bot import TelegramBotClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.monitoring.health import HealthService
from maestro.ops.readonly_refresh import refresh_readonly_accounts
from maestro.orchestration.live_gates import LiveExecutionGateService
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.portfolio.account_attribution import AccountAttributionReconciliationService
from maestro.safety.controls import SafetyControlService
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore

OPERATOR_CALLBACK_PREFIX = "operator:"
# Maps strategy ids to systemd units that dispatch the daily signal and
# non-blocking live approval pipeline for that strategy, e.g.
# MAESTRO_REBALANCE_UNITS="tranquillo=maestro-symphony-signal-kr.service,\
# crescendo_us=maestro-symphony-signal-us.service".
# The unit runs outside the telegram-operator cgroup so signal generation does
# not block command/update handling in the long-running operator.
REBALANCE_UNITS_ENV = "MAESTRO_REBALANCE_UNITS"
TELEGRAM_OPERATOR_COMMANDS: tuple[tuple[str, str], ...] = (
    ("help", "Show Maestro command list"),
    ("rebalance", "Show manual rebalance commands"),
    ("status", "Show Maestro status summary"),
    ("health", "Show health checks"),
    ("signal", "Show latest Symphony signal package"),
    ("account", "Show stored broker account freshness"),
    ("account_refresh", "Refresh broker account: /account_refresh [account_id]"),
    ("portfolio", "Show Maestro portfolio state"),
    ("apps", "Show configured strategy apps"),
    ("orders", "Show recent orders"),
    ("approvals", "Show recent approvals"),
    ("budget", "Select a pending contribution budget: /budget <request_id> <amount>"),
    ("attribution", "Review account attribution: /attribution <account_id>"),
    ("modify", "Propose order modification: /modify <broker_order_id> <price> [quantity]"),
    ("retry_order", "Retry a capacity-blocked order with a corrected quantity"),
    ("pause", "Confirm pause of live approval execution"),
    ("clear_halt", "Confirm recovery from a safety halt (runs preflight)"),
    ("kill_switch", "Confirm emergency live execution stop"),
)


class TelegramOperatorCommandRouter:
    def __init__(
        self,
        *,
        config: MaestroConfig,
        store: StateStore,
        audit: AuditLogger,
        client: TelegramBotClient,
        signal_config_path: str | Path | None = None,
        approval_config_path: str | Path | None = None,
        config_identity: ConfigIdentity | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.audit = audit
        self.client = client
        self.signal_config_path = Path(signal_config_path) if signal_config_path else None
        self.approval_config_path = Path(approval_config_path) if approval_config_path else None
        self.config_identity = config_identity

    def process_update(self, update: Mapping[str, Any]) -> bool:
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            return self._process_callback(update, callback)

        message = update.get("message")
        if not isinstance(message, Mapping):
            return False
        text = message.get("text")
        if not isinstance(text, str):
            return False
        chat_id = _chat_id(message)
        user = message.get("from")
        user_id, username = _user_identity(user if isinstance(user, Mapping) else {})
        if chat_id is None or user_id is None:
            return False
        command = _command_name(text) if text.startswith("/") else "/retry_quantity"
        if not self._chat_allowed(chat_id):
            self._record(command, chat_id, user_id, username, "denied_chat")
            return True
        if not self._user_allowed(user_id):
            self._send(chat_id, "Unauthorized Telegram user.")
            self._record(command, chat_id, user_id, username, "denied_user")
            return True

        if not text.startswith("/"):
            return self._process_retry_quantity_reply(
                message,
                text,
                chat_id,
                user_id,
                username,
            )

        if command.startswith("/signal_"):
            self._generate_strategy_signal(chat_id, command)
            self._record(command, chat_id, user_id, username, "handled")
            return True

        if command.startswith("/rebalance_"):
            self._request_manual_rebalance(chat_id, command)
            self._record(command, chat_id, user_id, username, "handled")
            return True

        if command == "/budget":
            self._process_budget_command(text, chat_id, user_id, username)
            return True
        if command == "/attribution":
            self._process_attribution_command(text, chat_id, user_id, username)
            return True
        if command == "/modify":
            self._process_modify_command(text, chat_id, user_id, username)
            return True
        if command == "/retry_order":
            self._process_retry_order_command(text, chat_id, user_id, username)
            return True
        if command == "/account_refresh":
            self._process_account_refresh(text, chat_id)
            self._record(command, chat_id, user_id, username, "handled")
            return True

        handler = {
            f"/{command}": handler
            for command, handler in {
                "help": self._help,
                "rebalance": self._rebalance_usage,
                "status": self._status,
                "health": self._health,
                "signal": self._signal,
                "account": self._account,
                "portfolio": self._portfolio,
                "apps": self._apps,
                "orders": self._orders,
                "approvals": self._approvals,
                "pause": self._pause,
                "clear-halt": self._clear_halt,
                "clear_halt": self._clear_halt,
                "kill-switch": self._kill_switch,
                "kill_switch": self._kill_switch,
            }.items()
        }.get(command)
        if handler is None:
            self._send(chat_id, "Unknown command. Use /help.")
            self._record(command, chat_id, user_id, username, "unknown")
            return True

        handler(chat_id)
        self._record(command, chat_id, user_id, username, "handled")
        return True

    def poll_once(self, *, offset: int | None = None, timeout_seconds: int = 0) -> int | None:
        self._sweep_pending_approvals()
        response = self.client.get_updates(
            offset=offset,
            timeout_seconds=timeout_seconds,
            allowed_updates=["message", "callback_query"],
        )
        updates = response.get("result")
        if not isinstance(updates, list):
            raise ValueError("Malformed Telegram updates response")

        next_offset = offset
        for update in updates:
            if not isinstance(update, Mapping):
                continue
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1
            try:
                self.process_update(update)
            except Exception as exc:  # noqa: BLE001 - one bad update must not
                # wedge the poll loop: re-raising here loses next_offset, so the
                # same batch (including already side-effecting commands) would
                # be fetched and executed again on the next poll.
                self._record_update_failure(update_id, exc)
        return next_offset

    def _process_callback(
        self,
        update: Mapping[str, Any],
        callback: Mapping[str, Any],
    ) -> bool:
        data = callback.get("data")
        if not isinstance(data, str) or not data.startswith(OPERATOR_CALLBACK_PREFIX):
            return False
        message = callback.get("message")
        if not isinstance(message, Mapping):
            return False
        chat_id = _chat_id(message)
        user_id, username = _user_identity(callback.get("from"))
        if chat_id is None or user_id is None:
            return False

        action = data.removeprefix(OPERATOR_CALLBACK_PREFIX)
        command = f"/{action.removeprefix('confirm:')}"
        if not self._chat_allowed(chat_id):
            self._record(command, chat_id, user_id, username, "denied_chat")
            return True
        if not self._user_allowed(user_id):
            self._answer(callback, "Unauthorized Telegram user.")
            self._record(command, chat_id, user_id, username, "denied_user")
            return True
        if action == "cancel":
            self._answer(callback, "Canceled.")
            self._edit_callback_message(callback, "Telegram command canceled.")
            self._record("/cancel", chat_id, user_id, username, "canceled")
            return True
        if action.startswith("funding:"):
            return self._process_funding_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action.startswith("budget:"):
            return self._process_budget_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action.startswith("cash-flow:"):
            return self._process_cash_flow_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action.startswith("attribution:"):
            return self._process_attribution_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action.startswith("modify:"):
            return self._process_modify_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action.startswith("retry-order:"):
            return self._process_retry_order_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action.startswith("appr:"):
            return self._process_async_approval_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action.startswith("cap:"):
            return self._process_capacity_retry_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action.startswith("recover:"):
            return self._process_recovery_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action.startswith("rebalance:"):
            return self._process_rebalance_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action not in {"confirm:pause", "confirm:kill-switch", "confirm:clear-halt"}:
            self._answer(callback, "This command is no longer active.")
            self._record(command, chat_id, user_id, username, "stale_callback")
            return True

        transition = action.removeprefix("confirm:")
        reason = f"Telegram /{transition} confirmed by {username or user_id}"
        if transition == "clear-halt":
            return self._confirm_clear_halt(callback, command, chat_id, user_id, username, reason)
        safety = SafetyControlService(self.store, self.audit)
        if transition == "pause":
            snapshot = safety.pause(new_run_id(), reason, source="telegram")
        else:
            snapshot = safety.kill_switch(new_run_id(), reason, source="telegram")
        text = f"Safety state changed: {snapshot.state.value}\nreason: {snapshot.reason}"
        self._answer(callback, f"{transition} confirmed.")
        self._edit_callback_message(callback, text)
        self._record(command, chat_id, user_id, username, "confirmed")
        return True

    def _confirm_clear_halt(
        self,
        callback: Mapping[str, Any],
        command: str,
        chat_id: int,
        user_id: int,
        username: str | None,
        reason: str,
    ) -> bool:
        # Mirrors the `maestro clear-halt` CLI: recovery preflight first
        # (every health check except safety_state itself must not fail),
        # then the guarded state transition.
        report = HealthService(self.config, self.store).run()
        blocking_checks = [
            check.name
            for check in report.checks
            if check.status == "fail" and check.name != "safety_state"
        ]
        if blocking_checks:
            self._answer(callback, "Recovery preflight failed.")
            self._edit_callback_message(
                callback,
                "Clear-halt blocked by failing health checks: " + ", ".join(blocking_checks),
            )
            self._record(command, chat_id, user_id, username, "preflight_failed")
            return True
        try:
            snapshot = SafetyControlService(self.store, self.audit).clear_halt(
                new_run_id(),
                reason,
                source="telegram",
            )
        except ValueError as exc:
            self._answer(callback, "Clear-halt failed.")
            self._edit_callback_message(callback, f"Clear-halt failed: {exc}")
            self._record(command, chat_id, user_id, username, "failed")
            return True
        self._answer(callback, "clear-halt confirmed.")
        self._edit_callback_message(
            callback,
            f"Safety state changed: {snapshot.state.value}\nreason: {snapshot.reason}",
        )
        self._record(command, chat_id, user_id, username, "confirmed")
        return True

    def _process_attribution_command(
        self,
        text: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> None:
        parts = text.strip().split()
        if len(parts) != 2:
            self._send(chat_id, "Usage: /attribution <account_id>")
            self._record("/attribution", chat_id, user_id, username, "invalid")
            return
        account_id = parts[1]
        latest = self._latest_attribution_event(account_id)
        if latest is None:
            self._send(chat_id, f"No attribution baseline for account_id={account_id}.")
            self._record("/attribution", chat_id, user_id, username, "missing")
            return
        payload = latest["payload"]
        positions = payload.get("positions") or []
        lines = [
            "Account attribution baseline",
            f"account_id: {account_id}",
            f"version: {payload.get('version')}",
            f"broker_snapshot_id: {payload.get('broker_snapshot_id')}",
            f"approved: {str(bool(payload.get('approved'))).lower()}",
        ]
        for position in positions:
            item = _mapping(position)
            lines.append(
                f"{item.get('symbol')}: {item.get('bucket_id')} {_number(item.get('quantity'))}"
            )
        reply_markup = None
        if not payload.get("approved"):
            reply_markup = _attribution_markup(account_id)
        self._send(chat_id, "\n".join(lines), reply_markup=reply_markup)
        self._record("/attribution", chat_id, user_id, username, "handled")

    def _process_attribution_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        parts = action.split(":", 2)
        if len(parts) != 3 or parts[1] != "approve":
            self._answer(callback, "This attribution approval is no longer active.")
            return True
        account_id = parts[2]
        try:
            positions = AccountAttributionReconciliationService(
                self.store,
                self.audit,
            ).adopt_latest(
                run_id=new_run_id(),
                account_id=account_id,
                reason=f"Telegram attribution approved by {username or user_id}",
                adopted_by=f"telegram:{user_id}",
            )
        except ValueError as exc:
            self._answer(callback, str(exc))
            self._record("/attribution", chat_id, user_id, username, "stale_callback")
            return True
        text = f"Attribution adopted\naccount_id: {account_id}\npositions: {len(positions)}"
        self._answer(callback, "Attribution adopted.")
        self._edit_callback_message(callback, text)
        self._record("/attribution", chat_id, user_id, username, "approved")
        return True

    def _process_modify_command(
        self,
        text: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> None:
        parts = text.strip().split()
        if len(parts) not in {3, 4}:
            self._send(
                chat_id,
                "Usage: /modify <broker_order_id> <price> [quantity]",
            )
            self._record("/modify", chat_id, user_id, username, "invalid")
            return
        status = self._latest_order_status(parts[1])
        if status is None:
            self._send(chat_id, "Order status not found.")
            self._record("/modify", chat_id, user_id, username, "missing")
            return
        status = self._status_with_resolved_account(status)
        if status.broker_order.account_id is None:
            self._send(chat_id, "Order account could not be resolved; modification is blocked.")
            self._record("/modify", chat_id, user_id, username, "invalid")
            return
        instrument = self.config.universe.get(status.symbol or "")
        if instrument is None:
            self._send(chat_id, "Order symbol is not in the configured universe.")
            self._record("/modify", chat_id, user_id, username, "invalid")
            return
        try:
            price = float(parts[2])
            quantity = float(parts[3]) if len(parts) == 4 else None
            proposal_id = new_run_id()
            request = LiveOrderModifyRequest(
                run_id=proposal_id,
                approval_id=f"modify_{proposal_id}",
                broker_order=status.broker_order,
                symbol=status.symbol or instrument.symbol,
                limit_price=price,
                quantity=quantity,
                currency=instrument.currency,
                reason=f"Telegram proposal by {username or user_id}",
            )
        except ValueError as exc:
            self._send(chat_id, f"Invalid modification: {exc}")
            self._record("/modify", chat_id, user_id, username, "invalid")
            return
        save_audited_system_event(
            self.store,
            self.audit,
            proposal_id,
            "live_order_modify_proposal",
            {
                "proposal_id": proposal_id,
                "request": request.model_dump(mode="json"),
                "status": "pending",
                "created_at": utc_now().isoformat(),
            },
        )
        self._send(
            chat_id,
            (
                "Order modification proposal\n"
                f"broker_order_id: {parts[1]}\n"
                f"symbol: {request.symbol}\n"
                f"price: {request.limit_price}\n"
                f"quantity: {request.quantity or 'unchanged'}"
            ),
            reply_markup=_modify_markup(proposal_id),
        )
        self._record("/modify", chat_id, user_id, username, "proposed")

    def _process_modify_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        parts = action.split(":", 2)
        if len(parts) != 3 or parts[1] != "approve":
            self._answer(callback, "This modification proposal is no longer active.")
            return True
        proposal = self._pending_modify_proposal(parts[2])
        if proposal is None:
            self._answer(callback, "This modification proposal is no longer active.")
            self._record("/modify", chat_id, user_id, username, "stale_callback")
            return True
        request = LiveOrderModifyRequest.model_validate(proposal["request"])
        try:
            config = self.config
            if self.approval_config_path is not None:
                config = load_config(self.approval_config_path)
            dependencies = build_live_approval_dependencies(
                config,
                self.store,
                self.audit,
                account_id=request.broker_order.account_id,
                telegram_client=self.client,
            )
            if dependencies.modify_service is None:
                raise ValueError("Configured broker does not support order modification")
            dependencies.status_service.poll_order_status(
                request.run_id,
                request.broker_order,
            )
            fill_result = dependencies.fill_reconciliation_service.reconcile_latest(request.run_id)
            if fill_result.applied_fills and dependencies.broker_reconciliation_service is not None:
                reconciliation = dependencies.broker_reconciliation_service.reconcile_latest()
                if reconciliation.passed is not True:
                    raise ValueError("Broker reconciliation failed after latest fills")
            result = dependencies.modify_service.modify_order(
                request,
                ApprovalDecision(
                    approval_id=request.approval_id,
                    run_id=request.run_id,
                    status="approved",
                    decided_at=utc_now(),
                    decided_by=f"telegram:{user_id}",
                ),
            )
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._answer(callback, f"Modification failed: {exc}")
            self._record("/modify", chat_id, user_id, username, "failed")
            return True
        save_audited_system_event(
            self.store,
            self.audit,
            request.run_id,
            "live_order_modify_proposal_ack",
            {
                "proposal_id": parts[2],
                "status": "approved",
                "replacement_broker_order_id": result.broker_order.broker_order_id,
                "decided_by": f"telegram:{user_id}",
            },
        )
        self._answer(callback, "Order modification submitted.")
        self._edit_callback_message(
            callback,
            (
                "Order modification submitted\n"
                f"replacement_broker_order_id: {result.broker_order.broker_order_id}"
            ),
        )
        self._record("/modify", chat_id, user_id, username, "approved")
        return True

    def _process_retry_order_command(
        self,
        text: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> None:
        parts = text.strip().split()
        if len(parts) not in {3, 4}:
            self._send(chat_id, "Usage: /retry_order <blocked_order_id> <quantity> [price]")
            self._record("/retry_order", chat_id, user_id, username, "invalid")
            return
        try:
            quantity = float(parts[2])
            price = float(parts[3]) if len(parts) == 4 else None
            self._propose_retry_order(
                parts[1],
                quantity,
                chat_id,
                price=price,
            )
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._send(chat_id, f"Invalid retry order: {exc}")
            self._record("/retry_order", chat_id, user_id, username, "invalid")
            return
        self._record("/retry_order", chat_id, user_id, username, "proposed")

    def _propose_retry_order(
        self,
        order_id: str,
        quantity: float,
        chat_id: int,
        *,
        price: float | None = None,
    ) -> str:
        candidate = self._pending_recovery_candidate(order_id)
        if candidate is None:
            raise ValueError("recoverable order was not found or was already retried")
        config = self.config
        if self.approval_config_path is not None:
            config = load_config(self.approval_config_path)
        if config.mode != RunMode.LIVE_APPROVAL:
            raise ValueError("retry orders require live_approval mode")
        self._validate_recovery_window(candidate, config)
        original = OrderIntent.model_validate(candidate.order)
        if not isfinite(quantity) or quantity <= 0 or quantity > original.quantity:
            raise ValueError("retry quantity must be positive and no greater than planned")
        retry_price = price if price is not None else self._lookup_retry_price(config, original)
        if not isfinite(retry_price) or retry_price <= 0:
            raise ValueError("retry price must be a positive finite number")
        proposal_id = new_run_id()
        metadata = dict(original.metadata)
        metadata["recovery_of"] = original.order_id
        if candidate.source_type == "capacity_blocked":
            metadata["capacity_retry_of"] = original.order_id
        order = original.model_copy(
            update={
                "order_id": new_order_id(),
                "quantity": quantity,
                "price": retry_price,
                "notional": quantity * retry_price,
                "metadata": metadata,
            }
        )
        capacity_service = OrderCapacityService(
            lambda candidate: self._lookup_retry_capacity(config, candidate)
        )
        accepted, capacity_blocks = capacity_service.partition([order])
        if capacity_blocks or not accepted:
            reason = capacity_blocks[0].reason if capacity_blocks else "capacity unavailable"
            raise ValueError(f"retry order is still blocked: {reason}")
        gate_blocks = LiveExecutionGateService(config, self.store, self.audit).evaluate(
            proposal_id,
            [order],
            [],
        )
        if gate_blocks:
            reasons = ", ".join(str(item.get("reason")) for item in gate_blocks)
            raise ValueError(f"retry order is blocked by live gate: {reasons}")
        request = LiveOrderRequest(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            limit_price=order.price,
            order_type=OrderType.LIMIT,
            approval_id=f"retry_{proposal_id}",
            run_id=proposal_id,
            duplicate_key=f"capacity-retry:{original.order_id}:{order.order_id}",
            currency=order.currency,
            sleeve=order.sleeve,
            execution_sleeve=order.execution_sleeve,
            account_id=order.account_id,
            broker_product=order.broker_product,
            signal_run_id=candidate.signal_run_id,
        )
        candidate_duplicate_key = f"live-order-recovery-candidate:{original.order_id}"
        if not self.store.duplicate_key_exists(candidate_duplicate_key):
            save_audited_system_event(
                self.store,
                self.audit,
                proposal_id,
                "live_order_recovery_candidate",
                {
                    **candidate.model_dump(mode="json"),
                    "duplicate_key": candidate_duplicate_key,
                },
            )
        save_audited_system_event(
            self.store,
            self.audit,
            proposal_id,
            "live_order_retry_proposal",
            {
                "proposal_id": proposal_id,
                "blocked_order_id": original.order_id,
                "recovery_order_id": original.order_id,
                "source_type": candidate.source_type,
                "order": order.model_dump(mode="json"),
                "request": request.model_dump(mode="json"),
                "status": "pending",
                "created_at": utc_now().isoformat(),
                "expires_at": (
                    utc_now() + timedelta(seconds=config.approval.timeout_seconds)
                ).isoformat(),
            },
        )
        self._send(
            chat_id,
            "\n".join(
                [
                    "Recoverable order retry proposal",
                    f"source_order_id: {original.order_id}",
                    f"new_order_id: {order.order_id}",
                    f"account_id: {order.account_id or 'default'}",
                    f"symbol: {order.symbol}",
                    f"quantity: {order.quantity:g}",
                    f"price: {order.price:g}",
                ]
            ),
            reply_markup=_retry_order_markup(proposal_id),
        )
        return proposal_id

    def _process_capacity_retry_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        order_id = action.removeprefix("cap:")
        blocked = self._pending_capacity_block(order_id)
        if blocked is None:
            self._answer(callback, "This capacity block is no longer active.")
            return True
        maximum = blocked.get("max_buy_quantity")
        if maximum is None or float(maximum) <= 0:
            self._answer(callback, "No positive retry quantity is currently available.")
            return True
        try:
            self._propose_retry_order(order_id, float(maximum), chat_id)
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._answer(callback, f"Retry failed: {exc}")
            self._record("/retry_order", chat_id, user_id, username, "failed")
            return True
        self._answer(callback, "Retry proposal created. Review the new approval message.")
        self._record("/retry_order", chat_id, user_id, username, "proposed")
        return True

    def _process_recovery_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        parts = action.split(":", 2)
        if len(parts) != 3 or parts[1] not in {"review", "original", "max", "input"}:
            self._answer(callback, "This recovery action is no longer active.")
            return True
        transition, order_id = parts[1], parts[2]
        try:
            candidate, original, price, maximum = self._retry_order_review(order_id)
            if transition == "review":
                self._send(
                    chat_id,
                    "\n".join(
                        [
                            "Recoverable order",
                            f"source_order_id: {order_id}",
                            f"account_id: {original.account_id or 'default'}",
                            f"symbol: {original.symbol}",
                            f"reason: {candidate.reason}",
                            f"original_quantity: {original.quantity:g}",
                            f"current_max_quantity: {maximum:g}",
                            f"latest_price: {price:g}",
                        ]
                    ),
                    reply_markup=_recovery_options_markup(
                        order_id,
                        original.quantity,
                        maximum,
                    ),
                )
                self._answer(callback, "Choose a retry quantity.")
                self._record("/retry_order", chat_id, user_id, username, "reviewed")
                return True
            if transition == "input":
                response = self._send(
                    chat_id,
                    (
                        f"{original.symbol} 재주문 수량을 입력하세요. "
                        f"(원 수량 {original.quantity:g}, 현재 최대 {maximum:g})"
                    ),
                    reply_markup={
                        "force_reply": True,
                        "selective": True,
                        "input_field_placeholder": "수량 입력",
                    },
                )
                message_id = _sent_message_id(response)
                if message_id is None:
                    raise RuntimeError("Telegram did not return the quantity prompt message id")
                prompt_id = new_run_id()
                save_audited_system_event(
                    self.store,
                    self.audit,
                    prompt_id,
                    "live_order_retry_quantity_prompt",
                    {
                        "prompt_id": prompt_id,
                        "source_order_id": order_id,
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "message_id": message_id,
                        "original_quantity": original.quantity,
                        "max_quantity": maximum,
                        "created_at": utc_now().isoformat(),
                        "expires_at": (utc_now() + timedelta(minutes=10)).isoformat(),
                        "duplicate_key": f"retry-quantity-prompt:{prompt_id}",
                    },
                )
                self._answer(callback, "Reply to the quantity prompt.")
                self._record("/retry_order", chat_id, user_id, username, "prompted")
                return True
            quantity = original.quantity if transition == "original" else maximum
            if quantity <= 0:
                raise ValueError("no positive retry quantity is currently available")
            self._propose_retry_order(order_id, quantity, chat_id, price=price)
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._answer(callback, f"Retry unavailable: {exc}")
            self._record("/retry_order", chat_id, user_id, username, "failed")
            return True
        self._answer(callback, "Retry proposal created. Review the new approval message.")
        self._record("/retry_order", chat_id, user_id, username, "proposed")
        return True

    def _retry_order_review(
        self,
        order_id: str,
    ) -> tuple[LiveOrderRecoveryCandidate, OrderIntent, float, float]:
        candidate = self._pending_recovery_candidate(order_id)
        if candidate is None:
            raise ValueError("recoverable order was not found or was already retried")
        config = (
            load_config(self.approval_config_path)
            if self.approval_config_path is not None
            else self.config
        )
        if config.mode != RunMode.LIVE_APPROVAL:
            raise ValueError("retry orders require live_approval mode")
        self._validate_recovery_window(candidate, config)
        original = OrderIntent.model_validate(candidate.order)
        price = self._lookup_retry_price(config, original)
        maximum = original.quantity
        if original.side == OrderSide.BUY:
            capacity = self._lookup_retry_capacity(
                config,
                original.model_copy(update={"price": price, "notional": original.quantity * price}),
            )
            cash_quantity = max(0.0, float(capacity.cash_buying_power)) / price
            maximum = min(original.quantity, cash_quantity)
            if capacity.max_buy_quantity is not None:
                maximum = min(maximum, float(capacity.max_buy_quantity))
        instrument = config.universe.get(original.symbol)
        step = float(instrument.quantity_step) if instrument is not None else 1.0
        maximum = floor((max(0.0, maximum) + 1e-9) / step) * step
        return candidate, original, price, maximum

    def _process_retry_quantity_reply(
        self,
        message: Mapping[str, Any],
        text: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        reply = message.get("reply_to_message")
        message_id = reply.get("message_id") if isinstance(reply, Mapping) else None
        if not isinstance(message_id, int):
            return False
        prompt = self._pending_retry_quantity_prompt(chat_id, user_id, message_id)
        if prompt is None:
            return False
        try:
            quantity = float(text.strip().replace(",", ""))
            if not isfinite(quantity) or quantity <= 0:
                raise ValueError("quantity must be a positive finite number")
            if quantity > float(prompt["original_quantity"]):
                raise ValueError("quantity cannot exceed the original planned quantity")
        except (TypeError, ValueError) as exc:
            self._send(chat_id, f"Invalid quantity: {exc}. Reply to the same prompt again.")
            self._record("/retry_quantity", chat_id, user_id, username, "invalid")
            return True
        try:
            proposal_id = self._propose_retry_order(
                str(prompt["source_order_id"]),
                quantity,
                chat_id,
            )
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._ack_retry_quantity_prompt(prompt, "failed", reason=str(exc))
            self._send(
                chat_id,
                f"Retry proposal failed: {exc}",
                reply_markup=_recovery_review_markup(str(prompt["source_order_id"])),
            )
            self._record("/retry_quantity", chat_id, user_id, username, "failed")
            return True
        self._ack_retry_quantity_prompt(prompt, "consumed", proposal_id=proposal_id)
        self._record("/retry_quantity", chat_id, user_id, username, "proposed")
        return True

    def _pending_retry_quantity_prompt(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        acknowledged = {
            str(row["payload"].get("prompt_id"))
            for row in self.store.list_system_events_by_type(
                "live_order_retry_quantity_prompt_ack",
                limit=5000,
            )
        }
        for row in self.store.list_system_events_by_type(
            "live_order_retry_quantity_prompt",
            limit=5000,
        ):
            prompt = row["payload"]
            if (
                str(prompt.get("prompt_id")) in acknowledged
                or prompt.get("chat_id") != chat_id
                or prompt.get("user_id") != user_id
                or prompt.get("message_id") != message_id
            ):
                continue
            expires_at = datetime.fromisoformat(str(prompt["expires_at"]).replace("Z", "+00:00"))
            if utc_now() >= expires_at:
                self._ack_retry_quantity_prompt(prompt, "expired")
                return None
            return prompt
        return None

    def _ack_retry_quantity_prompt(
        self,
        prompt: Mapping[str, Any],
        status: str,
        **details: Any,
    ) -> None:
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            "live_order_retry_quantity_prompt_ack",
            {
                "prompt_id": prompt["prompt_id"],
                "source_order_id": prompt["source_order_id"],
                "status": status,
                "decided_at": utc_now().isoformat(),
                "duplicate_key": f"retry-quantity-prompt-ack:{prompt['prompt_id']}",
                **details,
            },
        )

    def _process_retry_order_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int | str,
        username: str | None,
    ) -> bool:
        parts = action.split(":", 2)
        if len(parts) != 3 or parts[1] not in {"approve", "reject"}:
            self._answer(callback, "This retry proposal is no longer active.")
            return True
        proposal = self._pending_retry_proposal(parts[2])
        if proposal is None:
            self._answer(callback, "This retry proposal is no longer active.")
            self._record("/retry_order", chat_id, user_id, username, "stale_callback")
            return True
        if parts[1] == "reject":
            self._ack_retry_proposal(proposal, "rejected", user_id)
            self._answer(callback, "Retry order rejected.")
            self._edit_callback_message(callback, "Retry order proposal rejected.")
            self._record("/retry_order", chat_id, user_id, username, "rejected")
            return True
        request = LiveOrderRequest.model_validate(proposal["request"])
        config = self.config
        if self.approval_config_path is not None:
            config = load_config(self.approval_config_path)
        try:
            if config.mode != RunMode.LIVE_APPROVAL:
                raise ValueError("retry orders require live_approval mode")
            order = OrderIntent.model_validate(proposal["order"])
            accepted, capacity_blocks = OrderCapacityService(
                lambda candidate: self._lookup_retry_capacity(config, candidate)
            ).partition([order])
            if capacity_blocks or not accepted:
                reason = capacity_blocks[0].reason if capacity_blocks else "capacity unavailable"
                raise ValueError(f"retry order is still blocked: {reason}")
            gate_blocks = LiveExecutionGateService(config, self.store, self.audit).evaluate(
                request.run_id,
                [order],
                [],
            )
            if gate_blocks:
                reasons = ", ".join(str(item.get("reason")) for item in gate_blocks)
                raise ValueError(f"retry order is blocked by live gate: {reasons}")
            dependencies = build_live_approval_dependencies(
                config,
                self.store,
                self.audit,
                account_id=request.account_id,
                telegram_client=self.client,
                signal_run_id=request.signal_run_id,
            )
            decision = ApprovalDecision(
                approval_id=request.approval_id,
                run_id=request.run_id,
                status="approved",
                decided_at=utc_now(),
                decided_by=f"telegram:{user_id}",
            )
            result = dependencies.lifecycle_service.run(request, decision)
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._ack_retry_proposal(proposal, "rejected", user_id, reason=str(exc))
            self._answer(callback, f"Retry failed: {exc}")
            self._record("/retry_order", chat_id, user_id, username, "failed")
            return True
        self._ack_retry_proposal(
            proposal,
            "approved",
            user_id,
            final_status=result.final_status.value,
        )
        order_payload = dict(proposal["order"])
        order_payload["approval_status"] = "approved"
        order_payload["signal_run_id"] = request.signal_run_id
        self.store.save_order(request.run_id, request.order_id, order_payload)
        self._answer(callback, "Retry order submitted.")
        self._edit_callback_message(
            callback,
            (
                f"Retry order completed\norder_id: {request.order_id}\n"
                f"status: {result.final_status.value}"
            ),
        )
        self._record("/retry_order", chat_id, user_id, username, "approved")
        return True

    def _process_async_approval_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        parts = action.split(":", 2)
        if len(parts) != 3 or parts[1] not in {"a", "r"}:
            self._answer(callback, "This approval request is no longer active.")
            return True
        envelope = self._pending_async_approval(parts[2])
        if envelope is None:
            self._answer(callback, "This approval request is no longer active.")
            self._record("/approval", chat_id, user_id, username, "stale_callback")
            return True
        status = "approved" if parts[1] == "a" else "rejected"
        try:
            summary = self._resolve_async_approval(
                envelope,
                status=status,
                decided_by=f"telegram:{username or user_id}",
                reason=f"Telegram button {status} callback.",
            )
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._answer(callback, f"Approval failed: {exc}")
            self._record("/approval", chat_id, user_id, username, "failed")
            return True
        self._answer(callback, f"Approval {status}.")
        self._edit_callback_message(
            callback,
            (
                f"Approval {status}\n"
                f"approval_id: {envelope.approval_id}\n"
                f"orders: {summary.orders_created}"
            ),
        )
        self._record("/approval", chat_id, user_id, username, status)
        return True

    def _resolve_async_approval(
        self,
        envelope: PendingApprovalEnvelope,
        *,
        status: str,
        decided_by: str,
        reason: str,
    ):
        decision = ApprovalDecision(
            approval_id=envelope.approval_id,
            run_id=envelope.run_id,
            status=status,
            decided_at=utc_now(),
            decided_by=decided_by,
            reason=reason,
        )
        duplicate_key = f"telegram-approval-ack:{envelope.approval_id}"
        with self.store.writer_lock("telegram_approval_callback_claim"):
            if self.store.duplicate_key_exists(duplicate_key):
                raise ValueError("Approval request was already decided")
            save_audited_system_event(
                self.store,
                self.audit,
                envelope.run_id,
                "telegram_approval_ack",
                {
                    "approval_id": envelope.approval_id,
                    "signal_run_id": envelope.signal_run_id,
                    "status": status,
                    "decided_by": decided_by,
                    "decided_at": decision.decided_at.isoformat(),
                    "duplicate_key": duplicate_key,
                },
            )
        config = self.config
        identity = self.config_identity
        if self.approval_config_path is not None:
            config, identity = load_config_with_identity(self.approval_config_path)
        try:
            return MaestroOrchestrator(
                config,
                telegram_client=self.client,
                config_identity=identity,
            ).resolve_pending_signal_approval(envelope, decision)
        except Exception as exc:
            save_audited_system_event(
                self.store,
                self.audit,
                envelope.run_id,
                "telegram_approval_resolution_failed",
                {
                    "approval_id": envelope.approval_id,
                    "status": status,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            raise

    def _sweep_pending_approvals(self) -> None:
        acked = {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_ack",
                limit=2000,
            )
        }
        reminders = {
            (
                str(row["payload"].get("approval_id")),
                int(row["payload"].get("reminder_seconds", 0)),
            )
            for row in self.store.list_system_events_by_type(
                "telegram_approval_reminder",
                limit=5000,
            )
        }
        now = utc_now()
        for row in reversed(
            self.store.list_system_events_by_type(
                "telegram_approval_pending",
                limit=2000,
            )
        ):
            payload = row["payload"]
            approval_id = str(payload.get("approval_id") or "")
            if not approval_id or approval_id in acked:
                continue
            envelope = PendingApprovalEnvelope.model_validate(payload)
            if now >= envelope.expires_at:
                try:
                    self._resolve_async_approval(
                        envelope,
                        status="expired",
                        decided_by="telegram:timeout",
                        reason="Telegram approval timed out.",
                    )
                except ValueError:
                    pass
                continue
            elapsed = (now - envelope.created_at).total_seconds()
            for reminder_seconds in envelope.reminder_seconds:
                key = (approval_id, reminder_seconds)
                if elapsed < reminder_seconds or key in reminders:
                    continue
                for chat_id in self.config.approval.telegram_allowed_chat_ids:
                    self._send(
                        chat_id,
                        f"Approval reminder ({reminder_seconds // 60}m)\n\n{envelope.message}",
                        reply_markup=_async_approval_markup(envelope.approval_id),
                    )
                save_audited_system_event(
                    self.store,
                    self.audit,
                    envelope.run_id,
                    "telegram_approval_reminder",
                    {
                        "approval_id": envelope.approval_id,
                        "reminder_seconds": reminder_seconds,
                        "sent_at": now.isoformat(),
                        "duplicate_key": (
                            f"telegram-approval-reminder:{envelope.approval_id}:{reminder_seconds}"
                        ),
                    },
                )
                reminders.add(key)

    def _ack_retry_proposal(
        self,
        proposal: Mapping[str, Any],
        status: str,
        user_id: int,
        **details: Any,
    ) -> None:
        request = proposal.get("request") or {}
        decided_by = f"telegram:{user_id}" if isinstance(user_id, int) else str(user_id)
        save_audited_system_event(
            self.store,
            self.audit,
            str(request.get("run_id") or new_run_id()),
            "live_order_retry_proposal_ack",
            {
                "proposal_id": proposal["proposal_id"],
                "blocked_order_id": proposal["blocked_order_id"],
                "status": status,
                "decided_by": decided_by,
                **details,
            },
        )
        save_audited_system_event(
            self.store,
            self.audit,
            str(request.get("run_id") or new_run_id()),
            "live_order_recovery_ack",
            {
                "proposal_id": proposal["proposal_id"],
                "source_order_id": proposal["blocked_order_id"],
                "status": status,
                "decided_by": decided_by,
                **details,
            },
        )

    def _lookup_retry_capacity(self, config: MaestroConfig, order: OrderIntent):
        service = build_broker_readonly_service(
            config,
            self.store,
            self.audit,
            account_id=order.account_id,
        )
        while hasattr(service, "inner"):
            service = service.inner
        account = BrokerAccountRouter(config).account(order.account_id)
        broker = account.broker if account is not None else "kis"
        instrument = config.universe.get(order.symbol)
        symbol = instrument.symbol_for_broker(broker) if instrument is not None else order.symbol
        return service.client.get_buying_power(symbol, order.price)

    def _lookup_retry_price(self, config: MaestroConfig, order: OrderIntent) -> float:
        service = build_broker_readonly_service(
            config,
            self.store,
            self.audit,
            account_id=order.account_id,
        )
        while hasattr(service, "inner"):
            service = service.inner
        prices = service.client.get_current_prices([order.symbol])
        price = float(prices.get(order.symbol, 0.0))
        if price <= 0:
            raise ValueError(f"latest broker quote is unavailable for {order.symbol}")
        return price

    def _validate_recovery_window(
        self,
        candidate: LiveOrderRecoveryCandidate,
        config: MaestroConfig,
    ) -> None:
        timezone = ZoneInfo(operator_timezone(config))
        created_at = datetime.fromisoformat(candidate.created_at.replace("Z", "+00:00"))
        created_date = created_at.astimezone(timezone).date()
        current_date = utc_now().astimezone(timezone).date()
        metadata = candidate.order.get("metadata") or {}
        if metadata.get("order_generation_mode") == "buy_only_contribution":
            contribution_month = str(metadata.get("contribution_month") or "")
            if contribution_month != current_date.strftime("%Y-%m"):
                raise ValueError(
                    "contribution recovery orders expire at the end of their contribution month"
                )
            return
        if created_date != current_date:
            raise ValueError("rebalancing recovery orders can only be retried the same trading day")

    def _process_cash_flow_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        parts = action.split(":")
        transition = parts[1] if len(parts) > 1 else ""
        valid_callback = (
            len(parts) == 3 and parts[0] == "cash-flow" and transition in {"approve", "ignore"}
        ) or (len(parts) == 4 and parts[0] == "cash-flow" and transition in {"assign", "asg"})
        if not valid_callback:
            self._answer(callback, "This cash-flow proposal is no longer active.")
            self._record("/cash-flow", chat_id, user_id, username, "stale_callback")
            return True
        proposal_id = parts[2]
        proposal = self._load_pending_cash_flow_proposal(proposal_id)
        if proposal is None:
            self._answer(callback, "This cash-flow proposal is no longer active.")
            self._record("/cash-flow", chat_id, user_id, username, "stale_callback")
            return True
        assigned_strategy_id = None
        if transition in {"assign", "asg"}:
            assigned_strategy_id = _assigned_strategy_id_from_token(proposal, parts[3])
            if assigned_strategy_id is None:
                self._answer(callback, "This cash-flow proposal is no longer active.")
                self._record("/cash-flow", chat_id, user_id, username, "stale_callback")
                return True
        if transition == "ignore":
            self._save_cash_flow_proposal_ack(proposal_id, "ignored", user_id, username)
            self._answer(callback, "Cash-flow allocation ignored.")
            self._edit_callback_message(callback, "Cash-flow allocation ignored.")
            self._record("/cash-flow_ignore", chat_id, user_id, username, "ignored")
            return True
        allocations = list(proposal.get("allocations") or [])
        if assigned_strategy_id:
            allocation = next(
                (row for row in allocations if row.get("strategy_id") == assigned_strategy_id),
                self.config_identity,
            )
            if allocation is None:
                self._answer(callback, "This cash-flow proposal is no longer active.")
                self._record("/cash-flow", chat_id, user_id, username, "stale_callback")
                return True
            allocations = [dict(allocation, amount=proposal.get("amount"))]
        effective_at = utc_now().isoformat()
        for allocation in allocations:
            payload = {
                "strategy_id": allocation.get("strategy_id"),
                "account_id": proposal.get("account_id"),
                "execution_sleeve": allocation.get("execution_sleeve"),
                "amount": allocation.get("amount"),
                "currency": proposal.get("currency"),
                "flow_type": "deposit",
                "effective_at": effective_at,
                "source": "telegram_voluntary_deposit_allocation",
                "proposal_id": proposal_id,
                "decided_by": username or str(user_id),
            }
            save_audited_system_event(
                self.store,
                self.audit,
                new_run_id(),
                "strategy_cash_flow",
                payload,
            )
        if assigned_strategy_id:
            self._save_cash_flow_proposal_ack(
                proposal_id,
                "assigned",
                user_id,
                username,
                assigned_strategy_id=assigned_strategy_id,
            )
            self._answer(callback, "Cash-flow allocation assigned.")
            self._record("/cash-flow_assign", chat_id, user_id, username, "assigned")
        else:
            self._save_cash_flow_proposal_ack(proposal_id, "approved", user_id, username)
            self._answer(callback, "Cash-flow allocation approved.")
            self._record("/cash-flow_approve", chat_id, user_id, username, "approved")
        self._edit_callback_message(
            callback,
            "Strategy cash-flow allocation recorded\nproposal_id: " + proposal_id,
        )
        return True

    def _process_funding_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        parts = action.split(":", 2)
        if len(parts) != 3 or parts[0] != "funding" or parts[1] not in {"complete", "cancel"}:
            self._answer(callback, "This funding request is no longer active.")
            self._record("/funding", chat_id, user_id, username, "stale_callback")
            return True
        transition, request_id = parts[1], parts[2]
        request = self._load_pending_funding_request(request_id)
        if request is None:
            self._answer(callback, "This funding request is no longer active.")
            self._record("/funding", chat_id, user_id, username, "stale_callback")
            return True
        if transition == "cancel":
            self._save_funding_ack(request_id, "canceled", user_id, username)
            self._answer(callback, "Funding request canceled.")
            self._edit_callback_message(callback, "Funding request canceled.")
            self._record("/funding_cancel", chat_id, user_id, username, "canceled")
            return True
        self._answer(callback, "Funding request confirmed.")
        try:
            text = self._confirm_funding_request(
                request,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
            )
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            text = "\n".join(
                [
                    "Funding confirmation failed",
                    f"request_id: {request_id}",
                    f"message: {exc}",
                ]
            )
            self._record("/funding_complete", chat_id, user_id, username, "failed")
        else:
            self._record("/funding_complete", chat_id, user_id, username, "confirmed")
        self._edit_callback_message(callback, text)
        return True

    def _process_budget_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        parts = action.split(":", 3)
        valid = (len(parts) == 3 and parts[0] == "budget" and parts[1] == "cancel") or (
            len(parts) == 4
            and parts[0] == "budget"
            and parts[1] in {"select", "sel"}
            and parts[3] in BUDGET_SELECTION_KEYS
        )
        if not valid:
            self._answer(callback, "This budget request is no longer active.")
            self._record("/budget", chat_id, user_id, username, "stale_callback")
            return True
        transition = parts[1]
        request_id = parts[2]
        request = self._load_pending_budget_request(request_id)
        if request is None:
            self._answer(callback, "This budget request is no longer active.")
            self._record("/budget", chat_id, user_id, username, "stale_callback")
            return True
        if transition == "cancel":
            self._save_budget_decision(request, "canceled", user_id, username)
            self._answer(callback, "Budget request canceled.")
            self._edit_callback_message(callback, "Budget request canceled.")
            self._record("/budget_cancel", chat_id, user_id, username, "canceled")
            return True
        try:
            amount = selected_budget_from_request(request, parts[3])
            text = self._confirm_budget_request(
                request,
                selected_budget=amount,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
            )
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._answer(callback, "Budget selection failed.")
            text = "\n".join(
                [
                    "Budget selection failed",
                    f"request_id: {request_id}",
                    f"message: {exc}",
                ]
            )
            self._record("/budget_select", chat_id, user_id, username, "failed")
        else:
            self._answer(callback, "Budget selected.")
            self._record("/budget_select", chat_id, user_id, username, "selected")
        self._edit_callback_message(callback, text)
        return True

    def _process_budget_command(
        self,
        text: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> None:
        parts = text.strip().split()
        if len(parts) != 3:
            self._send(chat_id, "Usage: /budget <request_id> <amount>")
            self._record("/budget", chat_id, user_id, username, "invalid")
            return
        request_id = parts[1]
        request = self._load_pending_budget_request(request_id)
        if request is None:
            self._send(chat_id, "This budget request is no longer active.")
            self._record("/budget", chat_id, user_id, username, "stale")
            return
        try:
            amount = float(parts[2].replace(",", ""))
            text = self._confirm_budget_request(
                request,
                selected_budget=amount,
                chat_id=chat_id,
                user_id=user_id,
                username=username,
            )
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._send(chat_id, f"Budget amount out of range or invalid: {exc}")
            self._record("/budget", chat_id, user_id, username, "failed")
            return
        self._send(chat_id, text)
        self._record("/budget", chat_id, user_id, username, "selected")

    def _confirm_budget_request(
        self,
        request: dict[str, Any],
        *,
        selected_budget: float,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> str:
        validate_selected_budget(request, selected_budget)
        self._save_budget_decision(
            request,
            "selected",
            user_id,
            username,
            selected_budget=selected_budget,
        )
        lines = [
            "Budget selected",
            f"request_id: {request['request_id']}",
            f"selected_budget: {selected_budget:,.0f} {request.get('currency') or ''}".rstrip(),
        ]
        if self.signal_config_path is None:
            return "\n".join(lines)
        try:
            self._refresh_portfolio_from_broker_snapshot()
        except (RuntimeError, TimeoutError, ValueError):
            if self._has_readonly_broker_accounts():
                raise
        signal_config, signal_identity = load_config_with_identity(self.signal_config_path)
        strategy_ids = [str(item) for item in request.get("strategy_ids") or []]
        if not strategy_ids:
            raise ValueError("Budget request is missing strategy_ids")
        # A user-selected budget is an explicit instruction to invest now, so the
        # regenerated signal bypasses the contribution buy_day schedule (the
        # already-executed-this-month guard still applies).
        signal_summary = MaestroOrchestrator(
            signal_config,
            config_identity=signal_identity,
        ).run_signal(strategy_ids=strategy_ids, contribution_override=True)
        lines.append(f"new_signal_run_id: {signal_summary.signal_run_id}")
        lines.append(f"orders_preview_count: {signal_summary.orders_preview_count}")
        if signal_summary.orders_preview_count == 0 and signal_summary.no_order_reasons:
            lines.append("no orders were generated because:")
            lines.extend(f"- {reason}" for reason in signal_summary.no_order_reasons)
        if signal_summary.action_required:
            if self.approval_config_path is None:
                lines.append("approval_status: not_created_missing_approval_config")
                return "\n".join(lines)
            approval_config, approval_identity = load_config_with_identity(
                self.approval_config_path
            )
            approval_orchestrator = MaestroOrchestrator(
                approval_config,
                telegram_client=self.client,
                config_identity=approval_identity,
            )
            if (
                approval_config.mode == RunMode.LIVE_APPROVAL
                and approval_config.approval.provider == "telegram"
            ):
                approval_summary = approval_orchestrator.dispatch_signal_approval(
                    signal_summary.signal_run_id
                )
                order_line = f"orders_planned: {approval_summary.orders_planned}"
                pending_line = f"approvals_pending: {approval_summary.approvals_pending}"
            else:
                approval_summary = approval_orchestrator.approve_signal(
                    signal_summary.signal_run_id
                )
                order_line = f"orders_created: {approval_summary.orders_created}"
                pending_line = "approvals_pending: 0"
            lines.extend(
                [
                    f"approval_run_id: {approval_summary.run_id}",
                    f"approval_status: {approval_summary.approval_status}",
                    order_line,
                    pending_line,
                ]
            )
            return "\n".join(lines)
        signal = self.store.load_signal_package(signal_summary.signal_run_id) or {}
        for budget_request in signal.get("budget_requests") or []:
            self._send_budget_request(chat_id, budget_request)
        for funding_request in signal.get("funding_requests") or []:
            self._send_funding_request(chat_id, funding_request)
        if signal.get("budget_requests"):
            lines.append("approval_status: budget_still_required")
        elif signal.get("funding_requests"):
            lines.append("approval_status: funding_still_required")
        else:
            lines.append("approval_status: not_required")
        return "\n".join(lines)

    def _confirm_funding_request(
        self,
        request: dict[str, Any],
        *,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> str:
        if self.signal_config_path is None:
            raise ValueError("Funding confirmation requires telegram-operator --signal-config")
        try:
            self._refresh_portfolio_from_broker_snapshot()
        except (RuntimeError, TimeoutError, ValueError):
            if self._has_readonly_broker_accounts():
                raise
        signal_config, signal_identity = load_config_with_identity(self.signal_config_path)
        strategy_ids = [str(item) for item in request.get("strategy_ids") or []]
        if not strategy_ids:
            raise ValueError("Funding request is missing strategy_ids")
        self._record_strategy_cash_flow_from_funding_request(
            request,
            strategy_ids=strategy_ids,
            user_id=user_id,
            username=username,
        )
        # Funding was just confirmed by the operator; regenerate orders
        # immediately instead of waiting for the scheduled buy_day.
        signal_summary = MaestroOrchestrator(
            signal_config,
            config_identity=signal_identity,
        ).run_signal(strategy_ids=strategy_ids, contribution_override=True)
        self._save_funding_ack(
            str(request["request_id"]),
            "confirmed",
            user_id,
            username,
            new_signal_run_id=signal_summary.signal_run_id,
        )
        lines = [
            "Funding confirmed",
            f"request_id: {request['request_id']}",
            f"new_signal_run_id: {signal_summary.signal_run_id}",
            f"orders_preview_count: {signal_summary.orders_preview_count}",
        ]
        if signal_summary.orders_preview_count == 0 and signal_summary.no_order_reasons:
            lines.append("no orders were generated because:")
            lines.extend(f"- {reason}" for reason in signal_summary.no_order_reasons)
        if signal_summary.action_required:
            if self.approval_config_path is None:
                lines.append("approval_status: not_created_missing_approval_config")
                return "\n".join(lines)
            approval_config, approval_identity = load_config_with_identity(
                self.approval_config_path
            )
            approval_orchestrator = MaestroOrchestrator(
                approval_config,
                telegram_client=self.client,
                config_identity=approval_identity,
            )
            if (
                approval_config.mode == RunMode.LIVE_APPROVAL
                and approval_config.approval.provider == "telegram"
            ):
                approval_summary = approval_orchestrator.dispatch_signal_approval(
                    signal_summary.signal_run_id
                )
                order_line = f"orders_planned: {approval_summary.orders_planned}"
                pending_line = f"approvals_pending: {approval_summary.approvals_pending}"
            else:
                approval_summary = approval_orchestrator.approve_signal(
                    signal_summary.signal_run_id
                )
                order_line = f"orders_created: {approval_summary.orders_created}"
                pending_line = "approvals_pending: 0"
            lines.extend(
                [
                    f"approval_run_id: {approval_summary.run_id}",
                    f"approval_status: {approval_summary.approval_status}",
                    order_line,
                    pending_line,
                ]
            )
            return "\n".join(lines)
        signal = self.store.load_signal_package(signal_summary.signal_run_id) or {}
        funding_requests = signal.get("funding_requests") or []
        if funding_requests:
            lines.append("approval_status: funding_still_required")
            for funding_request in funding_requests:
                self._send_funding_request(chat_id, funding_request)
        else:
            lines.append("approval_status: not_required")
        return "\n".join(lines)

    def _record_strategy_cash_flow_from_funding_request(
        self,
        request: dict[str, Any],
        *,
        strategy_ids: list[str],
        user_id: int,
        username: str | None,
    ) -> None:
        amount = _float_or_none(request.get("required_shortfall"))
        if amount is None or amount <= 0:
            return
        currency = str(request.get("currency") or "")
        effective_at = utc_now().isoformat()
        per_strategy_amount = amount / len(strategy_ids)
        for strategy_id in strategy_ids:
            payload = {
                "strategy_id": strategy_id,
                "account_id": request.get("account_id"),
                "execution_sleeve": request.get("execution_sleeve"),
                "amount": per_strategy_amount,
                "currency": currency,
                "flow_type": "deposit",
                "effective_at": effective_at,
                "source": "telegram_funding_confirmation",
                "request_id": request.get("request_id"),
                "source_signal_run_id": request.get("source_signal_run_id"),
                "decided_by": username or str(user_id),
            }
            save_audited_system_event(
                self.store,
                self.audit,
                new_run_id(),
                "strategy_cash_flow",
                payload,
            )

    def _send_voluntary_deposit_allocation_proposal(self, chat_id: int) -> None:
        proposal = self._build_voluntary_deposit_proposal()
        if proposal is None:
            return
        lines = [
            "Unattributed deposit detected",
            f"proposal_id: {proposal['proposal_id']}",
            f"account_id: {proposal['account_id']}",
            f"amount: {_money(proposal['amount'])} {proposal['currency']}",
            "",
            "Suggested allocation:",
        ]
        for allocation in proposal["allocations"]:
            amount = _money(allocation["amount"])
            lines.append(
                f"- {_telegram_strategy_display_name(allocation['strategy_id'])}: "
                f"{amount} {proposal['currency']}"
            )
        # Send before saving: the proposal-exists dedup check means a proposal
        # saved without a delivered message would never be offered again.
        self._send(
            chat_id,
            "\n".join(lines),
            reply_markup=_cash_flow_proposal_markup(proposal),
        )
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            "strategy_cash_flow_proposal",
            proposal,
        )

    def _build_voluntary_deposit_proposal(self) -> dict[str, Any] | None:
        snapshots = self.store.list_broker_account_snapshots(limit=20)
        if len(snapshots) < 2:
            return None
        latest = snapshots[0]
        latest_payload = latest.get("payload") or {}
        latest_account = (
            latest_payload.get("account") if isinstance(latest_payload, Mapping) else {}
        )
        if not isinstance(latest_account, Mapping):
            return None
        account_id = _broker_snapshot_account_id(latest)
        previous = next(
            (row for row in snapshots[1:] if _broker_snapshot_account_id(row) == account_id),
            None,
        )
        if previous is None:
            return None
        previous_payload = previous.get("payload") or {}
        previous_account = (
            previous_payload.get("account") if isinstance(previous_payload, Mapping) else {}
        )
        if not isinstance(previous_account, Mapping):
            return None
        latest_cash = _cash_by_currency(latest_account)
        previous_cash = _cash_by_currency(previous_account)
        for currency, cash in latest_cash.items():
            increase = cash - previous_cash.get(currency, 0.0)
            if increase <= 0:
                continue
            if self._cash_flow_proposal_exists(latest.get("id"), account_id, currency, increase):
                return None
            allocations = self._target_cash_flow_allocations(account_id, increase)
            if not allocations:
                return None
            return {
                "proposal_id": new_run_id(),
                "status": "pending",
                "account_id": account_id,
                "broker_snapshot_id": latest.get("id"),
                "previous_broker_snapshot_id": previous.get("id"),
                "amount": increase,
                "currency": currency,
                "flow_type": "deposit",
                "source": "broker_snapshot_cash_increase",
                "created_at": utc_now().isoformat(),
                "allocations": allocations,
            }
        return None

    def _target_cash_flow_allocations(self, account_id: str, amount: float) -> list[dict[str, Any]]:
        candidates = []
        for strategy in getattr(self.config, "strategies", []):
            if not getattr(strategy, "enabled", False):
                continue
            if getattr(strategy, "account_id", None) != account_id:
                continue
            sleeve = self.config.execution_sleeves.sleeve(
                getattr(strategy, "account_id", None),
                getattr(strategy, "execution_sleeve", None),
            )
            weight = _float_or_none(getattr(sleeve, "target_weight", None))
            if weight is None:
                weight = _float_or_none(getattr(strategy, "weight", None))
            if weight is None or weight <= 0:
                continue
            candidates.append((strategy, weight))
        total_weight = sum(weight for _, weight in candidates)
        if total_weight <= 0:
            return []
        return [
            {
                "strategy_id": strategy.id,
                "execution_sleeve": getattr(strategy, "execution_sleeve", None),
                "target_weight": weight / total_weight,
                "amount": round(amount * weight / total_weight, 6),
            }
            for strategy, weight in candidates
        ]

    def _cash_flow_proposal_exists(
        self, broker_snapshot_id: object, account_id: str, currency: str, amount: float
    ) -> bool:
        for row in self.store.list_system_events_by_type("strategy_cash_flow_proposal", limit=1000):
            payload = row.get("payload") or {}
            if (
                payload.get("broker_snapshot_id") == broker_snapshot_id
                and payload.get("account_id") == account_id
                and payload.get("currency") == currency
                and _float_or_none(payload.get("amount")) == amount
            ):
                return True
        return False

    def _load_pending_cash_flow_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        acked = {
            str(row["payload"].get("proposal_id"))
            for row in self.store.list_system_events_by_type(
                "strategy_cash_flow_proposal_ack",
                limit=1000,
            )
        }
        if proposal_id in acked:
            return None
        for row in self.store.list_system_events_by_type("strategy_cash_flow_proposal", limit=1000):
            payload = row.get("payload") or {}
            if payload.get("proposal_id") == proposal_id and payload.get("status") == "pending":
                return payload
        return None

    def _save_cash_flow_proposal_ack(
        self,
        proposal_id: str,
        status: str,
        user_id: int,
        username: str | None,
        **extra: Any,
    ) -> None:
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            "strategy_cash_flow_proposal_ack",
            {
                "proposal_id": proposal_id,
                "status": status,
                "decided_by": username or str(user_id),
                **extra,
            },
        )

    def _send_funding_request(self, chat_id: int, request: dict[str, Any]) -> None:
        request_id = str(request.get("request_id") or "")
        self._send(
            chat_id,
            format_contribution_funding_request(request),
            reply_markup=funding_request_reply_markup(request_id),
        )

    def _send_budget_request(self, chat_id: int, request: dict[str, Any]) -> None:
        self._send(
            chat_id,
            format_contribution_budget_request(request),
            reply_markup=budget_request_reply_markup(request),
        )

    def _load_pending_budget_request(self, request_id: str) -> dict[str, Any] | None:
        decided = {
            str(row["payload"].get("request_id"))
            for row in self.store.list_system_events_by_type(
                "contribution_budget_request_decision",
                limit=1000,
            )
        }
        if request_id in decided:
            return None
        for row in self.store.list_system_events_by_type(
            "contribution_budget_request",
            limit=1000,
        ):
            payload = row.get("payload") or {}
            if payload.get("request_id") == request_id and payload.get("status") == "pending":
                return payload
        return None

    def _save_budget_decision(
        self,
        request: dict[str, Any],
        status: str,
        user_id: int,
        username: str | None,
        *,
        selected_budget: float | None = None,
    ) -> None:
        payload = {
            "request_id": request.get("request_id"),
            "status": status,
            "strategy_ids": list(request.get("strategy_ids") or []),
            "contribution_group_id": request.get("contribution_group_id"),
            "account_id": request.get("account_id"),
            "execution_sleeve": request.get("execution_sleeve"),
            "currency": request.get("currency"),
            "month_key": request.get("month_key"),
            "decided_by": username or str(user_id),
        }
        if selected_budget is not None:
            payload["selected_budget"] = selected_budget
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            "contribution_budget_request_decision",
            payload,
        )

    def _load_pending_funding_request(self, request_id: str) -> dict[str, Any] | None:
        acked = {
            str(row["payload"].get("request_id"))
            for row in self.store.list_system_events_by_type(
                "contribution_funding_request_ack",
                limit=1000,
            )
        }
        if request_id in acked:
            return None
        for row in self.store.list_system_events_by_type(
            "contribution_funding_request",
            limit=1000,
        ):
            payload = row.get("payload") or {}
            if payload.get("request_id") == request_id and payload.get("status") == "pending":
                return payload
        return None

    def _save_funding_ack(
        self,
        request_id: str,
        status: str,
        user_id: int,
        username: str | None,
        *,
        new_signal_run_id: str | None = None,
    ) -> None:
        payload = {
            "request_id": request_id,
            "status": status,
            "decided_by": username or str(user_id),
        }
        if new_signal_run_id is not None:
            payload["new_signal_run_id"] = new_signal_run_id
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            "contribution_funding_request_ack",
            payload,
        )

    def _help(self, chat_id: int) -> None:
        self._send(
            chat_id,
            "\n".join(
                [
                    "Maestro Telegram commands",
                    *[
                        f"/{command} - {description}"
                        for command, description in TELEGRAM_OPERATOR_COMMANDS
                    ],
                ]
            ),
        )

    def _status(self, chat_id: int) -> None:
        overview = build_overview(self.store)
        broker = build_broker_account_summary(self.store, self.config)
        safety = build_safety_state_card(self.store)
        operator_config = overview.get("operator_config") or {}
        fingerprint = operator_config.get("fingerprint", "none")
        state_path = Path(self.config.state.sqlite_path).expanduser().resolve()
        audit_path = Path(self.config.audit.jsonl_path).expanduser().resolve()
        broker_currency = self._broker_currency_breakdowns()
        self._send(
            chat_id,
            "\n".join(
                [
                    "Maestro status",
                    "",
                    "Runtime",
                    f"- mode: {self.config.mode.value}",
                    f"- order_posture: {self.config.execution.order_posture}",
                    f"- safety: {safety['state']}",
                    "",
                    "Broker",
                    f"- total_value: {_money_by_currency(broker_currency['total_value'])}",
                    f"- cash: {_money_by_currency(broker_currency['cash'])}",
                    f"- positions: {broker['positions_count']}",
                    f"- snapshot_at: {_operator_time(broker['created_at'], self.config)}",
                    "",
                    "Activity",
                    f"- orders: {overview['orders_count']}",
                    f"- approvals: {overview['approvals_count']}",
                    "",
                    "Config",
                    f"- path: {operator_config.get('path', 'unknown')}",
                    f"- fingerprint: {fingerprint[:12] if fingerprint != 'none' else 'none'}",
                    f"- state: {state_path}",
                    f"- audit: {audit_path}",
                ]
            ),
        )

    def _health(self, chat_id: int) -> None:
        health = build_health_summary(self.config, self.store)
        checks = health["checks"]
        problem_checks = [check for check in checks if check["status"] != "ok"]
        lines = [
            "Maestro health",
            f"status: {health['status']}",
            (
                "counts: "
                f"ok={health['counts'].get('ok', 0)} "
                f"warn={health['counts'].get('warn', 0)} "
                f"fail={health['counts'].get('fail', 0)}"
            ),
        ]
        for check in problem_checks[:5]:
            lines.append(f"{check['check']}: {check['status']} {check['message']}")
        self._send(chat_id, "\n".join(lines))

    def _signal(self, chat_id: int) -> None:
        signal = build_latest_signal_package_card(self.store)
        if signal["signal_run_id"] is None:
            self._send(chat_id, "Latest signal: none")
            return
        actionable = signal.get("actionable_signal_run_id")
        lines = [
            "Latest signal",
            f"signal_run_id: {signal['signal_run_id']}",
            f"status: {signal['status']}",
            f"action_required: {str(signal['action_required']).lower()}",
            f"approval_consumed: {str(signal['approval_consumed']).lower()}",
            f"orders_preview_count: {signal['orders_preview_count']}",
            f"datahub_issues: {signal['datahub_issue_count']}",
            f"created_at: {_operator_time(signal['created_at'], self.config)}",
        ]
        if actionable:
            lines.append(f"approval_signal_run_id: {actionable}")
        if signal.get("approval_run_id"):
            lines.append(f"approval_run_id: {signal['approval_run_id']}")
        self._send(chat_id, "\n".join(lines))

    def _generate_strategy_signal(self, chat_id: int, command: str) -> None:
        if self.signal_config_path is None:
            self._send(chat_id, "Signal generation requires telegram-operator --signal-config.")
            return
        try:
            signal_config = load_config(self.signal_config_path)
            strategy_id = _strategy_id_for_signal_command(command, signal_config)
            if strategy_id is None:
                raise ValueError(f"Unknown signal command: {command}")
            result = generate_strategy_signal(self.signal_config_path, strategy_id)
        except ValueError as exc:
            self._send(
                chat_id,
                "\n".join(
                    [
                        "Signal generation failed",
                        f"command: {command}",
                        f"message: {exc}",
                    ]
                ),
            )
            return
        loaded = (
            ", ".join(
                _telegram_strategy_display_name(item)
                for item in result.get("loaded_strategies") or []
            )
            or "none"
        )
        self._send(
            chat_id,
            "\n".join(
                [
                    "Signal generated",
                    f"strategy: {_telegram_strategy_display_name(result['strategy_id'])}",
                    f"signal_run_id: {result['signal_run_id']}",
                    f"loaded_strategies: {loaded}",
                    f"action_required: {str(result['action_required']).lower()}",
                    f"orders_preview_count: {result['orders_preview_count']}",
                    "approval_created: false",
                    "broker_submit: false",
                ]
            ),
        )

    def _rebalance_usage(self, chat_id: int) -> None:
        if self.signal_config_path is None:
            self._send(chat_id, "Manual rebalance requires telegram-operator --signal-config.")
            return
        signal_config = load_config(self.signal_config_path)
        commands = [
            f"/{_primary_rebalance_command(strategy.id)} - "
            f"{_telegram_strategy_display_name(strategy.id)}"
            for strategy in signal_config.strategies
            if strategy.enabled and strategy.signal_enabled
        ]
        if not commands:
            self._send(chat_id, "No signal-enabled strategies available for manual rebalance.")
            return
        self._send(
            chat_id,
            "\n".join(
                [
                    "Manual rebalance commands",
                    *commands,
                    "",
                    "Runs the daily signal + approval pipeline immediately.",
                    "Live orders still require Telegram approval.",
                ]
            ),
        )

    def _request_manual_rebalance(self, chat_id: int, command: str) -> None:
        if self.signal_config_path is None:
            self._send(chat_id, "Manual rebalance requires telegram-operator --signal-config.")
            return
        try:
            signal_config = load_config(self.signal_config_path)
            strategy_id = _strategy_id_for_rebalance_command(command, signal_config)
            if strategy_id is None:
                raise ValueError(f"Unknown rebalance command: {command}")
            unit = _rebalance_unit_for_strategy(strategy_id)
            if unit is None:
                raise ValueError(
                    f"No systemd unit mapped for strategy {strategy_id}; set "
                    f"{REBALANCE_UNITS_ENV} in the telegram-operator environment, e.g. "
                    f'"{strategy_id}=maestro-symphony-signal-kr.service"'
                )
        except ValueError as exc:
            self._send(
                chat_id,
                "\n".join(
                    [
                        "Manual rebalance failed",
                        f"command: {command}",
                        f"message: {exc}",
                    ]
                ),
            )
            return
        self._send(
            chat_id,
            "\n".join(
                [
                    f"Confirm manual rebalance for {_telegram_strategy_display_name(strategy_id)}.",
                    f"unit: {unit}",
                    "This runs the daily signal + approval pipeline now,",
                    "overriding the scheduled contribution buy day.",
                    "Live orders still require Telegram approval, and this bot",
                    "pauses while approval polling is active.",
                ]
            ),
            reply_markup=_rebalance_markup(strategy_id),
        )

    def _process_rebalance_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        command = "/rebalance"
        parts = action.split(":")
        if len(parts) != 3 or parts[1] != "approve" or not parts[2]:
            self._answer(callback, "This command is no longer active.")
            self._record(command, chat_id, user_id, username, "stale_callback")
            return True
        strategy_id = parts[2]
        display_name = _telegram_strategy_display_name(strategy_id)
        unit = _rebalance_unit_for_strategy(strategy_id)
        if unit is None:
            self._answer(callback, "Rebalance unit not configured.")
            self._edit_callback_message(
                callback,
                f"Manual rebalance failed: no systemd unit mapped for {strategy_id}. "
                f"Set {REBALANCE_UNITS_ENV} in the telegram-operator environment.",
            )
            self._record(command, chat_id, user_id, username, "failed")
            return True
        try:
            _start_systemd_unit(unit)
        except (subprocess.CalledProcessError, FileNotFoundError, OSError) as exc:
            self._answer(callback, "Manual rebalance failed.")
            self._edit_callback_message(
                callback,
                "\n".join(
                    [
                        f"Manual rebalance failed: {display_name}",
                        f"unit: {unit}",
                        f"message: {exc}",
                    ]
                ),
            )
            self._record(command, chat_id, user_id, username, "failed")
            return True
        self._answer(callback, "Manual rebalance triggered.")
        self._edit_callback_message(
            callback,
            "\n".join(
                [
                    f"Manual rebalance triggered: {display_name}",
                    f"unit: {unit}",
                    f"confirmed_by: {username or user_id}",
                    "This bot pauses while approval polling is active;",
                    "approve the order prompt when it arrives.",
                ]
            ),
        )
        self._record(command, chat_id, user_id, username, "confirmed")
        return True

    def _account(self, chat_id: int) -> None:
        overview = build_broker_account_overview(self.config, self.store)
        account = build_broker_account_summary(self.store, self.config)
        if account["created_at"] is None:
            self._send(chat_id, "Broker account snapshot: none")
            return
        currency = self._broker_currency_breakdowns()
        lines = [
            "Broker account snapshot (stored)",
            f"created_at: {_operator_time(account['created_at'], self.config)}",
            f"account_id: {_mask_identifier(account['account_id'])}",
            f"total_value: {_money_by_currency(currency['total_value'])}",
            f"cash: {_money_by_currency(currency['cash'])}",
            f"positions_market_value: {_money_by_currency(currency['positions_market_value'])}",
            f"positions: {account['positions_count']}",
            f"source: {account['source'] or 'unknown'}",
        ]
        for account in overview["accounts"]:
            age = account.get("age_seconds")
            limit = account.get("max_age_seconds")
            age_label = "missing" if age is None else _compact_age(float(age)) + " ago"
            limit_label = "n/a" if limit is None else _compact_age(float(limit))
            lines.append(
                f"{account['account_id']}: {account['status']} · {age_label} · limit {limit_label}"
            )
            if account.get("last_refresh_error"):
                lines.append(f"  last error: {account['last_refresh_error']}")
        self._send(chat_id, "\n".join(lines))
        self._send_voluntary_deposit_allocation_proposal(chat_id)

    def _process_account_refresh(self, text: str, chat_id: int) -> None:
        parts = text.split()
        account_ids = [parts[1]] if len(parts) > 1 else None
        try:
            report = refresh_readonly_accounts(
                self.config,
                None,
                account_ids=account_ids,
                source="telegram",
                state_store=self.store,
                audit_logger=self.audit,
            )
        except ValueError as exc:
            self._send(chat_id, f"Account refresh rejected: {exc}")
            return
        lines = ["Broker account refresh"]
        for result in report.results:
            lines.append(f"{result.account_id}: {result.status} · retries {result.retry_count}")
            if result.error_message:
                lines.append(f"  error: {result.error_message}")
        self._send(chat_id, "\n".join(lines))
        self._account(chat_id)

    def _portfolio(self, chat_id: int) -> None:
        refresh_error: Exception | None = None
        try:
            if self._has_readonly_broker_accounts():
                self._send(chat_id, "Maestro portfolio: refreshing from broker snapshot")
            self._refresh_portfolio_from_broker_snapshot()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            refresh_error = exc
        lines = []
        if refresh_error is not None:
            lines.extend(
                [
                    f"Maestro portfolio refresh failed: {refresh_error}",
                    "Showing latest stored Maestro portfolio.",
                ]
            )
        lines.append("Maestro portfolio")
        state = self.store.load_latest_portfolio_state()
        if state.cash_by_currency:
            lines.append("CASH")
            for currency, cash in sorted(state.cash_by_currency.items()):
                lines.append(f"- {currency}: {_number(cash)}")
        else:
            lines.append(f"CASH: {_number(state.cash)}")
        position_prices = self._broker_position_prices()
        instrument_currencies = self._instrument_currencies()
        position_labels = self._portfolio_position_labels()
        for symbol, quantity in sorted(state.positions.items())[:10]:
            lines.append(
                _portfolio_position_line(
                    symbol,
                    quantity,
                    position_prices,
                    instrument_currencies,
                    position_labels,
                )
            )
        self._send(chat_id, "\n".join(lines))

    def _apps(self, chat_id: int) -> None:
        lines = ["Maestro apps"]
        visible_strategies = [
            strategy
            for strategy in self.config.strategies
            if getattr(strategy, "readonly_enabled", True)
        ]
        for strategy in visible_strategies[:10]:
            status = "on" if strategy.enabled else "off"
            signal = "signal:on" if strategy.signal_enabled else "signal:off"
            posture = strategy.order_posture or self.config.execution.order_posture
            lines.append(
                f"{_telegram_strategy_display_name(strategy.id)}: {status} {signal} "
                f"account={strategy.account_id or 'n/a'} "
                f"order_posture={posture}"
            )
        latest_runs = build_strategy_runs_table(self.store, limit=5)
        if latest_runs:
            lines.append("")
            lines.append("Recent strategy runs")
            for row in latest_runs:
                ok = row["validation_ok"]
                lines.append(
                    f"{_telegram_strategy_display_name(row['strategy_id'])}: validation_ok={ok}"
                )
        self._send(chat_id, "\n".join(lines))

    def _orders(self, chat_id: int) -> None:
        open_statuses = self._refresh_open_order_statuses()
        rows = build_orders_table(self.store, limit=5)
        recoverable = []
        for row in self.store.list_orders(limit=5000):
            order_id = str(row.get("order_id") or "")
            candidate = self._pending_recovery_candidate(order_id)
            if candidate is not None:
                recoverable.append(candidate)
            if len(recoverable) >= 5:
                break
        if not rows and not open_statuses and not recoverable:
            self._send(chat_id, "Recent orders: none")
            return
        lines = ["Recent orders"]
        for row in rows:
            lines.append(
                f"{row['order_id']} {row['side']} {row['symbol']} "
                f"qty={_number(row['quantity'])} status={row['approval_status']}"
            )
        if open_statuses:
            lines.extend(["", "Open orders"])
            for status in open_statuses:
                remaining = status.partial_fill.remaining_quantity
                lines.append(
                    f"{status.broker_order.broker_order_id} "
                    f"{status.broker_order.account_id or 'default'} {status.symbol or 'unknown'} "
                    f"status={status.status.value} "
                    f"filled={_number(status.partial_fill.filled_quantity)} "
                    f"remaining={_number(remaining)}"
                )
                lines.append(
                    f"/modify {status.broker_order.broker_order_id} <price> {_number(remaining)}"
                )
        if recoverable:
            lines.extend(["", "Recoverable orders"])
            for candidate in recoverable:
                order = candidate.order
                lines.append(
                    f"{candidate.source_order_id} {order.get('account_id') or 'default'} "
                    f"{order.get('symbol')} qty={_number(order.get('quantity'))} "
                    f"reason={candidate.reason}"
                )
                lines.append(
                    f"/retry_order {candidate.source_order_id} {_number(order.get('quantity'))}"
                )
        self._send(
            chat_id,
            "\n".join(lines),
            reply_markup=(_recoverable_orders_markup(recoverable) if recoverable else None),
        )

    def _refresh_open_order_statuses(self) -> list[LiveOrderStatusSnapshot]:
        latest_by_broker: dict[str, LiveOrderStatusSnapshot] = {}
        for row in self.store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_STATUS,
            limit=2000,
        ):
            status = LiveOrderStatusSnapshot.model_validate(row["payload"])
            broker_order_id = status.broker_order.broker_order_id
            latest_by_broker.setdefault(broker_order_id, status)
        candidates = [
            self._status_with_resolved_account(status)
            for status in latest_by_broker.values()
            if status.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
            and status.partial_fill.remaining_quantity > 0
        ]
        config = self.config
        if self.approval_config_path is not None:
            config = load_config(self.approval_config_path)
        refreshed = []
        reconciler = None
        for status in candidates:
            try:
                dependencies = build_live_approval_dependencies(
                    config,
                    self.store,
                    self.audit,
                    account_id=status.broker_order.account_id,
                )
                current = dependencies.status_service.poll_order_status(
                    new_run_id(),
                    status.broker_order,
                )
                reconciler = dependencies.fill_reconciliation_service
            except (RuntimeError, TimeoutError, TypeError, ValueError):
                current = status
            if (
                current.status in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
                and current.partial_fill.remaining_quantity > 0
            ):
                refreshed.append(current)
        if reconciler is not None:
            reconciler.reconcile_latest(new_run_id())
        return refreshed

    def _approvals(self, chat_id: int) -> None:
        rows = build_approvals_table(self.store, limit=5)
        acked = {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_ack",
                limit=2000,
            )
        }
        pending = [
            PendingApprovalEnvelope.model_validate(row["payload"])
            for row in self.store.list_system_events_by_type(
                "telegram_approval_pending",
                limit=20,
            )
            if str(row["payload"].get("approval_id")) not in acked
        ][:5]
        if not rows and not pending:
            self._send(chat_id, "Recent approvals: none")
            return
        lines = ["Recent approvals"]
        for envelope in pending:
            expires = format_operator_time(
                envelope.expires_at,
                operator_timezone(self.config),
            )
            lines.append(
                f"{envelope.approval_id} status=pending "
                f"orders={envelope.request.order_count} "
                f"expires={expires}"
            )
        if rows:
            lines.append("Terminal approvals")
        for row in rows:
            lines.append(
                f"{row['approval_id']} status={row['status']} "
                f"orders={row['order_count']} notional={_approval_notional_label(row)}"
            )
        self._send(chat_id, "\n".join(lines))

    def _pause(self, chat_id: int) -> None:
        self._send(
            chat_id,
            "Confirm pause. This blocks live approval execution.",
            reply_markup=_confirmation_markup("pause"),
        )

    def _clear_halt(self, chat_id: int) -> None:
        current = SafetyControlService(self.store, self.audit).current_state()
        if current.state != SafetyState.HALTED:
            self._send(
                chat_id,
                f"Safety state is {current.state.value}; clear-halt only applies to halted.",
            )
            return
        self._send(
            chat_id,
            "\n".join(
                [
                    "Confirm clear-halt. This re-enables live approval execution.",
                    f"halt reason: {current.reason}",
                    "Recovery preflight (health checks) runs before the state change.",
                ]
            ),
            reply_markup=_confirmation_markup("clear-halt"),
        )

    def _kill_switch(self, chat_id: int) -> None:
        self._send(
            chat_id,
            "Confirm kill-switch. This blocks live execution until manual recovery.",
            reply_markup=_confirmation_markup("kill-switch"),
        )

    def _refresh_broker_snapshot(self) -> None:
        for _, service in build_broker_readonly_services(
            self.config,
            self.store,
            self.audit,
        ):
            service.fetch_and_store_snapshot(self.config.portfolio.allowed_symbols)

    def _refresh_portfolio_from_broker_snapshot(self) -> None:
        services = build_broker_readonly_services(self.config, self.store, self.audit)
        if not services:
            return
        run_id = new_run_id()
        states = []
        account_states: list[tuple[str | None, PortfolioState]] = []
        for logical_account_id, service in services:
            snapshot = service.fetch_and_store_snapshot(
                self.config.portfolio.allowed_symbols,
                run_id=run_id,
            )
            state = portfolio_state_from_broker_account(
                snapshot.account.model_dump(mode="json"),
                allowed_symbols=self.config.portfolio.allowed_symbols,
                universe=self.config.universe,
                unknown_symbol_policy=self.config.portfolio.unknown_broker_position_policy,
            )
            states.append(state)
            account_states.append((logical_account_id, state))
        for logical_account_id, state in account_states:
            if logical_account_id:
                self.store.save_portfolio_snapshot(
                    run_id,
                    state,
                    account_id=logical_account_id,
                )
        self.store.save_portfolio_snapshot(run_id, _merge_portfolio_states(states))

    def _broker_currency_breakdowns(self) -> dict[str, dict[str, float]]:
        snapshots = _latest_broker_snapshots_by_account(self.store, self.config)
        if not snapshots:
            return {"cash": {"unknown": 0.0}, "positions_market_value": {}, "total_value": {}}
        cash: dict[str, float] = {}
        positions_market_value: dict[str, float] = {}
        instrument_currencies = self._instrument_currencies()
        for snapshot in snapshots:
            account = _snapshot_account(snapshot)
            for currency, value in _cash_by_currency(account).items():
                cash[currency] = cash.get(currency, 0.0) + value
            for currency, value in _positions_market_value_by_currency(
                account,
                instrument_currencies,
            ).items():
                positions_market_value[currency] = positions_market_value.get(currency, 0.0) + value
        return {
            "cash": cash,
            "positions_market_value": positions_market_value,
            "total_value": _sum_currency_values(cash, positions_market_value),
        }

    def _instrument_currencies(self) -> dict[str, str]:
        return {
            instrument.symbol: _currency_value(instrument.currency)
            for instrument in self.config.universe.instruments
        }

    def _broker_position_prices(self) -> dict[str, tuple[float, str]]:
        prices: dict[str, float] = {}
        currencies: dict[str, str] = {}
        for snapshot in _latest_broker_snapshots_by_account(self.store, self.config):
            payload = _mapping(snapshot.get("payload"))
            account = payload.get("account")
            for symbol, price in _mapping_items(payload.get("current_prices")):
                if _is_number(price):
                    _merge_position_price(prices, str(symbol), float(price))
            if not isinstance(account, Mapping):
                continue
            positions = account.get("positions")
            if isinstance(positions, list):
                for position in positions:
                    if not isinstance(position, Mapping):
                        continue
                    symbol = position.get("symbol")
                    if not isinstance(symbol, str):
                        continue
                    if _is_number(position.get("current_price")):
                        _merge_position_price(prices, symbol, float(position["current_price"]))
                    currency = position.get("currency")
                    if currency:
                        currencies[symbol] = str(currency)
        instrument_currencies = self._instrument_currencies()
        return {
            symbol: (price, currencies.get(symbol) or instrument_currencies.get(symbol, "unknown"))
            for symbol, price in prices.items()
        }

    def _portfolio_position_labels(self) -> dict[str, str]:
        labels: dict[str, str] = {}
        for instrument in self.config.universe.instruments:
            currency = _currency_value(instrument.currency)
            if currency == "KRW" and instrument.name:
                labels[instrument.symbol] = f"{instrument.symbol} {instrument.name}"
        instrument_currencies = self._instrument_currencies()
        for snapshot in _latest_broker_snapshots_by_account(self.store, self.config):
            positions = _snapshot_account(snapshot).get("positions")
            if not isinstance(positions, list):
                continue
            for position in positions:
                if not isinstance(position, Mapping):
                    continue
                symbol = position.get("symbol")
                name = position.get("name")
                currency = _position_currency(position, instrument_currencies)
                if isinstance(symbol, str) and isinstance(name, str) and name and currency == "KRW":
                    labels[symbol] = f"{symbol} {name}"
        return labels

    def _has_readonly_broker_accounts(self) -> bool:
        return bool(broker_readonly_accounts(self.config))

    def _chat_allowed(self, chat_id: int) -> bool:
        return chat_id in set(self.config.approval.telegram_allowed_chat_ids)

    def _user_allowed(self, user_id: int) -> bool:
        return user_id in set(self.config.approval.whitelisted_user_ids)

    def _send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any] | None:
        try:
            return self.client.send_message(chat_id, text, reply_markup=reply_markup)
        except TypeError:
            return self.client.send_message(chat_id, text)

    def _answer(self, callback: Mapping[str, Any], text: str) -> None:
        callback_id = callback.get("id")
        if not isinstance(callback_id, str):
            return
        answer_callback_query = getattr(self.client, "answer_callback_query", None)
        if not callable(answer_callback_query):
            return
        try:
            answer_callback_query(callback_id, text)
        except (RuntimeError, TimeoutError, TypeError, ValueError):
            # Telegram rejects answers to old callbacks (e.g. after operator
            # downtime); the command result must still be processed.
            return

    def _edit_callback_message(self, callback: Mapping[str, Any], text: str) -> None:
        message = callback.get("message")
        if not isinstance(message, Mapping):
            return
        chat_id = _chat_id(message)
        message_id = message.get("message_id")
        edit_message_text = getattr(self.client, "edit_message_text", None)
        if chat_id is None or not isinstance(message_id, int) or not callable(edit_message_text):
            return
        try:
            edit_message_text(chat_id, message_id, text, reply_markup=None)
        except (RuntimeError, TimeoutError, TypeError, ValueError):
            return

    def _record(
        self,
        command: str,
        chat_id: int,
        user_id: int,
        username: str | None,
        status: str,
    ) -> None:
        payload = {
            "command": command,
            "chat_id": chat_id,
            "user_id": user_id,
            "username": username,
            "status": status,
        }
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            SystemEventType.TELEGRAM_COMMAND,
            payload,
        )

    def _record_update_failure(self, update_id: object, exc: Exception) -> None:
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            SystemEventType.TELEGRAM_COMMAND,
            {
                "status": "error",
                "update_id": update_id if isinstance(update_id, int) else None,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            },
        )

    def _latest_attribution_event(self, account_id: str) -> dict[str, Any] | None:
        event_types = {"account_attribution_reconciliation", "account_attribution_adopted"}
        for row in self.store.list_system_events(limit=2000):
            if row.get("event_type") not in event_types:
                continue
            if row["payload"].get("account_id") == account_id:
                return row
        return None

    def _latest_order_status(self, broker_order_id: str) -> LiveOrderStatusSnapshot | None:
        for row in self.store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_STATUS,
            limit=2000,
        ):
            status = LiveOrderStatusSnapshot.model_validate(row["payload"])
            if status.broker_order.broker_order_id == broker_order_id:
                return status
        return None

    def _status_with_resolved_account(
        self,
        status: LiveOrderStatusSnapshot,
    ) -> LiveOrderStatusSnapshot:
        if status.broker_order.account_id:
            return status
        internal_order_id = status.broker_order.order_id
        account_ids: set[str] = set()
        for row in self.store.list_orders(limit=2000):
            if row.get("order_id") == internal_order_id:
                account_id = (row.get("payload") or {}).get("account_id")
                if account_id:
                    account_ids.add(str(account_id))
        for row in self.store.list_system_events_by_type(
            "live_order_submit_intent",
            limit=2000,
        ):
            request = (row.get("payload") or {}).get("request") or {}
            if request.get("order_id") == internal_order_id:
                account_id = request.get("account_id")
                if account_id:
                    account_ids.add(str(account_id))
        if len(account_ids) != 1:
            return status
        account_id = next(iter(account_ids))
        return status.model_copy(
            update={
                "broker_order": status.broker_order.model_copy(
                    update={"account_id": str(account_id)}
                )
            }
        )

    def _pending_modify_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        for row in self.store.list_system_events_by_type(
            "live_order_modify_proposal_ack",
            limit=2000,
        ):
            if row["payload"].get("proposal_id") == proposal_id:
                return None
        for row in self.store.list_system_events_by_type(
            "live_order_modify_proposal",
            limit=2000,
        ):
            if row["payload"].get("proposal_id") == proposal_id:
                return row["payload"]
        return None

    def _pending_capacity_block(self, order_id: str) -> dict[str, Any] | None:
        if self._recovery_was_proposed(order_id):
            return None
        for row in self.store.list_system_events_by_type(
            "live_order_capacity_blocked",
            limit=2000,
        ):
            payload = row["payload"]
            if payload.get("blocked_order_id") == order_id:
                return payload
        return None

    def _pending_recovery_candidate(
        self,
        order_id: str,
    ) -> LiveOrderRecoveryCandidate | None:
        if self._recovery_was_proposed(order_id):
            return None
        for row in self.store.list_system_events_by_type(
            "live_order_recovery_candidate",
            limit=5000,
        ):
            if str(row["payload"].get("source_order_id")) == order_id:
                return LiveOrderRecoveryCandidate.model_validate(row["payload"])
        blocked = self._pending_capacity_block(order_id)
        if blocked is not None:
            return LiveOrderRecoveryCandidate(
                source_order_id=order_id,
                order=blocked["order"],
                source_type="capacity_blocked",
                reason=str(blocked.get("reason") or "capacity_blocked"),
                signal_run_id=blocked.get("signal_run_id"),
                created_at=str(blocked.get("checked_at") or utc_now().isoformat()),
            )

        order_row = next(
            (
                row
                for row in self.store.list_orders(limit=5000)
                if str(row.get("order_id")) == order_id
            ),
            None,
        )
        if order_row is None:
            return None
        order_payload = order_row["payload"]
        try:
            order = OrderIntent.model_validate(order_payload).model_dump(mode="json")
        except ValueError:
            return None
        if str(order_payload.get("approval_status") or "").lower() == "expired":
            return LiveOrderRecoveryCandidate(
                source_order_id=order_id,
                order=order,
                source_type="approval_expired",
                reason="telegram_approval_expired_before_submit",
                signal_run_id=order_payload.get("signal_run_id"),
                created_at=str(order_row.get("created_at") or utc_now().isoformat()),
            )

        for row in self.store.list_system_events_by_type(
            str(SystemEventType.LIVE_ORDER_LIFECYCLE),
            limit=5000,
        ):
            payload = row["payload"]
            if str(payload.get("order_id")) != order_id:
                continue
            final_status = str(payload.get("final_status") or "").lower()
            if final_status not in {"rejected", "failed", "halted"}:
                return None
            if payload.get("broker_order_id"):
                return None
            return LiveOrderRecoveryCandidate(
                source_order_id=order_id,
                order=order,
                source_type=f"lifecycle_{final_status}",
                reason=str(
                    payload.get("failed_reason") or payload.get("halt_reason") or final_status
                ),
                signal_run_id=payload.get("signal_run_id") or order_payload.get("signal_run_id"),
                created_at=str(payload.get("checked_at") or order_row.get("created_at")),
            )
        return None

    def _recovery_was_proposed(self, order_id: str) -> bool:
        proposals = {
            str(row["payload"].get("proposal_id")): row["payload"]
            for row in self.store.list_system_events_by_type(
                "live_order_retry_proposal",
                limit=5000,
            )
            if str(row["payload"].get("blocked_order_id")) == order_id
        }
        acknowledgements = {
            str(row["payload"].get("proposal_id")): str(row["payload"].get("status"))
            for row in self.store.list_system_events_by_type(
                "live_order_retry_proposal_ack",
                limit=5000,
            )
            if str(row["payload"].get("blocked_order_id")) == order_id
        }
        if "approved" in acknowledgements.values():
            return True
        for proposal_id, proposal in proposals.items():
            if proposal_id in acknowledgements:
                continue
            expires_at = proposal.get("expires_at")
            if not expires_at or utc_now() < datetime.fromisoformat(
                str(expires_at).replace("Z", "+00:00")
            ):
                return True
        return False

    def _pending_retry_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        for row in self.store.list_system_events_by_type(
            "live_order_retry_proposal_ack",
            limit=2000,
        ):
            if row["payload"].get("proposal_id") == proposal_id:
                return None
        for row in self.store.list_system_events_by_type(
            "live_order_retry_proposal",
            limit=2000,
        ):
            if row["payload"].get("proposal_id") == proposal_id:
                payload = row["payload"]
                expires_at = payload.get("expires_at")
                if expires_at and utc_now() >= datetime.fromisoformat(
                    str(expires_at).replace("Z", "+00:00")
                ):
                    self._ack_retry_proposal(payload, "expired", "telegram:timeout")
                    return None
                return payload
        return None

    def _pending_async_approval(
        self,
        approval_id: str,
    ) -> PendingApprovalEnvelope | None:
        for row in self.store.list_system_events_by_type(
            "telegram_approval_ack",
            limit=2000,
        ):
            if row["payload"].get("approval_id") == approval_id:
                return None
        for row in self.store.list_system_events_by_type(
            "telegram_approval_pending",
            limit=2000,
        ):
            if row["payload"].get("approval_id") == approval_id:
                envelope = PendingApprovalEnvelope.model_validate(row["payload"])
                if utc_now() >= envelope.expires_at:
                    return None
                return envelope
        return None


def _command_name(text: str) -> str:
    token = text.strip().split()[0]
    command = token.split("@", 1)[0].lower()
    return command


def _chat_id(message: Mapping[str, Any]) -> int | None:
    chat = message.get("chat")
    if not isinstance(chat, Mapping):
        return None
    chat_id = chat.get("id")
    return chat_id if isinstance(chat_id, int) else None


def _user_identity(user: object) -> tuple[int | None, str | None]:
    if not isinstance(user, Mapping):
        return None, None
    user_id = user.get("id")
    username = user.get("username")
    return (
        user_id if isinstance(user_id, int) else None,
        username if isinstance(username, str) else None,
    )


def _confirmation_markup(action: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"Confirm {action}",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}confirm:{action}",
                }
            ],
            [{"text": "Cancel", "callback_data": f"{OPERATOR_CALLBACK_PREFIX}cancel"}],
        ]
    }


def _async_approval_markup(approval_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Approve",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}appr:a:{approval_id}",
                },
                {
                    "text": "Reject",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}appr:r:{approval_id}",
                },
            ]
        ]
    }


def _attribution_markup(account_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Approve attribution",
                    "callback_data": (
                        f"{OPERATOR_CALLBACK_PREFIX}attribution:approve:{account_id}"
                    ),
                }
            ],
            [{"text": "Cancel", "callback_data": f"{OPERATOR_CALLBACK_PREFIX}cancel"}],
        ]
    }


def _modify_markup(proposal_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Approve modification",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}modify:approve:{proposal_id}",
                }
            ],
            [{"text": "Cancel", "callback_data": f"{OPERATOR_CALLBACK_PREFIX}cancel"}],
        ]
    }


def _retry_order_markup(proposal_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Approve retry order",
                    "callback_data": (
                        f"{OPERATOR_CALLBACK_PREFIX}retry-order:approve:{proposal_id}"
                    ),
                }
            ],
            [
                {
                    "text": "Reject",
                    "callback_data": (
                        f"{OPERATOR_CALLBACK_PREFIX}retry-order:reject:{proposal_id}"
                    ),
                }
            ],
        ]
    }


def _recovery_review_markup(order_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "재주문 검토",
                    "callback_data": (f"{OPERATOR_CALLBACK_PREFIX}recover:review:{order_id}"),
                }
            ]
        ]
    }


def _recoverable_orders_markup(
    candidates: list[LiveOrderRecoveryCandidate],
) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": (f"재주문 검토 · {candidate.order.get('symbol') or 'unknown'}"),
                    "callback_data": (
                        f"{OPERATOR_CALLBACK_PREFIX}recover:review:{candidate.source_order_id}"
                    ),
                }
            ]
            for candidate in candidates
        ]
    }


def _recovery_options_markup(
    order_id: str,
    original_quantity: float,
    max_quantity: float,
) -> dict[str, Any]:
    rows = [
        [
            {
                "text": f"원 수량 {original_quantity:g}",
                "callback_data": (f"{OPERATOR_CALLBACK_PREFIX}recover:original:{order_id}"),
            }
        ],
        [
            {
                "text": f"현재 최대 {max_quantity:g}",
                "callback_data": f"{OPERATOR_CALLBACK_PREFIX}recover:max:{order_id}",
            }
        ],
        [
            {
                "text": "직접 수량 입력",
                "callback_data": f"{OPERATOR_CALLBACK_PREFIX}recover:input:{order_id}",
            }
        ],
    ]
    return {"inline_keyboard": rows}


def _sent_message_id(response: Mapping[str, Any] | None) -> int | None:
    if not isinstance(response, Mapping):
        return None
    result = response.get("result")
    message_id = result.get("message_id") if isinstance(result, Mapping) else None
    return message_id if isinstance(message_id, int) else None


def _cash_flow_proposal_markup(proposal: Mapping[str, Any]) -> dict[str, Any]:
    proposal_id = str(proposal.get("proposal_id") or "")
    keyboard = [
        [
            {
                "text": "Approve allocation",
                "callback_data": f"{OPERATOR_CALLBACK_PREFIX}cash-flow:approve:{proposal_id}",
            },
            {
                "text": "Ignore",
                "callback_data": f"{OPERATOR_CALLBACK_PREFIX}cash-flow:ignore:{proposal_id}",
            },
        ]
    ]
    # Assign buttons carry the allocation index instead of the strategy id:
    # strategy ids pushed the callback_data past Telegram's 64-byte limit,
    # which made the whole sendMessage call fail.
    for index, allocation in enumerate(proposal.get("allocations") or []):
        strategy_id = str(_mapping(allocation).get("strategy_id") or "")
        if not strategy_id:
            continue
        keyboard.append(
            [
                {
                    "text": f"Assign {_telegram_strategy_display_name(strategy_id)}",
                    "callback_data": (
                        f"{OPERATOR_CALLBACK_PREFIX}cash-flow:asg:{proposal_id}:{index}"
                    ),
                }
            ]
        )
    return {"inline_keyboard": keyboard}


def _assigned_strategy_id_from_token(
    proposal: Mapping[str, Any],
    token: str,
) -> str | None:
    """Resolve an assign-callback token to a strategy id.

    New callbacks carry the allocation index; callbacks from messages sent
    before the 64-byte callback_data fix carry the strategy id itself.
    """
    allocations = [_mapping(row) for row in proposal.get("allocations") or []]
    if token.isdigit():
        index = int(token)
        if index >= len(allocations):
            return None
        strategy_id = str(allocations[index].get("strategy_id") or "")
        return strategy_id or None
    if any(row.get("strategy_id") == token for row in allocations):
        return token
    return None


def _mask_identifier(value: object) -> str:
    text = str(value or "")
    if not text:
        return "none"
    if text == "multiple":
        return text
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + ("*" * max(len(text) - 4, 1)) + text[-2:]


def _money(value: object) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "unknown"


def _money_by_currency(values: Mapping[str, float]) -> str:
    if not values:
        return "unknown"
    return ", ".join(f"{value:,.2f} {currency}" for currency, value in sorted(values.items()))


def _number(value: object) -> str:
    try:
        return f"{float(value):,.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "unknown"


def _float_or_none(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _merge_position_price(prices: dict[str, float], symbol: str, price: float) -> None:
    current = prices.get(symbol)
    if current is None or current <= 0 < price:
        prices[symbol] = price
    elif price <= 0 and symbol not in prices:
        prices[symbol] = price


def _disabled_broker_account_ids(config: MaestroConfig | None) -> set[str]:
    """Native identifiers (config `id` and, when set, the literal
    `account_id`) of accounts that are explicitly disabled in the config.

    Broker snapshots are never deleted, so a disabled/retired account (e.g.
    a mock/paper account used during setup) keeps its last snapshot in the
    state DB forever. Without this filter, `_latest_broker_snapshots_by_
    account` would keep including that stale snapshot in bot-facing
    aggregates like `_broker_currency_breakdowns` (the /status and /account
    commands' cash/exposure totals), silently overstating them the same way
    the dashboard's equivalent bug did. Mirrors `maestro.dashboard.
    read_models._disabled_native_account_ids` / `maestro.orchestration.
    live_gates._disabled_account_native_keys` — see those for the deny- vs
    allow-list rationale — duplicated here rather than imported to keep this
    module decoupled.
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


def _broker_snapshot_account_keys(row: Mapping[str, Any]) -> set[str]:
    payload = _mapping(row.get("payload"))
    account = _mapping(payload.get("account"))
    return {
        str(value)
        for value in (row.get("account_id"), payload.get("account_id"), account.get("account_id"))
        if value
    }


def _latest_broker_snapshots_by_account(
    store: StateStore,
    config: MaestroConfig | None = None,
) -> list[dict[str, Any]]:
    disabled_ids = _disabled_broker_account_ids(config)
    latest_by_account = []
    seen = set()
    for snapshot in store.list_broker_account_snapshots(limit=1000):
        account_id = _broker_snapshot_account_id(snapshot)
        if not account_id or account_id in seen:
            continue
        # `account_id` alone may resolve to the raw broker account number
        # (see _broker_snapshot_account_id's priority order) rather than the
        # config's logical id, so check every candidate identifier this
        # snapshot carries against the deny-list, not just the primary one.
        if disabled_ids and _broker_snapshot_account_keys(snapshot) & disabled_ids:
            continue
        seen.add(account_id)
        latest_by_account.append(snapshot)
    return latest_by_account


def _snapshot_account(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = _mapping(snapshot.get("payload"))
    account = payload.get("account")
    return account if isinstance(account, Mapping) else {}


def _merge_portfolio_states(states: list[PortfolioState]) -> PortfolioState:
    if not states:
        return PortfolioState(cash=0.0, cash_by_currency={}, positions={})
    cash_by_currency: dict[str, float] = {}
    positions: dict[str, float] = {}
    for state in states:
        if state.cash_by_currency:
            for currency, cash in state.cash_by_currency.items():
                cash_by_currency[currency] = cash_by_currency.get(currency, 0.0) + cash
        else:
            cash_by_currency["CASH"] = cash_by_currency.get("CASH", 0.0) + state.cash
        for symbol, quantity in state.positions.items():
            positions[symbol] = positions.get(symbol, 0.0) + quantity
    return PortfolioState(
        cash=sum(cash_by_currency.values()),
        cash_by_currency=cash_by_currency,
        positions=positions,
    )


def _broker_snapshot_account_id(row: Mapping[str, Any]) -> str:
    payload = _mapping(row.get("payload"))
    account = _mapping(payload.get("account"))
    return str(
        row.get("account_id") or payload.get("account_id") or account.get("account_id") or ""
    )


def _cash_by_currency(account: Mapping[str, Any]) -> dict[str, float]:
    cash_by_currency = account.get("cash_by_currency")
    if isinstance(cash_by_currency, Mapping) and cash_by_currency:
        return {
            str(currency): float(value)
            for currency, value in cash_by_currency.items()
            if _is_number(value)
        }
    cash = account.get("cash")
    if not _is_number(cash):
        return {}
    cash_balance = account.get("cash_balance")
    currency = str(account.get("currency") or "unknown")
    if isinstance(cash_balance, Mapping):
        currency = str(cash_balance.get("currency") or currency)
    return {currency: float(cash)}


def _positions_market_value_by_currency(
    account: Mapping[str, Any],
    instrument_currencies: Mapping[str, str],
) -> dict[str, float]:
    positions = account.get("positions")
    if not isinstance(positions, list):
        return {}
    values: dict[str, float] = {}
    for position in positions:
        if not isinstance(position, Mapping):
            continue
        value = _position_market_value(position)
        if value is None:
            continue
        currency = _position_currency(position, instrument_currencies)
        values[currency] = values.get(currency, 0.0) + value
    return values


def _position_market_value(position: Mapping[str, Any]) -> float | None:
    market_value = position.get("market_value")
    if _is_number(market_value):
        return float(market_value)
    quantity = position.get("quantity")
    current_price = position.get("current_price")
    if not _is_number(quantity) or not _is_number(current_price):
        return None
    return float(quantity) * float(current_price)


def _position_currency(
    position: Mapping[str, Any],
    instrument_currencies: Mapping[str, str],
) -> str:
    currency = position.get("currency")
    if currency:
        return str(currency)
    symbol = position.get("symbol")
    if isinstance(symbol, str) and symbol in instrument_currencies:
        return instrument_currencies[symbol]
    return "unknown"


def _sum_currency_values(
    left: Mapping[str, float],
    right: Mapping[str, float],
) -> dict[str, float]:
    values = dict(left)
    for currency, value in right.items():
        values[currency] = values.get(currency, 0.0) + value
    return values


def _approval_notional_label(row: Mapping[str, Any]) -> str:
    payload = row.get("payload")
    request = payload.get("request") if isinstance(payload, Mapping) else None
    orders = request.get("proposed_orders") if isinstance(request, Mapping) else None
    totals: dict[str, float] = {}
    if isinstance(orders, list):
        for order in orders:
            if not isinstance(order, Mapping):
                continue
            notional = order.get("notional")
            currency = order.get("currency")
            if not _is_number(notional) or not currency:
                continue
            totals[str(currency)] = totals.get(str(currency), 0.0) + float(notional)
    if totals:
        return _money_by_currency(totals)
    return _money(row.get("estimated_notional"))


def _portfolio_position_line(
    symbol: str,
    quantity: float,
    position_prices: Mapping[str, tuple[float, str]],
    instrument_currencies: Mapping[str, str],
    position_labels: Mapping[str, str],
) -> str:
    label = position_labels.get(symbol, symbol)
    price = position_prices.get(symbol)
    if price is None:
        return f"{label}: {_number(quantity)}"
    current_price, currency = price
    if currency == "unknown" and symbol in instrument_currencies:
        currency = instrument_currencies[symbol]
    market_value = quantity * current_price
    return (
        f"{label}: {_number(quantity)} @ {_number(current_price)} {currency} "
        f"= {_number(market_value)} {currency}"
    )


def _mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _mapping_items(value: object):
    if not isinstance(value, Mapping):
        return []
    return value.items()


def _currency_value(currency: object) -> str:
    return str(getattr(currency, "value", currency))


def _is_number(value: object) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def _operator_time(value: object, config: MaestroConfig) -> str:
    if not value:
        return "none"
    return format_operator_time(value, operator_timezone(config))


def telegram_bot_commands(signal_config: MaestroConfig | None = None) -> list[dict[str, str]]:
    commands = [
        {"command": command, "description": description}
        for command, description in TELEGRAM_OPERATOR_COMMANDS
    ]
    if signal_config is None:
        return commands
    seen = {command["command"] for command in commands}
    for strategy in signal_config.strategies:
        if not strategy.enabled or not strategy.signal_enabled:
            continue
        display_name = _telegram_strategy_display_name(strategy.id)
        for command, description in (
            (_primary_signal_command(strategy.id), f"Generate {display_name} signal"),
            (
                _primary_rebalance_command(strategy.id),
                f"Run {display_name} manual rebalance",
            ),
        ):
            if command in seen:
                continue
            seen.add(command)
            commands.append({"command": command, "description": description})
    return commands


def _strategy_id_for_signal_command(command: str, config: MaestroConfig) -> str | None:
    command_name = command.removeprefix("/")
    matches = {_primary_signal_command(strategy.id): strategy.id for strategy in config.strategies}
    return matches.get(command_name)


def _primary_signal_command(strategy_id: str) -> str:
    return f"signal_{_strategy_command_stem(strategy_id)}"


def _primary_rebalance_command(strategy_id: str) -> str:
    return f"rebalance_{_strategy_command_stem(strategy_id)}"


def _strategy_command_stem(strategy_id: str) -> str:
    slug = _telegram_strategy_command_slug(strategy_id) or _telegram_command_slug(strategy_id)
    if slug.endswith("_us"):
        slug = slug.removesuffix("_us")
    return slug


def _strategy_id_for_rebalance_command(command: str, config: MaestroConfig) -> str | None:
    command_name = command.removeprefix("/")
    matches = {
        _primary_rebalance_command(strategy.id): strategy.id
        for strategy in config.strategies
        if strategy.enabled and strategy.signal_enabled
    }
    return matches.get(command_name)


def _rebalance_units_from_env() -> dict[str, str]:
    units: dict[str, str] = {}
    for item in os.getenv(REBALANCE_UNITS_ENV, "").split(","):
        strategy_id, _, unit = item.partition("=")
        if strategy_id.strip() and unit.strip():
            units[strategy_id.strip()] = unit.strip()
    return units


def _rebalance_unit_for_strategy(strategy_id: str) -> str | None:
    return _rebalance_units_from_env().get(strategy_id)


def _start_systemd_unit(unit: str) -> None:
    subprocess.run(
        ["systemctl", "start", "--no-block", unit],
        check=True,
        capture_output=True,
        timeout=30,
    )


def _rebalance_markup(strategy_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Approve rebalance",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}rebalance:approve:{strategy_id}",
                }
            ],
            [{"text": "Cancel", "callback_data": f"{OPERATOR_CALLBACK_PREFIX}cancel"}],
        ]
    }


def _telegram_command_slug(value: str) -> str:
    slug = []
    previous_underscore = False
    for char in value.lower():
        allowed = ("a" <= char <= "z") or ("0" <= char <= "9") or char == "_"
        next_char = char if allowed else "_"
        if next_char == "_":
            if previous_underscore:
                continue
            previous_underscore = True
        else:
            previous_underscore = False
        slug.append(next_char)
    return "".join(slug).strip("_")


def _compact_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    return f"{seconds / 3600:.1f}h"


__all__ = ["TelegramOperatorCommandRouter", "telegram_bot_commands"]

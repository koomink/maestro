import os
import subprocess
from collections import defaultdict
from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta
from math import isfinite
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
    strategy_display_label as _telegram_strategy_display_label,
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
from maestro.execution.account_cash_flows import AccountCashFlowService
from maestro.execution.base import OrderIntent
from maestro.execution.broker_capacity_lookup import get_order_buying_power
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
from maestro.execution.cash_flow_candidates import (
    BROKER_REPORTED_CASH,
    PROXY_CASH,
    CashFlowCandidateDetector,
    FxConversionCandidate,
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
from maestro.execution.live_order_tracking import list_unreconciled_live_order_fills
from maestro.execution.order_builder import (
    QUOTED_QUANTITY_TOLERANCE,
    floor_to_step,
    round_price_to_tick,
)
from maestro.execution.order_capacity import OrderCapacityService
from maestro.integrations.telegram.bot import TelegramBotClient
from maestro.integrations.telegram.ui import catalog as ui_catalog
from maestro.integrations.telegram.ui.approval_stage import (
    PROGRESS_RANK,
    approval_needs_attention,
    approval_progress,
    card_stage,
    keep_forward_progress,
)
from maestro.integrations.telegram.ui.cards import (
    RenderedCard,
    approval_decision_text,
    approval_markup,
    approval_reminder_text,
    render_approval_card,
    render_approval_stage_card,
    render_daily_card,
)
from maestro.integrations.telegram.ui.lifecycle import CardLifecycleManager
from maestro.monitoring.audit_logger import AuditLogger
from maestro.monitoring.health import HealthService
from maestro.ops.readonly_refresh import refresh_readonly_accounts
from maestro.ops.workflow_recovery import WorkflowRecoveryService
from maestro.orchestration.live_gates import LiveExecutionGateService
from maestro.orchestration.orchestrator import (
    MaestroOrchestrator,
    SignalApprovalSummary,
    SignalRunSummary,
)
from maestro.portfolio.account_attribution import AccountAttributionReconciliationService
from maestro.safety.controls import SafetyControlService
from maestro.state.events import (
    CASH_SUSPENSE_CLASSIFICATIONS,
    SystemEventType,
    flow_class_for_cash_suspense,
    save_audited_system_event,
)
from maestro.state.funding_workflow import (
    PHASES,
    WorkflowClaimRefused,
    claim_workflow_attempt,
    complete_workflow,
    converge_workflow_invariants,
    is_request_pending,
    list_incomplete_workflows,
    load_migration_cutoff,
    load_request_payload,
    workflow_id_from_request,
)
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore
from maestro.state.upgrade_backfill import (
    completed_legacy_approval_ids,
    has_dispatch_manifest,
)

OPERATOR_CALLBACK_PREFIX = "operator:"
# Callback data is length-limited, so classifications travel as single letters.
_CASH_DRIFT_CLASSIFICATION_TOKENS = {
    "s": "settlement_candidate",
    "t": "transfer_candidate",
    "u": "unexplained",
    "d": "dividend",
    "i": "interest",
    "x": "tax",
    "f": "fee",
    "c": "fx_conversion",
}
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
    ("cash_drift", "Review Toss buying-power cash suspense"),
    ("cash_flow", "Confirm a detected cash flow: /cash_flow <proposal_id> <amount>"),
    ("portfolio", "Show Maestro portfolio state"),
    ("apps", "Show configured strategy apps"),
    ("orders", "Show recent orders"),
    ("approvals", "Show recent approvals"),
    ("budget", "Select a pending contribution budget: /budget <request_id> <amount>"),
    ("attribution", "Review account attribution: /attribution <account_id>"),
    ("modify", "Propose order modification: /modify <broker_order_id> <price> [quantity]"),
    ("retry_order", "Retry a capacity-blocked order with a corrected quantity"),
    ("pause", "Confirm pause of live approval execution"),
    ("recovery", "Show blocked workflows and safe recovery actions"),
    ("clear_halt", "Confirm recovery from a safety halt (runs preflight)"),
    ("kill_switch", "Confirm emergency live execution stop"),
)

TELEGRAM_UI_COMMANDS: tuple[tuple[str, str], ...] = (
    ("today", "오늘의 투자 현황"),
    ("portfolio", "내 자산"),
    ("system", "시스템 상태"),
    ("history", "지난 기록"),
    ("help", "도움말"),
)

# 메뉴(set_my_commands)에는 넣지 않지만 /help와 /system 버튼에서 항상 도달 가능해야 하는
# 비상·복구 명령. 장애 중에도 운영자가 UI만으로 정지·복구할 수 있어야 한다.
TELEGRAM_EMERGENCY_COMMANDS: tuple[tuple[str, str], ...] = (
    ("pause", "실행 일시중지"),
    ("kill_switch", "긴급 정지"),
    ("clear_halt", "정지 해제 (사전 점검 실행)"),
    ("recovery", "복구 센터"),
)

#: 자동 재개 횟수 상한. 넘으면 ⚠️ 알림으로 운영자에게 넘긴다.
#: 최초 콜백(attempt 1)도 실제 집행 시도이므로 예산에 포함된다 — 자동 재개는
#: attempt 2·3·4까지만 이어진다.
_MAX_RESUME_ATTEMPT = 4
#: Dispatch resume has no interactive first attempt to charge, so the whole
#: budget is automatic retries.
_MAX_DISPATCH_RESUME_ATTEMPT = 3
#: funding/budget 전이의 재개 상한. 승인 재개와 같은 값에서 출발한다 --
#: attempt 1은 운영자의 최초 탭이므로 실제 재개 예산은 2·3·4의 세 번이다.
#: 상한이 없으면 매번 실패하는 재개를 무한히 권하게 된다: 탭할 때마다
#: attempt가 오르고, stall 알림의 duplicate_key에 attempt가 들어 있어 그때마다
#: 새 카드가 나간다.
_MAX_WORKFLOW_RESUME_ATTEMPT = 4
#: claim 후 이 시간이 지나도록 종료 기록이 없으면 버려진 시도로 보고 회수한다.
#: 운영자 봇 poll 간격(초 단위)과 resolution 소요(브로커 폴링 포함)를 고려한 값.
_RESUME_LEASE_SECONDS = 900


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
        self._card_manager = CardLifecycleManager(
            store,
            audit,
            client,
            chat_ids=[int(chat_id) for chat_id in config.approval.telegram_allowed_chat_ids],
        )

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
        if command == "/cash_drift":
            self._cash_drift(chat_id)
            self._record(command, chat_id, user_id, username, "handled")
            return True
        if command in {"/cash_flow", "/cash-flow"}:
            self._process_cash_flow_command(text, chat_id, user_id, username)
            return True

        handler = {
            f"/{command}": handler
            for command, handler in {
                "help": self._help,
                "rebalance": self._rebalance_usage,
                "status": self._status,
                "health": self._health,
                "signal": self._signal,
                "today": self._signal,
                "system": self._health,
                "history": self._orders,
                "account": self._account,
                "portfolio": self._portfolio,
                "apps": self._apps,
                "orders": self._orders,
                "approvals": self._approvals,
                "pause": self._pause,
                "recovery": self._recovery,
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
        # sweep 실패가 update 폴링을 막으면 승인·거절 콜백을 영영 처리하지 못한다.
        for sweep in (
            self._sweep_pending_approvals,
            self._sweep_recovery_notifications,
            self._sweep_lifecycle_cards,
            self._sweep_incomplete_workflows,
            self._converge_workflow_invariants,
        ):
            try:
                sweep()
            except Exception as exc:  # noqa: BLE001 - 폴링 루프를 막지 않는 것이 우선
                self._record_update_failure(None, exc)
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

    def notify_pending_cash_flows(self) -> None:
        account_ids = {
            str(account_id)
            for account_id, _ in broker_readonly_accounts(self.config)
            if account_id is not None
        }
        account_ids.update(
            str(account.id)
            for account in getattr(self.config, "accounts", [])
            if getattr(account, "enabled", False) and getattr(account, "id", None)
        )
        for chat_id in self.config.approval.telegram_allowed_chat_ids:
            for account_id in sorted(account_ids):
                self._send_voluntary_deposit_allocation_proposal(
                    int(chat_id),
                    account_id=account_id,
                )
        self._notify_unreconciled_live_order_fills()

    def _notify_unreconciled_live_order_fills(self) -> None:
        candidates = list_unreconciled_live_order_fills(self.store)
        for candidate in candidates:
            notice_key = (
                "telegram-unreconciled-fill-notice:"
                f"{candidate['broker_order_id']}:{candidate['filled_quantity']}"
            )
            if self.store.duplicate_key_exists(notice_key):
                continue
            age_minutes = int(float(candidate["age_seconds"]) // 60)
            message = (
                "Maestro unreconciled fill warning\n"
                f"account_id: {_mask_identifier(str(candidate['account_id']))}\n"
                f"symbol: {candidate['symbol']}\n"
                f"side: {candidate['side']}\n"
                f"missing quantity: {float(candidate['missing_quantity']):g}\n"
                f"estimated principal: {_money(float(candidate['missing_notional']))}\n"
                f"unreconciled for: {age_minutes} minutes\n"
                f"broker_order_id: {_mask_identifier(str(candidate['broker_order_id']))}\n\n"
                "New live execution should remain blocked until fill reconciliation succeeds."
            )
            for chat_id in self.config.approval.telegram_allowed_chat_ids:
                self._send(int(chat_id), message)
            save_audited_system_event(
                self.store,
                self.audit,
                new_run_id(),
                SystemEventType.TELEGRAM_UNRECONCILED_FILL_NOTICE,
                {
                    **candidate,
                    "duplicate_key": notice_key,
                },
            )

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
        if action.startswith("menu:"):
            return self._process_menu_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
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
        if action.startswith("wfresume:"):
            return self._process_workflow_resume_callback(
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
        if action.startswith("cash-drift:"):
            return self._process_cash_drift_callback(
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
        if action.startswith("ui:"):
            return self._process_ui_toggle(callback, action, chat_id, user_id, username)
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
        if action.startswith("wfrec:"):
            return self._process_workflow_recovery_callback(
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

    def _process_workflow_recovery_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        if action == "wfrec:orders":
            self._answer(callback, "Opening recoverable orders.")
            self._orders(chat_id)
            self._record("/recovery", chat_id, user_id, username, "orders_opened")
            return True
        parts = action.split(":")
        if len(parts) != 3 or parts[1] not in {"auto", "attest"}:
            self._answer(callback, "This recovery action is no longer active.")
            self._record("/recovery", chat_id, user_id, username, "stale_callback")
            return True
        mode, fingerprint = parts[1], parts[2]
        decided_by = f"telegram:{username or user_id}"
        try:
            result = WorkflowRecoveryService(
                self.config,
                self.store,
                self.audit,
            ).recover_live_orders(
                reason=f"Telegram recovery confirmed by {username or user_id}",
                decided_by=decided_by,
                expected_fingerprint=fingerprint,
                manual_attestation=mode == "attest",
            )
        except Exception as exc:  # noqa: BLE001 - recovery must report any failed preflight
            self._answer(callback, "Recovery remains blocked.")
            self._edit_callback_message(callback, f"Recovery blocked: {exc}")
            self._record("/recovery", chat_id, user_id, username, "failed")
            return True
        if result.status == "attestation_required":
            lines = [
                "Automatic order matching was inconclusive.",
                "Check the broker app and confirm that every listed order was "
                "neither accepted nor filled.",
            ]
            for item in result.unmatched_orders:
                lines.append(
                    f"- {item.get('order_id') or 'unknown'} "
                    f"account={item.get('account_id') or 'unknown'} "
                    f"candidates={len(item.get('candidate_orders') or [])}"
                )
            self._answer(callback, "Broker verification required.")
            self._edit_callback_message(
                callback,
                "\n".join(lines),
                reply_markup=_workflow_recovery_markup(
                    result.fingerprint,
                    attestation=True,
                ),
            )
            self._record("/recovery", chat_id, user_id, username, "attestation_required")
            return True
        self._answer(callback, "Recovery completed.")
        self._edit_callback_message(
            callback,
            "\n".join(
                [
                    "Live-order recovery completed.",
                    f"resolved_orders: {len(result.resolved_orders)}",
                    f"applied_fills: {result.applied_fill_count}",
                    "Use /recovery again if a separate safety halt remains.",
                ]
            ),
        )
        self._record("/recovery", chat_id, user_id, username, "confirmed")
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
        retry_price = round_price_to_tick(
            retry_price,
            config.universe.get(original.symbol),
        )
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
            lambda candidate: self._lookup_retry_capacity(config, candidate),
            quantity_step=lambda candidate: _quantity_step(config, candidate),
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
        # Same rounding the pre-approval block report uses, so the quantity the
        # operator is offered here matches the one the alert quoted.
        maximum = floor_to_step(
            max(0.0, maximum),
            _quantity_step(config, original),
            tolerance=QUOTED_QUANTITY_TOLERANCE,
        )
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
                lambda candidate: self._lookup_retry_capacity(config, candidate),
                quantity_step=lambda candidate: _quantity_step(config, candidate),
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

    def _process_menu_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        """시스템 상태 카드의 비상 제어 버튼 → 기존 확인 절차로 연결한다."""
        target = action.removeprefix("menu:")
        handlers = {
            "pause": self._pause,
            "kill_switch": self._kill_switch,
            "clear_halt": self._clear_halt,
            "recovery": self._recovery,
        }
        handler = handlers.get(target)
        if handler is None:
            self._answer(callback, ui_catalog.STALE_CALLBACK_TEXT)
            return True
        self._answer(callback, "")
        handler(chat_id)
        self._record(f"/{target}", chat_id, user_id, username, "menu_opened")
        return True

    def _process_ui_toggle(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        parts = action.split(":")
        kind = parts[1] if len(parts) > 1 else ""
        page = 0
        if kind in {"d", "f"} and len(parts) == 3:
            approval_id = parts[2]
        elif kind == "p" and len(parts) == 4 and parts[3].isdigit():
            approval_id = parts[2]
            page = int(parts[3])
        else:
            self._answer(callback, ui_catalog.STALE_CALLBACK_TEXT)
            return True
        envelope = self._pending_async_approval(approval_id)
        if envelope is None:
            self._answer(callback, ui_catalog.STALE_CALLBACK_TEXT)
            self._record("/approval", chat_id, user_id, username, "stale_callback")
            return True
        card = render_approval_card(
            envelope.request,
            expanded=kind in {"d", "p"},
            page=page,
        )
        message = callback.get("message")
        message_id = message.get("message_id") if isinstance(message, Mapping) else None
        if message_id is None:
            self._answer(callback, ui_catalog.STALE_CALLBACK_TEXT)
            return True
        try:
            self.client.edit_message_text(
                chat_id,
                int(message_id),
                card.text,
                reply_markup=card.reply_markup,
            )
        except (RuntimeError, ValueError):
            self._answer(callback, ui_catalog.CALLBACK_FAILED_TEXT)
            return True
        self._answer(callback, "")
        self._record("/approval", chat_id, user_id, username, "ui_toggle")
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
            self._answer(callback, ui_catalog.STALE_CALLBACK_TEXT)
            return True
        envelope = self._pending_async_approval(parts[2])
        if envelope is None:
            self._answer(callback, ui_catalog.STALE_CALLBACK_TEXT)
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
        except (RuntimeError, TimeoutError, TypeError, ValueError):
            self._answer(callback, ui_catalog.CALLBACK_FAILED_TEXT)
            self._record("/approval", chat_id, user_id, username, "failed")
            return True
        self._answer(
            callback,
            ui_catalog.ANSWER_APPROVED if status == "approved" else ui_catalog.ANSWER_REJECTED,
        )
        self._edit_callback_message(
            callback,
            approval_decision_text(
                status,
                envelope.approval_id,
                orders_submitted=summary.orders_submitted,
                orders_failed=summary.orders_failed,
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
        attempt: int = 1,
    ):
        decision = ApprovalDecision(
            approval_id=envelope.approval_id,
            run_id=envelope.run_id,
            status=status,
            decided_at=utc_now(),
            decided_by=decided_by,
            reason=reason,
        )
        if attempt == 1:
            # ack는 운영자 의사를 처음 받아쓸 때만 기록한다. 재개(attempt > 1)는
            # 이미 기록된 ack를 읽어서 오는 길이므로 다시 쓰면 duplicate_key
            # 가드에 걸려 재개 자체가 불가능해진다.
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
                        "schema_version": 2,
                    },
                )
        # 집행과 그 종결 기록은 하나의 임계구역이다. 운영자 정산(settle_approval)은
        # live_order_lock을 쥔 채 증거를 읽고 종결을 쓴다. 여기서 집행 직후 락을
        # 놓고 기록만 따로 하면 그 사이에 정산이 끼어들어, 방금 나간 주문을 두고
        # "나가지 않았다"는 종결을 남길 수 있다. orchestrator가 같은 락을 재진입으로
        # 다시 잡으므로 순서(live_order_lock -> writer_lock)는 그대로다.
        with self.store.live_order_lock("telegram_approval_resolution"):
            try:
                summary = self._run_resolution(envelope, decision)
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
            self._record_resolution_completed(envelope, decision, summary, attempt=attempt)
        return summary

    def _run_resolution(
        self,
        envelope: PendingApprovalEnvelope,
        decision: ApprovalDecision,
    ) -> SignalApprovalSummary:
        config = self.config
        identity = self.config_identity
        if self.approval_config_path is not None:
            config, identity = load_config_with_identity(self.approval_config_path)
        return MaestroOrchestrator(
            config,
            telegram_client=self.client,
            config_identity=identity,
        ).resolve_pending_signal_approval(envelope, decision)

    def _record_resolution_completed(
        self,
        envelope: PendingApprovalEnvelope,
        decision: ApprovalDecision,
        summary: SignalApprovalSummary,
        *,
        attempt: int,
    ) -> None:
        payload = {
            "approval_id": envelope.approval_id,
            "signal_run_id": envelope.signal_run_id,
            "status": decision.status,
            "orders_submitted": summary.orders_submitted,
            "orders_failed": summary.orders_failed,
            "resolved_at": utc_now().isoformat(),
            "attempt": attempt,
            "duplicate_key": f"telegram-approval-completed:{envelope.approval_id}",
        }
        # duplicate_key 가드로는 부족하다. 운영자 정산은 같은 이벤트를
        # `telegram-approval-settled:`로 쓰므로 서로의 키를 보지 못하고, 읽는 쪽은
        # 승인당 가장 최근 행을 집는다 — 두 번째 행은 중복이 아니라 그 배치에 대한
        # 권위 있는 설명을 바꿔치기한다. 유일성은 키가 아니라 승인 단위여야 한다.
        stored, created = self.store.insert_approval_resolution(envelope.run_id, payload)
        if created:
            self.audit.log(
                envelope.run_id, "telegram_approval_resolution_completed", payload
            )
            return
        if stored.get("duplicate_key") == payload["duplicate_key"]:
            # 이 재개가 남긴 기록이 이미 있다. 종결 기록 직후 죽었다가 다시 온
            # 경우이므로 멱등하게 넘어간다 — 다른 이야기가 끼어든 것이 아니다.
            return
        self._record_resolution_conflict(envelope, payload, stored)

    def _record_resolution_conflict(
        self,
        envelope: PendingApprovalEnvelope,
        payload: Mapping[str, Any],
        stored: Mapping[str, Any],
    ) -> None:
        """종결 기록에서 졌다는 사실이 집행을 되돌리지는 않는다.

        기록에 남은 종결은 "운영자가 손으로 닫았다"고 말하는데, 진 쪽은 방금 그
        배치를 실제로 집행했다. 조용히 돌아서면 운영자는 첫 번째 이야기를 믿고
        움직이고 — 정산해 둔 자리를 손으로 다시 채우고 — 계좌의 실제 상태는 두
        번째 이야기다. 내구 기록으로 남기고 운영자에게 넘긴다.
        """
        try:
            duplicate_key = f"telegram-approval-resolution-conflict:{envelope.approval_id}"
            with self.store.writer_lock("telegram_approval_resolution_conflict"):
                if not self.store.duplicate_key_exists(duplicate_key):
                    save_audited_system_event(
                        self.store,
                        self.audit,
                        envelope.run_id,
                        "telegram_approval_resolution_conflict",
                        {
                            "approval_id": envelope.approval_id,
                            "signal_run_id": envelope.signal_run_id,
                            "orders_submitted": payload.get("orders_submitted"),
                            "orders_failed": payload.get("orders_failed"),
                            "attempt": payload.get("attempt"),
                            "settled_by": stored.get("settled_by"),
                            "recorded_closure": stored.get("duplicate_key"),
                            "duplicate_key": duplicate_key,
                        },
                    )
            # 주문이 브로커에 있을 수 있다는 문구로 알린다. 정산된 배치를 두고
            # 운영자가 확인해야 하는 것이 정확히 그것이다.
            self._notify_approval_needs_attention(
                envelope.approval_id, envelope.run_id, partial=True
            )
        # 충돌 보고가 재개 sweep을 멈추면 보고의 존재 이유가 사라진다.
        except Exception as exc:
            self._record_update_failure(None, exc)

    def _notify_operator_chats(
        self,
        *,
        run_id: str,
        approval_id: str,
        event_type: str,
        key_prefix: str,
        text: str,
        extra: Mapping[str, Any] | None = None,
        subject_field: str = "approval_id",
        reply_markup: Mapping[str, Any] | None = None,
    ) -> None:
        """운영자 채팅에 승인 관련 알림을 채팅 단위로 1회씩 보낸다.

        전송·기록을 채팅마다 독립적으로 처리한다. 한 채팅이 영구히 전송 불가여도
        (a) 나머지 채팅은 알림을 받고, (b) 호출한 sweep이 중단되지 않는다. 재개
        루프는 오래된 승인부터 도는데, 여기서 예외가 새어 나가면 그 뒤의 모든
        승인이 매 poll마다 조용히 재개되지 못한다.

        성공한 채팅은 자기 duplicate_key를 남기므로 재시도 때 다시 받지 않는다.
        리마인더 sweep이 쓰는 규약과 같다.
        """
        for chat_id in self.config.approval.telegram_allowed_chat_ids:
            duplicate_key = f"{key_prefix}:{approval_id}:{chat_id}"
            try:
                if self.store.duplicate_key_exists(duplicate_key):
                    continue
                self._send(int(chat_id), text, reply_markup=reply_markup)
                save_audited_system_event(
                    self.store,
                    self.audit,
                    run_id or f"run_{approval_id}",
                    event_type,
                    {
                        # A dispatch notice is about a signal run, not an
                        # approval; writing it under approval_id would put a
                        # signal_run_id in a column the card sweep reads.
                        subject_field: approval_id,
                        "chat_id": int(chat_id),
                        **(dict(extra) if extra else {}),
                        "duplicate_key": duplicate_key,
                    },
                )
            # 채팅 하나의 실패가 나머지 채팅과 상위 sweep을 멈추면 안 된다.
            except Exception as exc:
                self._record_update_failure(None, exc)

    def _notify_approval_needs_attention(
        self,
        approval_id: str,
        run_id: str,
        *,
        partial: bool,
    ) -> None:
        """partial=True면 브로커에 주문이 나갔을 수 있다는 뜻이다."""
        self._notify_operator_chats(
            run_id=run_id,
            approval_id=approval_id,
            event_type="telegram_approval_needs_attention",
            key_prefix="telegram-approval-attention",
            text=(
                ui_catalog.APPROVAL_NEEDS_RECONCILIATION
                if partial
                else ui_catalog.APPROVAL_NEEDS_ATTENTION
            ),
            extra={"partial_execution_possible": partial},
        )

    def _deliver_resume_completion_notices(self) -> None:
        """재개 완료 통지를 telegram_approval_resolution_completed를 outbox로 삼아 보낸다.

        재개 성공은 실제 브로커 주문을 뜻한다. 운영자가 마지막으로 본 메시지는
        "처리하지 못했어요"이므로, 알리지 않으면 같은 주문을 증권사 앱에서 손으로
        다시 낼 수 있다.

        재개 루프 안에서 한 번만 보내면 그 통지는 내구적이지 않다 — 전송이 실패한
        순간 승인은 이미 종결(resolution_completed)이라 다음 sweep이 다시 보지
        않기 때문이다. 게다가 실패는 크래시가 아니라 _notify_operator_chats가
        의도적으로 삼키는 정상 경로(텔레그램 장애, allowed_chat_ids 중 하나가
        잘못됨)다. 그래서 통지 여부를 내구 기록에서 매 sweep 다시 판정하고,
        자기 채팅별 키가 없는 채팅에만 재시도한다.

        attempt == 1(대화형 콜백)은 제외한다 — 방금 버튼을 누른 운영자가 그 자리에서
        같은 문구를 응답으로 받는다.
        """
        for row in self.store.list_system_events_by_type(
            "telegram_approval_resolution_completed",
            limit=None,
        ):
            payload = row["payload"]
            # 한 건의 손상된 payload가 나머지 통지를 막으면 안 된다.
            try:
                if payload.get("settled_by"):
                    # An operator settlement writes this same event but ran no
                    # orders. Announcing it would tell the operator the system
                    # handled what they just closed by hand, and they may then
                    # skip the replacement trade they settled it in order to do.
                    continue
                if int(payload.get("attempt") or 1) < 2:
                    continue
                approval_id = str(payload.get("approval_id"))
                status = str(payload.get("status"))
                orders_submitted = int(payload.get("orders_submitted") or 0)
                orders_failed = int(payload.get("orders_failed") or 0)
                self._notify_operator_chats(
                    run_id=str(row.get("run_id") or ""),
                    approval_id=approval_id,
                    event_type="telegram_approval_resume_notice",
                    key_prefix="telegram-approval-resume-notice",
                    text=approval_decision_text(
                        status,
                        approval_id,
                        orders_submitted=orders_submitted,
                        orders_failed=orders_failed,
                    ),
                    extra={
                        "status": status,
                        "orders_submitted": orders_submitted,
                        "orders_failed": orders_failed,
                    },
                )
            except Exception as exc:
                self._record_update_failure(None, exc)

    def _resume_incomplete_dispatches(self) -> None:
        """Finish a dispatch that was consumed but never reported settling.

        The orchestrator refuses to re-enter a settled package, so this only
        ever reaches runs whose approvals are genuinely incomplete. Each group
        is filed under a stable key, so re-entering adopts the approvals
        already created and makes only the missing ones.

        The attempt budget is what keeps a run that cannot be dispatched --
        a stale broker snapshot, a config that no longer loads -- from
        retrying on every poll forever. When it runs out the run goes to the
        operator instead, once.

        A durable manifest is a prerequisite for entering any of this; see the
        loop below.
        """
        if self.approval_config_path is None:
            return
        for signal_run_id in self.store.list_incomplete_signal_dispatches():
            if not has_dispatch_manifest(self.store, signal_run_id):
                # Pre-manifest history. The manifest is the only durable record
                # of which groups this dispatch was obligated to resolve;
                # without one, re-entering dispatch would rebuild that intent
                # from today's strategy output, capacity, buying power,
                # portfolio and approval grouping, and could place a set of
                # orders the run never decided on. Nor does a missing manifest
                # prove nothing went out. Neither replay nor "assume finished"
                # is safe, so it goes to a person.
                #
                # Checked before the attempt budget on purpose: this run is not
                # failing, it is refused, and spending attempts on a stable
                # refusal would eventually make it look merely exhausted.
                self._notify_dispatch_needs_attention(signal_run_id)
                continue
            attempt = self._next_dispatch_resume_attempt(signal_run_id)
            if attempt > _MAX_DISPATCH_RESUME_ATTEMPT:
                self._notify_dispatch_needs_attention(signal_run_id)
                continue
            if not self._claim_dispatch_resume(signal_run_id, attempt):
                continue
            outcome = "failed"
            try:
                self._run_dispatch(signal_run_id)
                outcome = "completed"
            # One stuck run must not starve the others, and the failure is
            # recorded either way so the budget advances.
            except Exception as exc:
                self._record_update_failure(None, exc)
            finally:
                self._record_dispatch_resume_finished(signal_run_id, attempt, outcome)

    def _run_dispatch(self, signal_run_id: str) -> None:
        """Seam: the orchestrator call the resume sweep drives."""
        approval_config, approval_identity = load_config_with_identity(
            self.approval_config_path
        )
        MaestroOrchestrator(
            approval_config,
            telegram_client=self.client,
            config_identity=approval_identity,
        ).dispatch_signal_approval(signal_run_id)

    def _dispatch_resume_finished_attempts(self, signal_run_id: str) -> list[int]:
        return [
            int(row["payload"].get("attempt", 0))
            for row in self.store.list_system_events_by_type(
                "telegram_dispatch_resume_finished", limit=None
            )
            if str(row["payload"].get("signal_run_id")) == signal_run_id
        ]

    def _next_dispatch_resume_attempt(self, signal_run_id: str) -> int:
        return len(self._dispatch_resume_finished_attempts(signal_run_id)) + 1

    def _claim_dispatch_resume(self, signal_run_id: str, attempt: int) -> bool:
        duplicate_key = f"telegram-dispatch-resume:{signal_run_id}:a{attempt}"
        with self.store.writer_lock("telegram_dispatch_resume_claim"):
            if self.store.duplicate_key_exists(duplicate_key):
                return False
            save_audited_system_event(
                self.store,
                self.audit,
                signal_run_id,
                "telegram_dispatch_resume_claim",
                {
                    "signal_run_id": signal_run_id,
                    "attempt": attempt,
                    "claimed_at": utc_now().isoformat(),
                    "duplicate_key": duplicate_key,
                },
            )
        return True

    def _record_dispatch_resume_finished(
        self, signal_run_id: str, attempt: int, outcome: str
    ) -> None:
        duplicate_key = f"telegram-dispatch-resume-finished:{signal_run_id}:a{attempt}"
        with self.store.writer_lock("telegram_dispatch_resume_finished"):
            if self.store.duplicate_key_exists(duplicate_key):
                return
            save_audited_system_event(
                self.store,
                self.audit,
                signal_run_id,
                "telegram_dispatch_resume_finished",
                {
                    "signal_run_id": signal_run_id,
                    "attempt": attempt,
                    "outcome": outcome,
                    "finished_at": utc_now().isoformat(),
                    "duplicate_key": duplicate_key,
                },
            )

    def _notify_dispatch_needs_attention(self, signal_run_id: str) -> None:
        self._notify_operator_chats(
            run_id=signal_run_id,
            approval_id=signal_run_id,
            event_type="telegram_dispatch_needs_attention",
            key_prefix="telegram-dispatch-attention",
            text=ui_catalog.APPROVAL_NEEDS_ATTENTION,
            subject_field="signal_run_id",
        )

    def _resume_unresolved_approvals(self) -> None:
        """결정은 기록됐지만 집행이 끝나지 않은 승인을 기록된 결정으로 재개한다."""
        self._reclaim_abandoned_resume_claims()
        completed = {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resolution_completed",
                limit=None,
            )
        }
        envelopes = {
            str(row["payload"].get("approval_id")): row["payload"]
            for row in self.store.list_system_events_by_type(
                "telegram_approval_pending",
                limit=None,
            )
        }
        for row in reversed(
            self.store.list_system_events_by_type("telegram_approval_ack", limit=None)
        ):
            ack = row["payload"]
            approval_id = str(ack.get("approval_id"))
            if not isinstance(ack.get("schema_version"), int) or approval_id in completed:
                continue
            run_id = str(row.get("run_id") or "")
            # 승인 하나의 실패가 그 뒤의 모든 승인을 굶기면 안 된다. 이 루프는
            # 오래된 것부터 돌고, 시간 창을 없앤 뒤로는 과거 envelope 전부가 매
            # poll마다 다시 검증된다 — system_events.payload에는 스키마 제약이
            # 없으므로 필드가 빠진 옛 행 하나가 영구 정지를 만들 수 있다.
            try:
                self._resume_one_approval(ack, envelopes.get(approval_id), run_id)
            except Exception as exc:
                self._quarantine_approval(approval_id, run_id, exc)
        self._deliver_resume_completion_notices()

    def _resume_one_approval(
        self,
        ack: Mapping[str, Any],
        payload: Mapping[str, Any] | None,
        run_id: str,
    ) -> None:
        """승인 한 건의 재개. 호출자가 예외를 격리하므로 여기서는 그대로 던진다."""
        approval_id = str(ack.get("approval_id"))
        if payload is None:
            # envelope이 없으면 재개할 재료가 없다. 조용히 건너뛰면 이 승인은
            # 영원히 미완으로 남아 롤백 preflight를 계속 막는다 — 운영자에게
            # 넘긴다. 주문이 나갔는지는 approvals 행으로만 짐작할 수 있다.
            self._notify_approval_needs_attention(
                approval_id,
                run_id,
                partial=self.store.approval_exists(approval_id),
            )
            return
        envelope = PendingApprovalEnvelope.model_validate(payload)
        if self.store.approval_exists(approval_id):
            # approvals 행은 resolve_pending_signal_approval이 브로커 호출보다
            # 먼저 쓴다. 행이 있으면 집행에 진입했을 수 있으므로 자동 재개하지
            # 않는다 — 브로커 제출 직후·로컬 lifecycle 기록 전에 중단된 창까지
            # fail-closed로 덮는다. 주문이 이미 나갔을 수 있으므로 브로커 대조
            # 문구로 알린다.
            #
            # 이 확인은 TOCTOU다 — 다른 프로세스의 attempt가 ack와 save_approval
            # 사이에 있으면 여기서 False로 보인다. 그래도 중복 주문은 나가지
            # 않는다. 2차 방어선이 orchestrator 쪽에 있다:
            # resolve_pending_signal_approval은 live_order_lock(프로세스 간
            # flock, store.py:342) 안에서 save_approval(orchestrator.py:332)을
            # 먼저 부르고, StateStore.save_approval(store.py:1139)은 중복
            # approval_id에 ValueError를 던진다. 주문 제출
            # (_execute_live_approval_orders, orchestrator.py:341)은 그 뒤에
            # 있으므로, 뒤늦게 락을 잡은 시도는 아무것도 제출하지 못하고
            # 실패한다. 그 실패는 resume_finished outcome="failed"로 남고,
            # 다음 poll에는 approvals 행이 보여 이 분기로 들어온다.
            self._notify_approval_needs_attention(
                envelope.approval_id, envelope.run_id, partial=True
            )
            return
        # 최초 콜백(attempt 1)도 실제 집행 시도였으므로 예산에 함께 센다.
        if self._executed_resume_attempts(approval_id) + 1 >= _MAX_RESUME_ATTEMPT:
            # 반복 실패는 자동 재시도로 풀리지 않는다. 매 poll마다 조용히
            # 실패를 쌓는 대신 운영자에게 넘긴다.
            # abandoned(한 번도 실행되지 않은 시도)는 이 예산을 깎지 않는다.
            self._notify_approval_needs_attention(
                envelope.approval_id, envelope.run_id, partial=False
            )
            return
        attempt = self._next_resume_attempt(approval_id)
        if not self._claim_resume(envelope, attempt):
            return  # 같은 attempt가 in-flight다
        outcome = "failed"
        try:
            self._resolve_async_approval(
                envelope,
                status=str(ack.get("status")),
                decided_by=str(ack.get("decided_by")),
                reason="Resumed from recorded approval decision.",
                attempt=attempt,
            )
            outcome = "completed"
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            self._record_update_failure(None, exc)
        finally:
            # 종료 기록이 있어야 다음 attempt 번호가 진행된다.
            self._record_resume_finished(
                run_id=envelope.run_id,
                approval_id=approval_id,
                attempt=attempt,
                outcome=outcome,
            )
        # 완료 통지는 여기서 보내지 않는다. resolution_completed를 outbox로 삼는
        # _deliver_resume_completion_notices가 sweep 끝에서 보낸다 — 전송 실패가
        # 통지를 영영 잃어버리지 않게 하기 위함이다.

    def _quarantine_approval(self, approval_id: str, run_id: str, exc: Exception) -> None:
        """재개할 수 없는 승인을 내구 기록으로 격리하고 운영자에게 넘긴다.

        조용히 건너뛰면 같은 행이 매 poll마다 같은 예외를 내고, 아무도 모르는 채
        그 승인이 롤백 preflight를 영구히 막는다. 기록은 approval당 1회이며,
        재개 자체를 막지는 않는다 — 원인이 일시적이었다면 다음 poll에 그대로
        재개된다.
        """
        try:
            duplicate_key = f"telegram-approval-quarantine:{approval_id}"
            with self.store.writer_lock("telegram_approval_resume_quarantined"):
                if not self.store.duplicate_key_exists(duplicate_key):
                    save_audited_system_event(
                        self.store,
                        self.audit,
                        run_id or f"run_{approval_id}",
                        "telegram_approval_resume_quarantined",
                        {
                            "approval_id": approval_id,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                            "duplicate_key": duplicate_key,
                        },
                    )
            self._notify_approval_needs_attention(
                approval_id, run_id, partial=self.store.approval_exists(approval_id)
            )
        # 격리 자체가 sweep을 멈추면 격리의 존재 이유가 사라진다.
        except Exception as quarantine_exc:
            self._record_update_failure(None, quarantine_exc)

    def _reclaim_abandoned_resume_claims(self) -> None:
        """프로세스가 claim 직후 죽으면 그 attempt는 영원히 종료되지 않는다.
        lease가 지난 미완 claim을 abandoned로 종결해 재개가 이어지게 한다."""
        finished = {
            (str(row["payload"].get("approval_id")), int(row["payload"].get("attempt", 0)))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resume_finished",
                limit=None,
            )
        }
        now = utc_now()
        for row in self.store.list_system_events_by_type(
            "telegram_approval_resume_claim",
            limit=None,
        ):
            payload = row["payload"]
            approval_id = str(payload.get("approval_id"))
            attempt = int(payload.get("attempt", 0))
            if (approval_id, attempt) in finished:
                continue
            claimed_at = payload.get("claimed_at")
            if not claimed_at:
                continue
            age = (now - datetime.fromisoformat(str(claimed_at))).total_seconds()
            if age < _RESUME_LEASE_SECONDS:
                continue
            self._record_resume_finished(
                run_id=str(payload.get("run_id") or ""),
                approval_id=approval_id,
                attempt=attempt,
                outcome="abandoned",
            )

    def _resume_finished_events(self, approval_id: str) -> list[dict[str, Any]]:
        return [
            row["payload"]
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resume_finished",
                limit=None,
            )
            if str(row["payload"].get("approval_id")) == approval_id
        ]

    def _next_resume_attempt(self, approval_id: str) -> int:
        """종료 기록 기준으로 번호를 매긴다. in-flight attempt가 있으면 같은
        번호가 다시 나오고, claim duplicate_key 충돌로 병행 진입이 막힌다.

        abandoned도 번호는 증가시킨다 — 안 그러면 회수된 claim과 같은 번호가
        다시 나와 duplicate_key 충돌로 영구 정지한다.
        """
        return len(self._resume_finished_events(approval_id)) + 2  # 최초 콜백이 attempt 1

    def _executed_resume_attempts(self, approval_id: str) -> int:
        """실제로 집행을 시도한 재개 횟수. 재시도 예산은 이 값으로만 판정한다 —
        abandoned(한 번도 실행되지 않고 회수된 claim)는 세지 않는다."""
        return sum(
            1
            for payload in self._resume_finished_events(approval_id)
            if payload.get("outcome") in {"completed", "failed"}
        )

    def _claim_resume(self, envelope: PendingApprovalEnvelope, attempt: int) -> bool:
        """approval당 단일 in-flight를 duplicate_key UNIQUE 제약으로 보장한다."""
        duplicate_key = f"telegram-approval-resume:{envelope.approval_id}:a{attempt}"
        with self.store.writer_lock("telegram_approval_resume_claim"):
            if self.store.duplicate_key_exists(duplicate_key):
                return False
            save_audited_system_event(
                self.store,
                self.audit,
                envelope.run_id,
                "telegram_approval_resume_claim",
                {
                    "approval_id": envelope.approval_id,
                    "run_id": envelope.run_id,
                    "attempt": attempt,
                    "claimed_at": utc_now().isoformat(),
                    "duplicate_key": duplicate_key,
                },
            )
        return True

    def _record_resume_finished(
        self,
        *,
        run_id: str,
        approval_id: str,
        attempt: int,
        outcome: str,
    ) -> None:
        """성공·실패·abandoned 세 경로가 함께 쓰는 종료 기록.

        lease 회수는 여러 poller가 동시에 같은 (approval, attempt)를 종결하려 할
        수 있으므로 claim과 같은 writer_lock 안에서 확인 후 쓴다.
        """
        duplicate_key = f"telegram-approval-resume-finished:{approval_id}:a{attempt}"
        with self.store.writer_lock("telegram_approval_resume_finished"):
            if self.store.duplicate_key_exists(duplicate_key):
                return
            save_audited_system_event(
                self.store,
                self.audit,
                run_id or f"run_{approval_id}",
                "telegram_approval_resume_finished",
                {
                    "approval_id": approval_id,
                    "attempt": attempt,
                    "outcome": outcome,
                    "finished_at": utc_now().isoformat(),
                    "duplicate_key": duplicate_key,
                },
            )

    def _notify_legacy_unresolved_approvals(self) -> None:
        """3a 이전 ack 중 완료 기록이 없는 건을 1회만 알린다. 자동 재집행은 하지 않는다.

        approvals 행 유무는 침묵의 근거가 아니라 문구를 가르는 기준이다 — 행이
        있는데 완료가 없으면 브로커 제출 중·직후에 중단된 가장 위험한 상태다.
        """
        completed = self._completed_legacy_approval_ids()
        envelopes = {
            str(row["payload"].get("approval_id")): row["payload"]
            for row in self.store.list_system_events_by_type(
                "telegram_approval_pending",
                limit=None,
            )
        }
        for row in self.store.list_system_events_by_type(
            "telegram_approval_ack",
            limit=None,
        ):
            ack = row["payload"]
            if isinstance(ack.get("schema_version"), int):
                continue  # 신규 스키마는 재개 경로가 처리한다
            approval_id = str(ack.get("approval_id"))
            payload = envelopes.get(approval_id)
            if payload is None or approval_id in completed:
                continue  # envelope 없음 또는 정상 종결
            envelope = PendingApprovalEnvelope.model_validate(payload)
            self._notify_approval_needs_attention(
                envelope.approval_id,
                envelope.run_id,
                partial=self.store.approval_exists(approval_id),
            )

    def _completed_legacy_approval_ids(self) -> set[str]:
        """See ``upgrade_backfill.completed_legacy_approval_ids``.

        Delegated rather than duplicated: the upgrade backfill classifies the
        same legacy acks by the same conservative rule, and two copies of it
        would drift -- invisibly, until one of them called a half-finished
        approval done.
        """
        return completed_legacy_approval_ids(self.store)

    def _sweep_pending_approvals(self) -> None:
        self._resume_incomplete_dispatches()
        self._resume_unresolved_approvals()
        self._notify_legacy_unresolved_approvals()
        acked = self._terminal_approval_ids()
        # 결정이 기록된 승인은 재개 경로가 단독으로 소유한다. 만료 재판정도
        # 리마인더도 보내지 않는다 — ack duplicate_key 때문에 만료 재판정은
        # 어차피 매 poll마다 ValueError로 조용히 실패하기만 했다.
        decided = self._decided_approval_ids()
        # 전송 완료는 chat_id 단위로 추적한다. 일부 채팅만 실패했을 때 성공한
        # 채팅에 같은 리마인더를 다시 보내지 않기 위함.
        # chat_id가 없는 과거 이벤트는 전체 채팅 완료로 간주한다 (하위 호환).
        reminders: set[tuple[str, int]] = set()
        chat_reminders: set[tuple[str, int, int]] = set()
        for row in self.store.list_system_events_by_type(
            "telegram_approval_reminder",
            limit=5000,
        ):
            payload = row["payload"]
            key = (
                str(payload.get("approval_id")),
                int(payload.get("reminder_seconds", 0)),
            )
            chat_id = payload.get("chat_id")
            if isinstance(chat_id, int):
                chat_reminders.add((*key, chat_id))
            else:
                reminders.add(key)
        now = utc_now()
        for row in reversed(
            # 창을 두지 않는다 — 결정이 한 번도 없던 승인이 창 밖으로 밀리면
            # 영원히 만료되지 않고 리마인더도 멈춘다.
            self.store.list_system_events_by_type(
                "telegram_approval_pending",
                limit=None,
            )
        ):
            payload = row["payload"]
            approval_id = str(payload.get("approval_id") or "")
            if not approval_id or approval_id in acked or approval_id in decided:
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
                    if (*key, chat_id) in chat_reminders:
                        continue
                    try:
                        self._send(
                            chat_id,
                            approval_reminder_text(reminder_seconds // 60, envelope.message),
                            reply_markup=approval_markup(envelope.approval_id, expanded=False),
                        )
                    except (RuntimeError, TimeoutError, ValueError) as exc:
                        # 리마인더 하나가 실패해도 만료 처리·콜백 폴링까지 막지 않는다.
                        # 해당 채팅만 완료로 기록하지 않아 다음 poll에서 재시도한다.
                        self._record_update_failure(None, exc)
                        continue
                    save_audited_system_event(
                        self.store,
                        self.audit,
                        envelope.run_id,
                        "telegram_approval_reminder",
                        {
                            "approval_id": envelope.approval_id,
                            "reminder_seconds": reminder_seconds,
                            "chat_id": chat_id,
                            "sent_at": now.isoformat(),
                            "duplicate_key": (
                                "telegram-approval-reminder:"
                                f"{envelope.approval_id}:{reminder_seconds}:{chat_id}"
                            ),
                        },
                    )
                    chat_reminders.add((*key, chat_id))

    def _sweep_incomplete_workflows(self) -> None:
        """중단된 funding/budget 전이를 운영자에게 노출한다. 재실행하지 않는다.

        claim만 있고 completion이 없는 상태를 sweep이 자동으로 이어 달리면,
        아직 살아 있을 수도 있는 이전 attempt의 run_signal()과 겹쳐 중복
        부작용(이중 신호, 이중 현금흐름 기록)을 낸다. 그래서 이 메서드는
        오직 [재개] 버튼을 위한 카드를 보낼 뿐이고, 진입은
        ``_process_workflow_resume_callback``을 통한 명시적 attempt+1
        커밋으로만 일어난다. 알림은 attempt당 채팅 하나에 정확히 한 번만
        나가도록 ``_notify_operator_chats``의 채팅별 duplicate_key에 맡긴다
        -- 새 attempt(재개 실패 후 다시 stall)는 새 키이므로 다시 알린다.
        """
        for row in list_incomplete_workflows(self.store):
            if int(row["attempt"]) >= _MAX_WORKFLOW_RESUME_ATTEMPT:
                self._notify_workflow_needs_attention(row)
                continue
            self._notify_operator_chats(
                run_id=f"run_{row['request_id']}",
                approval_id=f"{row['phase']}:{row['request_id']}:a{row['attempt']}",
                event_type="funding_workflow_stalled_notice",
                key_prefix="funding-workflow-stalled",
                subject_field="workflow_key",
                text="\n".join(
                    [
                        "A previous step didn't finish.",
                        f"request_id: {row['request_id']} ({row['phase']})",
                        "Tap Resume below to continue.",
                    ]
                ),
                extra={
                    "workflow_id": row["workflow_id"],
                    "request_id": row["request_id"],
                    "phase": row["phase"],
                    "attempt": row["attempt"],
                },
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Resume",
                                "callback_data": (
                                    f"operator:wfresume:{row['phase']}:{row['request_id']}"
                                ),
                            }
                        ]
                    ]
                },
            )

    def _notify_workflow_needs_attention(self, row: Mapping[str, Any]) -> None:
        """Stop offering a Resume that keeps failing, and say so once.

        The Resume card is not free to re-offer. Every tap commits a new
        attempt, and the stall notice is deduplicated per attempt -- so an
        attempt that always fails the same way (an undeliverable card, a
        config that no longer loads) produces a fresh card after every tap,
        forever. Past the budget the transition needs a person, not another
        identical tap.

        Deduplicated per (phase, request) with no attempt in the key, unlike
        the stall notice: this one is about the request having exhausted its
        budget, which does not become newly true on the next attempt.
        """
        self._notify_operator_chats(
            run_id=f"run_{row['request_id']}",
            approval_id=f"{row['phase']}:{row['request_id']}",
            event_type="funding_workflow_needs_attention",
            key_prefix="funding-workflow-attention",
            subject_field="workflow_key",
            text=ui_catalog.WORKFLOW_NEEDS_ATTENTION_TEMPLATE.format(
                request_id=row["request_id"], phase=row["phase"]
            ),
            extra={
                "workflow_id": row["workflow_id"],
                "request_id": row["request_id"],
                "phase": row["phase"],
                "attempt": row["attempt"],
            },
        )

    def _converge_workflow_invariants(self) -> None:
        """orphan 요청과 dangling head를 수렴시킨다 (Task 11의 backstop).

        atomic publish가 정상 경로에서 이 상태들을 막지만, 마이그레이션
        중단이나 수동 복구는 그 밖에서 일어날 수 있다. cutoff가 없으면
        (3a-5가 아직 배포되지 않았으면) 아무것도 하지 않는다 -- 3a 이전
        요청은 head가 원래 없었으므로 orphan으로 오판하면 안 된다.
        """
        converge_workflow_invariants(self.store, cutoff=load_migration_cutoff(self.store))

    def _sweep_lifecycle_cards(self) -> None:
        """승인 카드를 현재 단계로 맞춘다. 단계가 그대로면 아무것도 보내지 않는다.

        기존 알림 경로와 **병행**해서 돈다. 카드 전달이 프로덕션에서 증명되기
        전에 구 경로를 떼는 것은 단계 5다.
        """
        # 종결 표시를 먼저 걷어낸다. attention은 종점이 아니므로, 완료된 지
        # 한참 지난 회전에 복구가 붙으면 그 run은 다시 스캔에 들어와야 한다.
        # 되살릴 표시가 하나도 없으면 복구 미리보기조차 부르지 않는다.
        blocked_order_ids: set[str] | None = None
        if self.store.has_settled_signal_runs():
            blocked_order_ids = self._unresolved_recovery_order_ids()
            self.store.reopen_settled_signal_runs(blocked_order_ids)

        # 창을 두지 않는다 — 오래 기다린 승인이 밀려나면 그 카드는 영영
        # 갱신되지 않는다. 대신 끝난 run을 SQL에서 빼서, 매 poll 하는 일이
        # 운영 기간이 아니라 실제로 열려 있는 승인에 비례하게 만든다.
        rows = self.store.list_unsettled_pending_approvals()
        if not rows:
            # 여기서 돌아가는 것이 중요하다. 아래의 이력 조회는 전부 열린
            # 승인에 대한 것이므로, 열린 승인이 없으면 읽을 것도 없다.
            return
        envelopes = [
            (int(row["id"]), PendingApprovalEnvelope.model_validate(row["payload"]))
            for row in rows
        ]
        # 이력 조회는 전부 열려 있는 승인으로 한정한다. 전체를 읽어 접으면
        # 종결 인덱스로 줄인 비용이 그대로 돌아온다.
        open_approval_ids = {envelope.approval_id for _, envelope in envelopes}
        acks = self.store.latest_payloads_by_approval_id(
            "telegram_approval_ack", open_approval_ids
        )
        completions = self.store.latest_payloads_by_approval_id(
            "signal_approval_completed", open_approval_ids
        )
        if blocked_order_ids is None:
            blocked_order_ids = self._unresolved_recovery_order_ids()
        # 완료가 뒤따른 실패는 카드를 붙잡지 않는다 — 재개가 성공하면 완료가
        # 남는다. 되살리기 쪽 판정은 SQL에 같은 규칙으로 들어가 있다.
        unresolved_failures = (
            self._resolution_failed_approval_ids(open_approval_ids) - completions.keys()
        )

        stages: dict[str, str] = {}
        groups: dict[str, list[PendingApprovalEnvelope]] = defaultdict(list)
        run_settled: dict[str, bool] = {}
        run_max_event_id: dict[str, int] = {}
        run_approval_ids: dict[str, set[str]] = defaultdict(set)
        run_order_ids: dict[str, set[str]] = defaultdict(set)
        for event_id, envelope in envelopes:
            approval_id = envelope.approval_id
            signal_run_id = envelope.signal_run_id
            card_key = f"approval:{approval_id}"
            run_settled.setdefault(signal_run_id, True)
            run_max_event_id[signal_run_id] = max(
                run_max_event_id.get(signal_run_id, 0), event_id
            )
            run_approval_ids[signal_run_id].add(approval_id)
            run_order_ids[signal_run_id] |= self._envelope_order_ids(envelope)
            copies = self._card_manager.copies(card_key)
            if not copies and envelope.card_delivery_version < 1:
                # 카드 이관 이전에 dispatch된 승인이다. 그때는 카드를 직접
                # 전송하고 message_id를 남기지 않았으므로, 여기서 새로 보내면
                # 버튼 달린 카드가 두 장이 된다 — 갱신되는 쪽은 하나뿐이고
                # 낡은 쪽은 영원히 "승인 대기"로 남는다. 그 승인은 병행 유지
                # 중인 구 알림 경로가 계속 책임진다. 부모 카드 집계에도 넣지
                # 않는다 — 단계를 모르는 그룹이 남으면 sweep 전체가 죽는다.
                # 이 승인에는 할 일이 없으므로 run의 종결 여부도 바꾸지 않는다.
                continue
            # version 1인데 투영이 비었다면 전송이 시작되지도 못한 것이다
            # (deliver는 첫 API 호출 **전에** intent를 남긴다). 그대로 두면
            # 승인 요청이 운영자에게 영영 닿지 않으므로 아래 refresh가 최초
            # 전송을 대신한다.
            groups[envelope.signal_run_id].append(envelope)
            stage = card_stage(
                keep_forward_progress(
                    _shown_progress(copies),
                    approval_progress(acks.get(approval_id), completions.get(approval_id)),
                ),
                approval_needs_attention(
                    acks.get(approval_id),
                    completions.get(approval_id),
                    unresolved_recovery=bool(
                        blocked_order_ids & self._envelope_order_ids(envelope)
                    ),
                    unresolved_failure=approval_id in unresolved_failures,
                ),
            )
            stages[approval_id] = stage
            if self._card_is_settled(copies, card_key, stage):
                continue
            run_settled[signal_run_id] = False
            self._refresh_card(
                envelope.run_id,
                card_key,
                stage,
                lambda envelope=envelope, stage=stage: render_approval_stage_card(
                    envelope.request, stage
                ),
            )

        for signal_run_id, group in groups.items():
            # 승인 그룹이 하나뿐이면 부모 카드는 같은 말을 두 번 하는 것뿐이다.
            if len(group) < 2:
                continue
            card_key = f"daily:{signal_run_id}"
            group_stages = [stages[envelope.approval_id] for envelope in group]
            stage = _daily_card_stage(group_stages)
            if self._card_is_settled(self._card_manager.copies(card_key), card_key, stage):
                continue
            run_settled[signal_run_id] = False
            self._refresh_card(
                group[0].run_id,
                card_key,
                stage,
                lambda signal_run_id=signal_run_id, group=group, group_stages=group_stages: (
                    render_daily_card(
                        signal_run_id,
                        [
                            {
                                "label": _telegram_strategy_display_label(
                                    envelope.source_strategy_ids
                                ),
                                "stage": group_stage,
                            }
                            for envelope, group_stage in zip(group, group_stages, strict=True)
                        ],
                    )
                ),
            )

        for signal_run_id, settled in run_settled.items():
            if not settled:
                continue
            # 이 run의 카드는 전부 done이고 전달도 확인됐다. 표시를 남겨 다음
            # poll부터 스캔에서 빠지게 한다 — 되살아나야 할 이유(뒤늦은 승인,
            # 복구, 처리 실패)는 전부 위에서 표시를 걷어내는 쪽으로 처리한다.
            self.store.mark_signal_run_cards_settled(
                signal_run_id,
                approval_ids=sorted(run_approval_ids[signal_run_id]),
                order_ids=sorted(run_order_ids[signal_run_id]),
                max_event_id=run_max_event_id[signal_run_id],
            )

    def _refresh_card(
        self,
        run_id: str,
        card_key: str,
        stage: str,
        render: Callable[[], RenderedCard],
    ) -> None:
        """카드 하나를 갱신한다. 실패해도 나머지 카드는 계속 돈다.

        여기서 예외가 새어 나가면 뒤의 카드가 전부 갱신되지 않고, poll_once가
        예외를 삼키므로 조용히 그렇게 된다.

        **렌더 실패와 refresh 실패를 같은 것으로 다루지 않는다.** 렌더는 아무것도
        보내지 않았음이 확정이므로 전송 거절과 같은 카운터를 태워 fallback으로
        잇는다. 반면 refresh에서 새어 나온 예외는 이미 텔레그램에 카드를 보낸
        뒤일 수 있다(전송은 성공하고 result 기록만 실패한 경우가 그렇다). 그것을
        실패로 덮으면 '전달 여부 불명'이 '전달 안 됨'으로 바뀌고, 다음 sweep이
        재전송을 안전하다고 판단해 버튼 달린 카드가 두 장 생긴다. refresh는
        chat별 실패를 이미 스스로 기록하므로, 여기서는 격리만 하고 delivery
        상태는 건드리지 않는다.
        """
        try:
            rendered = render()
        except Exception as exc:  # noqa: BLE001 - 카드 하나가 나머지를 막지 않는다
            self._log_card_failure(exc)
            self._record_card_render_failure(run_id, card_key, stage, exc)
            return
        try:
            self._card_manager.refresh(run_id, card_key, stage, rendered)
        except Exception as exc:  # noqa: BLE001 - 카드 하나가 나머지를 막지 않는다
            self._log_card_failure(exc)

    def _log_card_failure(self, exc: Exception) -> None:
        """기록 경로 자체가 깨져도 sweep을 멈추지 않는다.

        _record_update_failure는 DB와 감사 로그에 쓴다. 그것이 실패해서 예외가
        새어 나가면 이 카드 이후의 카드가 전부 갱신되지 않는다 — 격리하려고 둔
        코드가 격리를 무너뜨리는 셈이다.
        """
        try:
            self._record_update_failure(None, exc)
        except Exception:  # noqa: BLE001 - 더 알릴 곳이 없다
            pass

    def _record_card_render_failure(
        self, run_id: str, card_key: str, stage: str, exc: Exception
    ) -> None:
        try:
            self._card_manager.record_render_failure(run_id, card_key, stage, str(exc))
        except Exception as record_exc:  # noqa: BLE001 - 저장까지 깨진 경우
            self._log_card_failure(record_exc)

    def _resolution_failed_approval_ids(self, approval_ids: Collection[str]) -> set[str]:
        """집행이 실패로 끝난 승인. 3a-1이 이미 남기고 있던 기록이다.

        완료가 뒤따랐는지는 호출부가 판단한다 — 재개가 성공하면 완료가 남으므로
        과거의 실패가 카드를 붙잡지 않는다.

        묻는 승인으로 한정한다. 전체를 읽으면 이미 끝난 승인의 실패까지 매 poll
        역직렬화하게 되는데, 그 답은 어차피 쓰이지 않는다.
        """
        return set(
            self.store.latest_payloads_by_approval_id(
                "telegram_approval_resolution_failed", list(approval_ids)
            )
        )

    def _unresolved_recovery_order_ids(self) -> set[str]:
        """아직 종결되지 않은 복구 대상 주문. 승인과는 order_id로만 이어진다.

        `live_order_recovery_required` 페이로드에는 approval_id가 없다. 복구가
        완료되면 blocker가 사라지므로 주의 플래그도 그대로 풀린다.
        """
        preview = WorkflowRecoveryService(self.config, self.store, self.audit).preview()
        return {blocker.order_id for blocker in preview.blockers if blocker.order_id}

    @staticmethod
    def _envelope_order_ids(envelope: PendingApprovalEnvelope) -> set[str]:
        # 집행은 envelope.orders를 그대로 OrderIntent로 되살리므로
        # (orchestrator.resolve_pending_signal_approval) order_id가 일치한다.
        return {
            str(order.get("order_id"))
            for order in envelope.orders
            if order.get("order_id") is not None
        }

    def _card_is_settled(
        self,
        copies: Mapping[tuple[str, int], Any],
        card_key: str,
        stage: str,
    ) -> bool:
        """이 카드에 더 할 일이 없는가.

        done은 종점이다. 승인 기록은 계속 쌓이므로 끝난 카드까지 매 poll 다시
        그리면 sweep 비용이 무한히 는다. **attention은 종점이 아니다** — 복구가
        해소되면 풀려야 하므로 계속 다시 판정한다.

        판정은 지금 계산한 단계로 매번 다시 한다. 투영이 done이라는 이유만으로
        건너뛰면 그 뒤에 생긴 복구 건이 카드에 영영 반영되지 않는다.

        기준은 이 카드의 수신자다. 현재 설정된 채팅 전부로 보면, 채팅을 하나
        추가한 순간 과거의 완료 카드가 전부 "미종결"이 되어 다시 전송된다.
        """
        if stage != "done":
            return False
        for chat_id in self._card_manager.audience(card_key, copies):
            copy = copies.get((card_key, chat_id))
            if copy is None or copy.delivery != "confirmed" or copy.stage != "done":
                return False
        return True

    def _sweep_recovery_notifications(self) -> None:
        safety = SafetyControlService(self.store, self.audit).current_state()
        preview = WorkflowRecoveryService(self.config, self.store, self.audit).preview()
        if safety.state not in {SafetyState.HALTED, SafetyState.KILLED} and not preview.blockers:
            return
        notice_key = (
            f"{safety.state.value}:"
            f"{safety.updated_at if safety.state != SafetyState.ACTIVE else ''}:"
            f"{preview.fingerprint}"
        )
        notices = self.store.list_system_events_by_type(
            SystemEventType.TELEGRAM_RECOVERY_NOTICE,
            limit=1000,
        )
        if any(row["payload"].get("notice_key") == notice_key for row in notices):
            return
        text, markup = self._recovery_message()
        for chat_id in self.config.approval.telegram_allowed_chat_ids:
            self._send(int(chat_id), text, reply_markup=markup)
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            SystemEventType.TELEGRAM_RECOVERY_NOTICE,
            {
                "notice_key": notice_key,
                "recovery_fingerprint": preview.fingerprint,
                "recovery_event_ids": [blocker.event_id for blocker in preview.blockers],
                "safety_state": safety.state.value,
                "sent_at": utc_now().isoformat(),
            },
        )

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
        return get_order_buying_power(service.client, config, broker, order)

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
        normalized_price = round_price_to_tick(
            price,
            config.universe.get(order.symbol),
        )
        if normalized_price <= 0:
            raise ValueError(f"latest broker quote is unavailable for {order.symbol}")
        return normalized_price

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

    def _process_cash_drift_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        parts = action.split(":")
        if len(parts) != 6 or parts[1] != "classify":
            self._answer(callback, "This cash-drift action is no longer active.")
            return True
        _, _, account_id, currency, classification, snapshot_id = parts
        classification = _CASH_DRIFT_CLASSIFICATION_TOKENS.get(classification, classification)
        if classification not in CASH_SUSPENSE_CLASSIFICATIONS:
            self._answer(callback, "This cash-drift action is no longer active.")
            return True
        row = next(
            (
                item
                for item in self.store.list_cash_suspense(account_id=account_id)
                if str(item.get("currency") or "").upper() == currency.upper()
            ),
            None,
        )
        if row is None or str(row.get("last_snapshot_id")) != snapshot_id:
            self._answer(callback, "This cash-drift action is stale.")
            self._record("/cash_drift", chat_id, user_id, username, "stale_callback")
            return True
        self.store.classify_cash_suspense(
            account_id=account_id,
            currency=currency,
            classification=classification,
        )
        run_id = new_run_id()
        save_audited_system_event(
            self.store,
            self.audit,
            run_id,
            SystemEventType.CASH_DRIFT_CLASSIFIED,
            {
                "account_id": account_id,
                "currency": currency.upper(),
                "classification": classification,
                "flow_class": flow_class_for_cash_suspense(classification),
                "snapshot_id": int(snapshot_id),
                "decided_at": utc_now().isoformat(),
                "decided_by": username or str(user_id),
                "previous_amount": row.get("amount"),
                "source": "telegram_cash_drift",
            },
        )
        self._answer(callback, "Cash-drift classification recorded.")
        self._edit_callback_message(
            callback,
            "Cash-drift classification recorded\n"
            f"account_id: {_mask_identifier(account_id)}\n"
            f"currency: {currency.upper()}\n"
            f"classification: {classification}\n"
            "Ledger cash unchanged; verify the broker before recording a flow.",
        )
        self._record("/cash_drift", chat_id, user_id, username, "classified")
        return True

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
            len(parts) == 3
            and parts[0] == "cash-flow"
            and transition in {"approve", "ignore", "confirm", "different", "reject"}
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
        is_toss_candidate = proposal.get("source") == "toss_buying_power_cash_flow_candidate"
        is_fx_candidate = proposal.get("source") == "toss_buying_power_fx_conversion_candidate"
        if is_toss_candidate and transition == "different":
            self._answer(callback, "Enter the verified amount with /cash_flow.")
            self._edit_callback_message(
                callback,
                "Cash-flow amount requires confirmation\n"
                f"proposal_id: {proposal_id}\n"
                f"Use /cash_flow {proposal_id} <actual_amount>",
            )
            self._record("/cash-flow_different", chat_id, user_id, username, "amount_required")
            return True
        if (is_toss_candidate or is_fx_candidate) and transition in {"reject", "ignore"}:
            self._save_cash_flow_proposal_ack(proposal_id, "rejected", user_id, username)
            self._answer(callback, "Cash-flow candidate rejected.")
            self._edit_callback_message(callback, "Cash-flow candidate rejected; ledger unchanged.")
            self._record("/cash-flow_reject", chat_id, user_id, username, "rejected")
            return True
        if is_toss_candidate or is_fx_candidate:
            current = CashFlowCandidateDetector(self.store).detect(
                str(proposal.get("account_id") or "")
            )
            if current is None or current.fingerprint != proposal.get("fingerprint"):
                self._answer(callback, "This cash-flow candidate is stale.")
                self._record("/cash-flow", chat_id, user_id, username, "stale_callback")
                return True
            transition = "approve"
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
        effective_at = str(proposal.get("effective_at") or utc_now().isoformat())
        flow_type = str(proposal.get("flow_type") or "deposit")
        account_amount = abs(_float_or_none(proposal.get("amount")) or 0.0)
        if flow_type == "withdrawal":
            account_amount = -account_amount
        account_id = str(proposal.get("account_id") or "")
        self._ensure_account_ledger_for_proposal(proposal)
        if is_fx_candidate:
            if self.store.load_latest_account_portfolio_state(account_id) is None:
                self._answer(callback, "Account cash ledger is not established.")
                self._record("/cash-flow", chat_id, user_id, username, "missing_ledger")
                return True
            if not self._fx_proposal_matches_latest_ledger(proposal):
                self._answer(callback, "This currency-conversion candidate is stale.")
                self._record("/cash-flow", chat_id, user_id, username, "stale_callback")
                return True
            cash_flow = AccountCashFlowService(self.store, self.audit).record_currency_conversion(
                account_id=account_id,
                from_currency=str(proposal.get("from_currency") or ""),
                from_amount=abs(_float_or_none(proposal.get("from_amount")) or 0.0),
                to_currency=str(proposal.get("to_currency") or ""),
                to_amount=abs(_float_or_none(proposal.get("to_amount")) or 0.0),
                transfer_id=f"telegram-fx:{proposal['fingerprint']}",
                effective_at=effective_at,
                source="telegram_toss_fx_conversion_confirmation",
                reason="operator confirmed detected Toss currency conversion",
                decided_by=username or str(user_id),
            )
            if not cash_flow.created:
                self._answer(callback, "This currency conversion was already applied.")
                self._record("/cash-flow", chat_id, user_id, username, "duplicate")
                return True
            self._save_cash_flow_proposal_ack(
                proposal_id,
                "approved",
                user_id,
                username,
                account_cash_flow_id=cash_flow.run_id,
            )
            self._answer(callback, "Currency conversion recorded.")
            self._edit_callback_message(
                callback,
                "Currency conversion recorded\n"
                f"{_money(proposal.get('from_amount'))} {proposal.get('from_currency')} → "
                f"{_money(proposal.get('to_amount'))} {proposal.get('to_currency')}",
            )
            self._record("/cash-flow_approve", chat_id, user_id, username, "approved_fx")
            return True
        if self.store.load_latest_account_portfolio_state(account_id) is not None:
            cash_flow = AccountCashFlowService(self.store, self.audit).record(
                account_id=account_id,
                amount=abs(account_amount),
                currency=str(proposal.get("currency") or "KRW"),
                flow_type=flow_type,
                effective_at=effective_at,
                source=(
                    "telegram_toss_cash_flow_confirmation"
                    if is_toss_candidate
                    else "telegram_cash_flow_confirmation"
                ),
                decided_by=username or str(user_id),
                proposal_id=proposal_id,
                evidence=dict(proposal.get("evidence") or {}),
                verification="operator_verified" if is_toss_candidate else "broker_verified",
                duplicate_key=f"account-cash-flow:proposal:{proposal_id}",
            )
            if not cash_flow.created:
                self._answer(callback, "This cash-flow proposal was already applied.")
                self._record("/cash-flow", chat_id, user_id, username, "duplicate")
                return True
            account_cash_flow_id = cash_flow.run_id
        else:
            # Compatibility for pre-account-ledger strategy-only proposals.
            account_cash_flow_id = new_run_id()
        for allocation in allocations:
            strategy_id = allocation.get("strategy_id")
            strategy_duplicate_key = f"strategy-cash-flow:proposal:{proposal_id}:{strategy_id}"
            if self.store.duplicate_key_exists(strategy_duplicate_key):
                continue
            payload = {
                "strategy_id": strategy_id,
                "account_id": proposal.get("account_id"),
                "execution_sleeve": allocation.get("execution_sleeve"),
                "amount": allocation.get("amount"),
                "currency": proposal.get("currency"),
                "flow_type": flow_type,
                "effective_at": effective_at,
                "source": "telegram_voluntary_deposit_allocation",
                "proposal_id": proposal_id,
                "account_cash_flow_id": account_cash_flow_id,
                "decided_by": username or str(user_id),
                "duplicate_key": strategy_duplicate_key,
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

    def _ensure_account_ledger_for_proposal(self, proposal: Mapping[str, Any]) -> None:
        account_id = str(proposal.get("account_id") or "")
        if not account_id or self.store.load_latest_account_portfolio_state(account_id) is not None:
            return
        previous_snapshot_id = proposal.get("previous_broker_snapshot_id")
        if previous_snapshot_id is None:
            return
        previous = next(
            (
                row
                for row in self.store.list_broker_account_snapshots(limit=1000)
                if row.get("id") == previous_snapshot_id
                and _broker_snapshot_account_id(row) == account_id
            ),
            None,
        )
        account = _mapping(_mapping(previous or {}).get("payload")).get("account")
        if not isinstance(account, Mapping):
            return
        self.store.save_portfolio_snapshot(
            new_run_id(),
            portfolio_state_from_broker_account(
                dict(account),
                allowed_symbols=self.config.portfolio.allowed_symbols,
                universe=self.config.universe,
                unknown_symbol_policy=self.config.portfolio.unknown_broker_position_policy,
            ),
            account_id=account_id,
        )

    def _fx_proposal_matches_latest_ledger(
        self,
        proposal: Mapping[str, Any],
    ) -> bool:
        account_id = str(proposal.get("account_id") or "")
        ledger = self.store.load_latest_account_portfolio_state(account_id)
        snapshots = self.store.list_broker_account_snapshots(
            limit=1,
            account_id=account_id,
        )
        if ledger is None or not snapshots:
            return False
        account = _mapping(_mapping(snapshots[0]).get("payload")).get("account")
        buying_power = _mapping(_mapping(account).get("buying_power_by_currency"))
        from_currency = str(proposal.get("from_currency") or "").upper()
        to_currency = str(proposal.get("to_currency") or "").upper()
        if from_currency not in buying_power or to_currency not in buying_power:
            return False
        from_amount = abs(_float_or_none(proposal.get("from_amount")) or 0.0)
        to_amount = abs(_float_or_none(proposal.get("to_amount")) or 0.0)
        from_after = float(ledger.cash_by_currency.get(from_currency, 0.0)) - from_amount
        to_after = float(ledger.cash_by_currency.get(to_currency, 0.0)) + to_amount
        from_tolerance = 1.0 if from_currency in {"KRW", "JPY"} else 0.01
        to_tolerance = 1.0 if to_currency in {"KRW", "JPY"} else 0.01
        return (
            abs(from_after - float(buying_power[from_currency])) <= from_tolerance
            and abs(to_after - float(buying_power[to_currency])) <= to_tolerance
        )

    def _process_cash_flow_command(
        self,
        text: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> None:
        parts = text.split()
        if len(parts) != 3:
            self._send(chat_id, "Usage: /cash_flow <proposal_id> <actual_amount>")
            self._record("/cash_flow", chat_id, user_id, username, "invalid")
            return
        proposal = self._load_pending_cash_flow_proposal(parts[1])
        amount = _float_or_none(parts[2].replace(",", ""))
        if (
            proposal is None
            or proposal.get("source") != "toss_buying_power_cash_flow_candidate"
            or amount is None
            or amount <= 0
        ):
            self._send(chat_id, "Cash-flow proposal is stale or the amount is invalid.")
            self._record("/cash_flow", chat_id, user_id, username, "invalid")
            return
        current = CashFlowCandidateDetector(self.store).detect(
            str(proposal.get("account_id") or "")
        )
        if current is None or current.fingerprint != proposal.get("fingerprint"):
            self._send(chat_id, "Cash-flow proposal is stale; refresh the account first.")
            self._record("/cash_flow", chat_id, user_id, username, "stale")
            return
        updated = dict(proposal, amount=amount)
        effective_at = str(updated.get("effective_at") or utc_now().isoformat())
        result = AccountCashFlowService(self.store, self.audit).record(
            account_id=str(updated.get("account_id") or ""),
            amount=amount,
            currency=str(updated.get("currency") or "KRW"),
            flow_type=str(updated.get("flow_type") or "deposit"),
            effective_at=effective_at,
            source="telegram_toss_cash_flow_confirmation",
            decided_by=username or str(user_id),
            proposal_id=parts[1],
            evidence={**dict(updated.get("evidence") or {}), "operator_corrected_amount": True},
            verification="operator_verified",
            duplicate_key=f"account-cash-flow:proposal:{parts[1]}",
        )
        self._save_cash_flow_proposal_ack(
            parts[1],
            "confirmed",
            user_id,
            username,
            actual_amount=amount,
            account_cash_flow_id=result.run_id,
        )
        self._send(chat_id, f"Cash flow recorded: {_money(amount)} {updated['currency']}")
        self._record("/cash_flow", chat_id, user_id, username, "confirmed")

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
            try:
                self._cancel_funding_request(request, user_id=user_id, username=username)
            except WorkflowClaimRefused as exc:
                answer_text, status = _funding_claim_refusal_response(exc.reason)
                self._answer(callback, answer_text)
                self._record("/funding_cancel", chat_id, user_id, username, status)
                return True
            except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
                self._answer(callback, "Funding cancellation failed.")
                text = "\n".join(
                    [
                        "Funding cancellation failed",
                        f"request_id: {request_id}",
                        f"message: {exc}",
                    ]
                )
                self._record("/funding_cancel", chat_id, user_id, username, "failed")
                self._edit_callback_message(callback, text)
                return True
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
        except WorkflowClaimRefused as exc:
            # The claim (not run_signal or the ack write) is what refuses this,
            # and exc.reason says which of two very different situations this
            # is: "not_head"/"no_head"/"head_moved" means a newer request
            # replaced this one -- nothing is wrong. "already_claimed" means an
            # earlier attempt on *this* request claimed and never finished
            # (e.g. it died between the claim and the ack, the scenario this
            # retry is walking into): the claim key already exists, so
            # reporting "already processed" here would be false -- the
            # request is stuck, not done, and a human needs to look at it
            # rather than keep tapping the button.
            confirm_text, status = _funding_claim_refusal_response(exc.reason)
            text = "\n".join(
                [
                    confirm_text,
                    f"request_id: {request_id}",
                ]
            )
            self._record("/funding_complete", chat_id, user_id, username, status)
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
            try:
                self._cancel_budget_request(request, user_id=user_id, username=username)
            except WorkflowClaimRefused as exc:
                answer_text, status = _funding_claim_refusal_response(
                    exc.reason, request_noun="budget"
                )
                self._answer(callback, answer_text)
                self._record("/budget_cancel", chat_id, user_id, username, status)
                return True
            except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
                self._answer(callback, "Budget cancellation failed.")
                text = "\n".join(
                    [
                        "Budget cancellation failed",
                        f"request_id: {request_id}",
                        f"message: {exc}",
                    ]
                )
                self._record("/budget_cancel", chat_id, user_id, username, "failed")
                self._edit_callback_message(callback, text)
                return True
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
        except WorkflowClaimRefused as exc:
            # Same distinction the funding path draws: "already_claimed"
            # means an earlier attempt on this request is stuck mid-flight,
            # not superseded -- see _funding_claim_refusal_response.
            confirm_text, status = _funding_claim_refusal_response(
                exc.reason, request_noun="budget"
            )
            text = "\n".join(
                [
                    confirm_text,
                    f"request_id: {request_id}",
                ]
            )
            self._answer(callback, "Budget selection failed.")
            self._record("/budget_select", chat_id, user_id, username, status)
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

    def _process_workflow_resume_callback(
        self,
        callback: Mapping[str, Any],
        action: str,
        chat_id: int,
        user_id: int,
        username: str | None,
    ) -> bool:
        """[재개]는 하나의 콜백 안에서 attempt+1 claim을 커밋한다.

        커밋 자체가 fencing token이다 -- 두 번 눌러도 동시에 들어오는 두
        claim_workflow_attempt 중 하나만 이긴다 (attempt+1의 duplicate_key는
        한 번만 존재할 수 있다). 이 메서드는 그 결과를 각 눌림에 보여줄 뿐,
        스스로 재시도하거나 다른 attempt를 대신 골라주지 않는다.
        """
        parts = action.split(":", 2)
        if len(parts) != 3 or parts[0] != "wfresume" or parts[1] not in PHASES:
            self._answer(callback, "This resume card is no longer active.")
            self._record("/wfresume", chat_id, user_id, username, "stale_callback")
            return True
        phase, request_id = parts[1], parts[2]
        stalled = next(
            (
                row
                for row in list_incomplete_workflows(self.store)
                if row["phase"] == phase and row["request_id"] == request_id
            ),
            None,
        )
        if stalled is None:
            self._answer(callback, "This workflow is no longer stalled.")
            self._record("/wfresume", chat_id, user_id, username, "stale_callback")
            return True
        request = (
            self._load_pending_funding_request(request_id)
            if phase == "funding"
            else self._load_pending_budget_request(request_id)
        )
        if request is None:
            self._answer(callback, "This request is no longer active.")
            self._record("/wfresume", chat_id, user_id, username, "stale_callback")
            return True
        next_attempt = stalled["attempt"] + 1
        if next_attempt > _MAX_WORKFLOW_RESUME_ATTEMPT:
            # An old Resume card is still sitting in the chat after the sweep
            # stopped offering new ones. Without this the operator can keep
            # burning attempts from it, and each one writes a claim that
            # nothing will ever complete.
            self._answer(callback, "This workflow needs manual attention.")
            self._notify_workflow_needs_attention(stalled)
            self._record("/wfresume", chat_id, user_id, username, "resume_budget_exhausted")
            self._edit_callback_message(
                callback,
                "\n".join(
                    [
                        ui_catalog.WORKFLOW_NEEDS_ATTENTION_TEMPLATE.format(
                            request_id=request_id, phase=phase
                        ),
                        f"request_id: {request_id}",
                    ]
                ),
            )
            return True
        intent = str(stalled["intent"])
        self._answer(callback, "Resuming...")
        try:
            # 재개는 phase가 아니라 claim에 기록된 intent로 갈라진다.
            # phase만 보고 confirm으로 이어 달리면, 운영자가 취소한 요청이
            # 재개 한 번으로 이번 달 투자로 뒤집힌다.
            if intent not in {"confirm", "cancel"}:
                raise ValueError(f"stalled workflow has an unknown intent: {intent!r}")
            if intent == "cancel":
                if phase == "funding":
                    self._cancel_funding_request(
                        request,
                        user_id=user_id,
                        username=username,
                        attempt=next_attempt,
                    )
                    text = "Funding request canceled."
                else:
                    self._cancel_budget_request(
                        request,
                        user_id=user_id,
                        username=username,
                        attempt=next_attempt,
                    )
                    text = "Budget request canceled."
            elif phase == "funding":
                text = self._confirm_funding_request(
                    request,
                    chat_id=chat_id,
                    user_id=user_id,
                    username=username,
                    attempt=next_attempt,
                )
            else:
                # A resumed budget confirmation must not ask the operator
                # again -- it replays the amount that was already recorded in
                # the stalled claim. Only a confirm needs one; a cancel never
                # carried an amount and must not be blocked for lacking it.
                selected_budget = stalled.get("selected_budget")
                if selected_budget is None:
                    raise ValueError(
                        "stalled budget workflow has no stored selected_budget"
                    )
                text = self._confirm_budget_request(
                    request,
                    selected_budget=float(selected_budget),
                    chat_id=chat_id,
                    user_id=user_id,
                    username=username,
                    attempt=next_attempt,
                )
        except WorkflowClaimRefused as exc:
            confirm_text, status = _funding_claim_refusal_response(
                exc.reason, request_noun=phase
            )
            text = "\n".join([confirm_text, f"request_id: {request_id}"])
            self._record("/wfresume", chat_id, user_id, username, status)
        except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
            text = "\n".join(
                [
                    "Resume failed",
                    f"request_id: {request_id}",
                    f"message: {exc}",
                ]
            )
            self._record("/wfresume", chat_id, user_id, username, "failed")
        else:
            self._record("/wfresume", chat_id, user_id, username, "resumed")
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
        except WorkflowClaimRefused as exc:
            # WorkflowClaimRefused subclasses RuntimeError, so this branch
            # must come before the generic except below -- otherwise a claim
            # refusal (stuck-in-flight or superseded) is misreported as an
            # invalid amount, same as the callback path this mirrors.
            send_text, status = _funding_claim_refusal_response(exc.reason, request_noun="budget")
            self._send(chat_id, send_text)
            self._record("/budget", chat_id, user_id, username, status)
            return
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
        attempt: int = 1,
    ) -> str:
        """Selecting a budget amount is an input to the transition, not its
        terminal state: it rides in the claim (via ``extra``) and the legacy
        ``contribution_budget_request_decision`` event is written only by
        ``complete_workflow``, in the same atomic batch as
        ``funding_workflow_completed``. If the config load or run_signal()
        below raises, no terminal event exists yet, so the workflow stays
        claimed-but-incomplete -- recoverable, not silently closed.
        """
        if self.signal_config_path is None:
            # Same refusal as _confirm_funding_request, and for the same
            # reason. A selection with no signal config behind it cannot
            # produce the child run the amount exists to feed, so completing
            # the workflow anyway would make the decision terminal -- the one
            # thing this transition is defined not to be -- and would answer
            # "Budget selected" to an operator whose month then quietly never
            # gets invested. Raising before the claim leaves nothing behind
            # to recover, because nothing was entered.
            raise ValueError("Budget selection requires telegram-operator --signal-config")
        validate_selected_budget(request, selected_budget)
        workflow_id = workflow_id_from_request(request)
        request_id = str(request["request_id"])
        # The claim (carrying selected_budget) has to land before any side
        # effect -- the portfolio refresh, run_signal(), the decision -- for
        # the same reason as the funding path: two callbacks racing on the
        # same request must not both regenerate a signal.
        claim = claim_workflow_attempt(
            self.store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="budget",
            attempt=attempt,
            # intent는 재개가 confirm/cancel 중 무엇을 이어 달릴지 가르는
            # 값이고, selected_budget은 그 confirm의 입력이다.
            extra={"selected_budget": selected_budget, "intent": "confirm"},
        )
        if not claim["claimed"]:
            raise WorkflowClaimRefused(str(claim["reason"]))
        lines = [
            "Budget selected",
            f"request_id: {request['request_id']}",
            f"selected_budget: {selected_budget:,.0f} {request.get('currency') or ''}".rstrip(),
        ]
        # Carrying the same request_id into both legs of complete_workflow is
        # what keeps the workflow log and the legacy decision log pointed at
        # the same request; a mismatch here would close one request in one
        # log and a different one in the other.
        legacy_payload = {
            "request_id": request_id,
            "status": "selected",
            "strategy_ids": list(request.get("strategy_ids") or []),
            "contribution_group_id": request.get("contribution_group_id"),
            "account_id": request.get("account_id"),
            "execution_sleeve": request.get("execution_sleeve"),
            "currency": request.get("currency"),
            "month_key": request.get("month_key"),
            "decided_by": username or str(user_id),
            "selected_budget": selected_budget,
        }
        try:
            self._refresh_portfolio_from_broker_snapshot()
        except (RuntimeError, TimeoutError, ValueError):
            if self._has_readonly_broker_accounts():
                raise
        strategy_ids = [str(item) for item in request.get("strategy_ids") or []]
        if not strategy_ids:
            raise ValueError("Budget request is missing strategy_ids")
        # A user-selected budget is an explicit instruction to invest now, so the
        # regenerated signal bypasses the contribution buy_day schedule (the
        # already-executed-this-month guard still applies).
        signal_summary = self._run_child_signal(
            request, workflow_id, attempt=attempt, phase="budget"
        )
        legacy_payload["new_signal_run_id"] = signal_summary.signal_run_id
        lines.append(f"new_signal_run_id: {signal_summary.signal_run_id}")
        lines.append(f"orders_preview_count: {signal_summary.orders_preview_count}")
        if signal_summary.orders_preview_count == 0 and signal_summary.no_order_reasons:
            lines.append("no orders were generated because:")
            lines.extend(f"- {reason}" for reason in signal_summary.no_order_reasons)
        follow_up = self._deliver_child_signal_outcome(chat_id, signal_summary)
        complete_workflow(
            self.store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="budget",
            attempt=attempt,
            legacy_payload=legacy_payload,
        )
        lines.extend(follow_up)
        return "\n".join(lines)

    def _confirm_funding_request(
        self,
        request: dict[str, Any],
        *,
        chat_id: int,
        user_id: int,
        username: str | None,
        attempt: int = 1,
    ) -> str:
        if self.signal_config_path is None:
            raise ValueError("Funding confirmation requires telegram-operator --signal-config")
        workflow_id = workflow_id_from_request(request)
        request_id = str(request["request_id"])
        # The claim has to land before any of this method's side effects --
        # cash-flow recording, run_signal(), the ack -- or two callbacks racing
        # on the same request (a fat-fingered double tap, a retried delivery)
        # would both record cash and both regenerate a signal. Once the claim
        # is committed a duplicate or superseded callback is refused for free,
        # before it can touch anything.
        claim = claim_workflow_attempt(
            self.store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="funding",
            attempt=attempt,
            # 어떤 종단 전이를 요청받았는지를 claim에 남긴다 -- 재개는
            # phase가 아니라 이 값으로 갈라진다. 결정론적인 값만 실을 수
            # 있으므로(duplicate_key 규약) 리터럴 문자열이다.
            extra={"intent": "confirm"},
        )
        if not claim["claimed"]:
            raise WorkflowClaimRefused(str(claim["reason"]))
        try:
            self._refresh_portfolio_from_broker_snapshot()
        except (RuntimeError, TimeoutError, ValueError):
            if self._has_readonly_broker_accounts():
                raise
        strategy_ids = [str(item) for item in request.get("strategy_ids") or []]
        if not strategy_ids:
            raise ValueError("Funding request is missing strategy_ids")
        self._record_account_cash_flow_from_funding_request(
            request,
            user_id=user_id,
            username=username,
        )
        self._record_strategy_cash_flow_from_funding_request(
            request,
            strategy_ids=strategy_ids,
            user_id=user_id,
            username=username,
        )
        # Funding was just confirmed by the operator; regenerate orders
        # immediately instead of waiting for the scheduled buy_day.
        signal_summary = self._run_child_signal(request, workflow_id, attempt=attempt)
        lines = [
            "Funding confirmed",
            f"request_id: {request['request_id']}",
            f"new_signal_run_id: {signal_summary.signal_run_id}",
            f"orders_preview_count: {signal_summary.orders_preview_count}",
        ]
        if signal_summary.orders_preview_count == 0 and signal_summary.no_order_reasons:
            lines.append("no orders were generated because:")
            lines.extend(f"- {reason}" for reason in signal_summary.no_order_reasons)
        follow_up = self._deliver_child_signal_outcome(chat_id, signal_summary)
        # Carrying the same request_id into both legs of complete_workflow is
        # what keeps the workflow log and the legacy ack log pointed at the
        # same request; a mismatch here would close one request in one log
        # and a different one in the other.
        complete_workflow(
            self.store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="funding",
            attempt=attempt,
            legacy_payload={
                "request_id": request_id,
                "status": "confirmed",
                "decided_by": username or str(user_id),
                "new_signal_run_id": signal_summary.signal_run_id,
            },
        )
        lines.extend(follow_up)
        return "\n".join(lines)

    def _deliver_child_signal_outcome(
        self,
        chat_id: int,
        signal_summary: SignalRunSummary,
    ) -> list[str]:
        """Hand the operator whatever the child run still owes them.

        Called *before* ``complete_workflow``, and the order is the point.
        Completing first drops the workflow out of
        ``list_incomplete_workflows``, so a crash in here would leave the
        decision on record, a child run nobody is watching, no approval card
        and no resume card -- the month's investment stops with nothing
        saying so. Delivering first means such a crash leaves the workflow
        claimed-but-incomplete, which is exactly what the resume sweep
        surfaces.

        The cost is that a resume reaches this a second time, so everything
        here has to tolerate being repeated: the approval is adopted rather
        than duplicated (see ``_dispatch_child_approval``), and a re-sent
        funding or budget card is harmless -- the request behind it has its
        own head and claim, so only one decision on it can ever land.
        """
        if signal_summary.action_required:
            return self._dispatch_child_approval(signal_summary.signal_run_id)
        signal = self.store.load_signal_package(signal_summary.signal_run_id) or {}
        budget_requests = signal.get("budget_requests") or []
        funding_requests = signal.get("funding_requests") or []
        for budget_request in budget_requests:
            if not self._request_still_needs_a_card(budget_request, phase="budget"):
                continue
            self._require_card_delivered(
                self._send_budget_request(chat_id, budget_request), "budget", budget_request
            )
        for funding_request in funding_requests:
            if not self._request_still_needs_a_card(funding_request, phase="funding"):
                continue
            self._require_card_delivered(
                self._send_funding_request(chat_id, funding_request), "funding", funding_request
            )
        if budget_requests:
            return ["approval_status: budget_still_required"]
        if funding_requests:
            return ["approval_status: funding_still_required"]
        return ["approval_status: not_required"]

    def _request_still_needs_a_card(self, request: Mapping[str, Any], *, phase: str) -> bool:
        """Whether putting this request in front of the operator still means anything.

        A resume reaches the delivery step again, and by then the card it
        could not confirm may well have arrived and been acted on -- that is
        the ordinary outcome, not the rare one. Demanding delivery of a
        request that is already decided, or that a later run has replaced,
        makes the parent workflow permanently uncompletable: the card is
        never re-sent (an unaccountable copy must not be), so the delivery
        can never be confirmed, so completion can never proceed, and the
        operator is offered a Resume button that fails the same way every
        time.

        Both questions are about the request's own lifecycle, not the
        card's. Decided is what the pre-CAS loaders already answer (they
        return None once the legacy terminal event exists); replaced is
        whether the workflow head still names it.

        The first delivery is unaffected: the child run's request was just
        published, so it is pending and at head.
        """
        request_id = str(request.get("request_id") or "")
        if not request_id:
            return True
        loader = (
            self._load_pending_funding_request
            if phase == "funding"
            else self._load_pending_budget_request
        )
        if loader(request_id) is None:
            return False
        try:
            head = self.store.load_funding_workflow_head(workflow_id_from_request(request))
        except (ValueError, KeyError):
            # A request we cannot derive a workflow id for predates the head
            # convention or is malformed. It has no head to have been
            # replaced by, so the delivery obligation stands.
            return True
        if head is None:
            return True
        return str(head.get("request_id") or "") == request_id

    def _require_card_delivered(
        self, outcome: str, phase: str, request: Mapping[str, Any]
    ) -> None:
        """Refuse to let a caller treat an unconfirmed card as delivered.

        "sent" and "skipped" (the operator already holds a confirmed copy)
        are the only outcomes that mean the operator can act on this request.
        "failed" means Telegram explicitly refused every chat; "unknown"
        means a timeout or dropped connection left delivery unaccountable --
        raising either keeps the caller (``_deliver_child_signal_outcome``)
        from reaching ``complete_workflow``.

        Leaving the parent workflow incomplete is not, on its own, enough to
        get anyone's attention. Publishing a follow-up request supersedes the
        parent in the same scope, and ``list_incomplete_workflows`` drops a
        request that is no longer head -- so in exactly the case that has a
        card to deliver, the parent is invisible to the resume sweep. The
        request that has no card is invisible too: nobody could tap it, so
        it has no claim to be stalled on. The notice below is therefore the
        operator's only account of it, and it names the request rather than
        the workflow for the same reason.

        It is deduplicated per (phase, request) rather than per attempt: the
        fact being reported is "this request has no card", which stays true
        however many times delivery is retried.
        """
        if outcome in ("sent", "skipped"):
            return
        request_id = str(request.get("request_id") or "")
        self._record_undelivered_request_card(request_id, phase=phase, delivery=outcome)
        # Best effort, and deliberately after the record above: this message
        # travels the same channel that just failed to carry the card, so it
        # may well not arrive either. _notify_operator_chats swallows a failed
        # chat, so a dead channel costs nothing here.
        self._notify_operator_chats(
            run_id=f"run_{request_id}",
            approval_id=f"{phase}:{request_id}",
            event_type="funding_request_card_undelivered_notice",
            key_prefix="funding-request-card-undelivered-notice",
            subject_field="request_key",
            text=ui_catalog.REQUEST_CARD_UNDELIVERED_TEMPLATE.format(
                request_id=request_id, phase=phase
            ),
            extra={"request_id": request_id, "phase": phase, "delivery": outcome},
        )
        raise RuntimeError(
            f"{phase} request card delivery is {outcome!r} for request {request_id!r}; "
            "refusing to complete the workflow until delivery is confirmed"
        )

    def _record_undelivered_request_card(
        self, request_id: str, *, phase: str, delivery: str
    ) -> None:
        """Write down that this request has no card, before trying to say so.

        The record has to be independent of the telling. The notice goes out
        over the channel that just failed to carry the card, and
        ``_notify_operator_chats`` only writes its event for chats it
        actually reached -- so on a dead channel, making the record a
        side effect of the notice means no record at all, in precisely the
        case that most needs one.

        Keyed by (phase, request) with no attempt or timestamp: the fact is
        "this request has no card", which does not change however many times
        delivery is retried, and a key that changed per retry would grow one
        row per tap.
        """
        duplicate_key = f"funding-request-card-undelivered:{phase}:{request_id}"
        if self.store.duplicate_key_exists(duplicate_key):
            return
        save_audited_system_event(
            self.store,
            self.audit,
            f"run_{request_id}",
            "funding_request_card_undelivered",
            {
                "request_id": request_id,
                "phase": phase,
                "delivery": delivery,
                "duplicate_key": duplicate_key,
            },
        )

    def _dispatch_child_approval(self, signal_run_id: str) -> list[str]:
        """Put an approval card in front of the operator for this child run.

        Every path out of here either creates the approval or raises. A
        status string the caller treats as success is not available: the
        caller completes the workflow on return, and a completed workflow
        with no approval card is the silent stall this whole ordering exists
        to prevent. Raising instead keeps the workflow claimed-but-incomplete,
        so once the missing configuration is supplied the operator can resume
        it rather than having to reconstruct the month by hand.

        The "already done" checks exist for that resume. A crash between the
        dispatch and ``complete_workflow`` leaves the workflow incomplete, so
        the resume runs this again against a child run whose approval already
        exists -- and both entry points refuse that outright
        (``dispatch_signal_approval`` on a settled package, ``approve_signal``
        on a consumed one). Without the checks the resume could never get
        past this line, and the workflow would stay stalled forever over work
        that was in fact already finished.

        The state is read through the approval orchestrator's own store
        rather than ``self.store``: it is the one that wrote the events being
        asked about, so the two cannot disagree even if the operator and the
        approval config are pointed at different databases.
        """
        if self.approval_config_path is None:
            raise ValueError(
                "Signal approval requires telegram-operator --approval-config; "
                f"child run {signal_run_id} needs an approval and cannot be given one"
            )
        approval_config, approval_identity = load_config_with_identity(self.approval_config_path)
        approval_orchestrator = MaestroOrchestrator(
            approval_config,
            telegram_client=self.client,
            config_identity=approval_identity,
        )
        approval_store = approval_orchestrator.state_store
        if (
            approval_config.mode == RunMode.LIVE_APPROVAL
            and approval_config.approval.provider == "telegram"
        ):
            if approval_store.signal_dispatch_settled(signal_run_id):
                return ["approval_status: already_dispatched"]
            approval_summary = approval_orchestrator.dispatch_signal_approval(signal_run_id)
            order_line = f"orders_planned: {approval_summary.orders_planned}"
            pending_line = f"approvals_pending: {approval_summary.approvals_pending}"
        else:
            if approval_store.signal_approval_run_completed(signal_run_id):
                return ["approval_status: already_approved"]
            package = approval_store.load_signal_package(signal_run_id) or {}
            if package.get("approval_consumed"):
                # approve_signal marks the package consumed *before* it starts
                # placing orders and only writes signal_approval_completed at
                # the very end, so consumed-without-completed means an
                # approval run died somewhere in between -- possibly with
                # some of its orders already at the broker. Adopting that as
                # success would close the funding workflow over a half-filled
                # batch. It cannot be re-run either (approve_signal refuses a
                # consumed package), so a human has to settle it.
                raise ValueError(
                    f"Signal package {signal_run_id} was consumed by an approval run that "
                    "never reported completion; settle it by hand before resuming"
                )
            approval_summary = approval_orchestrator.approve_signal(signal_run_id)
            order_line = f"orders_created: {approval_summary.orders_created}"
            pending_line = "approvals_pending: 0"
        return [
            f"approval_run_id: {approval_summary.run_id}",
            f"approval_status: {approval_summary.approval_status}",
            order_line,
            pending_line,
        ]

    def _run_child_signal(
        self,
        request: Mapping[str, Any],
        workflow_id: str,
        *,
        attempt: int,
        phase: str = "funding",
    ) -> SignalRunSummary:
        """Create this request's child run, or return the one it already has.

        Lineage (source_request_id/source_workflow_id/source_phase) is what
        lets a resumed attempt land here a second time without building a
        second signal package: run_signal() looks up the child by
        source_request_id before doing any work, inside the same lock it
        would use to create one. ``phase`` defaults to "funding" so the
        existing funding call sites and tests need no change; the budget
        path passes "budget" explicitly.
        """
        signal_config, signal_identity = load_config_with_identity(self.signal_config_path)
        return MaestroOrchestrator(
            signal_config,
            config_identity=signal_identity,
        ).run_signal(
            strategy_ids=[str(item) for item in request.get("strategy_ids") or []],
            contribution_override=True,
            source_request_id=str(request["request_id"]),
            source_workflow_id=workflow_id,
            source_phase=phase,
        )

    def _record_account_cash_flow_from_funding_request(
        self,
        request: dict[str, Any],
        *,
        user_id: int,
        username: str | None,
    ) -> None:
        amount = _float_or_none(request.get("required_shortfall"))
        account_id = str(request.get("account_id") or "")
        if amount is None or amount <= 0 or not account_id:
            return
        if self.store.load_latest_account_portfolio_state(account_id) is None:
            return
        latest = next(
            (
                row
                for row in self.store.list_broker_account_snapshots(limit=100)
                if _broker_snapshot_account_id(row) == account_id
            ),
            None,
        )
        account = _mapping(_mapping(latest or {}).get("payload")).get("account")
        source = str(_mapping(account).get("source") or "")
        verification = "operator_verified" if source.startswith("toss_") else "broker_verified"
        AccountCashFlowService(self.store, self.audit).record(
            account_id=account_id,
            amount=amount,
            currency=str(request.get("currency") or "KRW"),
            flow_type="deposit",
            effective_at=utc_now().isoformat(),
            source="telegram_funding_confirmation",
            decided_by=username or str(user_id),
            proposal_id=str(request.get("request_id") or ""),
            evidence={
                "funding_request_id": request.get("request_id"),
                "broker_snapshot_id": (latest or {}).get("id"),
            },
            verification=verification,
            duplicate_key=f"account-cash-flow:funding:{request.get('request_id')}",
        )

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
            duplicate_key = f"strategy-cash-flow:funding:{request.get('request_id')}:{strategy_id}"
            if self.store.duplicate_key_exists(duplicate_key):
                continue
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
                "duplicate_key": duplicate_key,
            }
            save_audited_system_event(
                self.store,
                self.audit,
                new_run_id(),
                "strategy_cash_flow",
                payload,
            )

    def _send_voluntary_deposit_allocation_proposal(
        self,
        chat_id: int,
        *,
        account_id: str | None = None,
    ) -> None:
        proposal = self._build_voluntary_deposit_proposal(account_id=account_id)
        if proposal is None:
            return
        if proposal.get("source") == "toss_buying_power_fx_conversion_candidate":
            self._send_toss_fx_conversion_candidate(chat_id, proposal)
            return
        if proposal.get("source") == "toss_buying_power_cash_flow_candidate":
            self._send_toss_cash_flow_candidate(chat_id, proposal)
            return
        lines = [
            f"Unattributed {proposal['flow_type']} detected",
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
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            "account_cash_flow_proposal",
            proposal,
        )

    def _send_toss_cash_flow_candidate(
        self,
        chat_id: int,
        proposal: dict[str, Any],
    ) -> None:
        fingerprint = str(proposal["fingerprint"])
        notice_key = f"telegram-toss-cash-flow-notice:{fingerprint}"
        if self.store.duplicate_key_exists(notice_key):
            return
        if not self._cash_flow_candidate_proposal(fingerprint):
            account_payload = dict(
                proposal,
                duplicate_key=f"toss-cash-flow-proposal:account:{fingerprint}",
            )
            strategy_payload = dict(
                proposal,
                duplicate_key=f"toss-cash-flow-proposal:strategy:{fingerprint}",
            )
            save_audited_system_event(
                self.store,
                self.audit,
                new_run_id(),
                SystemEventType.ACCOUNT_CASH_FLOW_PROPOSAL,
                account_payload,
            )
            save_audited_system_event(
                self.store,
                self.audit,
                new_run_id(),
                SystemEventType.STRATEGY_CASH_FLOW_PROPOSAL,
                strategy_payload,
            )
        evidence = dict(proposal.get("evidence") or {})
        label = "deposit" if proposal["flow_type"] == "deposit" else "withdrawal"
        self._send(
            chat_id,
            "Maestro cash-flow candidate\n"
            f"account_id: {_mask_identifier(proposal['account_id'])}\n"
            f"candidate: {label}\n"
            f"amount: {_money(proposal['amount'])} {proposal['currency']}\n"
            f"first_observed: {proposal['effective_at']}\n"
            f"stable_snapshots: {len(evidence.get('stable_snapshot_ids') or [])}\n"
            "evidence: positions/orders/fills unchanged\n\n"
            "Confirm only after checking the amount in the Toss app.",
            reply_markup=_toss_cash_flow_candidate_markup(proposal),
        )
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            "telegram_cash_flow_candidate_notice",
            {
                "proposal_id": proposal["proposal_id"],
                "fingerprint": fingerprint,
                "account_id": proposal["account_id"],
                "duplicate_key": notice_key,
            },
        )

    def _send_toss_fx_conversion_candidate(
        self,
        chat_id: int,
        proposal: dict[str, Any],
    ) -> None:
        fingerprint = str(proposal["fingerprint"])
        notice_key = f"telegram-toss-fx-conversion-notice:{fingerprint}"
        if self.store.duplicate_key_exists(notice_key):
            return
        if not self._cash_flow_candidate_proposal(fingerprint):
            for event_type, scope in (
                (SystemEventType.ACCOUNT_CASH_FLOW_PROPOSAL, "account"),
                (SystemEventType.STRATEGY_CASH_FLOW_PROPOSAL, "strategy"),
            ):
                save_audited_system_event(
                    self.store,
                    self.audit,
                    new_run_id(),
                    event_type,
                    dict(
                        proposal,
                        duplicate_key=f"toss-fx-proposal:{scope}:{fingerprint}",
                    ),
                )
        evidence = dict(proposal.get("evidence") or {})
        observed_from = float(proposal.get("observed_from_amount") or proposal["from_amount"])
        observed_to = float(proposal.get("observed_to_amount") or proposal["to_amount"])
        ledger_from = float(proposal["from_amount"])
        ledger_to = float(proposal["to_amount"])
        ledger_note = ""
        if abs(observed_from - ledger_from) > 1e-9 or abs(observed_to - ledger_to) > 1e-9:
            ledger_note = (
                "\nMaestro ledger adjustment:\n"
                f"- {_money(ledger_from)} {proposal['from_currency']} → "
                f"{_money(ledger_to)} {proposal['to_currency']}\n"
            )
        self._send(
            chat_id,
            "Maestro currency-conversion candidate\n"
            f"account_id: {_mask_identifier(proposal['account_id'])}\n"
            f"observed from: {_money(observed_from)} {proposal['from_currency']}\n"
            f"observed to: {_money(observed_to)} {proposal['to_currency']}\n"
            f"{ledger_note}"
            f"first_observed: {proposal['effective_at']}\n"
            f"stable_snapshots: {len(evidence.get('stable_snapshot_ids') or [])}\n"
            "evidence: paired cash moves; positions/orders/fills unchanged\n\n"
            "환전이 맞는 경우에만 승인하세요.",
            reply_markup=_toss_fx_conversion_candidate_markup(proposal),
        )
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            "telegram_cash_flow_candidate_notice",
            {
                "proposal_id": proposal["proposal_id"],
                "fingerprint": fingerprint,
                "account_id": proposal["account_id"],
                "candidate_type": "fx_conversion",
                "duplicate_key": notice_key,
            },
        )

    def _build_voluntary_deposit_proposal(
        self,
        *,
        account_id: str | None = None,
    ) -> dict[str, Any] | None:
        snapshots = self.store.list_broker_account_snapshots(limit=1000)
        if account_id is not None:
            snapshots = [row for row in snapshots if _broker_snapshot_account_id(row) == account_id]
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
        candidate = CashFlowCandidateDetector(self.store).detect(account_id)
        if candidate is None:
            return None
        existing = self._cash_flow_candidate_proposal(candidate.fingerprint)
        if existing is not None:
            return existing
        if isinstance(candidate, FxConversionCandidate):
            observed_from_amount = candidate.from_amount
            observed_to_amount = candidate.to_amount
            from_amount = observed_from_amount
            to_amount = observed_to_amount
            ledger_state = self.store.load_latest_account_portfolio_state(candidate.account_id)
            latest_buying_power = _mapping(latest_account.get("buying_power_by_currency"))
            if ledger_state is not None and latest_buying_power:
                ledger_from = float(ledger_state.cash_by_currency.get(candidate.from_currency, 0.0))
                ledger_to = float(ledger_state.cash_by_currency.get(candidate.to_currency, 0.0))
                broker_from = float(latest_buying_power.get(candidate.from_currency, ledger_from))
                broker_to = float(latest_buying_power.get(candidate.to_currency, ledger_to))
                required_from = ledger_from - broker_from
                required_to = broker_to - ledger_to
                if required_from > 0 and required_to > 0:
                    from_amount = required_from
                    to_amount = required_to
            evidence = {
                **candidate.evidence(),
                "observed_from_amount": observed_from_amount,
                "observed_to_amount": observed_to_amount,
                "ledger_from_amount": from_amount,
                "ledger_to_amount": to_amount,
            }
            return {
                "proposal_id": new_run_id(),
                "status": "pending",
                "fingerprint": candidate.fingerprint,
                "account_id": candidate.account_id,
                "broker_snapshot_id": candidate.latest_snapshot_id,
                "previous_broker_snapshot_id": candidate.baseline_snapshot_id,
                "from_currency": candidate.from_currency,
                "from_amount": from_amount,
                "to_currency": candidate.to_currency,
                "to_amount": to_amount,
                "observed_from_amount": observed_from_amount,
                "observed_to_amount": observed_to_amount,
                "source": "toss_buying_power_fx_conversion_candidate",
                "verification": "operator_required",
                "effective_at": candidate.effective_at,
                "created_at": utc_now().isoformat(),
                "allocations": [],
                "evidence": evidence,
            }
        allocations = self._target_cash_flow_allocations(
            candidate.account_id,
            candidate.amount,
        )
        if (
            candidate.cash_basis == BROKER_REPORTED_CASH
            and candidate.flow_type == "deposit"
            and not allocations
        ):
            # A broker-reported deposit is offered for allocation to a strategy;
            # with no enabled strategy on the account there is nothing to offer.
            return None
        return {
            "proposal_id": new_run_id(),
            "status": "pending",
            "fingerprint": candidate.fingerprint,
            "account_id": candidate.account_id,
            "broker_snapshot_id": candidate.latest_snapshot_id,
            "previous_broker_snapshot_id": candidate.baseline_snapshot_id,
            "amount": candidate.amount,
            "currency": candidate.currency,
            "flow_type": candidate.flow_type,
            # The source drives how the confirmed flow is verified downstream:
            # Toss cash is a proxy the operator checks in the app, while a
            # broker-reported figure is the broker's own number.
            "source": (
                "toss_buying_power_cash_flow_candidate"
                if candidate.cash_basis == PROXY_CASH
                else "broker_snapshot_unexplained_cash_change"
            ),
            "verification": "operator_required",
            "effective_at": candidate.effective_at,
            "created_at": utc_now().isoformat(),
            "allocations": allocations,
            "evidence": candidate.evidence(),
        }

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

    def _cash_flow_candidate_proposal(self, fingerprint: str) -> dict[str, Any] | None:
        acked = {
            str(row["payload"].get("proposal_id"))
            for row in self.store.list_system_events_by_type(
                SystemEventType.ACCOUNT_CASH_FLOW_PROPOSAL_ACK,
                limit=1000,
            )
        }
        for row in self.store.list_system_events_by_type(
            SystemEventType.ACCOUNT_CASH_FLOW_PROPOSAL,
            limit=1000,
        ):
            payload = row.get("payload") or {}
            if payload.get("fingerprint") != fingerprint:
                continue
            if str(payload.get("proposal_id")) in acked:
                return None
            return payload
        return None

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
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            "account_cash_flow_proposal_ack",
            {
                "proposal_id": proposal_id,
                "status": status,
                "decided_by": username or str(user_id),
                **extra,
            },
        )

    def _send_funding_request(self, chat_id: int, request: dict[str, Any]) -> str:
        request_id = str(request.get("request_id") or "")
        return self._send_request_card(
            chat_id,
            request_id,
            phase="funding",
            text=format_contribution_funding_request(request),
            reply_markup=funding_request_reply_markup(request_id),
        )

    def _send_budget_request(self, chat_id: int, request: dict[str, Any]) -> str:
        return self._send_request_card(
            chat_id,
            str(request.get("request_id") or ""),
            phase="budget",
            text=format_contribution_budget_request(request),
            reply_markup=budget_request_reply_markup(request),
        )

    def _send_request_card(
        self,
        chat_id: int,
        request_id: str,
        *,
        phase: str,
        text: str,
        reply_markup: dict[str, Any] | None,
    ) -> str:
        """Deliver an actionable request card at most once per chat.

        A request card is a live decision button, and the transition behind it
        accepts exactly one decision. The delivery is reached again whenever
        the workflow that produced it is resumed -- completion happens after
        delivery, so a crash in between replays this -- and a plain send would
        put a second button-bearing card for the same request in the chat.
        Recording the intent before the call and the result after is what lets
        the replay tell "already sent" from "never sent".

        A request with no id cannot be keyed, so it falls back to a plain
        send: an un-keyed card delivered twice is better than one that is
        deduplicated against an unrelated card.

        Returns the delivery outcome ("sent", "skipped", "failed" or
        "unknown") rather than swallowing it -- a caller that completes a
        workflow on the strength of this call must be able to tell a
        confirmed delivery from one Telegram refused or left ambiguous.
        """
        if not request_id:
            self._send(chat_id, text, reply_markup=reply_markup)
            return "sent"
        return self._card_manager.deliver_once(
            new_run_id(),
            f"{phase}-request:{request_id}",
            "pending",
            RenderedCard(text=text, reply_markup=reply_markup),
            chat_id=chat_id,
        )

    def _load_pending_budget_request(self, request_id: str) -> dict[str, Any] | None:
        """The request payload, if the *workflow* still says this request is open.

        This used to ask "is there no contribution_budget_request_decision row
        for it?". That row is the rollback compatibility projection
        complete_workflow writes for the pre-CAS binary, and using it here made
        it a second definition of "pending" -- one that cannot see supersession,
        phase or attempt, and that a rollback-era writer can produce on its own.
        It is still written, and rollback preflight still requires it (R4), but
        it no longer decides anything here.

        Both scans were also windowed at 1000 rows. The workflow readers are
        not: an operator acting on a request that has scrolled out of a window
        is exactly the case where being wrong costs a duplicate transition.
        """
        if not is_request_pending(self.store, request_id, "budget"):
            return None
        return load_request_payload(self.store, request_id, "budget")

    def _cancel_budget_request(
        self,
        request: dict[str, Any],
        *,
        user_id: int,
        username: str | None,
        attempt: int = 1,
    ) -> None:
        """Mirrors ``_cancel_funding_request``: cancel is a terminal
        transition too, so it goes through claim-then-complete rather than
        writing the legacy decision on its own.
        """
        workflow_id = workflow_id_from_request(request)
        request_id = str(request["request_id"])
        claim = claim_workflow_attempt(
            self.store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="budget",
            attempt=attempt,
            # cancel claim에는 selected_budget이 없다. 재개가 intent로
            # 갈라지므로 없어야 맞고, 있어서도 안 된다.
            extra={"intent": "cancel"},
        )
        if not claim["claimed"]:
            raise WorkflowClaimRefused(str(claim["reason"]))
        complete_workflow(
            self.store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="budget",
            attempt=attempt,
            legacy_payload={
                "request_id": request_id,
                "status": "canceled",
                "strategy_ids": list(request.get("strategy_ids") or []),
                "contribution_group_id": request.get("contribution_group_id"),
                "account_id": request.get("account_id"),
                "execution_sleeve": request.get("execution_sleeve"),
                "currency": request.get("currency"),
                "month_key": request.get("month_key"),
                "decided_by": username or str(user_id),
            },
        )

    def _load_pending_funding_request(self, request_id: str) -> dict[str, Any] | None:
        """See ``_load_pending_budget_request``: the workflow decides, not the
        legacy ``contribution_funding_request_ack`` projection."""
        if not is_request_pending(self.store, request_id, "funding"):
            return None
        return load_request_payload(self.store, request_id, "funding")

    def _cancel_funding_request(
        self,
        request: dict[str, Any],
        *,
        user_id: int,
        username: str | None,
        attempt: int = 1,
    ) -> None:
        """Cancel is a terminal transition too, so it goes through the same
        claim-then-complete shape as confirm: a canceled request still has to
        be refused if it was already decided or superseded, and the legacy
        ack still has to land in the same atomic batch as the workflow's own
        completed event, or a rollback would see a canceled request as still
        pending and let it be re-confirmed later.

        ``attempt`` mirrors ``_confirm_funding_request``'s parameter of the
        same name so the resume path (Task 10) can drive either transition:
        without it a stalled cancel would be pinned at the attempt its own
        stalled claim already holds, and the request could never be closed.
        """
        workflow_id = workflow_id_from_request(request)
        request_id = str(request["request_id"])
        claim = claim_workflow_attempt(
            self.store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="funding",
            attempt=attempt,
            # 재개가 confirm으로 새지 않도록, 운영자가 요청한 전이를
            # claim 자체에 기록한다.
            extra={"intent": "cancel"},
        )
        if not claim["claimed"]:
            raise WorkflowClaimRefused(str(claim["reason"]))
        complete_workflow(
            self.store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="funding",
            attempt=attempt,
            legacy_payload={
                "request_id": request_id,
                "status": "canceled",
                "decided_by": username or str(user_id),
            },
        )

    def _help(self, chat_id: int) -> None:
        self._send(
            chat_id,
            "\n".join(
                [
                    "Maestro 명령어",
                    *[
                        f"/{command} - {description}"
                        for command, description in TELEGRAM_UI_COMMANDS
                    ],
                    "",
                    "⚠️ 비상 조치 (메뉴에는 없지만 언제든 입력할 수 있어요)",
                    *[
                        f"/{command} - {description}"
                        for command, description in TELEGRAM_EMERGENCY_COMMANDS
                    ],
                    "",
                    "이런 알림이 올 수 있어요:",
                    "- 📩 투자 승인 요청 (버튼으로 승인/거절)",
                    "- ⏰ 승인 응답 리마인더",
                    "- ⚠️ 확인이 필요한 상황 안내",
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
        # 메뉴에서 사라진 비상 제어를 시스템 상태 카드에서만 노출한다.
        self._send(chat_id, "\n".join(lines), reply_markup=_emergency_menu_markup())

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
        account_ids = [
            str(account_id)
            for account_id, _ in broker_readonly_accounts(self.config)
            if account_id is not None
        ]
        if not account_ids:
            self._send_voluntary_deposit_allocation_proposal(chat_id)
        for account_id in account_ids:
            self._send_voluntary_deposit_allocation_proposal(
                chat_id,
                account_id=account_id,
            )

    def _cash_drift(self, chat_id: int) -> None:
        rows = self.store.list_cash_suspense()
        if not rows:
            self._send(chat_id, "Cash suspense: none")
            return
        for row in rows:
            self._send(
                chat_id,
                "Cash suspense (buying power is not ledger cash)\n"
                f"account_id: {_mask_identifier(str(row['account_id']))}\n"
                f"currency: {row['currency']}\n"
                f"difference: {_money(float(row['amount']))}\n"
                f"candidate: {row['candidate_label']}\n"
                f"status: {row['status']}\n"
                "Classification does not change the ledger automatically.",
                reply_markup=_cash_drift_markup(row),
            )

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
        recoverable = self._pending_recovery_candidates()

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

    def _pending_recovery_candidates(self) -> list[LiveOrderRecoveryCandidate]:
        recoverable: list[LiveOrderRecoveryCandidate] = []
        for row in self.store.list_orders(limit=5000):
            order_id = str(row.get("order_id") or "")
            candidate = self._pending_recovery_candidate(order_id)
            if candidate is not None:
                recoverable.append(candidate)
            if len(recoverable) >= 5:
                break
        return recoverable

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
        acked = self._terminal_approval_ids()
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

    def _recovery(self, chat_id: int) -> None:
        text, markup = self._recovery_message()
        self._send(chat_id, text, reply_markup=markup)

    def _recovery_message(self) -> tuple[str, dict[str, Any] | None]:
        safety = SafetyControlService(self.store, self.audit).current_state()
        preview = WorkflowRecoveryService(self.config, self.store, self.audit).preview()
        recoverable_orders = self._pending_recovery_candidates()
        health = HealthService(self.config, self.store).run()
        health_checks = {check.name: check for check in health.checks}
        broker_snapshot = health_checks.get("broker_snapshot")
        reconciliation = health_checks.get("reconciliation")
        lines = [
            "Maestro Recovery Center",
            f"safety: {safety.state.value}",
            f"safety_reason: {safety.reason}",
            f"health: {health.status}",
            "broker_snapshot: "
            + (
                f"{broker_snapshot.status}/{broker_snapshot.message}"
                if broker_snapshot is not None
                else "unknown"
            ),
            "reconciliation: "
            + (
                f"{reconciliation.status}/{reconciliation.message}"
                if reconciliation is not None
                else "unknown"
            ),
            f"live_order_blockers: {len(preview.blockers)}",
            f"retryable_orders: {len(recoverable_orders)}",
        ]
        for blocker in preview.blockers[:10]:
            lines.append(
                f"- {blocker.detail_reason or blocker.reason} "
                f"order={blocker.order_id or 'unknown'} "
                f"account={blocker.account_id or 'unknown'}"
            )
        if safety.state == SafetyState.KILLED:
            lines.append("Kill switch release remains CLI-only: maestro release-kill")
        if safety.state == SafetyState.ACTIVE and not preview.blockers:
            lines.append("No blocked workflow requires recovery.")
        return "\n".join(lines), _workflow_recovery_center_markup(
            preview.fingerprint,
            safety_state=safety.state,
            has_live_order_blockers=bool(preview.blockers),
            has_retryable_orders=bool(recoverable_orders),
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

    def _edit_callback_message(
        self,
        callback: Mapping[str, Any],
        text: str,
        *,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> None:
        message = callback.get("message")
        if not isinstance(message, Mapping):
            return
        chat_id = _chat_id(message)
        message_id = message.get("message_id")
        edit_message_text = getattr(self.client, "edit_message_text", None)
        if chat_id is None or not isinstance(message_id, int) or not callable(edit_message_text):
            return
        try:
            edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)
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

    def _terminal_approval_ids(self) -> set[str]:
        """더 이상 처리하지 않을 승인 집합. 종결 판정은 여기 한 곳에서만 한다.

        정합성 판정이므로 **어떤 창도 쓰지 않는다** — 개수 창(limit)이든 시간
        창(since)이든, 오래된 미완 승인이 조회 밖으로 밀리면 조용히 사라진다.
        긴 장애 뒤에 도달 가능한 상태이고, 승인 하나를 잃는 쪽이 작은 테이블을
        전수 조회하는 비용보다 훨씬 나쁘다. 승인 ack는 연 250행 규모로만
        늘고 idx_system_events_type_created가 event_type 필터를 덮는다.

        ack는 운영자 의사의 기록일 뿐 종결이 아니다 — 주문 집행까지 끝난
        resolution_completed가 있어야 종결이다. 단 schema_version이 없는
        ack는 3a 이전 기록이라 completed가 존재할 수 없으므로 종결로 본다
        (없으면 이미 정상 완료된 과거 승인을 전부 재집행하게 된다).
        """
        completed = {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_resolution_completed",
                limit=None,
            )
        }
        terminal = set(completed)
        for row in self.store.list_system_events_by_type(
            "telegram_approval_ack", limit=None
        ):
            payload = row["payload"]
            approval_id = str(payload.get("approval_id"))
            if not isinstance(payload.get("schema_version"), int):
                terminal.add(approval_id)
        return terminal

    def _decided_approval_ids(self) -> set[str]:
        """운영자 결정이 기록된(3a 스키마 ack) 승인 집합.

        종결(terminal)과는 다르다 — 결정만 있고 집행이 끝나지 않았을 수 있다.
        이 승인들의 후속 처리는 재개 경로가 전담하므로, 만료 재판정처럼 결정을
        다시 쓰려는 경로는 여기서 걸러야 한다.
        """
        return {
            str(row["payload"].get("approval_id"))
            for row in self.store.list_system_events_by_type(
                "telegram_approval_ack", limit=None
            )
            if isinstance(row["payload"].get("schema_version"), int)
        }

    def _pending_async_approval(
        self,
        approval_id: str,
    ) -> PendingApprovalEnvelope | None:
        if approval_id in self._terminal_approval_ids():
            return None
        for row in self.store.list_system_events_by_type(
            "telegram_approval_pending",
            limit=None,
        ):
            if row["payload"].get("approval_id") == approval_id:
                envelope = PendingApprovalEnvelope.model_validate(row["payload"])
                if utc_now() >= envelope.expires_at:
                    return None
                return envelope
        return None


def _shown_progress(copies: Mapping[tuple[str, int], Any]) -> str | None:
    """카드가 지금까지 보여준 진행 단계 중 가장 앞선 것.

    'attention'은 진행이 아니라 주의 축이므로 여기서 세지 않는다.
    """
    shown = [copy.stage for copy in copies.values() if copy.stage in PROGRESS_RANK]
    if not shown:
        return None
    return max(shown, key=lambda stage: PROGRESS_RANK[stage])


def _daily_card_stage(group_stages: list[str]) -> str:
    """부모 카드가 기록할 단계. 화면의 줄들은 그룹별로 따로 그려진다.

    이 값은 투영에 남는 요약일 뿐이므로 가장 나쁜 쪽을 취한다 — 한 그룹이라도
    주의가 필요하면 부모도 종점으로 접히지 않아야 다시 판정된다.
    """
    if "attention" in group_stages:
        return "attention"
    if all(stage == "done" for stage in group_stages):
        return "done"
    if any(stage == "in_progress" for stage in group_stages):
        return "in_progress"
    return "pending"


def _funding_claim_refusal_response(
    reason: str, *, request_noun: str = "funding"
) -> tuple[str, str]:
    """Map a claim refusal reason to operator-facing text and an audit status.

    ``already_claimed`` means an earlier attempt on this exact request
    already committed a claim and never reached completion -- the request is
    stuck, not done. ``no_head`` means this request has no workflow head at
    all, which for now can only mean it was published before this upgrade:
    nothing processed or superseded it, so saying so would be false and would
    send the operator looking for a replacement that does not exist. The
    remaining reasons (``not_head``, ``head_moved``) do mean a newer request
    replaced this one, which is a normal, harmless outcome. Conflating any of
    the three would tell an operator something untrue about why the button
    did nothing.

    ``attempt_superseded``/``unclaimed_attempt`` come from the fencing check
    in ``complete_workflow``, not from the claim: this attempt got as far as
    doing the work and was then refused the completion because a later
    attempt owns the transition (or because this one never held it). The work
    itself is not lost -- the child run and the cash flow are keyed by
    request id, so whoever does own the transition adopts them -- but the
    operator has to be told this tap is not the one that finished it, rather
    than being shown a success or a "superseded request" that is not true of
    the request at all. ``head_corrupt`` is the one reason that means neither
    "done" nor "replaced": the workflow record itself disagrees with the
    transition being asked for, which no amount of tapping can resolve.

    ``attempt_out_of_order`` is the claim-side twin of the completion
    fencing: this attempt number skipped one that was never claimed (a resume
    button computing the wrong count, or a stale/replayed callback), so
    nothing has entered the transition under it. There is no in-flight work
    to check the status of -- the operator just needs a fresh resume rather
    than to retry this exact tap.

    ``request_noun`` is phase-agnostic wording only ("funding" vs "budget");
    the claim/complete plumbing this describes is identical for both phases,
    so this helper is shared rather than duplicated per phase.
    """
    if reason in {"attempt_superseded", "unclaimed_attempt"}:
        return (
            f"This {request_noun} request was taken over by a newer resume attempt, "
            "so this one did not finish it. Check its current status before acting "
            "again.",
            "claim_fenced",
        )
    if reason == "attempt_out_of_order":
        return (
            f"This {request_noun} request's resume attempt is out of order and was "
            "not applied. Use the latest resume card for this request rather than "
            "retrying this tap.",
            "claim_attempt_out_of_order",
        )
    if reason == "head_corrupt":
        return (
            f"This {request_noun} request's workflow record does not match the "
            "action requested, so it was not applied. This needs to be looked at "
            "by hand.",
            "claim_head_corrupt",
        )
    if reason == "already_claimed":
        return (
            f"This {request_noun} request is already being processed -- an earlier "
            "attempt did not finish. Check its status before tapping again.",
            "claim_in_flight",
        )
    if reason == "no_head":
        return (
            f"This {request_noun} request predates the workflow upgrade and can no "
            "longer be actioned from this card. The next scheduled signal run will "
            "publish an equivalent request.",
            "claim_no_head",
        )
    return (
        f"This {request_noun} request was already processed or superseded.",
        "claim_superseded",
    )


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


def _emergency_menu_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "⏸ 일시중지",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}menu:pause",
                },
                {
                    "text": "🛑 긴급정지",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}menu:kill_switch",
                },
            ],
            [
                {
                    "text": "🛟 복구 센터",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}menu:recovery",
                },
            ],
        ]
    }


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


def _workflow_recovery_center_markup(
    fingerprint: str,
    *,
    safety_state: SafetyState,
    has_live_order_blockers: bool,
    has_retryable_orders: bool,
) -> dict[str, Any] | None:
    rows: list[list[dict[str, str]]] = []
    if has_live_order_blockers:
        rows.append(
            [
                {
                    "text": "주문 상태 확인 및 복구",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}wfrec:auto:{fingerprint}",
                }
            ]
        )
    if safety_state == SafetyState.HALTED:
        rows.append(
            [
                {
                    "text": "Safety halt 해제",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}confirm:clear-halt",
                }
            ]
        )
    if has_retryable_orders:
        rows.append(
            [
                {
                    "text": "재주문 검토 보기",
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}wfrec:orders",
                }
            ]
        )
    return {"inline_keyboard": rows} if rows else None


def _workflow_recovery_markup(
    fingerprint: str,
    *,
    attestation: bool,
) -> dict[str, Any]:
    action = "attest" if attestation else "auto"
    text = "브로커에서 미접수·미체결 확인 후 해제" if attestation else "주문 상태 확인 및 복구"
    return {
        "inline_keyboard": [
            [
                {
                    "text": text,
                    "callback_data": f"{OPERATOR_CALLBACK_PREFIX}wfrec:{action}:{fingerprint}",
                }
            ],
            [{"text": "Cancel", "callback_data": f"{OPERATOR_CALLBACK_PREFIX}cancel"}],
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


def _toss_cash_flow_candidate_markup(proposal: Mapping[str, Any]) -> dict[str, Any]:
    proposal_id = str(proposal.get("proposal_id") or "")
    flow_type = str(proposal.get("flow_type") or "deposit")
    label = "입금" if flow_type == "deposit" else "출금"
    amount = _money(proposal.get("amount"))
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"{label} {amount} 맞음",
                    "callback_data": (f"{OPERATOR_CALLBACK_PREFIX}cash-flow:confirm:{proposal_id}"),
                }
            ],
            [
                {
                    "text": "금액이 다름",
                    "callback_data": (
                        f"{OPERATOR_CALLBACK_PREFIX}cash-flow:different:{proposal_id}"
                    ),
                },
                {
                    "text": "입출금 아님",
                    "callback_data": (f"{OPERATOR_CALLBACK_PREFIX}cash-flow:reject:{proposal_id}"),
                },
            ],
        ]
    }


def _toss_fx_conversion_candidate_markup(
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    proposal_id = str(proposal.get("proposal_id") or "")
    return {
        "inline_keyboard": [
            [
                {
                    "text": "환전 맞음",
                    "callback_data": (f"{OPERATOR_CALLBACK_PREFIX}cash-flow:confirm:{proposal_id}"),
                }
            ],
            [
                {
                    "text": "환전 아님",
                    "callback_data": (f"{OPERATOR_CALLBACK_PREFIX}cash-flow:reject:{proposal_id}"),
                }
            ],
        ]
    }


def _cash_drift_markup(row: Mapping[str, Any]) -> dict[str, Any]:
    account_id = str(row.get("account_id") or "")
    currency = str(row.get("currency") or "").upper()
    snapshot_id = str(row.get("last_snapshot_id") or "")

    def button(token: str, label: str) -> dict[str, Any]:
        return {
            "text": label,
            "callback_data": (
                f"{OPERATOR_CALLBACK_PREFIX}cash-drift:classify:"
                f"{account_id}:{currency}:{token}:{snapshot_id}"
            ),
        }

    return {
        "inline_keyboard": [
            [button("s", "정산 후보"), button("t", "입출금 후보")],
            [button("d", "배당"), button("i", "이자")],
            [button("x", "세금"), button("f", "수수료")],
            [button("c", "환전")],
            [button("u", "미분류 유지")],
        ]
    }


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


def _quantity_step(config: MaestroConfig, order: OrderIntent) -> float:
    """The instrument's tradable step, defaulting to whole units.

    An unknown instrument is not evidence that fractional quantities are
    tradable, and quoting a partial share the broker would reject is worse than
    quoting one share fewer.
    """
    instrument = config.universe.get(order.symbol)
    return float(instrument.quantity_step) if instrument is not None else 1.0


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
    # 메뉴에는 UI 명령 5개만 노출한다. 기존 20개와 per-strategy 명령은
    # 타이핑하면 여전히 동작한다 (signal_config 인자는 하위 호환용으로 유지).
    return [
        {"command": command, "description": description}
        for command, description in TELEGRAM_UI_COMMANDS
    ]


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

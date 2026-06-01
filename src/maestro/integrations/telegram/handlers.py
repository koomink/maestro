from collections.abc import Mapping
from pathlib import Path
from typing import Any

from maestro.config.loader import load_config, load_config_with_identity
from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.ids import new_run_id
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
    build_broker_account_summary,
    build_health_summary,
    build_latest_signal_package_card,
    build_orders_table,
    build_overview,
    build_safety_state_card,
    build_strategy_runs_table,
)
from maestro.execution.broker_state import portfolio_state_from_broker_account
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.funding_requests import (
    format_contribution_funding_request,
    funding_request_reply_markup,
)
from maestro.integrations.telegram.bot import TelegramBotClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.safety.controls import SafetyControlService
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore

OPERATOR_CALLBACK_PREFIX = "operator:"
TELEGRAM_OPERATOR_COMMANDS: tuple[tuple[str, str], ...] = (
    ("help", "Show Maestro command list"),
    ("status", "Show Maestro status summary"),
    ("health", "Show health checks"),
    ("signal", "Show latest Symphony signal package"),
    ("account", "Refresh and show broker account snapshot"),
    ("portfolio", "Show Maestro portfolio state"),
    ("apps", "Show configured strategy apps"),
    ("orders", "Show recent orders"),
    ("approvals", "Show recent approvals"),
    ("pause", "Confirm pause of live approval execution"),
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
    ) -> None:
        self.config = config
        self.store = store
        self.audit = audit
        self.client = client
        self.signal_config_path = Path(signal_config_path) if signal_config_path else None
        self.approval_config_path = Path(approval_config_path) if approval_config_path else None

    def process_update(self, update: Mapping[str, Any]) -> bool:
        callback = update.get("callback_query")
        if isinstance(callback, Mapping):
            return self._process_callback(update, callback)

        message = update.get("message")
        if not isinstance(message, Mapping):
            return False
        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/"):
            return False
        command = _command_name(text)
        chat_id = _chat_id(message)
        user = message.get("from")
        user_id, username = _user_identity(user if isinstance(user, Mapping) else {})
        if chat_id is None or user_id is None:
            return False
        if not self._chat_allowed(chat_id):
            self._record(command, chat_id, user_id, username, "denied_chat")
            return True
        if not self._user_allowed(user_id):
            self._send(chat_id, "Unauthorized Telegram user.")
            self._record(command, chat_id, user_id, username, "denied_user")
            return True

        if command.startswith("/signal_"):
            self._generate_strategy_signal(chat_id, command)
            self._record(command, chat_id, user_id, username, "handled")
            return True

        handler = {
            f"/{command}": handler
            for command, handler in {
                "help": self._help,
                "status": self._status,
                "health": self._health,
                "signal": self._signal,
                "account": self._account,
                "portfolio": self._portfolio,
                "apps": self._apps,
                "orders": self._orders,
                "approvals": self._approvals,
                "pause": self._pause,
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
            self.process_update(update)
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
        if action.startswith("cash-flow:"):
            return self._process_cash_flow_callback(
                callback,
                action,
                chat_id,
                user_id,
                username,
            )
        if action not in {"confirm:pause", "confirm:kill-switch"}:
            self._answer(callback, "This command is no longer active.")
            self._record(command, chat_id, user_id, username, "stale_callback")
            return True

        transition = action.removeprefix("confirm:")
        reason = f"Telegram /{transition} confirmed by {username or user_id}"
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
            and transition in {"approve", "ignore"}
        ) or (
            len(parts) == 4
            and parts[0] == "cash-flow"
            and transition == "assign"
        )
        if not valid_callback:
            self._answer(callback, "This cash-flow proposal is no longer active.")
            self._record("/cash-flow", chat_id, user_id, username, "stale_callback")
            return True
        proposal_id = parts[2]
        assigned_strategy_id = parts[3] if transition == "assign" else None
        proposal = self._load_pending_cash_flow_proposal(proposal_id)
        if proposal is None:
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
                None,
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
            if self.config.kis.enabled:
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
        signal_summary = MaestroOrchestrator(
            signal_config,
            config_identity=signal_identity,
        ).run_signal(strategy_ids=strategy_ids)
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
        if signal_summary.action_required:
            if self.approval_config_path is None:
                lines.append("approval_status: not_created_missing_approval_config")
                return "\n".join(lines)
            approval_config, approval_identity = load_config_with_identity(
                self.approval_config_path
            )
            approval_summary = MaestroOrchestrator(
                approval_config,
                telegram_client=self.client,
                config_identity=approval_identity,
            ).approve_signal(signal_summary.signal_run_id)
            lines.extend(
                [
                    f"approval_run_id: {approval_summary.run_id}",
                    f"approval_status: {approval_summary.approval_status}",
                    f"orders_created: {approval_summary.orders_created}",
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
        save_audited_system_event(
            self.store,
            self.audit,
            new_run_id(),
            "strategy_cash_flow_proposal",
            proposal,
        )
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
        self._send(
            chat_id,
            "\n".join(lines),
            reply_markup=_cash_flow_proposal_markup(proposal),
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
        broker = build_broker_account_summary(self.store)
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

    def _account(self, chat_id: int) -> None:
        refresh_error: Exception | None = None
        try:
            if self.config.kis.enabled:
                self._send(chat_id, "Broker account snapshot: refreshing")
            self._refresh_broker_snapshot()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            refresh_error = exc
        account = build_broker_account_summary(self.store)
        if account["created_at"] is None:
            if refresh_error is not None:
                self._send(chat_id, f"Broker account snapshot refresh failed: {refresh_error}")
                return
            self._send(chat_id, "Broker account snapshot: none")
            return
        currency = self._broker_currency_breakdowns()
        lines = []
        if refresh_error is not None:
            lines.extend(
                [
                    f"Broker account snapshot refresh failed: {refresh_error}",
                    "Showing latest stored broker snapshot.",
                ]
            )
        lines.extend(
            [
                "Broker account snapshot",
                f"created_at: {_operator_time(account['created_at'], self.config)}",
                f"account_id: {_mask_identifier(account['account_id'])}",
                f"total_value: {_money_by_currency(currency['total_value'])}",
                f"cash: {_money_by_currency(currency['cash'])}",
                f"positions_market_value: {_money_by_currency(currency['positions_market_value'])}",
                f"positions: {account['positions_count']}",
                f"source: {account['source'] or 'unknown'}",
            ]
        )
        self._send(
            chat_id,
            "\n".join(lines),
        )
        self._send_voluntary_deposit_allocation_proposal(chat_id)

    def _portfolio(self, chat_id: int) -> None:
        refresh_error: Exception | None = None
        try:
            if self.config.kis.enabled:
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
        rows = build_orders_table(self.store, limit=5)
        if not rows:
            self._send(chat_id, "Recent orders: none")
            return
        lines = ["Recent orders"]
        for row in rows:
            lines.append(
                f"{row['order_id']} {row['side']} {row['symbol']} "
                f"qty={_number(row['quantity'])} status={row['approval_status']}"
            )
        self._send(chat_id, "\n".join(lines))

    def _approvals(self, chat_id: int) -> None:
        rows = build_approvals_table(self.store, limit=5)
        if not rows:
            self._send(chat_id, "Recent approvals: none")
            return
        lines = ["Recent approvals"]
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

    def _kill_switch(self, chat_id: int) -> None:
        self._send(
            chat_id,
            "Confirm kill-switch. This blocks live execution until manual recovery.",
            reply_markup=_confirmation_markup("kill-switch"),
        )

    def _refresh_broker_snapshot(self) -> None:
        if not self.config.kis.enabled:
            return
        KISReadOnlyService(
            self.config.kis,
            self.store,
            self.audit,
            instruments=self.config.universe.instruments,
        ).fetch_and_store_snapshot(self.config.portfolio.allowed_symbols)

    def _refresh_portfolio_from_broker_snapshot(self) -> None:
        if not self.config.kis.enabled:
            return
        snapshot = KISReadOnlyService(
            self.config.kis,
            self.store,
            self.audit,
            instruments=self.config.universe.instruments,
        ).fetch_and_store_snapshot(self.config.portfolio.allowed_symbols)
        state = portfolio_state_from_broker_account(
            snapshot.account.model_dump(mode="json"),
            allowed_symbols=self.config.portfolio.allowed_symbols,
            universe=self.config.universe,
        )
        self.store.save_portfolio_snapshot(new_run_id(), state)

    def _broker_currency_breakdowns(self) -> dict[str, dict[str, float]]:
        latest = self.store.load_latest_broker_account_snapshot()
        if latest is None:
            return {"cash": {"unknown": 0.0}, "positions_market_value": {}, "total_value": {}}
        payload = latest.get("payload")
        account = payload.get("account") if isinstance(payload, Mapping) else None
        if not isinstance(account, Mapping):
            return {"cash": {"unknown": 0.0}, "positions_market_value": {}, "total_value": {}}
        cash = _cash_by_currency(account)
        positions_market_value = _positions_market_value_by_currency(
            account,
            self._instrument_currencies(),
        )
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
        latest = self.store.load_latest_broker_account_snapshot()
        if latest is None:
            return {}
        payload = latest.get("payload")
        if not isinstance(payload, Mapping):
            return {}
        account = payload.get("account")
        prices = {
            str(symbol): float(price)
            for symbol, price in _mapping_items(payload.get("current_prices"))
            if _is_number(price)
        }
        currencies: dict[str, str] = {}
        if isinstance(account, Mapping):
            positions = account.get("positions")
            if isinstance(positions, list):
                for position in positions:
                    if not isinstance(position, Mapping):
                        continue
                    symbol = position.get("symbol")
                    if not isinstance(symbol, str):
                        continue
                    if symbol not in prices and _is_number(position.get("current_price")):
                        prices[symbol] = float(position["current_price"])
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
        latest = self.store.load_latest_broker_account_snapshot()
        if latest is None:
            return labels
        payload = latest.get("payload")
        if not isinstance(payload, Mapping):
            return labels
        account = payload.get("account")
        if not isinstance(account, Mapping):
            return labels
        positions = account.get("positions")
        if not isinstance(positions, list):
            return labels
        for position in positions:
            if not isinstance(position, Mapping):
                continue
            symbol = position.get("symbol")
            name = position.get("name")
            currency = _position_currency(position, self._instrument_currencies())
            if isinstance(symbol, str) and isinstance(name, str) and name and currency == "KRW":
                labels[symbol] = f"{symbol} {name}"
        return labels

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
    ) -> None:
        try:
            self.client.send_message(chat_id, text, reply_markup=reply_markup)
        except TypeError:
            self.client.send_message(chat_id, text)

    def _answer(self, callback: Mapping[str, Any], text: str) -> None:
        callback_id = callback.get("id")
        if not isinstance(callback_id, str):
            return
        answer_callback_query = getattr(self.client, "answer_callback_query", None)
        if callable(answer_callback_query):
            answer_callback_query(callback_id, text)

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
    for allocation in proposal.get("allocations") or []:
        strategy_id = str(_mapping(allocation).get("strategy_id") or "")
        if not strategy_id:
            continue
        keyboard.append(
            [
                {
                    "text": f"Assign {_telegram_strategy_display_name(strategy_id)}",
                    "callback_data": (
                        f"{OPERATOR_CALLBACK_PREFIX}cash-flow:assign:"
                        f"{proposal_id}:{strategy_id}"
                    ),
                }
            ]
        )
    return {"inline_keyboard": keyboard}


def _mask_identifier(value: object) -> str:
    text = str(value or "")
    if not text:
        return "none"
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


def _broker_snapshot_account_id(row: Mapping[str, Any]) -> str:
    payload = _mapping(row.get("payload"))
    account = _mapping(payload.get("account"))
    return str(
        row.get("account_id")
        or payload.get("account_id")
        or account.get("account_id")
        or ""
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
        command = _primary_signal_command(strategy.id)
        if command in seen:
            continue
        seen.add(command)
        commands.append(
            {
                "command": command,
                "description": f"Generate {_strategy_display_name(strategy.id)} signal",
            }
        )
    return commands


def _strategy_id_for_signal_command(command: str, config: MaestroConfig) -> str | None:
    command_name = command.removeprefix("/")
    matches = {
        name: strategy.id
        for strategy in config.strategies
        for name in _signal_command_names(strategy.id)
    }
    return matches.get(command_name)


def _signal_command_names(strategy_id: str) -> list[str]:
    return [_primary_signal_command(strategy_id)]


def _primary_signal_command(strategy_id: str) -> str:
    slug = _telegram_strategy_command_slug(strategy_id) or _telegram_command_slug(strategy_id)
    if slug.endswith("_us"):
        slug = slug.removesuffix("_us")
    return f"signal_{slug}"


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


def _strategy_display_name(strategy_id: str) -> str:
    return _telegram_strategy_display_name(strategy_id)


__all__ = ["TelegramOperatorCommandRouter", "telegram_bot_commands"]

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from maestro.config.models import MaestroConfig
from maestro.core.ids import new_run_id
from maestro.dashboard.read_models import (
    build_approvals_table,
    build_broker_account_summary,
    build_health_summary,
    build_orders_table,
    build_overview,
    build_portfolio_table,
    build_safety_state_card,
    build_strategy_runs_table,
)
from maestro.execution.broker_state import portfolio_state_from_broker_account
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.integrations.telegram.bot import TelegramBotClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.safety.controls import SafetyControlService
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.store import StateStore

OPERATOR_CALLBACK_PREFIX = "operator:"
TELEGRAM_OPERATOR_COMMANDS: tuple[tuple[str, str], ...] = (
    ("help", "Show Maestro command list"),
    ("status", "Show Maestro status summary"),
    ("health", "Show health checks"),
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
    ) -> None:
        self.config = config
        self.store = store
        self.audit = audit
        self.client = client

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

        handler = {
            f"/{command}": handler
            for command, handler in {
                "help": self._help,
                "status": self._status,
                "health": self._health,
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
        self._send(
            chat_id,
            "\n".join(
                [
                    "Maestro status",
                    f"mode: {self.config.mode.value}",
                    f"order_posture: {self.config.execution.order_posture}",
                    f"config: {operator_config.get('path', 'unknown')}",
                    f"config_fingerprint: {fingerprint[:12] if fingerprint != 'none' else 'none'}",
                    f"state: {state_path}",
                    f"audit: {audit_path}",
                    f"safety: {safety['state']}",
                    f"broker_total_value: {_money(broker['total_value'])}",
                    f"broker_cash: {_money(broker['cash'])}",
                    f"broker_positions: {broker['positions_count']}",
                    f"broker_snapshot_at: {broker['created_at'] or 'none'}",
                    f"orders: {overview['orders_count']}",
                    f"approvals: {overview['approvals_count']}",
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
                f"created_at: {account['created_at']}",
                f"account_id: {_mask_identifier(account['account_id'])}",
                f"total_value: {_money(account['total_value'])}",
                f"cash: {_money(account['cash'])}",
                f"positions_market_value: {_money(account['positions_market_value'])}",
                f"positions: {account['positions_count']}",
                f"source: {account['source'] or 'unknown'}",
            ]
        )
        self._send(
            chat_id,
            "\n".join(lines),
        )

    def _portfolio(self, chat_id: int) -> None:
        refresh_error: Exception | None = None
        try:
            if self.config.kis.enabled:
                self._send(chat_id, "Maestro portfolio: refreshing from broker snapshot")
            self._refresh_portfolio_from_broker_snapshot()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            refresh_error = exc
        rows = build_portfolio_table(self.store)
        lines = []
        if refresh_error is not None:
            lines.extend(
                [
                    f"Maestro portfolio refresh failed: {refresh_error}",
                    "Showing latest stored Maestro portfolio.",
                ]
            )
        lines.append("Maestro portfolio")
        for row in rows[:10]:
            lines.append(f"{row['symbol']}: {_number(row['quantity'])}")
        self._send(chat_id, "\n".join(lines))

    def _apps(self, chat_id: int) -> None:
        lines = ["Maestro apps"]
        for strategy in self.config.strategies[:10]:
            status = "on" if strategy.enabled else "off"
            if strategy.enabled:
                lines.append(f"{strategy.id}: {status} effective_mode={self.config.mode.value}")
            else:
                lines.append(f"{strategy.id}: {status}")
        latest_runs = build_strategy_runs_table(self.store, limit=5)
        if latest_runs:
            lines.append("")
            lines.append("Recent strategy runs")
            for row in latest_runs:
                ok = row["validation_ok"]
                lines.append(f"{row['strategy_id']}: validation_ok={ok}")
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
                f"orders={row['order_count']} notional={_money(row['estimated_notional'])}"
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


def _number(value: object) -> str:
    try:
        return f"{float(value):,.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "unknown"


def telegram_bot_commands() -> list[dict[str, str]]:
    return [
        {"command": command, "description": description}
        for command, description in TELEGRAM_OPERATOR_COMMANDS
    ]


__all__ = ["TelegramOperatorCommandRouter", "telegram_bot_commands"]

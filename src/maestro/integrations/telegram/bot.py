import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from maestro.approval.models import ApprovalDecision, ApprovalRequest
from maestro.core.clock import utc_now
from maestro.execution.live_orders import (
    LiveOrderLifecycleNotification,
    LiveOrderNotificationClient,
)
from maestro.integrations.telegram.formatter import format_approval_request


class TelegramBotClient(Protocol):
    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Send a Telegram message."""

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> Mapping[str, Any]:
        """Fetch Telegram updates."""


class TelegramBotAPIClient:
    def __init__(self, *, token_env: str, timeout_seconds: float = 10.0) -> None:
        token = os.getenv(token_env)
        if not token:
            raise ValueError(f"Telegram bot token environment variable is not set: {token_env}")
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout_seconds = timeout_seconds

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self._post("sendMessage", payload)

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> Mapping[str, Any]:
        payload: dict[str, Any] = {"timeout": timeout_seconds}
        if offset is not None:
            payload["offset"] = offset
        return self._post("getUpdates", payload)

    def _post(self, method: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        data = urllib.parse.urlencode(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{method}",
            data=data,
            headers={"User-Agent": "maestro-telegram/0.4"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise TimeoutError(f"Telegram Bot API timed out for method: {method}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram Bot API unavailable for method: {method}") from exc

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed Telegram response for method: {method}") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError(f"Malformed Telegram response for method: {method}")
        if not decoded.get("ok"):
            raise RuntimeError(f"Telegram Bot API returned not ok for method: {method}")
        return decoded


class TelegramApprovalNotifier:
    """No-network formatter used by non-Telegram approval providers."""

    def send_approval_request(self, request: ApprovalRequest) -> str:
        return format_approval_request(request)


class TelegramLiveOrderNotificationClient(LiveOrderNotificationClient):
    def __init__(self, *, client: TelegramBotClient, chat_ids: Sequence[int]) -> None:
        if not chat_ids:
            raise ValueError("telegram_allowed_chat_ids is required for live order notifications")
        self.client = client
        self.chat_ids = list(chat_ids)

    def notify(self, event: LiveOrderLifecycleNotification) -> None:
        text = _format_live_order_notification(event)
        for chat_id in self.chat_ids:
            self.client.send_message(chat_id, text)


class TelegramApprovalService:
    def __init__(
        self,
        *,
        client: TelegramBotClient,
        chat_ids: Sequence[int],
        allowed_user_ids: Sequence[int],
        poll_interval_seconds: float,
    ) -> None:
        if not chat_ids:
            raise ValueError("telegram_allowed_chat_ids is required for Telegram approvals")
        self.client = client
        self.chat_ids = list(chat_ids)
        self.allowed_user_ids = set(allowed_user_ids)
        self.poll_interval_seconds = poll_interval_seconds
        self._decided_approval_ids: set[str] = set()

    def request_decision(self, request: ApprovalRequest) -> tuple[ApprovalDecision, str]:
        message = format_approval_request(request)
        reply_markup = _approval_reply_markup(request.approval_id)
        for chat_id in self.chat_ids:
            self._send_message(chat_id, message, reply_markup)

        offset = None
        while utc_now() < request.expires_at:
            updates = self._updates(offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                decision = self._decision_from_update(update, request)
                if decision is not None:
                    self._decided_approval_ids.add(request.approval_id)
                    return decision, message
            if self.poll_interval_seconds > 0:
                time.sleep(self.poll_interval_seconds)

        decision = ApprovalDecision(
            approval_id=request.approval_id,
            run_id=request.run_id,
            status="expired",
            decided_at=utc_now(),
            decided_by="telegram:timeout",
            reason="Telegram approval timed out.",
        )
        self._decided_approval_ids.add(request.approval_id)
        return decision, message

    def _updates(self, offset: int | None) -> list[Mapping[str, Any]]:
        response = self.client.get_updates(offset=offset, timeout_seconds=0)
        updates = response.get("result")
        if not isinstance(updates, list):
            raise ValueError("Malformed Telegram updates response")
        return [item for item in updates if isinstance(item, Mapping)]

    def _decision_from_update(
        self, update: Mapping[str, Any], request: ApprovalRequest
    ) -> ApprovalDecision | None:
        if request.approval_id in self._decided_approval_ids:
            return None
        callback_decision = self._decision_from_callback_query(update, request)
        if callback_decision is not None:
            return callback_decision

        message = update.get("message")
        if not isinstance(message, Mapping):
            return None

        chat = message.get("chat")
        if not isinstance(chat, Mapping) or chat.get("id") not in self.chat_ids:
            return None

        user = message.get("from")
        if not isinstance(user, Mapping):
            return None
        user_id = user.get("id")
        if not isinstance(user_id, int):
            return None
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            return None

        text = message.get("text")
        if not isinstance(text, str):
            return None
        status = self._parse_status(text, request.approval_id)
        if status is None:
            return None

        username = user.get("username") if isinstance(user.get("username"), str) else None
        decided_by = f"telegram:{username or user_id}"
        return ApprovalDecision(
            approval_id=request.approval_id,
            run_id=request.run_id,
            status=status,
            decided_at=utc_now(),
            decided_by=decided_by,
            reason=f"Telegram {status} command.",
        )

    def _decision_from_callback_query(
        self,
        update: Mapping[str, Any],
        request: ApprovalRequest,
    ) -> ApprovalDecision | None:
        callback = update.get("callback_query")
        if not isinstance(callback, Mapping):
            return None

        message = callback.get("message")
        if not isinstance(message, Mapping):
            return None
        chat = message.get("chat")
        if not isinstance(chat, Mapping) or chat.get("id") not in self.chat_ids:
            return None

        user = callback.get("from")
        if not isinstance(user, Mapping):
            return None
        user_id = user.get("id")
        if not isinstance(user_id, int):
            return None
        if self.allowed_user_ids and user_id not in self.allowed_user_ids:
            return None

        data = callback.get("data")
        if not isinstance(data, str):
            return None
        status = self._parse_callback_status(data, request.approval_id)
        if status is None:
            return None

        username = user.get("username") if isinstance(user.get("username"), str) else None
        decided_by = f"telegram:{username or user_id}"
        return ApprovalDecision(
            approval_id=request.approval_id,
            run_id=request.run_id,
            status=status,
            decided_at=utc_now(),
            decided_by=decided_by,
            reason=f"Telegram button {status} callback.",
        )

    def _parse_status(self, text: str, approval_id: str) -> str | None:
        parts = text.strip().split()
        if len(parts) != 2:
            return None
        command = parts[0].lower().lstrip("/")
        if parts[1] != approval_id:
            return None
        if command == "approve":
            return "approved"
        if command == "reject":
            return "rejected"
        return None

    def _parse_callback_status(self, data: str, approval_id: str) -> str | None:
        command, separator, callback_approval_id = data.partition(":")
        if separator != ":" or callback_approval_id != approval_id:
            return None
        if command == "approve":
            return "approved"
        if command == "reject":
            return "rejected"
        return None

    def _send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            return self.client.send_message(chat_id, text, reply_markup=reply_markup)
        except TypeError:
            return self.client.send_message(chat_id, text)


def _approval_reply_markup(approval_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"approve:{approval_id}"},
                {"text": "Reject", "callback_data": f"reject:{approval_id}"},
            ]
        ]
    }


def _format_live_order_notification(event: LiveOrderLifecycleNotification) -> str:
    broker_order = event.broker_order_id or "pending"
    return "\n".join(
        [
            "Maestro live order update",
            f"run_id: {event.run_id}",
            f"order_id: {event.order_id}",
            f"broker_order_id: {broker_order}",
            f"status: {event.status.value}",
            f"message: {event.message}",
        ]
    )

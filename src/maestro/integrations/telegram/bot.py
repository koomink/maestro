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
from maestro.integrations.telegram.formatter import format_approval_request


class TelegramBotClient(Protocol):
    def send_message(self, chat_id: int, text: str) -> Mapping[str, Any]:
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

    def send_message(self, chat_id: int, text: str) -> Mapping[str, Any]:
        return self._post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
            },
        )

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
        for chat_id in self.chat_ids:
            self.client.send_message(chat_id, message)

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

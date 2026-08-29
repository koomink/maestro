import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from maestro.approval.models import ApprovalDecision, ApprovalRequest
from maestro.core.clock import utc_now
from maestro.core.enums import OrderStatus
from maestro.credentials import DEFAULT_CREDENTIAL_RESOLVER, CredentialResolver
from maestro.execution.live_orders import (
    LiveOrderBatchLifecycleResult,
    LiveOrderLifecycleNotification,
    LiveOrderNotificationClient,
)
from maestro.integrations.telegram.ui.cards import approval_detail_pages


class TelegramApiRejected(RuntimeError):
    """Telegram explicitly rejected this API operation.

    Separate from transport failures on purpose. A timeout or a dropped
    connection may have happened after Telegram accepted the message, and an
    unparseable body means it certainly answered, so those stay ambiguous.
    Only an explicit rejection is safe to retry automatically: only a rejected
    sendMessage proves that no new message was created, while an edit rejection
    requires evidence classification. Subclassing RuntimeError keeps every
    existing ``except RuntimeError`` caller working.
    """

    method: str | None
    error_code: int | None
    description: str | None

    def __init__(
        self,
        message: str | None = None,
        *,
        method: str | None = None,
        error_code: int | None = None,
        description: str | None = None,
    ) -> None:
        self.method = method
        self.error_code = error_code
        self.description = description
        text = (
            message
            if message is not None
            else (
                description
                if description is not None
                else (f"Telegram Bot API rejected method: {method}" if method is not None else "")
            )
        )
        super().__init__(text)


class TelegramBotClient(Protocol):
    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Send a Telegram message."""

    def get_updates(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
        allowed_updates: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        """Fetch Telegram updates."""

    def answer_callback_query(self, callback_query_id: str, text: str) -> Mapping[str, Any]:
        """Acknowledge a Telegram inline button callback."""

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Edit a Telegram message."""

    def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        """Edit only a Telegram message's inline keyboard."""

    def set_my_commands(self, commands: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        """Register bot slash commands."""


class TelegramBotAPIClient:
    def __init__(
        self,
        *,
        token_env: str,
        timeout_seconds: float = 10.0,
        credential_resolver: CredentialResolver | None = None,
    ) -> None:
        credentials = credential_resolver or DEFAULT_CREDENTIAL_RESOLVER
        token = credentials.get(token_env)
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

    def get_updates(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
        allowed_updates: Sequence[str] | None = None,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {"timeout": timeout_seconds}
        if offset is not None:
            payload["offset"] = offset
        if allowed_updates is not None:
            payload["allowed_updates"] = json.dumps(list(allowed_updates))
        return self._post("getUpdates", payload)

    def answer_callback_query(self, callback_query_id: str, text: str) -> Mapping[str, Any]:
        return self._post(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text,
            },
        )

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self._post("editMessageText", payload)

    def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup)
        return self._post("editMessageReplyMarkup", payload)

    def set_my_commands(self, commands: Sequence[Mapping[str, str]]) -> Mapping[str, Any]:
        return self._post("setMyCommands", {"commands": json.dumps(list(commands))})

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
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8")
            except Exception:
                raise RuntimeError(f"Telegram Bot API unavailable for method: {method}") from exc
            try:
                decoded = json.loads(body)
            except json.JSONDecodeError:
                raise RuntimeError(f"Telegram Bot API unavailable for method: {method}") from exc
            if not isinstance(decoded, Mapping):
                raise RuntimeError(f"Telegram Bot API unavailable for method: {method}") from exc
            return self._handle_decoded_payload(method, decoded)
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Telegram Bot API unavailable for method: {method}") from exc

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Malformed Telegram response for method: {method}") from exc
        if not isinstance(decoded, Mapping):
            raise ValueError(f"Malformed Telegram response for method: {method}")
        return self._handle_decoded_payload(method, decoded)

    def _handle_decoded_payload(self, method: str, decoded: Mapping[str, Any]) -> Mapping[str, Any]:
        if not decoded.get("ok"):
            raw_code = decoded.get("error_code")
            error_code = (
                raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
            )
            raw_description = decoded.get("description")
            description = raw_description if isinstance(raw_description, str) else None
            raise TelegramApiRejected(
                method=method,
                error_code=error_code,
                description=description,
            )
        return decoded


class TelegramApprovalNotifier:
    """No-network formatter used by non-Telegram approval providers."""

    def send_approval_request(self, request: ApprovalRequest) -> str:
        # 이 경로에는 자세히 버튼도 길이 제한도 없다. 감사 기록에 남는 문구가
        # 요약으로 줄어들지 않도록 펼친 뷰 전체를 이어 붙인다.
        return "\n\n".join(approval_detail_pages(request))


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

    def notify_batch(self, result: LiveOrderBatchLifecycleResult) -> None:
        text = _format_live_order_batch(result)
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

    def request_decision(self, request: ApprovalRequest) -> tuple[ApprovalDecision, str]:
        # sync 경로에는 ui:d/ui:p callback을 처리할 라우터가 없다. 승인 전에 숨겨진
        # 주문·위험 사유가 남지 않도록 펼친 뷰 전체를 쪽별 메시지로 보내고,
        # 승인/거절 버튼은 마지막 메시지에만 붙인다.
        pages = approval_detail_pages(request)
        reply_markup = _approval_reply_markup(request.approval_id)
        message = "\n\n".join(pages)
        for chat_id in self.chat_ids:
            for index, page in enumerate(pages):
                self._send_message(
                    chat_id,
                    page,
                    reply_markup if index == len(pages) - 1 else None,
                )

        offset = None
        while utc_now() < request.expires_at:
            updates = self._updates(offset)
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                decision = self._decision_from_update(update, request)
                if decision is not None:
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
            self._answer_callback_query(callback, "This approval request is no longer active.")
            return None

        username = user.get("username") if isinstance(user.get("username"), str) else None
        decided_by = f"telegram:{username or user_id}"
        decision = ApprovalDecision(
            approval_id=request.approval_id,
            run_id=request.run_id,
            status=status,
            decided_at=utc_now(),
            decided_by=decided_by,
            reason=f"Telegram button {status} callback.",
        )
        self._answer_callback_query(callback, f"Approval {status}.")
        self._edit_callback_message(callback, decision)
        return decision

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
        reply_markup: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        try:
            return self.client.send_message(chat_id, text, reply_markup=reply_markup)
        except TypeError:
            return self.client.send_message(chat_id, text)

    def _answer_callback_query(self, callback: Mapping[str, Any], text: str) -> None:
        callback_id = callback.get("id")
        if not isinstance(callback_id, str):
            return
        answer_callback_query = getattr(self.client, "answer_callback_query", None)
        if not callable(answer_callback_query):
            return
        try:
            answer_callback_query(callback_id, text)
        except (RuntimeError, TimeoutError, TypeError, ValueError):
            return

    def _edit_callback_message(
        self,
        callback: Mapping[str, Any],
        decision: ApprovalDecision,
    ) -> None:
        message = callback.get("message")
        if not isinstance(message, Mapping):
            return
        chat = message.get("chat")
        if not isinstance(chat, Mapping):
            return
        chat_id = chat.get("id")
        message_id = message.get("message_id")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return

        reply_markup = _decision_reply_markup(decision.status, decision.approval_id)
        if self._edit_decision_text(message, chat_id, message_id, decision, reply_markup):
            return
        if self._edit_decision_reply_markup(chat_id, message_id, reply_markup):
            return
        self._send_decision_confirmation(chat_id, decision)

    def _edit_decision_text(
        self,
        message: Mapping[str, Any],
        chat_id: int,
        message_id: int,
        decision: ApprovalDecision,
        reply_markup: Mapping[str, Any],
    ) -> bool:
        text = message.get("text")
        edit_message_text = getattr(self.client, "edit_message_text", None)
        if not isinstance(text, str) or not callable(edit_message_text):
            return False
        updated_text = _append_decision_log(text, decision)
        try:
            edit_message_text(
                chat_id,
                message_id,
                updated_text,
                reply_markup=reply_markup,
            )
        except (RuntimeError, TimeoutError, TypeError, ValueError):
            return False
        return True

    def _edit_decision_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: Mapping[str, Any],
    ) -> bool:
        edit_message_reply_markup = getattr(self.client, "edit_message_reply_markup", None)
        if not callable(edit_message_reply_markup):
            return False
        try:
            edit_message_reply_markup(chat_id, message_id, reply_markup=reply_markup)
        except (RuntimeError, TimeoutError, TypeError, ValueError):
            return False
        return True

    def _send_decision_confirmation(self, chat_id: int, decision: ApprovalDecision) -> None:
        try:
            self.client.send_message(chat_id, _decision_confirmation_text(decision))
        except (RuntimeError, TimeoutError, TypeError, ValueError):
            return


def _approval_reply_markup(approval_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": f"approve:{approval_id}"},
                {"text": "Reject", "callback_data": f"reject:{approval_id}"},
            ]
        ]
    }


def _decision_reply_markup(status: str, approval_id: str) -> dict[str, Any]:
    label = "✅ Approved" if status == "approved" else "⛔ Rejected"
    return {"inline_keyboard": [[{"text": label, "callback_data": f"decision:{approval_id}"}]]}


def _decision_confirmation_text(decision: ApprovalDecision) -> str:
    return "\n".join(
        [
            "Maestro approval decision recorded",
            f"approval_id: {decision.approval_id}",
            f"run_id: {decision.run_id}",
            f"status: {decision.status}",
            f"decided_by: {decision.decided_by}",
            f"decided_at: {decision.decided_at.isoformat()}",
        ]
    )


def _append_decision_log(text: str, decision: ApprovalDecision) -> str:
    base_text = text.split("\n\nDecision:", 1)[0]
    return "\n\n".join(
        [
            base_text,
            (
                f"Decision: {decision.status} by {decision.decided_by} "
                f"at {decision.decided_at.isoformat()}"
            ),
        ]
    )


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


def _format_live_order_batch(result: LiveOrderBatchLifecycleResult) -> str:
    lines = [
        "Maestro live order batch summary",
        f"run_id: {result.run_id}",
        f"poll_rounds: {result.poll_rounds}",
    ]
    modify_commands = []
    for index, item in enumerate(result.items, start=1):
        request = item.request
        lifecycle = item.lifecycle
        latest = lifecycle.status_snapshots[-1] if lifecycle.status_snapshots else None
        filled = latest.partial_fill.filled_quantity if latest else 0.0
        remaining = latest.partial_fill.remaining_quantity if latest else request.quantity
        lines.extend(
            [
                "",
                f"{index}. {request.account_id or 'default'} {request.symbol}",
                f"broker_order_id: {lifecycle.broker_order_id or 'pending'}",
                f"status: {lifecycle.final_status.value}",
                (
                    f"quantity: {request.quantity:g} filled: {filled:g} "
                    f"remaining: {remaining:g}"
                ),
                f"limit_price: {request.limit_price:g}",
            ]
        )
        if (
            lifecycle.broker_order_id
            and remaining > 0
            and lifecycle.final_status
            in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
        ):
            modify_commands.append(
                f"/modify {lifecycle.broker_order_id} <price> {remaining:g}"
            )
    if modify_commands:
        lines.extend(["", "Remaining orders can be modified with:", *modify_commands])
    return "\n".join(lines)

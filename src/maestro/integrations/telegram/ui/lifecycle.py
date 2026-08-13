"""The only stateful component in ``ui/``.

``cards.py`` renders and knows nothing else; this module owns the store and the
Telegram client. Every send records its intent first, so a process that dies
between the API call and the write leaves a visible copy of unknown delivery
instead of a card nobody will ever update again.
"""

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from maestro.integrations.telegram.bot import TelegramApiRejected
from maestro.integrations.telegram.ui.card_state import (
    EVENT_TYPE,
    card_failure_event,
    card_intent_event,
    card_result_event,
    new_operation_id,
)
from maestro.integrations.telegram.ui.cards import RenderedCard


class CardLifecycleManager:
    def __init__(
        self,
        store: Any,
        audit: Any,
        client: Any,
        *,
        chat_ids: Sequence[int],
    ) -> None:
        self.store = store
        self.audit = audit
        self.client = client
        self.chat_ids = tuple(chat_ids)

    @staticmethod
    def render_hash(rendered: RenderedCard) -> str:
        payload = f"{rendered.text}\x00{rendered.reply_markup!r}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def deliver(
        self,
        run_id: str,
        card_key: str,
        stage: str,
        rendered: RenderedCard,
    ) -> dict[str, Any]:
        """Send this card to every chat, one delivery copy at a time."""
        render_hash = self.render_hash(rendered)
        sent: list[int] = []
        failed: list[int] = []
        unknown: list[int] = []
        for chat_id in self.chat_ids:
            outcome = self._deliver_one(run_id, card_key, stage, rendered, render_hash, chat_id)
            {"sent": sent, "failed": failed, "unknown": unknown}[outcome].append(chat_id)
        return {
            "sent": tuple(sent),
            "failed": tuple(failed),
            "unknown": tuple(unknown),
        }

    def _deliver_one(
        self,
        run_id: str,
        card_key: str,
        stage: str,
        rendered: RenderedCard,
        render_hash: str,
        chat_id: int,
    ) -> str:
        """One chat's copy. Returns "sent", "failed" or "unknown"."""
        operation_id = new_operation_id()
        self.store.save_system_event(
            run_id,
            EVENT_TYPE,
            card_intent_event(card_key, chat_id, stage, render_hash, operation_id),
        )
        try:
            response = self._send(chat_id, rendered)
        except TelegramApiRejected as exc:
            # ok=false. Telegram looked at it and refused, so nothing was
            # delivered. This is the *only* exception that makes an attempt
            # safe to retry automatically.
            self.store.save_system_event(
                run_id,
                EVENT_TYPE,
                card_failure_event(
                    card_key, chat_id, stage, render_hash, operation_id, str(exc)
                ),
            )
            return "failed"
        except Exception:  # noqa: BLE001 - one chat must not stop the rest
            # Timeout, dropped connection, unparseable body: any of these can
            # happen after Telegram accepted the message. Leaving the intent
            # without an outcome is what marks it unknown, and unknown is never
            # resent. Writing a failure here is exactly the misclassification
            # that duplicates approval cards.
            return "unknown"
        message_id = self._message_id(response)
        if message_id is None:
            # ok=true with no message_id means we cannot address the message we
            # probably just created. Unknown, not failed.
            return "unknown"
        self.store.save_system_event(
            run_id,
            EVENT_TYPE,
            card_result_event(
                card_key, chat_id, stage, render_hash, operation_id, message_id
            ),
        )
        return "sent"

    def _send(self, chat_id: int, rendered: RenderedCard) -> Mapping[str, Any] | None:
        try:
            return self.client.send_message(
                chat_id, rendered.text, reply_markup=rendered.reply_markup
            )
        except TypeError:
            return self.client.send_message(chat_id, rendered.text)

    @staticmethod
    def _message_id(response: Mapping[str, Any] | None) -> int | None:
        if not isinstance(response, Mapping):
            return None
        value = response.get("message_id")
        return value if isinstance(value, int) else None

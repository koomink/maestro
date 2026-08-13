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
from maestro.integrations.telegram.ui import catalog
from maestro.integrations.telegram.ui.card_state import (
    FALLBACK_AFTER_FAILURES,
    CardCopy,
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
        self.store.record_card_event(
            run_id, card_intent_event(card_key, chat_id, stage, render_hash, operation_id)
        )
        try:
            response = self._send(chat_id, rendered)
        except TelegramApiRejected as exc:
            # ok=false. Telegram looked at it and refused, so nothing was
            # delivered. This is the *only* exception that makes an attempt
            # safe to retry automatically.
            self.store.record_card_event(
                run_id,
                card_failure_event(
                    card_key, chat_id, stage, render_hash, operation_id, str(exc)
                ),
            )
            if self.consecutive_failures(card_key, chat_id) >= FALLBACK_AFTER_FAILURES:
                self._escalate_repeated_failures(run_id, card_key, chat_id, stage)
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
        self.store.record_card_event(
            run_id,
            card_result_event(
                card_key, chat_id, stage, render_hash, operation_id, message_id
            ),
        )
        return "sent"

    def refresh(
        self,
        run_id: str,
        card_key: str,
        stage: str,
        rendered: RenderedCard,
    ) -> dict[str, Any]:
        """Bring every delivery copy up to the current stage."""
        render_hash = self.render_hash(rendered)
        copies = self.copies(card_key)
        edited: list[int] = []
        skipped: list[int] = []
        sent: list[int] = []
        failed: list[int] = []
        ambiguous: list[int] = []
        for chat_id in self.chat_ids:
            copy = copies.get((card_key, chat_id))
            if (
                copy is not None
                and copy.delivery == "confirmed"
                and copy.render_hash == render_hash
            ):
                skipped.append(chat_id)
                continue
            if copy is not None and copy.delivery == "unknown":
                # We do not know whether Telegram already has this card, and
                # the Bot API gives us no way to ask. Sending again is how a
                # duplicate is created; staying silent is how one is avoided.
                # The operator hears about it through the plain-text path,
                # which does not add a second button-bearing card.
                ambiguous.append(chat_id)
                self._escalate_ambiguous(run_id, card_key, chat_id, stage)
                continue
            if copy is None or copy.message_id is None:
                outcome = self._deliver_one(
                    run_id, card_key, stage, rendered, render_hash, chat_id
                )
                {"sent": sent, "failed": failed, "unknown": ambiguous}[outcome].append(chat_id)
                continue
            operation_id = new_operation_id()
            self.store.record_card_event(
                run_id, card_intent_event(card_key, chat_id, stage, render_hash, operation_id)
            )
            try:
                self.client.edit_message_text(
                    chat_id,
                    copy.message_id,
                    rendered.text,
                    reply_markup=rendered.reply_markup,
                )
            except TelegramApiRejected as exc:
                # Telegram refused the edit -- a 48h-expired or deleted message
                # is the documented case. We still hold no doubt about what
                # happened, so a fresh send is the right fallback.
                self.store.record_card_event(
                    run_id,
                    card_failure_event(
                        card_key, chat_id, stage, render_hash, operation_id, str(exc)
                    ),
                )
                outcome = self._deliver_one(
                    run_id, card_key, stage, rendered, render_hash, chat_id
                )
                {"sent": sent, "failed": failed, "unknown": ambiguous}[outcome].append(chat_id)
                continue
            except Exception:  # noqa: BLE001 - transport: we do not know
                ambiguous.append(chat_id)
                continue
            self.store.record_card_event(
                run_id,
                card_result_event(
                    card_key, chat_id, stage, render_hash, operation_id, copy.message_id
                ),
            )
            edited.append(chat_id)
        return {
            "edited": tuple(edited),
            "skipped": tuple(skipped),
            "sent": tuple(sent),
            "failed": tuple(failed),
            "ambiguous": tuple(ambiguous),
        }

    def copies(self, card_key: str) -> dict[tuple[str, int], CardCopy]:
        """Read the projection, not the log."""
        return {
            (row["card_key"], row["chat_id"]): CardCopy(**row)
            for row in self.store.load_card_delivery_state(card_key)
        }

    def consecutive_failures(self, card_key: str, chat_id: int) -> int:
        copy = self.copies(card_key).get((card_key, chat_id))
        return copy.consecutive_failures if copy is not None else 0

    def _escalate_ambiguous(
        self, run_id: str, card_key: str, chat_id: int, stage: str
    ) -> None:
        """Tell the operator, once, that we cannot account for this copy.

        Plain text with no buttons on purpose. Re-rendering the card is exactly
        what we refused to do, and a second button-bearing card for one
        approval is the outcome this whole path exists to avoid.

        Deduplicated per (card_key, chat_id, stage): the sweep runs every poll,
        and an ambiguous copy stays ambiguous until a human looks at it.
        """
        notice_key = f"telegram-ui-card-ambiguous:{card_key}:{chat_id}:{stage}"
        if self.store.duplicate_key_exists(notice_key):
            return
        try:
            self.client.send_message(
                chat_id, catalog.CARD_AMBIGUOUS_TEMPLATE.format(card_key=card_key, stage=stage)
            )
        except Exception:  # noqa: BLE001 - the sweep must keep going
            return
        self.store.save_system_event(
            run_id,
            "telegram_ui_card_ambiguous",
            {"card_key": card_key, "chat_id": chat_id, "stage": stage,
             "duplicate_key": notice_key},
        )

    def _escalate_repeated_failures(
        self, run_id: str, card_key: str, chat_id: int, stage: str
    ) -> None:
        """After three straight failures, say it without the renderer.

        If rendering is what keeps failing, rendering the fallback would fail
        too, so this is a fixed template carrying only the card identity.
        """
        notice_key = f"telegram-ui-card-fallback:{card_key}:{chat_id}:{stage}"
        if self.store.duplicate_key_exists(notice_key):
            return
        try:
            self.client.send_message(
                chat_id, catalog.CARD_FALLBACK_TEMPLATE.format(card_key=card_key, stage=stage)
            )
        except Exception:  # noqa: BLE001 - the sweep must keep going
            return
        self.store.save_system_event(
            run_id,
            "telegram_ui_card_fallback",
            {"card_key": card_key, "chat_id": chat_id, "stage": stage,
             "duplicate_key": notice_key},
        )

    def _send(self, chat_id: int, rendered: RenderedCard) -> Mapping[str, Any] | None:
        try:
            return self.client.send_message(
                chat_id, rendered.text, reply_markup=rendered.reply_markup
            )
        except TypeError:
            return self.client.send_message(chat_id, rendered.text)

    @staticmethod
    def _message_id(response: Mapping[str, Any] | None) -> int | None:
        """Accept both the raw Bot API envelope and an already-unwrapped result.

        TelegramBotAPIClient._post returns what Telegram sent -- ``{"ok": true,
        "result": {"message_id": ...}}`` -- while notification helpers pass the
        result alone. Looking only at the top level would leave every real card
        without a message_id: unknown forever, never editable, and escalated as
        ambiguous on the next sweep.
        """
        if not isinstance(response, Mapping):
            return None
        value = response.get("message_id")
        if isinstance(value, int):
            return value
        result = response.get("result")
        if isinstance(result, Mapping):
            nested = result.get("message_id")
            if isinstance(nested, int):
                return nested
        return None

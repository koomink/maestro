from maestro.integrations.telegram.bot import TelegramApiRejected
from maestro.integrations.telegram.ui.card_state import resolve_card_copies
from maestro.integrations.telegram.ui.cards import RenderedCard
from maestro.integrations.telegram.ui.lifecycle import CardLifecycleManager
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore

CARD = RenderedCard(text="📩 승인해 주세요", reply_markup=None)


class FakeClient:
    def __init__(self, *, reject_for: set[int] | None = None):
        self.sent: list[tuple[int, str]] = []
        self.edited: list[tuple[int, int, str]] = []
        # reject_for raises TelegramApiRejected -- a *known* non-delivery.
        # Transport ambiguity is modelled by the dedicated clients below.
        self.reject_for = reject_for or set()
        self.next_message_id = 5000

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.reject_for:
            raise TelegramApiRejected(f"telegram refused chat {chat_id}")
        self.sent.append((chat_id, text))
        self.next_message_id += 1
        return {"message_id": self.next_message_id}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append((chat_id, message_id, text))
        return {"message_id": message_id}


def _manager(tmp_path, client, chat_ids=(100, 200)):
    store = StateStore(str(tmp_path / "state.db"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    return store, CardLifecycleManager(store, audit, client, chat_ids=chat_ids)


def _copies_from_store(store, card_key="approval:appr_1"):
    """Read back the way lifecycle.py does: newest-first, so reverse it."""
    from maestro.integrations.telegram.ui.card_state import EVENT_TYPE

    rows = store.list_system_events_by_type(EVENT_TYPE, limit=None)
    return resolve_card_copies([row["payload"] for row in reversed(rows)])


def test_delivery_writes_intent_before_calling_telegram(tmp_path):
    """The intent must be durable before the side effect, not after it."""
    order: list[str] = []

    class RecordingClient(FakeClient):
        def send_message(self, chat_id, text, reply_markup=None):
            order.append("send")
            return super().send_message(chat_id, text, reply_markup)

    store, manager = _manager(tmp_path, RecordingClient(), chat_ids=(100,))
    from maestro.integrations.telegram.ui.card_state import EVENT_TYPE

    original = store.save_system_event

    def spy(run_id, event_type, payload):
        if event_type == EVENT_TYPE:
            order.append(str(payload.get("phase")))
        return original(run_id, event_type, payload)

    store.save_system_event = spy

    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert order == ["intent", "send", "result"]


def test_a_timeout_after_telegram_accepted_stays_unknown(tmp_path):
    """The window the spec amendment exists to make visible.

    A timeout does not tell us the message failed -- Telegram may hold it and
    only the reply was lost. Recording a failure here is what would license a
    resend and duplicate an approval card.
    """

    class TimingOutClient(FakeClient):
        def send_message(self, chat_id, text, reply_markup=None):
            super().send_message(chat_id, text, reply_markup)
            raise TimeoutError("Telegram Bot API timed out for method: sendMessage")

    store, manager = _manager(tmp_path, TimingOutClient(), chat_ids=(100,))

    result = manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert result["unknown"] == (100,)
    assert result["failed"] == ()
    copy = _copies_from_store(store)[("approval:appr_1", 100)]
    assert copy.delivery == "unknown"
    assert copy.message_id is None


def test_an_explicit_rejection_is_recorded_as_a_failure(tmp_path):
    """ok=false is the one exception that proves nothing was delivered."""
    store, manager = _manager(tmp_path, FakeClient(reject_for={100}), chat_ids=(100,))

    result = manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert result["failed"] == (100,)
    assert _copies_from_store(store)[("approval:appr_1", 100)].delivery == "failed"


def test_an_ok_response_without_a_message_id_is_unknown(tmp_path):
    """We cannot address what we probably just created."""

    class NoIdClient(FakeClient):
        def send_message(self, chat_id, text, reply_markup=None):
            return {"ok": True}

    store, manager = _manager(tmp_path, NoIdClient(), chat_ids=(100,))

    assert manager.deliver("run_1", "approval:appr_1", "pending", CARD)["unknown"] == (100,)


def test_every_chat_gets_its_own_copy(tmp_path):
    store, manager = _manager(tmp_path, FakeClient())

    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    copies = _copies_from_store(store)
    assert sorted(chat_id for _, chat_id in copies) == [100, 200]
    assert (
        copies[("approval:appr_1", 100)].message_id
        != copies[("approval:appr_1", 200)].message_id
    )


def test_one_rejected_chat_does_not_block_the_others(tmp_path):
    """The reminder path already works this way; the card path must too."""
    store, manager = _manager(tmp_path, FakeClient(reject_for={100}))

    result = manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert result["sent"] == (200,)
    assert result["failed"] == (100,)
    copies = _copies_from_store(store)
    assert copies[("approval:appr_1", 100)].delivery == "failed"
    assert copies[("approval:appr_1", 200)].delivery == "confirmed"


def test_the_render_hash_is_stable_for_equal_content(tmp_path):
    _, manager = _manager(tmp_path, FakeClient())
    same = RenderedCard(text=CARD.text, reply_markup=None)

    assert manager.render_hash(CARD) == manager.render_hash(same)

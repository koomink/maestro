from maestro.integrations.telegram.bot import TelegramApiRejected
from maestro.integrations.telegram.ui import catalog
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
    original = store.record_card_event

    def spy(run_id, payload):
        order.append(str(payload.get("phase")))
        return original(run_id, payload)

    store.record_card_event = spy

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


def test_the_real_bot_api_envelope_yields_a_message_id(tmp_path):
    """TelegramBotAPIClient returns the whole envelope, not the result alone.

    _post hands back {"ok": true, "result": {"message_id": ...}} (bot.py:159).
    Reading only a top-level message_id makes every production card unknown --
    never editable, and escalated as ambiguous on the very next sweep. The
    other fakes here flatten the response and cannot see that.
    """

    class EnvelopeClient(FakeClient):
        def send_message(self, chat_id, text, reply_markup=None):
            flat = super().send_message(chat_id, text, reply_markup)
            return {"ok": True, "result": {"message_id": flat["message_id"]}}

    store, manager = _manager(tmp_path, EnvelopeClient(), chat_ids=(100,))

    result = manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert result["sent"] == (100,)
    copy = _copies_from_store(store)[("approval:appr_1", 100)]
    assert copy.delivery == "confirmed"
    assert copy.message_id == 5001


def test_every_chat_gets_its_own_copy(tmp_path):
    store, manager = _manager(tmp_path, FakeClient())

    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    copies = _copies_from_store(store)
    assert sorted(chat_id for _, chat_id in copies) == [100, 200]
    assert (
        copies[("approval:appr_1", 100)].message_id != copies[("approval:appr_1", 200)].message_id
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


def test_refresh_edits_the_existing_message_per_chat(tmp_path):
    client = FakeClient()
    store, manager = _manager(tmp_path, client)
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh("run_1", "approval:appr_1", "in_progress", progressed)

    assert sorted(result["edited"]) == [100, 200]
    assert [chat_id for chat_id, _, _ in client.edited] == [100, 200]


def test_an_unchanged_render_is_not_sent_again(tmp_path):
    """Telegram answers 'message is not modified' and it costs an API call."""
    client = FakeClient()
    _, manager = _manager(tmp_path, client)
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    result = manager.refresh("run_1", "approval:appr_1", "pending", CARD)

    assert result["edited"] == ()
    assert result["skipped"] == (100, 200)
    assert client.edited == []


def test_a_known_failed_copy_is_sent_again(tmp_path):
    """A rejection means it never landed, so retrying is safe."""
    client = FakeClient(reject_for={100})
    store, manager = _manager(tmp_path, client)
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    client.reject_for = set()
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh("run_1", "approval:appr_1", "in_progress", progressed)

    assert 100 in result["sent"]
    assert 200 in result["edited"]


def test_an_ambiguous_copy_is_never_resent(tmp_path):
    """The crash window: Telegram may already hold this card.

    Resending would post a second card we have no message_id for, so it never
    updates -- it sits at the old stage forever while the real one moves on.
    For an approval card with buttons and a deadline, an operator reading the
    stale copy concludes the decision is still outstanding.
    """
    from maestro.integrations.telegram.ui.card_state import card_intent_event

    client = FakeClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    # Only an intent: exactly what a process death mid-send leaves behind.
    store.record_card_event(
        "run_1", card_intent_event("approval:appr_1", 100, "pending", "h1", "op-crashed")
    )
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh("run_1", "approval:appr_1", "in_progress", progressed)

    assert result["ambiguous"] == (100,)
    assert client.edited == []
    # The card itself is never re-sent; the operator gets a plain-text notice
    # instead, so one approval never grows a second set of buttons.
    assert [text for _, text in client.sent] == [
        catalog.CARD_AMBIGUOUS_TEMPLATE.format(card_key="approval:appr_1", stage="in_progress")
    ]


def test_the_ambiguous_notice_is_sent_only_once(tmp_path):
    """The sweep runs every poll and the copy stays ambiguous until a human
    looks; repeating the notice every two minutes would bury it."""
    from maestro.integrations.telegram.ui.card_state import card_intent_event

    client = FakeClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    store.record_card_event(
        "run_1", card_intent_event("approval:appr_1", 100, "pending", "h1", "op-crashed")
    )
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    manager.refresh("run_1", "approval:appr_1", "in_progress", progressed)
    manager.refresh("run_1", "approval:appr_1", "in_progress", progressed)

    assert len(client.sent) == 1


def test_a_delivered_card_survives_a_round_trip_through_the_store(tmp_path):
    """The integration failure a hand-built event list cannot show.

    Reconstructing state by folding recent events gets the direction wrong or
    runs past a limit; the projection is read by key and does neither.
    """
    client = FakeClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    sent_after_first = len(client.sent)

    result = manager.refresh("run_1", "approval:appr_1", "pending", CARD)

    assert len(client.sent) == sent_after_first
    assert result["skipped"] == (100,)
    assert result["ambiguous"] == ()


def test_a_card_older_than_any_event_window_is_still_found(tmp_path):
    """An approval can wait hours while sweeps append events for other cards.

    Scanning the newest N events would lose this card and post a duplicate.
    """
    client = FakeClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "approval:appr_old", "pending", CARD)
    for index in range(1200):
        manager.deliver("run_noise", f"daily:noise_{index}", "pending", CARD)
    sent_before = len(client.sent)

    result = manager.refresh("run_1", "approval:appr_old", "pending", CARD)

    assert result["skipped"] == (100,)
    assert len(client.sent) == sent_before


def test_an_edit_rejection_falls_back_to_a_new_message_under_default_policy(tmp_path):
    """Under default replace_on_rejection policy, any edit rejection triggers replacement."""

    class EditRejectingClient(FakeClient):
        def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
            raise TelegramApiRejected("message to edit not found")

    client = EditRejectingClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh("run_1", "approval:appr_1", "in_progress", progressed)

    assert result["sent"] == (100,)
    copies = manager.copies("approval:appr_1")
    assert copies[("approval:appr_1", 100)].message_id == client.next_message_id


def test_edit_rejection_message_not_modified_converges_without_send(tmp_path):
    """replace_on_target_absence: 'message is not modified' converges on existing message_id."""

    class NotModifiedClient(FakeClient):
        def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
            raise TelegramApiRejected(
                method="editMessageText",
                error_code=400,
                description="Bad Request: message is not modified",
            )

    client = NotModifiedClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "funding-workflow:w1", "pending", CARD)
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh(
        "run_1",
        "funding-workflow:w1",
        "in_progress",
        progressed,
        edit_replacement_policy="replace_on_target_absence",
    )

    assert result["edited"] == (100,)
    assert result["sent"] == ()
    assert len(client.sent) == 1  # only initial send
    copy = manager.copies("funding-workflow:w1")[("funding-workflow:w1", 100)]
    assert copy.delivery == "confirmed"
    assert copy.message_id == 5001
    assert copy.stage == "in_progress"
    assert copy.render_hash == manager.render_hash(progressed)


def test_edit_rejection_message_not_found_replaces_once(tmp_path):
    """replace_on_target_absence: 'message to edit not found' triggers a single replacement send."""

    class NotFoundClient(FakeClient):
        def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
            raise TelegramApiRejected(
                method="editMessageText",
                error_code=400,
                description="Bad Request: message to edit not found",
            )

    client = NotFoundClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "funding-workflow:w1", "pending", CARD)
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh(
        "run_1",
        "funding-workflow:w1",
        "in_progress",
        progressed,
        edit_replacement_policy="replace_on_target_absence",
    )

    assert result["sent"] == (100,)
    assert len(client.sent) == 2  # initial + 1 replacement
    copy = manager.copies("funding-workflow:w1")[("funding-workflow:w1", 100)]
    assert copy.delivery == "confirmed"
    assert copy.message_id == client.next_message_id


def test_generic_edit_rejection_records_failure_and_retries_edit(tmp_path):
    """replace_on_target_absence: generic rejection records failure, keeps message_id,
    retries edit on next sweep.
    """
    should_reject = True

    class GenericRejectingClient(FakeClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.edit_attempts: list[tuple[int, int, str]] = []

        def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
            self.edit_attempts.append((chat_id, message_id, text))
            if should_reject:
                raise TelegramApiRejected(
                    method="editMessageText",
                    error_code=400,
                    description="Bad Request: can't parse entities",
                )
            return super().edit_message_text(chat_id, message_id, text, reply_markup)

    client = GenericRejectingClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "funding-workflow:w1", "pending", CARD)
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh(
        "run_1",
        "funding-workflow:w1",
        "in_progress",
        progressed,
        edit_replacement_policy="replace_on_target_absence",
    )

    assert result["failed"] == (100,)
    assert result["sent"] == ()
    assert len(client.sent) == 1  # no replacement send
    copy = manager.copies("funding-workflow:w1")[("funding-workflow:w1", 100)]
    assert copy.delivery == "failed"
    assert copy.message_id == 5001  # preserved!

    # Check failure event payload in store
    rows = store.list_system_events_by_type("telegram_ui_card", limit=None)
    failure_row = next(r for r in rows if r["payload"]["phase"] == "failure")
    assert failure_row["payload"]["method"] == "editMessageText"
    assert failure_row["payload"]["error_code"] == 400
    assert failure_row["payload"]["description"] == "Bad Request: can't parse entities"

    # Subsequent sweep retries the edit (not send)
    should_reject = False
    sweep_result = manager.refresh(
        "run_1",
        "funding-workflow:w1",
        "in_progress",
        progressed,
        edit_replacement_policy="replace_on_target_absence",
    )
    assert sweep_result["edited"] == (100,)
    assert len(client.sent) == 1  # still no new send
    assert len(client.edit_attempts) == 2  # 1 failed + 1 successful edit attempt
    assert client.edit_attempts[0] == (100, 5001, progressed.text)
    assert client.edit_attempts[1] == (100, 5001, progressed.text)
    assert client.edited == [(100, 5001, progressed.text)]


def test_edit_timeout_leaves_unknown_and_emits_ambiguous_notice(tmp_path):
    """replace_on_target_absence: TimeoutError on edit leaves intent unknown without replacement."""

    class TimingOutEditClient(FakeClient):
        def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
            raise TimeoutError("Telegram Bot API timed out for method: editMessageText")

    client = TimingOutEditClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "funding-workflow:w1", "pending", CARD)
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    result = manager.refresh(
        "run_1",
        "funding-workflow:w1",
        "in_progress",
        progressed,
        edit_replacement_policy="replace_on_target_absence",
    )

    assert result["ambiguous"] == (100,)
    assert result["sent"] == ()
    assert len(client.sent) == 1  # no replacement send
    copy = manager.copies("funding-workflow:w1")[("funding-workflow:w1", 100)]
    assert copy.delivery == "unknown"

    # Subsequent refresh emits only the buttonless ambiguity notice
    manager.refresh(
        "run_1",
        "funding-workflow:w1",
        "in_progress",
        progressed,
        edit_replacement_policy="replace_on_target_absence",
    )
    assert [text for _, text in client.sent[1:]] == [
        catalog.CARD_AMBIGUOUS_TEMPLATE.format(card_key="funding-workflow:w1", stage="in_progress")
    ]


def test_refresh_chat_ids_intersects_with_pinned_audience(tmp_path):
    """refresh with chat_ids only refreshes the intersection with pinned audience."""
    client = FakeClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100, 200))
    manager.deliver("run_1", "funding-workflow:w1", "pending", CARD)
    progressed = RenderedCard(text="🔵 주문 진행 중", reply_markup=None)

    # Refresh only chat 100
    result = manager.refresh(
        "run_1",
        "funding-workflow:w1",
        "in_progress",
        progressed,
        chat_ids=[100, 999],  # 999 is unpinned
    )

    assert result["edited"] == (100,)
    assert 200 not in result["edited"]
    assert [c[0] for c in client.edited] == [100]


def test_three_consecutive_rejections_send_a_plain_text_fallback(tmp_path):
    """The operator must not lose the thread because the card path is broken.

    The fallback deliberately does not go through cards.py: if rendering is
    what fails, rendering the fallback would fail too.
    """

    class CardRejectingClient(FakeClient):
        """Rejects this card's content, accepts anything else.

        Models the case the fallback is for -- a card Telegram will not take,
        while the chat itself is reachable. A dead chat cannot be helped by
        sending it more messages.
        """

        def send_message(self, chat_id, text, reply_markup=None):
            if text == CARD.text:
                raise TelegramApiRejected("bad entity in card markup")
            return super().send_message(chat_id, text, reply_markup)

    client = CardRejectingClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))

    for _ in range(3):
        manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert manager.consecutive_failures("approval:appr_1", 100) == 3
    assert [text for _, text in client.sent] == [
        catalog.CARD_FALLBACK_TEMPLATE.format(card_key="approval:appr_1", stage="pending")
    ]


def test_two_rejections_do_not_trigger_the_fallback(tmp_path):
    client = FakeClient(reject_for={100})
    _, manager = _manager(tmp_path, client, chat_ids=(100,))

    for _ in range(2):
        manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert manager.consecutive_failures("approval:appr_1", 100) == 2
    assert client.sent == []


def test_a_confirmed_send_resets_the_failure_run(tmp_path):
    client = FakeClient(reject_for={100})
    _, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    client.reject_for = set()

    manager.deliver("run_1", "approval:appr_1", "pending", CARD)

    assert manager.consecutive_failures("approval:appr_1", 100) == 0


def test_a_card_that_cannot_be_rendered_counts_toward_the_fallback(tmp_path):
    """렌더 실패도 카드가 닿지 않는 것이다 — 전송 거절과 같은 카운터를 쓴다.

    렌더가 깨진 채 격리만 하면 같은 오류가 매 poll 반복되면서도 fallback은
    영원히 발송되지 않고 telegram_ui 헬스는 계속 ok로 남는다.
    """
    client = FakeClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))

    for _ in range(3):
        manager.record_render_failure("run_1", "approval:appr_1", "pending", "boom")

    assert manager.consecutive_failures("approval:appr_1", 100) == 3
    assert any("appr_1" in text for _, text in client.sent), "fallback이 나가지 않았다"
    assert store.list_failing_card_copies(3)


def test_a_render_failure_records_no_hash_and_is_retried(tmp_path):
    """일어나지 않은 렌더의 해시를 남기지 않는다.

    다음 정상 렌더가 건너뛰어지지 않게 막는 것은 delivery 상태다 (refresh는
    confirmed인 복사본만 접는다). 해시를 비워 두는 것은 그와 별개로, 기록이
    사실과 어긋나지 않게 하기 위함이다.
    """
    client = FakeClient()
    store, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    manager.record_render_failure("run_1", "approval:appr_1", "pending", "boom")

    copy = manager.copies("approval:appr_1")[("approval:appr_1", 100)]
    assert copy.render_hash == "", "일어나지 않은 렌더의 해시가 기록됐다"
    assert copy.delivery == "failed"

    result = manager.refresh("run_1", "approval:appr_1", "pending", CARD)

    assert result["skipped"] == ()
    assert result["edited"] == (100,)


def test_a_recovered_render_clears_the_failure_run(tmp_path):
    client = FakeClient()
    _, manager = _manager(tmp_path, client, chat_ids=(100,))
    manager.deliver("run_1", "approval:appr_1", "pending", CARD)
    manager.record_render_failure("run_1", "approval:appr_1", "pending", "boom")

    manager.refresh("run_1", "approval:appr_1", "pending", CARD)

    assert manager.consecutive_failures("approval:appr_1", 100) == 0

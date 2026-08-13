"""단계 2 — 승인 카드를 lifecycle sweep이 제자리에서 갱신한다.

여기서 검증하는 계약은 "알림을 연달아 보내지 않는다"이다. 승인 하나에 카드는
한 장이고, 단계가 바뀌면 그 메시지를 edit한다.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from maestro.approval.models import ApprovalRequest, PendingApprovalEnvelope
from maestro.config.loader import load_config
from maestro.integrations.telegram.bot import TelegramApiRejected
from maestro.integrations.telegram.handlers import TelegramOperatorCommandRouter
from maestro.integrations.telegram.ui import catalog
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def _telegram_config_path(tmp_path, *, chat_ids=(100,)) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "telegram_allowed_chat_ids": list(chat_ids),
        "whitelisted_user_ids": [100],
    }
    config_path = tmp_path / "telegram_operator.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


class FakeTelegramClient:
    def __init__(self, *, reject_for=()) -> None:
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        # An explicit refusal: the one error that proves nothing was delivered,
        # so the copy stays unconfirmed and the card never settles.
        self.reject_for = set(reject_for)
        self.next_message_id = 5000

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.reject_for:
            raise TelegramApiRejected(f"telegram refused chat {chat_id}")
        self.next_message_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        # The shape TelegramBotAPIClient actually returns (bot.py:159).
        return {"ok": True, "result": {"message_id": self.next_message_id}}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
            }
        )
        return {"ok": True, "result": {"message_id": message_id}}

    def get_updates(self, *, offset=None, timeout_seconds=0, allowed_updates=None):
        return {"ok": True, "result": []}

    def answer_callback_query(self, callback_query_id, text=""):
        return {"ok": True}


def _router_with_cards(tmp_path, *, chat_ids=(100,), reject_for=()):
    config = load_config(_telegram_config_path(tmp_path, chat_ids=chat_ids))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    client = FakeTelegramClient(reject_for=reject_for)
    router = TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=client,
    )
    return router, store, client


def _save_pending_envelope(
    store,
    *,
    approval_id,
    signal_run_id=None,
    order_count=1,
    reminder_seconds=None,
    created_ago=timedelta(0),
):
    now = datetime.now(UTC)
    created_at = now - created_ago
    orders = [
        {
            "order_id": f"ord_{approval_id}_{index}",
            "symbol": "069500",
            "name": "KODEX 200",
            "side": "buy",
            "quantity": 10,
            "notional": 712_000.0,
            "currency": "KRW",
        }
        for index in range(order_count)
    ]
    envelope = PendingApprovalEnvelope(
        approval_id=approval_id,
        run_id=f"run_{approval_id}",
        signal_run_id=signal_run_id or f"signal_{approval_id}",
        request=ApprovalRequest(
            approval_id=approval_id,
            run_id=f"run_{approval_id}",
            created_at=created_at,
            expires_at=now + timedelta(hours=1),
            channel="telegram",
            source_strategy_ids=["tranquillo"],
            order_count=len(orders),
            estimated_notional=sum(order["notional"] for order in orders),
            proposed_orders=orders,
        ),
        orders=orders,
        message="카드 본문",
        source_strategy_ids=["tranquillo"],
        account_ids=["kis_ps"],
        reminder_seconds=list(reminder_seconds or []),
        created_at=created_at,
        expires_at=now + timedelta(hours=1),
        duplicate_key=f"telegram-approval-pending:{approval_id}",
    )
    store.save_system_event(
        envelope.run_id, "telegram_approval_pending", envelope.model_dump(mode="json")
    )
    return envelope


def _save_ack(store, *, approval_id, status="approved"):
    store.save_system_event(
        f"run_{approval_id}",
        "telegram_approval_ack",
        {
            "approval_id": approval_id,
            "signal_run_id": f"signal_{approval_id}",
            "status": status,
            "decided_by": "telegram:tester",
            "decided_at": datetime.now(UTC).isoformat(),
            "schema_version": 2,
            "duplicate_key": f"telegram-approval-ack:{approval_id}",
        },
    )


def _save_completed(store, *, approval_id, orders_failed=0, approval_status="approved"):
    store.save_system_event(
        f"run_{approval_id}",
        "signal_approval_completed",
        {
            "approval_id": approval_id,
            "signal_run_id": f"signal_{approval_id}",
            "orders_created": 1,
            "orders_submitted": 1,
            "orders_failed": orders_failed,
            "approval_status": approval_status,
        },
    )


def _save_recovery_required(store, *, approval_id, index=0):
    order_id = f"ord_{approval_id}_{index}"
    store.save_system_event(
        f"run_{approval_id}",
        "live_order_recovery_required",
        {
            "reason": "batch_status_exception_after_submit",
            "order_id": order_id,
            "request": {"order_id": order_id},
            "result": {"message": "broker timed out"},
        },
    )


def _stage_of(store, card_key, chat_id=100):
    rows = {row["chat_id"]: row for row in store.load_card_delivery_state(card_key)}
    return rows[chat_id]["stage"] if chat_id in rows else None


def test_an_approval_stage_change_edits_the_card_instead_of_sending_a_new_one(tmp_path):
    """연달아 오던 'Maestro live order update' 스트림을 카드 한 장으로 대체한다."""
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")

    router._sweep_lifecycle_cards()
    sent_after_first = len(client.sent)

    _save_ack(store, approval_id="appr_1")
    router._sweep_lifecycle_cards()

    assert len(client.sent) == sent_after_first, "새 메시지를 보내면 안 된다"
    assert client.edited, "기존 카드를 edit해야 한다"
    assert _stage_of(store, "approval:appr_1") == "in_progress"


def test_an_unchanged_approval_is_not_edited_every_poll(tmp_path):
    """sweep은 2분마다 돈다. 같은 단계에서 매번 edit하면 API만 태운다."""
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")

    router._sweep_lifecycle_cards()
    router._sweep_lifecycle_cards()
    router._sweep_lifecycle_cards()

    assert len(client.sent) == 1
    assert client.edited == []


def test_a_half_executed_rotation_is_not_reported_as_complete(tmp_path):
    """2026-08-12 US 런의 모양: approval_status='approved' 인데 orders_failed=1.

    이벤트 *유형*만 보고 done으로 접으면 절반만 집행되고 멈춘 로테이션이
    "✅ 완료"로 표시된다.
    """
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    router._sweep_lifecycle_cards()

    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=1)
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "attention"
    assert catalog.CARD_STAGE_LABELS["attention"] in client.edited[-1]["text"]


def test_a_clean_completion_reaches_done(tmp_path):
    router, store, _ = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    router._sweep_lifecycle_cards()

    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=0)
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "done"


def test_an_unresolved_recovery_holds_the_card_in_attention(tmp_path):
    """복구 대상 주문은 이 승인의 order_id로 잇는다 — 페이로드에 approval_id가 없다."""
    router, store, _ = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=0)
    _save_recovery_required(store, approval_id="appr_1")

    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "attention"


def test_a_recovery_for_another_approval_does_not_touch_this_card(tmp_path):
    """order_id로 잇지 않고 '복구가 하나라도 있으면 주의'로 접으면 전부 물든다."""
    router, store, _ = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_pending_envelope(store, approval_id="appr_2")
    for approval_id in ("appr_1", "appr_2"):
        _save_ack(store, approval_id=approval_id)
        _save_completed(store, approval_id=approval_id, orders_failed=0)
    _save_recovery_required(store, approval_id="appr_2")

    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "done"
    assert _stage_of(store, "approval:appr_2") == "attention"


def test_a_resolved_recovery_releases_the_card_from_attention(tmp_path):
    """진행과 주의를 두 축으로 나눈 이유. 사고가 해소되면 카드도 풀려야 한다."""
    router, store, _ = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=0)
    _save_recovery_required(store, approval_id="appr_1")
    router._sweep_lifecycle_cards()
    assert _stage_of(store, "approval:appr_1") == "attention"

    store.save_system_event(
        "run_recovery", "live_order_recovery_completed", {"resolved_orders": []}
    )
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "done"


def test_progress_does_not_walk_back_when_an_event_lands_late(tmp_path):
    """순서가 뒤바뀐 도착이 "완료"를 "진행 중"으로 되돌리면 안 된다.

    chat 200이 계속 거절당해 카드가 종점(settled)에 들지 못하므로 sweep은 매번
    다시 판정한다. 주의 축은 건드리지 않는다 — attention이 표시를 가져가면
    진행 축이 되돌아가도 화면에서는 보이지 않아 아무것도 검증하지 못한다.
    """
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100, 200), reject_for=(200,))
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=0)
    router._sweep_lifecycle_cards()
    assert _stage_of(store, "approval:appr_1") == "done"

    # 완료 이벤트가 조회되지 않는 상황(늦게 도착·유실)을 그대로 재현한다.
    original = store.list_system_events_by_type

    def without_completion(event_type, **kwargs):
        if event_type == "signal_approval_completed":
            return []
        return original(event_type, **kwargs)

    store.list_system_events_by_type = without_completion
    try:
        router._sweep_lifecycle_cards()
    finally:
        store.list_system_events_by_type = original

    assert _stage_of(store, "approval:appr_1") == "done", "완료가 진행 중으로 되돌아갔다"


def test_a_settled_card_wakes_up_when_something_new_happens(tmp_path):
    """종점 판정은 매 sweep 다시 계산한다 — 카드에 붙는 영구 표식이 아니다.

    투영이 done이라는 이유만으로 건너뛰면, 그 뒤에 생긴 복구 건이 카드에
    영영 반영되지 않는다.
    """
    router, store, _ = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=0)
    router._sweep_lifecycle_cards()
    assert _stage_of(store, "approval:appr_1") == "done"

    _save_recovery_required(store, approval_id="appr_1")
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "attention"


def test_a_settled_card_stops_being_swept(tmp_path):
    """done은 종점이다. 승인은 계속 쌓이므로 끝난 카드를 매 poll 다시 그리면 안 된다."""
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=0)
    router._sweep_lifecycle_cards()
    edits_after_done = len(client.edited)

    router._sweep_lifecycle_cards()
    router._sweep_lifecycle_cards()

    assert len(client.edited) == edits_after_done


def test_the_daily_parent_card_appears_only_when_a_run_has_two_groups(tmp_path):
    """승인 그룹이 하나면 부모 카드는 같은 말을 두 번 하는 것뿐이다."""
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_solo", signal_run_id="signal_solo")

    router._sweep_lifecycle_cards()

    assert store.load_card_delivery_state("daily:signal_solo") == []
    assert not any(catalog.DAILY_CARD_TITLE in message["text"] for message in client.sent)


def test_two_groups_in_one_run_get_a_daily_parent_card(tmp_path):
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_a", signal_run_id="signal_1")
    _save_pending_envelope(store, approval_id="appr_b", signal_run_id="signal_1")

    router._sweep_lifecycle_cards()

    daily = [message for message in client.sent if catalog.DAILY_CARD_TITLE in message["text"]]
    assert len(daily) == 1
    assert daily[0]["reply_markup"] is None, "부모 카드에는 버튼이 없다"
    assert store.load_card_delivery_state("daily:signal_1")


def test_the_daily_card_follows_its_groups(tmp_path):
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_a", signal_run_id="signal_1")
    _save_pending_envelope(store, approval_id="appr_b", signal_run_id="signal_1")
    router._sweep_lifecycle_cards()

    _save_ack(store, approval_id="appr_a")
    _save_completed(store, approval_id="appr_a", orders_failed=1)
    router._sweep_lifecycle_cards()

    daily_edit = [
        message for message in client.edited if catalog.DAILY_CARD_TITLE in message["text"]
    ][-1]
    assert catalog.CARD_STAGE_LABELS["attention"] in daily_edit["text"]
    assert catalog.CARD_STAGE_LABELS["pending"] in daily_edit["text"]


def test_the_legacy_notification_path_still_runs(tmp_path):
    """제거는 단계 5다. 카드 전달이 프로덕션에서 증명된 뒤에 뗀다."""
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(
        store,
        approval_id="appr_1",
        reminder_seconds=[60],
        created_ago=timedelta(minutes=5),
    )

    router._sweep_pending_approvals()

    assert client.sent, "기존 리마인더 경로가 그대로 동작해야 한다"
    assert any(catalog.REMINDER.split("(")[0] in message["text"] for message in client.sent)


def test_the_card_sweep_is_registered_and_cannot_wedge_the_poll_loop(tmp_path):
    """poll_once의 sweep 튜플에 들어가 같은 예외 격리를 받는다."""
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    calls: list[str] = []

    def exploding_sweep():
        calls.append("swept")
        raise RuntimeError("card sweep is broken")

    router._sweep_lifecycle_cards = exploding_sweep

    assert router.poll_once(offset=None) is None
    assert calls == ["swept"], "poll_once가 카드 sweep을 부르지 않았다"

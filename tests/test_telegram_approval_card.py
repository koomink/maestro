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
from maestro.integrations.telegram.ui.cards import render_approval_stage_card
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
    # 기본값은 이관 이전의 모양이다. 그 봉투에는 이 필드 자체가 없었으므로
    # 저장된 payload에서 되살리면 0이 된다.
    card_delivery_version=0,
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
        card_delivery_version=card_delivery_version,
    )
    store.save_system_event(
        envelope.run_id, "telegram_approval_pending", envelope.model_dump(mode="json")
    )
    return envelope


def _dispatch_approval(router, **kwargs):
    """봉투를 남기고 카드를 보낸다 — orchestrator의 dispatch가 하는 그대로.

    프로덕션에서 승인 카드는 dispatch가 태어나게 하고 sweep은 갱신만 한다.
    카드 없이 봉투만 만들어 두고 sweep에 첫 전송을 시키는 테스트는 이관 이전
    상태를 흉내내는 것이지 정상 경로가 아니다.
    """
    kwargs.setdefault("card_delivery_version", 1)
    envelope = _save_pending_envelope(router.store, **kwargs)
    router._card_manager.deliver(
        envelope.run_id,
        f"approval:{envelope.approval_id}",
        "pending",
        render_approval_stage_card(envelope.request, "pending"),
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


def _save_resolution_failed(store, *, approval_id, status="approved"):
    store.save_system_event(
        f"run_{approval_id}",
        "telegram_approval_resolution_failed",
        {
            "approval_id": approval_id,
            "status": status,
            "error_type": "RuntimeError",
            "error_message": "broker rejected the batch",
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
    _dispatch_approval(router, approval_id="appr_1")

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
    _dispatch_approval(router, approval_id="appr_1")

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
    _dispatch_approval(router, approval_id="appr_1")
    router._sweep_lifecycle_cards()

    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=1)
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "attention"
    assert catalog.CARD_STAGE_LABELS["attention"] in client.edited[-1]["text"]


def test_a_clean_completion_reaches_done(tmp_path):
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    router._sweep_lifecycle_cards()

    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=0)
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "done"


def test_an_unresolved_recovery_holds_the_card_in_attention(tmp_path):
    """복구 대상 주문은 이 승인의 order_id로 잇는다 — 페이로드에 approval_id가 없다."""
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=0)
    _save_recovery_required(store, approval_id="appr_1")

    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "attention"


def test_a_recovery_for_another_approval_does_not_touch_this_card(tmp_path):
    """order_id로 잇지 않고 '복구가 하나라도 있으면 주의'로 접으면 전부 물든다."""
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _dispatch_approval(router, approval_id="appr_2")
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
    _dispatch_approval(router, approval_id="appr_1")
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
    _dispatch_approval(router, approval_id="appr_1")
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
    _dispatch_approval(router, approval_id="appr_1")
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
    _dispatch_approval(router, approval_id="appr_1")
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
    _dispatch_approval(router, approval_id="appr_solo", signal_run_id="signal_solo")

    router._sweep_lifecycle_cards()

    assert store.load_card_delivery_state("daily:signal_solo") == []
    assert not any(catalog.DAILY_CARD_TITLE in message["text"] for message in client.sent)


def test_two_groups_in_one_run_get_a_daily_parent_card(tmp_path):
    router, store, client = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_a", signal_run_id="signal_1")
    _dispatch_approval(router, approval_id="appr_b", signal_run_id="signal_1")

    router._sweep_lifecycle_cards()

    daily = [message for message in client.sent if catalog.DAILY_CARD_TITLE in message["text"]]
    assert len(daily) == 1
    assert daily[0]["reply_markup"] is None, "부모 카드에는 버튼이 없다"
    assert store.load_card_delivery_state("daily:signal_1")


def test_the_daily_card_follows_its_groups(tmp_path):
    router, store, client = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_a", signal_run_id="signal_1")
    _dispatch_approval(router, approval_id="appr_b", signal_run_id="signal_1")
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
    _dispatch_approval(router, approval_id="appr_1")
    calls: list[str] = []

    def exploding_sweep():
        calls.append("swept")
        raise RuntimeError("card sweep is broken")

    router._sweep_lifecycle_cards = exploding_sweep

    assert router.poll_once(offset=None) is None
    assert calls == ["swept"], "poll_once가 카드 sweep을 부르지 않았다"


def test_an_approval_dispatched_before_the_cutover_gets_no_second_card(tmp_path):
    """배포 시점에 이미 떠 있던 승인에 카드를 새로 보내면 안 된다.

    구 코드의 dispatch는 send_message로 직접 보내고 message_id를 남기지 않았다.
    그런 봉투는 투영에 행이 하나도 없고, sweep이 그것을 "아직 안 보냈다"로 읽으면
    운영자 화면에 버튼 달린 카드가 두 장 생긴다 — 배포 순간 진행 중이던 승인마다.

    투영이 비어 있다는 것은 곧 lifecycle이 이 카드를 보낸 적이 없다는 뜻이다.
    신규 봉투는 dispatch가 전송 전에 intent를 남기므로 거절당한 경우에도 행이 있다.
    """
    router, store, client = _router_with_cards(tmp_path)
    # 봉투만 있고 카드 기록은 없다 — 구 경로가 보낸 승인의 모양 그대로.
    _save_pending_envelope(store, approval_id="appr_legacy")

    router._sweep_lifecycle_cards()

    assert client.sent == [], "구 카드가 있는 승인에 새 카드를 보냈다"
    assert client.edited == []
    assert store.load_card_delivery_state("approval:appr_legacy") == []


def test_a_pre_cutover_run_with_two_groups_does_not_break_the_sweep(tmp_path):
    """건너뛴 승인이 부모 카드 집계에 남아 있으면 sweep 전체가 죽는다.

    poll_once가 예외를 삼켜 주므로 조용히 실패하고, 그 뒤로 어떤 카드도
    갱신되지 않는다.
    """
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_a", signal_run_id="signal_old")
    _save_pending_envelope(store, approval_id="appr_b", signal_run_id="signal_old")

    router._sweep_lifecycle_cards()

    assert client.sent == []
    assert store.load_card_delivery_state("daily:signal_old") == []


def test_an_approval_that_crashed_before_its_card_was_sent_is_repaired(tmp_path):
    """pending 저장과 카드 전송 사이에서 죽으면 승인이 운영자에게 닿지 않는다.

    투영이 비었다는 사실만으로는 "구 코드가 이미 보냈다"와 "새 코드가 보내기
    전에 죽었다"를 구분할 수 없다. 후자를 건너뛰면 그 승인은 카드도 없고 다시
    시도되지도 않는다. envelope의 delivery 버전이 그 둘을 가른다.
    """
    router, store, client = _router_with_cards(tmp_path)
    envelope = _save_pending_envelope(
        store, approval_id="appr_crashed", card_delivery_version=1
    )
    assert envelope.card_delivery_version == 1, "신규 envelope는 lifecycle 소유임을 선언한다"

    router._sweep_lifecycle_cards()

    assert len(client.sent) == 1, "전송 전에 중단된 승인은 sweep이 살려야 한다"
    assert _stage_of(store, "approval:appr_crashed") == "pending"


def test_a_resolution_failure_puts_the_card_in_attention(tmp_path):
    """집행이 실패했는데 카드가 "🔵 진행 중"이면 운영자는 기다리기만 한다.

    ack는 approved, completion은 없고, 3a-1이 남긴 resolution_failed만 있는
    상태 — 재개가 소진되면 여기서 멈춘다.
    """
    router, store, client = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    _save_resolution_failed(store, approval_id="appr_1")

    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "attention"
    assert catalog.CARD_STAGE_LABELS["attention"] in client.edited[-1]["text"]


def test_a_failure_that_a_retry_resolved_does_not_hold_the_card(tmp_path):
    """재개가 성공하면 완료 기록이 남는다. 과거의 실패가 카드를 붙잡으면 안 된다."""
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    _save_resolution_failed(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=0)

    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "done"


def test_one_unrenderable_card_does_not_stop_the_others(tmp_path):
    """카드 하나가 못 그려진다고 나머지 승인이 전부 멈추면 안 된다."""
    router, store, client = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_bad")
    _dispatch_approval(router, approval_id="appr_good")
    _save_ack(store, approval_id="appr_bad")
    _save_ack(store, approval_id="appr_good")
    broken = router._card_manager.refresh

    def refresh(run_id, card_key, stage, rendered):
        if card_key == "approval:appr_bad":
            raise ValueError("renderer blew up")
        return broken(run_id, card_key, stage, rendered)

    router._card_manager.refresh = refresh
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_good") == "in_progress"


def test_a_repeatedly_unrenderable_card_falls_back_to_plain_text(tmp_path):
    """격리만 하면 같은 오류가 매 poll 반복되면서 아무도 알지 못한다."""
    router, store, client = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    original = router._card_manager.refresh

    def exploding(run_id, card_key, stage, rendered):
        if card_key == "approval:appr_1":
            raise ValueError("renderer blew up")
        return original(run_id, card_key, stage, rendered)

    router._card_manager.refresh = exploding
    for _ in range(3):
        router._sweep_lifecycle_cards()

    assert store.list_failing_card_copies(3), "telegram_ui 헬스가 감지할 근거가 없다"
    assert any(
        catalog.CARD_FALLBACK_TEMPLATE.split("(")[0] in message["text"]
        for message in client.sent
    ), "고정 템플릿 fallback이 나가지 않았다"


def test_a_broken_parent_card_does_not_stop_the_next_signal_run(tmp_path):
    """부모 카드 하나가 실패하면 뒤쪽 run의 부모 카드까지 멈춘다."""
    router, store, client = _router_with_cards(tmp_path)
    for signal_run_id in ("signal_1", "signal_2"):
        _dispatch_approval(
            router, approval_id=f"appr_{signal_run_id}_a", signal_run_id=signal_run_id
        )
        _dispatch_approval(
            router, approval_id=f"appr_{signal_run_id}_b", signal_run_id=signal_run_id
        )
    original = router._card_manager.refresh

    def exploding(run_id, card_key, stage, rendered):
        if card_key == "daily:signal_1":
            raise ValueError("parent card blew up")
        return original(run_id, card_key, stage, rendered)

    router._card_manager.refresh = exploding
    router._sweep_lifecycle_cards()

    assert store.load_card_delivery_state("daily:signal_2"), "뒤쪽 부모 카드가 갱신되지 않았다"
    assert any(catalog.DAILY_CARD_TITLE in message["text"] for message in client.sent)

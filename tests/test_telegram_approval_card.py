"""단계 2 — 승인 카드를 lifecycle sweep이 제자리에서 갱신한다.

여기서 검증하는 계약은 "알림을 연달아 보내지 않는다"이다. 승인 하나에 카드는
한 장이고, 단계가 바뀌면 그 메시지를 edit한다.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
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


class ProcessDied(BaseException):
    """프로세스가 죽은 것. Exception이 아니므로 격리 코드가 삼키지 않는다."""


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
    original = store.latest_payloads_by_approval_id

    def without_completion(event_type, approval_ids):
        if event_type == "signal_approval_completed":
            return {}
        return original(event_type, approval_ids)

    store.latest_payloads_by_approval_id = without_completion
    try:
        router._sweep_lifecycle_cards()
    finally:
        store.latest_payloads_by_approval_id = original

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
    router._sweep_lifecycle_cards()  # 종결 표시가 남는 시점
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


def test_a_repeatedly_unrenderable_card_falls_back_to_plain_text(monkeypatch, tmp_path):
    """격리만 하면 같은 오류가 매 poll 반복되면서 아무도 알지 못한다.

    렌더 실패는 아무것도 보내지 않았음이 확정이므로 전송 거절과 같은 카운터를
    탄다.
    """
    router, store, client = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")

    def exploding_renderer(request, stage):
        raise ValueError("renderer blew up")

    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.render_approval_stage_card",
        exploding_renderer,
    )
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


def test_a_failed_result_write_after_sending_does_not_become_a_known_failure(tmp_path):
    """전송은 됐는데 result 기록이 실패한 경우 — 전달 여부는 '불명'이다.

    이것을 렌더 실패로 접어 projection을 failed로 덮으면, 다음 sweep이 재전송이
    안전하다고 판단해 버튼 달린 승인 카드가 두 장 생긴다. 이 브랜치가 처음부터
    막아 온 바로 그 상태다.
    """
    router, store, client = _router_with_cards(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1", card_delivery_version=1)
    original = store.record_card_event

    def failing_result(run_id, payload):
        if payload.get("phase") == "result":
            raise sqlite3.OperationalError("database is locked")
        return original(run_id, payload)

    store.record_card_event = failing_result
    router._sweep_lifecycle_cards()
    store.record_card_event = original

    cards = [message for message in client.sent if message["reply_markup"]]
    assert len(cards) == 1, "카드는 실제로 나갔다"
    copies = store.load_card_delivery_state("approval:appr_1")
    assert [copy["delivery"] for copy in copies] == ["unknown"], "불명이 실패로 바뀌었다"

    router._sweep_lifecycle_cards()

    cards = [message for message in client.sent if message["reply_markup"]]
    assert len(cards) == 1, "불명 상태의 카드를 다시 보냈다"


def test_a_broken_failure_log_does_not_break_the_card_isolation(tmp_path):
    """실패를 기록하는 경로도 DB를 쓴다 — 그것이 깨져도 sweep은 계속돌아야 한다."""
    router, store, client = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_bad")
    _dispatch_approval(router, approval_id="appr_good")
    _save_ack(store, approval_id="appr_bad")
    _save_ack(store, approval_id="appr_good")
    original = router._card_manager.refresh

    def exploding(run_id, card_key, stage, rendered):
        if card_key == "approval:appr_bad":
            raise ValueError("renderer blew up")
        return original(run_id, card_key, stage, rendered)

    def broken_log(update_id, exc):
        raise sqlite3.OperationalError("audit log is unwritable")

    router._card_manager.refresh = exploding
    router._record_update_failure = broken_log
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_good") == "in_progress"


def test_one_unrenderable_card_does_not_stop_the_others_render_path(monkeypatch, tmp_path):
    """렌더 단계에서 깨진 카드도 뒤의 승인을 막지 않는다.

    refresh 단계의 격리와는 다른 분기다 — 렌더 실패는 카운터를 태우고 그 자리에서
    돌아가므로, 그 경로가 좁게 잡히면 여기서만 드러난다.
    """
    router, store, client = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_bad")
    _dispatch_approval(router, approval_id="appr_good")
    _save_ack(store, approval_id="appr_bad")
    _save_ack(store, approval_id="appr_good")
    real_render = render_approval_stage_card

    def selective(request, stage):
        if request.approval_id == "appr_bad":
            raise ValueError("renderer blew up")
        return real_render(request, stage)

    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.render_approval_stage_card", selective
    )
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_good") == "in_progress"
    assert store.load_card_delivery_state("approval:appr_bad")[0]["consecutive_failures"] == 1


def test_adding_a_chat_does_not_resend_every_past_card(tmp_path):
    """허용 채팅을 늘려도 지난 카드가 새 채팅으로 쏟아지지 않는다.

    카드의 수신자는 그 카드가 처음 전송될 때 정해진다. 매 sweep 현재 설정을
    다시 읽으면, 채팅 하나를 추가한 순간 과거의 모든 완료 카드가 "복사본이
    없다"로 보여 신규 전송된다 — 운영 기간에 비례하는 양이고, 그 폭주가
    지금 처리해야 할 승인 알림을 rate limit 뒤로 밀어낸다.
    """
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100,))
    _dispatch_approval(router, approval_id="appr_old")
    _save_ack(store, approval_id="appr_old")
    _save_completed(store, approval_id="appr_old")
    router._sweep_lifecycle_cards()
    assert _stage_of(store, "approval:appr_old") == "done"

    wider, _, wider_client = _router_with_cards(tmp_path, chat_ids=(100, 200))
    wider._sweep_lifecycle_cards()

    assert [message["chat_id"] for message in wider_client.sent] == []
    assert _stage_of(store, "approval:appr_old", chat_id=200) is None


def test_adding_a_chat_does_not_resend_a_daily_parent_card(tmp_path):
    """부모 카드도 같다 — 승인 그룹이 여럿이면 폭주는 그만큼 커진다."""
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100,))
    for index in range(2):
        _dispatch_approval(
            router, approval_id=f"appr_{index}", signal_run_id="signal_shared"
        )
        _save_ack(store, approval_id=f"appr_{index}")
        _save_completed(store, approval_id=f"appr_{index}")
    router._sweep_lifecycle_cards()
    assert _stage_of(store, "daily:signal_shared") == "done"

    wider, _, wider_client = _router_with_cards(tmp_path, chat_ids=(100, 200))
    wider._sweep_lifecycle_cards()

    assert wider_client.sent == []


def test_a_new_chat_still_gets_a_card_that_was_never_delivered(tmp_path):
    """수신자를 고정하는 것은 전송된 카드에 대해서다.

    한 번도 나가지 못한 카드까지 새 채팅에서 빼면, 전송 직전에 죽은 승인이
    설정을 바꾼 뒤로는 영영 아무에게도 닿지 않는다.
    """
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100,))
    _save_pending_envelope(store, approval_id="appr_never", card_delivery_version=1)

    wider, _, wider_client = _router_with_cards(tmp_path, chat_ids=(100, 200))
    wider._sweep_lifecycle_cards()

    assert sorted(message["chat_id"] for message in wider_client.sent) == [100, 200]


def test_a_settled_run_is_not_read_back_on_every_poll(tmp_path):
    """종결된 카드는 렌더뿐 아니라 조회에서도 빠져야 한다.

    poll마다 전체 승인 이벤트를 파싱하고 승인당 투영을 한 번씩 더 읽으면,
    callback polling 지연이 운영 기간에 비례해 계속 늘어난다.
    """
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_done")
    _save_ack(store, approval_id="appr_done")
    _save_completed(store, approval_id="appr_done")
    router._sweep_lifecycle_cards()  # done으로 편집
    router._sweep_lifecycle_cards()  # 그 결과를 보고 종결로 표시
    _dispatch_approval(router, approval_id="appr_open")

    read: list[str] = []
    real_load = store.load_card_delivery_state
    store.load_card_delivery_state = lambda card_key: (
        read.append(card_key) or real_load(card_key)
    )
    router._sweep_lifecycle_cards()
    store.load_card_delivery_state = real_load

    assert "approval:appr_open" in read
    assert "approval:appr_done" not in read


def test_a_later_approval_for_a_settled_run_is_still_swept(tmp_path):
    """종결 표시는 run 단위다. 뒤늦게 붙은 승인까지 묻으면 안 된다."""
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_first", signal_run_id="signal_shared")
    _save_ack(store, approval_id="appr_first")
    _save_completed(store, approval_id="appr_first")
    router._sweep_lifecycle_cards()
    router._sweep_lifecycle_cards()  # 종결 표시가 남는 시점

    _dispatch_approval(router, approval_id="appr_late", signal_run_id="signal_shared")
    _save_ack(store, approval_id="appr_late")
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_late") == "in_progress"


def test_a_removed_chat_does_not_hold_health_in_warn_forever(tmp_path):
    """설정에서 뺀 채팅의 실패 카운터는 health를 영구 warn으로 만든다.

    그 복사본은 다시 성공할 기회가 없으므로 카운터가 0으로 돌아올 길이 없다.
    """
    from maestro.monitoring.health import HealthService

    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100, 200), reject_for=(200,))
    _dispatch_approval(router, approval_id="appr_1")
    for _ in range(3):
        router._card_manager.record_render_failure(
            "run_appr_1", "approval:appr_1", "pending", "boom"
        )
    assert HealthService(router.config, store).run().checks

    narrowed_config = load_config(_telegram_config_path(tmp_path, chat_ids=(100,)))
    checks = {
        check.name: check for check in HealthService(narrowed_config, store).run().checks
    }

    assert checks["telegram_ui"].details["cards"] == ["approval:appr_1@100:3"]


def test_a_run_whose_parent_card_is_stuck_is_not_marked_settled(monkeypatch, tmp_path):
    """자식 카드가 전부 done이어도 부모 카드가 못 따라오면 run은 종결이 아니다.

    여기서 종결로 표시하면 데일리 카드는 영원히 옛 단계에 멈춘 채 스캔에서
    빠진다 — 되살릴 사건이 없으므로 다시는 손대지 못한다.
    """
    router, store, _ = _router_with_cards(tmp_path)
    for index in range(2):
        _dispatch_approval(router, approval_id=f"appr_{index}", signal_run_id="signal_shared")
        _save_ack(store, approval_id=f"appr_{index}")
        _save_completed(store, approval_id=f"appr_{index}")

    def exploding_daily(signal_run_id, entries):
        raise ValueError("parent card renderer blew up")

    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.render_daily_card", exploding_daily
    )
    router._sweep_lifecycle_cards()
    router._sweep_lifecycle_cards()

    read: list[str] = []
    real_load = store.load_card_delivery_state
    store.load_card_delivery_state = lambda card_key: (
        read.append(card_key) or real_load(card_key)
    )
    router._sweep_lifecycle_cards()
    store.load_card_delivery_state = real_load

    assert "approval:appr_0" in read


def test_adding_a_chat_does_not_resend_an_open_card_either(tmp_path):
    """수신자 고정은 완료된 카드에만 적용되는 규칙이 아니다.

    아직 열려 있는 승인이라도, 이미 나간 카드에 뒤늦게 채팅을 더하면 그 채팅은
    갱신될 뿐인 두 번째 카드를 받는다 — 버튼 달린 카드가 두 장 도는 상태다.
    """
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100,))
    _dispatch_approval(router, approval_id="appr_open")

    wider, _, wider_client = _router_with_cards(tmp_path, chat_ids=(100, 200))
    _save_ack(store, approval_id="appr_open")
    wider._sweep_lifecycle_cards()

    assert wider_client.sent == []
    assert _stage_of(store, "approval:appr_open") == "in_progress"


def test_a_render_failure_does_not_invent_a_copy_in_a_new_chat(tmp_path):
    """렌더 실패 카운터도 이 카드의 수신자에게만 붙는다.

    없던 채팅에 실패 복사본을 만들면, 그 복사본이 다음 refresh에서 '아직 안 보낸
    카드'로 보여 결국 과거 카드를 새 채팅으로 보내게 된다.
    """
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100,))
    _dispatch_approval(router, approval_id="appr_1")

    wider, _, _ = _router_with_cards(tmp_path, chat_ids=(100, 200))
    wider._card_manager.record_render_failure(
        "run_appr_1", "approval:appr_1", "pending", "boom"
    )

    assert [row["chat_id"] for row in store.load_card_delivery_state("approval:appr_1")] == [100]


def test_adding_a_chat_does_not_reopen_every_settled_run(tmp_path):
    """종결 판정도 수신자 기준이다.

    현재 설정 전부로 보면 채팅을 하나 더한 순간 과거의 모든 run이 다시 미종결이
    되어, 전송은 막더라도 스캔 비용은 그대로 돌아온다.
    """
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100,))
    _dispatch_approval(router, approval_id="appr_old")
    _save_ack(store, approval_id="appr_old")
    _save_completed(store, approval_id="appr_old")
    router._sweep_lifecycle_cards()

    # 채팅을 넓힌 뒤에 종결 판정이 내려지는 순서다. 넓히기 전에 이미 표시가
    # 남았다면 그 run은 SQL에서 빠지므로 판정 자체가 실행되지 않는다.
    wider, wider_store, _ = _router_with_cards(tmp_path, chat_ids=(100, 200))
    wider._sweep_lifecycle_cards()

    read: list[str] = []
    real_load = wider_store.load_card_delivery_state
    wider_store.load_card_delivery_state = lambda card_key: (
        read.append(card_key) or real_load(card_key)
    )
    wider._sweep_lifecycle_cards()
    wider_store.load_card_delivery_state = real_load

    assert read == []


def test_a_crash_mid_delivery_does_not_orphan_the_remaining_chats(tmp_path):
    """수신자를 '복사본이 있는 채팅'으로 되짚으면 전송 도중의 중단을 못 읽는다.

    chat 100까지 기록하고 죽으면, 재시작한 sweep에게 chat 200은 "나중에 추가된
    채팅"과 똑같이 보인다 — 그래서 영영 전송되지 않는다. 수신자는 첫 API 호출
    **전에** 남아 있어야 구분할 수 있다.
    """
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100, 200))
    envelope = _save_pending_envelope(store, approval_id="appr_1", card_delivery_version=1)
    # 죽는 지점은 chat 200을 **시작하기 전**이다. 호출 도중에 죽으면 intent가
    # 이미 남아 unknown 복사본이 되고, 그것은 재전송하지 않는 것이 맞다.
    real_deliver_one = router._card_manager._deliver_one

    def die_before_the_second_chat(run_id, card_key, stage, rendered, render_hash, chat_id):
        if chat_id == 200:
            raise ProcessDied("died between chats")
        return real_deliver_one(run_id, card_key, stage, rendered, render_hash, chat_id)

    router._card_manager._deliver_one = die_before_the_second_chat
    with pytest.raises(ProcessDied):
        router._card_manager.deliver(
            envelope.run_id,
            "approval:appr_1",
            "pending",
            render_approval_stage_card(envelope.request, "pending"),
        )
    assert [row["chat_id"] for row in store.load_card_delivery_state("approval:appr_1")] == [100]

    resumed, _, resumed_client = _router_with_cards(tmp_path, chat_ids=(100, 200))
    resumed._sweep_lifecycle_cards()

    assert [message["chat_id"] for message in resumed_client.sent] == [200]


def test_a_group_added_to_a_settled_run_still_gets_a_parent_card(tmp_path):
    """되살아난 run은 새 승인만이 아니라 그 run 전체를 다시 봐야 한다.

    새 이벤트만 돌려주면 그룹이 하나로 보여 부모 카드 조건(2개 이상)에 닿지
    못한다 — 종결 전에는 있었을 부모 카드가 종결 뒤에는 생기지 않는다.
    """
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_first", signal_run_id="signal_shared")
    _save_ack(store, approval_id="appr_first")
    _save_completed(store, approval_id="appr_first")
    router._sweep_lifecycle_cards()
    router._sweep_lifecycle_cards()  # 종결 표시가 남는 시점

    _dispatch_approval(router, approval_id="appr_second", signal_run_id="signal_shared")
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "daily:signal_shared") is not None


def test_a_resolution_failure_that_completed_stops_reopening_its_run(tmp_path):
    """되살릴 대상은 아직 안 끝난 실패뿐이다.

    과거 실패를 무조건 되살리면, 재개가 성공해 완료된 run도 매 poll 표시가
    지워지고 다시 쓰인다 — 종결 인덱스가 없애려던 조회·쓰기가 그대로 남는다.
    """
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    _save_resolution_failed(store, approval_id="appr_1")
    router._sweep_lifecycle_cards()
    assert _stage_of(store, "approval:appr_1") == "attention"

    _save_completed(store, approval_id="appr_1")
    router._sweep_lifecycle_cards()  # done으로 편집
    router._sweep_lifecycle_cards()  # 종결 표시가 남는 시점

    read: list[str] = []
    real_load = store.load_card_delivery_state
    store.load_card_delivery_state = lambda card_key: (
        read.append(card_key) or real_load(card_key)
    )
    router._sweep_lifecycle_cards()
    store.load_card_delivery_state = real_load

    assert read == []


def test_the_audience_is_not_rewritten_on_every_poll(tmp_path):
    """수신자는 변하지 않는 사실이다. 매 poll 다시 쓰면 열린 승인마다 초당 한 번씩
    쓰기 트랜잭션이 도는데, 종결 인덱스로 줄이려던 것이 바로 그 비용이다."""
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    router._sweep_lifecycle_cards()

    writes: list[str] = []
    real_record = store.record_card_audience
    store.record_card_audience = lambda card_key, chat_ids: (
        writes.append(card_key) or real_record(card_key, chat_ids)
    )
    router._sweep_lifecycle_cards()
    store.record_card_audience = real_record

    assert writes == []


def test_a_card_from_before_the_audience_table_keeps_its_own_chats(tmp_path):
    """기록이 없는 옛 카드는 지금 가진 복사본이 수신자다 — 그리고 그것으로 고정된다."""
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100,))
    _dispatch_approval(router, approval_id="appr_old")
    with sqlite3.connect(store.path) as conn:
        conn.execute("DELETE FROM telegram_ui_card_audience")

    wider, wider_store, wider_client = _router_with_cards(tmp_path, chat_ids=(100, 200))
    _save_ack(wider_store, approval_id="appr_old")
    wider._sweep_lifecycle_cards()

    assert wider_client.sent == []
    assert wider_store.load_card_audience("approval:appr_old") == [100]


def test_a_removed_chat_stops_receiving_edits(tmp_path):
    """기록된 수신자라도 지금 설정에 없으면 건드리지 않는다.

    그 채팅으로는 다시 성공할 수 없으므로, 계속 갱신을 시도하면 실패만 쌓여
    fallback과 health degrade를 부른다.
    """
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100, 200))
    _dispatch_approval(router, approval_id="appr_1")
    assert store.load_card_audience("approval:appr_1") == [100, 200]

    narrowed, narrowed_store, narrowed_client = _router_with_cards(tmp_path, chat_ids=(100,))
    _save_ack(narrowed_store, approval_id="appr_1")
    narrowed._sweep_lifecycle_cards()

    assert [message["chat_id"] for message in narrowed_client.edited] == [100]


def test_a_resolution_failure_after_a_run_settles_still_reaches_the_card(tmp_path):
    """거절은 완료 이벤트 없이도 done이다 — 그 뒤에 붙은 집행 실패가 문제다.

    run이 이미 종결된 뒤라면 스캔에서 빠져 있으므로, 실패 쪽 되살리기가 없으면
    카드는 "거절 처리됨"에 멈춘 채 실패를 영영 알리지 못한다.
    """
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="rejected")
    router._sweep_lifecycle_cards()
    router._sweep_lifecycle_cards()  # 종결 표시가 남는 시점
    assert _stage_of(store, "approval:appr_1") == "done"

    _save_resolution_failed(store, approval_id="appr_1", status="rejected")
    router._sweep_lifecycle_cards()

    assert _stage_of(store, "approval:appr_1") == "attention"


def test_a_crash_while_recording_a_render_failure_does_not_orphan_a_chat(monkeypatch, tmp_path):
    """렌더가 처음부터 실패하면 refresh에 닿지 못하므로 수신자가 기록되지 않는다.

    그 상태에서 실패를 chat별로 남기다 죽으면, 남은 채팅은 다시 "나중에 추가된
    채팅"으로 보인다 — deliver와 똑같은 구멍이 렌더 실패 경로에 남아 있었다.
    """
    router, store, _ = _router_with_cards(tmp_path, chat_ids=(100, 200))
    _save_pending_envelope(store, approval_id="appr_1", card_delivery_version=1)

    def exploding_renderer(request, stage):
        raise ValueError("renderer blew up")

    monkeypatch.setattr(
        "maestro.integrations.telegram.handlers.render_approval_stage_card", exploding_renderer
    )
    real_record = store.record_card_event

    def die_on_the_second_chat(run_id, payload):
        if payload["chat_id"] == 200:
            raise ProcessDied("died between chats")
        return real_record(run_id, payload)

    store.record_card_event = die_on_the_second_chat
    with pytest.raises(ProcessDied):
        router._sweep_lifecycle_cards()
    store.record_card_event = real_record

    monkeypatch.undo()
    resumed, _, resumed_client = _router_with_cards(tmp_path, chat_ids=(100, 200))
    resumed._sweep_lifecycle_cards()

    assert sorted(message["chat_id"] for message in resumed_client.sent) == [100, 200]


def test_a_fully_settled_database_reads_no_event_history(tmp_path):
    """종결 인덱스의 목표는 "비용이 열린 승인 수에 비례"다.

    unsettled가 하나도 없는데 ack·completion·resolution failure 전체를 읽어
    역직렬화하면, 줄이려던 비용이 그대로 남는다.

    복구 미리보기가 읽는 이벤트는 여기서 세지 않는다. 그것은 이 sweep이 만든
    비용이 아니라 `_sweep_recovery_notifications`가 매 poll 이미 치르고 있는
    비용이고, blocker 판정을 SQL로 옮기면 그 로직이 두 벌이 된다.
    """
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1")
    router._sweep_lifecycle_cards()
    router._sweep_lifecycle_cards()  # 종결 표시가 남는 시점

    scanned: list[str] = []
    real_list = store.list_system_events_by_type
    store.list_system_events_by_type = lambda event_type, **kwargs: (
        scanned.append(event_type) or real_list(event_type, **kwargs)
    )
    router._sweep_lifecycle_cards()
    store.list_system_events_by_type = real_list

    assert {
        "telegram_approval_ack",
        "signal_approval_completed",
        "telegram_approval_resolution_failed",
        "telegram_approval_pending",
    }.isdisjoint(str(event_type) for event_type in scanned)


def test_the_stage_lookup_is_scoped_to_the_approvals_still_open(tmp_path):
    """열린 승인이 있어도 전체 이력을 읽을 이유는 없다.

    전체를 접어 dict로 만든 뒤 열린 승인만 꺼내 쓰면, 읽고 역직렬화하는 양은
    줄지 않는다 — 줄어야 하는 것이 바로 그 양이다.
    """
    router, store, _ = _router_with_cards(tmp_path)
    for index in range(3):
        _dispatch_approval(router, approval_id=f"appr_old_{index}")
        _save_ack(store, approval_id=f"appr_old_{index}")
    _dispatch_approval(router, approval_id="appr_open")
    _save_ack(store, approval_id="appr_open")

    acks = store.latest_payloads_by_approval_id("telegram_approval_ack", ["appr_open"])

    assert set(acks) == {"appr_open"}


def test_the_latest_payload_wins_when_an_approval_has_several(tmp_path):
    """스코프를 좁히면서 "마지막 것이 이긴다"를 잃으면 안 된다.

    재개가 부분 완료를 남긴 뒤 다시 완료를 남기는 경우가 그렇다 — 앞의 것을
    택하면 반쯤 집행된 회전이 완료로 보인다.
    """
    router, store, _ = _router_with_cards(tmp_path)
    _dispatch_approval(router, approval_id="appr_1")
    _save_completed(store, approval_id="appr_1", orders_failed=1)
    _save_completed(store, approval_id="appr_1", orders_failed=0)

    completions = store.latest_payloads_by_approval_id("signal_approval_completed", ["appr_1"])

    assert completions["appr_1"]["orders_failed"] == 0


def test_an_open_approval_does_not_drag_the_whole_history_in(tmp_path):
    """열린 승인이 하나 있다고 전체 이력을 읽을 이유는 없다.

    전체를 접어 놓고 열린 승인만 꺼내 쓰면 답은 같지만 비용은 그대로다 —
    종결 인덱스가 줄이려던 것이 정확히 그 비용이다.
    """
    router, store, _ = _router_with_cards(tmp_path)
    for index in range(3):
        _dispatch_approval(router, approval_id=f"appr_old_{index}")
        _save_ack(store, approval_id=f"appr_old_{index}")
        _save_completed(store, approval_id=f"appr_old_{index}")
    router._sweep_lifecycle_cards()
    router._sweep_lifecycle_cards()  # 종결 표시가 남는 시점
    _dispatch_approval(router, approval_id="appr_open")
    _save_ack(store, approval_id="appr_open")

    scanned: list[str] = []
    real_list = store.list_system_events_by_type
    store.list_system_events_by_type = lambda event_type, **kwargs: (
        scanned.append(event_type) or real_list(event_type, **kwargs)
    )
    router._sweep_lifecycle_cards()
    store.list_system_events_by_type = real_list

    assert _stage_of(store, "approval:appr_open") == "in_progress"
    assert {
        "telegram_approval_ack",
        "signal_approval_completed",
        "telegram_approval_resolution_failed",
        "telegram_approval_pending",
    }.isdisjoint(str(event_type) for event_type in scanned)

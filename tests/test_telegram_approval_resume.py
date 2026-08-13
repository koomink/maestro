import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from maestro.approval.models import ApprovalRequest, PendingApprovalEnvelope
from maestro.config.loader import load_config
from maestro.integrations.telegram.handlers import TelegramOperatorCommandRouter
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import SignalApprovalSummary
from maestro.state.store import StateStore


def _telegram_config_path(tmp_path, *, chat_ids=(100,)) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["market_session"] = {
        "required": False,
        "timezone": "Asia/Seoul",
        "open": "09:00",
        "close": "15:30",
        "weekdays": [0, 1, 2, 3, 4],
        "holidays": [],
    }
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


def _backdate_all_events(store, *, days: int) -> None:
    """모든 system_events를 과거로 옮긴다. 정합성 판정이 시간 창에 의존하는지
    확인하기 위한 장치다."""
    moved = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE system_events SET created_at = ?", (moved,))


class FakeTelegramClient:
    def __init__(self, *, failing_chat_ids=()) -> None:
        self.sent_messages: list[dict] = []
        self.failing_chat_ids = set(failing_chat_ids)

    def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.failing_chat_ids:
            raise RuntimeError(f"telegram send failed for chat {chat_id}")
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return {"result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, *, offset=None, timeout_seconds=0, allowed_updates=None):
        return {"ok": True, "result": []}

    def answer_callback_query(self, callback_query_id, text=""):
        return {"ok": True}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return {"ok": True}


class _StubRouter(TelegramOperatorCommandRouter):
    """resolve_pending_signal_approval만 대체한 라우터.

    실제 orchestrator를 띄우지 않고 집행 성공/실패를 제어한다.
    """

    def __init__(self, *args, resolve_error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._resolve_error = resolve_error
        self.resolved_decisions = []

    def _run_resolution(self, envelope, decision):
        self.resolved_decisions.append(decision)
        if self._resolve_error is not None:
            raise self._resolve_error
        return SignalApprovalSummary(
            signal_run_id=envelope.signal_run_id,
            run_id=envelope.run_id,
            orders_created=len(envelope.orders),
            orders_submitted=len(envelope.orders),
            approval_status=decision.status,
        )


def _router(tmp_path, *, resolve_error=None, chat_ids=(100,), failing_chat_ids=()):
    config = load_config(_telegram_config_path(tmp_path, chat_ids=chat_ids))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    router = _StubRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=FakeTelegramClient(failing_chat_ids=failing_chat_ids),
        resolve_error=resolve_error,
    )
    return router, store


def _save_pending_envelope(
    store,
    *,
    approval_id,
    order_count=1,
    signal_run_id=None,
    expires_in=timedelta(hours=1),
    reminder_seconds=None,
    created_ago=timedelta(0),
):
    now = datetime.now(UTC)
    created_at = now - created_ago
    signal_run_id = signal_run_id or f"signal_{approval_id}"
    orders = [
        {
            "order_id": f"ord_{approval_id}_{index}",
            "symbol": "069500",
            "side": "buy",
            "quantity": 10,
            "notional": 712_000.0,
        }
        for index in range(order_count)
    ]
    envelope = PendingApprovalEnvelope(
        approval_id=approval_id,
        run_id=f"run_{approval_id}",
        signal_run_id=signal_run_id,
        request=ApprovalRequest(
            approval_id=approval_id,
            run_id=f"run_{approval_id}",
            created_at=created_at,
            expires_at=now + expires_in,
            channel="telegram",
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
        expires_at=now + expires_in,
        duplicate_key=f"telegram-approval-pending:{approval_id}",
    )
    store.save_system_event(
        envelope.run_id, "telegram_approval_pending", envelope.model_dump(mode="json")
    )
    return envelope


def _save_ack(store, *, approval_id, status, schema_version=None):
    payload = {
        "approval_id": approval_id,
        "signal_run_id": f"signal_{approval_id}",
        "status": status,
        "decided_by": "telegram:tester",
        "decided_at": datetime.now(UTC).isoformat(),
        "duplicate_key": f"telegram-approval-ack:{approval_id}",
    }
    if schema_version is not None:
        payload["schema_version"] = schema_version
    store.save_system_event(f"run_{approval_id}", "telegram_approval_ack", payload)


def _save_completed(store, *, approval_id):
    store.save_system_event(
        f"run_{approval_id}",
        "telegram_approval_resolution_completed",
        {
            "approval_id": approval_id,
            "status": "approved",
            "attempt": 1,
            "duplicate_key": f"telegram-approval-completed:{approval_id}",
        },
    )


def _save_stale_resume_claim(store, *, approval_id, attempt, age_seconds):
    claimed_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
    store.save_system_event(
        f"run_{approval_id}",
        "telegram_approval_resume_claim",
        {
            "approval_id": approval_id,
            "run_id": f"run_{approval_id}",
            "attempt": attempt,
            "claimed_at": claimed_at.isoformat(),
            "duplicate_key": f"telegram-approval-resume:{approval_id}:a{attempt}",
        },
    )


def _latest_payload(store, event_type):
    rows = store.list_system_events_by_type(event_type, limit=1)
    assert rows, f"no {event_type} event"
    return rows[0]["payload"]


def test_terminal_approval_ids_collects_acked_approvals(tmp_path):
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved")

    assert router._terminal_approval_ids() == {"appr_1"}


def test_successful_resolution_records_completed_event(tmp_path):
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_1")

    router._resolve_async_approval(
        envelope,
        status="approved",
        decided_by="telegram:tester",
        reason="test",
    )

    ack = _latest_payload(store, "telegram_approval_ack")
    assert ack["schema_version"] == 2
    completed = _latest_payload(store, "telegram_approval_resolution_completed")
    assert completed["approval_id"] == "appr_1"
    assert completed["status"] == "approved"
    assert completed["attempt"] == 1
    assert completed["duplicate_key"] == "telegram-approval-completed:appr_1"


def test_acked_but_unresolved_approval_is_not_terminal(tmp_path):
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)

    assert router._terminal_approval_ids() == set()
    assert router._pending_async_approval("appr_1") is not None


def test_legacy_ack_without_schema_version_stays_terminal(tmp_path):
    # 3a 이전에 정상 완료된 승인이 재집행되면 안 된다.
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")  # schema_version 없음

    assert router._terminal_approval_ids() == {"appr_legacy"}
    assert router._pending_async_approval("appr_legacy") is None


def test_completed_approval_is_terminal(tmp_path):
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)
    _save_completed(store, approval_id="appr_1")

    assert router._terminal_approval_ids() == {"appr_1"}


def test_old_handler_semantics_still_settle_completed_approvals(tmp_path):
    """구버전은 ack만 보고 종결 판정한다. 새 코드가 남긴 ack에도
    status/decided_by가 그대로 있으므로 구버전이 재실행하지 않는다."""
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    router._resolve_async_approval(
        envelope, status="approved", decided_by="telegram:tester", reason="test"
    )

    legacy_acked = {
        str(row["payload"].get("approval_id"))
        for row in store.list_system_events_by_type("telegram_approval_ack", limit=None)
    }
    assert legacy_acked == {"appr_1"}  # 구버전 판정 로직과 동일한 식


def test_mixed_legacy_and_new_events_are_classified_independently(tmp_path):
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")
    _save_pending_envelope(store, approval_id="appr_new")
    _save_ack(store, approval_id="appr_new", status="approved", schema_version=2)

    assert router._terminal_approval_ids() == {"appr_legacy"}


def test_v2_ack_without_completed_is_rollback_unsafe(tmp_path):
    """이 상태에서 구버전으로 롤백하면 승인된 주문이 유실된다.
    배포 확인 절의 롤백 절차가 quiesce 아래에서 검사해야 하는 상태다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)

    # 구버전 판정식(ack만 조회)은 종결로 본다 = 롤백 시 유실
    legacy_terminal = {
        str(row["payload"].get("approval_id"))
        for row in store.list_system_events_by_type("telegram_approval_ack", limit=None)
    }
    assert legacy_terminal == {"appr_1"}
    assert router._terminal_approval_ids() == set()  # 신버전은 미완으로 본다


def test_failed_resolution_records_no_completed_event(tmp_path):
    router, store = _router(tmp_path, resolve_error=ValueError("stale broker snapshot"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")

    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope,
            status="approved",
            decided_by="telegram:tester",
            reason="test",
        )

    assert _latest_payload(store, "telegram_approval_ack")["schema_version"] == 2
    assert (
        store.list_system_events_by_type("telegram_approval_resolution_completed", limit=10)
        == []
    )


def test_legacy_ack_with_resolution_failure_is_isolated(tmp_path):
    """2026-08-07 형태: ack + resolution_failed, approvals 행 없음."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")
    store.save_system_event(
        "run_appr_legacy",
        "telegram_approval_resolution_failed",
        {"approval_id": "appr_legacy", "error_type": "ValueError"},
    )

    router._sweep_pending_approvals()

    assert any("확인이 필요" in message["text"] for message in router.client.sent_messages)
    assert len(store.list_system_events_by_type(
        "telegram_approval_needs_attention", limit=None
    )) == 1


def test_legacy_ack_only_crash_is_isolated(tmp_path):
    """ack 직후 프로세스 종료 / config 로드 실패: 후속 기록이 전혀 없다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")

    router._sweep_pending_approvals()

    assert any("확인이 필요" in message["text"] for message in router.client.sent_messages)


def test_legacy_ack_with_approvals_row_but_no_completion_needs_reconciliation(tmp_path):
    """가장 위험한 상태: 집행에 진입했으나 완료 기록이 없다.
    주문이 이미 브로커로 나갔을 수 있으므로 침묵하면 안 된다."""
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")
    store.save_approval(envelope.run_id, "appr_legacy", {"decision": {"status": "approved"}})

    router._sweep_pending_approvals()

    assert any("브로커" in message["text"] for message in router.client.sent_messages)
    assert len(store.list_system_events_by_type(
        "telegram_approval_needs_attention", limit=None
    )) == 1


def test_one_completed_group_does_not_hide_another_unresolved_group(tmp_path):
    """한 signal run이 여러 승인 그룹으로 나뉜 경우(다중 계좌·다중 전략).
    A 그룹이 완료됐다고 B 그룹의 유실이 가려지면 안 된다."""
    router, store = _router(tmp_path)
    signal_run_id = "signal_shared"
    envelope_a = _save_pending_envelope(
        store, approval_id="appr_a", signal_run_id=signal_run_id
    )
    _save_pending_envelope(store, approval_id="appr_b", signal_run_id=signal_run_id)
    _save_ack(store, approval_id="appr_a", status="approved")
    _save_ack(store, approval_id="appr_b", status="approved")
    # A만 완료 — 구 이벤트라 approval_id가 없다
    store.save_system_event(
        envelope_a.run_id,
        "signal_approval_completed",
        {"signal_run_id": signal_run_id, "approval_status": "approved"},
    )

    router._sweep_pending_approvals()

    # 그룹이 둘이라 어느 쪽 완료인지 모호하다 → 둘 다 알린다 (침묵보다 낫다)
    notified = {
        row["payload"]["approval_id"]
        for row in store.list_system_events_by_type(
            "telegram_approval_needs_attention", limit=None
        )
    }
    assert notified == {"appr_a", "appr_b"}


def test_completion_with_approval_id_matches_exactly(tmp_path):
    """신규 완료 이벤트는 approval_id가 있어 그룹 추론이 필요 없다."""
    router, store = _router(tmp_path)
    signal_run_id = "signal_shared"
    envelope_a = _save_pending_envelope(
        store, approval_id="appr_a", signal_run_id=signal_run_id
    )
    _save_pending_envelope(store, approval_id="appr_b", signal_run_id=signal_run_id)
    _save_ack(store, approval_id="appr_a", status="approved")
    _save_ack(store, approval_id="appr_b", status="approved")
    store.save_system_event(
        envelope_a.run_id,
        "signal_approval_completed",
        {
            "approval_id": "appr_a",
            "signal_run_id": signal_run_id,
            "approval_status": "approved",
        },
    )

    router._sweep_pending_approvals()

    notified = {
        row["payload"]["approval_id"]
        for row in store.list_system_events_by_type(
            "telegram_approval_needs_attention", limit=None
        )
    }
    assert notified == {"appr_b"}  # A는 완료가 증명됐다


def test_legacy_ack_with_completion_evidence_is_not_isolated(tmp_path):
    """완료 기록이 있으면 정상 종결이다 — 알리지 않는다."""
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")
    store.save_approval(envelope.run_id, "appr_legacy", {"decision": {"status": "approved"}})
    store.save_system_event(
        envelope.run_id,
        "signal_approval_completed",
        {"signal_run_id": envelope.signal_run_id, "approval_status": "approved"},
    )

    router._sweep_pending_approvals()

    assert store.list_system_events_by_type(
        "telegram_approval_needs_attention", limit=None
    ) == []


def test_sweep_resumes_unresolved_approval_with_recorded_decision(tmp_path):
    router, store = _router(tmp_path, resolve_error=ValueError("stale broker snapshot"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )

    router._resolve_error = None  # 다음 시도는 성공한다
    router._sweep_pending_approvals()

    completed = _latest_payload(store, "telegram_approval_resolution_completed")
    assert completed["approval_id"] == "appr_1"
    assert completed["attempt"] == 2
    # 운영자 재클릭 없이 기록된 결정을 그대로 썼다
    assert router.resolved_decisions[-1].status == "approved"
    assert router.resolved_decisions[-1].decided_by == "telegram:tester"


def test_in_flight_attempt_blocks_a_second_entry(tmp_path):
    """종료 기록이 없는 attempt가 있으면 다음 진입은 같은 번호를 계산해 거절된다."""
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)
    _save_stale_resume_claim(store, approval_id="appr_1", attempt=2, age_seconds=10)

    assert router._next_resume_attempt("appr_1") == 2
    assert router._claim_resume(envelope, 2) is False

    router._sweep_pending_approvals()
    assert store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=None
    ) == []


def test_each_failed_attempt_records_a_finished_event(tmp_path):
    router, store = _router(tmp_path, resolve_error=ValueError("boom"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )

    router._sweep_pending_approvals()  # attempt 2 — 또 실패
    router._sweep_pending_approvals()  # attempt 3

    finished = store.list_system_events_by_type(
        "telegram_approval_resume_finished", limit=None
    )
    assert sorted(row["payload"]["attempt"] for row in finished) == [2, 3]
    assert {row["payload"]["outcome"] for row in finished} == {"failed"}


def test_claim_abandoned_before_resolution_is_reclaimed(tmp_path):
    """claim 직후 프로세스가 죽은 상황: lease 만료 후 재개가 이어져야 한다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)
    _save_stale_resume_claim(store, approval_id="appr_1", attempt=2, age_seconds=1000)

    router._sweep_pending_approvals()

    outcomes = [
        row["payload"]["outcome"]
        for row in store.list_system_events_by_type(
            "telegram_approval_resume_finished", limit=None
        )
    ]
    assert "abandoned" in outcomes
    completed = _latest_payload(store, "telegram_approval_resolution_completed")
    assert completed["attempt"] == 3


def test_abandoned_attempts_do_not_consume_the_retry_budget(tmp_path):
    """lease 회수만 반복되면 실제 집행은 한 번도 없었으므로 예산을 깎지 않는다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)
    for attempt in range(2, 8):  # abandoned 6건 — _MAX_RESUME_ATTEMPT(4)를 넘는다
        router._record_resume_finished(
            run_id="run_appr_1", approval_id="appr_1", attempt=attempt, outcome="abandoned"
        )

    assert router._executed_resume_attempts("appr_1") == 0
    router._sweep_pending_approvals()

    # 예산이 남아 있으므로 실제 재개가 일어난다 (attention으로 빠지지 않는다)
    assert _latest_payload(
        store, "telegram_approval_resolution_completed"
    )["approval_id"] == "appr_1"
    assert store.list_system_events_by_type(
        "telegram_approval_needs_attention", limit=None
    ) == []


def test_resume_survives_more_than_2000_intervening_events(tmp_path):
    """정합성 판정이 조회 창 크기에 의존하지 않는다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_1")
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)
    for index in range(2100):
        store.save_system_event(
            "run_noise",
            "telegram_approval_ack",
            {"approval_id": f"appr_noise_{index}", "status": "approved", "schema_version": 2},
        )

    assert "appr_1" not in router._terminal_approval_ids()
    router._sweep_pending_approvals()

    assert _latest_payload(
        store, "telegram_approval_resolution_completed"
    )["approval_id"] == "appr_1"


def test_repeated_resume_failures_stop_and_notify_operator(tmp_path):
    router, store = _router(tmp_path, resolve_error=ValueError("boom"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )

    for _ in range(6):
        router._sweep_pending_approvals()

    claims = store.list_system_events_by_type("telegram_approval_resume_claim", limit=20)
    assert len(claims) == 3  # attempt 2,3,4 까지만 (_MAX_RESUME_ATTEMPT = 4)
    assert any("확인이 필요" in message["text"] for message in router.client.sent_messages)
    # 확인 필요 알림은 승인당 1회만 나간다
    attention = store.list_system_events_by_type("telegram_approval_needs_attention", limit=10)
    assert len(attention) == 1


def test_approval_that_entered_execution_is_not_auto_resumed(tmp_path):
    """approvals 행이 있으면 브로커 제출이 일어났을 수 있다 — fail-closed."""
    router, store = _router(tmp_path, resolve_error=ValueError("broker timeout"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )
    store.save_approval(envelope.run_id, "appr_1", {"decision": {"status": "approved"}})

    router._sweep_pending_approvals()

    assert store.list_system_events_by_type(
        "telegram_approval_resume_claim", limit=None
    ) == []
    assert any(
        "확인이 필요" in message["text"] for message in router.client.sent_messages
    )


def test_expired_approval_with_recorded_decision_is_still_resumed(tmp_path):
    """만료 시각이 지났어도 결정이 기록됐으면 재개 대상이다.

    운영자는 만료 전에 결정했고 집행만 실패했다 — 만료를 이유로 재개를
    포기하면 그 결정이 영원히 집행되지 않는다. 만료 재판정은 시도하지 않는다
    (기록된 결정을 만료로 덮어쓰면 안 된다).
    """
    router, store = _router(
        tmp_path, resolve_error=ValueError("boom")
    )
    _save_pending_envelope(
        store, approval_id="appr_1", expires_in=timedelta(seconds=-60)
    )
    _save_ack(store, approval_id="appr_1", status="approved", schema_version=2)

    router._sweep_pending_approvals()

    # 재개가 소유한다: attempt 2가 claim되고 실패로 종결된다
    claims = store.list_system_events_by_type("telegram_approval_resume_claim", limit=None)
    assert [row["payload"]["attempt"] for row in claims] == [2]
    # 만료 재판정은 시도조차 하지 않는다
    statuses = {
        row["payload"].get("status")
        for row in store.list_system_events_by_type(
            "telegram_approval_resolution_failed", limit=None
        )
    }
    assert statuses == {"approved"}
    assert router.resolved_decisions[-1].status == "approved"


def test_decided_approval_stops_receiving_reminders(tmp_path):
    """결정이 기록된 승인은 재개 경로가 소유한다 — 리마인더를 더 보내지 않는다.

    ack가 있는데도 리마인더가 계속 나가면 운영자는 이미 누른 버튼을 다시
    누르라는 재촉을 받는다. 다시 눌러도 ack duplicate_key에 걸려 실패한다.
    """
    router, store = _router(tmp_path, resolve_error=ValueError("boom"))
    envelope = _save_pending_envelope(
        store,
        approval_id="appr_1",
        reminder_seconds=[600],
        created_ago=timedelta(minutes=20),
    )
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )
    router.client.sent_messages.clear()

    router._sweep_pending_approvals()  # 재개는 또 실패한다 — 승인은 미종결로 남는다

    assert store.list_system_events_by_type("telegram_approval_reminder", limit=None) == []
    assert router.client.sent_messages == []
    # 미종결이라 다음 poll에서도 재개 대상이다 (조용해진 것이지 사라진 게 아니다)
    assert store.list_system_events_by_type(
        "telegram_approval_resolution_completed", limit=None
    ) == []


def test_stale_snapshot_failure_is_recovered_on_next_poll(tmp_path):
    """2026-08-07 운영 사고: 승인 ack 직후 stale snapshot으로 집행 실패.
    구 코드에서는 재클릭도 거절돼 승인이 영구 유실됐다."""
    router, store = _router(
        tmp_path,
        resolve_error=ValueError(
            "Signal package stale broker snapshot: "
            "account_id=toss_brokerage age_seconds=1025 max_age_seconds=900"
        ),
    )
    envelope = _save_pending_envelope(store, approval_id="appr_1")

    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:u1wHK0B", reason="button"
        )

    # 실패 직후: 승인은 종결이 아니며 재개 대상이다
    assert router._terminal_approval_ids() == set()
    assert _latest_payload(store, "telegram_approval_resolution_failed")["approval_id"] == "appr_1"

    # 스냅샷이 갱신되면 다음 poll에서 자동 재개된다
    router._resolve_error = None
    router.poll_once()

    assert router._terminal_approval_ids() == {"appr_1"}
    completed = _latest_payload(store, "telegram_approval_resolution_completed")
    assert completed["status"] == "approved"
    assert completed["attempt"] == 2

    # poll_once는 sweep 실패를 삼키고 telegram_command(status=error)로만 남긴다
    # (Task 4). 통과가 "재개 성공" 때문인지 "예외가 조용히 삼켜졌지만 우연히
    # 이전 상태가 조건을 만족했기 때문"인지 구분하려면 이 부정 조건도 함께
    # 확인해야 한다.
    swallowed_failures = [
        row
        for row in store.list_system_events_by_type("telegram_command", limit=None)
        if row["payload"].get("status") == "error"
    ]
    assert swallowed_failures == []


def test_undecided_approval_still_receives_its_reminder(tmp_path):
    """결정이 없는 승인의 리마인더는 그대로 나간다 (재개 게이트의 부수 피해 방지)."""
    router, store = _router(tmp_path)
    _save_pending_envelope(
        store,
        approval_id="appr_1",
        reminder_seconds=[600],
        created_ago=timedelta(minutes=20),
    )

    router._sweep_pending_approvals()

    reminders = store.list_system_events_by_type("telegram_approval_reminder", limit=None)
    assert [row["payload"]["reminder_seconds"] for row in reminders] == [600]
    assert any(
        "아직 응답을 기다리고 있어요" in message["text"]
        for message in router.client.sent_messages
    )


def test_successful_resume_tells_the_operator_the_decision_result(tmp_path):
    """F1: 자동 재개가 성공하면 실제 주문이 나간다. 운영자의 마지막 메시지는
    "잠시 후 다시 시도해 주세요"였으므로, 알리지 않으면 증권사 앱에서 같은 주문을
    손으로 다시 낼 수 있다 — 이 브랜치가 막으려는 바로 그 피해다."""
    router, store = _router(tmp_path, resolve_error=ValueError("stale broker snapshot"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )
    router._resolve_error = None
    router.client.sent_messages.clear()

    router._sweep_pending_approvals()

    assert _latest_payload(store, "telegram_approval_resolution_completed")["attempt"] == 2
    decision_messages = [
        message for message in router.client.sent_messages if "승인 완료" in message["text"]
    ]
    assert [message["chat_id"] for message in decision_messages] == [100]
    assert "1건" in decision_messages[0]["text"]


def test_resume_completion_notice_is_sent_only_once_per_chat(tmp_path):
    """F1: 같은 승인의 완료 통지가 poll마다 반복되면 안 된다."""
    router, store = _router(tmp_path, resolve_error=ValueError("boom"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )
    router._resolve_error = None
    router._sweep_pending_approvals()
    router.client.sent_messages.clear()

    router._sweep_pending_approvals()
    router._sweep_pending_approvals()

    assert router.client.sent_messages == []


def test_rejected_resume_reports_the_rejection_not_a_submission(tmp_path):
    """F1: 거절 결정의 재개도 알리되, 주문이 나간 것처럼 보이면 안 된다."""
    router, store = _router(tmp_path, resolve_error=ValueError("boom"))
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="rejected", decided_by="telegram:tester", reason="test"
        )
    router._resolve_error = None
    router.client.sent_messages.clear()

    router._sweep_pending_approvals()

    assert any("거절했어요" in message["text"] for message in router.client.sent_messages)
    assert not any("승인 완료" in message["text"] for message in router.client.sent_messages)


def test_one_unnotifiable_chat_does_not_block_a_newer_approval(tmp_path):
    """F2: 알림 실패가 sweep 전체를 중단시키면, 그 뒤의 모든 승인이 매 poll마다
    조용히 재개되지 못한다. 재개 루프는 오래된 것부터 돈다."""
    router, store = _router(tmp_path, failing_chat_ids=(100,))
    blocked = _save_pending_envelope(store, approval_id="appr_blocked")
    _save_ack(store, approval_id="appr_blocked", status="approved", schema_version=2)
    # approvals 행이 있으면 자동 재개 대신 알림 경로로 간다 (fail-closed)
    store.save_approval(blocked.run_id, "appr_blocked", {"decision": {"status": "approved"}})
    _save_pending_envelope(store, approval_id="appr_newer")
    _save_ack(store, approval_id="appr_newer", status="approved", schema_version=2)

    router._sweep_pending_approvals()

    resumed = {
        row["payload"]["approval_id"]
        for row in store.list_system_events_by_type(
            "telegram_approval_resolution_completed", limit=None
        )
    }
    assert resumed == {"appr_newer"}


def test_notify_failure_records_the_error_and_retries_only_that_chat(tmp_path):
    """F2: 성공한 채팅은 다시 알리지 않고, 실패한 채팅만 다음 poll에서 재시도한다."""
    router, store = _router(tmp_path, chat_ids=(100, 200), failing_chat_ids=(100,))
    _save_pending_envelope(store, approval_id="appr_legacy")
    _save_ack(store, approval_id="appr_legacy", status="approved")

    router._sweep_pending_approvals()

    assert [message["chat_id"] for message in router.client.sent_messages] == [200]
    assert any(
        row["payload"].get("status") == "error"
        for row in store.list_system_events_by_type("telegram_command", limit=None)
    )

    router.client.failing_chat_ids = set()
    router._sweep_pending_approvals()

    # 200은 이미 받았으므로 다시 받지 않는다. 100만 뒤늦게 받는다.
    assert [message["chat_id"] for message in router.client.sent_messages] == [200, 100]


def test_unresolved_ack_older_than_the_consistency_window_is_still_resumed(tmp_path):
    """F3: 긴 장애 뒤 90일이 지난 미완 승인이 재개 루프에서 사라지면 안 된다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_old")
    _save_ack(store, approval_id="appr_old", status="approved", schema_version=2)
    _backdate_all_events(store, days=200)

    assert router._terminal_approval_ids() == set()
    router._sweep_pending_approvals()

    completed = _latest_payload(store, "telegram_approval_resolution_completed")
    assert completed["approval_id"] == "appr_old"


def test_never_decided_approval_pushed_out_of_the_scan_window_still_expires(tmp_path):
    """F3: 만료·리마인더 스캔의 개수 창(limit=2000) 밖으로 밀린 승인은 영원히
    만료되지 않는다 — 결정도 없고 종결도 없는 채로 남는다."""
    router, store = _router(tmp_path)
    _save_pending_envelope(store, approval_id="appr_old", expires_in=timedelta(seconds=-60))
    for index in range(2100):
        # approval_id가 없는 이벤트는 스캔 초반에 걸러지므로 순수한 잡음이다
        store.save_system_event("run_noise", "telegram_approval_pending", {"noise": index})

    router._sweep_pending_approvals()

    assert {decision.status for decision in router.resolved_decisions} == {"expired"}


def test_v2_ack_with_missing_pending_envelope_is_surfaced(tmp_path):
    """F4: envelope이 사라진 v2 ack는 재개도 알림도 없이 영원히 묻히고,
    롤백 preflight를 영구히 막는다."""
    router, store = _router(tmp_path)
    _save_ack(store, approval_id="appr_orphan", status="approved", schema_version=2)

    router._sweep_pending_approvals()

    assert any("확인이 필요" in message["text"] for message in router.client.sent_messages)
    notified = {
        row["payload"]["approval_id"]
        for row in store.list_system_events_by_type(
            "telegram_approval_needs_attention", limit=None
        )
    }
    assert notified == {"appr_orphan"}


def test_record_resolution_completed_checks_and_writes_under_the_writer_lock(tmp_path):
    """F5: 형제 기록들과 달리 락 밖에서 check-then-write 하면, 경합 시
    IntegrityError가 **주문이 나간 뒤에** 터진다."""
    router, store = _router(tmp_path)
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    observed: list[bool] = []
    original = store.duplicate_key_exists

    def _spy(duplicate_key):
        if duplicate_key.startswith("telegram-approval-completed:"):
            observed.append(store.holds_writer_lock())
        return original(duplicate_key)

    store.duplicate_key_exists = _spy
    router._resolve_async_approval(
        envelope, status="approved", decided_by="telegram:tester", reason="test"
    )

    assert observed == [True]


def test_resume_notice_is_retried_for_the_chat_that_failed(tmp_path):
    """G1: 완료 통지가 재개 루프에서만 나가면 전송 실패 한 번으로 영원히 사라진다.

    승인은 그 순간 이미 종결(resolution_completed)이라 다음 sweep이 다시 보지
    않는다. F2의 채팅별 재시도는 legacy 알림에만 살아 있고 완료 통지에는 죽어
    있다 — 실주문이 나갔는데 운영자는 끝내 아무 말도 듣지 못한다.
    """
    router, store = _router(
        tmp_path,
        chat_ids=(100, 200),
        resolve_error=ValueError("stale broker snapshot"),
        failing_chat_ids=(100,),
    )
    envelope = _save_pending_envelope(store, approval_id="appr_1")
    with pytest.raises(ValueError):
        router._resolve_async_approval(
            envelope, status="approved", decided_by="telegram:tester", reason="test"
        )
    router._resolve_error = None
    router.client.sent_messages.clear()

    router._sweep_pending_approvals()

    assert _latest_payload(store, "telegram_approval_resolution_completed")["attempt"] == 2
    # 200만 받았다. 100은 전송이 실패했으므로 자기 키가 기록되지 않았다.
    submitted = [
        message["chat_id"]
        for message in router.client.sent_messages
        if "승인 완료" in message["text"]
    ]
    assert submitted == [200]

    router.client.failing_chat_ids = set()
    router._sweep_pending_approvals()
    router._sweep_pending_approvals()

    # 100만 뒤늦게 받고, 200은 중복 수신하지 않는다.
    submitted = [
        message["chat_id"]
        for message in router.client.sent_messages
        if "승인 완료" in message["text"]
    ]
    assert submitted == [200, 100]


def test_malformed_old_envelope_does_not_block_a_newer_resume(tmp_path):
    """G2: 재개 루프는 오래된 것부터 돌고 envelope 검증에 격리가 없다.

    F3가 시간 창을 없앤 뒤로는 과거 envelope 전부가 매 poll마다 검증된다 —
    필수 필드가 빠진 옛 행 하나가 sweep 전체를 매번 중단시켜 그 뒤의 모든
    승인을 영구히 굶긴다.
    """
    router, store = _router(tmp_path)
    store.save_system_event(
        "run_appr_bad", "telegram_approval_pending", {"approval_id": "appr_bad"}
    )
    _save_ack(store, approval_id="appr_bad", status="approved", schema_version=2)
    _save_pending_envelope(store, approval_id="appr_good")
    _save_ack(store, approval_id="appr_good", status="approved", schema_version=2)

    router._sweep_pending_approvals()

    resumed = {
        row["payload"]["approval_id"]
        for row in store.list_system_events_by_type(
            "telegram_approval_resolution_completed", limit=None
        )
    }
    assert resumed == {"appr_good"}
    quarantined = store.list_system_events_by_type(
        "telegram_approval_resume_quarantined", limit=None
    )
    assert [row["payload"]["approval_id"] for row in quarantined] == ["appr_bad"]
    assert any("확인이 필요" in message["text"] for message in router.client.sent_messages)

    router._sweep_pending_approvals()

    # 격리 기록은 poll마다 쌓이지 않는다.
    assert (
        len(
            store.list_system_events_by_type(
                "telegram_approval_resume_quarantined", limit=None
            )
        )
        == 1
    )

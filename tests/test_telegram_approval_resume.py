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


def _telegram_config_path(tmp_path) -> Path:
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
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
    }
    config_path = tmp_path / "telegram_operator.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return {"result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, offset=None, timeout=0):
        return []

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


def _router(tmp_path, *, resolve_error=None):
    config = load_config(_telegram_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    router = _StubRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=FakeTelegramClient(),
        resolve_error=resolve_error,
    )
    return router, store


def _save_pending_envelope(store, *, approval_id, order_count=1, signal_run_id=None):
    now = datetime.now(UTC)
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
            created_at=now,
            expires_at=now + timedelta(hours=1),
            channel="telegram",
            order_count=len(orders),
            estimated_notional=sum(order["notional"] for order in orders),
            proposed_orders=orders,
        ),
        orders=orders,
        message="카드 본문",
        source_strategy_ids=["tranquillo"],
        account_ids=["kis_ps"],
        reminder_seconds=[],
        created_at=now,
        expires_at=now + timedelta(hours=1),
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

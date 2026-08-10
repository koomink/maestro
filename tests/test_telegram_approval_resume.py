from datetime import UTC, datetime, timedelta
from pathlib import Path

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

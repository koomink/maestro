from pathlib import Path

import yaml

from maestro.config.loader import load_config
from maestro.integrations.telegram.handlers import (
    _MAX_DISPATCH_RESUME_ATTEMPT,
    TelegramOperatorCommandRouter,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


class FakeTelegramClient:
    def __init__(self):
        self.sent_messages = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return {"ok": True, "result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, offset=None, timeout=0):
        return {"ok": True, "result": []}

    def answer_callback_query(self, callback_query_id, text=""):
        return {"ok": True}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return {"ok": True}


class _StubRouter(TelegramOperatorCommandRouter):
    """Replaces only the orchestrator call, the way _StubRouter does for
    resolution in test_telegram_approval_resume.py."""

    def __init__(self, *args, dispatch_error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._dispatch_error = dispatch_error
        self.dispatched: list[str] = []

    def _run_dispatch(self, signal_run_id):
        self.dispatched.append(signal_run_id)
        if self._dispatch_error is not None:
            raise self._dispatch_error
        # A real dispatch ends by recording that it settled.
        self.store.save_system_event(
            signal_run_id, "signal_approval_pending", {"signal_run_id": signal_run_id}
        )


def _telegram_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
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


def _router(tmp_path, *, dispatch_error=None, approval_config_path="set"):
    config_path = _telegram_config_path(tmp_path)
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    router = _StubRouter(
        config=config,
        store=store,
        audit=AuditLogger(config.audit.jsonl_path),
        client=FakeTelegramClient(),
        approval_config_path=config_path if approval_config_path == "set" else None,
        dispatch_error=dispatch_error,
    )
    return router, store


def _strand(store, signal_run_id="signal-1"):
    """Leave a package consumed with no settled event -- a dispatch that died
    inside the group loop."""
    store.mark_signal_package_consumed(signal_run_id, "run-1")
    return signal_run_id


def test_an_unfinished_dispatch_is_resumed(tmp_path):
    router, store = _router(tmp_path)
    signal_run_id = _strand(store)

    router._resume_incomplete_dispatches()

    assert router.dispatched == [signal_run_id]
    assert store.list_incomplete_signal_dispatches() == []


def test_a_settled_dispatch_is_left_alone(tmp_path):
    router, store = _router(tmp_path)
    store.mark_signal_package_consumed("signal-1", "run-1")
    store.save_system_event("signal-1", "signal_approval_pending", {"signal_run_id": "signal-1"})

    router._resume_incomplete_dispatches()

    assert router.dispatched == []


def test_a_resumed_run_is_not_dispatched_again_on_the_next_sweep(tmp_path):
    router, store = _router(tmp_path)
    _strand(store)

    router._resume_incomplete_dispatches()
    router._resume_incomplete_dispatches()

    assert len(router.dispatched) == 1


def test_a_failing_dispatch_gives_up_after_the_budget_and_tells_the_operator(tmp_path):
    router, store = _router(tmp_path, dispatch_error=RuntimeError("broker snapshot is stale"))
    _strand(store)

    for _ in range(_MAX_DISPATCH_RESUME_ATTEMPT + 3):
        router._resume_incomplete_dispatches()

    # Retrying forever would rewrite the same failure every poll for as long
    # as the operator leaves it alone.
    assert len(router.dispatched) == _MAX_DISPATCH_RESUME_ATTEMPT
    notices = store.list_system_events_by_type("telegram_dispatch_needs_attention", limit=None)
    assert len(notices) == 1
    assert notices[0]["payload"]["signal_run_id"] == "signal-1"


def test_a_failing_dispatch_does_not_starve_the_next_one(tmp_path):
    router, store = _router(tmp_path, dispatch_error=RuntimeError("nope"))
    _strand(store, "signal-1")
    _strand(store, "signal-2")

    router._resume_incomplete_dispatches()

    assert router.dispatched == ["signal-1", "signal-2"]


def test_nothing_is_resumed_without_an_approval_config(tmp_path):
    # The sweep has no config to build an orchestrator from, and guessing one
    # would dispatch real approvals against the wrong profile.
    router, store = _router(tmp_path, approval_config_path=None)
    _strand(store)

    router._resume_incomplete_dispatches()

    assert router.dispatched == []


def test_the_attention_notice_is_sent_once_per_chat(tmp_path):
    router, store = _router(tmp_path, dispatch_error=RuntimeError("nope"))
    _strand(store)

    for _ in range(_MAX_DISPATCH_RESUME_ATTEMPT + 5):
        router._resume_incomplete_dispatches()

    assert len(router.client.sent_messages) == 1


def test_a_claimed_attempt_is_not_entered_twice(tmp_path):
    router, store = _router(tmp_path)
    _strand(store)

    assert router._claim_dispatch_resume("signal-1", 1) is True
    assert router._claim_dispatch_resume("signal-1", 1) is False


def test_the_sweep_skips_an_attempt_another_poller_already_claimed(tmp_path):
    # Two operator processes poll the same store. Without the claim both would
    # enter the same attempt and dispatch the same signal run concurrently.
    router, store = _router(tmp_path)
    _strand(store)
    other_poller, _ = _router(tmp_path)
    assert other_poller._claim_dispatch_resume("signal-1", 1) is True

    router._resume_incomplete_dispatches()

    assert router.dispatched == []

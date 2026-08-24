"""While a migration owns the database, financial paths stand down.

MIGRATING means some legacy history is classified and some is not, so any
decision made from it can be wrong in the one direction that costs money.
INVALID means the markers contradict each other and nothing says which
generation a row belongs to. Both fail closed.

Read-only views are deliberately not gated. Production is quiesced for the real
migration, and a global write framework for `status` would be scope the safety
argument does not need.
"""

from __future__ import annotations

import pytest
from migration_fixtures import (
    claim_only,
    event_count,
    legacy_pending_request,
    publish_current_request,
)
from test_funding_workflow_resume import (
    FakeTelegramClient,
    _readonly_config_path,
    _signal_config_path,
    callback_update,
)

from maestro.config.loader import load_config
from maestro.integrations.telegram.handlers import TelegramOperatorCommandRouter
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state import migration_state as ms
from maestro.state.store import StateStore

GATED_SWEEPS = (
    "_resume_unresolved_approvals",
    "_resume_incomplete_dispatches",
    "_sweep_incomplete_workflows",
    "_converge_workflow_invariants",
)


@pytest.fixture
def operator_bot(tmp_path):
    config = load_config(_readonly_config_path(tmp_path))
    return TelegramOperatorCommandRouter(
        config=config,
        store=StateStore(config.state.sqlite_path, config.portfolio.initial_cash),
        audit=AuditLogger(config.audit.jsonl_path),
        client=FakeTelegramClient(),
        signal_config_path=_signal_config_path(tmp_path),
        approval_config_path=_signal_config_path(tmp_path),
    )


def _start(store):
    with store.writer_lock("test"):
        return ms.start_migration(store, "run-migrate")


def _complete(store):
    with store.writer_lock("test"):
        state = ms.start_migration(store, "run-migrate")
        ms.complete_migration(store, "run-migrate", cutoff=state.cutoff)


def _statuses(store, command):
    return [
        row["payload"].get("status")
        for row in store.list_system_events_by_type("telegram_command", limit=None)
        if row["payload"].get("command") == command
    ]


def test_no_block_before_any_migration(operator_bot):
    assert operator_bot._migration_block_reason() is None


def test_no_block_on_a_completed_migration(operator_bot):
    _complete(operator_bot.store)
    assert operator_bot._migration_block_reason() is None


def test_migrating_blocks(operator_bot):
    _start(operator_bot.store)
    assert operator_bot._migration_block_reason() == "migrating"


def test_invalid_markers_block_and_say_why(operator_bot):
    operator_bot.store.save_system_event(
        "r", ms.STARTED_EVENT, {"cutoff": "x", "duplicate_key": ms.STARTED_KEY}
    )
    assert operator_bot._migration_block_reason() == "invalid:malformed_started_marker"


@pytest.mark.parametrize("sweep", GATED_SWEEPS)
def test_every_recovery_sweep_stands_down_while_migrating(operator_bot, sweep, monkeypatch):
    store = operator_bot.store
    legacy_pending_request(store, "req-1")
    store.mark_signal_package_consumed("sig-1", "run-1")
    _start(store)
    called: list[str] = []
    monkeypatch.setattr(operator_bot, "_run_dispatch", lambda *_a: called.append("dispatch"))
    monkeypatch.setattr(
        operator_bot, "_resume_one_approval", lambda *_a: called.append("approval")
    )
    before = event_count(store)

    getattr(operator_bot, sweep)()

    assert called == []
    assert event_count(store) == before


@pytest.mark.parametrize("sweep", GATED_SWEEPS)
def test_every_recovery_sweep_stands_down_on_invalid_markers(operator_bot, sweep):
    store = operator_bot.store
    store.save_system_event(
        "r", ms.STARTED_EVENT, {"cutoff": "x", "duplicate_key": ms.STARTED_KEY}
    )
    before = event_count(store)

    getattr(operator_bot, sweep)()

    assert event_count(store) == before


def test_a_funding_confirm_callback_is_refused_while_migrating(operator_bot):
    store = operator_bot.store
    publish_current_request(store, "req-1")
    _start(store)

    assert operator_bot.process_update(callback_update("operator:funding:complete:req-1"))

    assert store.list_system_events_by_type("funding_workflow_claim", limit=None) == []
    assert _statuses(store, "/funding") == ["migration_blocked"]


def test_a_funding_cancel_callback_is_refused_while_migrating(operator_bot):
    store = operator_bot.store
    publish_current_request(store, "req-1")
    _start(store)

    assert operator_bot.process_update(callback_update("operator:funding:cancel:req-1"))

    assert store.list_system_events_by_type("funding_workflow_claim", limit=None) == []


def test_a_budget_callback_is_refused_while_migrating(operator_bot):
    store = operator_bot.store
    publish_current_request(store, "req-b", phase="budget")
    _start(store)

    assert operator_bot.process_update(callback_update("operator:budget:cancel:req-b"))

    assert store.list_system_events_by_type("funding_workflow_claim", limit=None) == []
    assert _statuses(store, "/budget") == ["migration_blocked"]


def test_a_budget_command_is_refused_while_migrating(operator_bot):
    store = operator_bot.store
    publish_current_request(store, "req-b", phase="budget")
    _start(store)

    operator_bot._process_budget_command("/budget req-b 3000000", 100, 100, "operator")

    assert store.list_system_events_by_type("funding_workflow_claim", limit=None) == []
    assert _statuses(store, "/budget") == ["migration_blocked"]


def test_a_workflow_resume_callback_is_refused_while_migrating(operator_bot):
    store = operator_bot.store
    publish_current_request(store, "req-1")
    claim_only(store, "req-1")
    _start(store)
    before = len(store.list_system_events_by_type("funding_workflow_claim", limit=None))

    assert operator_bot.process_update(callback_update("operator:wfresume:funding:req-1"))

    assert (
        len(store.list_system_events_by_type("funding_workflow_claim", limit=None)) == before
    )
    assert _statuses(store, "/wfresume") == ["migration_blocked"]


def test_an_async_approval_callback_is_refused_while_migrating(operator_bot):
    store = operator_bot.store
    store.save_system_event(
        "run-ap",
        "telegram_approval_pending",
        {"approval_id": "ap-1", "signal_run_id": "sig-1"},
    )
    _start(store)

    assert operator_bot.process_update(callback_update("operator:appr:a:ap-1"))

    assert store.list_system_events_by_type("telegram_approval_ack", limit=None) == []
    assert _statuses(store, "/approval") == ["migration_blocked"]


def test_a_completed_migration_leaves_the_funding_callback_working(operator_bot):
    """The gate must lift. A migration that permanently disables confirmation
    is a different outage from the one it prevents."""
    store = operator_bot.store
    publish_current_request(store, "req-1")
    _complete(store)

    assert operator_bot.process_update(callback_update("operator:funding:cancel:req-1"))

    assert _statuses(store, "/funding_cancel") == ["canceled"]


# --- direct orchestration entry points (no Telegram in sight) --------------
#
# A crashed migration leaves MIGRATING behind after its process exits, so
# safety cannot depend on the operator remembering not to run some other
# command. The orchestrator methods that can create workflow ownership,
# dispatch approvals or execute signals check the same fence themselves.


@pytest.fixture
def orchestrator(tmp_path):
    from contribution_fixtures import _multi_account_config

    from maestro.orchestration.orchestrator import MaestroOrchestrator

    config = _multi_account_config(tmp_path)
    return MaestroOrchestrator(config)


def test_run_signal_refuses_while_migrating(orchestrator):
    _start(orchestrator.state_store)
    before = event_count(orchestrator.state_store)

    with pytest.raises(ms.MigrationActive, match="MIGRATING"):
        orchestrator.run_signal()

    assert event_count(orchestrator.state_store) == before


def test_run_signal_refuses_on_invalid_markers(orchestrator):
    store = orchestrator.state_store
    store.save_system_event("r", ms.STARTED_EVENT, {"cutoff": "x", "duplicate_key": ms.STARTED_KEY})
    before = event_count(store)

    with pytest.raises(ms.MigrationActive, match="contradictory"):
        orchestrator.run_signal()

    assert event_count(store) == before


@pytest.mark.parametrize(
    ("entry", "inner", "args"),
    (
        ("run_signal", "_run_signal_locked", ()),
        ("run_once", "_run_once_locked", ()),
        ("approve_signal", "_approve_signal_locked", ("sig-1",)),
        ("dispatch_signal_approval", "_dispatch_signal_approval_locked", ("sig-1",)),
    ),
)
def test_every_authoritative_entry_point_is_fenced_while_migrating(
    orchestrator, monkeypatch, entry, inner, args
):
    """Directly, not through Telegram: `maestro run-signal`, run-once,
    daily-signal-approval, dashboard generate-signal and every other caller
    all pass through these four methods."""
    store = orchestrator.state_store
    reached: list[bool] = []
    monkeypatch.setattr(
        type(orchestrator), inner, lambda self, *a, **k: reached.append(True) or None
    )
    _start(store)
    before = event_count(store)

    with pytest.raises(ms.MigrationActive):
        getattr(orchestrator, entry)(*args)

    assert reached == []
    assert event_count(store) == before


@pytest.mark.parametrize(
    ("entry", "inner", "args"),
    (
        ("run_signal", "_run_signal_locked", ()),
        ("run_once", "_run_once_locked", ()),
        ("approve_signal", "_approve_signal_locked", ("sig-1",)),
        ("dispatch_signal_approval", "_dispatch_signal_approval_locked", ("sig-1",)),
    ),
)
def test_the_fence_lifts_once_no_migration_is_ongoing(
    orchestrator, monkeypatch, entry, inner, args
):
    """NOT_STARTED and COMPLETED are ordinary operating states."""
    from test_migration_runtime_gates import _stub_summary

    reached: list[bool] = []
    monkeypatch.setattr(
        type(orchestrator),
        inner,
        lambda self, *a, **k: reached.append(True) or _stub_summary(entry),
    )

    getattr(orchestrator, entry)(*args)
    _complete(orchestrator.state_store)

    getattr(orchestrator, entry)(*args)

    assert reached == [True, True]


def _stub_summary(entry):
    from maestro.approval.models import ApprovalDispatchResult
    from maestro.orchestration.orchestrator import RunOnceSummary

    if entry == "run_once":
        return RunOnceSummary(
            run_id="r", loaded_strategies=[], orders_created=0, total_value=0.0, cash=0.0
        )
    if entry == "dispatch_signal_approval":
        return ApprovalDispatchResult(
            signal_run_id="s", run_id="r", orders_planned=0, approval_status="pending"
        )
    return None

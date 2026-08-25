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

import threading

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
from test_signal_approval_handoff import (
    _capacity_lookup,
    _dispatch_one_pending_envelope,
    _dispatch_orchestrator_with_capacity,
    _dry_run_armed_dispatch_config,
    _mock_kis_snapshot_refresh,
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


# --- pending approval resolution (direct, no Telegram) ---------------------
#
# resolve_pending_signal_approval is a public, authoritative execution
# boundary: it persists the operator's decision and can submit broker orders.
# The Telegram callback two frames up checks migration state, but a public
# boundary that can execute money must own its own fence -- and it must hold
# the writer lock across the whole admission+execution interval, or a
# migration could begin classification halfway through an already-admitted
# resolution (and vice versa).


def _pending_resolution(tmp_path, monkeypatch):
    """A dispatched approval envelope, dry-run armed, ready to resolve."""
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _dry_run_armed_dispatch_config(tmp_path)
    store, envelope, decision, _order_ids = _dispatch_one_pending_envelope(config)
    orchestrator = _dispatch_orchestrator_with_capacity(
        config, FakeTelegramClient(), _capacity_lookup()
    )
    return store, orchestrator, envelope, decision


def _assert_no_financial_effect(store, envelope, execution):
    assert execution == []
    assert store.approval_exists(envelope.approval_id) is False
    assert (
        [
            row
            for row in store.list_system_events_by_type(
                "signal_approval_completed", limit=None
            )
            if row["payload"].get("approval_id") == envelope.approval_id
        ]
        == []
    )


def test_resolution_refuses_directly_while_migrating(tmp_path, monkeypatch):
    store, orchestrator, envelope, decision = _pending_resolution(tmp_path, monkeypatch)
    execution: list[str] = []
    original = orchestrator._execute_live_approval_orders

    def spy(*args, **kwargs):
        execution.append("executed")
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_execute_live_approval_orders", spy)
    _start(store)

    with pytest.raises(ms.MigrationActive, match="MIGRATING"):
        orchestrator.resolve_pending_signal_approval(envelope, decision)

    _assert_no_financial_effect(store, envelope, execution)


def test_resolution_refuses_directly_on_invalid_markers(tmp_path, monkeypatch):
    store, orchestrator, envelope, decision = _pending_resolution(tmp_path, monkeypatch)
    execution: list[str] = []
    original = orchestrator._execute_live_approval_orders

    def spy(*args, **kwargs):
        execution.append("executed")
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_execute_live_approval_orders", spy)
    store.save_system_event("r", ms.STARTED_EVENT, {"cutoff": "x", "duplicate_key": ms.STARTED_KEY})

    with pytest.raises(ms.MigrationActive, match="contradictory"):
        orchestrator.resolve_pending_signal_approval(envelope, decision)

    _assert_no_financial_effect(store, envelope, execution)


def test_resolution_still_works_before_any_migration(tmp_path, monkeypatch):
    """The fence must not disable ordinary operation."""
    store, orchestrator, envelope, decision = _pending_resolution(tmp_path, monkeypatch)

    summary = orchestrator.resolve_pending_signal_approval(envelope, decision)

    assert summary.orders_created > 0
    assert store.approval_exists(envelope.approval_id) is True
    completed = [
        row
        for row in store.list_system_events_by_type("signal_approval_completed", limit=None)
        if row["payload"].get("approval_id") == envelope.approval_id
    ]
    assert len(completed) == 1


def test_resolution_still_works_after_a_completed_migration(tmp_path, monkeypatch):
    """COMPLETED lifts the fence: post-classification resolutions are ordinary."""
    store, orchestrator, envelope, decision = _pending_resolution(tmp_path, monkeypatch)
    _complete(store)

    summary = orchestrator.resolve_pending_signal_approval(envelope, decision)

    assert summary.orders_created > 0
    assert store.approval_exists(envelope.approval_id) is True


def test_an_admitted_resolution_excludes_every_cooperating_writer_including_the_migration(
    tmp_path, monkeypatch
):
    """No interleaving where the resolution observes a safe state, the
    migration starts, and the resolution then executes financial work.

    The resolution holds live_order_lock -> writer_lock for its whole
    admission+execution interval. While that interval is open, the migration
    -- which needs the same writer lock to write its start marker -- cannot
    begin; the database it would classify cannot move under it. A probe
    writer is refused for the entire interval, proving the exclusion holds
    against any cooperating writer, not just the migration by name.
    """
    from maestro.state import upgrade_backfill as ub

    store, orchestrator, envelope, decision = _pending_resolution(tmp_path, monkeypatch)
    entered = threading.Event()
    may_finish = threading.Event()
    original = orchestrator._execute_live_approval_orders

    def gated_executor(*args, **kwargs):
        # Inside the protected interval: both locks held, no financial
        # effect has landed yet.
        entered.set()
        assert may_finish.wait(timeout=30)
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_execute_live_approval_orders", gated_executor)

    summaries: list[object] = []
    resolution_thread = threading.Thread(
        target=lambda: summaries.append(
            orchestrator.resolve_pending_signal_approval(envelope, decision)
        )
    )
    resolution_thread.start()
    assert entered.wait(timeout=30), "resolution never reached its protected interval"

    probe_errors: list[BaseException] = []

    def probe_writer():
        try:
            with store.writer_lock("probe", timeout_seconds=0.5):
                probe_errors.append(AssertionError("writer_lock acquired mid-resolution"))
        except TimeoutError:
            pass

    probe = threading.Thread(target=probe_writer)
    probe.start()
    probe.join(timeout=10)
    assert probe_errors == []

    migration_results: list[object] = []
    mig_started = threading.Event()

    def migration_runner():
        mig_started.set()
        migration_results.append(ub.run_upgrade_backfill(store, "run-mig"))

    migration_thread = threading.Thread(target=migration_runner)
    migration_thread.start()
    assert mig_started.wait(timeout=10)
    # A bounded window in which the migration has every chance to contend;
    # it must still be waiting outside the interval.
    contention_window = threading.Event()
    contention_window.wait(timeout=0.5)
    assert migration_thread.is_alive()
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.NOT_STARTED

    may_finish.set()
    resolution_thread.join(timeout=60)
    migration_thread.join(timeout=60)

    assert summaries[0].orders_created > 0
    assert ms.load_migration_state(store).phase is ms.MigrationPhase.COMPLETED


def test_a_migration_that_won_first_leaves_the_resolution_refusing(tmp_path, monkeypatch):
    """If the migration owns the interval first, the resolution must observe
    MIGRATING and refuse before any execution -- not execute on stale
    admission."""
    store, orchestrator, envelope, decision = _pending_resolution(tmp_path, monkeypatch)
    execution: list[str] = []
    original = orchestrator._execute_live_approval_orders

    def spy(*args, **kwargs):
        execution.append("executed")
        return original(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "_execute_live_approval_orders", spy)
    lock_held = threading.Event()
    release = threading.Event()

    def hold_migration_lock():
        with store.writer_lock("migration-holder", timeout_seconds=10):
            # The migration's first act under its lock is to take ownership.
            ms.start_migration(store, "run-mig")
            lock_held.set()
            assert release.wait(timeout=30)

    holder = threading.Thread(target=hold_migration_lock)
    holder.start()
    assert lock_held.wait(timeout=10)

    outcomes: list[BaseException | str] = []

    def run_resolution():
        try:
            orchestrator.resolve_pending_signal_approval(envelope, decision)
        except BaseException as exc:
            outcomes.append(exc)

    resolution_thread = threading.Thread(target=run_resolution)
    resolution_thread.start()
    # The resolution cannot even read state while the migration holds the
    # writer lock: it stays parked outside the fence.
    parked = threading.Event()
    parked.wait(timeout=0.5)
    assert resolution_thread.is_alive()
    assert outcomes == []

    release.set()
    resolution_thread.join(timeout=60)
    holder.join(timeout=10)

    # The lock came free only after a crash-style abort left MIGRATING behind,
    # which is exactly what the fence must see.
    assert isinstance(outcomes[0], ms.MigrationActive)
    _assert_no_financial_effect(store, envelope, execution)

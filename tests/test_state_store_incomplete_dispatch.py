from maestro.state.store import StateStore


def _store(tmp_path) -> StateStore:
    return StateStore(str(tmp_path / "state.db"))


def _consume(store: StateStore, signal_run_id: str) -> None:
    store.mark_signal_package_consumed(signal_run_id, f"run-for-{signal_run_id}")


def _settle(store: StateStore, signal_run_id: str, event_type: str) -> None:
    store.save_system_event(signal_run_id, event_type, {"signal_run_id": signal_run_id})


def test_a_package_that_was_never_consumed_is_not_incomplete(tmp_path):
    store = _store(tmp_path)
    store.save_signal_package("signal-1", {"orders_preview": []})

    assert store.list_incomplete_signal_dispatches() == []


def test_a_consumed_package_with_no_settled_event_is_incomplete(tmp_path):
    store = _store(tmp_path)
    _consume(store, "signal-1")

    assert store.list_incomplete_signal_dispatches() == ["signal-1"]


def test_a_dispatch_that_reported_pending_is_settled(tmp_path):
    store = _store(tmp_path)
    _consume(store, "signal-1")
    _settle(store, "signal-1", "signal_approval_pending")

    assert store.list_incomplete_signal_dispatches() == []
    assert store.signal_dispatch_settled("signal-1") is True


def test_a_run_that_needed_no_approval_is_settled(tmp_path):
    # signal_approval_completed covers the not_required and capacity_blocked
    # exits, which consume the package without creating any approval.
    store = _store(tmp_path)
    _consume(store, "signal-1")
    _settle(store, "signal-1", "signal_approval_completed")

    assert store.list_incomplete_signal_dispatches() == []


def test_only_the_unfinished_run_is_reported(tmp_path):
    store = _store(tmp_path)
    _consume(store, "signal-done")
    _settle(store, "signal-done", "signal_approval_pending")
    _consume(store, "signal-stuck")

    assert store.list_incomplete_signal_dispatches() == ["signal-stuck"]


def test_another_runs_settled_event_does_not_settle_this_one(tmp_path):
    store = _store(tmp_path)
    _consume(store, "signal-1")
    _settle(store, "signal-2", "signal_approval_pending")

    assert store.list_incomplete_signal_dispatches() == ["signal-1"]
    assert store.signal_dispatch_settled("signal-1") is False


def test_the_oldest_unfinished_runs_come_first_and_the_limit_holds(tmp_path):
    store = _store(tmp_path)
    for index in range(5):
        _consume(store, f"signal-{index}")

    assert store.list_incomplete_signal_dispatches(limit=3) == [
        "signal-0",
        "signal-1",
        "signal-2",
    ]


def test_a_run_consumed_twice_is_reported_once(tmp_path):
    store = _store(tmp_path)
    _consume(store, "signal-1")
    _consume(store, "signal-1")

    assert store.list_incomplete_signal_dispatches() == ["signal-1"]


def test_one_groups_completion_does_not_settle_a_multi_group_dispatch(tmp_path):
    # A crash between creating the first group's approval and the second
    # group's leaves only one group's card in existence. The operator can
    # still resolve that one card, which writes a per-group
    # signal_approval_completed carrying that group's approval_id -- but the
    # second group was never created, so the dispatch is not done.
    store = _store(tmp_path)
    _consume(store, "signal-1")
    store.save_system_event(
        "signal-1",
        "signal_approval_completed",
        {"signal_run_id": "signal-1", "approval_id": "approval-group-1"},
    )

    assert store.signal_dispatch_settled("signal-1") is False
    assert store.list_incomplete_signal_dispatches() == ["signal-1"]

def test_the_package_level_completion_settles_a_multi_group_dispatch(tmp_path):
    # Once every group has been created, dispatch_pending_approvals writes
    # the package-level signal_approval_pending event -- that alone marks
    # the dispatch settled, regardless of how many per-group completions
    # have also landed by then.
    store = _store(tmp_path)
    _consume(store, "signal-1")
    store.save_system_event(
        "signal-1",
        "signal_approval_completed",
        {"signal_run_id": "signal-1", "approval_id": "approval-group-1"},
    )
    _settle(store, "signal-1", "signal_approval_pending")

    assert store.signal_dispatch_settled("signal-1") is True
    assert store.list_incomplete_signal_dispatches() == []

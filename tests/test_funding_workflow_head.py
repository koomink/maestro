import json

import pytest

from maestro.state.funding_workflow import head_key, publish_contribution_request
from maestro.state.store import StateStore


def _store(tmp_path) -> StateStore:
    return StateStore(tmp_path / "state.db")


def _write_head(store, workflow_id, version, request_id, status="pending"):
    store.save_system_events_atomic(
        "run-1",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": head_key(workflow_id, version),
                    "workflow_id": workflow_id,
                    "version": version,
                    "request_id": request_id,
                    "status": status,
                    "scope": ["core", "acct-1", "krw", "KRW"],
                },
            }
        ],
    )


def test_no_head_yet_reads_as_none(tmp_path):
    store = _store(tmp_path)
    assert store.load_funding_workflow_head("funding:x:2026-08") is None


def test_the_highest_version_is_the_head(tmp_path):
    store = _store(tmp_path)
    _write_head(store, "wf-a", 1, "req-1")
    _write_head(store, "wf-a", 2, "req-2")
    head = store.load_funding_workflow_head("wf-a")
    assert head["version"] == 2
    assert head["request_id"] == "req-2"


def test_heads_of_other_workflows_are_not_visible(tmp_path):
    store = _store(tmp_path)
    _write_head(store, "wf-a", 1, "req-1")
    _write_head(store, "wf-b", 1, "req-2")
    assert store.load_funding_workflow_head("wf-a")["request_id"] == "req-1"
    assert store.load_funding_workflow_head("wf-b")["request_id"] == "req-2"


def test_listing_gives_one_row_per_workflow(tmp_path):
    store = _store(tmp_path)
    _write_head(store, "wf-a", 1, "req-1")
    _write_head(store, "wf-a", 2, "req-2")
    _write_head(store, "wf-b", 1, "req-3")
    heads = {row["workflow_id"]: row for row in store.list_funding_workflow_heads()}
    assert heads["wf-a"]["version"] == 2
    assert heads["wf-b"]["version"] == 1


def _request(request_id, month_key="2026-08"):
    return {
        "request_id": request_id,
        "contribution_group_id": "core",
        "account_id": "acct-1",
        "execution_sleeve": "krw",
        "currency": "KRW",
        "month_key": month_key,
        "status": "pending",
        "strategy_ids": ["s1"],
    }


def test_the_first_request_becomes_head_v1(tmp_path):
    store = _store(tmp_path)
    result = publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    assert result["committed"] is True
    assert result["version"] == 1
    head = store.load_funding_workflow_head(result["workflow_id"])
    assert head["request_id"] == "req-1"


def test_a_replacement_request_supersedes_the_previous_one_atomically(tmp_path):
    store = _store(tmp_path)
    first = publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    second = publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")
    assert second["committed"] is True
    assert second["version"] == 2
    head = store.load_funding_workflow_head(first["workflow_id"])
    assert head["request_id"] == "req-2"
    superseded = [
        row["payload"]
        for row in store.list_system_events_by_type("funding_workflow_superseded", limit=None)
    ]
    assert [row["request_id"] for row in superseded] == ["req-1"]


def test_the_request_event_and_the_head_land_together_or_not_at_all(tmp_path):
    store = _store(tmp_path)
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    requests = store.list_system_events_by_type("contribution_funding_request", limit=None)
    heads = store.list_system_events_by_type("funding_workflow_head", limit=None)
    assert len(requests) == 1
    assert len(heads) == 1


def test_a_publisher_working_from_a_stale_head_cannot_take_a_taken_version(tmp_path, monkeypatch):
    store = _store(tmp_path)
    first = publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    workflow_id = first["workflow_id"]
    # A rival advances the head to v2 in the window between our read and our write.
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")
    stale_view = {
        "workflow_id": workflow_id,
        "version": 1,
        "request_id": "req-1",
        "phase": "funding",
        "status": "pending",
    }
    monkeypatch.setattr(store, "load_funding_workflow_head", lambda _workflow_id: stale_view)

    result = publish_contribution_request(store, "run-3", _request("req-3"), phase="funding")
    monkeypatch.undo()  # stop shadowing load_funding_workflow_head before reading the real head

    assert result["committed"] is False
    assert result["conflict"] == "precondition_present"
    # The loser must leave nothing behind: no request event, no supersede marker.
    requests = store.list_system_events_by_type("contribution_funding_request", limit=None)
    assert [row["payload"]["request_id"] for row in requests] == ["req-2", "req-1"]
    assert store.load_funding_workflow_head(workflow_id)["request_id"] == "req-2"


def test_republishing_the_same_request_is_a_replay_not_a_new_version(tmp_path):
    store = _store(tmp_path)
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    again = publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    assert again["committed"] is False
    assert again["conflict"] == "already_committed"


def test_different_scopes_in_the_same_month_do_not_supersede_each_other(tmp_path):
    store = _store(tmp_path)
    left = dict(_request("req-1"), execution_sleeve="krw")
    right = dict(_request("req-2"), execution_sleeve="usd")
    a = publish_contribution_request(store, "run-1", left, phase="funding")
    b = publish_contribution_request(store, "run-1", right, phase="funding")
    assert a["workflow_id"] != b["workflow_id"]
    assert store.load_funding_workflow_head(a["workflow_id"])["request_id"] == "req-1"
    assert store.load_funding_workflow_head(b["workflow_id"])["request_id"] == "req-2"


def test_a_budget_request_uses_the_budget_event_type(tmp_path):
    store = _store(tmp_path)
    publish_contribution_request(store, "run-1", _request("req-1"), phase="budget")
    assert len(store.list_system_events_by_type("contribution_budget_request", limit=None)) == 1


def test_an_unknown_phase_is_refused(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="phase"):
        publish_contribution_request(store, "run-1", _request("req-1"), phase="rebate")


def test_a_signal_run_that_asks_for_funding_also_publishes_a_head(funding_orchestrator):
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)

    orchestrator.run_signal(strategy_ids=["tranquillo"])

    requests = store.list_system_events_by_type("contribution_funding_request", limit=None)
    assert len(requests) == 1
    payload = requests[0]["payload"]
    head = store.load_funding_workflow_head(payload["funding_workflow_id"])
    assert head["request_id"] == payload["request_id"]
    assert head["version"] == 1


def test_the_audit_entry_names_the_workflow_the_request_belongs_to(funding_orchestrator):
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)

    orchestrator.run_signal(strategy_ids=["tranquillo"])

    payload = store.list_system_events_by_type("contribution_funding_request", limit=None)[0][
        "payload"
    ]
    audited = [
        entry
        for entry in _audit_entries(orchestrator)
        if entry["event_type"] == "contribution_funding_request"
    ]
    assert [entry["details"]["funding_workflow_id"] for entry in audited] == [
        payload["funding_workflow_id"]
    ]


def test_a_request_that_loses_the_head_cas_is_absent_from_the_package(
    funding_orchestrator, monkeypatch
):
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)
    monkeypatch.setattr(
        orchestrator,
        "_publish_contribution_request",
        lambda *args, **kwargs: False,
    )

    summary = orchestrator.run_signal(strategy_ids=["tranquillo"])

    package = store.load_signal_package(summary.signal_run_id)
    # Nothing landed in the event log, so the package must not advertise one.
    assert package["funding_requests"] == []
    assert package["funding_requests_count"] == 0
    assert package["status"] != "funding_required"


def _audit_entries(orchestrator):
    """The tamper-evident audit trail as parsed records."""
    return [
        json.loads(line)
        for line in orchestrator.audit.path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

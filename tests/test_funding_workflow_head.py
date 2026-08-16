from maestro.state.funding_workflow import head_key
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

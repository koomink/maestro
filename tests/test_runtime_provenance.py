from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

from maestro.config.loader import load_config_with_identity
from maestro.core.provenance import DeploymentIdentity, deployment_identity
from maestro.orchestration.orchestrator import MaestroOrchestrator


def test_deployment_identity_changes_when_worktree_becomes_dirty(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / "source.py"
    source.write_text("VALUE = 1\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "source.py"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Maestro Test",
            "-c",
            "user.email=maestro@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )

    clean = deployment_identity(source)
    source.write_text("VALUE = 2\n")
    dirty = deployment_identity(source)

    assert clean.commit == dirty.commit
    assert clean.dirty is False
    assert dirty.dirty is True
    assert clean.source_fingerprint != dirty.source_fingerprint


def test_orchestration_runs_record_deployment_and_config_fingerprints(tmp_path, monkeypatch):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config, identity = load_config_with_identity(config_path)
    deployment = DeploymentIdentity(
        commit="a" * 40,
        source_fingerprint="b" * 64,
        dirty=False,
    )
    monkeypatch.setattr(
        "maestro.orchestration.orchestrator.current_deployment_identity",
        lambda: deployment,
    )
    orchestrator = MaestroOrchestrator(config, config_identity=identity)

    run_once = orchestrator.run_once()
    signal = orchestrator.run_signal()
    approval = orchestrator.approve_signal(signal.signal_run_id)

    events = orchestrator.state_store.list_system_events_by_type("run_provenance", limit=10)
    by_run_id = {event["run_id"]: event["payload"] for event in events}
    assert {run_once.run_id, signal.signal_run_id, approval.run_id} <= by_run_id.keys()
    assert by_run_id[run_once.run_id]["run_kind"] == "run_once"
    assert by_run_id[signal.signal_run_id]["run_kind"] == "signal"
    assert by_run_id[approval.run_id]["run_kind"] == "approval"
    assert by_run_id[approval.run_id]["signal_run_id"] == signal.signal_run_id
    for payload in by_run_id.values():
        assert payload["deployment_commit"] == "a" * 40
        assert payload["deployment_source_fingerprint"] == "b" * 64
        assert payload["deployment_dirty"] is False
        assert payload["config_fingerprint"] == identity.fingerprint
        assert payload["config_runtime_fingerprint"] == identity.runtime_fingerprint

    audit_events = [
        json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()
    ]
    provenance_events = [event for event in audit_events if event["event_type"] == "run_provenance"]
    assert len(provenance_events) == 3

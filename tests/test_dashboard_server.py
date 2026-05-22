from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from maestro.config.loader import load_config
from maestro.dashboard.server import create_app
from maestro.orchestration.orchestrator import MaestroOrchestrator


def _dashboard_config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    MaestroOrchestrator(config).run_once()
    return config_path


def test_dashboard_health_reports_config_and_readiness(tmp_path):
    config_path = _dashboard_config(tmp_path)
    client = TestClient(create_app(config_path))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["read_only"] is True
    assert response.json()["config_path"] == str(config_path)


def test_dashboard_snapshot_includes_feature_parity_read_models(tmp_path):
    config_path = _dashboard_config(tmp_path)
    client = TestClient(create_app(config_path))

    response = client.get("/api/dashboard/snapshot?display_currency=USD")

    assert response.status_code == 200
    payload = response.json()
    assert payload["display_currency"] == "USD"
    assert payload["operator_home"]["status"] in {"ok", "warning", "danger"}
    assert payload["system_verdict"]["capital_summary"]
    assert payload["system_verdict"]["asset_summary_rows"]
    assert payload["symphony_map"]["nodes"]
    assert payload["operator_cockpit"]["freshness"]
    assert isinstance(payload["investment_console"]["account_performance"], list)
    assert isinstance(payload["investment_console"]["total_portfolio_performance"], list)
    assert isinstance(payload["investment_console"]["total_portfolio_performance_krw"], list)
    assert isinstance(payload["investment_console"]["total_portfolio_performance_usd"], list)
    assert payload["virtuoso_apps"]["strategies"]
    assert payload["audit_trail"]["run_index"]
    assert payload["raw"]["status"]


def test_dashboard_run_detail_endpoint_returns_audit_drilldown(tmp_path):
    config_path = _dashboard_config(tmp_path)
    client = TestClient(create_app(config_path))
    snapshot = client.get("/api/dashboard/snapshot").json()
    run_id = snapshot["audit_trail"]["run_index"][0]["run_id"]

    response = client.get(f"/api/dashboard/runs/{run_id}")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == run_id
    assert isinstance(payload["timeline"], list)


def test_dashboard_run_detail_endpoint_returns_404_for_missing_run(tmp_path):
    config_path = _dashboard_config(tmp_path)
    client = TestClient(create_app(config_path))

    response = client.get("/api/dashboard/runs/missing-run")

    assert response.status_code == 404


def test_dashboard_serves_react_shell(tmp_path):
    config_path = _dashboard_config(tmp_path)
    client = TestClient(create_app(config_path))

    response = client.get("/")
    head_response = client.head("/")

    assert response.status_code == 200
    assert head_response.status_code == 200
    assert "Symphony" in response.text
    assert "root" in response.text

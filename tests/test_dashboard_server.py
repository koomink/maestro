from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.dashboard.server import create_app
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.state.store import StateStore


def _dashboard_config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    MaestroOrchestrator(config).run_once()
    return config_path


def _multi_strategy_multi_currency_config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["portfolio"]["base_currency"] = "KRW"
    raw["portfolio"]["cash_by_currency"] = {"KRW": 1_000_000.0, "USD": 1000.0}
    raw["portfolio"]["allowed_symbols"] = ["CASH_KRW", "CASH_USD", "AAPL", "005930"]
    raw["strategies"] = [
        {
            "id": "ataraxia_us",
            "enabled": True,
            "weight": 0.6,
            "entrypoint": "sample_static_allocation.strategy:SampleStaticAllocationStrategy",
            "config": {"allocations": {"AAPL": 0.7, "CASH_USD": 0.3}},
        },
        {
            "id": "snowball_kr",
            "enabled": True,
            "weight": 0.4,
            "entrypoint": "sample_static_allocation.strategy:SampleStaticAllocationStrategy",
            "config": {"allocations": {"005930": 0.5, "CASH_KRW": 0.5}},
        },
    ]
    raw["state"]["sqlite_path"] = str(tmp_path / "multi_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "multi_audit.jsonl")
    config_path = tmp_path / "multi_paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)
    store = StateStore(
        config.state.sqlite_path,
        config.portfolio.initial_cash,
        config.portfolio.cash_by_currency,
    )
    for run_id, usd_value, krw_value in [
        ("run_multi_1", 1000.0, 1_000_000.0),
        ("run_multi_2", 1100.0, 1_050_000.0),
    ]:
        store.save_broker_account_snapshot(
            run_id,
            "acct_usd",
            {
                "account": {
                    "account_id": "acct_usd",
                    "currency": "USD",
                    "cash": 250.0,
                    "total_value": usd_value,
                    "positions": [
                        {
                            "symbol": "AAPL",
                            "quantity": 2.0,
                            "current_price": usd_value / 2.0,
                        }
                    ],
                    "source": "fixture",
                }
            },
        )
        store.save_broker_account_snapshot(
            run_id,
            "acct_krw",
            {
                "account": {
                    "account_id": "acct_krw",
                    "currency": "KRW",
                    "cash": 500_000.0,
                    "total_value": krw_value,
                    "positions": [
                        {
                            "symbol": "005930",
                            "quantity": 10.0,
                            "current_price": krw_value / 10.0,
                        }
                    ],
                    "source": "fixture",
                }
            },
        )
    store.save_system_event(
        "run_fx",
        "fx_rate_snapshot",
        {
            "source": "fixture",
            "as_of": utc_now().isoformat(),
            "max_age_seconds": 3600,
            "rates": {"USD/KRW": 1000.0},
        },
    )
    for strategy_id, symbol, book_id, first_value, second_value in [
        ("ataraxia_us", "AAPL", "ataraxia_us:USD", 1000.0, 1100.0),
        ("snowball_kr", "005930", "snowball_kr:KRW", 1_000_000.0, 1_050_000.0),
    ]:
        store.save_strategy_run(
            "run_multi_2",
            strategy_id,
            {
                "source_signal": {"symbol": symbol, "action": "buy"},
                "result": {"confidence": 0.8, "allocations": {symbol: 0.7}},
                "validation": {"ok": True, "errors": []},
            },
        )
        store.save_strategy_book_snapshots(
            "run_multi_1",
            [
                {
                    "strategy_id": strategy_id,
                    "book_id": book_id,
                    "book_value": first_value,
                    "allocations": {symbol: 0.7},
                }
            ],
        )
        store.save_strategy_book_snapshots(
            "run_multi_2",
            [
                {
                    "strategy_id": strategy_id,
                    "book_id": book_id,
                    "book_value": second_value,
                    "allocations": {symbol: 0.7},
                }
            ],
        )
    store.save_system_event(
        "run_multi_2",
        "broker_reconciliation",
        {
            "passed": True,
            "checked_at": utc_now().isoformat(),
            "issues": [],
            "broker_account_id": "acct_usd",
        },
    )
    return config_path


def _mismatched_dashboard_config(config_path: Path) -> Path:
    raw = yaml.safe_load(config_path.read_text())
    raw["portfolio"]["initial_cash"] = int(raw["portfolio"]["initial_cash"]) + 1
    mismatch_path = config_path.with_name("paper_mismatch.yaml")
    mismatch_path.write_text(yaml.safe_dump(raw))
    return mismatch_path


def test_dashboard_health_reports_config_and_readiness(tmp_path):
    config_path = _dashboard_config(tmp_path)
    client = TestClient(create_app(config_path))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["read_only"] is True
    assert response.json()["config_path"] == str(config_path)


def test_dashboard_reports_config_state_mismatch_as_readable_409(tmp_path):
    config_path = _dashboard_config(tmp_path)
    original_client = TestClient(create_app(config_path))
    assert original_client.get("/api/health").status_code == 200
    mismatch_path = _mismatched_dashboard_config(config_path)
    client = TestClient(create_app(mismatch_path), raise_server_exceptions=False)

    health_response = client.get("/api/health")
    snapshot_response = client.get("/api/dashboard/snapshot")

    assert health_response.status_code == 409
    assert snapshot_response.status_code == 409
    payload = health_response.json()["detail"]
    assert payload["status"] == "config_state_mismatch"
    assert payload["read_only"] is True
    assert "does not match" in payload["message"]
    assert payload["config_path"] == str(mismatch_path.resolve())
    assert payload["state_path"] == str((tmp_path / "state.db").resolve())


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
    assert payload["operator_cockpit"]["freshness"][0]["policy"]["failed_precedence"] is True
    assert isinstance(payload["investment_console"]["account_performance"], list)
    assert isinstance(payload["investment_console"]["total_portfolio_performance"], list)
    assert isinstance(payload["investment_console"]["total_portfolio_performance_krw"], list)
    assert isinstance(payload["investment_console"]["total_portfolio_performance_usd"], list)
    performance_snapshot = payload["investment_console"]["performance_snapshot"]
    assert performance_snapshot["schema_version"] == 1
    assert performance_snapshot["display_currency"] == "USD"
    assert set(performance_snapshot["latest"]) >= {
        "created_at",
        "run_id",
        "display_currency",
        "total_value",
        "period_return",
        "cumulative_return",
        "drawdown",
        "fx_status",
        "reconciliation_status",
    }
    assert performance_snapshot["series"]["total_portfolio"] == payload["investment_console"][
        "total_portfolio_performance"
    ]
    assert performance_snapshot["series"]["account"] == payload["investment_console"][
        "account_performance"
    ]
    if performance_snapshot["series"]["strategy_attribution"]:
        attribution_row = performance_snapshot["series"]["strategy_attribution"][0]
        assert "lineage" in attribution_row
        assert "source_tables" in attribution_row["lineage"]
    assert performance_snapshot["quality"]["status"] in {"ok", "warning", "missing"}
    assert "broker_account_snapshots" in performance_snapshot["lineage"]["source_tables"]
    assert payload["virtuoso_apps"]["strategies"]
    assert payload["audit_trail"]["run_index"]
    if payload["audit_trail"]["system_events"]:
        event_row = payload["audit_trail"]["system_events"][0]
        assert "schema_status" in event_row
        assert "missing_required_fields" in event_row
    assert payload["raw"]["status"]


def test_dashboard_snapshot_supports_multi_strategy_multi_currency_fixture(tmp_path):
    config_path = _multi_strategy_multi_currency_config(tmp_path)
    client = TestClient(create_app(config_path))

    response = client.get("/api/dashboard/snapshot?display_currency=KRW")

    assert response.status_code == 200
    payload = response.json()
    investment = payload["investment_console"]
    performance = investment["performance_snapshot"]
    assert payload["display_currency"] == "KRW"
    assert performance["latest"]["total_value"] == 2_150_000.0
    assert performance["latest"]["fx_status"] == "fresh"
    assert performance["series"]["total_portfolio"][0]["component_values"] == {
        "KRW": 1_050_000.0,
        "USD": 1100.0,
    }
    sleeve_currencies = {row["currency"] for row in investment["currency_sleeve_performance"]}
    assert {"KRW", "USD"} <= sleeve_currencies
    strategy_ids = {row["strategy_id"] for row in investment["strategy_attribution"]}
    assert {"ataraxia_us", "snowball_kr"} <= strategy_ids
    virtuoso_ids = {strategy["strategy_id"] for strategy in payload["virtuoso_apps"]["strategies"]}
    assert {"ataraxia_us", "snowball_kr"} <= virtuoso_ids


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

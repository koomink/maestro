from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.dashboard.actions import build_signal_freshness
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
            "id": "tranquillo_us",
            "enabled": True,
            "weight": 0.6,
            "entrypoint": "sample_static_allocation.strategy:SampleStaticAllocationStrategy",
            "config": {"allocations": {"AAPL": 0.7, "CASH_USD": 0.3}},
        },
        {
            "id": "crescendo_kr",
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
        ("tranquillo_us", "AAPL", "tranquillo_us:USD", 1000.0, 1100.0),
        ("crescendo_kr", "005930", "crescendo_kr:KRW", 1_000_000.0, 1_050_000.0),
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
    store.save_strategy_run(
        "run_legacy",
        "legacy_removed_app",
        {
            "source_signal": {"symbol": "LEGACY", "action": "hold"},
            "result": {"confidence": 0.1, "allocations": {}},
            "validation": {"ok": True, "errors": []},
        },
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE strategy_book_snapshots SET created_at = ? WHERE run_id = ?",
            ("2024-01-01T00:00:00+00:00", "run_multi_1"),
        )
        conn.execute(
            "UPDATE strategy_book_snapshots SET created_at = ? WHERE run_id = ?",
            ("2025-01-01T00:00:00+00:00", "run_multi_2"),
        )
    store.save_system_event(
        "cash_flow_tranquillo",
        "strategy_cash_flow",
        {
            "strategy_id": "tranquillo_us",
            "account_id": "acct_usd",
            "execution_sleeve": "usd_core",
            "amount": 100.0,
            "currency": "USD",
            "flow_type": "deposit",
            "effective_at": "2024-07-01T00:00:00+00:00",
            "source": "telegram_voluntary_deposit_allocation",
        },
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


def _live_readonly_kis_config(tmp_path: Path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["mode"] = "live_readonly"
    raw["portfolio"].pop("initial_cash", None)
    raw["accounts"] = [
        {
            "id": "kis_mock",
            "broker": "kis",
            "broker_products": ["kis_overseas_stock"],
            "environment": "paper_trading",
            "enabled": True,
            "provider": "kis",
            "account_id_env": "KIS_MOCK_ACCOUNT_ID",
            "app_key_env": "KIS_MOCK_APP_KEY",
            "app_secret_env": "KIS_MOCK_APP_SECRET",
        }
    ]
    raw["strategies"][0]["account_id"] = "kis_mock"
    raw["state"]["sqlite_path"] = str(tmp_path / "live_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "live_audit.jsonl")
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def test_dashboard_health_reports_config_and_readiness(tmp_path):
    config_path = _dashboard_config(tmp_path)
    client = TestClient(create_app(config_path))

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["read_only"] is True
    assert response.json()["config_path"] == str(config_path)


def test_dashboard_health_reports_missing_credential_env_without_secret_values(
    monkeypatch, tmp_path
):
    for key in ("KIS_MOCK_ACCOUNT_ID", "KIS_MOCK_APP_KEY", "KIS_MOCK_APP_SECRET"):
        monkeypatch.delenv(key, raising=False)
    config_path = _live_readonly_kis_config(tmp_path)
    client = TestClient(create_app(config_path))

    response = client.get("/api/health")

    assert response.status_code == 200
    credential_env = response.json()["credential_env"]
    assert credential_env["status"] == "missing"
    assert credential_env["missing"] == [
        "KIS_MOCK_ACCOUNT_ID",
        "KIS_MOCK_APP_KEY",
        "KIS_MOCK_APP_SECRET",
    ]
    assert "file-app-key" not in str(credential_env)


def test_dashboard_refresh_missing_credentials_mentions_env_file(monkeypatch, tmp_path):
    for key in ("KIS_MOCK_ACCOUNT_ID", "KIS_MOCK_APP_KEY", "KIS_MOCK_APP_SECRET"):
        monkeypatch.delenv(key, raising=False)
    config_path = _live_readonly_kis_config(tmp_path)
    client = TestClient(create_app(config_path), raise_server_exceptions=False)

    response = client.post("/api/dashboard/refresh")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["status"] == "dashboard_refresh_failed"
    assert "--env-file /etc/maestro/maestro.env" in detail["message"]


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


def test_signal_freshness_uses_newest_signal_package_per_strategy():
    class FakeStore:
        def list_system_events_by_type(self, event_type, limit=10):
            assert event_type == "signal_package"
            return [
                {
                    "run_id": "older_signal",
                    "created_at": "2000-01-01T00:00:00+00:00",
                    "payload": {
                        "status": "failed",
                        "signal_run_id": "older_signal",
                        "loaded_strategies": ["sample_static_allocation"],
                    },
                },
                {
                    "run_id": "newer_signal",
                    "created_at": utc_now().isoformat(),
                    "payload": {
                        "status": "no_action",
                        "signal_run_id": "newer_signal",
                        "loaded_strategies": ["sample_static_allocation"],
                    },
                },
            ][:limit]

    freshness = build_signal_freshness(FakeStore(), max_age_seconds=300)

    assert freshness["overall"] == "fresh"
    assert len(freshness["strategies"]) == 1
    strategy = freshness["strategies"][0]
    assert strategy["strategy_id"] == "sample_static_allocation"
    assert strategy["status"] == "fresh"
    assert strategy["latest_signal_run_id"] == "newer_signal"
    assert strategy["max_age_seconds"] == 300
    assert 0 <= strategy["age_seconds"] <= 300


def test_dashboard_refresh_syncs_accounts_without_running_strategies(monkeypatch, tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    calls = []

    class FakeSnapshot:
        class Account:
            account_id = "acct_fake"
            cash = 12.0
            buying_power = 12.0
            positions = []
            total_value = 12.0

        account = Account()

    def fake_fetch(self, symbols, run_id=None):
        calls.append({"symbols": list(symbols), "run_id": run_id})
        return FakeSnapshot()

    monkeypatch.setattr(
        "maestro.execution.brokers.kis.service.KISReadOnlyService.fetch_and_store_snapshot",
        fake_fetch,
    )
    raw["mode"] = "live_readonly"
    raw["state"]["sqlite_path"] = str(tmp_path / "readonly_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "readonly_audit.jsonl")
    raw["portfolio"].pop("initial_cash", None)
    raw["execution"]["order_posture"] = "disabled"
    raw["accounts"] = [
        {
            "id": "kis_paper",
            "broker": "kis",
            "broker_products": ["kis_overseas_stock"],
            "environment": "paper_trading",
            "enabled": True,
            "provider": "mock",
            "account_id": "MOCK",
        },
        {
            "id": "kis_ps",
            "broker": "kis",
            "broker_products": ["kis_overseas_stock"],
            "environment": "paper_trading",
            "enabled": True,
            "provider": "mock",
            "account_id": "MOCK-PS",
        }
    ]
    raw["strategies"] = []
    readonly_path = tmp_path / "readonly.yaml"
    readonly_path.write_text(yaml.safe_dump(raw))
    client = TestClient(create_app(readonly_path))

    response = client.post("/api/dashboard/refresh")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["accounts_synced"] == 2
    assert len(calls) == 2
    assert calls[0]["run_id"] is not None
    assert {call["run_id"] for call in calls} == {calls[0]["run_id"]}
    assert payload["signal_freshness"]["overall"] in {"missing", "fresh", "stale", "failed"}
    assert [call["symbols"] for call in calls] == [
        ["CASH", "MOCK_ETF_A", "MOCK_ETF_B"],
        ["CASH", "MOCK_ETF_A", "MOCK_ETF_B"],
    ]


def test_dashboard_refresh_records_fx_failure_without_failing(monkeypatch, tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "readonly_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "readonly_audit.jsonl")
    raw["strategies"] = []
    readonly_path = tmp_path / "readonly.yaml"
    readonly_path.write_text(yaml.safe_dump(raw))

    def fail_fx_refresh(self, run_id=None):
        del run_id
        raise ValueError("EXCHANGERATE_API_KEY is not set")

    monkeypatch.setattr(
        "maestro.fx.service.ConfiguredFXRefreshService.refresh_from_config",
        fail_fx_refresh,
    )
    client = TestClient(create_app(readonly_path))

    response = client.post("/api/dashboard/refresh")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["fx_refresh"]["status"] == "failed"
    assert payload["fx_refresh"]["error_type"] == "ValueError"


def test_dashboard_refresh_reports_action_errors_as_readable_409(monkeypatch, tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())

    def fail_fetch(self, symbols, run_id=None):
        raise ValueError("Missing KIS credential environment variables: ['KIS_MOCK_APP_KEY']")

    monkeypatch.setattr(
        "maestro.execution.brokers.kis.service.KISReadOnlyService.fetch_and_store_snapshot",
        fail_fetch,
    )
    raw["mode"] = "live_readonly"
    raw["state"]["sqlite_path"] = str(tmp_path / "readonly_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "readonly_audit.jsonl")
    raw["portfolio"].pop("initial_cash", None)
    raw["execution"]["order_posture"] = "disabled"
    raw["accounts"] = [
        {
            "id": "kis_paper",
            "broker": "kis",
            "broker_products": ["kis_overseas_stock"],
            "environment": "paper_trading",
            "enabled": True,
            "provider": "mock",
            "account_id": "MOCK",
        }
    ]
    raw["strategies"] = []
    readonly_path = tmp_path / "readonly.yaml"
    readonly_path.write_text(yaml.safe_dump(raw))
    client = TestClient(create_app(readonly_path), raise_server_exceptions=False)

    response = client.post("/api/dashboard/refresh")

    assert response.status_code == 409
    payload = response.json()["detail"]
    assert payload["status"] == "dashboard_refresh_failed"
    assert payload["read_only"] is True
    assert "Failed to refresh account kis_paper" in payload["message"]
    assert "Missing KIS credential" in payload["message"]


def test_generate_signal_requires_signal_config(tmp_path):
    config_path = _dashboard_config(tmp_path)
    client = TestClient(create_app(config_path))

    response = client.post("/api/dashboard/virtuoso/sample_static_allocation/generate-signal")

    assert response.status_code == 409
    assert response.json()["detail"]["status"] == "missing_signal_config"


def test_generate_signal_persists_signal_without_approvals_or_orders(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "signal_state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "signal_audit.jsonl")
    config_path = tmp_path / "signal.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    client = TestClient(create_app(config_path, signal_config_path=config_path))

    response = client.post("/api/dashboard/virtuoso/sample_static_allocation/generate-signal")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["strategy_id"] == "sample_static_allocation"
    assert payload["loaded_strategies"] == ["sample_static_allocation"]
    store = StateStore(
        config.state.sqlite_path,
        config.portfolio.initial_cash,
        config.portfolio.cash_by_currency,
    )
    assert store.load_signal_package(payload["signal_run_id"])
    assert [row["strategy_id"] for row in store.list_strategy_runs(limit=10)] == [
        "sample_static_allocation"
    ]
    counts = store.status()["counts"]
    assert counts["approvals"] == 0
    assert counts["orders"] == 0


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
    assert (
        performance_snapshot["series"]["total_portfolio"]
        == payload["investment_console"]["total_portfolio_performance"]
    )
    assert (
        performance_snapshot["series"]["account"]
        == payload["investment_console"]["account_performance"]
    )
    if performance_snapshot["series"]["strategy_attribution"]:
        attribution_row = performance_snapshot["series"]["strategy_attribution"][0]
        assert "lineage" in attribution_row
        assert "source_tables" in attribution_row["lineage"]
    assert performance_snapshot["quality"]["status"] in {"ok", "warning", "missing"}
    assert "broker_account_snapshots" in performance_snapshot["lineage"]["source_tables"]
    assert payload["virtuoso_apps"]["strategies"]
    assert payload["virtuoso_apps"]["signal_freshness"]["overall"] in {
        "missing",
        "fresh",
        "stale",
        "failed",
    }
    assert isinstance(payload["virtuoso_apps"]["signal_freshness"]["strategies"], list)
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
    assert {"tranquillo_us", "crescendo_kr"} <= strategy_ids
    virtuoso_strategies = payload["virtuoso_apps"]["strategies"]
    virtuoso_ids = {strategy["strategy_id"] for strategy in virtuoso_strategies}
    assert {"tranquillo_us", "crescendo_kr"} <= virtuoso_ids
    assert "legacy_removed_app" not in virtuoso_ids
    virtuoso_metrics = {
        metric["label"]: metric["value"] for metric in payload["virtuoso_apps"]["metrics"]
    }
    assert virtuoso_metrics["Configured Apps"] == 2
    assert virtuoso_metrics["Evidence Strategy IDs"] == 3
    overview_by_id = {row["strategy_id"]: row for row in payload["virtuoso_apps"]["overview"]}
    assert overview_by_id["tranquillo_us"]["app"] == "Tranquillo"
    assert overview_by_id["crescendo_kr"]["app"] == "Crescendo"
    virtuoso_by_id = {strategy["strategy_id"]: strategy for strategy in virtuoso_strategies}
    tranquillo_performance = virtuoso_by_id["tranquillo_us"]["performance_snapshot"]
    assert tranquillo_performance["latest"]["current_value"] == 1100.0
    assert tranquillo_performance["latest"]["cumulative_cash_flow"] == 100.0
    assert tranquillo_performance["latest"]["net_pnl"] == 0.0
    assert tranquillo_performance["latest"]["twr"] == 0.0
    assert tranquillo_performance["series"]["cash_flow_markers"][0]["amount"] == 100.0
    assert tranquillo_performance["quality"]["status"] == "ok"
    pipelines = payload["workflow_pipelines"]
    assert [node["id"] for node in pipelines["system"]["nodes"]] == [
        "data",
        "virtuoso",
        "signal",
        "maestro",
        "risk",
        "output",
        "state",
    ]
    assert [app["strategy_id"] for app in pipelines["apps"]] == [
        "tranquillo_us",
        "crescendo_kr",
    ]
    tranquillo_pipeline = pipelines["apps"][0]
    assert tranquillo_pipeline["display_name"] == "Tranquillo"
    assert [node["id"] for node in tranquillo_pipeline["nodes"]] == [
        "account",
        "data",
        "app",
        "signal",
        "risk",
        "output",
        "evidence",
    ]
    account_node = {node["id"]: node for node in tranquillo_pipeline["nodes"]}["account"]
    assert account_node["status"] == "missing"
    signal_node = {node["id"]: node for node in tranquillo_pipeline["nodes"]}["signal"]
    assert signal_node["run_id"] == "run_multi_2"
    assert signal_node["tone"] == "success"


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

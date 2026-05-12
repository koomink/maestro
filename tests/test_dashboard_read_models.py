from pathlib import Path

import yaml

from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.dashboard.read_models import (
    build_approvals_table,
    build_broker_snapshots_table,
    build_daily_live_order_usage,
    build_fill_reconciliation_table,
    build_health_summary,
    build_latest_broker_snapshot_card,
    build_latest_reconciliation_card,
    build_live_order_events_table,
    build_orders_table,
    build_overview,
    build_portfolio_table,
    build_recent_halt_failure_events_table,
    build_risk_decisions_table,
    build_safety_state_card,
    build_strategy_runs_table,
    build_system_events_table,
)
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.state.store import StateStore


def test_build_overview_works_with_empty_db(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)

    overview = build_overview(store)

    assert overview["cash"] == 1000
    assert overview["positions_count"] == 0
    assert overview["orders_count"] == 0
    assert overview["risk_decisions_count"] == 0
    assert overview["latest_run_id"] is None


def test_dashboard_read_models_work_after_run(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)

    MaestroOrchestrator(config).run_once()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    assert build_overview(store)["orders_count"] == 2
    assert build_portfolio_table(store)
    assert len(build_strategy_runs_table(store)) == 1
    assert len(build_orders_table(store)) == 2
    assert build_approvals_table(store) == []
    assert len(build_risk_decisions_table(store)) == 1
    assert build_broker_snapshots_table(store) == []


def test_dashboard_read_models_tolerate_sparse_payloads(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_strategy_run("run_1", "strategy_1", {})
    store.save_order("run_1", "ord_1", {"symbol": "MOCK_ETF_A"})
    store.save_approval("run_1", "appr_1", {"decision": {"status": "approved"}})
    store.save_risk_decision("run_1", True, {"approved": True})
    store.save_system_event("run_1", "event", {})
    store.save_broker_account_snapshot("run_1", "acct", {"account": {"account_id": "acct"}})

    strategy_row = build_strategy_runs_table(store)[0]
    assert strategy_row["run_id"] == "run_1"
    assert strategy_row["signal_action"] is None
    assert build_orders_table(store)[0]["symbol"] == "MOCK_ETF_A"
    assert build_approvals_table(store)[0]["status"] == "approved"
    assert build_risk_decisions_table(store)[0]["approved"] is True
    assert build_system_events_table(store)[0]["event_type"] == "event"
    assert build_broker_snapshots_table(store)[0]["account_id"] == "acct"


def test_strategy_runs_table_includes_top_level_source_signal(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_strategy_run(
        "run_signal",
        "signal_strategy",
        {
            "source_signal": {
                "symbol": "NVDA",
                "action": "buy",
                "rating": "strong_buy",
                "price_target": 1250.0,
                "stop_loss": 900.0,
                "position_sizing": "half",
            },
            "result": {
                "confidence": 0.82,
                "allocations": {"NVDA": 0.4, "CASH": 0.6},
                "time_horizon": "30d",
                "rationale": "positive momentum",
                "risk_flags": ["earnings_window"],
            },
            "validation": {"ok": False, "errors": ["symbol not in allowed universe"]},
        },
    )

    row = build_strategy_runs_table(store)[0]

    assert row["run_id"] == "run_signal"
    assert row["signal_action"] == "buy"
    assert row["signal_symbol"] == "NVDA"
    assert row["rating"] == "strong_buy"
    assert row["price_target"] == 1250.0
    assert row["stop_loss"] == 900.0
    assert row["position_sizing"] == "half"
    assert row["confidence"] == 0.82
    assert row["allocations"] == {"NVDA": 0.4, "CASH": 0.6}
    assert row["time_horizon"] == "30d"
    assert row["rationale"] == "positive momentum"
    assert row["risk_flags"] == ["earnings_window"]
    assert row["validation_ok"] is False
    assert row["validation_errors"] == ["symbol not in allowed universe"]


def test_strategy_runs_table_reads_metadata_source_signal_fallback(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_strategy_run(
        "run_metadata_signal",
        "signal_strategy",
        {
            "result": {
                "confidence": 0.64,
                "allocations": {"CASH": 1.0},
                "metadata": {
                    "source_signal": {
                        "symbol": "TSLA",
                        "action": "sell",
                        "rating": "underperform",
                        "price_target": 120.0,
                        "stop_loss": 210.0,
                        "position_sizing": "zero",
                    }
                },
            },
            "validation": {"ok": True, "errors": []},
        },
    )

    row = build_strategy_runs_table(store)[0]

    assert row["signal_action"] == "sell"
    assert row["signal_symbol"] == "TSLA"
    assert row["rating"] == "underperform"
    assert row["price_target"] == 120.0
    assert row["stop_loss"] == 210.0
    assert row["position_sizing"] == "zero"
    assert row["confidence"] == 0.64
    assert row["validation_ok"] is True


def test_strategy_runs_table_keeps_target_allocation_payload_compatible(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_strategy_run(
        "run_allocation",
        "allocation_strategy",
        {
            "result": {
                "confidence": 0.7,
                "allocations": {"MOCK_ETF_A": 0.5, "MOCK_ETF_B": 0.5},
                "risk_flags": [],
            },
            "validation": {"ok": True, "errors": []},
        },
    )

    row = build_strategy_runs_table(store)[0]

    assert row["signal_action"] is None
    assert row["signal_symbol"] is None
    assert row["rating"] is None
    assert row["price_target"] is None
    assert row["stop_loss"] is None
    assert row["position_sizing"] is None
    assert row["confidence"] == 0.7
    assert row["allocations"] == {"MOCK_ETF_A": 0.5, "MOCK_ETF_B": 0.5}
    assert row["risk_flags"] == []
    assert row["validation_ok"] is True


def test_dashboard_operational_read_models(tmp_path):
    raw = yaml.safe_load(Path("configs/kis_overseas_readonly.example.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["kis"]["token_cache_path"] = str(tmp_path / "token.json")
    config_path = tmp_path / "kis_overseas_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    store.save_system_event(
        "run_safety",
        "safety_state",
        {
            "state": "halted",
            "reason": "operator review",
            "source": "test",
            "created_at": "2026-05-10T00:00:00+00:00",
            "updated_at": "2026-05-10T00:00:00+00:00",
        },
    )
    store.save_broker_account_snapshot(
        "run_broker",
        "acct",
        {
            "account": {
                "account_id": "acct",
                "cash": 1000.0,
                "buying_power": 900.0,
                "positions": [{"symbol": "AAPL", "quantity": 1}],
                "source": "kis_overseas_stock_readonly",
            }
        },
    )
    store.save_system_event(
        "run_reconcile",
        "broker_reconciliation",
        {
            "passed": False,
            "issues": [{"issue_type": "cash_mismatch"}],
            "cash_difference": 10.0,
            "broker_account_id": "acct",
        },
    )
    store.save_system_event(
        "run_status",
        "live_order_status",
        {
            "snapshot": {
                "status": "open",
                "symbol": "AAPL",
            },
            "message": "open at broker",
        },
    )
    store.save_system_event(
        "run_lifecycle",
        "live_order_lifecycle",
        {
            "status": "completed",
            "request": {"symbol": "VOO"},
        },
    )
    store.save_system_event(
        "run_fill",
        "fill_reconciliation",
        {
            "applied_fills": [{"broker_order_id": "1"}],
            "skipped_fills": [],
            "portfolio_updated": True,
            "cash": 900.0,
            "positions": {"AAPL": 1},
        },
    )
    store.save_system_event(
        "run_live",
        "live_order_result",
        {
            "submitted_date": utc_now().date().isoformat(),
            "notional": 123.45,
        },
    )
    store.save_system_event(
        "run_halt",
        "live_order_halt",
        {
            "reason": "unknown broker state",
        },
    )

    assert build_safety_state_card(store)["state"] == "halted"
    assert build_health_summary(config, store)["status"] == "fail"
    assert build_latest_broker_snapshot_card(store)["positions_count"] == 1
    assert build_latest_reconciliation_card(store)["passed"] is False
    assert build_recent_halt_failure_events_table(store)[0]["event_type"] == "live_order_halt"
    assert len(build_live_order_events_table(store)) == 2
    assert build_fill_reconciliation_table(store)[0]["applied_fills"] == 1
    usage = build_daily_live_order_usage(config, store)
    assert usage["order_count"] == 1
    assert usage["notional"] == 123.45

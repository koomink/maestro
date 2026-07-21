from pathlib import Path

import pytest
import yaml

from maestro.config.broker import BrokerAccountConfig
from maestro.config.loader import load_config
from maestro.config.reconciliation_config import ReconciliationConfig
from maestro.core.clock import utc_now
from maestro.dashboard.read_models import (
    build_account_performance_table,
    build_approvals_table,
    build_broker_account_overview,
    build_broker_account_summary,
    build_broker_position_exposure_table,
    build_broker_snapshot_history_table,
    build_broker_snapshots_table,
    build_currency_sleeve_performance_table,
    build_daily_live_order_usage,
    build_fill_reconciliation_table,
    build_freshness_table,
    build_fx_rate_snapshot_card,
    build_health_summary,
    build_latest_broker_snapshot_card,
    build_latest_reconciliation_card,
    build_latest_signal_package_card,
    build_live_order_events_table,
    build_maestro_state_exposure_table,
    build_operator_summary,
    build_orders_table,
    build_overview,
    build_portfolio_snapshot_history_table,
    build_portfolio_table,
    build_recent_halt_failure_events_table,
    build_risk_decisions_table,
    build_run_detail,
    build_run_index_table,
    build_safety_state_card,
    build_strategy_actual_performance_table,
    build_strategy_attribution_table,
    build_strategy_book_performance_table,
    build_strategy_book_snapshots_table,
    build_strategy_runs_table,
    build_system_events_table,
    build_total_portfolio_performance_table,
)
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


def test_build_overview_works_with_empty_db(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)

    overview = build_overview(store)

    assert overview["cash"] == 1000
    assert overview["positions_count"] == 0
    assert overview["orders_count"] == 0
    assert overview["risk_decisions_count"] == 0
    assert overview["latest_run_id"] is None


def test_latest_signal_package_card_exposes_actionable_signal_run_id(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_signal_package(
        "signal_abc",
        {
            "status": "action_required",
            "action_required": True,
            "orders_preview_count": 2,
            "loaded_strategies": ["tranquillo"],
            "datahub_evidence": {"issue_count": 0},
        },
    )

    card = build_latest_signal_package_card(store)

    assert card["signal_run_id"] == "signal_abc"
    assert card["status"] == "action_required"
    assert card["action_required"] is True
    assert card["actionable_signal_run_id"] == "signal_abc"
    assert card["approval_consumed"] is False
    assert card["orders_preview_count"] == 2


def test_latest_signal_package_card_hides_consumed_signal_action(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_signal_package(
        "signal_consumed",
        {
            "status": "action_required",
            "action_required": True,
            "orders_preview_count": 1,
        },
    )
    store.mark_signal_package_consumed("signal_consumed", "run_approval")

    card = build_latest_signal_package_card(store)

    assert card["approval_consumed"] is True
    assert card["approval_run_id"] == "run_approval"
    assert card["actionable_signal_run_id"] is None


def test_operator_summary_works_with_empty_db(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    summary = build_operator_summary(config, store)

    assert summary["safety"]["state"] == "active"
    assert summary["reconciliation"]["passed"] is None
    assert summary["daily_live_usage"]["order_count"] == 0
    assert summary["live_order_lifecycle"]["recent_status_counts"] == {}
    assert isinstance(summary["attention_items"], list)


def test_health_summary_includes_operator_timezone_display_fields(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["market_session"] = {
        "required": False,
        "timezone": "Asia/Seoul",
        "open": "09:00",
        "close": "15:30",
        "weekdays": [0, 1, 2, 3, 4],
        "holidays": [],
    }
    raw["monitoring"] = {
        "heartbeat_max_age_seconds": 3600,
        "scheduled_run_max_age_seconds": 3600,
    }
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_system_event("run_heartbeat", "maestro_heartbeat", {})

    summary = build_health_summary(config, store)

    heartbeat = next(row for row in summary["checks"] if row["check"] == "heartbeat")
    assert summary["generated_at_display"].endswith("KST")
    assert heartbeat["details"]["created_at_display"].endswith("KST")


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
    assert len(build_strategy_book_snapshots_table(store)) == 1
    assert len(build_strategy_book_performance_table(store)) == 1


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
    assert build_broker_account_summary(store)["positions_count"] == 0
    assert build_broker_position_exposure_table(store) == []
    assert build_strategy_book_snapshots_table(store) == []
    assert build_strategy_book_performance_table(store) == []
    assert build_maestro_state_exposure_table(store)[0]["symbol"] == "CASH"
    assert build_portfolio_snapshot_history_table(store) == []
    assert build_broker_snapshot_history_table(store)[0]["account_id"] == "acct"



def test_broker_account_overview_reports_configured_account_states(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    config = type(
        "Config",
        (),
        {
            "accounts": [
                BrokerAccountConfig(
                    id="kis_mock",
                    broker="kis",
                    enabled=True,
                    broker_products=["kis_overseas_stock"],
                ),
                BrokerAccountConfig(
                    id="kis_isa",
                    broker="kis",
                    enabled=True,
                    broker_products=["kis_overseas_stock"],
                ),
                BrokerAccountConfig(id="dev_sandbox", broker="sandbox", enabled=True),
                BrokerAccountConfig(
                    id="disabled",
                    broker="kis",
                    enabled=False,
                    broker_products=["kis_overseas_stock"],
                ),
            ],
            "reconciliation": ReconciliationConfig(max_age_seconds=86400),
        },
    )()
    store.save_broker_account_snapshot(
        "run_old",
        "kis_mock",
        {
            "account_id": "kis_mock",
            "broker_account_id": "OLD-BROKER",
            "account": {
                "account_id": "OLD-BROKER",
                "currency": "KRW",
                "cash": 100.0,
                "total_value": 100.0,
                "positions": [],
            },
        },
    )
    store.save_broker_account_snapshot(
        "run_new",
        "kis_mock",
        {
            "account_id": "kis_mock",
            "broker_account_id": "MOCK-BROKER",
            "account": {
                "account_id": "MOCK-BROKER",
                "currency": "KRW",
                "cash": 900.0,
                "total_value": 1000.0,
                "positions": [{"symbol": "AAA", "quantity": 1, "current_price": 100.0}],
                "source": "kis_rest_readonly",
            },
        },
    )

    overview = build_broker_account_overview(config, store)

    assert overview["summary"]["configured_accounts"] == 2
    assert overview["summary"]["fresh_accounts"] == 1
    assert overview["summary"]["missing_accounts"] == 1
    assert overview["summary"]["attention_accounts"] == 1
    assert overview["summary"]["total_value"] == 1000.0
    assert overview["summary"]["currency"] == "KRW"
    rows = {row["account_id"]: row for row in overview["accounts"]}
    assert set(rows) == {"kis_mock", "kis_isa"}
    assert rows["kis_mock"]["status"] == "fresh"
    assert rows["kis_mock"]["broker_account_id"] == "MOCK-BROKER"
    assert rows["kis_mock"]["cash"] == 900.0
    assert rows["kis_mock"]["positions_count"] == 1
    assert rows["kis_isa"]["status"] == "missing"
    assert rows["kis_isa"]["broker_account_id"] is None


def test_system_events_table_reports_required_field_status(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_system_event(
        "run_valid",
        "broker_reconciliation",
        {
            "passed": True,
            "checked_at": utc_now().isoformat(),
            "issues": [],
        },
    )
    store.save_system_event(
        "run_invalid",
        "fill_reconciliation",
        {
            "applied_fills": [],
            "skipped_fills": [],
        },
    )
    store.save_system_event("run_custom", "custom_event", {})

    rows = build_system_events_table(store, limit=3)
    by_type = {row["event_type"]: row for row in rows}

    assert by_type["broker_reconciliation"]["schema_status"] == "ok"
    assert by_type["broker_reconciliation"]["missing_required_fields"] == []
    assert by_type["fill_reconciliation"]["schema_status"] == "missing_required_fields"
    assert by_type["fill_reconciliation"]["missing_required_fields"] == [
        "checked_at",
        "portfolio_updated",
    ]
    assert by_type["custom_event"]["schema_status"] == "untracked"


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
    raw = yaml.safe_load(
        Path("tests/fixtures/configs/live_readonly_multi_asset_kis.yaml").read_text()
    )
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["kis"]["token_cache_path"] = str(tmp_path / "token.json")
    config_path = tmp_path / "multi_asset_readonly.yaml"
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


def test_operator_summary_collects_attention_items_and_live_order_lifecycle(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["kis"]["token_cache_path"] = str(tmp_path / "token.json")
    raw["execution"]["live_order_limits"]["max_daily_order_count"] = 2
    raw["execution"]["live_order_limits"]["max_daily_notional"] = 200.0
    config_path = tmp_path / "live_approval.yaml"
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
            "created_at": utc_now().isoformat(),
            "updated_at": utc_now().isoformat(),
        },
    )
    store.save_system_event(
        "run_reconcile",
        "broker_reconciliation",
        {"passed": False, "issues": [{"issue_type": "cash_mismatch"}]},
    )
    store.save_system_event(
        "run_order_open",
        "live_order_lifecycle",
        {
            "final_status": "open",
            "order_id": "ord_open",
            "broker_order_id": "broker_open",
            "status_snapshots": [{"status": "open", "symbol": "AAPL"}],
        },
    )
    store.save_system_event(
        "run_order_failed",
        "live_order_lifecycle",
        {
            "final_status": "failed",
            "order_id": "ord_failed",
            "broker_order_id": "broker_failed",
            "failed_reason": "broker reconciliation failed",
        },
    )
    for index in range(2):
        store.save_system_event(
            f"run_live_{index}",
            "live_order_result",
            {
                "submitted_date": utc_now().date().isoformat(),
                "notional": 125.0,
            },
        )

    summary = build_operator_summary(config, store)
    codes = {item["code"] for item in summary["attention_items"]}

    assert "safety_not_active" in codes
    assert "health_not_ok" in codes
    assert "reconciliation_failed" in codes
    assert "daily_live_order_count_limit" in codes
    assert "daily_live_notional_limit" in codes
    assert "recent_live_order_issue" in codes
    assert summary["live_order_lifecycle"]["latest"]["status"] == "failed"
    assert summary["live_order_lifecycle"]["recent_status_counts"] == {
        "failed": 1,
        "open": 1,
    }


def test_freshness_table_labels_fresh_stale_missing_and_disabled_rows(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["kis"]["token_cache_path"] = str(tmp_path / "token.json")
    raw.setdefault("reconciliation", {})["max_age_seconds"] = 60
    raw["monitoring"] = {
        "heartbeat_max_age_seconds": 60,
        "scheduled_run_max_age_seconds": 0,
    }
    config_path = tmp_path / "live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    store.save_broker_account_snapshot(
        "run_broker",
        "acct",
        {"account": {"account_id": "acct", "cash": 1000.0, "positions": []}},
    )
    store.save_system_event("run_heartbeat", "maestro_heartbeat", {"source": "test"})

    rows = {row["name"]: row for row in build_freshness_table(config, store)}

    assert rows["broker_snapshot"]["status"] == "fresh"
    assert rows["broker_reconciliation"]["status"] == "missing"
    assert rows["heartbeat"]["status"] == "fresh"
    assert rows["scheduled_run"]["status"] == "not_configured"


def test_freshness_policy_marks_stale_invalid_and_failed_precedence(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["kis"]["token_cache_path"] = str(tmp_path / "token.json")
    raw.setdefault("reconciliation", {})["max_age_seconds"] = 60
    raw["monitoring"] = {
        "heartbeat_max_age_seconds": 60,
        "scheduled_run_max_age_seconds": 60,
    }
    config_path = tmp_path / "live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)

    store.save_broker_account_snapshot(
        "run_broker",
        "acct",
        {"account": {"account_id": "acct", "cash": 1000.0, "positions": []}},
    )
    store.save_system_event(
        "run_reconciliation",
        "broker_reconciliation",
        {"passed": False, "checked_at": utc_now().isoformat(), "issues": []},
    )
    store.save_system_event("run_heartbeat", "maestro_heartbeat", {"source": "test"})
    store.save_system_event(
        "run_completed",
        "run_once_completed",
        {"orders_created": 0, "total_value": 1000.0, "cash": 1000.0},
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE broker_account_snapshots SET created_at = '2000-01-01 00:00:00'"
        )
        conn.execute("UPDATE system_events SET created_at = '2000-01-01 00:00:00'")
        conn.execute(
            "UPDATE system_events SET created_at = 'not-a-date' "
            "WHERE event_type = 'maestro_heartbeat'"
        )

    rows = {row["name"]: row for row in build_freshness_table(config, store)}

    assert rows["broker_snapshot"]["status"] == "stale"
    assert rows["broker_reconciliation"]["status"] == "failed"
    assert rows["heartbeat"]["status"] == "stale"
    assert rows["scheduled_run"]["status"] == "stale"
    assert rows["heartbeat"]["age_seconds"] is None
    assert rows["broker_reconciliation"]["payload_status"] == "failed"


def test_dashboard_broker_portfolio_analytics_from_latest_snapshot(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_broker_account_snapshot(
        "run_broker",
        "acct",
        {
            "account": {
                "account_id": "acct",
                "cash": 1000.0,
                "buying_power": 800.0,
                "positions": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple",
                        "quantity": 2,
                        "average_price": 100,
                        "current_price": 150,
                        "unrealized_pnl": 100,
                    },
                    {
                        "symbol": "MSFT",
                        "quantity": 1,
                        "average_price": 200,
                        "current_price": 250,
                        "unrealized_pnl": 50,
                    },
                ],
                "source": "kis_overseas_stock_readonly",
            },
            "current_prices": {"AAPL": 150.0, "MSFT": 250.0},
        },
    )

    summary = build_broker_account_summary(store)
    positions = build_broker_position_exposure_table(store)
    history = build_broker_snapshot_history_table(store)

    assert summary["positions_count"] == 2
    assert summary["positions_market_value"] == 550.0
    assert summary["total_value"] == 1550.0
    assert summary["cash_weight"] == 1000.0 / 1550.0
    assert summary["exposure_weight"] == 550.0 / 1550.0
    assert summary["unrealized_pnl"] == 150.0
    assert positions[0]["symbol"] == "AAPL"
    assert positions[0]["market_value"] == 300.0
    assert positions[0]["weight"] == 300.0 / 1550.0
    assert positions[1]["symbol"] == "MSFT"
    assert history[0]["total_value"] == 1550.0


def test_broker_summary_and_exposure_aggregate_latest_snapshot_per_account(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_broker_account_snapshot(
        "run_refresh",
        "kis_mock",
        {
            "account_id": "kis_mock",
            "account": {
                "account_id": "MOCK",
                "currency": "KRW",
                "cash": 9_939_195.0,
                "positions": [
                    {
                        "symbol": "KODEX_US_DIVIDEND_DOWJONES",
                        "quantity": 1,
                        "average_price": 32_000,
                        "current_price": 32_000,
                    },
                    {
                        "symbol": "TIGER_NASDAQ100_LEVERAGE",
                        "quantity": 1,
                        "average_price": 33_970,
                        "current_price": 33_970,
                    },
                ],
            },
        },
    )
    store.save_broker_account_snapshot(
        "run_refresh",
        "kis_ps",
        {
            "account_id": "kis_ps",
            "account": {
                "account_id": "PS",
                "currency": "KRW",
                "cash": 0.0,
                "positions": [],
            },
        },
    )

    summary = build_broker_account_summary(store)
    positions = build_broker_position_exposure_table(store)

    assert summary["account_id"] == "multiple"
    assert summary["cash"] == 9_939_195.0
    assert summary["positions_count"] == 2
    assert summary["positions_market_value"] == 65_970.0
    assert summary["total_value"] == 10_005_165.0
    assert [row["symbol"] for row in positions] == [
        "KODEX_US_DIVIDEND_DOWJONES",
        "TIGER_NASDAQ100_LEVERAGE",
    ]
    assert positions[0]["account_id"] == "kis_mock"


def test_disabled_account_excluded_from_totals_when_config_is_supplied(tmp_path):
    """Regression test: a disabled/retired account's stale snapshot must not
    keep inflating aggregate totals forever.

    Broker snapshots are never deleted, so once an account is disabled (e.g.
    a mock/paper account retired after setup), its last known snapshot would
    otherwise be "carried forward" into every total computed from
    build_broker_account_summary / build_broker_position_exposure_table /
    build_total_portfolio_performance_table — silently overstating Total
    Asset by whatever that stale snapshot's value was. See the kis_mock
    incident this test models: a disabled paper account's June snapshot
    inflated production "Total assets" by ~10M KRW alongside three real,
    still-enabled accounts.
    """
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    config = type(
        "Config",
        (),
        {
            "accounts": [
                BrokerAccountConfig(
                    id="kis_mock",
                    broker="kis",
                    enabled=False,
                    broker_products=["kis_overseas_stock"],
                ),
                BrokerAccountConfig(
                    id="kis_ps",
                    broker="kis",
                    enabled=True,
                    broker_products=["kis_overseas_stock"],
                ),
            ],
        },
    )()
    store.save_broker_account_snapshot(
        "run_disabled",
        "kis_mock",
        {
            "account_id": "kis_mock",
            "account": {
                "account_id": "MOCK-BROKER",
                "currency": "KRW",
                "cash": 10_000_000.0,
                "total_value": 10_000_000.0,
                "positions": [],
            },
        },
    )
    store.save_broker_account_snapshot(
        "run_enabled",
        "kis_ps",
        {
            "account_id": "kis_ps",
            "account": {
                "account_id": "PS-BROKER",
                "currency": "KRW",
                "cash": 5_000_000.0,
                "total_value": 5_000_000.0,
                "positions": [],
            },
        },
    )

    # With config: the disabled account must be excluded.
    summary = build_broker_account_summary(store, config)
    positions = build_broker_position_exposure_table(store, config)
    performance = build_total_portfolio_performance_table(store, config, display_currency="KRW")

    assert summary["cash"] == 5_000_000.0
    assert summary["total_value"] == 5_000_000.0
    assert all(row["account_id"] != "kis_mock" for row in positions)
    assert performance[0]["total_value"] == 5_000_000.0

    # Without config: unfiltered, backward-compatible behavior is preserved
    # (older/simple configs with no accounts list have nothing to filter by).
    unfiltered_summary = build_broker_account_summary(store)
    assert unfiltered_summary["total_value"] == 15_000_000.0


def test_disabled_account_excluded_from_account_and_currency_sleeve_tables(tmp_path):
    """Same disabled-account leak as above, but for the per-account and
    per-currency-sleeve performance tables that back the Portfolio tab's
    Account Matrix. These builders historically never accepted a config and
    so never filtered disabled accounts at all.
    """
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    config = type(
        "Config",
        (),
        {
            "accounts": [
                BrokerAccountConfig(
                    id="kis_mock",
                    broker="kis",
                    enabled=False,
                    broker_products=["kis_overseas_stock"],
                ),
                BrokerAccountConfig(
                    id="kis_ps",
                    broker="kis",
                    enabled=True,
                    broker_products=["kis_overseas_stock"],
                ),
            ],
        },
    )()
    store.save_broker_account_snapshot(
        "run_disabled",
        "kis_mock",
        {
            "account_id": "kis_mock",
            "account": {
                "account_id": "MOCK-BROKER",
                "currency": "KRW",
                "cash": 10_000_000.0,
                "total_value": 10_000_000.0,
                "positions": [],
            },
        },
    )
    store.save_broker_account_snapshot(
        "run_enabled",
        "kis_ps",
        {
            "account_id": "kis_ps",
            "account": {
                "account_id": "PS-BROKER",
                "currency": "KRW",
                "cash": 5_000_000.0,
                "total_value": 5_000_000.0,
                "positions": [],
            },
        },
    )

    account_rows = build_account_performance_table(store, config)
    assert all(row["account_id"] != "kis_mock" for row in account_rows)
    assert [row["account_id"] for row in account_rows] == ["kis_ps"]

    currency_rows = build_currency_sleeve_performance_table(store, config)
    assert sum(row["total_value"] for row in currency_rows) == 5_000_000.0

    # Without config: unfiltered, backward-compatible behavior is preserved.
    unfiltered_account_rows = build_account_performance_table(store)
    assert {row["account_id"] for row in unfiltered_account_rows} == {"kis_mock", "kis_ps"}


def test_account_performance_table_tracks_returns_drawdown_and_reconciliation(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    snapshots = [
        ("run_1", 1000.0, 0.0),
        ("run_2", 1100.0, 100.0),
        ("run_3", 990.0, -10.0),
    ]
    for run_id, total_value, unrealized_pnl in snapshots:
        store.save_broker_account_snapshot(
            run_id,
            "acct",
            {
                "account": {
                    "account_id": "acct",
                    "currency": "USD",
                    "cash": 100.0,
                    "total_value": total_value,
                    "unrealized_pnl": unrealized_pnl,
                    "positions": [],
                    "source": "kis_overseas_stock_readonly",
                }
            },
        )
    latest_snapshot = store.list_broker_account_snapshots(limit=1)[0]
    store.save_system_event(
        "run_reconcile",
        "broker_reconciliation",
        {
            "passed": False,
            "issues": [{"issue_type": "position_mismatch"}],
            "broker_snapshot_id": latest_snapshot["id"],
        },
    )

    rows = build_account_performance_table(store)

    assert [row["run_id"] for row in rows] == ["run_3", "run_2", "run_1"]
    assert rows[0]["total_value"] == 990.0
    assert rows[0]["period_return"] == -0.1
    assert rows[0]["cumulative_return"] == -0.01
    assert rows[0]["drawdown"] == -0.1
    assert rows[0]["reconciliation_status"] == "failed"
    assert rows[1]["period_return"] == 0.1
    assert rows[1]["cumulative_return"] == 0.1
    assert rows[1]["drawdown"] == 0.0
    assert rows[2]["period_return"] is None
    assert rows[2]["cumulative_return"] == 0.0
    assert rows[2]["reconciliation_status"] == "unreconciled"


def test_strategy_book_performance_uses_explicit_strategy_cash_flows_for_twr_and_mwr(
    tmp_path,
):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    snapshots = [
        ("run_1", 1000.0, "2024-01-01T00:00:00+00:00"),
        ("run_2", 1600.0, "2024-07-01T00:00:00+00:00"),
        ("run_3", 1700.0, "2025-01-01T00:00:00+00:00"),
    ]
    for run_id, book_value, created_at in snapshots:
        store.save_strategy_book_snapshots(
            run_id,
            [
                {
                    "strategy_id": "ataraxia",
                    "book_id": "ataraxia:KRW",
                    "book_value": book_value,
                    "cash": 100.0,
                    "allocations": {"CASH": 1.0},
                }
            ],
        )
        with store._connect() as conn:
            conn.execute(
                "UPDATE strategy_book_snapshots SET created_at = ? WHERE run_id = ?",
                (created_at, run_id),
            )
    store.save_system_event(
        "cash_flow_1",
        "strategy_cash_flow",
        {
            "strategy_id": "ataraxia",
            "account_id": "paper_cash",
            "execution_sleeve": "krw_contribution",
            "amount": 500.0,
            "currency": "KRW",
            "flow_type": "deposit",
            "effective_at": "2024-07-01T00:00:00+00:00",
            "source": "telegram_funding_confirmation",
        },
    )

    rows = build_strategy_book_performance_table(store)

    assert [row["run_id"] for row in rows] == ["run_3", "run_2", "run_1"]
    latest = rows[0]
    assert latest["current_value"] == 1700.0
    assert latest["book_value"] == 1700.0
    assert latest["cash_flow"] == 0.0
    assert latest["cumulative_cash_flow"] == 500.0
    assert latest["net_pnl"] == 200.0
    assert latest["period_return"] == 0.0625
    assert latest["twr"] == 0.16875
    assert latest["cumulative_return"] == 0.16875
    assert latest["drawdown"] == 0.0
    assert latest["mwr"] == pytest.approx(0.161, abs=0.01)
    assert rows[1]["cash_flow"] == 500.0
    assert rows[1]["period_return"] == 0.1


def test_broker_performance_prefers_broker_total_asset_value_and_currency(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_broker_account_snapshot(
        "run_domestic",
        "acct",
        {
            "account": {
                "account_id": "acct",
                "cash": 1_000_000.0,
                "cash_by_currency": {"KRW": 1_000_000.0},
                "cash_balance": {
                    "currency": "KRW",
                    "cash": 1_000_000.0,
                    "total_asset_value": 999_990.0,
                    "withdrawable_cash": 1_000_000.0,
                },
                "positions": [
                    {
                        "symbol": "KODEX_US_DIVIDEND_DOWJONES",
                        "quantity": 1.0,
                        "current_price": 12_905.0,
                        "average_price": 12_915.0,
                        "unrealized_pnl": -10.0,
                    }
                ],
                "source": "kis_rest_readonly",
            }
        },
    )

    broker_history = build_broker_snapshot_history_table(store)
    account_rows = build_account_performance_table(store)
    currency_rows = build_currency_sleeve_performance_table(store)
    total_rows = build_total_portfolio_performance_table(store)

    assert broker_history[0]["total_value"] == 999_990.0
    assert account_rows[0]["currency"] == "KRW"
    assert account_rows[0]["total_value"] == 999_990.0
    assert currency_rows[0]["currency"] == "KRW"
    assert currency_rows[0]["total_value"] == 999_990.0
    assert total_rows[0]["currency"] == "KRW"
    assert total_rows[0]["total_value"] == 999_990.0


def test_currency_sleeve_performance_preserves_currencies_separately(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    snapshots = [
        ("run_1", "acct_usd", "USD", 1000.0),
        ("run_1", "acct_krw", "KRW", 1_000_000.0),
        ("run_2", "acct_usd", "USD", 1100.0),
        ("run_2", "acct_krw", "KRW", 950_000.0),
    ]
    for run_id, account_id, currency, total_value in snapshots:
        store.save_broker_account_snapshot(
            run_id,
            account_id,
            {
                "account": {
                    "account_id": account_id,
                    "currency": currency,
                    "cash": total_value,
                    "total_value": total_value,
                    "positions": [],
                }
            },
        )

    rows = build_currency_sleeve_performance_table(store)
    latest_by_currency = {row["currency"]: row for row in rows[:2]}

    assert latest_by_currency["USD"]["total_value"] == 1100.0
    assert latest_by_currency["USD"]["period_return"] == 0.1
    assert latest_by_currency["USD"]["cumulative_return"] == 0.1
    assert latest_by_currency["KRW"]["total_value"] == 950_000.0
    assert latest_by_currency["KRW"]["period_return"] == -0.05
    assert latest_by_currency["KRW"]["cumulative_return"] == -0.05


def test_currency_sleeve_performance_splits_single_mixed_currency_account(tmp_path):
    """Regression test: a broker (e.g. Toss) can report ONE account holding
    both KRW cash and a USD-listed position with no pre-aggregated total.
    Labeling the whole account with a single currency (as
    build_currency_sleeve_performance_table used to) folds the USD sleeve
    into the KRW row, making the USD sleeve appear to not exist even though
    Portfolio Pulse's separately-computed "USD Assets" shows it correctly.
    """
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_broker_account_snapshot(
        "run_1",
        "toss_brokerage",
        {
            "account": {
                "account_id": "toss_brokerage",
                "currency": "KRW",
                "cash": 500_000.0,
                "positions": [
                    {
                        "symbol": "QQQ",
                        "currency": "USD",
                        "quantity": 10.0,
                        "current_price": 500.0,
                    }
                ],
            }
        },
    )

    rows = build_currency_sleeve_performance_table(store)
    by_currency = {row["currency"]: row for row in rows}

    assert set(by_currency) == {"KRW", "USD"}
    assert by_currency["KRW"]["total_value"] == 500_000.0
    assert by_currency["USD"]["total_value"] == 5_000.0


def test_account_performance_drawdown_is_not_contaminated_across_accounts(tmp_path):
    """Regression test: build_account_performance_table used to track ONE
    shared first/previous/peak-value state across ALL accounts' snapshots
    interleaved together, instead of one state per account. A small
    cash-only account sitting flat at its own value would show a huge fake
    "drawdown" purely because a much larger, unrelated account's peak value
    got compared against it.
    """
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    # A big account whose value swings a lot...
    for run_id, total_value in [("run_1", 10_000_000.0), ("run_2", 12_000_000.0)]:
        store.save_broker_account_snapshot(
            run_id,
            "big_account",
            {
                "account": {
                    "account_id": "big_account",
                    "currency": "KRW",
                    "cash": total_value,
                    "total_value": total_value,
                    "positions": [],
                }
            },
        )
    # ...interleaved with a small account that never changes at all.
    for run_id in ["run_1", "run_2"]:
        store.save_broker_account_snapshot(
            run_id,
            "small_account",
            {
                "account": {
                    "account_id": "small_account",
                    "currency": "KRW",
                    "cash": 500_000.0,
                    "total_value": 500_000.0,
                    "positions": [],
                }
            },
        )

    rows = build_account_performance_table(store)
    latest_by_account = {row["account_id"]: row for row in rows[:2]}

    assert latest_by_account["small_account"]["total_value"] == 500_000.0
    assert latest_by_account["small_account"]["drawdown"] == 0.0
    assert latest_by_account["small_account"]["cumulative_return"] == 0.0


def test_total_portfolio_performance_marks_missing_fx_for_mixed_currency(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    for run_id, usd_value, krw_value in [
        ("run_1", 1000.0, 1_000_000.0),
        ("run_2", 1100.0, 1_100_000.0),
    ]:
        store.save_broker_account_snapshot(
            run_id,
            "acct_usd",
            {
                "account": {
                    "account_id": "acct_usd",
                    "currency": "USD",
                    "cash": usd_value,
                    "total_value": usd_value,
                    "positions": [],
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
                    "cash": krw_value,
                    "total_value": krw_value,
                    "positions": [],
                }
            },
        )

    rows = build_total_portfolio_performance_table(store)

    assert [row["run_id"] for row in rows] == ["run_2", "run_1"]
    assert rows[0]["missing_fx"] is True
    assert rows[0]["total_value"] is None
    assert rows[0]["component_values"] == {"KRW": 1_100_000.0, "USD": 1100.0}
    assert rows[0]["cumulative_return"] is None


def test_total_portfolio_performance_sums_latest_snapshots_across_accounts(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    for run_id, account_id, total_value in [
        ("run_live_account", "acct_live", 10_006_055.0),
        ("run_empty_isa", "acct_isa", 0.0),
        ("run_empty_ps", "acct_ps", 0.0),
    ]:
        store.save_broker_account_snapshot(
            run_id,
            account_id,
            {
                "account": {
                    "account_id": account_id,
                    "currency": "KRW",
                    "cash": total_value,
                    "total_value": total_value,
                    "positions": [],
                }
            },
        )

    rows = build_total_portfolio_performance_table(store)

    assert rows[0]["run_id"] == "run_empty_ps"
    assert rows[0]["total_value"] == 10_006_055.0
    assert rows[0]["component_values"] == {"KRW": 10_006_055.0}


def test_total_portfolio_performance_uses_persisted_fx_for_display_currency(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    for run_id, usd_value, krw_value in [
        ("run_1", 1000.0, 1_000_000.0),
        ("run_2", 1100.0, 1_100_000.0),
    ]:
        store.save_broker_account_snapshot(
            run_id,
            "acct_usd",
            {
                "account": {
                    "account_id": "acct_usd",
                    "currency": "USD",
                    "cash": usd_value,
                    "total_value": usd_value,
                    "positions": [],
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
                    "cash": krw_value,
                    "total_value": krw_value,
                    "positions": [],
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

    fx = build_fx_rate_snapshot_card(store)
    rows = build_total_portfolio_performance_table(store, display_currency="KRW")

    assert fx["status"] == "fresh"
    assert rows[0]["display_currency"] == "KRW"
    assert rows[0]["fx_status"] == "fresh"
    assert rows[0]["total_value"] == 2_200_000.0
    assert rows[0]["period_return"] == 0.1
    assert rows[0]["local_return"] == 0.1
    assert rows[0]["fx_effect"] == 0.0


def test_fx_snapshot_freshness_uses_fetched_at_when_available(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_broker_account_snapshot(
        "run_1",
        "acct_usd",
        {
            "account": {
                "account_id": "acct_usd",
                "currency": "USD",
                "cash": 1000.0,
                "total_value": 1000.0,
                "positions": [],
            }
        },
    )
    store.save_system_event(
        "run_fx",
        "fx_rate_snapshot",
        {
            "source": "fixture",
            "as_of": "2000-01-01T00:00:00+00:00",
            "fetched_at": utc_now().isoformat(),
            "max_age_seconds": 3600,
            "rates": {"USD/KRW": 1000.0},
        },
    )

    fx = build_fx_rate_snapshot_card(store)
    rows = build_total_portfolio_performance_table(store, display_currency="KRW")

    assert fx["status"] == "fresh"
    assert fx["as_of"] == "2000-01-01T00:00:00+00:00"
    assert rows[0]["fx_status"] == "fresh"
    assert rows[0]["total_value"] == 1_000_000.0


def test_total_portfolio_performance_disables_converted_return_for_stale_fx(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_broker_account_snapshot(
        "run_1",
        "acct_usd",
        {
            "account": {
                "account_id": "acct_usd",
                "currency": "USD",
                "cash": 1000.0,
                "total_value": 1000.0,
                "positions": [],
            }
        },
    )
    store.save_system_event(
        "run_fx",
        "fx_rate_snapshot",
        {
            "source": "fixture",
            "as_of": "2000-01-01T00:00:00+00:00",
            "max_age_seconds": 1,
            "rates": {"USD/KRW": 1000.0},
        },
    )

    rows = build_total_portfolio_performance_table(store, display_currency="KRW")

    assert rows[0]["fx_status"] == "stale"
    assert rows[0]["stale_fx"] is True
    assert rows[0]["total_value"] is None
    assert rows[0]["cumulative_return"] is None


def test_run_detail_and_strategy_attribution_group_persisted_rows_by_run(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_strategy_run(
        "run_1",
        "strategy_1",
        {
            "source_signal": {"symbol": "AAPL", "action": "buy"},
            "result": {"confidence": 0.7, "allocations": {"AAPL": 0.5}},
            "validation": {"ok": True, "errors": []},
        },
    )
    store.save_order(
        "run_1",
        "ord_1",
        {
            "order_id": "ord_1",
            "broker_order_id": "KIS-1",
            "symbol": "AAPL",
            "side": "buy",
            "notional": 500.0,
        },
    )
    store.save_risk_decision("run_1", True, {"violations": []})
    store.save_system_event("run_1", "run_once_completed", {"orders_created": 1})
    store.save_system_event(
        "run_1",
        "fill_reconciliation",
        {
            "applied_fills": [
                {
                    "broker_order_id": "KIS-1",
                    "symbol": "AAPL",
                    "quantity": 2.0,
                    "notional": 300.0,
                }
            ],
            "skipped_fills": [],
        },
    )
    store.save_strategy_book_snapshots(
        "run_1",
        [
            {
                "strategy_id": "strategy_1",
                "book_id": "strategy_1:USD",
                "book_value": 1000.0,
                "allocations": {"AAPL": 0.5, "CASH_USD": 0.5},
            }
        ],
    )

    index = build_run_index_table(store)
    detail = build_run_detail(store, "run_1")
    attribution = build_strategy_attribution_table(store)

    assert index[0]["run_id"] == "run_1"
    assert detail["summary"]["strategy_runs"] == 1
    assert detail["summary"]["orders"] == 1
    assert {row["kind"] for row in detail["timeline"]} >= {
        "strategy_run",
        "order",
        "system_event",
    }
    assert attribution[0]["strategy_id"] == "strategy_1"
    assert attribution[0]["signal_action"] == "buy"
    assert attribution[0]["allocation_count"] == 2
    assert attribution[0]["order_count"] == 1
    assert attribution[0]["fill_count"] == 1
    assert attribution[0]["lineage"]["strategy_run"]["run_id"] == "run_1"
    assert attribution[0]["lineage"]["orders"][0]["order_id"] == "ord_1"
    assert attribution[0]["lineage"]["fills"][0]["broker_order_id"] == "KIS-1"
    assert attribution[0]["lineage"]["allocation_symbols"] == ["AAPL", "CASH_USD"]


def test_dashboard_maestro_state_exposure_uses_latest_broker_prices(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    store.save_portfolio_snapshot(
        "run_state",
        PortfolioState(
            cash=300.0,
            cash_by_currency={"USD": 300.0},
            positions={"AAPL": 2.0, "MISSING": 1.0},
        ),
    )
    store.save_broker_account_snapshot(
        "run_broker",
        "acct",
        {
            "account": {
                "account_id": "acct",
                "cash": 300.0,
                "buying_power": 300.0,
                "positions": [{"symbol": "AAPL", "quantity": 2, "current_price": 150.0}],
            },
            "current_prices": {"AAPL": 150.0},
        },
    )

    rows = build_maestro_state_exposure_table(store)
    history = build_portfolio_snapshot_history_table(store)

    assert rows[0] == {
        "symbol": "USD",
        "kind": "cash",
        "quantity": 300.0,
        "price": 1.0,
        "estimated_value": 300.0,
        "missing_price": False,
    }
    assert rows[1]["symbol"] == "AAPL"
    assert rows[1]["estimated_value"] == 300.0
    assert rows[1]["missing_price"] is False
    assert rows[2]["symbol"] == "MISSING"
    assert rows[2]["estimated_value"] is None
    assert rows[2]["missing_price"] is True
    assert history[0]["estimated_positions_value"] == 300.0
    assert history[0]["estimated_total_value"] is None
    assert history[0]["missing_prices"] == ["MISSING"]


def test_dashboard_snapshot_histories_are_latest_first_and_limited(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1000)
    for index in range(3):
        store.save_portfolio_snapshot(
            f"run_state_{index}",
            PortfolioState(cash=1000.0 + index, positions={}),
        )
        store.save_broker_account_snapshot(
            f"run_broker_{index}",
            f"acct_{index}",
            {
                "account": {
                    "account_id": f"acct_{index}",
                    "cash": 1000.0 + index,
                    "buying_power": 900.0 + index,
                    "positions": [],
                }
            },
        )

    portfolio_history = build_portfolio_snapshot_history_table(store, limit=2)
    broker_history = build_broker_snapshot_history_table(store, limit=2)

    assert [row["run_id"] for row in portfolio_history] == ["run_state_2", "run_state_1"]
    assert [row["run_id"] for row in broker_history] == ["run_broker_2", "run_broker_1"]


def _set_created_at(store, table, run_id, created_at):
    with store._connect() as conn:
        conn.execute(
            f"UPDATE {table} SET created_at = ? WHERE run_id = ?",
            (created_at, run_id),
        )


def _actual_perf_config(strategies):
    return type(
        "Config",
        (),
        {
            "strategies": strategies,
            "multi_account_contribution_group_for_strategy": staticmethod(lambda _sid: None),
        },
    )()


def _actual_perf_strategy(strategy_id, account_id, sleeve):
    return type(
        "Strategy",
        (),
        {
            "id": strategy_id,
            "account_id": account_id,
            "execution_sleeve": sleeve,
            "enabled": True,
        },
    )()


def _toss_broker_payload(quantity, price):
    return {
        "account_id": "toss_brokerage",
        "account": {
            "account_id": "toss_brokerage",
            "currency": "KRW",
            "cash": 0.0,
            "positions": [
                {"symbol": "QQQ", "quantity": quantity, "current_price": price},
            ],
        },
    }


def test_strategy_actual_performance_values_attributed_holdings_with_broker_prices(tmp_path):
    from maestro.portfolio.account_attribution import AttributionPosition

    store = StateStore(str(tmp_path / "state.db"))
    store.save_account_attribution_snapshot(
        "attr_1",
        [
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="crescendo_us",
                quantity=2.0,
                version=1,
            ),
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="manual",
                quantity=1.0,
                version=1,
            ),
        ],
    )
    _set_created_at(store, "account_attribution_snapshots", "attr_1", "2026-07-01 00:00:00")
    store.save_broker_account_snapshot("broker_1", "toss_brokerage", _toss_broker_payload(3, 100.0))
    _set_created_at(store, "broker_account_snapshots", "broker_1", "2026-07-01 00:00:01")
    store.save_broker_account_snapshot("broker_2", "toss_brokerage", _toss_broker_payload(3, 110.0))
    _set_created_at(store, "broker_account_snapshots", "broker_2", "2026-07-02 00:00:01")
    store.save_account_attribution_snapshot(
        "attr_2",
        [
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="crescendo_us",
                quantity=3.0,
                version=2,
            ),
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="manual",
                quantity=1.0,
                version=2,
            ),
        ],
    )
    _set_created_at(store, "account_attribution_snapshots", "attr_2", "2026-07-03 00:00:00")
    store.save_broker_account_snapshot("broker_3", "toss_brokerage", _toss_broker_payload(4, 110.0))
    _set_created_at(store, "broker_account_snapshots", "broker_3", "2026-07-03 00:00:01")

    config = _actual_perf_config(
        [
            _actual_perf_strategy("crescendo_us", "toss_brokerage", "crescendo_us"),
            _actual_perf_strategy("tranquillo", "kis_isa", "tranquillo"),
        ]
    )

    rows = build_strategy_actual_performance_table(store, config)

    # No attribution ledger exists for tranquillo's account, so it emits no rows.
    assert {row["strategy_id"] for row in rows} == {"crescendo_us"}
    ordered = list(reversed(rows))
    # Manual-bucket quantities never count toward the strategy's value.
    assert [row["book_value"] for row in ordered] == [200.0, 220.0, 330.0]
    assert ordered[0]["basis"] == "actual"
    assert ordered[1]["period_return"] == pytest.approx(0.10)
    # The attributed buy is a cash flow, not performance: TWR stays at +10%.
    assert ordered[2]["cash_flow"] == pytest.approx(110.0)
    assert ordered[2]["period_return"] == pytest.approx(0.0)
    assert ordered[2]["cumulative_return"] == pytest.approx(0.10)
    assert ordered[2]["positions"] == {"QQQ": 3.0}


def test_strategy_actual_performance_resolves_multi_account_scopes(tmp_path):
    from maestro.portfolio.account_attribution import AttributionPosition

    store = StateStore(str(tmp_path / "state.db"))
    store.save_account_attribution_snapshot(
        "attr_isa",
        [
            AttributionPosition(
                account_id="kis_isa",
                symbol="KODEX",
                bucket_id="tranquillo",
                quantity=5.0,
                version=1,
            ),
        ],
    )
    _set_created_at(store, "account_attribution_snapshots", "attr_isa", "2026-07-01 00:00:00")
    store.save_broker_account_snapshot(
        "broker_isa",
        "kis_isa",
        {
            "account_id": "kis_isa",
            "account": {
                "account_id": "kis_isa",
                "currency": "KRW",
                "cash": 0.0,
                "positions": [{"symbol": "KODEX", "quantity": 5, "current_price": 10_000.0}],
            },
        },
    )
    _set_created_at(store, "broker_account_snapshots", "broker_isa", "2026-07-01 00:00:01")

    group_target = type(
        "Target", (), {"account_id": "kis_isa", "execution_sleeve": "tranquillo"}
    )()
    group = type("Group", (), {"account_targets": [group_target]})()
    strategy = _actual_perf_strategy(
        "tranquillo", "multi_account_contributions.tranquillo", None
    )
    config = type(
        "Config",
        (),
        {
            "strategies": [strategy],
            "multi_account_contribution_group_for_strategy": staticmethod(
                lambda sid: group if sid == "tranquillo" else None
            ),
        },
    )()

    rows = build_strategy_actual_performance_table(store, config)

    assert [
        (row["strategy_id"], row["book_value"], row["positions"]) for row in rows
    ] == [("tranquillo", 50_000.0, {"KODEX": 5.0})]

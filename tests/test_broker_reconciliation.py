from pathlib import Path

import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.core.ids import new_run_id
from maestro.execution.reconciliation import BrokerReconciliationService
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


def test_reconciliation_exact_match_passes(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    _save_portfolio_and_broker(
        store,
        portfolio=PortfolioState(cash=1000.0, positions={"005930": 2.0}),
        broker_cash=1000.0,
        broker_positions={"005930": 2.0},
    )

    result = BrokerReconciliationService(config.reconciliation, store, audit).reconcile_latest()

    assert result.passed is True
    assert result.issues == []
    assert store.list_system_events()[0]["payload"]["passed"] is True


def test_reconciliation_refreshes_broker_snapshot_before_compare(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    store.save_portfolio_snapshot(
        new_run_id(),
        PortfolioState(cash=1000.0, positions={"005930": 2.0}),
    )
    _save_broker_snapshot(store, broker_cash=1000.0, broker_positions={"005930": 1.0})

    def refresh_snapshot() -> None:
        _save_broker_snapshot(store, broker_cash=1000.0, broker_positions={"005930": 2.0})

    result = BrokerReconciliationService(
        config.reconciliation,
        store,
        audit,
        snapshot_refresher=refresh_snapshot,
    ).reconcile_latest()

    assert result.passed is True
    assert result.position_differences == {"005930": 0.0}


def test_reconciliation_cash_mismatch_fails(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    _save_portfolio_and_broker(
        store,
        portfolio=PortfolioState(cash=1000.0, positions={"005930": 2.0}),
        broker_cash=900.0,
        broker_positions={"005930": 2.0},
    )

    result = BrokerReconciliationService(config.reconciliation, store, audit).reconcile_latest()

    assert result.passed is False
    assert [issue.issue_type for issue in result.issues] == ["cash_mismatch"]
    assert result.cash_difference == -100.0


def test_reconciliation_position_quantity_mismatch_fails(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    _save_portfolio_and_broker(
        store,
        portfolio=PortfolioState(cash=1000.0, positions={"005930": 2.0}),
        broker_cash=1000.0,
        broker_positions={"005930": 3.0},
    )

    result = BrokerReconciliationService(config.reconciliation, store, audit).reconcile_latest()

    assert result.passed is False
    assert [issue.issue_type for issue in result.issues] == ["position_quantity_mismatch"]
    assert result.position_differences == {"005930": 1.0}


def test_reconciliation_unknown_broker_position_fails(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    _save_portfolio_and_broker(
        store,
        portfolio=PortfolioState(cash=1000.0, positions={"005930": 2.0}),
        broker_cash=1000.0,
        broker_positions={"005930": 2.0, "000660": 1.0},
    )

    result = BrokerReconciliationService(config.reconciliation, store, audit).reconcile_latest()

    assert result.passed is False
    assert [issue.issue_type for issue in result.issues] == ["unknown_broker_position"]
    assert result.issues[0].symbol == "000660"


def test_reconciliation_missing_broker_position_fails(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    _save_portfolio_and_broker(
        store,
        portfolio=PortfolioState(cash=1000.0, positions={"005930": 2.0, "000660": 1.0}),
        broker_cash=1000.0,
        broker_positions={"005930": 2.0},
    )

    result = BrokerReconciliationService(config.reconciliation, store, audit).reconcile_latest()

    assert result.passed is False
    assert [issue.issue_type for issue in result.issues] == ["missing_broker_position"]
    assert result.issues[0].symbol == "000660"


def test_reconciliation_no_broker_snapshot_fails_and_persists(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    store.save_portfolio_snapshot(
        new_run_id(),
        PortfolioState(cash=1000.0, positions={"005930": 2.0}),
    )

    result = BrokerReconciliationService(config.reconciliation, store, audit).reconcile_latest()

    latest_event = store.list_system_events()[0]
    assert result.passed is False
    assert [issue.issue_type for issue in result.issues] == ["no_broker_snapshot"]
    assert latest_event["event_type"] == "broker_reconciliation"
    assert latest_event["payload"]["issues"][0]["issue_type"] == "no_broker_snapshot"


def test_reconcile_cli_outputs_passed_result(tmp_path):
    config, store, _ = _reconciliation_context(tmp_path)
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    _save_portfolio_and_broker(
        store,
        portfolio=PortfolioState(cash=1000.0, positions={"005930": 2.0}),
        broker_cash=1000.0,
        broker_positions={"005930": 2.0},
    )

    result = CliRunner().invoke(app, ["reconcile", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "status=passed" in result.output
    assert "issues=0" in result.output


def _reconciliation_context(tmp_path):
    raw = yaml.safe_load(Path("configs/live_readonly.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["portfolio"]["allowed_symbols"] = ["CASH", "005930", "000660"]
    raw["reconciliation"] = {
        "cash_tolerance": 0.0,
        "position_quantity_tolerance": 0.0,
        "value_tolerance": 0.0,
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    return config, store, audit


def _save_portfolio_and_broker(
    store: StateStore,
    *,
    portfolio: PortfolioState,
    broker_cash: float,
    broker_positions: dict[str, float],
) -> None:
    store.save_portfolio_snapshot(new_run_id(), portfolio)
    account_id = "TEST-ACCOUNT"
    store.save_broker_account_snapshot(
        new_run_id(),
        account_id,
        {
            "account": {
                "account_id": account_id,
                "cash": broker_cash,
                "buying_power": broker_cash,
                "positions": [
                    {
                        "symbol": symbol,
                        "quantity": quantity,
                        "average_price": 1.0,
                        "current_price": 1.0,
                    }
                    for symbol, quantity in broker_positions.items()
                ],
                "fetched_at": "2026-05-07T00:00:00+00:00",
                "source": "test",
            },
            "current_prices": {},
            "order_fills": [],
            "unfilled_orders": [],
        },
    )


def _save_broker_snapshot(
    store: StateStore,
    *,
    broker_cash: float,
    broker_positions: dict[str, float],
) -> None:
    account_id = "TEST-ACCOUNT"
    store.save_broker_account_snapshot(
        new_run_id(),
        account_id,
        {
            "account": {
                "account_id": account_id,
                "cash": broker_cash,
                "buying_power": broker_cash,
                "positions": [
                    {
                        "symbol": symbol,
                        "quantity": quantity,
                        "average_price": 1.0,
                        "current_price": 1.0,
                    }
                    for symbol, quantity in broker_positions.items()
                ],
                "fetched_at": "2026-05-07T00:00:00+00:00",
                "source": "test",
            },
            "current_prices": {},
            "order_fills": [],
            "unfilled_orders": [],
        },
    )

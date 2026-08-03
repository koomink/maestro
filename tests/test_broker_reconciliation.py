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


def test_reconciliation_can_persist_with_supplied_run_id(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    _save_portfolio_and_broker(
        store,
        portfolio=PortfolioState(cash=1000.0, positions={"005930": 2.0}),
        broker_cash=1000.0,
        broker_positions={"005930": 2.0},
    )

    result = BrokerReconciliationService(config.reconciliation, store, audit).reconcile_latest(
        run_id="run_parent"
    )

    assert result.run_id == "run_parent"
    latest_event = store.load_latest_system_event("broker_reconciliation")
    assert latest_event is not None
    assert latest_event["run_id"] == "run_parent"


def test_reconciliation_persists_signal_run_id(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    _save_portfolio_and_broker(
        store,
        portfolio=PortfolioState(cash=1000.0, positions={"005930": 2.0}),
        broker_cash=1000.0,
        broker_positions={"005930": 2.0},
    )

    result = BrokerReconciliationService(
        config.reconciliation,
        store,
        audit,
        signal_run_id="signal_abc",
    ).reconcile_latest(run_id="run_parent")

    assert result.run_id == "run_parent"
    latest_event = store.load_latest_system_event("broker_reconciliation")
    assert latest_event["payload"]["signal_run_id"] == "signal_abc"


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


def test_reconciliation_matches_account_scoped_portfolio_and_broker_snapshots(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    store.save_portfolio_snapshot(
        "run_global_stale",
        PortfolioState(cash=999.0, positions={"000660": 9.0}),
    )
    store.save_portfolio_snapshot(
        "run_mock_state",
        PortfolioState(cash=1000.0, positions={"005930": 2.0}),
        account_id="kis_mock",
    )
    store.save_portfolio_snapshot(
        "run_ps_state",
        PortfolioState(cash=0.0, positions={}),
        account_id="kis_ps",
    )
    _save_broker_snapshot(
        store,
        account_id="kis_mock",
        broker_cash=1000.0,
        broker_positions={"005930": 2.0},
    )
    _save_broker_snapshot(
        store,
        account_id="kis_ps",
        broker_cash=0.0,
        broker_positions={},
    )

    result = BrokerReconciliationService(
        config.reconciliation,
        store,
        audit,
        account_ids=["kis_mock", "kis_ps"],
    ).reconcile_latest()

    assert result.passed is True
    assert result.cash_difference == 0.0
    assert result.position_differences == {"005930": 0.0}
    assert {item["account_id"] for item in result.account_results} == {"kis_mock", "kis_ps"}


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


def test_toss_buying_power_drift_is_observational(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    store.save_portfolio_snapshot(
        new_run_id(),
        PortfolioState(cash=1000.0, cash_by_currency={"USD": 1000.0}, positions={}),
        account_id="toss_brokerage",
    )
    store.save_broker_account_snapshot(
        new_run_id(),
        "toss_brokerage",
        {
            "account": {
                "account_id": "toss-1",
                "cash": 1002.0,
                "cash_by_currency": {"USD": 1002.0},
                "ledger_cash_by_currency": None,
                "buying_power_by_currency": {"USD": 1002.0},
                "buying_power": 1002.0,
                "positions": [],
                "fetched_at": "2026-05-07T00:00:00+00:00",
                "source": "toss_openapi_readonly",
            },
            "current_prices": {},
            "order_fills": [],
            "unfilled_orders": [],
        },
    )

    result = BrokerReconciliationService(
        config.reconciliation,
        store,
        audit,
        account_ids=["toss_brokerage"],
    ).reconcile_latest()

    assert result.passed is True
    assert result.issues == []
    assert result.observations[0].issue_type == "buying_power_drift"
    assert result.observations[0].difference == 2.0
    suspense = store.list_cash_suspense(account_id="toss_brokerage")
    assert suspense[0]["amount"] == 2.0
    assert suspense[0]["status"] == "open"
    assert result.account_results[0]["observations"][0]["issue_type"] == (
        "buying_power_drift"
    )


def test_known_1343_krw_drift_replays_as_l1_without_blocking(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    store.save_portfolio_snapshot(
        new_run_id(),
        PortfolioState(
            cash=10_000_000.0,
            cash_by_currency={"KRW": 10_000_000.0},
            positions={"QQQ": 1.0},
        ),
        account_id="toss_brokerage",
    )
    store.save_broker_account_snapshot(
        new_run_id(),
        "toss_brokerage",
        {
            "account": {
                "account_id": "toss-1",
                "cash": 10_001_343.0,
                "cash_by_currency": {"KRW": 10_001_343.0},
                "ledger_cash_by_currency": None,
                "buying_power_by_currency": {"KRW": 10_001_343.0},
                "buying_power": 10_001_343.0,
                "positions": [
                    {
                        "symbol": "QQQ",
                        "quantity": 1.0,
                        "average_price": 1_000_000.0,
                        "current_price": 1_000_000.0,
                    }
                ],
                "fetched_at": "2026-07-02T00:00:00+00:00",
                "source": "toss_openapi_readonly",
            },
            "current_prices": {"QQQ": 1_000_000.0},
            "order_fills": [
                {
                    "order_id": "recent",
                    "symbol": "QQQ",
                    "side": "buy",
                    "quantity": 1.0,
                    "filled_quantity": 1.0,
                    "average_fill_price": 1_000_000.0,
                    "status": "FILLED",
                    "submitted_at": "2026-07-01T00:00:00+00:00",
                }
            ],
            "unfilled_orders": [],
        },
    )

    result = BrokerReconciliationService(
        config.reconciliation,
        store,
        audit,
        account_ids=["toss_brokerage"],
    ).reconcile_latest()

    assert result.passed is True
    assert result.observations[0].difference == 1_343.0
    assert result.observations[0].drift_level == "L1"


def test_toss_reconciliation_requires_coupled_order_history_evidence(tmp_path):
    config, store, audit = _reconciliation_context(tmp_path)
    store.save_portfolio_snapshot(
        "ledger",
        PortfolioState(cash=100.0, cash_by_currency={"USD": 100.0}, positions={}),
        account_id="toss_brokerage",
    )
    store.save_broker_account_snapshot(
        "snapshot-run",
        "toss_brokerage",
        {
            "account_id": "toss_brokerage",
            "order_history_backfill_run_id": "snapshot-run",
            "account": {
                "account_id": "toss-1",
                "cash": 100.0,
                "cash_by_currency": {"USD": 100.0},
                "ledger_cash_by_currency": None,
                "buying_power_by_currency": {"USD": 100.0},
                "buying_power": 100.0,
                "positions": [],
                "fetched_at": "2026-07-02T00:00:00+00:00",
                "source": "toss_openapi_readonly",
            },
            "current_prices": {},
            "order_fills": [],
            "unfilled_orders": [],
        },
    )

    result = BrokerReconciliationService(
        config.reconciliation,
        store,
        audit,
        account_ids=["toss_brokerage"],
    ).reconcile_latest()

    assert result.passed is False
    assert result.issues[0].issue_type == "order_history_unverified"


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
    raw = config.model_dump(mode="json")
    raw["portfolio"]["allowed_symbols"] = ["CASH", "MOCK_ETF_A", "MOCK_ETF_B"]
    config_path.write_text(yaml.safe_dump(raw))
    store.save_portfolio_snapshot(
        new_run_id(),
        PortfolioState(
            cash=5_000_000.0,
            cash_by_currency={"KRW": 5_000_000.0},
            positions={"MOCK_ETF_A": 30_000.0, "MOCK_ETF_B": 40_000.0},
        ),
    )

    result = CliRunner().invoke(app, ["reconcile", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "status=passed" in result.output
    assert "issues=0" in result.output


def _reconciliation_context(tmp_path):
    raw = yaml.safe_load(Path("tests/fixtures/configs/live_readonly_mock.yaml").read_text())
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
    account_id: str = "TEST-ACCOUNT",
    broker_cash: float,
    broker_positions: dict[str, float],
) -> None:
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


def test_cash_suspense_classifications_carry_an_accounting_meaning():
    """Each label an operator can pick must say what it does to the return.

    Without the mapping, "dividend" and "transfer_candidate" are equally
    plausible words that would be neutralised out of performance identically.
    """
    from maestro.state.events import (
        CASH_SUSPENSE_CLASSIFICATIONS,
        flow_class_for_cash_suspense,
    )

    assert flow_class_for_cash_suspense("transfer_candidate") == "external_transfer"
    assert flow_class_for_cash_suspense("dividend") == "investment_income"
    assert flow_class_for_cash_suspense("interest") == "investment_income"
    assert flow_class_for_cash_suspense("tax") == "cost"
    assert flow_class_for_cash_suspense("fee") == "cost"
    assert flow_class_for_cash_suspense("fx_conversion") == "fx_conversion"
    # Timing and ignorance are not cash flows and must not imply one.
    assert flow_class_for_cash_suspense("settlement_candidate") is None
    assert flow_class_for_cash_suspense("unexplained") is None
    assert flow_class_for_cash_suspense("not_a_label") is None
    assert set(CASH_SUSPENSE_CLASSIFICATIONS) >= {"dividend", "tax", "fx_conversion"}


def test_cause_classifications_survive_a_suspense_round_trip(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.upsert_cash_suspense(
        account_id="kis_brokerage",
        currency="KRW",
        amount=50_000.0,
        snapshot_id=7,
        observed_at="2026-01-01T00:00:00+00:00",
    )

    assert store.classify_cash_suspense(
        account_id="kis_brokerage",
        currency="KRW",
        classification="dividend",
    )

    row = store.list_cash_suspense(account_id="kis_brokerage")[0]
    assert row["candidate_label"] == "dividend"
    # Adopting broker cash is gated on a non-unexplained label, so a cause
    # classification has to clear that gate the way transfer_candidate does.
    assert row["status"] == "classified"
    assert row["candidate_label"] != "unexplained"

import argparse
import csv
import io
import json
from pathlib import Path

from maestro.config.loader import load_config
from maestro.dashboard.read_models import (
    build_approvals_table,
    build_broker_account_summary,
    build_broker_position_exposure_table,
    build_broker_snapshot_history_table,
    build_broker_snapshots_table,
    build_daily_live_order_usage,
    build_fill_reconciliation_table,
    build_health_summary,
    build_latest_broker_snapshot_card,
    build_latest_reconciliation_card,
    build_live_order_events_table,
    build_maestro_state_exposure_table,
    build_orders_table,
    build_overview,
    build_portfolio_snapshot_history_table,
    build_portfolio_table,
    build_recent_halt_failure_events_table,
    build_risk_decisions_table,
    build_safety_state_card,
    build_strategy_runs_table,
    build_system_events_table,
)
from maestro.state.store import StateStore


def render(config_path: str | Path) -> None:
    try:
        import streamlit as st
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Streamlit is required for the dashboard. Install with `uv sync --extra dashboard`."
        ) from exc

    config = load_config(config_path)
    store = StateStore(
        config.state.sqlite_path,
        config.portfolio.initial_cash,
        config.portfolio.cash_by_currency,
    )
    overview = build_overview(store)
    status = store.status()

    st.set_page_config(page_title="Maestro Dashboard", layout="wide")
    st.title("Maestro")
    st.caption(
        "Read-only portfolio OS dashboard for stock/ETF and KIS domestic/overseas workflows"
    )

    action_cols = st.columns([1, 5])
    if action_cols[0].button("Refresh", type="primary"):
        st.rerun()
    action_cols[1].caption("Local refresh and CSV downloads only; no broker calls or writes.")

    safety = build_safety_state_card(store)
    health = build_health_summary(config, store)
    broker_snapshot = build_latest_broker_snapshot_card(store)
    broker_summary = build_broker_account_summary(store)
    reconciliation = build_latest_reconciliation_card(store)
    daily_usage = build_daily_live_order_usage(config, store)
    strategy_runs = build_strategy_runs_table(store)
    orders = build_orders_table(store)
    approvals = build_approvals_table(store)
    risk_decisions = build_risk_decisions_table(store)
    broker_snapshots = build_broker_snapshots_table(store)
    halt_failure_events = build_recent_halt_failure_events_table(store)
    live_order_events = build_live_order_events_table(store)
    fill_reconciliation = build_fill_reconciliation_table(store)
    system_events = build_system_events_table(store)
    portfolio_table = build_portfolio_table(store)
    maestro_exposure = build_maestro_state_exposure_table(store)
    broker_positions = build_broker_position_exposure_table(store)
    portfolio_history = build_portfolio_snapshot_history_table(store)
    broker_history = build_broker_snapshot_history_table(store)

    metric_cols = st.columns(7)
    metric_cols[0].metric("Broker Total Value", _money(broker_summary["total_value"]))
    metric_cols[1].metric("Broker Cash", _money(broker_summary["cash"]))
    metric_cols[2].metric("Broker Exposure", _percent(broker_summary["exposure_weight"]))
    metric_cols[3].metric("Broker PnL", _money(broker_summary["unrealized_pnl"]))
    metric_cols[4].metric("Maestro Cash", f"{overview['cash']:,.2f}")
    metric_cols[5].metric("Maestro Positions", overview["positions_count"])
    metric_cols[6].metric("Reconciliation", _reconciliation_label(reconciliation["passed"]))

    tabs = st.tabs(["Portfolio", "Operations", "Orders", "Events", "Raw"])

    with tabs[0]:
        st.subheader("Account / Portfolio")
        account_cols = st.columns(2)
        with account_cols[0]:
            st.subheader("Latest Broker Account")
            st.json(broker_summary)
        with account_cols[1]:
            st.subheader("Latest Broker / Reconciliation")
            st.json(
                {
                    "broker_snapshot": broker_snapshot,
                    "reconciliation": reconciliation,
                }
            )

        _table(st, "Broker Position Exposure", broker_positions, "broker_position_exposure")
        _table(st, "Maestro State Exposure", maestro_exposure, "maestro_state_exposure")
        _table(st, "Portfolio", portfolio_table, "portfolio")
        history_cols = st.columns(2)
        with history_cols[0]:
            _table(
                st,
                "Maestro Snapshot History",
                portfolio_history,
                "portfolio_snapshot_history",
            )
        with history_cols[1]:
            _table(st, "Broker Snapshot History", broker_history, "broker_snapshot_history")

    with tabs[1]:
        st.subheader("Operational Summary")
        summary_cols = st.columns(5)
        summary_cols[0].metric("Safety State", str(safety["state"]).upper())
        summary_cols[1].metric("Health", str(health["status"]).upper())
        summary_cols[2].metric("Daily Live Orders", daily_usage["order_count"])
        summary_cols[3].metric("Daily Notional", _money(daily_usage["notional"]))
        summary_cols[4].metric("Risk Decisions", overview["risk_decisions_count"])
        _table(st, "Health Checks", health["checks"], "health_checks")
        _table(st, "Recent Risk Decisions", risk_decisions, "risk_decisions")
        _table(st, "Recent Halt / Failure Events", halt_failure_events, "halt_failure_events")
        with st.expander("Safety / Usage Payload"):
            st.json({"daily_live_usage": daily_usage, "safety": safety})

    with tabs[2]:
        st.subheader("Strategy Signals / Results")
        strategy_signal_columns = [
            "created_at",
            "run_id",
            "strategy_id",
            "signal_action",
            "signal_symbol",
            "rating",
            "confidence",
            "allocations",
            "risk_flags",
            "validation_ok",
            "validation_errors",
        ]
        strategy_signal_rows = [
            {column: row.get(column) for column in strategy_signal_columns}
            for row in strategy_runs
        ]
        _table(st, "Strategy Signals / Results", strategy_signal_rows, "strategy_runs")
        with st.expander("Strategy Run Payloads"):
            st.json([row.get("payload", {}) for row in strategy_runs])
        _table(st, "Recent Paper Orders", orders, "orders")
        _table(st, "Recent Approvals", approvals, "approvals")

    with tabs[3]:
        _table(st, "Recent Broker Account Snapshots", broker_snapshots, "broker_snapshots")
        _table(st, "Live Order Status / Lifecycle Events", live_order_events, "live_order_events")
        _table(
            st,
            "Fill Reconciliation Events",
            fill_reconciliation,
            "fill_reconciliation",
        )
        _table(st, "Recent System Events", system_events, "system_events")

    with tabs[4]:
        st.subheader("Raw System Status")
        st.json(status)


def _table(st: object, title: str, rows: list[dict[str, object]], key: str) -> None:
    st.subheader(title)
    st.dataframe(rows, width="stretch")
    st.download_button(
        "Download CSV",
        data=_rows_to_csv(rows),
        file_name=f"{key}.csv",
        mime="text/csv",
        key=f"download_{key}",
        disabled=not rows,
    )


def _rows_to_csv(rows: list[dict[str, object]]) -> str:
    if not rows:
        return ""
    fieldnames = sorted({key for row in rows for key in row})
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _csv_value(row.get(key)) for key in fieldnames})
    return output.getvalue()


def _csv_value(value: object) -> object:
    if isinstance(value, dict | list):
        return json.dumps(value, default=str, sort_keys=True)
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/paper.yaml")
    args = parser.parse_args()
    render(args.config)


def _money(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):,.2f}"


def _percent(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2%}"


def _reconciliation_label(value: object) -> str:
    if value is True:
        return "PASSED"
    if value is False:
        return "FAILED"
    return "MISSING"


if __name__ == "__main__":
    main()

import argparse
from pathlib import Path

from maestro.config.loader import load_config
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
from maestro.state.store import StateStore


def render(config_path: str | Path) -> None:
    try:
        import streamlit as st
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Streamlit is required for the dashboard. Install with `uv sync --extra dashboard`."
        ) from exc

    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    overview = build_overview(store)
    status = store.status()

    st.set_page_config(page_title="Maestro Dashboard", layout="wide")
    st.title("Maestro")
    st.caption("Read-only portfolio OS dashboard for US stock/ETF and KIS overseas workflows")

    metric_cols = st.columns(7)
    metric_cols[0].metric("Cash", f"{overview['cash']:,.2f}")
    metric_cols[1].metric("Positions", overview["positions_count"])
    metric_cols[2].metric("Strategy Runs", overview["strategy_runs_count"])
    metric_cols[3].metric("Orders", overview["orders_count"])
    metric_cols[4].metric("Approvals", overview["approvals_count"])
    metric_cols[5].metric("Risk Decisions", overview["risk_decisions_count"])
    metric_cols[6].metric("Broker Snapshots", overview["broker_snapshots_count"])

    safety = build_safety_state_card(store)
    health = build_health_summary(config, store)
    broker_snapshot = build_latest_broker_snapshot_card(store)
    reconciliation = build_latest_reconciliation_card(store)
    daily_usage = build_daily_live_order_usage(config, store)

    st.subheader("Operational Summary")
    summary_cols = st.columns(5)
    summary_cols[0].metric("Safety State", str(safety["state"]).upper())
    summary_cols[1].metric("Health", str(health["status"]).upper())
    summary_cols[2].metric("Broker Cash", _money(broker_snapshot["cash"]))
    summary_cols[3].metric("Reconciliation", _reconciliation_label(reconciliation["passed"]))
    summary_cols[4].metric("Daily Live Orders", daily_usage["order_count"])

    detail_cols = st.columns(2)
    with detail_cols[0]:
        st.subheader("Health Checks")
        st.dataframe(health["checks"], width="stretch")
    with detail_cols[1]:
        st.subheader("Latest Broker / Reconciliation")
        st.json(
            {
                "broker_snapshot": broker_snapshot,
                "reconciliation": reconciliation,
                "daily_live_usage": daily_usage,
                "safety": safety,
            }
        )

    st.subheader("Portfolio")
    st.dataframe(build_portfolio_table(store), width="stretch")

    st.subheader("Strategy Signals / Results")
    strategy_runs = build_strategy_runs_table(store)
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
    st.dataframe(
        [{column: row.get(column) for column in strategy_signal_columns} for row in strategy_runs],
        width="stretch",
    )
    with st.expander("Strategy Run Payloads"):
        st.json([row.get("payload", {}) for row in strategy_runs])

    st.subheader("Recent Paper Orders")
    st.dataframe(build_orders_table(store), width="stretch")

    st.subheader("Recent Approvals")
    st.dataframe(build_approvals_table(store), width="stretch")

    st.subheader("Recent Risk Decisions")
    st.dataframe(build_risk_decisions_table(store), width="stretch")

    st.subheader("Recent Broker Account Snapshots")
    st.dataframe(build_broker_snapshots_table(store), width="stretch")

    st.subheader("Recent Halt / Failure Events")
    st.dataframe(build_recent_halt_failure_events_table(store), width="stretch")

    st.subheader("Live Order Status / Lifecycle Events")
    st.dataframe(build_live_order_events_table(store), width="stretch")

    st.subheader("Fill Reconciliation Events")
    st.dataframe(build_fill_reconciliation_table(store), width="stretch")

    st.subheader("Recent System Events")
    st.dataframe(build_system_events_table(store), width="stretch")

    with st.expander("Raw System Status"):
        st.json(status)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/paper.yaml")
    args = parser.parse_args()
    render(args.config)


def _money(value: object) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):,.2f}"


def _reconciliation_label(value: object) -> str:
    if value is True:
        return "PASSED"
    if value is False:
        return "FAILED"
    return "MISSING"


if __name__ == "__main__":
    main()

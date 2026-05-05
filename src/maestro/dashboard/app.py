import argparse
from pathlib import Path

from maestro.config.loader import load_config
from maestro.dashboard.read_models import (
    build_approvals_table,
    build_broker_snapshots_table,
    build_orders_table,
    build_overview,
    build_portfolio_table,
    build_risk_decisions_table,
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
    st.caption("Read-only portfolio operating dashboard")

    metric_cols = st.columns(7)
    metric_cols[0].metric("Cash", f"{overview['cash']:,.2f}")
    metric_cols[1].metric("Positions", overview["positions_count"])
    metric_cols[2].metric("Strategy Runs", overview["strategy_runs_count"])
    metric_cols[3].metric("Orders", overview["orders_count"])
    metric_cols[4].metric("Approvals", overview["approvals_count"])
    metric_cols[5].metric("Risk Decisions", overview["risk_decisions_count"])
    metric_cols[6].metric("Broker Snapshots", overview["broker_snapshots_count"])

    st.subheader("Portfolio")
    st.dataframe(build_portfolio_table(store), use_container_width=True)

    st.subheader("Recent Strategy Runs")
    st.dataframe(build_strategy_runs_table(store), use_container_width=True)

    st.subheader("Recent Paper Orders")
    st.dataframe(build_orders_table(store), use_container_width=True)

    st.subheader("Recent Approvals")
    st.dataframe(build_approvals_table(store), use_container_width=True)

    st.subheader("Recent Risk Decisions")
    st.dataframe(build_risk_decisions_table(store), use_container_width=True)

    st.subheader("Recent Broker Account Snapshots")
    st.dataframe(build_broker_snapshots_table(store), use_container_width=True)

    st.subheader("Recent System Events")
    st.dataframe(build_system_events_table(store), use_container_width=True)

    with st.expander("Raw System Status"):
        st.json(status)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/paper.yaml")
    args = parser.parse_args()
    render(args.config)


if __name__ == "__main__":
    main()

import argparse
from pathlib import Path

from maestro.config.loader import load_config
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
    status = store.status()
    state = store.load_latest_portfolio_state()

    st.set_page_config(page_title="Maestro Dashboard", layout="wide")
    st.title("Maestro")
    st.caption("Read-only portfolio operating dashboard")

    metric_cols = st.columns(4)
    metric_cols[0].metric("Cash", f"{state.cash:,.2f}")
    metric_cols[1].metric("Positions", len(state.positions))
    metric_cols[2].metric("Strategy Runs", status["counts"]["strategy_runs"])
    metric_cols[3].metric("Paper Orders", status["counts"]["orders"])

    st.subheader("Portfolio Overview")
    st.json(state.model_dump(mode="json"))

    st.subheader("Recent Strategy Runs")
    st.dataframe(store.list_strategy_runs(limit=20), use_container_width=True)

    st.subheader("Recent Paper Orders")
    st.dataframe(store.list_orders(limit=20), use_container_width=True)

    st.subheader("Recent Approvals")
    st.dataframe(store.list_approvals(limit=20), use_container_width=True)

    st.subheader("Recent Broker Account Snapshots")
    st.dataframe(store.list_broker_account_snapshots(limit=20), use_container_width=True)

    st.subheader("System Status")
    st.json(status)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/paper.yaml")
    args = parser.parse_args()
    render(args.config)


if __name__ == "__main__":
    main()

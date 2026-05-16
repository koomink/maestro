import argparse
import csv
import io
import json
from pathlib import Path

from maestro.config.loader import load_config
from maestro.dashboard.read_models import (
    build_account_performance_table,
    build_approvals_table,
    build_broker_position_exposure_table,
    build_broker_snapshot_history_table,
    build_broker_snapshots_table,
    build_currency_sleeve_performance_table,
    build_fill_reconciliation_table,
    build_freshness_table,
    build_fx_rate_snapshot_card,
    build_live_order_events_table,
    build_maestro_state_exposure_table,
    build_operator_home,
    build_operator_summary,
    build_orders_table,
    build_overview,
    build_portfolio_snapshot_history_table,
    build_portfolio_table,
    build_recent_halt_failure_events_table,
    build_risk_decisions_table,
    build_run_detail,
    build_run_index_table,
    build_strategy_attribution_table,
    build_strategy_book_performance_table,
    build_strategy_book_snapshots_table,
    build_strategy_runs_table,
    build_system_events_table,
    build_total_portfolio_performance_table,
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
    operator_summary = build_operator_summary(config, store)
    status = store.status()

    st.set_page_config(page_title="Maestro Dashboard", layout="wide")
    st.title("Maestro")
    st.caption("Read-only portfolio OS dashboard for stock/ETF and KIS domestic/overseas workflows")

    action_cols = st.columns([1, 5])
    if action_cols[0].button("Refresh", type="primary"):
        st.rerun()
    action_cols[1].caption("Local refresh and CSV downloads only; no broker calls or writes.")
    filters = _dashboard_filters(st)
    display_currency = st.sidebar.selectbox(
        "Display currency",
        ["KRW", "USD"],
        index=0,
        help="Reporting only; does not affect orders, risk, or reconciliation.",
    )

    def table(title: str, rows: list[dict[str, object]], key: str) -> None:
        _table(st, title, rows, key, filters)

    operator_home = build_operator_home(config, store)
    freshness = build_freshness_table(config, store)
    fx_snapshot = build_fx_rate_snapshot_card(store)
    safety = operator_summary["safety"]
    health = operator_summary["health"]
    broker_snapshot = operator_summary["broker_snapshot"]
    broker_summary = operator_summary["broker_summary"]
    reconciliation = operator_summary["reconciliation"]
    daily_usage = operator_summary["daily_live_usage"]
    live_order_lifecycle = operator_summary["live_order_lifecycle"]
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
    account_performance = build_account_performance_table(store)
    currency_sleeve_performance = build_currency_sleeve_performance_table(store)
    total_portfolio_performance = build_total_portfolio_performance_table(
        store,
        display_currency=display_currency,
    )
    strategy_book_snapshots = build_strategy_book_snapshots_table(store)
    strategy_book_performance = build_strategy_book_performance_table(store)
    strategy_attribution = build_strategy_attribution_table(store)
    run_index = build_run_index_table(store)

    metric_cols = st.columns(7)
    metric_cols[0].metric("Broker Total Value", _money(broker_summary["total_value"]))
    metric_cols[1].metric("Broker Cash", _money(broker_summary["cash"]))
    metric_cols[2].metric("Broker Exposure", _percent(broker_summary["exposure_weight"]))
    metric_cols[3].metric("Broker PnL", _money(broker_summary["unrealized_pnl"]))
    metric_cols[4].metric("Maestro Cash", f"{overview['cash']:,.2f}")
    metric_cols[5].metric("Maestro Positions", overview["positions_count"])
    metric_cols[6].metric("Reconciliation", _reconciliation_label(reconciliation["passed"]))

    tabs = st.tabs(
        ["Home", "Portfolio", "Performance", "Operations", "Orders", "Events", "Run Detail", "Raw"]
    )

    with tabs[0]:
        st.subheader("Operator Home")
        home_cols = st.columns(5)
        home_cols[0].metric("Overall", str(operator_home["status"]).upper())
        home_cols[1].metric("Mode", operator_home["mode"])
        home_cols[2].metric("Latest Run", operator_home["latest_run_id"] or "n/a")
        home_cols[3].metric("Attention", operator_home["attention_count"])
        home_cols[4].metric("Stale / Missing", operator_home["stale_count"])
        if operator_home["attention_items"]:
            st.error(f"{operator_home['attention_count']} attention item(s)")
            st.dataframe(operator_home["attention_items"], width="stretch")
        else:
            st.success("No attention items")
        table("Freshness", freshness, "freshness")
        table("Run Index", run_index, "run_index")

    with tabs[1]:
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

        table("Broker Position Exposure", broker_positions, "broker_position_exposure")
        table("Maestro State Exposure", maestro_exposure, "maestro_state_exposure")
        table("Portfolio", portfolio_table, "portfolio")
        history_cols = st.columns(2)
        with history_cols[0]:
            table(
                "Maestro Snapshot History",
                portfolio_history,
                "portfolio_snapshot_history",
            )
        with history_cols[1]:
            table("Broker Snapshot History", broker_history, "broker_snapshot_history")

    with tabs[2]:
        st.subheader("Account Performance")
        latest_performance = account_performance[0] if account_performance else {}
        performance_cols = st.columns(5)
        performance_cols[0].metric("Account Value", _money(latest_performance.get("total_value")))
        performance_cols[1].metric(
            "Period Return",
            _percent(latest_performance.get("period_return")),
        )
        performance_cols[2].metric(
            "Cumulative Return",
            _percent(latest_performance.get("cumulative_return")),
        )
        performance_cols[3].metric("Drawdown", _percent(latest_performance.get("drawdown")))
        performance_cols[4].metric(
            "Reconciliation",
            latest_performance.get("reconciliation_status") or "n/a",
        )
        if account_performance:
            chart_rows = list(reversed(account_performance))
            st.line_chart(chart_rows, x="created_at", y="total_value")
            return_cols = st.columns(2)
            with return_cols[0]:
                st.line_chart(chart_rows, x="created_at", y="cumulative_return")
            with return_cols[1]:
                st.line_chart(chart_rows, x="created_at", y="drawdown")
        table("Account Performance", account_performance, "account_performance")
        st.subheader("Currency Sleeve Performance")
        if currency_sleeve_performance:
            st.line_chart(
                list(reversed(currency_sleeve_performance)),
                x="created_at",
                y="cumulative_return",
                color="currency",
            )
        table(
            "Currency Sleeve Performance",
            currency_sleeve_performance,
            "currency_sleeve_performance",
        )
        st.subheader("Total Portfolio Performance")
        total_chart_rows = [
            row
            for row in reversed(total_portfolio_performance)
            if row.get("total_value") is not None
        ]
        if total_chart_rows:
            st.line_chart(total_chart_rows, x="created_at", y="total_value")
        if total_portfolio_performance and total_portfolio_performance[0].get("missing_fx"):
            st.warning("Total portfolio return needs an explicit FX source for mixed currencies.")
        st.caption(f"Display currency: {display_currency}; FX status: {fx_snapshot['status']}")
        table(
            "Total Portfolio Performance",
            total_portfolio_performance,
            "total_portfolio_performance",
        )
        st.subheader("Strategy Book Performance")
        if strategy_book_performance:
            st.line_chart(
                list(reversed(strategy_book_performance)),
                x="created_at",
                y="book_value",
                color="book_id",
            )
        table(
            "Strategy Book Performance",
            strategy_book_performance,
            "strategy_book_performance",
        )
        table(
            "Strategy Attribution",
            strategy_attribution,
            "strategy_attribution",
        )
        table(
            "Strategy Book Snapshots",
            strategy_book_snapshots,
            "strategy_book_snapshots",
        )

    with tabs[3]:
        st.subheader("Operational Summary")
        summary_cols = st.columns(5)
        summary_cols[0].metric("Safety State", str(safety["state"]).upper())
        summary_cols[1].metric("Health", str(health["status"]).upper())
        summary_cols[2].metric("Reconciliation", _reconciliation_label(reconciliation["passed"]))
        summary_cols[3].metric(
            "Broker Snapshot Age",
            _duration(operator_summary["broker_snapshot_age_seconds"]),
        )
        summary_cols[4].metric("Risk Decisions", overview["risk_decisions_count"])
        usage_cols = st.columns(2)
        usage_cols[0].metric(
            "Daily Live Orders",
            f"{daily_usage['order_count']} / {daily_usage['max_daily_live_order_count']}",
        )
        usage_cols[1].metric(
            "Daily Live Notional",
            f"{_money(daily_usage['notional'])} / {_money(daily_usage['max_daily_live_notional'])}",
        )
        attention_items = operator_summary["attention_items"]
        if attention_items:
            st.error(f"{len(attention_items)} attention item(s)")
            st.dataframe(
                [
                    {
                        "severity": item.get("severity"),
                        "code": item.get("code"),
                        "message": item.get("message"),
                    }
                    for item in attention_items
                ],
                width="stretch",
            )
        else:
            st.success("No attention items")
        lifecycle_cols = st.columns(3)
        latest_lifecycle = live_order_lifecycle["latest"] or {}
        lifecycle_cols[0].metric("Latest Live Order", latest_lifecycle.get("status") or "n/a")
        lifecycle_cols[1].metric(
            "Recent Live Order Issues",
            live_order_lifecycle["recent_issue_count"],
        )
        lifecycle_cols[2].metric(
            "Lifecycle Rows",
            len(live_order_lifecycle["recent"]),
        )
        table(
            "Live Order Lifecycle Summary",
            live_order_lifecycle["recent"],
            "live_order_lifecycle_summary",
        )
        table("Health Checks", health["checks"], "health_checks")
        table("Recent Risk Decisions", risk_decisions, "risk_decisions")
        table("Recent Halt / Failure Events", halt_failure_events, "halt_failure_events")
        with st.expander("Operator Summary Payload"):
            st.json(operator_summary)

    with tabs[4]:
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
            {column: row.get(column) for column in strategy_signal_columns} for row in strategy_runs
        ]
        table("Strategy Signals / Results", strategy_signal_rows, "strategy_runs")
        with st.expander("Strategy Run Payloads"):
            st.json([row.get("payload", {}) for row in strategy_runs])
        table("Recent Paper Orders", orders, "orders")
        table("Recent Approvals", approvals, "approvals")

    with tabs[5]:
        table("Recent Broker Account Snapshots", broker_snapshots, "broker_snapshots")
        table("Live Order Status / Lifecycle Events", live_order_events, "live_order_events")
        table(
            "Fill Reconciliation Events",
            fill_reconciliation,
            "fill_reconciliation",
        )
        table("Recent System Events", system_events, "system_events")

    with tabs[6]:
        st.subheader("Run Detail")
        run_ids = [row["run_id"] for row in run_index]
        selected_run_id = st.selectbox("Run", run_ids, index=0) if run_ids else None
        if selected_run_id:
            detail = build_run_detail(store, selected_run_id)
            st.json(detail["summary"])
            table("Run Timeline", detail["timeline"], "run_timeline")
            with st.expander("Run Payloads"):
                st.json(detail)
        else:
            st.info("No run data found.")

    with tabs[7]:
        st.subheader("Raw System Status")
        st.json(status)


def _dashboard_filters(st: object) -> dict[str, object]:
    st.sidebar.header("Filters")
    query = st.sidebar.text_input("Search tables")
    statuses = st.sidebar.multiselect(
        "Status",
        ["fresh", "stale", "missing", "failed", "ok", "warn", "fail", "approved", "rejected"],
    )
    return {"query": query, "statuses": statuses}


def _table(
    st: object,
    title: str,
    rows: list[dict[str, object]],
    key: str,
    filters: dict[str, object] | None = None,
) -> None:
    display_rows = _filter_rows(rows, filters or {})
    st.subheader(title)
    st.dataframe(display_rows, width="stretch")
    st.download_button(
        "Download CSV",
        data=_rows_to_csv(display_rows),
        file_name=f"{key}.csv",
        mime="text/csv",
        key=f"download_{key}",
        disabled=not display_rows,
    )


def _filter_rows(
    rows: list[dict[str, object]],
    filters: dict[str, object],
) -> list[dict[str, object]]:
    query = str(filters.get("query") or "").strip().lower()
    statuses = {str(status) for status in filters.get("statuses") or []}
    if not query and not statuses:
        return rows
    return [
        row for row in rows if _row_matches_query(row, query) and _row_matches_status(row, statuses)
    ]


def _row_matches_query(row: dict[str, object], query: str) -> bool:
    if not query:
        return True
    return query in json.dumps(row, default=str, sort_keys=True).lower()


def _row_matches_status(row: dict[str, object], statuses: set[str]) -> bool:
    if not statuses:
        return True
    candidates = {
        str(row.get(key))
        for key in (
            "status",
            "reconciliation_status",
            "state",
            "approved",
            "validation_ok",
            "passed",
            "fx_status",
        )
        if row.get(key) is not None
    }
    return bool(candidates & statuses)


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


def _duration(value: object) -> str:
    if value is None:
        return "n/a"
    seconds = int(float(value))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _reconciliation_label(value: object) -> str:
    if value is True:
        return "PASSED"
    if value is False:
        return "FAILED"
    return "MISSING"


if __name__ == "__main__":
    main()

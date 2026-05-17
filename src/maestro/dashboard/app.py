import argparse
import csv
import html
import io
import json
import os
from pathlib import Path

from maestro.config.loader import load_config_with_identity
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

CONFIG_ENV_VAR = "MAESTRO_CONFIG"


def render(config_path: str | Path | None) -> None:
    try:
        import streamlit as st
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Streamlit is required for the dashboard. Install with `uv sync --extra dashboard`."
        ) from exc

    resolved_config = _resolve_config(config_path)
    config, identity = load_config_with_identity(resolved_config)
    store = StateStore(
        config.state.sqlite_path,
        config.portfolio.initial_cash,
        config.portfolio.cash_by_currency,
        config_identity=identity,
    )
    overview = build_overview(store)
    operator_summary = build_operator_summary(config, store)
    status = store.status()

    st.set_page_config(page_title="Maestro Dashboard", layout="wide")
    _apply_design_theme(st)
    _page_header(st, config.mode.value, status.get("operator_config"))
    st.sidebar.caption(f"Config: {identity.path}")
    st.sidebar.caption(f"State: {Path(config.state.sqlite_path).expanduser().resolve()}")
    st.sidebar.caption(f"Audit: {Path(config.audit.jsonl_path).expanduser().resolve()}")

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

    _metric_strip(
        st,
        [
            ("Broker Total Value", _money(broker_summary["total_value"]), "neutral"),
            ("Broker Cash", _money(broker_summary["cash"]), "neutral"),
            ("Broker Exposure", _percent(broker_summary["exposure_weight"]), "neutral"),
            ("Broker PnL", _money(broker_summary["unrealized_pnl"]), "neutral"),
            ("Maestro Cash", f"{overview['cash']:,.2f}", "neutral"),
            ("Maestro Positions", overview["positions_count"], "neutral"),
            (
                "Reconciliation",
                _reconciliation_label(reconciliation["passed"]),
                _boolean_tone(reconciliation["passed"]),
            ),
        ],
    )

    tabs = st.tabs(
        ["Home", "Portfolio", "Performance", "Operations", "Orders", "Events", "Run Detail", "Raw"]
    )

    with tabs[0]:
        _section_header(
            st,
            "Operator Home",
            "Control state, data freshness, and recent runs from persisted Maestro state.",
        )
        _metric_strip(
            st,
            [
                ("Overall", str(operator_home["status"]).upper(), operator_home["status"]),
                ("Mode", operator_home["mode"], "neutral"),
                ("Latest Run", operator_home["latest_run_id"] or "n/a", "neutral"),
                (
                    "Attention",
                    operator_home["attention_count"],
                    _count_tone(operator_home["attention_count"]),
                ),
                (
                    "Stale / Missing",
                    operator_home["stale_count"],
                    _count_tone(operator_home["stale_count"]),
                ),
            ],
        )
        if operator_home["attention_items"]:
            _status_banner(
                st,
                "Attention required",
                f"{operator_home['attention_count']} item(s) need review.",
                "danger",
            )
            st.dataframe(operator_home["attention_items"], width="stretch")
        else:
            _status_banner(st, "No attention items", "Operator summary is clear.", "success")
        table("Freshness", freshness, "freshness")
        table("Run Index", run_index, "run_index")

    with tabs[1]:
        _section_header(
            st,
            "Account / Portfolio",
            "Broker truth beside Maestro state, with snapshot histories for review.",
        )
        account_cols = st.columns(2)
        with account_cols[0]:
            _section_header(st, "Latest Broker Account")
            st.json(broker_summary)
        with account_cols[1]:
            _section_header(st, "Latest Broker / Reconciliation")
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
        _section_header(
            st,
            "Account Performance",
            "Persisted broker snapshots rendered as read-only return and drawdown views.",
        )
        latest_performance = account_performance[0] if account_performance else {}
        _metric_strip(
            st,
            [
                ("Account Value", _money(latest_performance.get("total_value")), "neutral"),
                ("Period Return", _percent(latest_performance.get("period_return")), "neutral"),
                (
                    "Cumulative Return",
                    _percent(latest_performance.get("cumulative_return")),
                    "neutral",
                ),
                ("Drawdown", _percent(latest_performance.get("drawdown")), "neutral"),
                (
                    "Reconciliation",
                    latest_performance.get("reconciliation_status") or "n/a",
                    _status_tone(latest_performance.get("reconciliation_status")),
                ),
            ],
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
        _section_header(st, "Currency Sleeve Performance")
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
        _section_header(st, "Total Portfolio Performance")
        total_chart_rows = [
            row
            for row in reversed(total_portfolio_performance)
            if row.get("total_value") is not None
        ]
        if total_chart_rows:
            st.line_chart(total_chart_rows, x="created_at", y="total_value")
        if total_portfolio_performance and total_portfolio_performance[0].get("missing_fx"):
            _status_banner(
                st,
                "FX source required",
                "Total portfolio return needs an explicit FX source for mixed currencies.",
                "warning",
            )
        st.markdown(
            _badge_row(
                [
                    ("Display", display_currency, "neutral"),
                    ("FX", fx_snapshot["status"], _status_tone(fx_snapshot["status"])),
                ]
            ),
            unsafe_allow_html=True,
        )
        table(
            "Total Portfolio Performance",
            total_portfolio_performance,
            "total_portfolio_performance",
        )
        _section_header(st, "Strategy Book Performance")
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
        _section_header(
            st,
            "Operational Summary",
            "Safety, health, live-order usage, lifecycle state, and recent risk events.",
        )
        daily_notional_value = (
            f"{_money(daily_usage['notional'])} / {_money(daily_usage['max_daily_live_notional'])}"
        )
        _metric_strip(
            st,
            [
                ("Safety State", str(safety["state"]).upper(), _status_tone(safety["state"])),
                ("Health", str(health["status"]).upper(), _status_tone(health["status"])),
                (
                    "Reconciliation",
                    _reconciliation_label(reconciliation["passed"]),
                    _boolean_tone(reconciliation["passed"]),
                ),
                (
                    "Broker Snapshot Age",
                    _duration(operator_summary["broker_snapshot_age_seconds"]),
                    "neutral",
                ),
                ("Risk Decisions", overview["risk_decisions_count"], "neutral"),
                (
                    "Daily Live Orders",
                    f"{daily_usage['order_count']} / {daily_usage['max_daily_live_order_count']}",
                    _limit_tone(
                        daily_usage["order_count"],
                        daily_usage["max_daily_live_order_count"],
                    ),
                ),
                (
                    "Daily Live Notional",
                    daily_notional_value,
                    _limit_tone(daily_usage["notional"], daily_usage["max_daily_live_notional"]),
                ),
            ],
        )
        attention_items = operator_summary["attention_items"]
        if attention_items:
            _status_banner(st, "Attention required", f"{len(attention_items)} item(s)", "danger")
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
            _status_banner(st, "No attention items", "Operational summary is clear.", "success")
        latest_lifecycle = live_order_lifecycle["latest"] or {}
        _metric_strip(
            st,
            [
                (
                    "Latest Live Order",
                    latest_lifecycle.get("status") or "n/a",
                    _status_tone(latest_lifecycle.get("status")),
                ),
                (
                    "Recent Live Order Issues",
                    live_order_lifecycle["recent_issue_count"],
                    _count_tone(live_order_lifecycle["recent_issue_count"]),
                ),
                ("Lifecycle Rows", len(live_order_lifecycle["recent"]), "neutral"),
            ],
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
        _section_header(
            st,
            "Strategy Signals / Results",
            "Normalized proposals, validation state, generated orders, and approvals.",
        )
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
        _section_header(
            st,
            "Run Detail",
            "Trace strategy, risk, approval, order, and event rows by run identifier.",
        )
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


def _apply_design_theme(st: object) -> None:
    st.markdown(
        """
        <style>
        :root {
          --maestro-primary: #5e6ad2;
          --maestro-primary-hover: #828fff;
          --maestro-primary-focus: #5e69d1;
          --maestro-ink: #f7f8f8;
          --maestro-ink-muted: #d0d6e0;
          --maestro-ink-subtle: #8a8f98;
          --maestro-canvas: #010102;
          --maestro-surface-1: #0f1011;
          --maestro-surface-2: #141516;
          --maestro-surface-3: #18191a;
          --maestro-hairline: #23252a;
          --maestro-hairline-strong: #34343a;
          --maestro-success: #27a644;
          --maestro-danger: #d06262;
          --maestro-warning: #d0a85c;
        }
        .stApp {
          background: var(--maestro-canvas);
          color: var(--maestro-ink);
          font-family: "Inter", "SF Pro Display", -apple-system, BlinkMacSystemFont,
            "Segoe UI", sans-serif;
        }
        .block-container {
          max-width: 1280px;
          padding-top: 32px;
          padding-bottom: 64px;
        }
        h1, h2, h3, h4, h5, h6, p, label, span {
          letter-spacing: 0;
        }
        h1, h2, h3 {
          color: var(--maestro-ink);
          font-weight: 600;
        }
        .stTabs [data-baseweb="tab-list"] {
          gap: 4px;
          background: var(--maestro-surface-1);
          border: 1px solid var(--maestro-hairline);
          border-radius: 8px;
          padding: 4px;
        }
        .stTabs [data-baseweb="tab"] {
          border-radius: 6px;
          color: var(--maestro-ink-subtle);
          padding: 8px 12px;
        }
        .stTabs [aria-selected="true"] {
          background: var(--maestro-surface-3);
          color: var(--maestro-ink);
        }
        .stDataFrame, [data-testid="stDataFrame"] {
          border: 1px solid var(--maestro-hairline);
          border-radius: 8px;
          overflow: hidden;
          background: var(--maestro-surface-1);
        }
        [data-testid="stSidebar"] {
          background: #09090a;
          border-right: 1px solid var(--maestro-hairline);
        }
        .stButton > button, .stDownloadButton > button {
          background: var(--maestro-surface-1);
          color: var(--maestro-ink);
          border: 1px solid var(--maestro-hairline);
          border-radius: 8px;
          min-height: 40px;
        }
        .stButton > button[kind="primary"] {
          background: var(--maestro-primary);
          color: #ffffff;
          border-color: var(--maestro-primary);
        }
        .stButton > button:hover, .stDownloadButton > button:hover {
          border-color: var(--maestro-hairline-strong);
          color: var(--maestro-ink);
        }
        .stButton > button:focus, .stDownloadButton > button:focus {
          box-shadow: 0 0 0 2px rgba(94, 105, 209, 0.45);
          outline: none;
        }
        .maestro-header {
          border: 1px solid var(--maestro-hairline);
          background: var(--maestro-surface-1);
          border-radius: 8px;
          padding: 24px;
          margin-bottom: 16px;
        }
        .maestro-eyebrow {
          color: var(--maestro-primary-hover);
          font-size: 13px;
          font-weight: 500;
          margin-bottom: 8px;
        }
        .maestro-title {
          color: var(--maestro-ink);
          font-size: 40px;
          font-weight: 600;
          line-height: 1.15;
          margin: 0 0 8px 0;
        }
        .maestro-subtitle {
          color: var(--maestro-ink-muted);
          font-size: 15px;
          line-height: 1.5;
          margin: 0;
        }
        .maestro-section {
          margin: 24px 0 12px 0;
        }
        .maestro-section-title {
          color: var(--maestro-ink);
          font-size: 22px;
          font-weight: 600;
          margin: 0;
        }
        .maestro-section-copy {
          color: var(--maestro-ink-subtle);
          font-size: 13px;
          margin-top: 4px;
        }
        .maestro-metric-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 8px;
          margin: 12px 0 18px 0;
        }
        .maestro-metric {
          border: 1px solid var(--maestro-hairline);
          background: var(--maestro-surface-1);
          border-radius: 8px;
          padding: 14px;
          min-height: 86px;
        }
        .maestro-metric-label {
          color: var(--maestro-ink-subtle);
          font-size: 12px;
          line-height: 1.35;
          margin-bottom: 10px;
        }
        .maestro-metric-value {
          color: var(--maestro-ink);
          font-size: 20px;
          font-weight: 600;
          line-height: 1.2;
          word-break: break-word;
        }
        .maestro-tone-success { border-color: rgba(39, 166, 68, 0.45); }
        .maestro-tone-warning { border-color: rgba(208, 168, 92, 0.55); }
        .maestro-tone-danger { border-color: rgba(208, 98, 98, 0.65); }
        .maestro-tone-primary { border-color: rgba(94, 106, 210, 0.65); }
        .maestro-badge-row {
          display: flex;
          flex-wrap: wrap;
          gap: 8px;
          margin: 10px 0 14px 0;
        }
        .maestro-badge {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          background: var(--maestro-surface-2);
          color: var(--maestro-ink-muted);
          border: 1px solid var(--maestro-hairline);
          border-radius: 9999px;
          padding: 3px 9px;
          font-size: 12px;
          line-height: 1.4;
        }
        .maestro-badge strong {
          color: var(--maestro-ink);
          font-weight: 500;
        }
        .maestro-banner {
          border: 1px solid var(--maestro-hairline);
          background: var(--maestro-surface-1);
          border-radius: 8px;
          padding: 14px 16px;
          margin: 12px 0;
        }
        .maestro-banner-title {
          color: var(--maestro-ink);
          font-size: 14px;
          font-weight: 600;
          margin-bottom: 4px;
        }
        .maestro-banner-copy {
          color: var(--maestro-ink-subtle);
          font-size: 13px;
        }
        .maestro-table-title {
          display: flex;
          align-items: baseline;
          justify-content: space-between;
          gap: 12px;
          margin: 20px 0 8px 0;
        }
        .maestro-table-title strong {
          color: var(--maestro-ink);
          font-size: 16px;
          font-weight: 600;
        }
        .maestro-table-title span {
          color: var(--maestro-ink-tertiary, #62666d);
          font-size: 12px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _page_header(st: object, mode: str, operator_config: dict[str, str] | None) -> None:
    fingerprint = (operator_config or {}).get("fingerprint", "none")
    short_fingerprint = fingerprint[:12] if fingerprint != "none" else "none"
    st.markdown(
        f"""
        <div class="maestro-header">
          <div class="maestro-eyebrow">SYMPHONY / MAESTRO</div>
          <h1 class="maestro-title">Maestro Dashboard</h1>
          <p class="maestro-subtitle">
            Read-only portfolio OS visibility for stock/ETF and KIS
            domestic/overseas workflows.
          </p>
          {
            _badge_row(
                [
                    ("Mode", mode, "primary"),
                    ("Access", "read-only", "success"),
                    ("Config", short_fingerprint, "neutral"),
                ]
            )
        }
        </div>
        """,
        unsafe_allow_html=True,
    )


def _section_header(st: object, title: str, copy: str | None = None) -> None:
    copy_html = f'<div class="maestro-section-copy">{_escape(copy)}</div>' if copy else ""
    st.markdown(
        f"""
        <div class="maestro-section">
          <h2 class="maestro-section-title">{_escape(title)}</h2>
          {copy_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def _metric_strip(st: object, metrics: list[tuple[str, object, str]]) -> None:
    cards = []
    for label, value, tone in metrics:
        tone_class = _tone_class(tone)
        cards.append(
            f"""
            <div class="maestro-metric {tone_class}">
              <div class="maestro-metric-label">{_escape(label)}</div>
              <div class="maestro-metric-value">{_escape(value)}</div>
            </div>
            """
        )
    st.markdown(
        f'<div class="maestro-metric-grid">{"".join(cards)}</div>',
        unsafe_allow_html=True,
    )


def _status_banner(st: object, title: str, copy: str, tone: str) -> None:
    st.markdown(
        f"""
        <div class="maestro-banner {_tone_class(tone)}">
          <div class="maestro-banner-title">{_escape(title)}</div>
          <div class="maestro-banner-copy">{_escape(copy)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _table(
    st: object,
    title: str,
    rows: list[dict[str, object]],
    key: str,
    filters: dict[str, object] | None = None,
) -> None:
    display_rows = _filter_rows(rows, filters or {})
    st.markdown(
        f"""
        <div class="maestro-table-title">
          <strong>{_escape(title)}</strong>
          <span>{len(display_rows)} / {len(rows)} rows</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
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


def _badge_row(badges: list[tuple[str, object, str]]) -> str:
    return (
        '<div class="maestro-badge-row">'
        + "".join(
            f'<span class="maestro-badge {_tone_class(tone)}">'
            f"{_escape(label)} <strong>{_escape(value)}</strong></span>"
            for label, value, tone in badges
        )
        + "</div>"
    )


def _tone_class(tone: str | None) -> str:
    return {
        "success": "maestro-tone-success",
        "warning": "maestro-tone-warning",
        "danger": "maestro-tone-danger",
        "fail": "maestro-tone-danger",
        "error": "maestro-tone-danger",
        "primary": "maestro-tone-primary",
    }.get(str(tone or "neutral"), "")


def _status_tone(value: object) -> str:
    normalized = str(value or "").lower()
    if normalized in {"ok", "active", "fresh", "passed", "approved", "completed", "filled"}:
        return "success"
    if normalized in {"warn", "warning", "stale", "missing", "open", "partially_filled"}:
        return "warning"
    if normalized in {"fail", "failed", "halted", "killed", "rejected", "unknown"}:
        return "danger"
    return "neutral"


def _boolean_tone(value: object) -> str:
    if value is True:
        return "success"
    if value is False:
        return "danger"
    return "warning"


def _count_tone(value: object) -> str:
    return "warning" if float(value or 0) > 0 else "success"


def _limit_tone(value: object, limit: object) -> str:
    current = float(value or 0)
    maximum = float(limit or 0)
    if maximum <= 0:
        return "neutral"
    ratio = current / maximum
    if ratio >= 1:
        return "danger"
    if ratio >= 0.8:
        return "warning"
    return "success"


def _escape(value: object) -> str:
    return html.escape(str(value))


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
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    render(args.config)


def _resolve_config(config_path: str | Path | None) -> Path:
    if config_path:
        return Path(config_path)
    env_config = os.getenv(CONFIG_ENV_VAR)
    if env_config:
        return Path(env_config)
    raise ValueError(f"--config is required or set {CONFIG_ENV_VAR}")


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

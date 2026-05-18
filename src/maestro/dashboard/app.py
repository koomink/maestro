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
THEME_OPTIONS = ("System Default", "Dark", "Light")


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

    st.set_page_config(page_title="Symphony", layout="wide")
    theme = st.sidebar.selectbox(
        "Theme",
        THEME_OPTIONS,
        index=0,
        help="Display preference only; no broker calls or writes.",
    )
    _apply_design_theme(st, theme)
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
        [
            "Symphony Map",
            "Operator Cockpit",
            "Investment Console",
            "Virtuoso Apps",
            "Audit Trail",
            "Raw",
        ]
    )

    with tabs[0]:
        _render_symphony_map(
            st,
            config,
            operator_home,
            health,
            reconciliation,
            freshness,
            safety,
            broker_summary,
            broker_snapshot,
            strategy_runs,
            risk_decisions,
            orders,
            approvals,
            live_order_lifecycle,
            run_index,
            table,
        )

    with tabs[1]:
        _render_operator_cockpit(
            st,
            operator_home,
            freshness,
            safety,
            health,
            reconciliation,
            operator_summary,
            daily_usage,
            live_order_lifecycle,
            risk_decisions,
            halt_failure_events,
            run_index,
            table,
        )

    with tabs[2]:
        _render_investment_console(
            st,
            broker_summary,
            broker_snapshot,
            reconciliation,
            account_performance,
            currency_sleeve_performance,
            total_portfolio_performance,
            display_currency,
            fx_snapshot,
            strategy_book_performance,
            strategy_attribution,
            strategy_book_snapshots,
            broker_positions,
            maestro_exposure,
            portfolio_table,
            portfolio_history,
            broker_history,
            table,
        )

    with tabs[3]:
        _render_virtuoso_tab(
            st,
            config,
            strategy_runs,
            strategy_book_performance,
            strategy_attribution,
            strategy_book_snapshots,
            table,
        )

    with tabs[4]:
        _render_audit_trail(
            st,
            strategy_runs,
            orders,
            approvals,
            broker_snapshots,
            live_order_events,
            fill_reconciliation,
            system_events,
            run_index,
            store,
            table,
        )

    with tabs[5]:
        st.subheader("Raw System Status")
        st.json(status)


def _render_symphony_map(
    st: object,
    config: object,
    operator_home: dict[str, object],
    health: dict[str, object],
    reconciliation: dict[str, object],
    freshness: list[dict[str, object]],
    safety: dict[str, object],
    broker_summary: dict[str, object],
    broker_snapshot: dict[str, object],
    strategy_runs: list[dict[str, object]],
    risk_decisions: list[dict[str, object]],
    orders: list[dict[str, object]],
    approvals: list[dict[str, object]],
    live_order_lifecycle: dict[str, object],
    run_index: list[dict[str, object]],
    table: object,
) -> None:
    latest_strategy = strategy_runs[0] if strategy_runs else {}
    latest_risk = risk_decisions[0] if risk_decisions else {}
    latest_order = orders[0] if orders else {}
    latest_approval = approvals[0] if approvals else {}
    latest_lifecycle = live_order_lifecycle.get("latest") or {}
    freshest_status = _freshness_rollup(freshness)
    reconciliation_passed = reconciliation.get("passed")
    enabled_strategies = [
        strategy
        for strategy in getattr(config, "strategies", [])
        if getattr(strategy, "enabled", False)
    ]

    _section_header(
        st,
        "Symphony Map",
        "A live read-only map of proposal, decision, protection, execution, and recorded truth.",
    )
    _metric_strip(
        st,
        [
            ("Overall", str(operator_home["status"]).upper(), operator_home["status"]),
            ("Enabled Virtuoso Apps", len(enabled_strategies), "neutral"),
            ("Health", str(health["status"]).upper(), _status_tone(health["status"])),
            (
                "Reconciliation",
                _reconciliation_label(reconciliation_passed),
                _boolean_tone(reconciliation_passed),
            ),
            ("Broker Total Value", _money(broker_summary["total_value"]), "neutral"),
            (
                "Attention",
                operator_home["attention_count"],
                _count_tone(operator_home["attention_count"]),
            ),
        ],
    )
    _system_map(
        st,
        [
            _system_node(
                "Virtuoso",
                "Propose",
                f"{len(enabled_strategies)} enabled app(s)",
                latest_strategy.get("created_at") or "No recent proposal",
                _boolean_tone(bool(enabled_strategies)),
            ),
            _system_node(
                "Maestro",
                "Decide",
                _validation_label(latest_strategy.get("validation_ok")),
                latest_strategy.get("run_id") or "No run yet",
                _boolean_tone(latest_strategy.get("validation_ok")),
            ),
            _system_node(
                "Risk",
                "Protect",
                _approval_label(latest_risk.get("approved")),
                latest_risk.get("created_at") or "No recent risk decision",
                _boolean_tone(latest_risk.get("approved")),
            ),
            _system_node(
                "Execution",
                "Execute",
                latest_order.get("approval_status") or latest_approval.get("status") or "read-only",
                latest_lifecycle.get("status")
                or latest_order.get("created_at")
                or "No live lifecycle",
                _status_tone(
                    latest_lifecycle.get("status")
                    or latest_order.get("approval_status")
                    or latest_approval.get("status")
                    or "ok"
                ),
            ),
            _system_node(
                "State",
                "Record",
                f"{len(run_index)} indexed run(s)",
                broker_snapshot.get("created_at") or "No broker snapshot",
                freshest_status,
            ),
            _system_node(
                "Operator",
                "Observe",
                f"{operator_home['attention_count']} attention item(s)",
                "Approve through Telegram; administer through CLI/config",
                _count_tone(operator_home["attention_count"]),
            ),
        ],
    )
    if operator_home["attention_items"]:
        _status_banner(
            st,
            "Attention required",
            f"{operator_home['attention_count']} item(s) need operator review.",
            "danger",
        )
        st.dataframe(operator_home["attention_items"], width="stretch")
    else:
        _status_banner(st, "No attention items", "The observed system map is clear.", "success")
    table("Freshness", freshness, "symphony_freshness")
    table("Run Index", run_index, "symphony_run_index")


def _render_operator_cockpit(
    st: object,
    operator_home: dict[str, object],
    freshness: list[dict[str, object]],
    safety: dict[str, object],
    health: dict[str, object],
    reconciliation: dict[str, object],
    operator_summary: dict[str, object],
    daily_usage: dict[str, object],
    live_order_lifecycle: dict[str, object],
    risk_decisions: list[dict[str, object]],
    halt_failure_events: list[dict[str, object]],
    run_index: list[dict[str, object]],
    table: object,
) -> None:
    _section_header(
        st,
        "Operator Cockpit",
        "Operational trust, safety, freshness, and attention queue before the next cycle.",
    )
    daily_notional_value = (
        f"{_money(daily_usage['notional'])} / {_money(daily_usage['max_daily_live_notional'])}"
    )
    latest_lifecycle = live_order_lifecycle["latest"] or {}
    _metric_strip(
        st,
        [
            ("Overall", str(operator_home["status"]).upper(), operator_home["status"]),
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
            (
                "Live Order Issues",
                live_order_lifecycle["recent_issue_count"],
                _count_tone(live_order_lifecycle["recent_issue_count"]),
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
    _metric_strip(
        st,
        [
            (
                "Latest Live Order",
                latest_lifecycle.get("status") or "n/a",
                _status_tone(latest_lifecycle.get("status")),
            ),
            ("Lifecycle Rows", len(live_order_lifecycle["recent"]), "neutral"),
            ("Risk Decisions", len(risk_decisions), "neutral"),
            ("Indexed Runs", len(run_index), "neutral"),
        ],
    )
    table("Freshness", freshness, "operator_freshness")
    table("Health Checks", health["checks"], "operator_health_checks")
    table(
        "Live Order Lifecycle Summary",
        live_order_lifecycle["recent"],
        "operator_live_order_lifecycle_summary",
    )
    table("Recent Risk Decisions", risk_decisions, "operator_risk_decisions")
    table("Recent Halt / Failure Events", halt_failure_events, "operator_halt_failure_events")
    with st.expander("Operator Summary Payload"):
        st.json(operator_summary)


def _render_investment_console(
    st: object,
    broker_summary: dict[str, object],
    broker_snapshot: dict[str, object],
    reconciliation: dict[str, object],
    account_performance: list[dict[str, object]],
    currency_sleeve_performance: list[dict[str, object]],
    total_portfolio_performance: list[dict[str, object]],
    display_currency: str,
    fx_snapshot: dict[str, object],
    strategy_book_performance: list[dict[str, object]],
    strategy_attribution: list[dict[str, object]],
    strategy_book_snapshots: list[dict[str, object]],
    broker_positions: list[dict[str, object]],
    maestro_exposure: list[dict[str, object]],
    portfolio_table: list[dict[str, object]],
    portfolio_history: list[dict[str, object]],
    broker_history: list[dict[str, object]],
    table: object,
) -> None:
    _section_header(
        st,
        "Investment Console",
        "Capital, exposure, performance, currency sleeves, and strategy contribution.",
    )
    latest_performance = account_performance[0] if account_performance else {}
    _metric_strip(
        st,
        [
            ("Account Value", _money(latest_performance.get("total_value")), "neutral"),
            ("Broker Cash", _money(broker_summary["cash"]), "neutral"),
            ("Broker Exposure", _percent(broker_summary["exposure_weight"]), "neutral"),
            ("Period Return", _percent(latest_performance.get("period_return")), "neutral"),
            (
                "Cumulative Return",
                _percent(latest_performance.get("cumulative_return")),
                "neutral",
            ),
            ("Drawdown", _percent(latest_performance.get("drawdown")), "neutral"),
            (
                "Reconciliation",
                latest_performance.get("reconciliation_status")
                or _reconciliation_label(reconciliation["passed"]),
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

    table("Account Performance", account_performance, "investment_account_performance")
    _section_header(st, "Total Portfolio Performance")
    total_chart_rows = [
        row for row in reversed(total_portfolio_performance) if row.get("total_value") is not None
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
        "investment_total_portfolio_performance",
    )

    _section_header(st, "Currency Sleeves")
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
        "investment_currency_sleeve_performance",
    )

    _section_header(st, "Strategy Contribution")
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
        "investment_strategy_book_performance",
    )
    table("Strategy Attribution", strategy_attribution, "investment_strategy_attribution")
    table(
        "Strategy Book Snapshots",
        strategy_book_snapshots,
        "investment_strategy_book_snapshots",
    )

    _section_header(st, "Portfolio And Broker Truth")
    account_cols = st.columns(2)
    with account_cols[0]:
        _section_header(st, "Latest Broker Account")
        st.json(broker_summary)
    with account_cols[1]:
        _section_header(st, "Latest Broker / Reconciliation")
        st.json({"broker_snapshot": broker_snapshot, "reconciliation": reconciliation})
    table("Broker Position Exposure", broker_positions, "investment_broker_position_exposure")
    table("Maestro State Exposure", maestro_exposure, "investment_maestro_state_exposure")
    table("Portfolio", portfolio_table, "investment_portfolio")
    history_cols = st.columns(2)
    with history_cols[0]:
        table(
            "Maestro Snapshot History",
            portfolio_history,
            "investment_portfolio_snapshot_history",
        )
    with history_cols[1]:
        table("Broker Snapshot History", broker_history, "investment_broker_snapshot_history")


def _render_audit_trail(
    st: object,
    strategy_runs: list[dict[str, object]],
    orders: list[dict[str, object]],
    approvals: list[dict[str, object]],
    broker_snapshots: list[dict[str, object]],
    live_order_events: list[dict[str, object]],
    fill_reconciliation: list[dict[str, object]],
    system_events: list[dict[str, object]],
    run_index: list[dict[str, object]],
    store: StateStore,
    table: object,
) -> None:
    _section_header(
        st,
        "Audit Trail",
        "Run-level evidence, proposals, orders, approvals, events, and raw persisted payloads.",
    )
    _metric_strip(
        st,
        [
            ("Indexed Runs", len(run_index), "neutral"),
            ("Strategy Runs", len(strategy_runs), "neutral"),
            ("Orders", len(orders), "neutral"),
            ("Approvals", len(approvals), "neutral"),
            ("Broker Snapshots", len(broker_snapshots), "neutral"),
            ("System Events", len(system_events), "neutral"),
        ],
    )

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
    table("Strategy Signals / Results", strategy_signal_rows, "audit_strategy_runs")
    with st.expander("Strategy Run Payloads"):
        st.json([row.get("payload", {}) for row in strategy_runs])
    table("Recent Paper Orders", orders, "audit_orders")
    table("Recent Approvals", approvals, "audit_approvals")

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
        table("Run Timeline", detail["timeline"], "audit_run_timeline")
        with st.expander("Run Payloads"):
            st.json(detail)
    else:
        st.info("No run data found.")

    table("Recent Broker Account Snapshots", broker_snapshots, "audit_broker_snapshots")
    table("Live Order Status / Lifecycle Events", live_order_events, "audit_live_order_events")
    table("Fill Reconciliation Events", fill_reconciliation, "audit_fill_reconciliation")
    table("Recent System Events", system_events, "audit_system_events")


def _render_virtuoso_tab(
    st: object,
    config: object,
    strategy_runs: list[dict[str, object]],
    strategy_book_performance: list[dict[str, object]],
    strategy_attribution: list[dict[str, object]],
    strategy_book_snapshots: list[dict[str, object]],
    table: object,
) -> None:
    strategy_configs = {strategy.id: strategy for strategy in getattr(config, "strategies", [])}
    strategy_ids = _virtuoso_strategy_ids(
        list(strategy_configs),
        strategy_runs,
        strategy_book_performance,
        strategy_book_snapshots,
    )
    enabled_count = sum(1 for strategy in strategy_configs.values() if strategy.enabled)
    observed_count = len(
        {
            row.get("strategy_id")
            for row in [*strategy_runs, *strategy_book_performance, *strategy_book_snapshots]
            if row.get("strategy_id")
        }
    )
    latest_run = strategy_runs[0] if strategy_runs else {}

    _section_header(
        st,
        "Virtuoso",
        "Configured strategy apps, persisted execution state, and strategy-book returns.",
    )
    _metric_strip(
        st,
        [
            ("Configured Apps", len(strategy_configs), "neutral"),
            ("Enabled Apps", enabled_count, _count_tone(len(strategy_configs) - enabled_count)),
            ("Observed Apps", observed_count, "neutral"),
            ("Latest Strategy Run", latest_run.get("created_at") or "n/a", "neutral"),
        ],
    )
    if not strategy_ids:
        _status_banner(
            st,
            "No Virtuoso strategies",
            "No configured or persisted strategy app state was found.",
            "warning",
        )
        return

    overview_rows = [
        _virtuoso_strategy_overview_row(
            strategy_id,
            strategy_configs.get(strategy_id),
            _strategy_rows(strategy_runs, strategy_id),
            _strategy_rows(strategy_book_performance, strategy_id),
        )
        for strategy_id in strategy_ids
    ]
    table("Virtuoso Strategy Overview", overview_rows, "virtuoso_strategy_overview")

    strategy_tabs = st.tabs(strategy_ids)
    for strategy_tab, strategy_id in zip(strategy_tabs, strategy_ids, strict=True):
        with strategy_tab:
            _render_virtuoso_strategy_tab(
                st,
                strategy_id,
                strategy_configs.get(strategy_id),
                _strategy_rows(strategy_runs, strategy_id),
                _strategy_rows(strategy_book_performance, strategy_id),
                _strategy_rows(strategy_attribution, strategy_id),
                _strategy_rows(strategy_book_snapshots, strategy_id),
                table,
            )


def _render_virtuoso_strategy_tab(
    st: object,
    strategy_id: str,
    strategy_config: object | None,
    runs: list[dict[str, object]],
    performance: list[dict[str, object]],
    attribution: list[dict[str, object]],
    snapshots: list[dict[str, object]],
    table: object,
) -> None:
    summary = _strategy_return_summary(performance)
    latest_run = runs[0] if runs else {}
    enabled = getattr(strategy_config, "enabled", bool(runs or performance or snapshots))
    validation_ok = latest_run.get("validation_ok")
    key = _key_slug(strategy_id)

    _section_header(
        st,
        strategy_id,
        "Virtuoso app concept, operation state, latest signals, and strategy-book returns.",
    )
    _metric_strip(
        st,
        [
            ("State", "enabled" if enabled else "disabled", _boolean_tone(enabled)),
            ("Weight", getattr(strategy_config, "weight", "n/a"), "neutral"),
            ("Latest Run", latest_run.get("created_at") or "n/a", "neutral"),
            ("Validation", _validation_label(validation_ok), _boolean_tone(validation_ok)),
            ("Book Value", _money(summary["book_value"]), "neutral"),
            ("Cumulative Return", _percent(summary["cumulative_return"]), "neutral"),
            ("Drawdown", _percent(summary["drawdown"]), _status_tone("warn")),
        ],
    )

    concept_cols = st.columns([1, 1])
    with concept_cols[0]:
        _section_header(st, "App Concept")
        st.dataframe(_virtuoso_concept_rows(strategy_id, strategy_config), width="stretch")
    with concept_cols[1]:
        _section_header(st, "Operation State")
        st.dataframe(
            _virtuoso_operation_rows(strategy_config, runs, performance, snapshots),
            width="stretch",
        )

    if performance:
        chart_rows = list(reversed(performance))
        st.line_chart(chart_rows, x="created_at", y="cumulative_return", color="book_id")
    else:
        _status_banner(
            st,
            "No strategy-book returns",
            "Returns will appear after Maestro records strategy book snapshots.",
            "warning",
        )

    table("Strategy Book Returns", performance, f"virtuoso_{key}_book_returns")
    table("Strategy Attribution", attribution, f"virtuoso_{key}_attribution")
    table("Strategy Book Snapshots", snapshots, f"virtuoso_{key}_book_snapshots")
    table("Recent Strategy Runs", runs, f"virtuoso_{key}_strategy_runs")
    if strategy_config is not None:
        with st.expander("Virtuoso App Config"):
            st.json(_strategy_config_payload(strategy_config))


def _virtuoso_strategy_ids(
    configured_ids: list[str],
    strategy_runs: list[dict[str, object]],
    strategy_book_performance: list[dict[str, object]],
    strategy_book_snapshots: list[dict[str, object]],
) -> list[str]:
    ids = list(configured_ids)
    observed_ids = {
        str(row.get("strategy_id"))
        for row in [*strategy_runs, *strategy_book_performance, *strategy_book_snapshots]
        if row.get("strategy_id")
    }
    ids.extend(sorted(observed_ids - set(ids)))
    return ids


def _system_map(st: object, nodes: list[dict[str, object]]) -> None:
    cards = []
    for node in nodes:
        cards.append(
            f"""
            <div class="maestro-flow-card {_tone_class(str(node["tone"]))}">
              <div class="maestro-flow-step">{_escape(node["step"])}</div>
              <div class="maestro-flow-title">{_escape(node["title"])}</div>
              <div class="maestro-flow-status">{_escape(node["status"])}</div>
              <div class="maestro-flow-detail">{_escape(node["detail"])}</div>
            </div>
            """
        )
    st.markdown(
        f'<div class="maestro-flow-grid">{"".join(cards)}</div>',
        unsafe_allow_html=True,
    )


def _system_node(
    title: str,
    step: str,
    status: object,
    detail: object,
    tone: str,
) -> dict[str, object]:
    return {
        "title": title,
        "step": step,
        "status": status,
        "detail": detail,
        "tone": tone,
    }


def _freshness_rollup(rows: list[dict[str, object]]) -> str:
    statuses = {str(row.get("status")) for row in rows}
    if statuses & {"failed", "missing"}:
        return "danger"
    if "stale" in statuses:
        return "warning"
    if "fresh" in statuses:
        return "success"
    return "neutral"


def _approval_label(value: object) -> str:
    if value is True:
        return "approved"
    if value is False:
        return "blocked"
    return "missing"


def _virtuoso_strategy_overview_row(
    strategy_id: str,
    strategy_config: object | None,
    runs: list[dict[str, object]],
    performance: list[dict[str, object]],
) -> dict[str, object]:
    summary = _strategy_return_summary(performance)
    latest_run = runs[0] if runs else {}
    enabled = getattr(strategy_config, "enabled", bool(runs or performance))
    return {
        "strategy_id": strategy_id,
        "enabled": enabled,
        "entrypoint": getattr(strategy_config, "entrypoint", None),
        "weight": getattr(strategy_config, "weight", None),
        "latest_run_at": latest_run.get("created_at"),
        "latest_run_id": latest_run.get("run_id"),
        "validation_ok": latest_run.get("validation_ok"),
        "book_count": summary["book_count"],
        "book_value": summary["book_value"],
        "period_return": summary["period_return"],
        "cumulative_return": summary["cumulative_return"],
        "drawdown": summary["drawdown"],
    }


def _virtuoso_concept_rows(
    strategy_id: str,
    strategy_config: object | None,
) -> list[dict[str, object]]:
    entrypoint = getattr(strategy_config, "entrypoint", None)
    app_module, app_class = _entrypoint_parts(entrypoint)
    allocation_policy = getattr(strategy_config, "signal_to_allocation", None)
    config_payload = getattr(strategy_config, "config", {}) or {}
    return [
        {"aspect": "Virtuoso app", "value": strategy_id},
        {"aspect": "Python module", "value": app_module or "n/a"},
        {"aspect": "Strategy class", "value": app_class or "n/a"},
        {
            "aspect": "Result handling",
            "value": "signal-to-allocation" if allocation_policy else "target allocation",
        },
        {
            "aspect": "Configured weight",
            "value": _display_value(getattr(strategy_config, "weight", "n/a")),
        },
        {"aspect": "Config keys", "value": ", ".join(sorted(config_payload)) or "none"},
    ]


def _virtuoso_operation_rows(
    strategy_config: object | None,
    runs: list[dict[str, object]],
    performance: list[dict[str, object]],
    snapshots: list[dict[str, object]],
) -> list[dict[str, object]]:
    latest_run = runs[0] if runs else {}
    latest_performance = performance[0] if performance else {}
    latest_snapshot = snapshots[0] if snapshots else {}
    return [
        {
            "item": "Configured",
            "value": _display_value(strategy_config is not None),
            "status": "ok" if strategy_config is not None else "missing",
        },
        {
            "item": "Enabled",
            "value": _display_value(getattr(strategy_config, "enabled", "observed-only")),
            "status": "ok" if getattr(strategy_config, "enabled", False) else "warn",
        },
        {
            "item": "Latest run",
            "value": _display_value(latest_run.get("created_at")),
            "status": _validation_label(latest_run.get("validation_ok")),
        },
        {
            "item": "Latest book snapshot",
            "value": _display_value(latest_snapshot.get("created_at")),
            "status": "ok" if latest_snapshot else "missing",
        },
        {
            "item": "Latest book value",
            "value": _display_value(latest_performance.get("book_value")),
            "status": "ok" if latest_performance else "missing",
        },
    ]


def _strategy_return_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    latest_by_book: dict[str, dict[str, object]] = {}
    for row in rows:
        book_id = str(row.get("book_id") or row.get("strategy_id") or "default")
        latest_by_book.setdefault(book_id, row)
    latest_rows = list(latest_by_book.values())
    book_value = sum(_float_value(row.get("book_value")) or 0.0 for row in latest_rows)
    return {
        "book_count": len(latest_rows),
        "book_value": book_value if latest_rows else None,
        "period_return": _weighted_return(latest_rows, "period_return"),
        "cumulative_return": _weighted_return(latest_rows, "cumulative_return"),
        "drawdown": _weighted_return(latest_rows, "drawdown"),
    }


def _weighted_return(rows: list[dict[str, object]], key: str) -> float | None:
    valued_rows = [
        (value, weight)
        for row in rows
        if (value := _float_value(row.get(key))) is not None
        for weight in [_float_value(row.get("book_value")) or 0.0]
    ]
    total_weight = sum(weight for _, weight in valued_rows)
    if total_weight > 0:
        return sum(value * weight for value, weight in valued_rows) / total_weight
    values = [value for value, _ in valued_rows]
    if not values:
        return None
    return sum(values) / len(values)


def _strategy_rows(rows: list[dict[str, object]], strategy_id: str) -> list[dict[str, object]]:
    return [row for row in rows if row.get("strategy_id") == strategy_id]


def _entrypoint_parts(entrypoint: object) -> tuple[str | None, str | None]:
    if not entrypoint:
        return None, None
    module, _, class_name = str(entrypoint).partition(":")
    return module or None, class_name or None


def _strategy_config_payload(strategy_config: object) -> dict[str, object]:
    model_dump = getattr(strategy_config, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return {
        "id": getattr(strategy_config, "id", None),
        "enabled": getattr(strategy_config, "enabled", None),
        "weight": getattr(strategy_config, "weight", None),
        "entrypoint": getattr(strategy_config, "entrypoint", None),
        "config": getattr(strategy_config, "config", {}),
    }


def _validation_label(value: object) -> str:
    if value is True:
        return "passed"
    if value is False:
        return "failed"
    return "missing"


def _display_value(value: object) -> str:
    if value is None:
        return "n/a"
    return str(value)


def _key_slug(value: str) -> str:
    return "".join(character if character.isalnum() else "_" for character in value.lower())


def _dashboard_filters(st: object) -> dict[str, object]:
    st.sidebar.header("Filters")
    query = st.sidebar.text_input("Search tables")
    statuses = st.sidebar.multiselect(
        "Status",
        ["fresh", "stale", "missing", "failed", "ok", "warn", "fail", "approved", "rejected"],
    )
    return {"query": query, "statuses": statuses}


def _apply_design_theme(st: object, theme: str) -> None:
    st.markdown(
        """
        <style>
        """
        + _theme_variables(theme)
        + """
        .stApp {
          background: var(--maestro-canvas);
          color: var(--maestro-ink);
          font-family: "Inter", "SF Pro Display", -apple-system, BlinkMacSystemFont,
            "Segoe UI", sans-serif;
        }
        [data-testid="stAppViewContainer"], [data-testid="stHeader"] {
          background: var(--maestro-canvas);
        }
        .block-container {
          max-width: 1280px;
          padding-top: 32px;
          padding-bottom: 64px;
        }
        h1, h2, h3, h4, h5, h6, p, label, span, div {
          letter-spacing: 0;
        }
        h1, h2, h3, h4, h5, h6, p, label,
        [data-testid="stMarkdownContainer"], [data-testid="stCaptionContainer"] {
          color: var(--maestro-ink);
        }
        h1, h2, h3 {
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
          background: var(--maestro-sidebar);
          border-right: 1px solid var(--maestro-hairline);
        }
        [data-testid="stSidebar"] * {
          color: var(--maestro-ink);
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
        .stSelectbox [data-baseweb="select"],
        .stMultiSelect [data-baseweb="select"],
        .stTextInput input {
          background: var(--maestro-surface-1);
          color: var(--maestro-ink);
          border-color: var(--maestro-hairline);
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
        .maestro-flow-grid {
          display: grid;
          grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
          gap: 8px;
          margin: 14px 0 20px 0;
        }
        .maestro-flow-card {
          border: 1px solid var(--maestro-hairline);
          background: var(--maestro-surface-1);
          border-radius: 8px;
          padding: 14px;
          min-height: 132px;
        }
        .maestro-flow-step {
          color: var(--maestro-primary-hover);
          font-size: 12px;
          font-weight: 600;
          margin-bottom: 10px;
          text-transform: uppercase;
        }
        .maestro-flow-title {
          color: var(--maestro-ink);
          font-size: 18px;
          font-weight: 600;
          line-height: 1.2;
          margin-bottom: 10px;
        }
        .maestro-flow-status {
          color: var(--maestro-ink-muted);
          font-size: 13px;
          font-weight: 500;
          line-height: 1.35;
          margin-bottom: 8px;
        }
        .maestro-flow-detail {
          color: var(--maestro-ink-subtle);
          font-size: 12px;
          line-height: 1.4;
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


def _theme_variables(theme: str) -> str:
    dark = """
        :root {
          --maestro-primary: #5e6ad2;
          --maestro-primary-hover: #828fff;
          --maestro-primary-focus: #5e69d1;
          --maestro-ink: #f7f8f8;
          --maestro-ink-muted: #d0d6e0;
          --maestro-ink-subtle: #8a8f98;
          --maestro-ink-tertiary: #62666d;
          --maestro-canvas: #010102;
          --maestro-sidebar: #09090a;
          --maestro-surface-1: #0f1011;
          --maestro-surface-2: #141516;
          --maestro-surface-3: #18191a;
          --maestro-hairline: #23252a;
          --maestro-hairline-strong: #34343a;
          --maestro-success: #27a644;
          --maestro-danger: #d06262;
          --maestro-warning: #d0a85c;
        }
    """
    light = """
        :root {
          --maestro-primary: #4f5bd5;
          --maestro-primary-hover: #3f49ba;
          --maestro-primary-focus: #4f5bd5;
          --maestro-ink: #111827;
          --maestro-ink-muted: #334155;
          --maestro-ink-subtle: #64748b;
          --maestro-ink-tertiary: #94a3b8;
          --maestro-canvas: #f7f8fb;
          --maestro-sidebar: #eef1f6;
          --maestro-surface-1: #ffffff;
          --maestro-surface-2: #f1f4f8;
          --maestro-surface-3: #e8edf5;
          --maestro-hairline: #d8dee9;
          --maestro-hairline-strong: #b8c2d3;
          --maestro-success: #1f8f3a;
          --maestro-danger: #b42318;
          --maestro-warning: #a16207;
        }
    """
    if theme == "Light":
        return light
    if theme == "Dark":
        return dark
    return (
        dark
        + """
        @media (prefers-color-scheme: light) {
        """
        + light.strip()
        + """
        }
        """
    )


def _page_header(st: object, mode: str, operator_config: dict[str, str] | None) -> None:
    fingerprint = (operator_config or {}).get("fingerprint", "none")
    short_fingerprint = fingerprint[:12] if fingerprint != "none" else "none"
    st.markdown(
        f"""
        <div class="maestro-header">
          <div class="maestro-eyebrow">SYMPHONY / MAESTRO</div>
          <h1 class="maestro-title">Symphony</h1>
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


def _float_value(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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

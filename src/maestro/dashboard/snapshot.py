from pathlib import Path
from typing import Any

from maestro.config.loader import load_config_with_identity
from maestro.core.time_display import add_operator_time_fields, operator_timezone
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
    build_latest_signal_package_card,
    build_live_order_events_table,
    build_maestro_state_exposure_table,
    build_operator_home,
    build_operator_summary,
    build_orders_table,
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

DISPLAY_CURRENCIES = {"KRW", "USD"}


def build_dashboard_snapshot(
    config_path: str | Path,
    display_currency: str = "KRW",
) -> dict[str, Any]:
    selected_currency = _display_currency(display_currency)
    config_path = Path(config_path)
    config, identity = load_config_with_identity(config_path)
    store = _state_store(config, identity)
    status = store.status()

    operator_summary = build_operator_summary(config, store)
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
    latest_signal_package = build_latest_signal_package_card(store)

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
    total_portfolio_performance_krw = build_total_portfolio_performance_table(
        store,
        display_currency="KRW",
    )
    total_portfolio_performance_usd = build_total_portfolio_performance_table(
        store,
        display_currency="USD",
    )
    total_portfolio_performance = {
        "KRW": total_portfolio_performance_krw,
        "USD": total_portfolio_performance_usd,
    }[selected_currency]
    account_performance_currency = _broker_display_currency(
        config,
        account_performance,
        total_portfolio_performance_krw,
    )
    asset_summary_metrics = _asset_summary_metrics(
        _latest_component_value(total_portfolio_performance_krw, "KRW"),
        _latest_component_value(total_portfolio_performance_krw, "USD"),
        _latest_total_value(total_portfolio_performance_krw),
        _latest_total_value(total_portfolio_performance_usd),
        fx_snapshot,
    )
    asset_summary_rows = _asset_summary_rows(
        total_portfolio_performance_krw,
        total_portfolio_performance_usd,
        fx_snapshot,
    )
    verdict_reason_rows = _verdict_reason_rows(
        operator_summary,
        freshness,
        health,
        reconciliation,
        live_order_lifecycle,
        fx_snapshot,
    )
    strategy_book_snapshots = build_strategy_book_snapshots_table(store)
    strategy_book_performance = build_strategy_book_performance_table(store)
    strategy_attribution = build_strategy_attribution_table(store)
    run_index = build_run_index_table(store)
    enabled_strategies = [
        strategy
        for strategy in getattr(config, "strategies", [])
        if getattr(strategy, "enabled", False)
    ]

    snapshot = {
        "title": "Symphony",
        "read_only": True,
        "display_currency": selected_currency,
        "header": {
            "mode": config.mode.value,
            "order_posture": config.execution.order_posture,
            "operator_config": status.get("operator_config"),
            "config_path": str(identity.path),
            "state_path": str(Path(config.state.sqlite_path).expanduser().resolve()),
            "audit_path": str(Path(config.audit.jsonl_path).expanduser().resolve()),
        },
        "operator_home": operator_home,
        "system_verdict": _system_verdict(
            operator_home,
            safety,
            health,
            reconciliation,
            asset_summary_metrics,
            asset_summary_rows,
            verdict_reason_rows,
        ),
        "symphony_map": {
            "metrics": [
                _metric("Overall", str(operator_home["status"]).upper(), operator_home["status"]),
                _metric("Enabled Virtuoso Apps", len(enabled_strategies), "neutral"),
                _metric("Health", str(health["status"]).upper(), _status_tone(health["status"])),
                _metric(
                    "Reconciliation",
                    _reconciliation_label(reconciliation.get("passed")),
                    _boolean_tone(reconciliation.get("passed")),
                ),
                _metric(
                    "Freshness",
                    str(_freshness_rollup(freshness)).upper(),
                    _freshness_rollup(freshness),
                ),
                _metric(
                    "Attention",
                    operator_home["attention_count"],
                    _count_tone(operator_home["attention_count"]),
                ),
            ],
            "nodes": _system_nodes(
                config,
                operator_home,
                health,
                reconciliation,
                broker_snapshot,
                freshness,
                strategy_runs,
                risk_decisions,
                orders,
                approvals,
                live_order_lifecycle,
                run_index,
            ),
            "attention_items": operator_home["attention_items"],
            "verdict_reason_rows": verdict_reason_rows,
            "asset_summary_rows": asset_summary_rows,
            "freshness": freshness,
            "run_index": run_index,
        },
        "operator_cockpit": {
            "metrics": _operator_metrics(
                operator_home,
                safety,
                health,
                reconciliation,
                operator_summary,
                daily_usage,
                live_order_lifecycle,
                risk_decisions,
                run_index,
            ),
            "attention_items": operator_summary["attention_items"],
            "freshness": freshness,
            "health_checks": health["checks"],
            "operator_summary": operator_summary,
            "daily_usage": daily_usage,
            "live_order_lifecycle": live_order_lifecycle,
            "latest_signal_package": latest_signal_package,
            "risk_decisions": risk_decisions,
            "halt_failure_events": halt_failure_events,
            "run_index": run_index,
        },
        "investment_console": {
            "metrics": _investment_metrics(
                broker_summary,
                reconciliation,
                account_performance,
                account_performance_currency,
            ),
            "broker_summary": broker_summary,
            "broker_snapshot": broker_snapshot,
            "reconciliation": reconciliation,
            "account_performance": account_performance,
            "account_performance_currency": account_performance_currency,
            "currency_sleeve_performance": currency_sleeve_performance,
            "total_portfolio_performance": total_portfolio_performance,
            "total_portfolio_performance_krw": total_portfolio_performance_krw,
            "total_portfolio_performance_usd": total_portfolio_performance_usd,
            "performance_snapshot": _performance_snapshot(
                selected_currency,
                account_performance,
                account_performance_currency,
                currency_sleeve_performance,
                total_portfolio_performance,
                total_portfolio_performance_krw,
                total_portfolio_performance_usd,
                strategy_book_performance,
                strategy_attribution,
                fx_snapshot,
            ),
            "fx_snapshot": fx_snapshot,
            "asset_summary_metrics": asset_summary_metrics,
            "asset_summary_rows": asset_summary_rows,
            "strategy_book_performance": strategy_book_performance,
            "strategy_attribution": strategy_attribution,
            "strategy_book_snapshots": strategy_book_snapshots,
            "broker_positions": broker_positions,
            "maestro_exposure": maestro_exposure,
            "portfolio": portfolio_table,
            "portfolio_history": portfolio_history,
            "broker_history": broker_history,
        },
        "virtuoso_apps": _virtuoso_apps(
            config,
            strategy_runs,
            strategy_book_performance,
            strategy_attribution,
            strategy_book_snapshots,
        ),
        "audit_trail": {
            "metrics": [
                _metric("Indexed Runs", len(run_index), "neutral"),
                _metric("Strategy Runs", len(strategy_runs), "neutral"),
                _metric("Orders", len(orders), "neutral"),
                _metric("Approvals", len(approvals), "neutral"),
                _metric("Broker Snapshots", len(broker_snapshots), "neutral"),
                _metric("System Events", len(system_events), "neutral"),
            ],
            "strategy_signal_rows": _strategy_signal_rows(strategy_runs),
            "strategy_run_payloads": [row.get("payload", {}) for row in strategy_runs],
            "orders": orders,
            "approvals": approvals,
            "broker_snapshots": broker_snapshots,
            "live_order_events": live_order_events,
            "fill_reconciliation": fill_reconciliation,
            "system_events": system_events,
            "run_index": run_index,
        },
        "raw": {"status": status},
    }
    return add_operator_time_fields(snapshot, operator_timezone(config))


def build_dashboard_run_detail(config_path: str | Path, run_id: str) -> dict[str, Any]:
    config, identity = load_config_with_identity(config_path)
    return build_run_detail(_state_store(config, identity), run_id)


def _state_store(config: Any, identity: Any) -> StateStore:
    return StateStore(
        config.state.sqlite_path,
        config.portfolio.initial_cash,
        config.portfolio.cash_by_currency,
        config_identity=identity,
    )


def _display_currency(display_currency: str) -> str:
    normalized = str(display_currency or "KRW").upper()
    return normalized if normalized in DISPLAY_CURRENCIES else "KRW"


def _system_verdict(
    operator_home: dict[str, Any],
    safety: dict[str, Any],
    health: dict[str, Any],
    reconciliation: dict[str, Any],
    asset_summary_metrics: list[dict[str, Any]],
    asset_summary_rows: list[dict[str, Any]],
    verdict_reason_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    top_reason = verdict_reason_rows[0] if verdict_reason_rows else None
    title = str(operator_home["status"]).upper()
    if top_reason:
        title = f"{title} · {top_reason['source']}"
        copy = str(top_reason["reason"])
        tone = str(top_reason["tone"])
    else:
        copy = "No blocking attention, health, reconciliation, lifecycle, or FX reason found."
        tone = _status_tone(operator_home["status"])
    return {
        "title": title,
        "copy": copy,
        "tone": tone,
        "status_metrics": [
            _metric(
                "Operational State",
                str(operator_home["status"]).upper(),
                operator_home["status"],
            ),
            _metric("Safety", str(safety["state"]).upper(), _status_tone(safety["state"])),
            _metric("Health", str(health["status"]).upper(), _status_tone(health["status"])),
            _metric(
                "Reconciliation",
                _reconciliation_label(reconciliation.get("passed")),
                _boolean_tone(reconciliation.get("passed")),
            ),
            _metric(
                "Attention",
                operator_home["attention_count"],
                _count_tone(operator_home["attention_count"]),
            ),
        ],
        "capital_summary": asset_summary_metrics,
        "asset_summary_rows": asset_summary_rows,
        "reason_rows": verdict_reason_rows,
    }


def _system_nodes(
    config: Any,
    operator_home: dict[str, Any],
    health: dict[str, Any],
    reconciliation: dict[str, Any],
    broker_snapshot: dict[str, Any],
    freshness: list[dict[str, Any]],
    strategy_runs: list[dict[str, Any]],
    risk_decisions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    approvals: list[dict[str, Any]],
    live_order_lifecycle: dict[str, Any],
    run_index: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest_strategy = strategy_runs[0] if strategy_runs else {}
    latest_risk = risk_decisions[0] if risk_decisions else {}
    latest_order = orders[0] if orders else {}
    latest_approval = approvals[0] if approvals else {}
    latest_lifecycle = live_order_lifecycle.get("latest") or {}
    enabled_count = sum(
        1 for strategy in getattr(config, "strategies", []) if getattr(strategy, "enabled", False)
    )
    freshest_status = _freshness_rollup(freshness)
    return [
        _system_node(
            "Virtuoso",
            "Propose",
            f"{enabled_count} enabled app(s)",
            latest_strategy.get("created_at") or "No recent proposal",
            _boolean_tone(bool(enabled_count)),
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
            latest_lifecycle.get("status") or latest_order.get("created_at") or "No live lifecycle",
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
    ]


def _operator_metrics(
    operator_home: dict[str, Any],
    safety: dict[str, Any],
    health: dict[str, Any],
    reconciliation: dict[str, Any],
    operator_summary: dict[str, Any],
    daily_usage: dict[str, Any],
    live_order_lifecycle: dict[str, Any],
    risk_decisions: list[dict[str, Any]],
    run_index: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    daily_notional_value = (
        f"{_money(daily_usage['notional'])} / {_money(daily_usage['max_daily_live_notional'])}"
    )
    latest_lifecycle = live_order_lifecycle["latest"] or {}
    return [
        _metric("Overall", str(operator_home["status"]).upper(), operator_home["status"]),
        _metric("Safety State", str(safety["state"]).upper(), _status_tone(safety["state"])),
        _metric("Health", str(health["status"]).upper(), _status_tone(health["status"])),
        _metric(
            "Reconciliation",
            _reconciliation_label(reconciliation.get("passed")),
            _boolean_tone(reconciliation.get("passed")),
        ),
        _metric("Broker Snapshot Age", _duration(operator_summary["broker_snapshot_age_seconds"])),
        _metric(
            "Daily Live Orders",
            f"{daily_usage['order_count']} / {daily_usage['max_daily_live_order_count']}",
            _limit_tone(daily_usage["order_count"], daily_usage["max_daily_live_order_count"]),
        ),
        _metric(
            "Daily Live Notional",
            daily_notional_value,
            _limit_tone(daily_usage["notional"], daily_usage["max_daily_live_notional"]),
        ),
        _metric(
            "Live Order Issues",
            live_order_lifecycle["recent_issue_count"],
            _count_tone(live_order_lifecycle["recent_issue_count"]),
        ),
        _metric(
            "Latest Live Order",
            latest_lifecycle.get("status") or "n/a",
            _status_tone(latest_lifecycle.get("status")),
        ),
        _metric("Lifecycle Rows", len(live_order_lifecycle["recent"])),
        _metric("Risk Decisions", len(risk_decisions)),
        _metric("Indexed Runs", len(run_index)),
    ]


def _investment_metrics(
    broker_summary: dict[str, Any],
    reconciliation: dict[str, Any],
    account_performance: list[dict[str, Any]],
    account_performance_currency: str | None,
) -> list[dict[str, Any]]:
    latest_performance = account_performance[0] if account_performance else {}
    return [
        _metric(
            "Account Value",
            _money(latest_performance.get("total_value"), account_performance_currency),
        ),
        _metric("Broker Cash", _money(broker_summary["cash"], account_performance_currency)),
        _metric("Broker Exposure", _percent(broker_summary["exposure_weight"])),
        _metric("Period Return", _percent(latest_performance.get("period_return"))),
        _metric("Cumulative Return", _percent(latest_performance.get("cumulative_return"))),
        _metric("Drawdown", _percent(latest_performance.get("drawdown"))),
        _metric(
            "Reconciliation",
            latest_performance.get("reconciliation_status")
            or _reconciliation_label(reconciliation.get("passed")),
            _status_tone(latest_performance.get("reconciliation_status")),
        ),
    ]


def _performance_snapshot(
    display_currency: str,
    account_performance: list[dict[str, Any]],
    account_performance_currency: str | None,
    currency_sleeve_performance: list[dict[str, Any]],
    total_portfolio_performance: list[dict[str, Any]],
    total_portfolio_performance_krw: list[dict[str, Any]],
    total_portfolio_performance_usd: list[dict[str, Any]],
    strategy_book_performance: list[dict[str, Any]],
    strategy_attribution: list[dict[str, Any]],
    fx_snapshot: dict[str, Any],
) -> dict[str, Any]:
    latest_total = total_portfolio_performance[0] if total_portfolio_performance else {}
    latest_account = account_performance[0] if account_performance else {}
    quality = _performance_quality(latest_total, account_performance, total_portfolio_performance)
    return {
        "schema_version": 1,
        "display_currency": display_currency,
        "latest": {
            "created_at": latest_total.get("created_at") or latest_account.get("created_at"),
            "run_id": latest_total.get("run_id") or latest_account.get("run_id"),
            "display_currency": display_currency,
            "currency": latest_total.get("currency"),
            "total_value": latest_total.get("total_value"),
            "period_return": latest_total.get("period_return"),
            "daily_return": latest_total.get("daily_return"),
            "cumulative_return": latest_total.get("cumulative_return"),
            "drawdown": latest_total.get("drawdown"),
            "local_return": latest_total.get("local_return"),
            "fx_effect": latest_total.get("fx_effect"),
            "fx_status": latest_total.get("fx_status"),
            "reconciliation_status": latest_total.get("reconciliation_status")
            or latest_account.get("reconciliation_status"),
            "account_value": latest_account.get("total_value"),
            "account_currency": account_performance_currency or latest_account.get("currency"),
        },
        "series": {
            "account": account_performance,
            "currency_sleeves": currency_sleeve_performance,
            "total_portfolio": total_portfolio_performance,
            "total_portfolio_krw": total_portfolio_performance_krw,
            "total_portfolio_usd": total_portfolio_performance_usd,
            "strategy_books": strategy_book_performance,
            "strategy_attribution": strategy_attribution,
        },
        "quality": quality,
        "fx": {
            "status": fx_snapshot.get("status"),
            "source": fx_snapshot.get("source"),
            "rate": fx_snapshot.get("rate"),
            "as_of": fx_snapshot.get("as_of"),
            "age_seconds": fx_snapshot.get("age_seconds"),
        },
        "lineage": {
            "source_tables": [
                "broker_account_snapshots",
                "system_events",
                "strategy_book_snapshots",
                "strategy_runs",
            ],
            "return_method": "time_series_return_with_cash_flow_adjustment",
            "fx_policy": "converted_totals_require_fresh_persisted_fx",
        },
    }


def _performance_quality(
    latest_total: dict[str, Any],
    account_performance: list[dict[str, Any]],
    total_portfolio_performance: list[dict[str, Any]],
) -> dict[str, Any]:
    reasons = []
    if not account_performance and not total_portfolio_performance:
        reasons.append(
            {
                "code": "missing_performance_history",
                "message": "No broker account snapshots are available for performance history.",
            }
        )
    if latest_total.get("missing_fx"):
        reasons.append(
            {
                "code": "missing_fx",
                "message": (
                    "Converted total portfolio values are unavailable because FX is missing."
                ),
            }
        )
    if latest_total.get("stale_fx"):
        reasons.append(
            {
                "code": "stale_fx",
                "message": "Converted total portfolio values are unavailable because FX is stale.",
            }
        )
    if not reasons:
        status = "ok"
    elif reasons[0]["code"] == "missing_performance_history":
        status = "missing"
    else:
        status = "warning"
    return {"status": status, "reasons": reasons}


def _virtuoso_apps(
    config: Any,
    strategy_runs: list[dict[str, Any]],
    strategy_book_performance: list[dict[str, Any]],
    strategy_attribution: list[dict[str, Any]],
    strategy_book_snapshots: list[dict[str, Any]],
) -> dict[str, Any]:
    strategy_configs = {
        strategy.id: strategy
        for strategy in getattr(config, "strategies", [])
        if getattr(strategy, "readonly_enabled", True)
    }
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
    overview_rows = [
        _virtuoso_strategy_overview_row(
            strategy_id,
            strategy_configs.get(strategy_id),
            _strategy_rows(strategy_runs, strategy_id),
            _strategy_rows(strategy_book_performance, strategy_id),
        )
        for strategy_id in strategy_ids
    ]
    return {
        "metrics": [
            _metric("Configured Apps", len(strategy_configs)),
            _metric(
                "Enabled Apps",
                enabled_count,
                _count_tone(len(strategy_configs) - enabled_count),
            ),
            _metric("Observed Apps", observed_count),
            _metric("Latest Strategy Run", latest_run.get("created_at") or "n/a"),
        ],
        "overview": overview_rows,
        "strategies": [
            {
                "strategy_id": strategy_id,
                "concept": _virtuoso_concept_rows(strategy_id, strategy_configs.get(strategy_id)),
                "operation": _virtuoso_operation_rows(
                    strategy_configs.get(strategy_id),
                    _strategy_rows(strategy_runs, strategy_id),
                    _strategy_rows(strategy_book_performance, strategy_id),
                    _strategy_rows(strategy_book_snapshots, strategy_id),
                ),
                "performance": _strategy_rows(strategy_book_performance, strategy_id),
                "attribution": _strategy_rows(strategy_attribution, strategy_id),
                "snapshots": _strategy_rows(strategy_book_snapshots, strategy_id),
                "runs": _strategy_rows(strategy_runs, strategy_id),
                "config": _strategy_config_payload(strategy_configs[strategy_id])
                if strategy_id in strategy_configs
                else None,
                "summary": _strategy_return_summary(
                    _strategy_rows(strategy_book_performance, strategy_id)
                ),
            }
            for strategy_id in strategy_ids
        ],
    }


def _asset_summary_metrics(
    krw_assets: Any,
    usd_assets: Any,
    krw_total: Any,
    usd_total: Any,
    fx_snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        _metric("KRW Assets", _money(krw_assets, "KRW")),
        _metric("USD Assets", _money(usd_assets, "USD")),
        _metric("Total Assets (KRW)", _money(krw_total, "KRW"), _asset_total_tone(krw_total)),
        _metric("Total Assets (USD)", _money(usd_total, "USD"), _asset_total_tone(usd_total)),
        _metric("FX", fx_snapshot["status"], _status_tone(fx_snapshot["status"])),
    ]


def _asset_summary_rows(
    krw_rows: list[dict[str, Any]],
    usd_rows: list[dict[str, Any]],
    fx_snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    krw_row = krw_rows[0] if krw_rows else {}
    usd_row = usd_rows[0] if usd_rows else {}
    krw_assets = _latest_component_value(krw_rows, "KRW")
    usd_assets = _latest_component_value(krw_rows, "USD")
    return [
        {
            "label": "Native KRW assets",
            "amount": krw_assets,
            "currency": "KRW",
            "display": _money(krw_assets, "KRW"),
            "fx_status": "not_needed",
        },
        {
            "label": "Native USD assets",
            "amount": usd_assets,
            "currency": "USD",
            "display": _money(usd_assets, "USD"),
            "fx_status": "not_needed",
        },
        {
            "label": "Total assets in KRW",
            "amount": _float_value(krw_row.get("total_value")),
            "currency": "KRW",
            "display": _money(krw_row.get("total_value"), "KRW"),
            "fx_status": krw_row.get("fx_status"),
            "fx_rate": krw_row.get("fx_rate"),
            "fx_timestamp": krw_row.get("fx_timestamp"),
        },
        {
            "label": "Total assets in USD",
            "amount": _float_value(usd_row.get("total_value")),
            "currency": "USD",
            "display": _money(usd_row.get("total_value"), "USD"),
            "fx_status": usd_row.get("fx_status"),
            "fx_rate": usd_row.get("fx_rate"),
            "fx_timestamp": usd_row.get("fx_timestamp"),
        },
        {
            "label": "FX snapshot",
            "amount": fx_snapshot.get("rate"),
            "currency": "USD/KRW",
            "display": _display_value(fx_snapshot.get("rate")),
            "fx_status": fx_snapshot.get("status"),
            "fx_source": fx_snapshot.get("source"),
            "fx_timestamp": fx_snapshot.get("as_of"),
        },
    ]


def _verdict_reason_rows(
    operator_summary: dict[str, Any],
    freshness: list[dict[str, Any]],
    health: dict[str, Any],
    reconciliation: dict[str, Any],
    live_order_lifecycle: dict[str, Any],
    fx_snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in operator_summary.get("attention_items", []):
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity") or "warning")
        rows.append(
            _verdict_reason_row(
                source=item.get("code") or "Attention",
                status=severity,
                reason=item.get("message") or item.get("code") or "Attention item needs review",
                next_check="Open Operator Cockpit attention queue",
                tone=_status_tone(severity),
            )
        )

    for row in freshness:
        status = str(row.get("status") or "")
        if _status_tone(status) in {"warning", "danger"}:
            source = _row_label(row, "Freshness")
            age = row.get("age_seconds")
            max_age = row.get("max_age_seconds")
            detail = f"{source} is {status}"
            if age is not None and max_age is not None:
                detail = f"{detail} ({_duration(age)} old, limit {_duration(max_age)})"
            rows.append(
                _verdict_reason_row(
                    source=source,
                    status=status,
                    reason=detail,
                    next_check="Review freshness evidence and run the relevant sync if needed",
                    tone=_status_tone(status),
                )
            )

    health_checks = health.get("checks") if isinstance(health.get("checks"), list) else []
    for check in health_checks:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status") or "")
        if status.lower() in {"", "ok", "pass", "passed", "fresh"}:
            continue
        rows.append(
            _verdict_reason_row(
                source=_row_label(check, "Health"),
                status=status,
                reason=check.get("message") or f"{_row_label(check, 'Health')} is {status}",
                next_check="Open Operator Cockpit health checks",
                tone=_status_tone(status),
            )
        )

    if reconciliation.get("passed") is not True:
        passed = reconciliation.get("passed")
        rows.append(
            _verdict_reason_row(
                source="Reconciliation",
                status=_reconciliation_label(passed).lower(),
                reason="Latest broker and Maestro state reconciliation is not passed",
                next_check="Review reconciliation card and broker/portfolio evidence",
                tone=_boolean_tone(passed),
            )
        )

    recent_issue_count = int(live_order_lifecycle.get("recent_issue_count") or 0)
    if recent_issue_count > 0:
        rows.append(
            _verdict_reason_row(
                source="Live orders",
                status="issues",
                reason=f"{recent_issue_count} recent live order lifecycle issue(s)",
                next_check="Review live order lifecycle events",
                tone="danger",
            )
        )

    fx_status = str(fx_snapshot.get("status") or "")
    if _status_tone(fx_status) in {"warning", "danger"}:
        fx_source = fx_snapshot.get("source") or "unknown source"
        rows.append(
            _verdict_reason_row(
                source="FX",
                status=fx_status,
                reason=f"FX snapshot from {fx_source} is {fx_status}",
                next_check="Review FX snapshot before trusting converted totals",
                tone=_status_tone(fx_status),
            )
        )

    return sorted(rows, key=lambda row: _tone_sort_key(row["tone"]))


def _strategy_signal_rows(strategy_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    columns = [
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
    return [{column: row.get(column) for column in columns} for row in strategy_runs]


def _virtuoso_strategy_ids(
    configured_ids: list[str],
    strategy_runs: list[dict[str, Any]],
    strategy_book_performance: list[dict[str, Any]],
    strategy_book_snapshots: list[dict[str, Any]],
) -> list[str]:
    ids = list(configured_ids)
    observed_ids = {
        str(row.get("strategy_id"))
        for row in [*strategy_runs, *strategy_book_performance, *strategy_book_snapshots]
        if row.get("strategy_id")
    }
    ids.extend(sorted(observed_ids - set(ids)))
    return ids


def _virtuoso_strategy_overview_row(
    strategy_id: str,
    strategy_config: Any | None,
    runs: list[dict[str, Any]],
    performance: list[dict[str, Any]],
) -> dict[str, Any]:
    summary = _strategy_return_summary(performance)
    latest_run = runs[0] if runs else {}
    enabled = getattr(strategy_config, "enabled", bool(runs or performance))
    return {
        "strategy_id": strategy_id,
        "enabled": enabled,
        "entrypoint": getattr(strategy_config, "entrypoint", None),
        "account_id": getattr(strategy_config, "account_id", None),
        "signal_enabled": getattr(strategy_config, "signal_enabled", None),
        "order_posture": getattr(strategy_config, "order_posture", None),
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


def _virtuoso_concept_rows(strategy_id: str, strategy_config: Any | None) -> list[dict[str, Any]]:
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
    strategy_config: Any | None,
    runs: list[dict[str, Any]],
    performance: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
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
            "item": "Readonly visible",
            "value": _display_value(getattr(strategy_config, "readonly_enabled", "n/a")),
            "status": "ok" if getattr(strategy_config, "readonly_enabled", False) else "missing",
        },
        {
            "item": "Signal enabled",
            "value": _display_value(getattr(strategy_config, "signal_enabled", "n/a")),
            "status": "ok" if getattr(strategy_config, "signal_enabled", False) else "warn",
        },
        {
            "item": "Account",
            "value": _display_value(getattr(strategy_config, "account_id", None)),
            "status": "ok" if getattr(strategy_config, "account_id", None) else "missing",
        },
        {
            "item": "Order posture",
            "value": _display_value(getattr(strategy_config, "order_posture", None)),
            "status": "ok" if getattr(strategy_config, "order_posture", None) else "missing",
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


def _strategy_return_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latest_by_book: dict[str, dict[str, Any]] = {}
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


def _weighted_return(rows: list[dict[str, Any]], key: str) -> float | None:
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


def _strategy_rows(rows: list[dict[str, Any]], strategy_id: str) -> list[dict[str, Any]]:
    return [row for row in rows if row.get("strategy_id") == strategy_id]


def _entrypoint_parts(entrypoint: Any) -> tuple[str | None, str | None]:
    if not entrypoint:
        return None, None
    module, _, class_name = str(entrypoint).partition(":")
    return module or None, class_name or None


def _strategy_config_payload(strategy_config: Any) -> dict[str, Any]:
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


def _metric(label: str, value: Any, tone: str = "neutral") -> dict[str, Any]:
    return {"label": label, "value": value, "tone": tone}


def _system_node(title: str, step: str, status: Any, detail: Any, tone: str) -> dict[str, Any]:
    return {"title": title, "step": step, "status": status, "detail": detail, "tone": tone}


def _verdict_reason_row(
    source: Any,
    status: Any,
    reason: Any,
    next_check: Any,
    tone: str,
) -> dict[str, Any]:
    return {
        "severity": tone,
        "source": source,
        "status": status,
        "reason": reason,
        "next_check": next_check,
        "tone": tone,
    }


def _broker_display_currency(
    config: Any,
    account_performance: list[dict[str, Any]],
    total_portfolio_performance_krw: list[dict[str, Any]],
) -> str:
    explicit_currency = _latest_currency(account_performance)
    if explicit_currency:
        return explicit_currency
    component_values = (
        total_portfolio_performance_krw[0].get("component_values")
        if total_portfolio_performance_krw
        else None
    )
    if isinstance(component_values, dict) and len(component_values) == 1:
        return str(next(iter(component_values))).upper()
    broker_products = getattr(getattr(config, "kis", None), "effective_broker_products", None)
    if callable(broker_products):
        product_values = {str(product) for product in broker_products()}
        if any("DOMESTIC" in value for value in product_values):
            return "KRW"
        if product_values and all("OVERSEAS" in value for value in product_values):
            return "USD"
    base_currency = getattr(getattr(config, "portfolio", None), "base_currency", None)
    return str(base_currency or "KRW").upper()


def _latest_currency(rows: list[dict[str, Any]]) -> str | None:
    if not rows:
        return None
    currency = rows[0].get("currency") or rows[0].get("display_currency")
    if not currency:
        return None
    currency_text = str(currency).upper()
    if currency_text in {"UNKNOWN", "MIXED", "N/A"}:
        return None
    return currency_text


def _latest_total_value(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return None
    return _float_value(rows[0].get("total_value"))


def _latest_component_value(rows: list[dict[str, Any]], currency: str) -> float | None:
    if not rows:
        return None
    component_values = rows[0].get("component_values")
    if not isinstance(component_values, dict):
        return None
    return _float_value(component_values.get(currency))


def _freshness_rollup(rows: list[dict[str, Any]]) -> str:
    statuses = {str(row.get("status")) for row in rows}
    if statuses & {"failed", "missing"}:
        return "danger"
    if "stale" in statuses:
        return "warning"
    if "fresh" in statuses:
        return "success"
    return "neutral"


def _status_tone(value: Any) -> str:
    normalized = str(value or "").lower()
    if normalized in {"ok", "active", "fresh", "passed", "approved", "completed", "filled"}:
        return "success"
    if normalized in {"warn", "warning", "stale", "missing", "open", "partially_filled"}:
        return "warning"
    if normalized in {"fail", "failed", "halted", "killed", "rejected", "unknown"}:
        return "danger"
    return "neutral"


def _boolean_tone(value: Any) -> str:
    if value is True:
        return "success"
    if value is False:
        return "danger"
    return "warning"


def _count_tone(value: Any) -> str:
    return "warning" if float(value or 0) > 0 else "success"


def _limit_tone(value: Any, limit: Any) -> str:
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


def _asset_total_tone(value: Any) -> str:
    return "success" if value is not None else "warning"


def _validation_label(value: Any) -> str:
    if value is True:
        return "passed"
    if value is False:
        return "failed"
    return "missing"


def _approval_label(value: Any) -> str:
    if value is True:
        return "approved"
    if value is False:
        return "blocked"
    return "missing"


def _reconciliation_label(value: Any) -> str:
    if value is True:
        return "PASSED"
    if value is False:
        return "FAILED"
    return "MISSING"


def _row_label(row: dict[str, Any], fallback: str) -> str:
    return str(
        row.get("name") or row.get("check") or row.get("source") or row.get("component") or fallback
    )


def _tone_sort_key(tone: Any) -> int:
    return {
        "danger": 0,
        "fail": 0,
        "error": 0,
        "warning": 1,
        "success": 2,
        "neutral": 3,
    }.get(str(tone or "neutral"), 3)


def _float_value(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _money(value: Any, currency: str | None = None) -> str:
    if value is None:
        return "n/a"
    formatted = f"{float(value):,.2f}"
    if currency and str(currency).upper() not in {"UNKNOWN", "MIXED", "N/A"}:
        return f"{formatted} {currency}"
    return formatted


def _percent(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.2%}"


def _duration(value: Any) -> str:
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


def _display_value(value: Any) -> str:
    if value is None:
        return "n/a"
    return str(value)

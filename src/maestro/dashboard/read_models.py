from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.time_display import (
    add_operator_time_details,
    format_operator_time,
    operator_timezone,
)
from maestro.monitoring.health import HealthService
from maestro.state.store import StateStore


def build_overview(store: StateStore) -> dict[str, Any]:
    status = store.status()
    state = store.load_latest_portfolio_state()
    counts = status["counts"]
    latest_snapshot = status.get("latest_snapshot") or {}
    return {
        "cash": state.cash,
        "positions_count": len(state.positions),
        "strategy_runs_count": counts.get("strategy_runs", 0),
        "orders_count": counts.get("orders", 0),
        "approvals_count": counts.get("approvals", 0),
        "risk_decisions_count": counts.get("risk_decisions", 0),
        "broker_snapshots_count": counts.get("broker_account_snapshots", 0),
        "strategy_book_snapshots_count": counts.get("strategy_book_snapshots", 0),
        "system_events_count": counts.get("system_events", 0),
        "latest_run_id": latest_snapshot.get("run_id"),
        "latest_run_time": latest_snapshot.get("created_at"),
        "operator_config": status.get("operator_config"),
    }


def build_operator_home(config: MaestroConfig, store: StateStore) -> dict[str, Any]:
    operator_summary = build_operator_summary(config, store)
    overview = operator_summary["overview"]
    freshness = build_freshness_table(config, store)
    health = operator_summary["health"]
    attention_items = operator_summary["attention_items"]
    blocking_items = [item for item in attention_items if item.get("severity") in {"fail", "error"}]
    stale_items = [row for row in freshness if row.get("status") in {"missing", "stale", "failed"}]
    if blocking_items:
        status = "danger"
    elif attention_items or stale_items or health.get("status") == "warn":
        status = "warning"
    else:
        status = "ok"
    return {
        "status": status,
        "mode": str(config.mode),
        "order_posture": config.execution.order_posture,
        "operator_config": overview.get("operator_config"),
        "latest_run_id": overview.get("latest_run_id"),
        "latest_run_time": overview.get("latest_run_time"),
        "attention_count": len(attention_items),
        "blocking_count": len(blocking_items),
        "stale_count": len(stale_items),
        "freshness": freshness,
        "attention_items": attention_items,
    }


def build_freshness_table(config: MaestroConfig, store: StateStore) -> list[dict[str, Any]]:
    broker_snapshot = store.load_latest_broker_account_snapshot()
    reconciliation = store.load_latest_system_event("broker_reconciliation")
    heartbeat = store.load_latest_system_event("maestro_heartbeat")
    scheduled_run = store.load_latest_system_event("run_once_completed")
    max_reconciliation_age = config.reconciliation.max_age_seconds
    timezone = operator_timezone(config)
    return [
        _freshness_row(
            "broker_snapshot",
            broker_snapshot,
            max_reconciliation_age,
            timezone=timezone,
        ),
        _freshness_row(
            "broker_reconciliation",
            reconciliation,
            max_reconciliation_age,
            timezone=timezone,
            failed=reconciliation is not None
            and reconciliation.get("payload", {}).get("passed") is False,
        ),
        _freshness_row(
            "heartbeat",
            heartbeat,
            config.monitoring.heartbeat_max_age_seconds,
            timezone=timezone,
        ),
        _freshness_row(
            "scheduled_run",
            scheduled_run,
            config.monitoring.scheduled_run_max_age_seconds,
            timezone=timezone,
        ),
    ]


def build_portfolio_table(store: StateStore) -> list[dict[str, Any]]:
    state = store.load_latest_portfolio_state()
    rows = [{"symbol": "CASH", "quantity": state.cash, "kind": "cash"}]
    rows.extend(
        {"symbol": symbol, "quantity": quantity, "kind": "position"}
        for symbol, quantity in sorted(state.positions.items())
    )
    return rows


def build_strategy_runs_table(store: StateStore, limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_strategy_runs(limit=limit):
        payload = _mapping(row.get("payload"))
        validation = _mapping(payload.get("validation"))
        result = _mapping(payload.get("result"))
        source_signal = _source_signal(payload, result)
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "strategy_id": row.get("strategy_id"),
                "signal_action": source_signal.get("action"),
                "signal_symbol": source_signal.get("symbol"),
                "rating": source_signal.get("rating"),
                "price_target": source_signal.get("price_target"),
                "stop_loss": source_signal.get("stop_loss"),
                "position_sizing": source_signal.get("position_sizing"),
                "validation_ok": validation.get("ok"),
                "validation_errors": validation.get("errors", []),
                "confidence": result.get("confidence"),
                "allocations": result.get("allocations", {}),
                "time_horizon": result.get("time_horizon"),
                "rationale": result.get("rationale"),
                "risk_flags": result.get("risk_flags", []),
                "payload": payload,
            }
        )
    return rows


def _source_signal(payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    top_level_signal = _mapping(payload.get("source_signal"))
    if top_level_signal:
        return top_level_signal
    metadata = _mapping(result.get("metadata"))
    return _mapping(metadata.get("source_signal"))


def _mapping(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def build_orders_table(store: StateStore, limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_orders(limit=limit):
        payload = row.get("payload", {})
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "order_id": row.get("order_id"),
                "symbol": payload.get("symbol"),
                "side": payload.get("side"),
                "quantity": payload.get("quantity"),
                "price": payload.get("price"),
                "notional": payload.get("notional"),
                "approval_status": payload.get("approval_status"),
                "payload": payload,
            }
        )
    return rows


def build_approvals_table(store: StateStore, limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_approvals(limit=limit):
        payload = row.get("payload", {})
        request = payload.get("request", {})
        decision = payload.get("decision", {})
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "approval_id": row.get("approval_id"),
                "status": decision.get("status"),
                "order_count": request.get("order_count"),
                "estimated_notional": request.get("estimated_notional"),
                "decided_by": decision.get("decided_by"),
                "payload": payload,
            }
        )
    return rows


def build_risk_decisions_table(store: StateStore, limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_risk_decisions(limit=limit):
        payload = row.get("payload", {})
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "approved": payload.get("approved"),
                "violations": payload.get("violations", []),
                "target_allocations": (payload.get("target") or {}).get("allocations", {}),
                "payload": payload,
            }
        )
    return rows


def build_broker_snapshots_table(store: StateStore, limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_broker_account_snapshots(limit=limit):
        payload = row.get("payload", {})
        account = payload.get("account", {})
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "account_id": row.get("account_id") or account.get("account_id"),
                "cash": account.get("cash"),
                "buying_power": account.get("buying_power"),
                "positions_count": len(account.get("positions", [])),
                "payload": payload,
            }
        )
    return rows


def build_broker_account_summary(store: StateStore) -> dict[str, Any]:
    latest = store.load_latest_broker_account_snapshot()
    if latest is None:
        return {
            "created_at": None,
            "run_id": None,
            "account_id": None,
            "cash": None,
            "buying_power": None,
            "positions_count": 0,
            "positions_market_value": None,
            "total_value": None,
            "cash_weight": None,
            "exposure_weight": None,
            "unrealized_pnl": None,
            "source": None,
        }
    payload = _mapping(latest.get("payload"))
    account = _mapping(payload.get("account"))
    positions = _positions(account)
    positions_market_value = sum(_position_market_value(position) for position in positions)
    cash = _float_or_none(account.get("cash"))
    total_value = _float_or_none(account.get("total_value"))
    if total_value is None:
        total_value = (cash or 0.0) + positions_market_value
    unrealized_pnls = [
        pnl
        for pnl in (_float_or_none(position.get("unrealized_pnl")) for position in positions)
        if pnl is not None
    ]
    return {
        "created_at": latest.get("created_at"),
        "run_id": latest.get("run_id"),
        "account_id": latest.get("account_id") or account.get("account_id"),
        "cash": cash,
        "buying_power": _float_or_none(account.get("buying_power")),
        "positions_count": len(positions),
        "positions_market_value": positions_market_value,
        "total_value": total_value,
        "cash_weight": _safe_weight(cash, total_value),
        "exposure_weight": _safe_weight(positions_market_value, total_value),
        "unrealized_pnl": sum(unrealized_pnls) if unrealized_pnls else None,
        "source": account.get("source") or payload.get("source"),
    }


def build_broker_position_exposure_table(
    store: StateStore,
    limit: int = 100,
) -> list[dict[str, Any]]:
    latest = store.load_latest_broker_account_snapshot()
    if latest is None:
        return []
    payload = _mapping(latest.get("payload"))
    account = _mapping(payload.get("account"))
    positions = _positions(account)
    total_value = _float_or_none(account.get("total_value"))
    if total_value is None:
        total_value = (_float_or_none(account.get("cash")) or 0.0) + sum(
            _position_market_value(position) for position in positions
        )
    rows = []
    for position in sorted(positions, key=lambda item: str(item.get("symbol") or "")):
        market_value = _position_market_value(position)
        rows.append(
            {
                "symbol": position.get("symbol"),
                "name": position.get("name"),
                "quantity": _float_or_none(position.get("quantity")),
                "average_price": _float_or_none(position.get("average_price")),
                "current_price": _float_or_none(position.get("current_price")),
                "market_value": market_value,
                "weight": _safe_weight(market_value, total_value),
                "unrealized_pnl": _float_or_none(position.get("unrealized_pnl")),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def build_maestro_state_exposure_table(store: StateStore) -> list[dict[str, Any]]:
    state = store.load_latest_portfolio_state()
    prices = _latest_broker_prices(store)
    rows = []
    if state.cash_by_currency:
        for currency, cash in sorted(state.cash_by_currency.items()):
            rows.append(
                {
                    "symbol": currency,
                    "kind": "cash",
                    "quantity": cash,
                    "price": 1.0,
                    "estimated_value": cash,
                    "missing_price": False,
                }
            )
    else:
        rows.append(
            {
                "symbol": "CASH",
                "kind": "cash",
                "quantity": state.cash,
                "price": 1.0,
                "estimated_value": state.cash,
                "missing_price": False,
            }
        )
    for symbol, quantity in sorted(state.positions.items()):
        price = prices.get(symbol)
        rows.append(
            {
                "symbol": symbol,
                "kind": "position",
                "quantity": quantity,
                "price": price,
                "estimated_value": quantity * price if price is not None else None,
                "missing_price": price is None,
            }
        )
    return rows


def build_portfolio_snapshot_history_table(
    store: StateStore,
    limit: int = 20,
) -> list[dict[str, Any]]:
    prices = _latest_broker_prices(store)
    rows = []
    for row in store.list_portfolio_snapshots(limit=limit):
        payload = _mapping(row.get("payload"))
        cash_by_currency = _mapping(payload.get("cash_by_currency"))
        cash = _float_or_none(payload.get("cash")) or 0.0
        cash_value = sum(_float_or_none(value) or 0.0 for value in cash_by_currency.values())
        if not cash_by_currency:
            cash_value = cash
        positions = _mapping(payload.get("positions"))
        estimated_positions_value = 0.0
        missing_prices = []
        for symbol, quantity in positions.items():
            price = prices.get(symbol)
            if price is None:
                missing_prices.append(symbol)
                continue
            estimated_positions_value += (_float_or_none(quantity) or 0.0) * price
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "cash": cash,
                "cash_value": cash_value,
                "positions_count": len(positions),
                "estimated_positions_value": estimated_positions_value,
                "estimated_total_value": None
                if missing_prices
                else cash_value + estimated_positions_value,
                "missing_prices": missing_prices,
            }
        )
    return rows


def build_broker_snapshot_history_table(
    store: StateStore,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_broker_account_snapshots(limit=limit):
        payload = _mapping(row.get("payload"))
        account = _mapping(payload.get("account"))
        positions = _positions(account)
        positions_market_value = sum(_position_market_value(position) for position in positions)
        cash = _float_or_none(account.get("cash"))
        total_value = _account_total_value(account, positions)
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "account_id": row.get("account_id") or account.get("account_id"),
                "cash": cash,
                "buying_power": _float_or_none(account.get("buying_power")),
                "positions_count": len(positions),
                "positions_market_value": positions_market_value,
                "total_value": total_value,
                "source": account.get("source") or payload.get("source"),
            }
        )
    return rows


def build_account_performance_table(
    store: StateStore,
    limit: int = 100,
) -> list[dict[str, Any]]:
    source_rows = store.list_broker_account_snapshots(limit=limit)
    reconciliation_by_snapshot_id = _reconciliation_by_snapshot_id(store)
    rows = []
    first_value = None
    previous_value = None
    peak_value = None
    cumulative_cash_flow = 0.0

    for row in reversed(source_rows):
        payload = _mapping(row.get("payload"))
        account = _mapping(payload.get("account"))
        positions = _positions(account)
        total_value = _account_total_value(account, positions)
        cash_flow = _first_float(account, payload, ("cash_flow", "net_cash_flow")) or 0.0
        period_return = None
        if previous_value is not None and previous_value > 0 and total_value is not None:
            period_return = (total_value - previous_value - cash_flow) / previous_value
        if first_value is None and total_value is not None:
            first_value = total_value
        elif total_value is not None:
            cumulative_cash_flow += cash_flow
        cumulative_return = None
        if first_value is not None and first_value > 0 and total_value is not None:
            cumulative_return = (total_value - first_value - cumulative_cash_flow) / first_value
        if total_value is not None:
            peak_value = total_value if peak_value is None else max(peak_value, total_value)
        drawdown = _safe_weight(total_value, peak_value)
        if drawdown is not None:
            drawdown -= 1.0

        reconciliation = reconciliation_by_snapshot_id.get(str(row.get("id")))
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "account_id": row.get("account_id") or account.get("account_id"),
                "currency": _snapshot_currency(account, payload),
                "total_value": total_value,
                "cash": _float_or_none(account.get("cash")),
                "positions_market_value": sum(
                    _position_market_value(position) for position in positions
                ),
                "realized_pnl": _first_float(
                    account,
                    payload,
                    ("realized_pnl", "realized_profit_loss", "realized_profit"),
                ),
                "unrealized_pnl": _account_unrealized_pnl(account, positions),
                "fees": _first_float(account, payload, ("fees", "fee", "commission")),
                "cash_flow": cash_flow,
                "period_return": _round_ratio(period_return),
                "daily_return": _round_ratio(period_return),
                "cumulative_return": _round_ratio(cumulative_return),
                "drawdown": _round_ratio(drawdown),
                "reconciliation_status": _reconciliation_status(reconciliation),
                "reconciliation_created_at": (reconciliation or {}).get("created_at"),
                "reconciliation_issues_count": (reconciliation or {}).get("issues_count"),
                "source": account.get("source") or payload.get("source"),
            }
        )
        if total_value is not None:
            previous_value = total_value
    return list(reversed(rows))


def build_currency_sleeve_performance_table(
    store: StateStore,
    limit: int = 200,
) -> list[dict[str, Any]]:
    source_rows = store.list_broker_account_snapshots(limit=limit)
    reconciliation_by_snapshot_id = _reconciliation_by_snapshot_id(store)
    state_by_currency: dict[str, dict[str, Any]] = {}
    rows = []

    for row in reversed(source_rows):
        payload = _mapping(row.get("payload"))
        account = _mapping(payload.get("account"))
        positions = _positions(account)
        currency = _snapshot_currency(account, payload)
        total_value = _account_total_value(account, positions)
        cash_flow = _first_float(account, payload, ("cash_flow", "net_cash_flow")) or 0.0
        state = state_by_currency.setdefault(
            currency,
            {
                "first_value": None,
                "previous_value": None,
                "peak_value": None,
                "cumulative_cash_flow": 0.0,
            },
        )
        performance = _advance_performance_state(state, total_value, cash_flow)
        reconciliation = reconciliation_by_snapshot_id.get(str(row.get("id")))
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "currency": currency,
                "account_id": row.get("account_id") or account.get("account_id"),
                "total_value": total_value,
                "cash": _float_or_none(account.get("cash")),
                "cash_flow": cash_flow,
                "period_return": performance["period_return"],
                "daily_return": performance["period_return"],
                "cumulative_return": performance["cumulative_return"],
                "drawdown": performance["drawdown"],
                "reconciliation_status": _reconciliation_status(reconciliation),
                "source": account.get("source") or payload.get("source"),
            }
        )
    return list(reversed(rows))


def build_total_portfolio_performance_table(
    store: StateStore,
    limit: int = 200,
    display_currency: str = "KRW",
) -> list[dict[str, Any]]:
    source_rows = store.list_broker_account_snapshots(limit=limit)
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in reversed(source_rows):
        group_key = str(row.get("run_id") or row.get("created_at") or row.get("id"))
        grouped.setdefault(group_key, []).append(row)

    reconciliation_by_snapshot_id = _reconciliation_by_snapshot_id(store)
    fx_snapshot = build_fx_rate_snapshot_card(store)
    performance_state = {
        "first_value": None,
        "previous_value": None,
        "peak_value": None,
        "cumulative_cash_flow": 0.0,
    }
    rows = []
    previous_component_values: dict[str, float] = {}
    for group_rows in grouped.values():
        component_values: dict[str, float] = {}
        component_cash_flows: dict[str, float] = {}
        created_at = group_rows[-1].get("created_at")
        run_id = group_rows[-1].get("run_id")
        reconciliation_statuses = []
        for row in group_rows:
            payload = _mapping(row.get("payload"))
            account = _mapping(payload.get("account"))
            positions = _positions(account)
            currency = _snapshot_currency(account, payload)
            total_value = _account_total_value(account, positions)
            if total_value is not None:
                component_values[currency] = component_values.get(currency, 0.0) + total_value
            cash_flow = _first_float(account, payload, ("cash_flow", "net_cash_flow")) or 0.0
            component_cash_flows[currency] = component_cash_flows.get(currency, 0.0) + cash_flow
            reconciliation = reconciliation_by_snapshot_id.get(str(row.get("id")))
            reconciliation_statuses.append(_reconciliation_status(reconciliation))

        currencies = sorted(component_values)
        converted_value = _convert_components(
            component_values,
            display_currency,
            fx_snapshot,
        )
        converted_cash_flow = _convert_components(
            component_cash_flows,
            display_currency,
            fx_snapshot,
        )
        fx_needed = any(currency != display_currency for currency in currencies)
        fx_ready = not fx_needed or fx_snapshot["status"] == "fresh"
        missing_fx = fx_needed and fx_snapshot["status"] == "missing"
        stale_fx = fx_needed and fx_snapshot["status"] == "stale"
        total_value = converted_value if fx_ready else None
        cash_flow = converted_cash_flow if fx_ready else None
        local_return = _local_component_return(component_values, previous_component_values)
        performance = _advance_performance_state(
            performance_state,
            total_value,
            cash_flow if cash_flow is not None else 0.0,
        )
        period_return = performance["period_return"]
        rows.append(
            {
                "created_at": created_at,
                "run_id": run_id,
                "currency": display_currency
                if total_value is not None
                else _portfolio_currency_label(currencies),
                "display_currency": display_currency,
                "total_value": total_value,
                "component_values": {key: component_values[key] for key in currencies},
                "missing_fx": missing_fx,
                "fx_status": fx_snapshot["status"] if fx_needed else "not_needed",
                "fx_source": fx_snapshot["source"] if fx_needed else None,
                "fx_rate": fx_snapshot["rate"],
                "fx_timestamp": fx_snapshot["as_of"],
                "stale_fx": stale_fx,
                "cash_flow": cash_flow,
                "local_return": local_return if fx_needed else period_return,
                "fx_effect": _round_ratio(period_return - local_return)
                if period_return is not None and local_return is not None and fx_needed
                else None,
                "period_return": period_return,
                "daily_return": period_return,
                "cumulative_return": performance["cumulative_return"],
                "drawdown": performance["drawdown"],
                "reconciliation_status": _combined_reconciliation_status(reconciliation_statuses),
            }
        )
        previous_component_values = component_values
    return list(reversed(rows))


def build_fx_rate_snapshot_card(store: StateStore) -> dict[str, Any]:
    latest = store.load_latest_system_event("fx_rate_snapshot")
    if latest is None:
        return {
            "status": "missing",
            "source": None,
            "as_of": None,
            "age_seconds": None,
            "max_age_seconds": None,
            "rate": None,
            "rates": {},
        }
    payload = _mapping(latest.get("payload"))
    as_of = payload.get("as_of") or payload.get("created_at") or latest.get("created_at")
    age_seconds = _age_seconds(as_of)
    max_age_seconds = int(
        payload.get("max_age_seconds") or payload.get("stale_after_seconds") or 86400
    )
    stale = age_seconds is None or age_seconds > max_age_seconds
    rates = _mapping(payload.get("rates"))
    rate = _first_float(payload, rates, ("USD/KRW", "USDKRW", "usd_krw"))
    return {
        "status": "stale" if stale else "fresh",
        "source": payload.get("source"),
        "as_of": as_of,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "rate": rate,
        "rates": rates,
    }


def build_strategy_book_snapshots_table(
    store: StateStore,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_strategy_book_snapshots(limit=limit):
        payload = _mapping(row.get("payload"))
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "strategy_id": row.get("strategy_id") or payload.get("strategy_id"),
                "book_id": row.get("book_id") or payload.get("book_id"),
                "label": payload.get("label"),
                "target_weight": _float_or_none(payload.get("target_weight")),
                "book_value": _float_or_none(payload.get("book_value")),
                "cash": _float_or_none(payload.get("cash")),
                "allocations": _mapping(payload.get("allocations")),
                "positions": _mapping(payload.get("positions")),
                "missing_prices": payload.get("missing_prices", []),
                "rationale": payload.get("rationale"),
                "payload": payload,
            }
        )
    return rows


def build_strategy_book_performance_table(
    store: StateStore,
    limit: int = 500,
) -> list[dict[str, Any]]:
    source_rows = store.list_strategy_book_snapshots(limit=limit)
    states: dict[str, dict[str, Any]] = {}
    rows = []
    for row in reversed(source_rows):
        payload = _mapping(row.get("payload"))
        book_id = str(row.get("book_id") or payload.get("book_id") or "")
        state = states.setdefault(
            book_id,
            {
                "first_value": None,
                "previous_value": None,
                "peak_value": None,
                "cumulative_cash_flow": 0.0,
            },
        )
        book_value = _float_or_none(payload.get("book_value"))
        performance = _advance_performance_state(state, book_value, 0.0)
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "strategy_id": row.get("strategy_id") or payload.get("strategy_id"),
                "book_id": book_id,
                "label": payload.get("label"),
                "target_weight": _float_or_none(payload.get("target_weight")),
                "book_value": book_value,
                "period_return": performance["period_return"],
                "cumulative_return": performance["cumulative_return"],
                "drawdown": performance["drawdown"],
                "cash": _float_or_none(payload.get("cash")),
                "allocations": _mapping(payload.get("allocations")),
            }
        )
    return list(reversed(rows))


def build_strategy_attribution_table(
    store: StateStore,
    limit: int = 100,
) -> list[dict[str, Any]]:
    signals_by_run = {
        str(row.get("run_id")): row for row in build_strategy_runs_table(store, limit=limit)
    }
    rows = []
    for row in build_strategy_book_performance_table(store, limit=limit):
        signal = signals_by_run.get(str(row.get("run_id"))) or {}
        allocations = _mapping(row.get("allocations"))
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "strategy_id": row.get("strategy_id"),
                "book_id": row.get("book_id"),
                "book_value": row.get("book_value"),
                "period_return": row.get("period_return"),
                "cumulative_return": row.get("cumulative_return"),
                "drawdown": row.get("drawdown"),
                "allocation_count": len(allocations),
                "signal_action": signal.get("signal_action"),
                "signal_symbol": signal.get("signal_symbol"),
                "confidence": signal.get("confidence"),
                "attribution_source": "strategy_book_snapshot",
            }
        )
    return rows


def build_run_index_table(store: StateStore, limit: int = 50) -> list[dict[str, Any]]:
    runs: dict[str, dict[str, Any]] = {}
    sources = (
        ("strategy_runs", store.list_strategy_runs(limit=limit)),
        ("orders", store.list_orders(limit=limit)),
        ("approvals", store.list_approvals(limit=limit)),
        ("risk_decisions", store.list_risk_decisions(limit=limit)),
        ("system_events", store.list_system_events(limit=limit)),
        ("broker_snapshots", store.list_broker_account_snapshots(limit=limit)),
        ("portfolio_snapshots", store.list_portfolio_snapshots(limit=limit)),
        ("strategy_book_snapshots", store.list_strategy_book_snapshots(limit=limit)),
    )
    for source, rows in sources:
        for row in rows:
            run_id = row.get("run_id")
            if not run_id:
                continue
            item = runs.setdefault(
                str(run_id),
                {
                    "run_id": str(run_id),
                    "latest_at": row.get("created_at"),
                    "strategy_runs": 0,
                    "orders": 0,
                    "approvals": 0,
                    "risk_decisions": 0,
                    "system_events": 0,
                    "broker_snapshots": 0,
                    "portfolio_snapshots": 0,
                    "strategy_book_snapshots": 0,
                },
            )
            item[source] += 1
            if str(row.get("created_at") or "") > str(item.get("latest_at") or ""):
                item["latest_at"] = row.get("created_at")
    return sorted(runs.values(), key=lambda item: str(item.get("latest_at") or ""), reverse=True)[
        :limit
    ]


def build_run_detail(store: StateStore, run_id: str, limit: int = 200) -> dict[str, Any]:
    def matching(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if row.get("run_id") == run_id]

    strategy_runs = matching(build_strategy_runs_table(store, limit=limit))
    orders = matching(build_orders_table(store, limit=limit))
    approvals = matching(build_approvals_table(store, limit=limit))
    risk_decisions = matching(build_risk_decisions_table(store, limit=limit))
    system_events = matching(build_system_events_table(store, limit=limit))
    broker_snapshots = matching(build_broker_snapshots_table(store, limit=limit))
    portfolio_snapshots = matching(build_portfolio_snapshot_history_table(store, limit=limit))
    strategy_books = matching(build_strategy_book_snapshots_table(store, limit=limit))
    timeline = sorted(
        [
            *_timeline_rows("strategy_run", strategy_runs),
            *_timeline_rows("order", orders),
            *_timeline_rows("approval", approvals),
            *_timeline_rows("risk_decision", risk_decisions),
            *_timeline_rows("system_event", system_events),
            *_timeline_rows("broker_snapshot", broker_snapshots),
            *_timeline_rows("portfolio_snapshot", portfolio_snapshots),
            *_timeline_rows("strategy_book_snapshot", strategy_books),
        ],
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    return {
        "run_id": run_id,
        "summary": {
            "strategy_runs": len(strategy_runs),
            "orders": len(orders),
            "approvals": len(approvals),
            "risk_decisions": len(risk_decisions),
            "system_events": len(system_events),
            "broker_snapshots": len(broker_snapshots),
            "portfolio_snapshots": len(portfolio_snapshots),
            "strategy_book_snapshots": len(strategy_books),
        },
        "timeline": timeline,
        "strategy_runs": strategy_runs,
        "orders": orders,
        "approvals": approvals,
        "risk_decisions": risk_decisions,
        "system_events": system_events,
        "broker_snapshots": broker_snapshots,
        "portfolio_snapshots": portfolio_snapshots,
        "strategy_book_snapshots": strategy_books,
    }


def build_safety_state_card(store: StateStore) -> dict[str, Any]:
    latest = store.load_latest_system_event("safety_state")
    if latest is None:
        return {
            "state": "active",
            "reason": "default",
            "source": "system",
            "updated_at": None,
        }
    payload = latest.get("payload", {})
    return {
        "state": payload.get("state", "unknown"),
        "reason": payload.get("reason"),
        "source": payload.get("source"),
        "updated_at": payload.get("updated_at") or latest.get("created_at"),
    }


def build_health_summary(config: MaestroConfig, store: StateStore) -> dict[str, Any]:
    report = HealthService(config, store).run()
    timezone = operator_timezone(config)
    counts = {"ok": 0, "warn": 0, "fail": 0}
    rows = []
    for check in report.checks:
        counts[check.status] = counts.get(check.status, 0) + 1
        rows.append(
            {
                "check": check.name,
                "status": check.status,
                "message": check.message,
                "details": add_operator_time_details(check.details, timezone),
            }
        )
    return {
        "status": report.status,
        "generated_at": report.generated_at,
        "generated_at_display": format_operator_time(report.generated_at, timezone),
        "counts": counts,
        "checks": rows,
    }


def build_operator_summary(config: MaestroConfig, store: StateStore) -> dict[str, Any]:
    safety = build_safety_state_card(store)
    health = build_health_summary(config, store)
    broker_snapshot = build_latest_broker_snapshot_card(store)
    broker_summary = build_broker_account_summary(store)
    reconciliation = build_latest_reconciliation_card(store)
    daily_live_usage = build_daily_live_order_usage(config, store)
    live_order_lifecycle = build_live_order_lifecycle_summary(store)
    halt_failure_events = build_recent_halt_failure_events_table(store, limit=5)
    overview = build_overview(store)
    return {
        "overview": overview,
        "safety": safety,
        "health": health,
        "broker_snapshot": broker_snapshot,
        "broker_snapshot_age_seconds": _age_seconds(broker_snapshot.get("created_at")),
        "broker_summary": broker_summary,
        "reconciliation": reconciliation,
        "daily_live_usage": daily_live_usage,
        "daily_live_usage_status": _daily_live_usage_status(daily_live_usage),
        "live_order_lifecycle": live_order_lifecycle,
        "recent_halt_failure_events": halt_failure_events,
        "attention_items": _operator_attention_items(
            safety=safety,
            health=health,
            reconciliation=reconciliation,
            daily_live_usage=daily_live_usage,
            live_order_lifecycle=live_order_lifecycle,
            halt_failure_events=halt_failure_events,
        ),
    }


def build_live_order_lifecycle_summary(
    store: StateStore,
    limit: int = 20,
) -> dict[str, Any]:
    rows = []
    status_counts: dict[str, int] = {}
    for row in store.list_system_events(limit=200):
        event_type = str(row.get("event_type") or "")
        if event_type not in {"live_order_lifecycle", "live_order_workflow"}:
            continue
        payload = _mapping(row.get("payload"))
        status = _live_order_summary_status(payload)
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id") or payload.get("run_id"),
                "event_type": event_type,
                "status": status,
                "order_id": payload.get("order_id"),
                "broker_order_id": payload.get("broker_order_id"),
                "poll_count": payload.get("poll_count"),
                "applied_fills": len(payload.get("applied_fills", [])),
                "max_polls_reached": payload.get("max_polls_reached"),
                "halt_reason": payload.get("halt_reason"),
                "failed_reason": payload.get("failed_reason"),
                "symbol": _live_order_summary_symbol(payload),
            }
        )
        if len(rows) >= limit:
            break
    return {
        "latest": rows[0] if rows else None,
        "recent_status_counts": status_counts,
        "recent_issue_count": sum(
            count
            for status, count in status_counts.items()
            if status in {"failed", "halted", "unknown"}
        ),
        "recent": rows,
    }


def build_latest_broker_snapshot_card(store: StateStore) -> dict[str, Any]:
    latest = store.load_latest_broker_account_snapshot()
    if latest is None:
        return {
            "created_at": None,
            "account_id": None,
            "cash": None,
            "buying_power": None,
            "positions_count": 0,
            "source": None,
        }
    payload = latest.get("payload", {})
    account = payload.get("account", {})
    return {
        "created_at": latest.get("created_at"),
        "account_id": latest.get("account_id") or account.get("account_id"),
        "cash": account.get("cash"),
        "buying_power": account.get("buying_power"),
        "positions_count": len(account.get("positions", [])),
        "source": account.get("source") or payload.get("source"),
    }


def build_latest_reconciliation_card(store: StateStore) -> dict[str, Any]:
    latest = store.load_latest_system_event("broker_reconciliation")
    if latest is None:
        return {
            "created_at": None,
            "passed": None,
            "issues_count": 0,
            "cash_difference": None,
            "broker_account_id": None,
        }
    payload = latest.get("payload", {})
    return {
        "created_at": latest.get("created_at"),
        "passed": payload.get("passed"),
        "issues_count": len(payload.get("issues", [])),
        "cash_difference": payload.get("cash_difference"),
        "broker_account_id": payload.get("broker_account_id"),
    }


def build_recent_halt_failure_events_table(
    store: StateStore,
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_system_events(limit=100):
        event_type = str(row.get("event_type") or "")
        payload = row.get("payload", {})
        if not _is_halt_or_failure_event(event_type, payload):
            continue
        rows.append(_event_row(row))
        if len(rows) >= limit:
            break
    return rows


def build_live_order_events_table(store: StateStore, limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_system_events(limit=200):
        event_type = row.get("event_type")
        if event_type not in {"live_order_status", "live_order_lifecycle"}:
            continue
        payload = row.get("payload", {})
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "event_type": event_type,
                "status": payload.get("status")
                or (payload.get("snapshot") or {}).get("status")
                or (payload.get("result") or {}).get("status"),
                "symbol": payload.get("symbol")
                or (payload.get("snapshot") or {}).get("symbol")
                or (payload.get("request") or {}).get("symbol"),
                "message": payload.get("message") or payload.get("failed_reason"),
                "payload": payload,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def build_fill_reconciliation_table(store: StateStore, limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_system_events_by_type("fill_reconciliation", limit=limit):
        payload = row.get("payload", {})
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "applied_fills": len(payload.get("applied_fills", [])),
                "skipped_fills": len(payload.get("skipped_fills", [])),
                "portfolio_updated": payload.get("portfolio_updated"),
                "cash": payload.get("cash"),
                "positions_count": len(payload.get("positions", {})),
                "payload": payload,
            }
        )
    return rows


def build_daily_live_order_usage(config: MaestroConfig, store: StateStore) -> dict[str, Any]:
    today = utc_now().date().isoformat()
    count = 0
    notional = 0.0
    for row in store.list_system_events_by_type("live_order_result", limit=1000):
        payload = row.get("payload", {})
        if payload.get("submitted_date") != today:
            continue
        count += 1
        notional += float(payload.get("notional", 0.0))
    return {
        "date": today,
        "order_count": count,
        "max_daily_live_order_count": config.execution.live_order_limits.max_daily_order_count,
        "notional": notional,
        "max_daily_live_notional": config.execution.live_order_limits.max_daily_notional,
    }


def build_system_events_table(store: StateStore, limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_system_events(limit=limit):
        payload = row.get("payload", {})
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "event_type": row.get("event_type"),
                "error_type": payload.get("error_type"),
                "error_message": payload.get("error_message") or payload.get("error"),
                "payload": payload,
            }
        )
    return rows


def _operator_attention_items(
    *,
    safety: dict[str, Any],
    health: dict[str, Any],
    reconciliation: dict[str, Any],
    daily_live_usage: dict[str, Any],
    live_order_lifecycle: dict[str, Any],
    halt_failure_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items = []
    safety_state = str(safety.get("state") or "unknown")
    if safety_state != "active":
        items.append(
            {
                "severity": "fail",
                "code": "safety_not_active",
                "message": f"Safety state is {safety_state}.",
                "details": {
                    "reason": safety.get("reason"),
                    "updated_at": safety.get("updated_at"),
                },
            }
        )

    health_status = str(health.get("status") or "unknown")
    if health_status != "ok":
        items.append(
            {
                "severity": "fail" if health_status == "fail" else "warn",
                "code": "health_not_ok",
                "message": f"Health status is {health_status}.",
                "details": health.get("counts", {}),
            }
        )

    if reconciliation.get("passed") is False:
        items.append(
            {
                "severity": "fail",
                "code": "reconciliation_failed",
                "message": "Latest broker reconciliation failed.",
                "details": {
                    "created_at": reconciliation.get("created_at"),
                    "issues_count": reconciliation.get("issues_count"),
                    "cash_difference": reconciliation.get("cash_difference"),
                },
            }
        )

    usage_status = _daily_live_usage_status(daily_live_usage)
    for key, code in (
        ("order_count", "daily_live_order_count"),
        ("notional", "daily_live_notional"),
    ):
        status = usage_status[key]["status"]
        if status == "not_configured" or status == "ok":
            continue
        items.append(
            {
                "severity": "fail" if status == "limit" else "warn",
                "code": f"{code}_{status}",
                "message": usage_status[key]["message"],
                "details": usage_status[key],
            }
        )

    latest_lifecycle = live_order_lifecycle.get("latest") or {}
    latest_status = latest_lifecycle.get("status")
    recent_issue_count = int(live_order_lifecycle.get("recent_issue_count") or 0)
    if latest_status in {"failed", "halted", "unknown"} or recent_issue_count:
        items.append(
            {
                "severity": "fail",
                "code": "recent_live_order_issue",
                "message": f"Recent live order lifecycle issues: {recent_issue_count}.",
                "details": {
                    "latest": latest_lifecycle,
                    "recent_issue_count": recent_issue_count,
                },
            }
        )

    if halt_failure_events:
        latest_event = halt_failure_events[0]
        items.append(
            {
                "severity": "warn",
                "code": "recent_halt_failure_event",
                "message": f"Recent halt/failure event: {latest_event.get('event_type')}.",
                "details": {
                    "created_at": latest_event.get("created_at"),
                    "run_id": latest_event.get("run_id"),
                    "reason": latest_event.get("reason"),
                    "error_type": latest_event.get("error_type"),
                },
            }
        )
    return items


def _daily_live_usage_status(daily_live_usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "order_count": _limit_status(
            daily_live_usage.get("order_count"),
            daily_live_usage.get("max_daily_live_order_count"),
            "Daily live order count",
        ),
        "notional": _limit_status(
            daily_live_usage.get("notional"),
            daily_live_usage.get("max_daily_live_notional"),
            "Daily live notional",
        ),
    }


def _limit_status(value: object, limit: object, label: str) -> dict[str, Any]:
    current = _float_or_none(value) or 0.0
    maximum = _float_or_none(limit) or 0.0
    if maximum <= 0:
        return {
            "status": "not_configured",
            "value": current,
            "limit": maximum,
            "ratio": None,
            "message": f"{label} limit is not configured.",
        }
    ratio = current / maximum
    if ratio >= 1:
        status = "limit"
    elif ratio >= 0.8:
        status = "near_limit"
    else:
        status = "ok"
    return {
        "status": status,
        "value": current,
        "limit": maximum,
        "ratio": ratio,
        "message": f"{label} is {current:g} / {maximum:g}.",
    }


def _live_order_summary_status(payload: dict[str, Any]) -> str | None:
    status = (
        payload.get("final_status")
        or payload.get("workflow_status")
        or payload.get("status")
        or _mapping(payload.get("snapshot")).get("status")
        or _mapping(payload.get("status_snapshot")).get("status")
    )
    if status is None:
        return None
    return str(status)


def _live_order_summary_symbol(payload: dict[str, Any]) -> str | None:
    symbol = (
        payload.get("symbol")
        or _mapping(payload.get("request")).get("symbol")
        or _mapping(payload.get("snapshot")).get("symbol")
        or _mapping(payload.get("status_snapshot")).get("symbol")
    )
    if symbol is not None:
        return str(symbol)
    snapshots = payload.get("status_snapshots")
    if isinstance(snapshots, list):
        for snapshot in snapshots:
            symbol = _mapping(snapshot).get("symbol")
            if symbol is not None:
                return str(symbol)
    return None


def _age_seconds(value: object) -> float | None:
    if not value:
        return None
    if isinstance(value, datetime):
        created_at = value
    else:
        raw = str(value)
        try:
            created_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                created_at = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return max((utc_now() - created_at).total_seconds(), 0.0)


def _freshness_row(
    name: str,
    row: dict[str, Any] | None,
    max_age_seconds: int,
    *,
    timezone: str = "UTC",
    failed: bool = False,
) -> dict[str, Any]:
    if max_age_seconds <= 0:
        return {
            "name": name,
            "status": "not_configured",
            "created_at": None,
            "created_at_display": None,
            "age_seconds": None,
            "max_age_seconds": max_age_seconds,
            "run_id": None,
        }
    if row is None:
        return {
            "name": name,
            "status": "missing",
            "created_at": None,
            "created_at_display": None,
            "age_seconds": None,
            "max_age_seconds": max_age_seconds,
            "run_id": None,
        }
    age_seconds = _age_seconds(row.get("created_at"))
    status = "failed" if failed else "fresh"
    if age_seconds is None or age_seconds > max_age_seconds:
        status = "stale"
    return {
        "name": name,
        "status": status,
        "created_at": row.get("created_at"),
        "created_at_display": format_operator_time(row.get("created_at"), timezone),
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "run_id": row.get("run_id"),
    }


def _convert_components(
    values: dict[str, float],
    display_currency: str,
    fx_snapshot: dict[str, Any],
) -> float | None:
    total = 0.0
    for currency, value in values.items():
        if currency == display_currency:
            total += value
            continue
        rate = _fx_rate(currency, display_currency, fx_snapshot)
        if rate is None or fx_snapshot.get("status") != "fresh":
            return None
        total += value * rate
    return total


def _fx_rate(
    source_currency: str,
    target_currency: str,
    fx_snapshot: dict[str, Any],
) -> float | None:
    if source_currency == target_currency:
        return 1.0
    rates = _mapping(fx_snapshot.get("rates"))
    direct_keys = (
        f"{source_currency}/{target_currency}",
        f"{source_currency}{target_currency}",
        f"{source_currency.lower()}_{target_currency.lower()}",
    )
    for key in direct_keys:
        rate = _float_or_none(rates.get(key))
        if rate is not None:
            return rate
    inverse_keys = (
        f"{target_currency}/{source_currency}",
        f"{target_currency}{source_currency}",
        f"{target_currency.lower()}_{source_currency.lower()}",
    )
    for key in inverse_keys:
        rate = _float_or_none(rates.get(key))
        if rate is not None and rate != 0:
            return 1 / rate
    return None


def _local_component_return(
    component_values: dict[str, float],
    previous_component_values: dict[str, float],
) -> float | None:
    if not previous_component_values:
        return None
    previous_total = sum(previous_component_values.values())
    if previous_total <= 0:
        return None
    weighted_return = 0.0
    has_value = False
    for currency, previous_value in previous_component_values.items():
        current_value = component_values.get(currency)
        if current_value is None or previous_value <= 0:
            continue
        weighted_return += (previous_value / previous_total) * (
            (current_value - previous_value) / previous_value
        )
        has_value = True
    return _round_ratio(weighted_return) if has_value else None


def _portfolio_currency_label(currencies: list[str]) -> str:
    if not currencies:
        return "UNKNOWN"
    if len(currencies) == 1:
        return currencies[0]
    return "MIXED"


def _timeline_rows(kind: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        output.append(
            {
                "created_at": row.get("created_at"),
                "kind": kind,
                "run_id": row.get("run_id"),
                "status": row.get("status")
                or row.get("approved")
                or row.get("validation_ok")
                or row.get("reconciliation_status"),
                "symbol": row.get("symbol") or row.get("signal_symbol"),
                "summary": row.get("event_type")
                or row.get("order_id")
                or row.get("approval_id")
                or row.get("strategy_id")
                or row.get("account_id")
                or row.get("book_id"),
            }
        )
    return output


def _event_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = row.get("payload", {})
    return {
        "created_at": row.get("created_at"),
        "run_id": row.get("run_id"),
        "event_type": row.get("event_type"),
        "state": payload.get("state"),
        "reason": payload.get("reason"),
        "error_type": payload.get("error_type"),
        "error_message": payload.get("error_message") or payload.get("error"),
        "payload": payload,
    }


def _is_halt_or_failure_event(event_type: str, payload: dict[str, Any]) -> bool:
    if "halt" in event_type or "failure" in event_type or "failed" in event_type:
        return True
    return payload.get("state") in {"halted", "killed"}


def _positions(account: dict[str, Any]) -> list[dict[str, Any]]:
    return [position for position in account.get("positions", []) if isinstance(position, dict)]


def _position_market_value(position: dict[str, Any]) -> float:
    market_value = _float_or_none(position.get("market_value"))
    if market_value is not None:
        return market_value
    quantity = _float_or_none(position.get("quantity")) or 0.0
    current_price = _float_or_none(position.get("current_price")) or 0.0
    return quantity * current_price


def _account_total_value(account: dict[str, Any], positions: list[dict[str, Any]]) -> float | None:
    cash_balance = _mapping(account.get("cash_balance"))
    total_value = _first_float(
        cash_balance,
        {},
        ("total_asset_value", "total_value", "total_equity", "net_asset_value"),
    )
    if total_value is not None:
        return total_value
    total_value = _first_float(
        account,
        {},
        ("total_value", "total_equity", "equity", "net_asset_value"),
    )
    if total_value is not None:
        return total_value
    cash = _float_or_none(account.get("cash"))
    if cash is None and not positions:
        return None
    return (cash or 0.0) + sum(_position_market_value(position) for position in positions)


def _account_unrealized_pnl(
    account: dict[str, Any],
    positions: list[dict[str, Any]],
) -> float | None:
    unrealized_pnl = _first_float(
        account,
        {},
        ("unrealized_pnl", "unrealized_profit_loss", "evaluation_profit_loss"),
    )
    if unrealized_pnl is not None:
        return unrealized_pnl
    values = [
        value
        for value in (_float_or_none(position.get("unrealized_pnl")) for position in positions)
        if value is not None
    ]
    if not values:
        return None
    return sum(values)


def _snapshot_currency(account: dict[str, Any], payload: dict[str, Any]) -> str:
    currency = account.get("currency") or payload.get("currency")
    if currency is not None:
        return str(currency)
    cash_balance = _mapping(account.get("cash_balance"))
    currency = cash_balance.get("currency")
    if currency is not None:
        return str(currency)
    cash_by_currency = _mapping(account.get("cash_by_currency"))
    if len(cash_by_currency) == 1:
        return str(next(iter(cash_by_currency)))
    return "UNKNOWN"


def _advance_performance_state(
    state: dict[str, Any],
    total_value: float | None,
    cash_flow: float,
) -> dict[str, float | None]:
    period_return = None
    previous_value = state["previous_value"]
    if previous_value is not None and previous_value > 0 and total_value is not None:
        period_return = (total_value - previous_value - cash_flow) / previous_value
    if state["first_value"] is None and total_value is not None:
        state["first_value"] = total_value
    elif total_value is not None:
        state["cumulative_cash_flow"] += cash_flow
    cumulative_return = None
    first_value = state["first_value"]
    if first_value is not None and first_value > 0 and total_value is not None:
        cumulative_return = (
            total_value - first_value - state["cumulative_cash_flow"]
        ) / first_value
    if total_value is not None:
        state["peak_value"] = (
            total_value if state["peak_value"] is None else max(state["peak_value"], total_value)
        )
        state["previous_value"] = total_value
    drawdown = _safe_weight(total_value, state["peak_value"])
    if drawdown is not None:
        drawdown -= 1.0
    return {
        "period_return": _round_ratio(period_return),
        "cumulative_return": _round_ratio(cumulative_return),
        "drawdown": _round_ratio(drawdown),
    }


def _first_float(
    primary: dict[str, Any],
    secondary: dict[str, Any],
    keys: tuple[str, ...],
) -> float | None:
    for key in keys:
        value = _float_or_none(primary.get(key))
        if value is not None:
            return value
        value = _float_or_none(secondary.get(key))
        if value is not None:
            return value
    return None


def _reconciliation_by_snapshot_id(store: StateStore) -> dict[str, dict[str, Any]]:
    output = {}
    for row in store.list_system_events_by_type("broker_reconciliation", limit=1000):
        payload = _mapping(row.get("payload"))
        snapshot_id = payload.get("broker_snapshot_id") or payload.get("snapshot_id")
        if snapshot_id is None:
            continue
        output[str(snapshot_id)] = {
            "created_at": row.get("created_at"),
            "passed": payload.get("passed"),
            "issues_count": len(payload.get("issues", [])),
        }
    return output


def _reconciliation_status(reconciliation: dict[str, Any] | None) -> str:
    if reconciliation is None:
        return "unreconciled"
    if reconciliation.get("passed") is True:
        return "passed"
    if reconciliation.get("passed") is False:
        return "failed"
    return "unknown"


def _combined_reconciliation_status(statuses: list[str]) -> str:
    if not statuses:
        return "unreconciled"
    if "failed" in statuses:
        return "failed"
    if "unknown" in statuses:
        return "unknown"
    if "unreconciled" in statuses:
        return "unreconciled"
    return "passed"


def _round_ratio(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 10)


def _safe_weight(value: float | None, total: float | None) -> float | None:
    if value is None or total is None or total <= 0:
        return None
    return value / total


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest_broker_prices(store: StateStore) -> dict[str, float]:
    latest = store.load_latest_broker_account_snapshot()
    if latest is None:
        return {}
    payload = _mapping(latest.get("payload"))
    prices = {
        str(symbol): price
        for symbol, value in _mapping(payload.get("current_prices")).items()
        if (price := _float_or_none(value)) is not None
    }
    account = _mapping(payload.get("account"))
    for position in _positions(account):
        symbol = position.get("symbol")
        price = _float_or_none(position.get("current_price"))
        if symbol and price is not None:
            prices.setdefault(str(symbol), price)
    return prices

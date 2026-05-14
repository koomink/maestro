from typing import Any

from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
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
        "system_events_count": counts.get("system_events", 0),
        "latest_run_id": latest_snapshot.get("run_id"),
        "latest_run_time": latest_snapshot.get("created_at"),
    }


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
                "modifications": payload.get("modifications", []),
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
        total_value = _float_or_none(account.get("total_value"))
        if total_value is None:
            total_value = (cash or 0.0) + positions_market_value
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
    counts = {"ok": 0, "warn": 0, "fail": 0}
    rows = []
    for check in report.checks:
        counts[check.status] = counts.get(check.status, 0) + 1
        rows.append(
            {
                "check": check.name,
                "status": check.status,
                "message": check.message,
                "details": check.details,
            }
        )
    return {
        "status": report.status,
        "generated_at": report.generated_at,
        "counts": counts,
        "checks": rows,
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
        "max_daily_live_order_count": config.execution.max_daily_live_order_count,
        "notional": notional,
        "max_daily_live_notional": config.execution.max_daily_live_notional,
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
    return [
        position
        for position in account.get("positions", [])
        if isinstance(position, dict)
    ]


def _position_market_value(position: dict[str, Any]) -> float:
    market_value = _float_or_none(position.get("market_value"))
    if market_value is not None:
        return market_value
    quantity = _float_or_none(position.get("quantity")) or 0.0
    current_price = _float_or_none(position.get("current_price")) or 0.0
    return quantity * current_price


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

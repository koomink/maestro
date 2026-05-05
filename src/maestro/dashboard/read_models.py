from typing import Any

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
        payload = row.get("payload", {})
        validation = payload.get("validation", {})
        result = payload.get("result", {})
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "strategy_id": row.get("strategy_id"),
                "validation_ok": validation.get("ok"),
                "validation_errors": validation.get("errors", []),
                "confidence": result.get("confidence"),
                "allocations": result.get("allocations", {}),
                "payload": payload,
            }
        )
    return rows


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

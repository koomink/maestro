from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.time_display import (
    add_operator_time_details,
    format_operator_time,
    operator_timezone,
)
from maestro.dashboard.actions import build_signal_freshness
from maestro.monitoring.health import HealthService, latest_scheduled_run_event
from maestro.state.events import (
    EXTERNAL_TRANSFER,
    FX_CONVERSION,
    INTERNAL_TRANSFER,
    LINKED_FLOW_CLASSES,
    SystemEventType,
    missing_system_event_required_fields,
    required_system_event_fields,
)
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
    scheduled_run = latest_scheduled_run_event(store)
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
                "account_id": payload.get("account_id"),
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


def build_signal_freshness_card(
    store: StateStore,
    *,
    max_age_seconds: int,
) -> dict[str, Any]:
    return build_signal_freshness(store, max_age_seconds=max_age_seconds)


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
                "account_id": payload.get("account_id"),
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
                "account_id": payload.get("account_id"),
                "account_ids": payload.get("account_ids", []),
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
                "account_id": payload.get("account_id"),
                "account_ids": payload.get("account_ids", []),
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
                "account_id": payload.get("account_id")
                or row.get("account_id")
                or account.get("account_id"),
                "broker_account_id": payload.get("broker_account_id") or account.get("account_id"),
                "cash": account.get("cash"),
                "buying_power": account.get("buying_power"),
                "positions_count": len(account.get("positions", [])),
                "payload": payload,
            }
        )
    return rows


def _disabled_native_account_ids(config: MaestroConfig | None) -> set[str]:
    """Native identifiers (config `id` and, when set, the literal
    `account_id`) of accounts that are explicitly disabled in the config.

    Broker snapshots are never deleted, so a disabled/retired account (e.g. a
    mock/paper account used during setup) keeps its last snapshot in the
    state DB forever. Without this filter, aggregation functions that "carry
    forward the latest snapshot per account" would keep including that
    account's stale value indefinitely, silently inflating every total.

    This is a DENY-list (only positively-identified disabled accounts are
    excluded) rather than an allow-list (only positively-identified enabled
    accounts pass), which matters in practice: many configs — real ones
    using `account_id_env` instead of a literal `account_id`, and test
    fixtures that hand-write a broker snapshot with an ad hoc account_id
    string unrelated to any config field — have no reliable way to prove a
    snapshot "belongs" to a specific enabled account. An allow-list would
    wrongly exclude those as unmatched; a deny-list only ever excludes a
    snapshot when it's a confirmed match for an account we positively know
    is disabled, so ambiguous/unmatched snapshots safely pass through
    exactly as they did before this filter existed.

    Unlike `build_broker_account_overview` (which also excludes
    `broker == "sandbox"` for its "connected real broker accounts" display),
    this only checks `enabled` — sandbox/paper accounts are legitimate data
    sources for other features (e.g. voluntary-deposit cash-flow detection
    on a paper account), so excluding them here would be wrong.
    """
    if config is None:
        return set()
    disabled: set[str] = set()
    for account in getattr(config, "accounts", None) or []:
        if getattr(account, "enabled", False):
            continue
        disabled.add(account.id)
        # Snapshots are usually tagged with the account's logical config id
        # (e.g. "kis_mock"), but some configs set a literal `account_id`
        # that snapshots could be keyed by instead. Excluding on either
        # avoids missing a disabled account just because of how a snapshot
        # happened to be tagged.
        literal_account_id = getattr(account, "account_id", None)
        if literal_account_id:
            disabled.add(str(literal_account_id))
    return disabled


def _latest_broker_snapshots_by_account(
    store: StateStore,
    config: MaestroConfig | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    disabled_ids = _disabled_native_account_ids(config)
    latest_by_account = []
    seen = set()
    for snapshot in store.list_broker_account_snapshots(limit=limit):
        account_id = _broker_snapshot_account_id(snapshot)
        if not account_id or account_id in seen:
            continue
        if account_id in disabled_ids:
            continue
        seen.add(account_id)
        latest_by_account.append(snapshot)
    return latest_by_account


def _broker_snapshot_account_id(snapshot: dict[str, Any]) -> str:
    payload = _mapping(snapshot.get("payload"))
    account = _mapping(payload.get("account"))
    return str(
        payload.get("account_id")
        or snapshot.get("account_id")
        or account.get("account_id")
        or ""
    )


def build_broker_account_summary(
    store: StateStore,
    config: MaestroConfig | None = None,
) -> dict[str, Any]:
    latest_snapshots = _latest_broker_snapshots_by_account(store, config)
    if not latest_snapshots:
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
    latest = latest_snapshots[0]
    # Positions and account totals are summed in their OWN currency first and
    # only converted to `display_currency` at the end. Brokers such as Toss
    # report a single account holding both KRW and USD-listed instruments
    # with no pre-aggregated total; summing raw prices across accounts
    # without this split would silently mix units.
    display_currency = "KRW"
    fx_snapshot = build_fx_rate_snapshot_card(store)
    cash_values = []
    buying_power_values = []
    total_value_components: dict[str, float] = {}
    positions_value_components: dict[str, float] = {}
    all_positions = []
    unrealized_pnls = []
    sources = []
    for snapshot in latest_snapshots:
        payload = _mapping(snapshot.get("payload"))
        account = _mapping(payload.get("account"))
        positions = _positions(account)
        all_positions.extend(positions)
        cash = _float_or_none(account.get("cash"))
        if cash is not None:
            cash_values.append(cash)
        buying_power = _float_or_none(account.get("buying_power"))
        if buying_power is not None:
            buying_power_values.append(buying_power)
        for currency, value in _account_value_components(
            account, positions, payload, default_currency=display_currency
        ).items():
            total_value_components[currency] = total_value_components.get(currency, 0.0) + value
        unrealized_pnls.extend(
            pnl
            for pnl in (_float_or_none(position.get("unrealized_pnl")) for position in positions)
            if pnl is not None
        )
        source = account.get("source") or payload.get("source")
        if source:
            sources.append(str(source))

    for position in all_positions:
        currency = str(position.get("currency") or display_currency)
        positions_value_components[currency] = positions_value_components.get(
            currency, 0.0
        ) + _position_market_value(position)

    positions_market_value = (
        _convert_components(positions_value_components, display_currency, fx_snapshot)
        if positions_value_components
        else None
    )
    cash = sum(cash_values) if cash_values else None
    total_value = (
        _convert_components(total_value_components, display_currency, fx_snapshot)
        if total_value_components
        else None
    )
    account_id = _broker_snapshot_account_id(latest)
    return {
        "created_at": latest.get("created_at"),
        "run_id": latest.get("run_id"),
        "account_id": "multiple" if len(latest_snapshots) > 1 else account_id,
        "cash": cash,
        "buying_power": sum(buying_power_values) if buying_power_values else None,
        "positions_count": len(all_positions),
        "positions_market_value": positions_market_value,
        "total_value": total_value,
        "cash_weight": _safe_weight(cash, total_value),
        "exposure_weight": _safe_weight(positions_market_value, total_value),
        "unrealized_pnl": sum(unrealized_pnls) if unrealized_pnls else None,
        "source": (
            "broker_account_aggregate"
            if len(latest_snapshots) > 1
            else sources[0]
            if sources
            else None
        ),
    }


def build_broker_account_overview(config: MaestroConfig, store: StateStore) -> dict[str, Any]:
    max_age_seconds = config.reconciliation.observation_snapshot_max_age_seconds
    timezone = operator_timezone(config)
    latest_by_account: dict[str, dict[str, Any]] = {}
    latest_refresh_by_account: dict[str, dict[str, Any]] = {}
    for event in store.list_system_events_by_type(
        SystemEventType.BROKER_READONLY_REFRESH,
        limit=1000,
    ):
        account_id = str(_mapping(event.get("payload")).get("account_id") or "")
        if account_id and account_id not in latest_refresh_by_account:
            latest_refresh_by_account[account_id] = event
    for snapshot in store.list_broker_account_snapshots(limit=1000):
        payload = _mapping(snapshot.get("payload"))
        account = _mapping(payload.get("account"))
        account_id = str(
            payload.get("account_id")
            or snapshot.get("account_id")
            or account.get("account_id")
            or ""
        )
        if not account_id or account_id in latest_by_account:
            continue
        latest_by_account[account_id] = snapshot

    accounts = [
        account
        for account in config.accounts
        if getattr(account, "enabled", False) and getattr(account, "broker", None) != "sandbox"
    ]
    rows = [
        _broker_account_overview_row(
            account,
            latest_by_account.get(account.id),
            max_age_seconds,
            timezone,
            latest_refresh_by_account.get(account.id),
        )
        for account in accounts
    ]
    fresh_count = sum(1 for row in rows if row["status"] == "fresh")
    stale_count = sum(1 for row in rows if row["status"] == "stale")
    missing_count = sum(1 for row in rows if row["status"] == "missing")
    quarantined_count = sum(1 for row in rows if row["status"] == "quarantined")
    attention_count = stale_count + missing_count + quarantined_count
    totals_by_currency: dict[str, float] = {}
    latest_sync_at = None
    for row in rows:
        total_value = _float_or_none(row.get("total_value"))
        currency = row.get("currency")
        if total_value is not None and currency:
            current_total = totals_by_currency.get(str(currency), 0.0)
            totals_by_currency[str(currency)] = current_total + total_value
        created_at = row.get("created_at")
        if created_at and (latest_sync_at is None or str(created_at) > str(latest_sync_at)):
            latest_sync_at = created_at
    currency = (
        next(iter(totals_by_currency), None)
        if len(totals_by_currency) == 1
        else "mixed"
        if totals_by_currency
        else None
    )
    total_value = (
        next(iter(totals_by_currency.values()), None)
        if len(totals_by_currency) == 1
        else None
    )
    summary = {
        "configured_accounts": len(rows),
        "fresh_accounts": fresh_count,
        "stale_accounts": stale_count,
        "missing_accounts": missing_count,
        "quarantined_accounts": quarantined_count,
        "attention_accounts": attention_count,
        "total_value": total_value,
        "currency": currency,
        "totals_by_currency": totals_by_currency,
        "latest_sync_at": latest_sync_at,
        "latest_sync_at_display": (
            format_operator_time(latest_sync_at, timezone) if latest_sync_at else None
        ),
    }
    return {
        "summary": summary,
        "metrics": [
            {
                "label": "Accounts",
                "value": f"{fresh_count}/{len(rows)} fresh",
                "tone": "success" if attention_count == 0 else "warning",
            },
            {
                "label": "Attention",
                "value": attention_count,
                "tone": "success" if attention_count == 0 else "warning",
            },
            {
                "label": "Broker Value",
                "value": total_value if total_value is not None else currency or "n/a",
                "tone": "neutral",
            },
            {
                "label": "Last Sync",
                "value": summary["latest_sync_at_display"] or "n/a",
                "tone": "neutral",
            },
        ],
        "accounts": rows,
    }


def _broker_account_overview_row(
    account_config: Any,
    snapshot: dict[str, Any] | None,
    max_age_seconds: int,
    timezone: str,
    refresh_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    refresh_payload = _mapping((refresh_event or {}).get("payload"))
    if snapshot is None:
        return {
            "account_id": account_config.id,
            "broker_account_id": None,
            "broker": account_config.broker,
            "environment": account_config.environment,
            "status": "missing",
            "tone": "warning",
            "created_at": None,
            "created_at_display": None,
            "age_seconds": None,
            "max_age_seconds": max_age_seconds,
            "currency": None,
            "total_value": None,
            "cash": None,
            "buying_power": None,
            "positions_count": 0,
            "source": None,
            "run_id": None,
            "last_refresh_status": refresh_payload.get("status"),
            "last_refresh_error": refresh_payload.get("error_message"),
        }
    payload = _mapping(snapshot.get("payload"))
    account = _mapping(payload.get("account"))
    positions = _positions(account)
    age_seconds = _age_seconds(snapshot.get("created_at"))
    status = (
        "stale"
        if age_seconds is None or (max_age_seconds > 0 and age_seconds > max_age_seconds)
        else "fresh"
    )
    if refresh_payload.get("status") == "quarantined":
        status = "quarantined"
    return {
        "account_id": account_config.id,
        "broker_account_id": payload.get("broker_account_id") or account.get("account_id"),
        "broker": account_config.broker,
        "environment": account_config.environment,
        "status": status,
        "tone": "success" if status == "fresh" else "warning",
        "created_at": snapshot.get("created_at"),
        "created_at_display": format_operator_time(snapshot.get("created_at"), timezone),
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "currency": account.get("currency")
        or _mapping(account.get("cash_balance")).get("currency"),
        "total_value": _account_total_value(account, positions),
        "cash": _float_or_none(account.get("cash")),
        "buying_power": _float_or_none(account.get("buying_power")),
        "positions_count": len(positions),
        "source": account.get("source") or payload.get("source"),
        "run_id": snapshot.get("run_id"),
        "last_refresh_status": refresh_payload.get("status"),
        "last_refresh_error": refresh_payload.get("error_message"),
    }


def build_broker_position_exposure_table(
    store: StateStore,
    config: MaestroConfig | None = None,
    limit: int = 100,
    display_currency: str = "KRW",
) -> list[dict[str, Any]]:
    latest_snapshots = _latest_broker_snapshots_by_account(store, config)
    if not latest_snapshots:
        return []
    fx_snapshot = build_fx_rate_snapshot_card(store)
    positions_by_account = []
    # Weight has to be measured against the same total the NAV uses. Summing
    # raw per-account totals mixed units: a Toss account reporting KRW cash
    # alongside USD holdings produced a denominator that excluded the USD leg,
    # so USD positions were divided by a KRW-only total and reported far too
    # small (a 21% holding showed as 0.04%).
    components: dict[str, float] = {}
    for snapshot in latest_snapshots:
        payload = _mapping(snapshot.get("payload"))
        account = _mapping(payload.get("account"))
        positions = _positions(account)
        account_id = _broker_snapshot_account_id(snapshot)
        positions_by_account.extend(
            (account_id, position, account, payload) for position in positions
        )
        for currency, value in _account_value_components(account, positions, payload).items():
            components[currency] = components.get(currency, 0.0) + value
    total_value = _convert_components(components, display_currency, fx_snapshot)
    rows = []
    for account_id, position, account, payload in sorted(
        positions_by_account,
        key=lambda item: (item[0], str(item[1].get("symbol") or "")),
    ):
        market_value = _position_market_value(position)
        currency = _position_currency(position, account, payload)
        rate = _fx_rate(currency, display_currency, fx_snapshot)
        if currency != display_currency and fx_snapshot.get("status") != "fresh":
            # Same rule as _convert_components: a stale rate must not silently
            # produce a converted weight.
            rate = None
        converted_value = None if rate is None else market_value * rate
        rows.append(
            {
                "account_id": account_id,
                "symbol": position.get("symbol"),
                "name": position.get("name"),
                "quantity": _float_or_none(position.get("quantity")),
                "average_price": _float_or_none(position.get("average_price")),
                "current_price": _float_or_none(position.get("current_price")),
                "market_value": market_value,
                "currency": currency,
                "weight": _safe_weight(converted_value, total_value),
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
                "performance_status": "legacy",
            }
        )
    return rows


def _account_currency_aware_total(
    account: dict[str, Any],
    positions: list[dict[str, Any]],
    payload: dict[str, Any],
    fx_snapshot: dict[str, Any],
    display_currency: str,
) -> tuple[float | None, str, float | None]:
    """Returns (total_value, currency_label, cash) for one account snapshot.

    Single-currency accounts pass through unconverted (no FX dependency, and
    labeled with their OWN currency) — only a genuinely mixed-currency
    account (e.g. Toss holding both KRW cash and USD-listed positions under
    one account, with no broker-reported aggregate) needs FX to combine its
    components into one number; that case is labeled `display_currency`.
    """
    value_components = _account_value_components(
        account, positions, payload, default_currency=display_currency
    )
    cash_components = _account_cash_components(account, payload, default_currency=display_currency)
    if len(value_components) <= 1:
        currency = next(iter(value_components), display_currency)
        return value_components.get(currency), currency, cash_components.get(currency)
    total_value = _convert_components(value_components, display_currency, fx_snapshot)
    cash = _convert_components(cash_components, display_currency, fx_snapshot)
    return total_value, display_currency, cash


def _ledger_account_snapshot_rows(store: StateStore) -> list[dict[str, Any]]:
    """Return account-scoped ledger snapshots in chronological order.

    Portfolio snapshots are the cash-ledger timeline. Broker snapshots remain
    the source of historical prices and positions until valuation snapshots are
    persisted natively.
    """
    rows = []
    for row in store.list_portfolio_snapshots(limit=100000):
        account_id = row.get("account_id")
        if not account_id:
            continue
        rows.append(row)
    return sorted(rows, key=lambda row: (str(row.get("created_at") or ""), int(row.get("id") or 0)))


def _ledger_state_as_of(
    rows: list[dict[str, Any]],
    account_id: str,
    broker_row: dict[str, Any],
) -> dict[str, Any] | None:
    broker_created_at = str(broker_row.get("created_at") or "")
    selected = None
    for row in rows:
        if str(row.get("account_id") or "") != account_id:
            continue
        if str(row.get("created_at") or "") > broker_created_at:
            break
        selected = row
    return (selected or {}).get("payload") if selected is not None else None


def _account_with_ledger_cash(
    account: dict[str, Any],
    ledger_payload: dict[str, Any],
) -> dict[str, Any]:
    cash_by_currency = _mapping(ledger_payload.get("cash_by_currency"))
    cash = _float_or_none(ledger_payload.get("cash"))
    updated = dict(account)
    if cash_by_currency:
        updated["cash_by_currency"] = cash_by_currency
    if cash is not None:
        updated["cash"] = cash
    updated["ledger_cash_by_currency"] = cash_by_currency or None
    return updated


def _broker_rows_with_ledger_cash(
    store: StateStore,
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    ledger_rows = _ledger_account_snapshot_rows(store)
    checkpoints = _operator_cash_checkpoints(store)
    buying_power_by_snapshot_id = {
        str(row.get("id")): _buying_power_by_currency(
            _mapping(_mapping(row.get("payload")).get("account"))
        )
        for row in rows
    }
    output: list[dict[str, Any]] = []
    for row in rows:
        ledger = _ledger_state_as_of(ledger_rows, _broker_snapshot_account_id(row), row)
        if ledger is None:
            output.append(row)
            continue
        updated = dict(row)
        payload = dict(_mapping(row.get("payload")))
        payload["account"] = _account_with_ledger_cash(
            _mapping(payload.get("account")),
            ledger,
        )
        payload["account"]["broker_cash_verification"] = _cash_verification_as_of(
            row,
            _mapping(payload.get("account")),
            checkpoints.get(_broker_snapshot_account_id(row), []),
            buying_power_by_snapshot_id,
        )
        updated["payload"] = payload
        output.append(updated)
    return output


def _performance_quality(rows: list[dict[str, Any]]) -> str:
    flags = [
        _mapping(_mapping(row.get("payload")).get("account")).get(
            "ledger_cash_by_currency"
        )
        is not None
        for row in rows
    ]
    if flags and all(flags):
        return "confirmed"
    if any(flags):
        return "degraded"
    return "provisional"


CASH_FLOW_SCOPE_ACCOUNT = "account"
CASH_FLOW_SCOPE_PORTFOLIO = "portfolio"
CASH_FLOW_SCOPE_CURRENCY_SLEEVE = "currency_sleeve"

# What each scope counts as money crossing its own boundary.
#
# An internal transfer is investor money leaving *that account*, so an account
# neutralises it, while the portfolio it never left does not.  A currency
# conversion crosses no account and no portfolio edge -- the total is unchanged
# -- but it is precisely what moves one currency sleeve into another, so only a
# sleeve neutralises it.  Linked classes are additionally required to have both
# of their legs recorded; see ``cash_flow_effects_for_scope``.
_SCOPE_NEUTRALIZING_CLASSES = {
    CASH_FLOW_SCOPE_ACCOUNT: (EXTERNAL_TRANSFER, INTERNAL_TRANSFER),
    CASH_FLOW_SCOPE_PORTFOLIO: (EXTERNAL_TRANSFER,),
    CASH_FLOW_SCOPE_CURRENCY_SLEEVE: (
        EXTERNAL_TRANSFER,
        INTERNAL_TRANSFER,
        FX_CONVERSION,
    ),
}

# Worst first: an aggregate is only as trustworthy as its weakest account.
_CASH_VERIFICATION_RANK = (
    "unavailable",
    "checkpoint_stale",
    "operator_verified",
    "broker_verified",
)


def _cash_verification_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(
            _mapping(_mapping(row.get("payload")).get("account")).get(
                "broker_cash_verification"
            )
            or "unavailable"
        )
        counts[value] = counts.get(value, 0) + 1
    return counts


def _cash_verification_quality(rows: list[dict[str, Any]]) -> str:
    """Aggregate cash verification across accounts by collapsing to the worst.

    A previous "mixed" reading said only that the accounts disagreed, which an
    operator reads as a curiosity rather than as a warning.  One account whose
    cash cannot be verified means the total cannot be verified either, so the
    weakest value is the honest headline; ``_cash_verification_counts`` keeps
    the per-account detail so the weak account stays identifiable.
    """
    counts = _cash_verification_counts(rows)
    if not counts:
        return "unavailable"
    return min(
        counts,
        key=lambda value: (
            _CASH_VERIFICATION_RANK.index(value)
            if value in _CASH_VERIFICATION_RANK
            else -1
        ),
    )


def _operator_cash_checkpoints(store: StateStore) -> dict[str, list[dict[str, Any]]]:
    """Points in time at which an operator confirmed an account's actual cash.

    A confirmation is evidence about the moment it was made, not a standing
    guarantee about every later snapshot.
    """
    checkpoints: dict[str, list[dict[str, Any]]] = {}
    for row in store.list_system_events_by_type(SystemEventType.ACCOUNT_CASH_FLOW, limit=1000):
        payload = _mapping(row.get("payload"))
        if payload.get("verification") != "operator_verified":
            continue
        timestamp = _parse_timestamp(payload.get("effective_at") or row.get("created_at"))
        if timestamp is None:
            continue
        evidence = _mapping(payload.get("evidence"))
        verified_ids = {
            str(value)
            for value in [
                *(evidence.get("stable_snapshot_ids") or []),
                evidence.get("latest_snapshot_id"),
            ]
            if value is not None
        }
        checkpoints.setdefault(str(payload.get("account_id") or ""), []).append(
            {
                "timestamp": timestamp,
                "verified_snapshot_ids": verified_ids,
                "latest_snapshot_id": str(evidence.get("latest_snapshot_id") or ""),
            }
        )
    for values in checkpoints.values():
        values.sort(key=lambda item: item["timestamp"])
    return checkpoints


def _cash_verification_as_of(
    row: dict[str, Any],
    account: dict[str, Any],
    checkpoints: list[dict[str, Any]],
    buying_power_by_snapshot_id: dict[str, dict[str, float]],
) -> str:
    """Cash verification for one broker snapshot.

    Staleness is decided by account activity, not by elapsed time: a
    confirmation still describes the cash of a later snapshot as long as the
    cash has not moved since, and stops describing it the moment it does.
    """
    source = str(account.get("source") or "")
    default = str(account.get("broker_cash_verification") or "")
    if not default:
        default = "unavailable" if source.startswith("toss_") else "broker_verified"
    snapshot_at = _parse_timestamp(row.get("created_at"))
    if not source.startswith("toss_") or snapshot_at is None:
        return default
    checkpoint = next(
        (
            item
            for item in reversed(checkpoints)
            if item["timestamp"] <= snapshot_at
        ),
        None,
    )
    if checkpoint is None:
        return default
    snapshot_id = str(row.get("id"))
    if snapshot_id in checkpoint["verified_snapshot_ids"]:
        return "operator_verified"
    checkpoint_cash = buying_power_by_snapshot_id.get(checkpoint["latest_snapshot_id"])
    if checkpoint_cash is None:
        # The confirmed snapshot is outside the window we can compare against,
        # so we cannot show that the cash has held since.
        return "checkpoint_stale"
    if checkpoint_cash == _buying_power_by_currency(account):
        return "operator_verified"
    return "checkpoint_stale"


def _buying_power_by_currency(account: dict[str, Any]) -> dict[str, float]:
    values = _mapping(account.get("buying_power_by_currency"))
    return {str(key).upper(): float(value) for key, value in values.items()}


def build_account_performance_table(
    store: StateStore,
    config: MaestroConfig | None = None,
    limit: int = 100,
    display_currency: str = "KRW",
) -> list[dict[str, Any]]:
    baseline = store.load_latest_system_event(SystemEventType.PERFORMANCE_BASELINE_ADOPTED)
    if baseline is not None:
        return _build_baselined_account_performance_table(
            store,
            baseline,
            config=config,
            display_currency=display_currency,
        )
    disabled_ids = _disabled_native_account_ids(config)
    source_rows = [
        row
        for row in store.list_broker_account_snapshots(limit=limit)
        if _broker_snapshot_account_id(row) not in disabled_ids
    ]
    source_rows = _broker_rows_with_ledger_cash(store, source_rows)
    performance_account_ids = _expected_account_ids(source_rows)
    reconciliation_by_snapshot_id = _reconciliation_by_snapshot_id(store)
    ledger_rows = _ledger_account_snapshot_rows(store)
    fx_snapshot = build_fx_rate_snapshot_card(store)
    fx_events = _fx_rate_events(store)
    # Cash flow comes from the recorded events, not from a field on the broker
    # snapshot: the events are what the ledger and the return were actually
    # built from, so reading anything else here makes the same number disagree
    # with itself depending on which table the reader is looking at.  An
    # internal transfer is still money leaving *this* account, so it counts.
    cash_flows, cash_flow_reasons = cash_flow_effects_for_scope(
        load_account_cash_flow_facts(store, account_ids=performance_account_ids),
        CASH_FLOW_SCOPE_ACCOUNT,
    )
    # Performance state (first/previous/peak value) is tracked PER ACCOUNT —
    # a shared/global state across interleaved multi-account rows would
    # compare one account's snapshot against a different account's previous
    # value or peak, producing a nonsensical "drawdown" (e.g. a small
    # cash-only account showing -96% simply because a much larger account's
    # peak got attributed to it).
    state_by_account: dict[str, dict[str, Any]] = {}
    rows = []

    for row in reversed(source_rows):
        payload = _mapping(row.get("payload"))
        account = _mapping(payload.get("account"))
        ledger_state = _ledger_state_as_of(ledger_rows, _broker_snapshot_account_id(row), row)
        if ledger_state is not None:
            account = _account_with_ledger_cash(account, ledger_state)
        positions = _positions(account)
        account_id = _broker_snapshot_account_id(row)
        total_value, currency, cash = _account_currency_aware_total(
            account, positions, payload, fx_snapshot, display_currency
        )
        positions_market_value = (
            total_value - cash if total_value is not None and cash is not None else None
        )
        timestamp = _parse_timestamp(row.get("created_at"))
        state = state_by_account.setdefault(
            account_id,
            {
                "first_value": None,
                "previous_value": None,
                "peak_value": None,
                "cumulative_cash_flow": 0.0,
                # Flows recorded before an account's first snapshot belong to no
                # period here, so each account starts its own clock.
                "previous_timestamp": timestamp,
            },
        )
        cash_flow, converted_events = _converted_period_cash_flow(
            cash_flows,
            fx_events,
            previous_timestamp=state["previous_timestamp"],
            timestamp=timestamp,
            currency=currency,
            account_id=account_id,
        )
        state["previous_timestamp"] = timestamp
        performance = _advance_performance_state(
            state,
            total_value if cash_flow is not None else None,
            cash_flow or 0.0,
        )

        reconciliation = reconciliation_by_snapshot_id.get(str(row.get("id")))
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "account_id": account_id,
                "currency": currency,
                "total_value": total_value,
                "cash": cash,
                "positions_market_value": positions_market_value,
                "realized_pnl": _first_float(
                    account,
                    payload,
                    ("realized_pnl", "realized_profit_loss", "realized_profit"),
                ),
                "unrealized_pnl": _account_unrealized_pnl(account, positions),
                "fees": _first_float(account, payload, ("fees", "fee", "commission")),
                "cash_flow": _round_money(cash_flow) if cash_flow is not None else None,
                "cash_flow_events": [event["payload"] for event in converted_events],
                "period_return": performance["period_return"],
                "daily_return": performance["period_return"],
                "cumulative_return": performance["cumulative_return"],
                "drawdown": performance["drawdown"],
                "reconciliation_status": _reconciliation_status(reconciliation),
                "reconciliation_created_at": (reconciliation or {}).get("created_at"),
                "reconciliation_issues_count": (reconciliation or {}).get("issues_count"),
                "source": account.get("source") or payload.get("source"),
                "cash_flow_quality": _cash_flow_quality(
                    cash_flow_reasons,
                    account_ids={account_id},
                    as_of=timestamp,
                ),
                "performance_status": (
                    "confirmed" if ledger_state is not None else "provisional"
                ),
                "broker_cash_verification": account.get(
                    "broker_cash_verification", "unavailable"
                ),
            }
        )
    return list(reversed(rows))


def _build_baselined_account_performance_table(
    store: StateStore,
    baseline_row: dict[str, Any],
    *,
    config: MaestroConfig | None,
    display_currency: str,
) -> list[dict[str, Any]]:
    baseline = _mapping(baseline_row.get("payload"))
    baseline_timestamp = _parse_timestamp(
        baseline.get("effective_at") or baseline_row.get("created_at")
    )
    if baseline_timestamp is None:
        return []
    since = baseline_timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    source_rows = list(
        reversed(store.list_broker_account_snapshots(limit=None, since=since))
    )
    source_rows = _broker_rows_with_ledger_cash(store, source_rows)
    fx_events = _fx_rate_events(store)
    baseline_fx = _fx_snapshot_as_of(fx_events, baseline_timestamp)
    baseline_accounts = _mapping(baseline.get("accounts"))
    tracked_accounts = set(str(account_id) for account_id in baseline_accounts)
    tracked_accounts.update(
        event["account_id"]
        for event in _account_lifecycle_events(store, after=baseline_timestamp)
        if event["event_type"] == SystemEventType.ACCOUNT_TRACKING_STARTED
    )
    cash_flows, cash_flow_reasons = cash_flow_effects_for_scope(
        load_account_cash_flow_facts(
            store,
            after=baseline_timestamp,
            account_ids=tracked_accounts,
        ),
        CASH_FLOW_SCOPE_ACCOUNT,
    )
    reconciliation_by_snapshot_id = _reconciliation_by_snapshot_id(store)
    ledger_rows = _ledger_account_snapshot_rows(store)
    states: dict[str, dict[str, Any]] = {}
    rows = []
    for row in source_rows:
        timestamp = _parse_timestamp(row.get("created_at"))
        if timestamp is None or timestamp <= baseline_timestamp:
            continue
        account_id = _broker_snapshot_account_id(row)
        if account_id not in tracked_accounts:
            continue
        payload = _mapping(row.get("payload"))
        account = _mapping(payload.get("account"))
        ledger_state = _ledger_state_as_of(ledger_rows, account_id, row)
        if ledger_state is not None:
            account = _account_with_ledger_cash(account, ledger_state)
        positions = _positions(account)
        fx_snapshot = _fx_snapshot_as_of(fx_events, timestamp)
        total_value, currency, cash = _account_currency_aware_total(
            account,
            positions,
            payload,
            fx_snapshot,
            display_currency,
        )
        state = states.get(account_id)
        if state is None:
            baseline_account = _mapping(baseline_accounts.get(account_id))
            baseline_components = {
                str(key): float(value)
                for key, value in _mapping(baseline_account.get("components")).items()
            }
            if len(baseline_components) == 1:
                baseline_value = next(iter(baseline_components.values()))
            else:
                baseline_value = _convert_components(
                    baseline_components,
                    display_currency,
                    baseline_fx,
                )
            initial_value = baseline_value if baseline_value is not None else total_value
            state = {
                "first_value": initial_value,
                "previous_value": initial_value,
                "peak_value": initial_value,
                "cumulative_cash_flow": 0.0,
                "twr_growth": 1.0,
                "twr_peak": 1.0,
                "previous_timestamp": (
                    baseline_timestamp if baseline_value is not None else timestamp
                ),
                "cash_flow_events": [],
                "mwr_flows": (
                    [(baseline_timestamp, -initial_value)]
                    if initial_value is not None and baseline_value is not None
                    else [(timestamp, -initial_value)]
                    if initial_value is not None
                    else []
                ),
            }
            states[account_id] = state
            if baseline_value is None:
                total_value = None
        period_events = [
            event
            for event in _period_cash_flow_events(
                cash_flows,
                state.get("previous_timestamp"),
                timestamp,
            )
            if event.get("account_id") == account_id
        ]
        converted_events = []
        flow_failed = False
        cash_flow = 0.0
        for event in period_events:
            event_fx = _fx_snapshot_as_of(fx_events, event["timestamp"])
            converted = _convert_components(
                {event["currency"]: event["signed_amount"]},
                currency,
                event_fx,
            )
            if converted is None:
                flow_failed = True
                break
            cash_flow += converted
            converted_events.append(
                {
                    "timestamp": event["timestamp"],
                    "signed_amount": converted,
                    "payload": {
                        **event["payload"],
                        "display_amount": converted,
                        "display_currency": currency,
                        "fx_rate": _fx_rate(event["currency"], currency, event_fx),
                        "fx_as_of": event_fx.get("as_of"),
                        "fx_event_id": event_fx.get("event_id"),
                    },
                }
            )
        performance = _advance_twr_performance_state(
            state,
            total_value if not flow_failed else None,
            cash_flow,
            timestamp,
            converted_events,
        )
        reconciliation = reconciliation_by_snapshot_id.get(str(row.get("id")))
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "account_id": account_id,
                "currency": currency,
                "total_value": total_value,
                "cash": cash,
                "positions_market_value": (
                    total_value - cash
                    if total_value is not None and cash is not None
                    else None
                ),
                "realized_pnl": _first_float(
                    account,
                    payload,
                    ("realized_pnl", "realized_profit_loss", "realized_profit"),
                ),
                "unrealized_pnl": _account_unrealized_pnl(account, positions),
                "fees": _first_float(account, payload, ("fees", "fee", "commission")),
                "cash_flow": _round_money(cash_flow),
                "period_return": performance["period_return"],
                "daily_return": None,
                "cumulative_return": performance["twr"],
                "twr": performance["twr"],
                "mwr": performance["mwr"],
                "drawdown": performance["drawdown"],
                "reconciliation_status": _reconciliation_status(reconciliation),
                "reconciliation_created_at": (reconciliation or {}).get("created_at"),
                "reconciliation_issues_count": (reconciliation or {}).get("issues_count"),
                "source": account.get("source") or payload.get("source"),
                "baseline_id": baseline.get("baseline_id"),
                "baseline_at": baseline.get("effective_at"),
                "cash_flow_events": [event["payload"] for event in converted_events],
                "cash_flow_quality": _cash_flow_quality(
                    cash_flow_reasons,
                    account_ids={account_id},
                    as_of=timestamp,
                ),
                "performance_status": (
                    "confirmed" if ledger_state is not None else "provisional"
                ),
                "broker_cash_verification": account.get(
                    "broker_cash_verification", "unavailable"
                ),
            }
        )
    for account_id in states:
        _assign_daily_returns([row for row in rows if row["account_id"] == account_id])
    return list(reversed(rows))


def build_currency_sleeve_performance_table(
    store: StateStore,
    config: MaestroConfig | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Per-currency-sleeve performance, one row per currency per run.

    Splits each account's cash+positions by their OWN currency via
    `_account_value_components` rather than labeling the whole account with
    one currency (`_snapshot_currency`) — brokers such as Toss report a
    single account mixing KRW and USD-listed instruments with no
    pre-aggregated per-currency total, so a naive whole-account label would
    silently fold a USD sleeve into the KRW row (or vice versa) and make
    that sleeve appear to not exist at all.
    """
    baseline = store.load_latest_system_event(SystemEventType.PERFORMANCE_BASELINE_ADOPTED)
    if baseline is not None:
        return _build_baselined_currency_sleeve_performance_table(
            store,
            baseline,
            config=config,
        )
    disabled_ids = _disabled_native_account_ids(config)
    source_rows = [
        row
        for row in store.list_broker_account_snapshots(limit=limit)
        if _broker_snapshot_account_id(row) not in disabled_ids
    ]
    source_rows = _broker_rows_with_ledger_cash(store, source_rows)
    expected_accounts = _expected_account_ids(source_rows)
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in reversed(source_rows):
        group_key = str(row.get("run_id") or row.get("created_at") or row.get("id"))
        grouped.setdefault(group_key, []).append(row)

    reconciliation_by_snapshot_id = _reconciliation_by_snapshot_id(store)
    # A sleeve is denominated in its own currency, so a flow belongs to the
    # sleeve it is denominated in and needs no conversion.  Reading it off the
    # broker snapshot and labelling it with the account's single currency put
    # a USD deposit into the KRW sleeve of any account holding both.
    cash_flows, cash_flow_reasons = cash_flow_effects_for_scope(
        load_account_cash_flow_facts(store, account_ids=expected_accounts),
        CASH_FLOW_SCOPE_CURRENCY_SLEEVE,
    )
    state_by_currency: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    previous_timestamp: datetime | None = None
    latest_by_account: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for group_rows in grouped.values():
        for row in group_rows:
            account_id = _broker_snapshot_account_id(row)
            if account_id:
                latest_by_account[account_id] = row
        if not expected_accounts <= latest_by_account.keys():
            continue

        component_values: dict[str, float] = {}
        component_cash: dict[str, float] = {}
        component_cash_flows: dict[str, float] = {}
        created_at = group_rows[-1].get("created_at")
        run_id = group_rows[-1].get("run_id")
        reconciliation_statuses = []
        for row in latest_by_account.values():
            payload = _mapping(row.get("payload"))
            account = _mapping(payload.get("account"))
            positions = _positions(account)
            for currency, value in _account_value_components(account, positions, payload).items():
                component_values[currency] = component_values.get(currency, 0.0) + value
            for currency, value in _account_cash_components(account, payload).items():
                component_cash[currency] = component_cash.get(currency, 0.0) + value
            reconciliation = reconciliation_by_snapshot_id.get(str(row.get("id")))
            reconciliation_statuses.append(_reconciliation_status(reconciliation))
        timestamp = _parse_timestamp(created_at)
        if previous_timestamp is None:
            previous_timestamp = timestamp
        for event in _period_cash_flow_events(cash_flows, previous_timestamp, timestamp):
            component_cash_flows[event["currency"]] = (
                component_cash_flows.get(event["currency"], 0.0) + event["signed_amount"]
            )
        previous_timestamp = timestamp

        combined_reconciliation = _combined_reconciliation_status(reconciliation_statuses)
        # A sleeve can be created by a flow before the broker reports a holding
        # in it -- a conversion into a currency the account did not hold -- so
        # the flow decides the row exists just as much as the value does.
        for currency in sorted(set(component_values) | set(component_cash_flows)):
            total_value = component_values.get(currency, 0.0)
            state = state_by_currency.setdefault(
                currency,
                {
                    "first_value": None,
                    "previous_value": None,
                    "peak_value": None,
                    "cumulative_cash_flow": 0.0,
                },
            )
            cash_flow = component_cash_flows.get(currency, 0.0)
            performance = _advance_performance_state(state, total_value, cash_flow)
            rows.append(
                {
                    "created_at": created_at,
                    "run_id": run_id,
                    "currency": currency,
                    "total_value": total_value,
                    "cash": component_cash.get(currency),
                    "cash_flow": cash_flow,
                    "cash_flow_quality": _cash_flow_quality(
                        cash_flow_reasons,
                        account_ids=expected_accounts,
                        as_of=timestamp,
                    ),
                    "period_return": performance["period_return"],
                    "daily_return": performance["period_return"],
                    "cumulative_return": performance["cumulative_return"],
                    "drawdown": performance["drawdown"],
                    "reconciliation_status": combined_reconciliation,
                    "performance_status": _performance_quality(
                        list(latest_by_account.values())
                    ),
                    "broker_cash_verification": _cash_verification_quality(
                        list(latest_by_account.values())
                    ),
                    "broker_cash_verification_counts": _cash_verification_counts(
                        list(latest_by_account.values())
                    ),
                }
            )
    return list(reversed(rows))


def _build_baselined_currency_sleeve_performance_table(
    store: StateStore,
    baseline_row: dict[str, Any],
    *,
    config: MaestroConfig | None,
) -> list[dict[str, Any]]:
    baseline = _mapping(baseline_row.get("payload"))
    baseline_timestamp = _parse_timestamp(
        baseline.get("effective_at") or baseline_row.get("created_at")
    )
    if baseline_timestamp is None:
        return []
    since = baseline_timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    source_rows = list(
        reversed(store.list_broker_account_snapshots(limit=None, since=since))
    )
    source_rows = _broker_rows_with_ledger_cash(store, source_rows)
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in source_rows:
        grouped.setdefault(
            str(row.get("run_id") or row.get("created_at") or row.get("id")),
            [],
        ).append(row)
    baseline_components = {
        str(currency): float(value)
        for currency, value in _mapping(baseline.get("component_values")).items()
    }
    active_accounts = set(str(key) for key in _mapping(baseline.get("accounts")))
    lifecycle_events = _account_lifecycle_events(store, after=baseline_timestamp)
    tracked_cash_flow_accounts = active_accounts | {
        event["account_id"] for event in lifecycle_events
    }
    cash_flows, cash_flow_reasons = cash_flow_effects_for_scope(
        load_account_cash_flow_facts(
            store,
            after=baseline_timestamp,
            account_ids=tracked_cash_flow_accounts,
        ),
        CASH_FLOW_SCOPE_CURRENCY_SLEEVE,
    )
    reconciliation_by_snapshot_id = _reconciliation_by_snapshot_id(store)
    states = {
        currency: {
            "first_value": value,
            "previous_value": value,
            "peak_value": value,
            "cumulative_cash_flow": 0.0,
            "twr_growth": 1.0,
            "twr_peak": 1.0,
            "previous_timestamp": baseline_timestamp,
            "cash_flow_events": [],
            "mwr_flows": [(baseline_timestamp, -value)],
        }
        for currency, value in baseline_components.items()
    }
    latest_by_account: OrderedDict[str, dict[str, Any]] = OrderedDict()
    lifecycle_index = 0
    previous_timestamp = baseline_timestamp
    rows: list[dict[str, Any]] = []
    for group_rows in grouped.values():
        timestamp = _parse_timestamp(group_rows[-1].get("created_at"))
        if timestamp is None or timestamp <= baseline_timestamp:
            continue
        started: set[str] = set()
        ended: set[str] = set()
        while (
            lifecycle_index < len(lifecycle_events)
            and lifecycle_events[lifecycle_index]["timestamp"] <= timestamp
        ):
            event = lifecycle_events[lifecycle_index]
            account_id = event["account_id"]
            if event["event_type"] == SystemEventType.ACCOUNT_TRACKING_STARTED:
                if account_id not in active_accounts:
                    active_accounts.add(account_id)
                    started.add(account_id)
            elif account_id in active_accounts:
                active_accounts.discard(account_id)
                ended.add(account_id)
            lifecycle_index += 1
        for row in group_rows:
            account_id = _broker_snapshot_account_id(row)
            if account_id:
                latest_by_account[account_id] = row
        if not active_accounts <= latest_by_account.keys():
            continue
        if any(
            (snapshot_timestamp := _parse_timestamp(row.get("created_at"))) is None
            or (timestamp - snapshot_timestamp).total_seconds() > 1200
            for account_id, row in latest_by_account.items()
            if account_id in active_accounts
        ):
            continue
        values: dict[str, float] = {}
        statuses = []
        for account_id in active_accounts:
            row = latest_by_account[account_id]
            for currency, value in broker_snapshot_value_components(row).items():
                values[currency] = values.get(currency, 0.0) + value
            statuses.append(
                _reconciliation_status(reconciliation_by_snapshot_id.get(str(row.get("id"))))
            )
        flow_by_currency: dict[str, float] = {}
        period_events = _period_cash_flow_events(
            cash_flows,
            previous_timestamp,
            timestamp,
        )
        period_scope_accounts = active_accounts | ended
        period_events = [
            event
            for event in period_events
            if event.get("account_id") in period_scope_accounts
        ]
        for event in period_events:
            flow_by_currency[event["currency"]] = (
                flow_by_currency.get(event["currency"], 0.0) + event["signed_amount"]
            )
        for account_ids, sign in ((started, 1.0), (ended, -1.0)):
            for account_id in account_ids:
                row = latest_by_account.get(account_id)
                if row is None:
                    continue
                for currency, value in broker_snapshot_value_components(row).items():
                    flow_by_currency[currency] = (
                        flow_by_currency.get(currency, 0.0) + sign * value
                    )
        for currency in sorted(set(values) | set(flow_by_currency)):
            total_value = values.get(currency, 0.0)
            new_currency = currency not in states
            if new_currency:
                states[currency] = {
                    "first_value": total_value,
                    "previous_value": total_value,
                    "peak_value": total_value,
                    "cumulative_cash_flow": 0.0,
                    "twr_growth": 1.0,
                    "twr_peak": 1.0,
                    "previous_timestamp": timestamp,
                    "cash_flow_events": [],
                    "mwr_flows": [(timestamp, -total_value)],
                }
            state = states[currency]
            currency_events = [
                event for event in period_events if event["currency"] == currency
            ]
            cash_flow = 0.0 if new_currency else flow_by_currency.get(currency, 0.0)
            performance = _advance_twr_performance_state(
                state,
                total_value,
                cash_flow,
                timestamp,
                currency_events,
            )
            rows.append(
                {
                    "created_at": group_rows[-1].get("created_at"),
                    "run_id": group_rows[-1].get("run_id"),
                    "currency": currency,
                    "total_value": total_value,
                    "cash": None,
                    "cash_flow": _round_money(cash_flow),
                    "period_return": performance["period_return"],
                    "daily_return": None,
                    "cumulative_return": performance["twr"],
                    "twr": performance["twr"],
                    "mwr": performance["mwr"],
                    "drawdown": performance["drawdown"],
                    "reconciliation_status": _combined_reconciliation_status(statuses),
                    "cash_flow_quality": _cash_flow_quality(
                        cash_flow_reasons,
                        account_ids=period_scope_accounts,
                        as_of=timestamp,
                    ),
                    "performance_status": _performance_quality(
                        [latest_by_account[account_id] for account_id in active_accounts]
                    ),
                    "broker_cash_verification": _cash_verification_quality(
                        [latest_by_account[account_id] for account_id in active_accounts]
                    ),
                    "broker_cash_verification_counts": _cash_verification_counts(
                        [latest_by_account[account_id] for account_id in active_accounts]
                    ),
                    "baseline_id": baseline.get("baseline_id"),
                    "baseline_at": baseline.get("effective_at"),
                }
            )
        previous_timestamp = timestamp
    for currency in states:
        _assign_daily_returns([row for row in rows if row["currency"] == currency])
    return list(reversed(rows))


def build_total_portfolio_performance_table(
    store: StateStore,
    config: MaestroConfig | None = None,
    limit: int = 200,
    display_currency: str = "KRW",
) -> list[dict[str, Any]]:
    baseline = store.load_latest_system_event(SystemEventType.PERFORMANCE_BASELINE_ADOPTED)
    if baseline is not None:
        return _build_baselined_total_portfolio_performance_table(
            store,
            baseline,
            config=config,
            display_currency=display_currency,
        )
    disabled_ids = _disabled_native_account_ids(config)
    source_rows = [
        row
        for row in store.list_broker_account_snapshots(limit=limit)
        if _broker_snapshot_account_id(row) not in disabled_ids
    ]
    source_rows = _broker_rows_with_ledger_cash(store, source_rows)
    expected_accounts = _expected_account_ids(source_rows)
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in reversed(source_rows):
        group_key = str(row.get("run_id") or row.get("created_at") or row.get("id"))
        grouped.setdefault(group_key, []).append(row)

    reconciliation_by_snapshot_id = _reconciliation_by_snapshot_id(store)
    fx_snapshot = build_fx_rate_snapshot_card(store)
    fx_events = _fx_rate_events(store)
    # Portfolio level nets out transfers between the operator's own accounts:
    # money moving from one account to another never entered or left the
    # portfolio, so counting it would neutralise a flow that never happened.
    cash_flows, cash_flow_reasons = cash_flow_effects_for_scope(
        load_account_cash_flow_facts(store, account_ids=expected_accounts),
        CASH_FLOW_SCOPE_PORTFOLIO,
    )
    performance_state = {
        "first_value": None,
        "previous_value": None,
        "peak_value": None,
        "cumulative_cash_flow": 0.0,
    }
    rows = []
    previous_component_values: dict[str, float] = {}
    previous_timestamp: datetime | None = None
    latest_by_account: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for group_rows in grouped.values():
        for row in group_rows:
            account_id = _broker_snapshot_account_id(row)
            if account_id:
                latest_by_account[account_id] = row
        if not expected_accounts <= latest_by_account.keys():
            continue

        component_values: dict[str, float] = {}
        created_at = group_rows[-1].get("created_at")
        run_id = group_rows[-1].get("run_id")
        reconciliation_statuses = []
        for row in latest_by_account.values():
            payload = _mapping(row.get("payload"))
            account = _mapping(payload.get("account"))
            positions = _positions(account)
            for currency, value in _account_value_components(
                account, positions, payload, default_currency=display_currency
            ).items():
                component_values[currency] = component_values.get(currency, 0.0) + value
            reconciliation = reconciliation_by_snapshot_id.get(str(row.get("id")))
            reconciliation_statuses.append(_reconciliation_status(reconciliation))

        timestamp = _parse_timestamp(created_at)
        if previous_timestamp is None:
            previous_timestamp = timestamp
        period_cash_flow, converted_events = _converted_period_cash_flow(
            cash_flows,
            fx_events,
            previous_timestamp=previous_timestamp,
            timestamp=timestamp,
            currency=display_currency,
        )
        previous_timestamp = timestamp

        currencies = sorted(component_values)
        converted_value = _convert_components(
            component_values,
            display_currency,
            fx_snapshot,
        )
        fx_needed = any(currency != display_currency for currency in currencies)
        fx_ready = not fx_needed or fx_snapshot["status"] == "fresh"
        missing_fx = fx_needed and fx_snapshot["status"] == "missing"
        stale_fx = fx_needed and fx_snapshot["status"] == "stale"
        total_value = converted_value if fx_ready else None
        cash_flow = period_cash_flow
        local_return = _local_component_return(component_values, previous_component_values)
        performance = _advance_performance_state(
            performance_state,
            total_value if cash_flow is not None else None,
            cash_flow or 0.0,
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
                "cash_flow": _round_money(cash_flow) if cash_flow is not None else None,
                "cash_flow_events": [event["payload"] for event in converted_events],
                "local_return": local_return if fx_needed else period_return,
                "fx_effect": _round_ratio(period_return - local_return)
                if period_return is not None and local_return is not None and fx_needed
                else None,
                "period_return": period_return,
                "daily_return": period_return,
                "cumulative_return": performance["cumulative_return"],
                "drawdown": performance["drawdown"],
                "reconciliation_status": _combined_reconciliation_status(reconciliation_statuses),
                "cash_flow_quality": _cash_flow_quality(
                    cash_flow_reasons,
                    account_ids=expected_accounts,
                    as_of=timestamp,
                ),
                "performance_status": _performance_quality(
                    list(latest_by_account.values())
                ),
                "broker_cash_verification": _cash_verification_quality(
                    list(latest_by_account.values())
                ),
                "broker_cash_verification_counts": _cash_verification_counts(
                    list(latest_by_account.values())
                ),
            }
        )
        previous_component_values = component_values
    return list(reversed(rows))


def _build_baselined_total_portfolio_performance_table(
    store: StateStore,
    baseline_row: dict[str, Any],
    *,
    config: MaestroConfig | None,
    display_currency: str,
) -> list[dict[str, Any]]:
    baseline = _mapping(baseline_row.get("payload"))
    baseline_timestamp = _parse_timestamp(
        baseline.get("effective_at") or baseline_row.get("created_at")
    )
    if baseline_timestamp is None:
        return []
    baseline_text = baseline_timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")
    source_rows = store.list_broker_account_snapshots(limit=None, since=baseline_text)
    source_rows = _broker_rows_with_ledger_cash(store, source_rows)
    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in reversed(source_rows):
        group_key = str(row.get("run_id") or row.get("created_at") or row.get("id"))
        grouped.setdefault(group_key, []).append(row)

    fx_events = _fx_rate_events(store)
    baseline_components = {
        str(currency): float(value)
        for currency, value in _mapping(baseline.get("component_values")).items()
    }
    baseline_fx = _fx_snapshot_as_of(fx_events, baseline_timestamp)
    baseline_value = _convert_components(baseline_components, display_currency, baseline_fx)
    if baseline_value is None:
        return []
    baseline_accounts = _mapping(baseline.get("accounts"))
    active_accounts = set(str(account_id) for account_id in baseline_accounts)
    lifecycle_events = _account_lifecycle_events(store, after=baseline_timestamp)
    tracked_cash_flow_accounts = active_accounts | {
        event["account_id"] for event in lifecycle_events
    }
    cash_flow_events, cash_flow_reasons = cash_flow_effects_for_scope(
        load_account_cash_flow_facts(
            store,
            after=baseline_timestamp,
            account_ids=tracked_cash_flow_accounts,
        ),
        CASH_FLOW_SCOPE_PORTFOLIO,
    )
    reconciliation_by_snapshot_id = _reconciliation_by_snapshot_id(store)
    state = {
        "first_value": baseline_value,
        "previous_value": baseline_value,
        "peak_value": baseline_value,
        "cumulative_cash_flow": 0.0,
        "twr_growth": 1.0,
        "twr_peak": 1.0,
        "previous_timestamp": baseline_timestamp,
        "cash_flow_events": [],
        "mwr_flows": [(baseline_timestamp, -baseline_value)],
    }
    latest_by_account: OrderedDict[str, dict[str, Any]] = OrderedDict()
    previous_component_values = baseline_components
    rows: list[dict[str, Any]] = []
    lifecycle_index = 0
    for group_rows in grouped.values():
        current_timestamp = _parse_timestamp(group_rows[-1].get("created_at"))
        if current_timestamp is None or current_timestamp <= baseline_timestamp:
            continue
        started: set[str] = set()
        ended: set[str] = set()
        while (
            lifecycle_index < len(lifecycle_events)
            and lifecycle_events[lifecycle_index]["timestamp"] <= current_timestamp
        ):
            event = lifecycle_events[lifecycle_index]
            if event["event_type"] == SystemEventType.ACCOUNT_TRACKING_STARTED:
                if event["account_id"] not in active_accounts:
                    active_accounts.add(event["account_id"])
                    started.add(event["account_id"])
            else:
                if event["account_id"] in active_accounts:
                    active_accounts.discard(event["account_id"])
                    ended.add(event["account_id"])
            lifecycle_index += 1
        for row in group_rows:
            account_id = _broker_snapshot_account_id(row)
            if account_id:
                latest_by_account[account_id] = row
        missing_accounts = sorted(active_accounts - latest_by_account.keys())
        stale_accounts = sorted(
            account_id
            for account_id in active_accounts & latest_by_account.keys()
            if (
                snapshot_timestamp := _parse_timestamp(
                    latest_by_account[account_id].get("created_at")
                )
            )
            is None
            or (current_timestamp - snapshot_timestamp).total_seconds() > 1200
        )
        if missing_accounts or stale_accounts:
            rows.append(
                {
                    "created_at": group_rows[-1].get("created_at"),
                    "run_id": group_rows[-1].get("run_id"),
                    "currency": display_currency,
                    "display_currency": display_currency,
                    "total_value": None,
                    "component_values": {},
                    "cash_flow": None,
                    "period_return": None,
                    "daily_return": None,
                    "cumulative_return": None,
                    "drawdown": None,
                    "fx_status": "not_evaluated",
                    "missing_fx": False,
                    "stale_fx": False,
                    "reconciliation_status": "partial",
                    "cash_flow_quality": _cash_flow_quality(
                        cash_flow_reasons,
                        account_ids=active_accounts | ended,
                        as_of=current_timestamp,
                    ),
                    "performance_status": "partial",
                    "baseline_id": baseline.get("baseline_id"),
                    "baseline_at": baseline.get("effective_at"),
                    "active_accounts": sorted(active_accounts),
                    "missing_accounts": missing_accounts,
                    "stale_accounts": stale_accounts,
                    "unexplained_return": False,
                }
            )
            continue

        component_values: dict[str, float] = {}
        reconciliation_statuses: list[str] = []
        for account_id in sorted(active_accounts):
            row = latest_by_account[account_id]
            for currency, value in broker_snapshot_value_components(
                row,
                default_currency=display_currency,
            ).items():
                component_values[currency] = component_values.get(currency, 0.0) + value
            reconciliation_statuses.append(
                _reconciliation_status(reconciliation_by_snapshot_id.get(str(row.get("id"))))
            )

        period_events = _period_cash_flow_events(
            cash_flow_events,
            state.get("previous_timestamp"),
            current_timestamp,
        )
        period_scope_accounts = active_accounts | ended
        period_events = [
            event
            for event in period_events
            if event.get("account_id") in period_scope_accounts
        ]
        membership_components: dict[str, float] = {}
        converted_event_payloads: list[dict[str, Any]] = []
        converted_events: list[dict[str, Any]] = []
        flow_conversion_failed = False
        for event in period_events:
            event_fx = _fx_snapshot_as_of(fx_events, event["timestamp"])
            converted = _convert_components(
                {event["currency"]: event["signed_amount"]},
                display_currency,
                event_fx,
            )
            if converted is None:
                flow_conversion_failed = True
                break
            converted_payload = {
                **event["payload"],
                "display_amount": converted,
                "display_currency": display_currency,
                "fx_rate": _fx_rate(event["currency"], display_currency, event_fx),
                "fx_as_of": event_fx.get("as_of"),
                "fx_event_id": event_fx.get("event_id"),
            }
            converted_event_payloads.append(converted_payload)
            converted_events.append(
                {
                    "timestamp": event["timestamp"],
                    "signed_amount": converted,
                    "payload": converted_payload,
                }
            )
        for account_id in started:
            row = latest_by_account.get(account_id)
            if row is None:
                flow_conversion_failed = True
                continue
            for currency, value in broker_snapshot_value_components(
                row,
                default_currency=display_currency,
            ).items():
                membership_components[currency] = (
                    membership_components.get(currency, 0.0) + value
                )
        for account_id in ended:
            row = latest_by_account.get(account_id)
            if row is None:
                continue
            for currency, value in broker_snapshot_value_components(
                row,
                default_currency=display_currency,
            ).items():
                membership_components[currency] = (
                    membership_components.get(currency, 0.0) - value
                )

        fx_snapshot = _fx_snapshot_as_of(fx_events, current_timestamp)
        total_value = _convert_components(component_values, display_currency, fx_snapshot)
        membership_flow = _convert_components(
            membership_components,
            display_currency,
            fx_snapshot,
        )
        cash_flow = (
            None
            if flow_conversion_failed or membership_flow is None
            else sum(event["signed_amount"] for event in converted_events)
            + membership_flow
        )
        performance = _advance_twr_performance_state(
            state,
            total_value if cash_flow is not None else None,
            cash_flow or 0.0,
            current_timestamp,
            converted_events,
        )
        local_return = _local_component_return(component_values, previous_component_values)
        period_return = performance["period_return"]
        unexplained_return = (
            period_return is not None
            and abs(period_return) > 0.15
            and not period_events
            and not started
            and not ended
        )
        rows.append(
            {
                "created_at": group_rows[-1].get("created_at"),
                "run_id": group_rows[-1].get("run_id"),
                "currency": display_currency,
                "display_currency": display_currency,
                "total_value": total_value,
                "component_values": dict(sorted(component_values.items())),
                "cash_flow": _round_money(cash_flow),
                "cumulative_cash_flow": performance["cumulative_cash_flow"],
                "net_pnl": performance["net_pnl"],
                "period_return": period_return,
                "daily_return": None,
                "cumulative_return": performance["twr"],
                "twr": performance["twr"],
                "mwr": performance["mwr"],
                "drawdown": performance["drawdown"],
                "local_return": local_return,
                "fx_effect": (
                    _round_ratio(period_return - local_return)
                    if period_return is not None and local_return is not None
                    else None
                ),
                "fx_status": fx_snapshot.get("status"),
                "fx_source": fx_snapshot.get("source"),
                "fx_rate": fx_snapshot.get("rate"),
                "fx_timestamp": fx_snapshot.get("as_of"),
                "missing_fx": total_value is None,
                "stale_fx": fx_snapshot.get("status") == "stale",
                "reconciliation_status": _combined_reconciliation_status(
                    reconciliation_statuses
                ),
                "cash_flow_quality": _cash_flow_quality(
                    cash_flow_reasons,
                    account_ids=period_scope_accounts,
                    as_of=current_timestamp,
                ),
                "performance_status": _performance_quality(
                    [latest_by_account[account_id] for account_id in active_accounts]
                ),
                "broker_cash_verification": _cash_verification_quality(
                    [latest_by_account[account_id] for account_id in active_accounts]
                ),
                "broker_cash_verification_counts": _cash_verification_counts(
                    [latest_by_account[account_id] for account_id in active_accounts]
                ),
                "baseline_id": baseline.get("baseline_id"),
                "baseline_at": baseline.get("effective_at"),
                "active_accounts": sorted(active_accounts),
                "cash_flow_events": converted_event_payloads,
                "unexplained_return": unexplained_return,
            }
        )
        previous_component_values = component_values
    _assign_daily_returns(rows)
    return list(reversed(rows))


def build_cash_flow_center(
    store: StateStore,
    config: MaestroConfig | None = None,
    display_currency: str = "KRW",
) -> dict[str, Any]:
    """Everything known about money entering and leaving, in one place.

    The pieces already existed and were scattered: confirmed flows lived inside
    performance rows, candidates awaiting confirmation lived only in Telegram,
    and the unresolved ledger-versus-broker differences in ``cash_suspense``
    were not surfaced anywhere at all. An operator who missed a Telegram message
    had no screen that would tell them so.

    Events are pre-converted here rather than in the browser, each at the rate
    of its own moment, so the figures agree with the ones the returns were
    built from.
    """
    fx_events = _fx_rate_events(store)
    facts = load_account_cash_flow_facts(store)
    neutralised, reasons = cash_flow_effects_for_scope(facts, CASH_FLOW_SCOPE_PORTFOLIO)
    neutralised_run_ids = {
        str(_mapping(fact["payload"]).get("run_id")) for fact in neutralised
    }

    events = []
    for fact in facts:
        payload = _mapping(fact["payload"])
        event_fx = _fx_snapshot_as_of(fx_events, fact["timestamp"])
        converted = _convert_components(
            {fact["currency"]: fact["signed_amount"]},
            display_currency,
            event_fx,
        )
        events.append(
            {
                "run_id": payload.get("run_id"),
                "account_id": fact["account_id"],
                "effective_at": payload.get("effective_at"),
                "amount": fact["signed_amount"],
                "currency": fact["currency"],
                "display_amount": _round_money(converted) if converted is not None else None,
                "display_currency": display_currency,
                "fx_rate": _fx_rate(fact["currency"], display_currency, event_fx),
                "flow_type": payload.get("flow_type"),
                "flow_class": fact["flow_class"],
                "verification": payload.get("verification"),
                "source": payload.get("source"),
                "decided_by": payload.get("decided_by"),
                "transfer_id": payload.get("transfer_id"),
                # Whether the portfolio return treats this as money crossing its
                # edge. Without it a reader cannot tell why a dividend shows up
                # in the return and a deposit does not.
                "neutralised_in_return": str(payload.get("run_id")) in neutralised_run_ids,
            }
        )
    events.reverse()

    pending, decisions = _cash_flow_proposal_states(store)
    return {
        "schema_version": 1,
        "display_currency": display_currency,
        "events": events,
        "pending_candidates": pending,
        "recent_decisions": decisions[:20],
        "unresolved_deltas": _unresolved_cash_deltas(store),
        "account_statuses": _cash_flow_account_statuses(store, config),
        "quality": _cash_flow_quality(reasons),
    }


def _cash_flow_proposal_states(
    store: StateStore,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Candidates still awaiting an operator, and what was decided recently.

    A candidate the operator never answered is the one that matters: nothing
    else in the system will raise it again once the balance settles into its
    new level.
    """
    acks: dict[str, dict[str, Any]] = {}
    decisions = []
    for row in store.list_system_events_by_type(
        SystemEventType.ACCOUNT_CASH_FLOW_PROPOSAL_ACK, limit=1000
    ):
        payload = _mapping(row.get("payload"))
        proposal_id = str(payload.get("proposal_id") or "")
        if not proposal_id or proposal_id in acks:
            continue
        acks[proposal_id] = payload
        decisions.append(
            {
                "proposal_id": proposal_id,
                "status": payload.get("status"),
                "decided_by": payload.get("decided_by"),
                "decided_at": row.get("created_at"),
            }
        )

    seen: set[str] = set()
    pending = []
    for row in store.list_system_events_by_type(
        SystemEventType.ACCOUNT_CASH_FLOW_PROPOSAL, limit=1000
    ):
        payload = _mapping(row.get("payload"))
        proposal_id = str(payload.get("proposal_id") or "")
        if not proposal_id or proposal_id in acks:
            continue
        key = f"{payload.get('account_id')}:{payload.get('currency')}"
        # Rows arrive newest first, so a later proposal for the same account and
        # currency has already replaced anything older.
        status = "pending" if key not in seen else "superseded"
        seen.add(key)
        pending.append(
            {
                "proposal_id": proposal_id,
                "status": status,
                "account_id": payload.get("account_id"),
                "amount": payload.get("amount"),
                "currency": payload.get("currency"),
                "flow_type": payload.get("flow_type"),
                "source": payload.get("source"),
                "effective_at": payload.get("effective_at"),
                "created_at": row.get("created_at"),
                "evidence": payload.get("evidence") or {},
                "confirm_in": "telegram",
            }
        )
    return pending, decisions


def _unresolved_cash_deltas(store: StateStore) -> list[dict[str, Any]]:
    """Ledger-versus-broker differences nobody has explained yet."""
    return [
        {
            "account_id": row.get("account_id"),
            "currency": row.get("currency"),
            "amount": _float_or_none(row.get("amount")),
            "classification": row.get("candidate_label"),
            "status": row.get("status"),
            "first_observed_at": row.get("first_observed_at"),
            "last_observed_at": row.get("last_observed_at"),
            "last_snapshot_id": row.get("last_snapshot_id"),
        }
        for row in store.list_cash_suspense()
    ]


def _cash_flow_account_statuses(
    store: StateStore,
    config: MaestroConfig | None,
) -> list[dict[str, Any]]:
    disabled_ids = _disabled_native_account_ids(config)
    rows = [
        row
        for row in store.list_broker_account_snapshots(limit=200)
        if _broker_snapshot_account_id(row) not in disabled_ids
    ]
    rows = _broker_rows_with_ledger_cash(store, rows)
    reconciliation_by_snapshot_id = _reconciliation_by_snapshot_id(store)
    deltas_by_account: dict[str, list[dict[str, Any]]] = {}
    for delta in _unresolved_cash_deltas(store):
        deltas_by_account.setdefault(str(delta["account_id"]), []).append(delta)

    statuses: dict[str, dict[str, Any]] = {}
    for row in rows:
        account_id = _broker_snapshot_account_id(row)
        if account_id in statuses:
            continue
        account = _mapping(_mapping(row.get("payload")).get("account"))
        source = str(account.get("source") or "")
        reconciliation = reconciliation_by_snapshot_id.get(str(row.get("id")))
        statuses[account_id] = {
            "account_id": account_id,
            "source": source,
            # Toss publishes buying power and no settled-cash endpoint, so its
            # cash is a proxy however recently it was confirmed.
            "cash_basis": "proxy" if source.startswith("toss_") else "broker_reported",
            "cash_verification": account.get("broker_cash_verification", "unavailable"),
            "ledger_status": (
                "confirmed"
                if account.get("ledger_cash_by_currency") is not None
                else "provisional"
            ),
            "reconciliation_status": _reconciliation_status(reconciliation),
            "last_snapshot_at": row.get("created_at"),
            "unresolved_deltas": deltas_by_account.get(account_id, []),
        }
    return list(statuses.values())


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
    freshness_at = payload.get("fetched_at") or as_of
    age_seconds = _age_seconds(freshness_at)
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
    cash_flows_by_strategy = _strategy_cash_flows_by_strategy(store)
    states: dict[str, dict[str, Any]] = {}
    rows = []
    for row in reversed(source_rows):
        payload = _mapping(row.get("payload"))
        strategy_id = str(row.get("strategy_id") or payload.get("strategy_id") or "")
        book_id = str(row.get("book_id") or payload.get("book_id") or "")
        state = states.setdefault(
            book_id,
            {
                "first_value": None,
                "previous_value": None,
                "peak_value": None,
                "cumulative_cash_flow": 0.0,
                "twr_growth": 1.0,
                "previous_timestamp": None,
                "cash_flow_events": [],
                "mwr_flows": [],
            },
        )
        book_value = _float_or_none(payload.get("book_value"))
        timestamp = _parse_timestamp(row.get("created_at"))
        cash_flow_events = _period_cash_flow_events(
            cash_flows_by_strategy.get(strategy_id, []),
            state.get("previous_timestamp"),
            timestamp,
        )
        cash_flow = sum(event["signed_amount"] for event in cash_flow_events)
        performance = _advance_twr_performance_state(
            state,
            book_value,
            cash_flow,
            timestamp,
            cash_flow_events,
        )
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "strategy_id": strategy_id,
                "book_id": book_id,
                "label": payload.get("label"),
                "target_weight": _float_or_none(payload.get("target_weight")),
                "book_value": book_value,
                "current_value": book_value,
                "cash_flow": _round_money(cash_flow),
                "cumulative_cash_flow": performance["cumulative_cash_flow"],
                "net_pnl": performance["net_pnl"],
                "period_return": performance["period_return"],
                "twr": performance["twr"],
                "cumulative_return": performance["twr"],
                "mwr": performance["mwr"],
                "irr": performance["mwr"],
                "drawdown": performance["drawdown"],
                "cash": _float_or_none(payload.get("cash")),
                "allocations": _mapping(payload.get("allocations")),
                "cash_flow_events": [event["payload"] for event in cash_flow_events],
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
    orders_by_run = _rows_by_run(build_orders_table(store, limit=limit))
    fills_by_run = _fill_lineage_by_run(store, limit=limit)
    rows = []
    for row in build_strategy_book_performance_table(store, limit=limit):
        run_id = str(row.get("run_id"))
        signal = signals_by_run.get(run_id) or {}
        allocations = _mapping(row.get("allocations"))
        orders = _strategy_lineage_orders(orders_by_run.get(run_id, []), allocations)
        fills = _strategy_lineage_fills(fills_by_run.get(run_id, []), orders, allocations)
        allocation_symbols = sorted(str(symbol) for symbol in allocations)
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
                "order_count": len(orders),
                "fill_count": len(fills),
                "signal_action": signal.get("signal_action"),
                "signal_symbol": signal.get("signal_symbol"),
                "confidence": signal.get("confidence"),
                "attribution_source": "strategy_book_snapshot",
                "lineage": {
                    "status": _strategy_lineage_status(signal, orders, fills),
                    "strategy_run": _strategy_run_lineage(signal),
                    "allocation_symbols": allocation_symbols,
                    "orders": orders,
                    "fills": fills,
                    "source_tables": [
                        "strategy_book_snapshots",
                        "strategy_runs",
                        "orders",
                        "system_events.fill_reconciliation",
                    ],
                    "attribution_rule": (
                        "strategy_book_snapshot_first; same-run orders and fills "
                        "linked by symbol or broker order id"
                    ),
                },
            }
        )
    return rows


def _strategy_attribution_scopes(config: Any) -> dict[str, list[tuple[str, str]]]:
    """Map each strategy to the (account_id, bucket_id) pairs holding its positions.

    The attribution ledger tracks quantities per (account, symbol, bucket) where
    bucket == the strategy's execution sleeve. Multi-account contribution
    strategies carry a routing label instead of a broker account id, so their
    scope comes from the group's member account targets.
    """
    scopes: dict[str, list[tuple[str, str]]] = {}
    group_method = getattr(config, "multi_account_contribution_group_for_strategy", None)
    for strategy in getattr(config, "strategies", []):
        strategy_id = getattr(strategy, "id", None)
        if not strategy_id:
            continue
        group = group_method(strategy_id) if callable(group_method) else None
        if group is not None:
            pairs = [
                (target.account_id, target.execution_sleeve) for target in group.account_targets
            ]
        else:
            account_id = getattr(strategy, "account_id", None)
            sleeve = getattr(strategy, "execution_sleeve", None)
            pairs = [(str(account_id), str(sleeve))] if account_id and sleeve else []
        if pairs:
            scopes[str(strategy_id)] = pairs
    return scopes


def build_strategy_actual_performance_table(
    store: StateStore,
    config: MaestroConfig | None,
    limit: int = 200,
    attribution_limit: int = 5000,
) -> list[dict[str, Any]]:
    """Per-strategy performance series valued from ACTUAL attributed holdings.

    Unlike ``build_strategy_book_performance_table`` (whose book values are
    target projections — total portfolio value x target weight), this series
    values the quantities the attribution ledger assigns to each strategy
    bucket with the prices of each account's broker snapshots over time.
    Quantity changes (fills) enter TWR as explicit cash flows, so the returns
    measure the strategy's actual holdings performance, are immune to capital
    shared with other strategies, and stay unaffected by pricing regressions
    in strategy-run valuations.
    """
    scopes = _strategy_attribution_scopes(config) if config is not None else {}
    if not scopes:
        return []
    scoped_accounts = {account_id for pairs in scopes.values() for account_id, _ in pairs}

    # Attribution history grouped into whole-account versions (each save writes
    # the account's full position set), oldest first.
    versions: list[dict[str, Any]] = []
    version_map: dict[tuple[str, Any, Any], dict[str, Any]] = {}
    for row in reversed(store.list_account_attribution_snapshots(limit=attribution_limit)):
        payload = _mapping(row.get("payload"))
        account_id = str(row.get("account_id") or payload.get("account_id") or "")
        if account_id not in scoped_accounts:
            continue
        key = (account_id, payload.get("version"), row.get("run_id"))
        entry = version_map.get(key)
        if entry is None:
            entry = {
                "account_id": account_id,
                "created_at": str(row.get("created_at") or ""),
                "positions": {},
            }
            version_map[key] = entry
            versions.append(entry)
        symbol = str(row.get("symbol") or payload.get("symbol") or "")
        bucket_id = str(row.get("bucket_id") or payload.get("bucket_id") or "")
        entry["positions"][(symbol, bucket_id)] = _float_or_none(payload.get("quantity")) or 0.0

    if not versions:
        return []

    broker_rows = [
        row
        for row in reversed(store.list_broker_account_snapshots(limit=limit))
        if _broker_snapshot_account_id(row) in scoped_accounts
    ]

    attribution_state: dict[str, dict[tuple[str, str], float]] = {}
    prices_by_account: dict[str, dict[str, float]] = {}
    twr_states: dict[str, dict[str, Any]] = {}
    previous_quantities: dict[str, dict[tuple[str, str, str], float]] = {}
    version_index = 0
    rows: list[dict[str, Any]] = []
    for broker_row in broker_rows:
        created_at = str(broker_row.get("created_at") or "")
        # Apply every attribution version recorded up to this valuation time.
        while version_index < len(versions) and versions[version_index]["created_at"] <= created_at:
            version = versions[version_index]
            attribution_state[version["account_id"]] = dict(version["positions"])
            version_index += 1
        account_id = _broker_snapshot_account_id(broker_row)
        payload = _mapping(broker_row.get("payload"))
        account = _mapping(payload.get("account"))
        prices_by_account[account_id] = {
            str(position.get("symbol")): price
            for position in _positions(account)
            if (price := _float_or_none(position.get("current_price"))) is not None
        }
        for strategy_id, pairs in scopes.items():
            if all(pair[0] != account_id for pair in pairs):
                continue
            quantities: dict[tuple[str, str, str], float] = {}
            missing_prices: set[str] = set()
            value = 0.0
            has_ledger = False
            for scope_account, bucket_id in pairs:
                ledger = attribution_state.get(scope_account)
                if ledger is None:
                    continue
                has_ledger = True
                prices = prices_by_account.get(scope_account, {})
                for (symbol, position_bucket), quantity in ledger.items():
                    if position_bucket != bucket_id or quantity <= 0:
                        continue
                    quantities[(scope_account, bucket_id, symbol)] = quantity
                    price = prices.get(symbol)
                    if price is None:
                        missing_prices.add(symbol)
                        continue
                    value += quantity * price
            if not has_ledger:
                continue
            total_value = None if missing_prices else value
            # Quantity deltas are contributions/withdrawals, not performance.
            previous = previous_quantities.get(strategy_id, {})
            cash_flow = 0.0
            for key, quantity in quantities.items():
                scope_account, _, symbol = key
                price = prices_by_account.get(scope_account, {}).get(symbol)
                if price is not None:
                    cash_flow += (quantity - previous.get(key, 0.0)) * price
            for key, quantity in previous.items():
                if key not in quantities:
                    scope_account, _, symbol = key
                    price = prices_by_account.get(scope_account, {}).get(symbol)
                    if price is not None:
                        cash_flow -= quantity * price
            previous_quantities[strategy_id] = quantities
            timestamp = _parse_timestamp(created_at)
            cash_flow_events = (
                [
                    {
                        "timestamp": timestamp,
                        "signed_amount": cash_flow,
                        "payload": {
                            "created_at": created_at,
                            "amount": _round_money(cash_flow),
                            "type": "attributed_fill",
                        },
                    }
                ]
                if abs(cash_flow) > 1e-9 and total_value is not None
                else []
            )
            state = twr_states.setdefault(
                strategy_id,
                {
                    "first_value": None,
                    "previous_value": None,
                    "peak_value": None,
                    "cumulative_cash_flow": 0.0,
                    "twr_growth": 1.0,
                    "previous_timestamp": None,
                    "cash_flow_events": [],
                    "mwr_flows": [],
                },
            )
            performance = _advance_twr_performance_state(
                state,
                total_value,
                cash_flow if total_value is not None else 0.0,
                timestamp,
                cash_flow_events,
            )
            rows.append(
                {
                    "created_at": created_at,
                    "run_id": broker_row.get("run_id"),
                    "strategy_id": strategy_id,
                    "book_id": strategy_id,
                    "label": "actual holdings",
                    "basis": "actual",
                    "book_value": _round_money(total_value),
                    "current_value": _round_money(total_value),
                    "cash_flow": _round_money(cash_flow if total_value is not None else None),
                    "cumulative_cash_flow": performance["cumulative_cash_flow"],
                    "net_pnl": performance["net_pnl"],
                    "period_return": performance["period_return"],
                    "twr": performance["twr"],
                    "cumulative_return": performance["twr"],
                    "mwr": performance["mwr"],
                    "irr": performance["mwr"],
                    "drawdown": performance["drawdown"],
                    "positions": {key[2]: quantity for key, quantity in quantities.items()},
                    "missing_prices": sorted(missing_prices),
                    "cash_flow_events": [event["payload"] for event in cash_flow_events],
                }
            )
    return list(reversed(rows))


def build_account_bucket_attribution_table(
    store: StateStore,
    *,
    prices: dict[str, float],
    target_weights: dict[str, dict[str, float]] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    rows = []
    latest_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in store.list_account_attribution_snapshots(limit=limit):
        payload = _mapping(row.get("payload"))
        key = (
            str(row.get("account_id") or payload.get("account_id") or ""),
            str(row.get("symbol") or payload.get("symbol") or ""),
            str(row.get("bucket_id") or payload.get("bucket_id") or ""),
        )
        if key not in latest_by_key:
            latest_by_key[key] = {**row, "payload": payload}

    values_by_account_bucket: dict[tuple[str, str], float] = {}
    latest_created_at: dict[tuple[str, str], str | None] = {}
    for (account_id, symbol, bucket_id), row in latest_by_key.items():
        quantity = _float_or_none(row["payload"].get("quantity")) or 0.0
        price = _float_or_none(prices.get(symbol))
        if price is None:
            continue
        bucket_key = (account_id, bucket_id)
        values_by_account_bucket[bucket_key] = (
            values_by_account_bucket.get(bucket_key, 0.0) + quantity * price
        )
        latest_created_at[bucket_key] = row.get("created_at")

    totals_by_account: dict[str, float] = {}
    for (account_id, _), value in values_by_account_bucket.items():
        totals_by_account[account_id] = totals_by_account.get(account_id, 0.0) + value

    for (account_id, bucket_id), market_value in sorted(values_by_account_bucket.items()):
        total_value = totals_by_account.get(account_id, 0.0)
        actual_weight = market_value / total_value if total_value > 0 else None
        target_weight = (target_weights or {}).get(account_id, {}).get(bucket_id)
        rows.append(
            {
                "created_at": latest_created_at.get((account_id, bucket_id)),
                "account_id": account_id,
                "bucket_id": bucket_id,
                "market_value": _round_money(market_value),
                "target_weight": target_weight,
                "actual_weight": _round_ratio(actual_weight),
                "status": _bucket_status(actual_weight, target_weight),
            }
        )
    return rows


def _rows_by_run(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        run_id = row.get("run_id")
        if run_id is not None:
            grouped.setdefault(str(run_id), []).append(row)
    return grouped


def _fill_lineage_by_run(store: StateStore, limit: int) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in store.list_system_events_by_type("fill_reconciliation", limit=limit):
        payload = _mapping(row.get("payload"))
        fills = payload.get("applied_fills", [])
        if not isinstance(fills, list):
            continue
        run_id = row.get("run_id")
        if run_id is None:
            continue
        grouped.setdefault(str(run_id), []).extend(
            _fill_lineage_item(fill) for fill in fills if isinstance(fill, dict)
        )
    return grouped


def _strategy_lineage_orders(
    orders: list[dict[str, Any]],
    allocations: dict[str, Any],
) -> list[dict[str, Any]]:
    allocation_symbols = {str(symbol) for symbol in allocations}
    output = []
    for order in orders:
        payload = _mapping(order.get("payload"))
        symbol = order.get("symbol") or payload.get("symbol")
        if allocation_symbols and str(symbol) not in allocation_symbols:
            continue
        output.append(
            {
                "created_at": order.get("created_at"),
                "run_id": order.get("run_id"),
                "order_id": order.get("order_id") or payload.get("order_id"),
                "broker_order_id": payload.get("broker_order_id"),
                "symbol": symbol,
                "side": order.get("side") or payload.get("side"),
                "notional": order.get("notional") or payload.get("notional"),
            }
        )
    return output


def _strategy_lineage_fills(
    fills: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    allocations: dict[str, Any],
) -> list[dict[str, Any]]:
    broker_order_ids = {
        str(order["broker_order_id"]) for order in orders if order.get("broker_order_id")
    }
    allocation_symbols = {str(symbol) for symbol in allocations}
    output = []
    for fill in fills:
        broker_order_id = fill.get("broker_order_id")
        symbol = fill.get("symbol")
        if broker_order_ids and str(broker_order_id) not in broker_order_ids:
            continue
        if not broker_order_ids and allocation_symbols and str(symbol) not in allocation_symbols:
            continue
        output.append(fill)
    return output


def _fill_lineage_item(fill: dict[str, Any]) -> dict[str, Any]:
    return {
        "broker_order_id": fill.get("broker_order_id"),
        "symbol": fill.get("symbol"),
        "side": fill.get("side"),
        "quantity": fill.get("quantity"),
        "notional": fill.get("notional"),
        "price": fill.get("price"),
        "filled_at": fill.get("filled_at"),
    }


def _strategy_run_lineage(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        "created_at": signal.get("created_at"),
        "run_id": signal.get("run_id"),
        "strategy_id": signal.get("strategy_id"),
        "signal_action": signal.get("signal_action"),
        "signal_symbol": signal.get("signal_symbol"),
        "confidence": signal.get("confidence"),
        "validation_ok": signal.get("validation_ok"),
    }


def _strategy_lineage_status(
    signal: dict[str, Any],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
) -> str:
    if fills:
        return "filled"
    if orders:
        return "ordered"
    if signal:
        return "proposed"
    return "book_only"


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
    broker_summary = build_broker_account_summary(store, config)
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
            "observations_count": 0,
            "cash_difference": None,
            "broker_account_id": None,
        }
    payload = latest.get("payload", {})
    return {
        "created_at": latest.get("created_at"),
        "passed": payload.get("passed"),
        "issues_count": len(payload.get("issues", [])),
        "observations_count": len(payload.get("observations", [])),
        "cash_difference": payload.get("cash_difference"),
        "broker_account_id": payload.get("broker_account_id"),
    }


def build_latest_signal_package_card(store: StateStore) -> dict[str, Any]:
    rows = store.list_system_events_by_type("signal_package", limit=1)
    if not rows:
        return {
            "created_at": None,
            "signal_run_id": None,
            "status": "missing",
            "action_required": False,
            "actionable_signal_run_id": None,
            "approval_consumed": False,
            "approval_run_id": None,
            "orders_preview_count": 0,
            "loaded_strategies": [],
            "datahub_issue_count": 0,
            "no_op_reason": None,
        }
    row = rows[0]
    signal_run_id = str(row.get("run_id") or "")
    payload = store.load_signal_package(signal_run_id) or row.get("payload", {})
    approval_consumed = bool(payload.get("approval_consumed"))
    action_required = bool(payload.get("action_required"))
    return {
        "created_at": row.get("created_at"),
        "signal_run_id": payload.get("signal_run_id") or signal_run_id,
        "status": payload.get("status"),
        "action_required": action_required,
        "actionable_signal_run_id": signal_run_id
        if action_required and not approval_consumed
        else None,
        "approval_consumed": approval_consumed,
        "approval_run_id": payload.get("approval_run_id"),
        "orders_preview_count": payload.get("orders_preview_count", 0),
        "loaded_strategies": payload.get("loaded_strategies", []),
        "datahub_issue_count": (payload.get("datahub_evidence") or {}).get(
            "issue_count",
            len(payload.get("data_quality_issues", [])),
        ),
        "no_op_reason": payload.get("no_op_reason"),
        "payload": payload,
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
    notional_by_currency: dict[str, float] = {}
    for row in store.list_system_events_by_type("live_order_result", limit=1000):
        payload = row.get("payload", {})
        if payload.get("submitted_date") != today:
            continue
        count += 1
        row_notional = float(payload.get("notional", 0.0))
        notional += row_notional
        currency = _live_order_result_currency(payload, config.portfolio.base_currency)
        notional_by_currency[currency] = notional_by_currency.get(currency, 0.0) + row_notional
    limits = config.execution.live_order_limits
    return {
        "date": today,
        "order_count": count,
        "max_daily_live_order_count": limits.max_daily_order_count,
        "notional": notional,
        "notional_by_currency": notional_by_currency,
        "max_daily_live_notional": limits.max_daily_notional,
        "max_daily_live_notional_by_currency": {
            currency.value: value
            for currency, value in limits.max_daily_notional_by_currency.items()
        },
    }


def _live_order_result_currency(payload: dict[str, Any], default: str) -> str:
    request = payload.get("request")
    if isinstance(request, dict) and request.get("currency"):
        return str(request["currency"])
    if payload.get("currency"):
        return str(payload["currency"])
    return default


def build_system_events_table(store: StateStore, limit: int = 20) -> list[dict[str, Any]]:
    rows = []
    for row in store.list_system_events(limit=limit):
        payload = row.get("payload", {})
        event_type = row.get("event_type")
        required_fields = list(required_system_event_fields(str(event_type)))
        missing_fields = missing_system_event_required_fields(str(event_type), payload)
        rows.append(
            {
                "created_at": row.get("created_at"),
                "run_id": row.get("run_id"),
                "event_type": event_type,
                "schema_status": _event_schema_status(required_fields, missing_fields),
                "required_fields": required_fields,
                "missing_required_fields": missing_fields,
                "error_type": payload.get("error_type"),
                "error_message": payload.get("error_message") or payload.get("error"),
                "payload": payload,
            }
        )
    return rows


def _event_schema_status(required_fields: list[str], missing_fields: list[str]) -> str:
    if not required_fields:
        return "untracked"
    if missing_fields:
        return "missing_required_fields"
    return "ok"


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
        "notional": _currency_limit_status(
            daily_live_usage.get("notional_by_currency"),
            daily_live_usage.get("max_daily_live_notional_by_currency"),
            daily_live_usage.get("notional"),
            daily_live_usage.get("max_daily_live_notional"),
            "Daily live notional",
        ),
    }


def _currency_limit_status(
    values_by_currency: object,
    limits_by_currency: object,
    fallback_value: object,
    fallback_limit: object,
    label: str,
) -> dict[str, Any]:
    if (
        isinstance(values_by_currency, dict)
        and isinstance(limits_by_currency, dict)
        and limits_by_currency
    ):
        statuses = {
            str(currency): _limit_status(
                values_by_currency.get(currency, 0.0),
                limit,
                f"{label} {currency}",
            )
            for currency, limit in limits_by_currency.items()
        }
        status_values = {item["status"] for item in statuses.values()}
        if "exceeded" in status_values:
            status = "exceeded"
        elif "warning" in status_values:
            status = "warning"
        elif "ok" in status_values:
            status = "ok"
        else:
            status = "not_configured"
        return {"status": status, "by_currency": statuses}
    return _limit_status(fallback_value, fallback_limit, label)


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
    policy = {
        "max_age_seconds": max_age_seconds,
        "stale_when_age_gt_max": True,
        "failed_precedence": True,
    }
    if max_age_seconds <= 0:
        return {
            "name": name,
            "status": "not_configured",
            "payload_status": None,
            "created_at": None,
            "created_at_display": None,
            "age_seconds": None,
            "max_age_seconds": max_age_seconds,
            "policy": policy,
            "run_id": None,
        }
    if row is None:
        return {
            "name": name,
            "status": "missing",
            "payload_status": None,
            "created_at": None,
            "created_at_display": None,
            "age_seconds": None,
            "max_age_seconds": max_age_seconds,
            "policy": policy,
            "run_id": None,
        }
    age_seconds = _age_seconds(row.get("created_at"))
    payload_status = "failed" if failed else "fresh"
    if failed:
        status = "failed"
    elif age_seconds is None or age_seconds > max_age_seconds:
        status = "stale"
    else:
        status = "fresh"
    return {
        "name": name,
        "status": status,
        "payload_status": payload_status,
        "created_at": row.get("created_at"),
        "created_at_display": format_operator_time(row.get("created_at"), timezone),
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "policy": policy,
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


def _broker_reported_total_value(account: dict[str, Any]) -> float | None:
    cash_balance = _mapping(account.get("cash_balance"))
    total_value = _first_float(
        cash_balance,
        {},
        ("total_asset_value", "total_value", "total_equity", "net_asset_value"),
    )
    if total_value is not None:
        return total_value
    return _first_float(
        account,
        {},
        ("total_value", "total_equity", "equity", "net_asset_value"),
    )


def _account_total_value(account: dict[str, Any], positions: list[dict[str, Any]]) -> float | None:
    total_value = _broker_reported_total_value(account)
    if total_value is not None:
        return total_value
    cash = _float_or_none(account.get("cash"))
    if cash is None and not positions:
        return None
    return (cash or 0.0) + sum(_position_market_value(position) for position in positions)


def _account_value_components(
    account: dict[str, Any],
    positions: list[dict[str, Any]],
    payload: dict[str, Any],
    default_currency: str = "KRW",
) -> dict[str, float]:
    """Native-currency breakdown of one account's assets.

    Prefers the broker's own pre-aggregated total (already correctly
    denominated in the account's reporting currency) when available.
    Otherwise sums cash and each position at ITS OWN currency: brokers such
    as Toss report a mixed KRW/USD portfolio under a single account with no
    aggregate total, and summing raw prices without this split silently
    mixes units (a $353 USD position would be counted as if it were 353
    KRW). `_snapshot_currency` only labels the whole account with one
    currency, so it cannot express that split — this looks at each
    position's own `currency` field instead.

    `default_currency` is used only when NO currency information exists
    anywhere on the account/payload/position (`_snapshot_currency` falls
    back to the "UNKNOWN" sentinel) — e.g. older paper/sandbox fixtures that
    never set a currency field. In that case there is nothing to split by
    definition, so this preserves the historical single-bucket behavior
    instead of producing an unconvertible "UNKNOWN" component.
    """
    reported_total = _broker_reported_total_value(account)
    account_currency = _snapshot_currency(account, payload)
    if account_currency == "UNKNOWN":
        account_currency = default_currency
    if reported_total is not None:
        return {account_currency: reported_total}

    components: dict[str, float] = {}
    for currency, cash in _account_cash_components(
        account,
        payload,
        default_currency=default_currency,
    ).items():
        components[currency] = components.get(currency, 0.0) + cash
    for position in positions:
        currency = _position_currency(position, account, payload, default_currency)
        components[currency] = components.get(currency, 0.0) + _position_market_value(position)
    return components


def broker_snapshot_value_components(
    row: dict[str, Any],
    *,
    default_currency: str = "KRW",
) -> dict[str, float]:
    payload = _mapping(row.get("payload"))
    account = _mapping(payload.get("account"))
    return _account_value_components(
        account,
        _positions(account),
        payload,
        default_currency=default_currency,
    )


def _expected_account_ids(source_rows: list[dict[str, Any]]) -> set[str]:
    """Accounts a complete portfolio total has to include.

    The performance builders walk snapshots forward in time and carry each
    account's latest value forward, because accounts refresh on independent
    timers and rarely land in the same group. That means the first groups in
    the window are summed before every account has reported: the total is a
    partial one, and since it anchors `first_value`, cumulative return is
    computed against it (a 3-account portfolio showed +351% this way).

    "Expected" is the set observed anywhere in the window rather than the
    configured set, so an account that is configured but has never synced
    (`mapped · not synced`) cannot blank the series forever. Disabled
    accounts are already filtered out of `source_rows` upstream.
    """
    return {
        account_id for row in source_rows if (account_id := _broker_snapshot_account_id(row))
    }


def _position_currency(
    position: dict[str, Any],
    account: dict[str, Any],
    payload: dict[str, Any],
    default_currency: str = "KRW",
) -> str:
    """Currency one position is denominated in.

    Shared by `_account_value_components` and the position exposure table so
    both bucket a position the same way; a mismatch there silently divides a
    USD value by a KRW total.
    """
    account_currency = _snapshot_currency(account, payload)
    if account_currency == "UNKNOWN":
        account_currency = default_currency
    cash_balance = _mapping(account.get("cash_balance"))
    cash_currency = str(cash_balance.get("currency") or account_currency)
    return str(position.get("currency") or cash_currency)


def _account_cash_components(
    account: dict[str, Any],
    payload: dict[str, Any],
    default_currency: str = "KRW",
) -> dict[str, float]:
    """Cash-only counterpart to `_account_value_components` (no positions)."""
    cash_by_currency = _mapping(account.get("cash_by_currency"))
    if cash_by_currency:
        return {
            str(currency): cash
            for currency, value in cash_by_currency.items()
            if (cash := _float_or_none(value)) is not None
        }
    account_currency = _snapshot_currency(account, payload)
    if account_currency == "UNKNOWN":
        account_currency = default_currency
    cash_balance = _mapping(account.get("cash_balance"))
    cash_currency = str(cash_balance.get("currency") or account_currency)
    cash = _float_or_none(account.get("cash"))
    if cash is None:
        return {}
    return {cash_currency: cash}


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



def _strategy_cash_flows_by_strategy(store: StateStore) -> dict[str, list[dict[str, Any]]]:
    flows: dict[str, list[dict[str, Any]]] = {}
    for row in store.list_system_events_by_type("strategy_cash_flow", limit=1000):
        payload = _mapping(row.get("payload"))
        strategy_id = str(payload.get("strategy_id") or "")
        if not strategy_id:
            continue
        amount = _float_or_none(payload.get("amount"))
        if amount is None:
            continue
        flow_type = str(payload.get("flow_type") or "deposit").lower()
        signed_amount = -abs(amount) if flow_type in {"withdrawal", "withdraw"} else abs(amount)
        timestamp = _parse_timestamp(payload.get("effective_at") or row.get("created_at"))
        if timestamp is None:
            continue
        event = {
            "timestamp": timestamp,
            "signed_amount": signed_amount,
            "payload": {
                **payload,
                "amount": amount,
                "signed_amount": signed_amount,
                "effective_at": payload.get("effective_at") or row.get("created_at"),
                "run_id": row.get("run_id"),
            },
        }
        flows.setdefault(strategy_id, []).append(event)
    for strategy_flows in flows.values():
        strategy_flows.sort(key=lambda item: item["timestamp"])
    return flows


def load_account_cash_flow_facts(
    store: StateStore,
    *,
    after: datetime | None = None,
    account_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Recorded cash flows, normalised before any performance interpretation.

    Only an explicit account scope can filter facts here. What a retained flow
    means depends on which scope is asking -- a currency conversion crosses no
    account and no portfolio boundary, yet it is exactly what moves one currency
    sleeve into another --
    so reading the events is kept apart from deciding what they do to a return.
    """
    facts: list[dict[str, Any]] = []
    for row in store.list_system_events_by_type(SystemEventType.ACCOUNT_CASH_FLOW, limit=None):
        payload = _mapping(row.get("payload"))
        amount = _float_or_none(payload.get("amount"))
        timestamp = _parse_timestamp(payload.get("effective_at") or row.get("created_at"))
        currency = str(payload.get("currency") or "").upper()
        if amount is None or timestamp is None or not currency:
            continue
        if after is not None and timestamp <= after:
            continue
        account_id = str(payload.get("account_id") or "")
        if account_ids is not None and account_id not in account_ids:
            continue
        flow_type = str(payload.get("flow_type") or "deposit").lower()
        signed_amount = -abs(amount) if flow_type == "withdrawal" else abs(amount)
        facts.append(
            {
                "timestamp": timestamp,
                "signed_amount": signed_amount,
                "currency": currency,
                "account_id": account_id,
                "transfer_id": payload.get("transfer_id"),
                # Events written before the class existed are external
                # transfers, which is what they were recorded as.
                "flow_class": str(payload.get("flow_class") or EXTERNAL_TRANSFER).lower(),
                "payload": {
                    **payload,
                    "signed_amount": signed_amount,
                    "run_id": row.get("run_id"),
                },
            }
        )
    facts.sort(key=lambda item: item["timestamp"])
    return facts


def cash_flow_effects_for_scope(
    facts: list[dict[str, Any]],
    scope: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Flows a scope must neutralise, and reasons the figure may be incomplete.

    Every scope shares one rule table so the three performance views cannot
    drift into disagreeing about the same deposit.  Dividends and costs are the
    portfolio earning or spending its own cash and are never neutralised at any
    scope; neutralising them would move a real gain or loss out of the return.
    """
    neutralizing = _SCOPE_NEUTRALIZING_CLASSES[scope]
    linked_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    linked_classes: dict[str, set[str]] = {}
    invalid_groups: set[tuple[str, str]] = set()
    reasons: list[dict[str, Any]] = []
    for fact in facts:
        flow_class = fact["flow_class"]
        if flow_class not in LINKED_FLOW_CLASSES:
            continue
        transfer_id = str(fact["transfer_id"] or "")
        if not transfer_id:
            reasons.append(_linked_flow_reason("unpaired_linked_cash_flow", [fact]))
            continue
        key = (flow_class, transfer_id)
        linked_groups.setdefault(key, []).append(fact)
        linked_classes.setdefault(transfer_id, set()).add(flow_class)

    for transfer_id, classes in linked_classes.items():
        if len(classes) <= 1:
            continue
        groups = [
            linked_groups[(flow_class, transfer_id)] for flow_class in sorted(classes)
        ]
        combined = [fact for group in groups for fact in group]
        reasons.append(
            _linked_flow_reason(
                "conflicting_linked_cash_flow_classes",
                combined,
                message=f"transfer {transfer_id} is reused across linked flow classes",
            )
        )
        invalid_groups.update((flow_class, transfer_id) for flow_class in classes)

    for key, group in linked_groups.items():
        if key in invalid_groups:
            continue
        error = _linked_flow_group_error(key[0], group)
        if error is None:
            continue
        code = "unpaired_linked_cash_flow" if len(group) == 1 else "invalid_linked_cash_flow"
        reasons.append(_linked_flow_reason(code, group, message=error))
        invalid_groups.add(key)

    effects: list[dict[str, Any]] = []
    for fact in facts:
        flow_class = fact["flow_class"]
        transfer_id = str(fact["transfer_id"]) if fact["transfer_id"] else ""
        if flow_class in LINKED_FLOW_CLASSES and (
            not transfer_id or (flow_class, transfer_id) in invalid_groups
        ):
            continue
        if flow_class not in neutralizing:
            continue
        effects.append(fact)
    return effects, reasons


def _linked_flow_group_error(
    flow_class: str,
    group: list[dict[str, Any]],
) -> str | None:
    if len(group) != 2:
        return f"{flow_class} needs exactly two legs, found {len(group)}"
    signs = {1 if fact["signed_amount"] > 0 else -1 for fact in group}
    if signs != {-1, 1}:
        return f"{flow_class} needs one withdrawal and one deposit"
    if len({fact["timestamp"] for fact in group}) != 1:
        return f"{flow_class} legs must share one effective_at"
    account_ids = {fact["account_id"] for fact in group}
    currencies = {fact["currency"] for fact in group}
    if flow_class == FX_CONVERSION:
        if len(account_ids) != 1 or len(currencies) != 2:
            return "fx_conversion needs one account and two different currencies"
    elif flow_class == INTERNAL_TRANSFER:
        if len(account_ids) != 2:
            return "internal_transfer needs two different accounts"
        if len(currencies) == 1:
            amounts = [abs(float(fact["signed_amount"])) for fact in group]
            currency = next(iter(currencies))
            tolerance = 1.0 if currency in {"KRW", "JPY"} else 0.01
            if abs(amounts[0] - amounts[1]) > tolerance:
                return "same-currency internal_transfer legs must have equal amounts"
    return None


def _linked_flow_reason(
    code: str,
    facts: list[dict[str, Any]],
    *,
    message: str | None = None,
) -> dict[str, Any]:
    first = min(facts, key=lambda fact: fact["timestamp"])
    flow_class = str(first["flow_class"])
    return {
        "code": code,
        "message": message
        or f"{flow_class} leg for account {first['account_id']} has no counterpart leg",
        "run_id": first["payload"].get("run_id"),
        "transfer_id": first.get("transfer_id"),
        "account_ids": sorted({str(fact["account_id"]) for fact in facts}),
        "effective_at": first["timestamp"].isoformat(),
    }


def _cash_flow_quality(
    reasons: list[dict[str, Any]],
    *,
    account_ids: set[str] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    relevant = []
    for reason in reasons:
        if account_ids is not None and not account_ids.intersection(
            reason.get("account_ids") or []
        ):
            continue
        reason_at = _parse_timestamp(reason.get("effective_at"))
        if as_of is not None and reason_at is not None and reason_at > as_of:
            continue
        relevant.append(reason)
    return {"status": "degraded" if relevant else "ok", "reasons": relevant}


def _account_lifecycle_events(
    store: StateStore,
    *,
    after: datetime | None = None,
) -> list[dict[str, Any]]:
    events = []
    for event_type in (
        SystemEventType.ACCOUNT_TRACKING_STARTED,
        SystemEventType.ACCOUNT_TRACKING_ENDED,
    ):
        for row in store.list_system_events_by_type(event_type, limit=10000):
            payload = _mapping(row.get("payload"))
            timestamp = _parse_timestamp(payload.get("effective_at") or row.get("created_at"))
            account_id = str(payload.get("account_id") or "")
            if timestamp is None or not account_id:
                continue
            if after is not None and timestamp <= after:
                continue
            events.append(
                {
                    "timestamp": timestamp,
                    "event_type": str(event_type),
                    "account_id": account_id,
                }
            )
    events.sort(key=lambda item: item["timestamp"])
    return events


def _fx_rate_events(store: StateStore) -> list[dict[str, Any]]:
    events = []
    for row in store.list_system_events_by_type("fx_rate_snapshot", limit=10000):
        payload = _mapping(row.get("payload"))
        timestamp = _parse_timestamp(
            payload.get("as_of") or payload.get("fetched_at") or row.get("created_at")
        )
        if timestamp is None:
            continue
        events.append(
            {
                "timestamp": timestamp,
                "created_at": row.get("created_at"),
                "payload": payload,
                "event_id": row.get("id"),
            }
        )
    events.sort(key=lambda item: item["timestamp"])
    return events


def _fx_snapshot_as_of(
    events: list[dict[str, Any]],
    timestamp: datetime,
) -> dict[str, Any]:
    selected = None
    for event in events:
        if event["timestamp"] > timestamp:
            break
        selected = event
    if selected is None:
        return {
            "status": "missing",
            "source": None,
            "as_of": None,
            "rate": None,
            "rates": {},
            "event_id": None,
        }
    payload = selected["payload"]
    max_age_seconds = int(
        payload.get("max_age_seconds") or payload.get("stale_after_seconds") or 86400
    )
    age_seconds = max(0.0, (timestamp - selected["timestamp"]).total_seconds())
    rates = _mapping(payload.get("rates"))
    return {
        "status": "fresh" if age_seconds <= max_age_seconds else "stale",
        "source": payload.get("source"),
        "as_of": payload.get("as_of") or selected["created_at"],
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "rate": _first_float(payload, rates, ("USD/KRW", "USDKRW", "usd_krw")),
        "rates": rates,
        "event_id": selected["event_id"],
    }


def _assign_daily_returns(rows: list[dict[str, Any]]) -> None:
    previous_close_growth: float | None = None
    current_day = None
    day_rows: list[dict[str, Any]] = []
    for row in rows:
        timestamp = _parse_timestamp(row.get("created_at"))
        if timestamp is None:
            continue
        day = timestamp.astimezone(ZoneInfo("Asia/Seoul")).date()
        if current_day is not None and day != current_day and day_rows:
            close_return = _float_or_none(day_rows[-1].get("cumulative_return"))
            if close_return is not None:
                previous_close_growth = 1.0 + close_return
            day_rows = []
        current_day = day
        day_rows.append(row)
        cumulative_return = _float_or_none(row.get("cumulative_return"))
        row["daily_return"] = (
            _round_ratio((1.0 + cumulative_return) / previous_close_growth - 1.0)
            if cumulative_return is not None
            and previous_close_growth is not None
            and previous_close_growth != 0
            else None
        )


def _period_cash_flow_events(
    events: list[dict[str, Any]],
    previous_timestamp: datetime | None,
    current_timestamp: datetime | None,
) -> list[dict[str, Any]]:
    if current_timestamp is None:
        return []
    period_events = []
    for event in events:
        timestamp = event["timestamp"]
        after_previous = previous_timestamp is None or timestamp > previous_timestamp
        if after_previous and timestamp <= current_timestamp:
            period_events.append(event)
    return period_events


def _converted_period_cash_flow(
    cash_flows: list[dict[str, Any]],
    fx_events: list[dict[str, Any]],
    *,
    previous_timestamp: datetime | None,
    timestamp: datetime | None,
    currency: str,
    account_id: str | None = None,
) -> tuple[float | None, list[dict[str, Any]]]:
    """Net external cash flow for one period, each event at its own FX rate.

    Converting the whole period at the closing rate would produce a total that
    no longer matches the amounts the return was neutralised by, so the two
    figures would stop reconciling.  A missing rate returns ``None`` rather
    than a partial sum that would silently understate the flow.
    """
    events = _period_cash_flow_events(cash_flows, previous_timestamp, timestamp)
    if account_id is not None:
        events = [event for event in events if event.get("account_id") == account_id]
    total = 0.0
    converted_events: list[dict[str, Any]] = []
    for event in events:
        event_fx = _fx_snapshot_as_of(fx_events, event["timestamp"])
        converted = _convert_components(
            {event["currency"]: event["signed_amount"]},
            currency,
            event_fx,
        )
        if converted is None:
            return None, []
        total += converted
        converted_events.append(
            {
                "timestamp": event["timestamp"],
                "signed_amount": converted,
                "payload": {
                    **event["payload"],
                    "display_amount": converted,
                    "display_currency": currency,
                    "fx_rate": _fx_rate(event["currency"], currency, event_fx),
                    "fx_as_of": event_fx.get("as_of"),
                    "fx_event_id": event_fx.get("event_id"),
                },
            }
        )
    return total, converted_events


def _advance_twr_performance_state(
    state: dict[str, Any],
    total_value: float | None,
    cash_flow: float,
    timestamp: datetime | None,
    cash_flow_events: list[dict[str, Any]],
) -> dict[str, float | None]:
    period_return = None
    previous_value = state["previous_value"]
    if previous_value is not None and previous_value > 0 and total_value is not None:
        period_return = (total_value - previous_value - cash_flow) / previous_value
        state["twr_growth"] *= 1.0 + period_return
    if state["first_value"] is None and total_value is not None:
        state["first_value"] = total_value
        if timestamp is not None:
            state["mwr_flows"].append((timestamp, -total_value))
    elif total_value is not None:
        state["cumulative_cash_flow"] += cash_flow
    for event in cash_flow_events:
        state["cash_flow_events"].append(event["payload"])
        state["mwr_flows"].append((event["timestamp"], -event["signed_amount"]))
    net_pnl = None
    first_value = state["first_value"]
    if first_value is not None and total_value is not None:
        net_pnl = total_value - first_value - state["cumulative_cash_flow"]
    if total_value is not None:
        state["previous_value"] = total_value
    if timestamp is not None:
        state["previous_timestamp"] = timestamp
    twr = state["twr_growth"] - 1.0 if first_value is not None else None
    state["twr_peak"] = max(float(state.get("twr_peak") or 1.0), state["twr_growth"])
    drawdown = _safe_weight(state["twr_growth"], state["twr_peak"])
    if drawdown is not None:
        drawdown -= 1.0
    mwr = _money_weighted_return(state["mwr_flows"], timestamp, total_value)
    return {
        "period_return": _round_ratio(period_return),
        "twr": _round_ratio(twr),
        "mwr": _round_ratio(mwr),
        "cumulative_cash_flow": _round_money(state["cumulative_cash_flow"]),
        "net_pnl": _round_money(net_pnl),
        "drawdown": _round_ratio(drawdown),
    }


def _money_weighted_return(
    flows: list[tuple[datetime, float]],
    ending_timestamp: datetime | None,
    ending_value: float | None,
) -> float | None:
    if ending_timestamp is None or ending_value is None or not flows:
        return None
    dated_flows = [(timestamp, amount) for timestamp, amount in flows]
    dated_flows.append((ending_timestamp, ending_value))
    if not any(amount < 0 for _, amount in dated_flows) or not any(
        amount > 0 for _, amount in dated_flows
    ):
        return None
    base_date = min(timestamp for timestamp, _ in dated_flows)

    def npv(rate: float) -> float:
        total = 0.0
        for timestamp, amount in dated_flows:
            years = max((timestamp - base_date).total_seconds() / (365.0 * 24 * 60 * 60), 0.0)
            total += amount / ((1.0 + rate) ** years)
        return total

    low = -0.999999
    high = 10.0
    low_value = npv(low)
    high_value = npv(high)
    expansion_count = 0
    while low_value * high_value > 0 and high < 1_000_000 and expansion_count < 12:
        high *= 10.0
        high_value = npv(high)
        expansion_count += 1
    if low_value * high_value > 0:
        return None
    for _ in range(100):
        mid = (low + high) / 2.0
        mid_value = npv(mid)
        if abs(mid_value) < 1e-7:
            return mid
        if low_value * mid_value <= 0:
            high = mid
            high_value = mid_value
        else:
            low = mid
            low_value = mid_value
    return (low + high) / 2.0


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _round_money(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 6)

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
            "observations_count": len(payload.get("observations", [])),
        }
    return output


def _reconciliation_status(reconciliation: dict[str, Any] | None) -> str:
    if reconciliation is None:
        return "unreconciled"
    if reconciliation.get("passed") is True:
        return (
            "passed_with_observations"
            if reconciliation.get("observations_count", 0)
            else "passed"
        )
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


def _bucket_status(actual_weight: float | None, target_weight: float | None) -> str:
    if actual_weight is None or target_weight is None:
        return "missing_target"
    if actual_weight > target_weight + 1e-6:
        return "over_target"
    if actual_weight < target_weight - 1e-6:
        return "under_target"
    return "on_target"


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

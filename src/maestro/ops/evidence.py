from pathlib import Path
from typing import Any

from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.enums import RunMode
from maestro.credentials import DEFAULT_CREDENTIAL_RESOLVER
from maestro.monitoring.health import HealthService
from maestro.monitoring.health_models import HealthReport
from maestro.ops.preflight import private_beta_failures
from maestro.state.events import SystemEventType
from maestro.state.store import StateStore


def build_operator_evidence(
    config: MaestroConfig,
    store: StateStore,
    *,
    config_path: Path | str | None = None,
) -> dict[str, Any]:
    report = HealthService(config, store).run()
    config_label = str(config_path) if config_path is not None else "<config>"
    stages = _personal_stages(config, report, config_label)
    beta_failures = private_beta_failures(config, report)
    return {
        "schema_version": 1,
        "generated_at": utc_now().isoformat(),
        "overall_status": _overall_personal_status(stages),
        "config": _config_summary(config, config_label),
        "stages": stages,
        "health": _health_summary(report),
        "private_beta": {
            "status": "ok" if not beta_failures else "fail",
            "failures": beta_failures,
        },
        "latest": _latest_evidence(store),
        "counts": store.status()["counts"],
    }


def _personal_stages(
    config: MaestroConfig,
    report: HealthReport,
    config_label: str,
) -> list[dict[str, str]]:
    checks = {check.name: check for check in report.checks}
    return [
        _personal_stage(
            "paper_ready",
            _worst_status(
                checks,
                ["config", "state_db", "audit_path", "audit_integrity", "datahub"],
            ),
            "local config, state, audit, and DataHub checks are usable",
            f"maestro health --config {config_label}",
        ),
        _personal_stage(
            "readonly_ready",
            _all_ok(checks, ["kis_env", "broker_snapshot", "reconciliation"]),
            "KIS env, broker snapshot, and reconciliation are ready",
            f"maestro live-smoke --config {config_label} --check kis-readonly",
        ),
        _personal_stage(
            "telegram_ready",
            _telegram_personal_status(config),
            "Telegram approval config and token are ready",
            f"maestro live-smoke --config {config_label} --check telegram-approval",
        ),
        _personal_stage(
            "dry_run_ready",
            _dry_run_personal_status(config, checks),
            "approval-gated dry-run config is ready",
            f"maestro live-smoke --config {config_label} --check live-dry-run",
        ),
        _personal_stage(
            "minimum_live_ready",
            _minimum_live_personal_status(config, report),
            "minimum-size approval-gated live order gate is ready",
            f"maestro beta-preflight --config {config_label}",
        ),
    ]


def _config_summary(config: MaestroConfig, config_label: str) -> dict[str, Any]:
    return {
        "path": config_label,
        "mode": config.mode.value,
        "state_path": config.state.sqlite_path,
        "audit_path": config.audit.jsonl_path,
        "strategies": [
            {
                "id": strategy.id,
                "enabled": strategy.enabled,
                "effective_mode": config.mode.value if strategy.enabled else None,
            }
            for strategy in config.strategies
        ],
        "approval": {
            "enabled": config.approval.enabled,
            "provider": config.approval.provider,
            "require_approval": config.approval.require_approval,
            "telegram_chats": len(config.approval.telegram_allowed_chat_ids),
            "telegram_whitelisted_users": len(config.approval.whitelisted_user_ids),
            "telegram_token_env": config.approval.telegram_bot_token_env,
            "telegram_token_present": DEFAULT_CREDENTIAL_RESOLVER.present(
                config.approval.telegram_bot_token_env
            ),
        },
        "execution": {
            "order_posture": config.execution.order_posture,
            "live_order_enabled": config.execution.live_order_enabled,
            "live_order_dry_run": config.execution.live_order_dry_run,
            "allowed_order_type": config.execution.allowed_order_type.value,
            "max_live_order_notional": config.execution.live_order_limits.max_order_notional,
            "max_live_order_notional_by_currency": {
                currency.value: value
                for currency, value in (
                    config.execution.live_order_limits.max_order_notional_by_currency.items()
                )
            },
            "max_daily_live_notional": config.execution.live_order_limits.max_daily_notional,
            "max_daily_live_notional_by_currency": {
                currency.value: value
                for currency, value in (
                    config.execution.live_order_limits.max_daily_notional_by_currency.items()
                )
            },
            "max_daily_live_order_count": (
                config.execution.live_order_limits.max_daily_order_count
            ),
            "require_reconciliation_pass": config.execution.require_reconciliation_pass,
            "require_market_session": config.execution.market_session.required,
            "require_broker_quote_validation": (
                config.execution.broker_validation.require_quote_validation
            ),
            "require_broker_risk_validation": (
                config.execution.broker_validation.require_risk_validation
            ),
            "daily_loss_limit": config.execution.live_order_limits.daily_loss_limit,
            "daily_loss_limit_by_currency": {
                currency.value: value
                for currency, value in (
                    config.execution.live_order_limits.daily_loss_limit_by_currency.items()
                )
            },
            "heartbeat_max_age_seconds": config.monitoring.heartbeat_max_age_seconds,
            "scheduled_run_max_age_seconds": config.monitoring.scheduled_run_max_age_seconds,
        },
        "kis": {
            "enabled": config.kis.enabled,
            "provider": config.kis.provider,
            "broker_product": config.kis.broker_product.value,
            "broker_products": [
                product.value for product in config.kis.effective_broker_products()
            ],
            "account_id": _mask_identifier(config.kis.account_id),
            "account_id_env": config.kis.account_id_env,
            "account_id_env_present": bool(
                config.kis.account_id_env
                and DEFAULT_CREDENTIAL_RESOLVER.present(config.kis.account_id_env)
            ),
        },
        "portfolio": {
            "allowed_symbols": config.portfolio.allowed_symbols,
            "initial_cash": config.portfolio.initial_cash,
            "cash_by_currency": config.portfolio.cash_by_currency,
        },
    }


def _health_summary(report: HealthReport) -> dict[str, Any]:
    counts = {"ok": 0, "warn": 0, "fail": 0}
    checks = []
    for check in report.checks:
        counts[check.status] = counts.get(check.status, 0) + 1
        checks.append(check.model_dump(mode="json"))
    return {
        "status": report.status,
        "generated_at": report.generated_at,
        "counts": counts,
        "checks": checks,
    }


def _latest_evidence(store: StateStore) -> dict[str, Any]:
    return {
        "broker_snapshot": _broker_snapshot(store.load_latest_broker_account_snapshot()),
        "reconciliation": _reconciliation(
            store.load_latest_system_event(SystemEventType.BROKER_RECONCILIATION)
        ),
        "safety_state": _generic_event(store.load_latest_system_event("safety_state")),
        "heartbeat": _generic_event(
            store.load_latest_system_event(SystemEventType.MAESTRO_HEARTBEAT)
        ),
        "scheduled_run": _generic_event(
            store.load_latest_system_event(SystemEventType.RUN_ONCE_COMPLETED)
        ),
        "approval": _approval(_latest_approval(store)),
        "live_proposal_data_snapshot": _proposal_snapshot(
            store.load_latest_system_event(SystemEventType.LIVE_PROPOSAL_DATA_SNAPSHOT)
        ),
        "live_order_dry_run": _dry_run(
            store.load_latest_system_event(SystemEventType.LIVE_ORDER_DRY_RUN)
        ),
        "live_order_status": _live_order_event(
            store.load_latest_system_event(SystemEventType.LIVE_ORDER_STATUS)
        ),
        "live_order_lifecycle": _live_order_event(
            store.load_latest_system_event(SystemEventType.LIVE_ORDER_LIFECYCLE)
        ),
        "fill_reconciliation": _fill_reconciliation(
            store.load_latest_system_event(SystemEventType.FILL_RECONCILIATION)
        ),
        "broker_baseline_required": _generic_event(
            store.load_latest_system_event(SystemEventType.BROKER_BASELINE_REQUIRED)
        ),
        "live_order_recovery_required": _generic_event(
            store.load_latest_system_event(SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED)
        ),
        "live_order_recovery_completed": _generic_event(
            store.load_latest_system_event(SystemEventType.LIVE_ORDER_RECOVERY_COMPLETED)
        ),
        "live_order_recovery_halt": _generic_event(
            store.load_latest_system_event(SystemEventType.LIVE_ORDER_RECOVERY_HALT)
        ),
    }


def _broker_snapshot(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = row.get("payload", {})
    account = payload.get("account", {})
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "run_id": row.get("run_id"),
        "account_id": _mask_identifier(row.get("account_id") or account.get("account_id")),
        "cash": account.get("cash"),
        "buying_power": account.get("buying_power"),
        "positions_count": len(account.get("positions", [])),
        "current_prices_count": len(payload.get("current_prices", {})),
        "fills_count": len(payload.get("order_fills", [])),
        "unfilled_orders_count": len(payload.get("unfilled_orders", [])),
        "source": account.get("source") or payload.get("source"),
    }


def _reconciliation(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = row.get("payload", {})
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "run_id": row.get("run_id"),
        "passed": payload.get("passed"),
        "issues_count": len(payload.get("issues", [])),
        "cash_difference": payload.get("cash_difference"),
        "broker_account_id": _mask_identifier(payload.get("broker_account_id")),
        "broker_snapshot_id": payload.get("broker_snapshot_id"),
    }


def _approval(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = row.get("payload", {})
    request = payload.get("request", {})
    decision = payload.get("decision", {})
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "run_id": row.get("run_id"),
        "approval_id": row.get("approval_id"),
        "status": decision.get("status"),
        "decided_by": decision.get("decided_by"),
        "order_count": request.get("order_count"),
        "estimated_notional": request.get("estimated_notional"),
    }


def _proposal_snapshot(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = row.get("payload", {})
    risk = payload.get("risk_decision", {})
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "run_id": row.get("run_id"),
        "prices_count": len(payload.get("prices", {})),
        "order_prices_count": len(payload.get("order_prices", {})),
        "data_quality_issues_count": len(payload.get("data_quality_issues", [])),
        "risk_approved": risk.get("approved"),
        "proposed_orders": [
            {
                "order_id": order.get("order_id"),
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "quantity": order.get("quantity"),
                "price": order.get("price"),
                "notional": order.get("notional"),
                "broker_product": order.get("broker_product"),
            }
            for order in payload.get("proposed_orders", [])
        ],
    }


def _dry_run(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = row.get("payload", {})
    request = payload.get("request", {})
    decision = payload.get("approval_decision", {})
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "run_id": row.get("run_id"),
        "order_id": request.get("order_id"),
        "symbol": request.get("symbol"),
        "side": request.get("side"),
        "quantity": request.get("quantity"),
        "limit_price": request.get("limit_price"),
        "notional": payload.get("notional"),
        "approval_id": request.get("approval_id"),
        "approval_status": decision.get("status"),
        "broker_submit_skipped": payload.get("broker_submit_skipped"),
    }


def _live_order_event(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = row.get("payload", {})
    snapshot = payload.get("snapshot", {})
    result = payload.get("result", {})
    request = payload.get("request", {})
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "run_id": row.get("run_id"),
        "event_type": row.get("event_type"),
        "status": payload.get("status") or snapshot.get("status") or result.get("status"),
        "symbol": payload.get("symbol") or snapshot.get("symbol") or request.get("symbol"),
        "order_id": payload.get("order_id") or snapshot.get("order_id") or request.get("order_id"),
        "broker_order_id": snapshot.get("broker_order_id") or result.get("broker_order_id"),
        "message": payload.get("message") or payload.get("failed_reason"),
    }


def _fill_reconciliation(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = row.get("payload", {})
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "run_id": row.get("run_id"),
        "applied_fills": len(payload.get("applied_fills", [])),
        "skipped_fills": len(payload.get("skipped_fills", [])),
        "portfolio_updated": payload.get("portfolio_updated"),
        "cash": payload.get("cash"),
        "positions_count": len(payload.get("positions", {})),
    }


def _generic_event(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    payload = row.get("payload", {})
    return {
        "id": row.get("id"),
        "created_at": row.get("created_at"),
        "run_id": row.get("run_id"),
        "event_type": row.get("event_type"),
        "status": payload.get("status") or payload.get("state"),
        "reason": payload.get("reason"),
        "message": payload.get("message") or payload.get("error_message") or payload.get("error"),
    }


def _latest_approval(store: StateStore) -> dict[str, Any] | None:
    rows = store.list_approvals(limit=1)
    return rows[0] if rows else None


def _personal_stage(stage: str, status: str, message: str, next_command: str) -> dict[str, str]:
    return {"stage": stage, "status": status, "message": message, "next": next_command}


def _overall_personal_status(stages: list[dict[str, str]]) -> str:
    if any(stage["stage"] == "minimum_live_ready" and stage["status"] == "ok" for stage in stages):
        return "ok"
    if all(stage["status"] == "ok" for stage in stages):
        return "ok"
    if any(stage["status"] == "fail" for stage in stages):
        return "blocked"
    return "warn"


def _worst_status(checks: dict[str, Any], names: list[str]) -> str:
    selected = [checks[name].status for name in names if name in checks]
    if not selected or "fail" in selected:
        return "fail"
    if "warn" in selected:
        return "warn"
    return "ok"


def _all_ok(checks: dict[str, Any], names: list[str]) -> str:
    if all(checks.get(name) and checks[name].status == "ok" for name in names):
        return "ok"
    return "fail"


def _telegram_personal_status(config: MaestroConfig) -> str:
    if not config.approval.enabled or not config.approval.require_approval:
        return "fail"
    if config.approval.provider != "telegram":
        return "fail"
    if not config.approval.telegram_allowed_chat_ids or not config.approval.whitelisted_user_ids:
        return "fail"
    if not DEFAULT_CREDENTIAL_RESOLVER.present(config.approval.telegram_bot_token_env):
        return "fail"
    return "ok"


def _dry_run_personal_status(config: MaestroConfig, checks: dict[str, Any]) -> str:
    preflight = checks.get("live_approval_preflight")
    if config.mode != RunMode.LIVE_APPROVAL or config.execution.order_posture != "dry_run":
        return "fail"
    if preflight is None or preflight.status == "fail":
        return "fail"
    if not config.strategies:
        return "fail"
    return "ok" if preflight.status == "ok" else "warn"


def _minimum_live_personal_status(config: MaestroConfig, report: HealthReport) -> str:
    if config.mode != RunMode.LIVE_APPROVAL:
        return "fail"
    if config.execution.order_posture != "armed":
        return "fail"
    if _telegram_personal_status(config) != "ok":
        return "fail"
    return "ok" if not private_beta_failures(config, report) else "fail"


def _mask_identifier(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + ("*" * max(len(text) - 4, 1)) + text[-2:]

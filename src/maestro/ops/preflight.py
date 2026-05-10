from maestro.config.models import MaestroConfig
from maestro.monitoring.health_models import HealthReport


def private_beta_failures(config: MaestroConfig, report: HealthReport) -> list[str]:
    failures = []
    checks = {check.name: check for check in report.checks}
    if report.status == "fail":
        failures.extend(f"health:{check.name}" for check in report.checks if check.status == "fail")
    if not config.execution.live_order_enabled:
        failures.append("live_order_disabled")
    if config.execution.live_order_dry_run:
        failures.append("live_order_dry_run_enabled")
    if not config.execution.require_market_session:
        failures.append("market_session_not_required")
    if not config.execution.require_broker_quote_validation:
        failures.append("broker_quote_validation_not_required")
    if not config.execution.require_broker_risk_validation:
        failures.append("broker_risk_validation_not_required")
    if config.execution.daily_loss_limit is None:
        failures.append("daily_loss_limit_missing")
    if config.execution.heartbeat_max_age_seconds <= 0:
        failures.append("heartbeat_monitoring_missing")
    if config.execution.scheduled_run_max_age_seconds <= 0:
        failures.append("scheduled_run_monitoring_missing")
    if checks.get("live_approval_preflight") is None:
        failures.append("live_approval_preflight_missing")
    elif checks["live_approval_preflight"].status != "ok":
        failures.append("live_approval_preflight_not_ok")
    if checks.get("broker_snapshot") is None or checks["broker_snapshot"].status != "ok":
        failures.append("broker_snapshot_not_fresh")
    if checks.get("reconciliation") is None or checks["reconciliation"].status != "ok":
        failures.append("reconciliation_not_passed")
    if checks.get("audit_integrity") is None or checks["audit_integrity"].status != "ok":
        failures.append("audit_integrity_not_ok")
    return sorted(set(failures))

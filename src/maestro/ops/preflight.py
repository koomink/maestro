from maestro.config.models import MaestroConfig
from maestro.monitoring.health_models import HealthReport


def private_beta_failures(config: MaestroConfig, report: HealthReport) -> list[str]:
    failures = []
    checks = {check.name: check for check in report.checks}
    if report.status == "fail":
        failures.extend(f"health:{check.name}" for check in report.checks if check.status == "fail")
    if config.execution.order_posture != "armed":
        failures.append(f"order_posture_not_armed:{config.execution.order_posture}")
    if config.kis.paper_trading:
        failures.append("kis_paper_trading_enabled")
    if _uses_mock_datahub(config):
        failures.append("datahub_mock_provider")
    if not config.execution.market_session.required:
        failures.append("market_session_not_required")
    if not config.execution.broker_validation.require_quote_validation:
        failures.append("broker_quote_validation_not_required")
    if not config.execution.broker_validation.require_risk_validation:
        failures.append("broker_risk_validation_not_required")
    if config.execution.live_order_limits.daily_loss_limit is None:
        failures.append("daily_loss_limit_missing")
    if config.monitoring.heartbeat_max_age_seconds <= 0:
        failures.append("heartbeat_monitoring_missing")
    if config.monitoring.scheduled_run_max_age_seconds <= 0:
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


def _uses_mock_datahub(config: MaestroConfig) -> bool:
    return any(
        provider.provider == "mock" and provider.enabled
        for provider in config.datahub.effective_providers()
    )

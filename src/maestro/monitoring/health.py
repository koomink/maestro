import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.enums import RunMode
from maestro.monitoring.audit_logger import _event_hash
from maestro.monitoring.health_models import (
    HealthCheck,
    HealthReport,
    overall_health_status,
)
from maestro.monitoring.health_providers import (
    FunctionHealthCheckProvider,
    HealthCheckProvider,
)
from maestro.monitoring.live_preflight import live_approval_preflight_findings
from maestro.safety.controls import SafetyControlService
from maestro.state.store import StateStore


class HealthService:
    def __init__(self, config: MaestroConfig, store: StateStore) -> None:
        self.config = config
        self.store = store

    def run(self) -> HealthReport:
        checks = [provider.run() for provider in self._providers()]
        return HealthReport(
            status=overall_health_status(checks),
            generated_at=utc_now().isoformat(),
            checks=checks,
        )

    def _providers(self) -> list[HealthCheckProvider]:
        return [
            FunctionHealthCheckProvider("config", self._config_check),
            FunctionHealthCheckProvider("state_db", self._state_db_check),
            FunctionHealthCheckProvider("audit_path", self._audit_path_check),
            FunctionHealthCheckProvider("audit_integrity", self._audit_integrity_check),
            FunctionHealthCheckProvider("safety_state", self._safety_state_check),
            FunctionHealthCheckProvider(
                "recent_halt_failure_events",
                self._recent_halt_failure_events_check,
            ),
            FunctionHealthCheckProvider("heartbeat", self._heartbeat_check),
            FunctionHealthCheckProvider("scheduled_run", self._scheduled_run_check),
            FunctionHealthCheckProvider("datahub", self._datahub_check),
            FunctionHealthCheckProvider("kis_env", self._kis_env_check),
            FunctionHealthCheckProvider("token_cache", self._token_cache_check),
            FunctionHealthCheckProvider(
                "live_approval_preflight",
                self._live_approval_preflight_check,
            ),
            FunctionHealthCheckProvider("broker_snapshot", self._broker_snapshot_check),
            FunctionHealthCheckProvider("reconciliation", self._reconciliation_check),
        ]

    def _config_check(self) -> HealthCheck:
        return HealthCheck(
            name="config",
            status="ok",
            message="loaded",
            details={"mode": self.config.mode.value},
        )

    def _state_db_check(self) -> HealthCheck:
        try:
            status = self.store.status()
        except Exception as exc:
            return HealthCheck(name="state_db", status="fail", message=type(exc).__name__)
        return HealthCheck(
            name="state_db",
            status="ok",
            message="reachable",
            details={
                "path": str(self.store.path),
                "portfolio_snapshots": status["counts"]["portfolio_snapshots"],
                "system_events": status["counts"]["system_events"],
            },
        )

    def _audit_path_check(self) -> HealthCheck:
        path = Path(self.config.audit.jsonl_path)
        parent = path.parent
        if not parent.exists():
            return HealthCheck(
                name="audit_path",
                status="warn",
                message="parent_missing",
                details={"path": _path_only(path)},
            )
        if not os.access(parent, os.W_OK):
            return HealthCheck(
                name="audit_path",
                status="fail",
                message="parent_not_writable",
                details={"path": _path_only(path)},
            )
        return HealthCheck(
            name="audit_path",
            status="ok",
            message="writable",
            details={"path": _path_only(path), "exists": path.exists()},
        )

    def _audit_integrity_check(self) -> HealthCheck:
        path = Path(self.config.audit.jsonl_path)
        if not path.exists():
            return HealthCheck(name="audit_integrity", status="ok", message="missing")
        previous_hash = None
        checked = 0
        legacy = 0
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    checked += 1
                    event = json.loads(line)
                    event_hash = event.get("event_hash")
                    if not event_hash:
                        legacy += 1
                        previous_hash = None
                        continue
                    if event.get("previous_hash") != previous_hash:
                        return HealthCheck(
                            name="audit_integrity",
                            status="fail",
                            message="broken_previous_hash",
                            details={"line": checked},
                        )
                    if _event_hash(event) != event_hash:
                        return HealthCheck(
                            name="audit_integrity",
                            status="fail",
                            message="hash_mismatch",
                            details={"line": checked},
                        )
                    previous_hash = event_hash
        except (OSError, json.JSONDecodeError) as exc:
            return HealthCheck(
                name="audit_integrity",
                status="fail",
                message=type(exc).__name__,
            )
        if legacy:
            return HealthCheck(
                name="audit_integrity",
                status="warn",
                message="legacy_unhashed_events",
                details={"checked": checked, "legacy": legacy},
            )
        return HealthCheck(
            name="audit_integrity",
            status="ok",
            message="verified",
            details={"checked": checked},
        )

    def _safety_state_check(self) -> HealthCheck:
        safety = SafetyControlService(self.store, _NoopAuditLogger()).current_state()
        status = "fail" if safety.blocks_live_execution else "ok"
        return HealthCheck(
            name="safety_state",
            status=status,
            message=safety.state.value,
            details={"source": safety.source, "updated_at": safety.updated_at},
        )

    def _recent_halt_failure_events_check(self) -> HealthCheck:
        rows = self.store.list_system_events(limit=50)
        event_types = [
            str(row.get("event_type"))
            for row in rows
            if _is_halt_or_failure_event(str(row.get("event_type")), row.get("payload", {}))
        ]
        if not event_types:
            return HealthCheck(name="recent_halt_failure_events", status="ok", message="none")
        return HealthCheck(
            name="recent_halt_failure_events",
            status="warn",
            message="present",
            details={"count": len(event_types), "latest": event_types[0]},
        )

    def _heartbeat_check(self) -> HealthCheck:
        max_age = self.config.execution.heartbeat_max_age_seconds
        if max_age <= 0:
            return HealthCheck(name="heartbeat", status="ok", message="not_configured")
        latest = self.store.load_latest_system_event("maestro_heartbeat")
        if latest is None:
            return HealthCheck(
                name="heartbeat",
                status="fail",
                message="missing",
                details={"max_age_seconds": max_age},
            )
        age_seconds = _age_seconds(latest["created_at"])
        stale = age_seconds is None or age_seconds > max_age
        return HealthCheck(
            name="heartbeat",
            status="fail" if stale else "ok",
            message="stale" if stale else "fresh",
            details={
                "created_at": latest["created_at"],
                "age_seconds": age_seconds if age_seconds is not None else "unknown",
                "max_age_seconds": max_age,
            },
        )

    def _scheduled_run_check(self) -> HealthCheck:
        max_age = self.config.execution.scheduled_run_max_age_seconds
        if max_age <= 0:
            return HealthCheck(name="scheduled_run", status="ok", message="not_configured")
        latest = self.store.load_latest_system_event("run_once_completed")
        if latest is None:
            return HealthCheck(
                name="scheduled_run",
                status="fail",
                message="missing",
                details={"max_age_seconds": max_age},
            )
        age_seconds = _age_seconds(latest["created_at"])
        stale = age_seconds is None or age_seconds > max_age
        return HealthCheck(
            name="scheduled_run",
            status="fail" if stale else "ok",
            message="stale" if stale else "fresh",
            details={
                "created_at": latest["created_at"],
                "age_seconds": age_seconds if age_seconds is not None else "unknown",
                "max_age_seconds": max_age,
            },
        )

    def _datahub_check(self) -> HealthCheck:
        provider_count = len(self.config.datahub.providers)
        return HealthCheck(
            name="datahub",
            status="ok",
            message="configured",
            details={
                "provider": self.config.datahub.provider,
                "providers": provider_count,
                "symbol_map": len(self.config.datahub.symbol_map),
            },
        )

    def _kis_env_check(self) -> HealthCheck:
        if not self.config.kis.enabled:
            return HealthCheck(name="kis_env", status="ok", message="disabled")
        required = [self.config.kis.app_key_env, self.config.kis.app_secret_env]
        account_present = bool(self.config.kis.account_id)
        account_env = self.config.kis.account_id_env
        if account_env:
            account_present = account_present or bool(os.getenv(account_env))
        missing = [name for name in required if not os.getenv(name)]
        if not account_present:
            missing.append(account_env or "account_id")
        status = "warn" if missing else "ok"
        return HealthCheck(
            name="kis_env",
            status=status,
            message="missing_required_env" if missing else "present",
            details={
                "provider": self.config.kis.provider,
                "broker_product": self.config.kis.broker_product.value,
                "missing": ",".join(missing) if missing else "none",
                "access_token_present": bool(os.getenv(self.config.kis.access_token_env)),
            },
        )

    def _token_cache_check(self) -> HealthCheck:
        path_value = self.config.kis.token_cache_path
        if not path_value:
            return HealthCheck(name="token_cache", status="ok", message="not_configured")
        path = Path(path_value)
        parent = path.parent
        if not parent.exists():
            return HealthCheck(
                name="token_cache",
                status="warn",
                message="parent_missing",
                details={"path": _path_only(path)},
            )
        if not os.access(parent, os.W_OK):
            return HealthCheck(
                name="token_cache",
                status="fail",
                message="parent_not_writable",
                details={"path": _path_only(path)},
            )
        return HealthCheck(
            name="token_cache",
            status="ok",
            message="path_ready",
            details={"path": _path_only(path), "exists": path.exists()},
        )

    def _live_approval_preflight_check(self) -> HealthCheck:
        if self.config.mode != RunMode.LIVE_APPROVAL:
            return HealthCheck(
                name="live_approval_preflight",
                status="ok",
                message="not_applicable",
            )

        failures, warnings = live_approval_preflight_findings(self.config)

        if failures:
            return HealthCheck(
                name="live_approval_preflight",
                status="fail",
                message="failed",
                details={
                    "failures": ",".join(failures),
                    "warnings": ",".join(warnings) if warnings else "none",
                },
            )
        if warnings:
            return HealthCheck(
                name="live_approval_preflight",
                status="warn",
                message="warnings",
                details={"warnings": ",".join(warnings)},
            )
        return HealthCheck(name="live_approval_preflight", status="ok", message="ready")

    def _broker_snapshot_check(self) -> HealthCheck:
        latest = self.store.load_latest_broker_account_snapshot()
        if latest is None:
            return HealthCheck(name="broker_snapshot", status="warn", message="missing")
        age_seconds = _age_seconds(latest["created_at"])
        status = (
            "warn"
            if age_seconds is None or age_seconds > self.config.reconciliation.max_age_seconds
            else "ok"
        )
        return HealthCheck(
            name="broker_snapshot",
            status=status,
            message="stale" if status == "warn" else "fresh",
            details={
                "created_at": latest["created_at"],
                "age_seconds": age_seconds if age_seconds is not None else "unknown",
            },
        )

    def _reconciliation_check(self) -> HealthCheck:
        latest = self.store.load_latest_system_event("broker_reconciliation")
        if latest is None:
            return HealthCheck(name="reconciliation", status="warn", message="missing")
        passed = latest["payload"].get("passed") is True
        age_seconds = _age_seconds(latest["created_at"])
        stale = age_seconds is None or age_seconds > self.config.reconciliation.max_age_seconds
        if not passed:
            status = "fail"
            message = "failed"
        elif stale:
            status = "warn"
            message = "stale"
        else:
            status = "ok"
            message = "passed"
        return HealthCheck(
            name="reconciliation",
            status=status,
            message=message,
            details={
                "created_at": latest["created_at"],
                "age_seconds": age_seconds if age_seconds is not None else "unknown",
            },
        )


class _NoopAuditLogger:
    def log(self, run_id: str, event_type: str, details: dict[str, Any]) -> None:
        return None


def _is_halt_or_failure_event(event_type: str, payload: dict[str, Any]) -> bool:
    if "halt" in event_type or "failure" in event_type or "failed" in event_type:
        return True
    state = payload.get("state")
    return state in {"halted", "killed"}


def _age_seconds(created_at: str) -> int | None:
    parsed = _parse_datetime(created_at)
    if parsed is None:
        return None
    return max(int((utc_now() - parsed).total_seconds()), 0)


def _parse_datetime(value: str) -> datetime | None:
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=utc_now().tzinfo)
    return parsed.astimezone(utc_now().tzinfo)


def _path_only(path: Path) -> str:
    return str(path)

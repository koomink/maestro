import os
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.safety.controls import SafetyControlService
from maestro.state.store import StateStore


class HealthCheck(BaseModel):
    name: str
    status: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class HealthReport(BaseModel):
    status: str
    generated_at: str
    checks: list[HealthCheck]

    def text_lines(self) -> list[str]:
        lines = [f"status={self.status} generated_at={self.generated_at}"]
        for check in self.checks:
            detail_text = " ".join(f"{key}={value}" for key, value in check.details.items())
            suffix = f" {detail_text}" if detail_text else ""
            lines.append(
                f"check={check.name} status={check.status} message={check.message}{suffix}"
            )
        return lines


class HealthService:
    def __init__(self, config: MaestroConfig, store: StateStore) -> None:
        self.config = config
        self.store = store

    def run(self) -> HealthReport:
        checks = [
            self._config_check(),
            self._state_db_check(),
            self._audit_path_check(),
            self._safety_state_check(),
            self._recent_halt_failure_events_check(),
            self._datahub_check(),
            self._kis_env_check(),
            self._token_cache_check(),
            self._broker_snapshot_check(),
            self._reconciliation_check(),
        ]
        return HealthReport(
            status=_overall_status(checks),
            generated_at=utc_now().isoformat(),
            checks=checks,
        )

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


def _overall_status(checks: list[HealthCheck]) -> str:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "ok"


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

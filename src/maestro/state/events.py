from enum import StrEnum
from typing import Any

from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


class SystemEventType(StrEnum):
    MAESTRO_HEARTBEAT = "maestro_heartbeat"
    RUN_ONCE_COMPLETED = "run_once_completed"
    RUN_ONCE_FAILED = "run_once_failed"
    EXECUTION_SKIPPED = "execution_skipped"
    STALE_DATA_HALT = "stale_data_halt"
    STALE_DATA_WARNING = "stale_data_warning"
    LIVE_PROPOSAL_DATA_SNAPSHOT = "live_proposal_data_snapshot"
    LIVE_ORDER_DRY_RUN = "live_order_dry_run"
    LIVE_ORDER_STATUS = "live_order_status"
    LIVE_ORDER_CANCEL = "live_order_cancel"
    LIVE_ORDER_WORKFLOW = "live_order_workflow"
    LIVE_ORDER_LIFECYCLE = "live_order_lifecycle"
    LIVE_ORDER_RESULT = "live_order_result"
    LIVE_ORDER_HALT = "live_order_halt"
    LIVE_ORDER_RECOVERY_REQUIRED = "live_order_recovery_required"
    LIVE_ORDER_RECOVERY_COMPLETED = "live_order_recovery_completed"
    LIVE_ORDER_RECOVERY_HALT = "live_order_recovery_halt"
    LIVE_ORDER_LIMIT_HALT = "live_order_limit_halt"
    MARKET_SESSION_HALT = "market_session_halt"
    BROKER_RECONCILIATION = "broker_reconciliation"
    BROKER_RECONCILIATION_HALT = "broker_reconciliation_halt"
    INSTRUMENT_VALIDATION_HALT = "instrument_validation_halt"
    BROKER_QUOTE_VALIDATION_HALT = "broker_quote_validation_halt"
    BROKER_RISK_HALT = "broker_risk_halt"
    FILL_RECONCILIATION = "fill_reconciliation"
    DYNAMIC_UNIVERSE_EVALUATION = "dynamic_universe_evaluation"


def save_audited_system_event(
    store: StateStore,
    audit: AuditLogger,
    run_id: str,
    event_type: SystemEventType | str,
    payload: dict[str, Any],
) -> None:
    event_value = str(event_type)
    store.save_system_event(run_id, event_value, payload)
    audit.log(run_id, event_value, payload)

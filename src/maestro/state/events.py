from enum import StrEnum
from typing import Any

from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


class SystemEventType(StrEnum):
    RUN_PROVENANCE = "run_provenance"
    BROKER_READONLY_REFRESH = "broker_readonly_refresh"
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
    LIVE_ORDER_TRACKING_INCOMPLETE = "live_order_tracking_incomplete"
    LIVE_ORDER_TRACKING_RESOLVED = "live_order_tracking_resolved"
    LIVE_ORDER_RECOVERY_REQUIRED = "live_order_recovery_required"
    LIVE_ORDER_RECOVERY_RESOLUTION = "live_order_recovery_resolution"
    LIVE_ORDER_RECOVERY_ATTESTATION = "live_order_recovery_attestation"
    LIVE_ORDER_RECOVERY_COMPLETED = "live_order_recovery_completed"
    LIVE_ORDER_RECOVERY_HALT = "live_order_recovery_halt"
    LIVE_ORDER_LIMIT_HALT = "live_order_limit_halt"
    MARKET_SESSION_HALT = "market_session_halt"
    BROKER_BASELINE_REQUIRED = "broker_baseline_required"
    BROKER_SNAPSHOT_ADOPTED = "broker_snapshot_adopted"
    BROKER_RECONCILIATION = "broker_reconciliation"
    BROKER_RECONCILIATION_HALT = "broker_reconciliation_halt"
    INSTRUMENT_VALIDATION_HALT = "instrument_validation_halt"
    BROKER_QUOTE_VALIDATION_HALT = "broker_quote_validation_halt"
    BROKER_RISK_HALT = "broker_risk_halt"
    FILL_RECONCILIATION = "fill_reconciliation"
    DYNAMIC_UNIVERSE_EVALUATION = "dynamic_universe_evaluation"
    STRATEGY_CASH_FLOW = "strategy_cash_flow"
    STRATEGY_CASH_FLOW_PROPOSAL = "strategy_cash_flow_proposal"
    STRATEGY_CASH_FLOW_PROPOSAL_ACK = "strategy_cash_flow_proposal_ack"
    ACCOUNT_CASH_FLOW = "account_cash_flow"
    ACCOUNT_CASH_FLOW_PROPOSAL = "account_cash_flow_proposal"
    ACCOUNT_CASH_FLOW_PROPOSAL_ACK = "account_cash_flow_proposal_ack"
    ACCOUNT_TRACKING_STARTED = "account_tracking_started"
    ACCOUNT_TRACKING_ENDED = "account_tracking_ended"
    PERFORMANCE_BASELINE_ADOPTED = "performance_baseline_adopted"
    LEDGER_OPENING_BASELINE = "ledger_opening_baseline"
    CASH_DRIFT_OBSERVED = "cash_drift_observed"
    CASH_DRIFT_CLASSIFIED = "cash_drift_classified"
    BROKER_ORDER_HISTORY_BACKFILL = "broker_order_history_backfill"
    TELEGRAM_COMMAND = "telegram_command"
    TELEGRAM_RECOVERY_NOTICE = "telegram_recovery_notice"


SYSTEM_EVENT_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    SystemEventType.RUN_PROVENANCE: (
        "run_kind",
        "deployment_source_fingerprint",
    ),
    SystemEventType.BROKER_READONLY_REFRESH: (
        "account_id",
        "source",
        "status",
        "checked_at",
    ),
    SystemEventType.MAESTRO_HEARTBEAT: ("mode", "source"),
    SystemEventType.RUN_ONCE_COMPLETED: ("orders_created", "total_value", "cash"),
    SystemEventType.RUN_ONCE_FAILED: ("error_type", "error_message"),
    SystemEventType.LIVE_ORDER_STATUS: ("broker_order_id", "symbol", "status", "checked_at"),
    SystemEventType.LIVE_ORDER_LIFECYCLE: ("run_id", "order_id", "final_status", "checked_at"),
    SystemEventType.LIVE_ORDER_RESULT: ("submitted_date", "notional", "request", "result"),
    SystemEventType.LIVE_ORDER_HALT: ("reason",),
    SystemEventType.LIVE_ORDER_TRACKING_INCOMPLETE: (
        "reason",
        "order_id",
        "broker_order",
        "last_status",
        "poll_count",
    ),
    SystemEventType.LIVE_ORDER_TRACKING_RESOLVED: (
        "order_id",
        "broker_order_id",
        "final_status",
        "checked_at",
    ),
    SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED: ("reason", "order_id"),
    SystemEventType.LIVE_ORDER_RECOVERY_COMPLETED: (
        "broker_reconciliation_event_id",
        "fill_reconciliation",
    ),
    SystemEventType.BROKER_RECONCILIATION: ("passed", "checked_at", "issues"),
    SystemEventType.BROKER_RECONCILIATION_HALT: ("reason",),
    SystemEventType.FILL_RECONCILIATION: (
        "checked_at",
        "applied_fills",
        "skipped_fills",
        "portfolio_updated",
    ),
    SystemEventType.STRATEGY_CASH_FLOW: (
        "strategy_id",
        "account_id",
        "execution_sleeve",
        "amount",
        "currency",
        "flow_type",
        "effective_at",
    ),
    SystemEventType.STRATEGY_CASH_FLOW_PROPOSAL: (
        "proposal_id",
        "account_id",
        "amount",
        "currency",
        "effective_at",
        "allocations",
    ),
    SystemEventType.STRATEGY_CASH_FLOW_PROPOSAL_ACK: ("proposal_id", "status"),
    SystemEventType.ACCOUNT_CASH_FLOW: (
        "account_id",
        "amount",
        "currency",
        "flow_type",
        "effective_at",
        "source",
    ),
    SystemEventType.ACCOUNT_CASH_FLOW_PROPOSAL: (
        "proposal_id",
        "account_id",
        "amount",
        "currency",
        "flow_type",
        "effective_at",
    ),
    SystemEventType.ACCOUNT_CASH_FLOW_PROPOSAL_ACK: ("proposal_id", "status"),
    SystemEventType.ACCOUNT_TRACKING_STARTED: ("account_id", "effective_at"),
    SystemEventType.ACCOUNT_TRACKING_ENDED: ("account_id", "effective_at"),
    SystemEventType.PERFORMANCE_BASELINE_ADOPTED: (
        "baseline_id",
        "effective_at",
        "accounts",
        "component_values",
    ),
    SystemEventType.LEDGER_OPENING_BASELINE: (
        "account_id",
        "currency",
        "amount",
        "source",
        "effective_at",
    ),
    SystemEventType.CASH_DRIFT_OBSERVED: (
        "account_id",
        "currency",
        "difference",
        "snapshot_id",
        "observed_at",
    ),
    SystemEventType.CASH_DRIFT_CLASSIFIED: (
        "account_id",
        "currency",
        "classification",
        "snapshot_id",
        "decided_at",
    ),
    SystemEventType.BROKER_ORDER_HISTORY_BACKFILL: (
        "account_id",
        "from_date",
        "to_date",
        "orders_checked",
        "applied_count",
        "checked_at",
    ),
    "fx_rate_snapshot": ("source", "as_of", "rates"),
    "fx_rate_snapshot_failed": ("provider", "pairs", "error_type", "checked_at"),
}

# What an account cash flow *is*, which is independent of how confident we are
# that it happened.  Only an external transfer is investor money entering or
# leaving; everything else is the portfolio earning or spending its own cash and
# must stay inside the return.
EXTERNAL_TRANSFER = "external_transfer"
INTERNAL_TRANSFER = "internal_transfer"
FX_CONVERSION = "fx_conversion"
INVESTMENT_INCOME = "investment_income"
COST = "cost"
ACCOUNT_CASH_FLOW_CLASSES = (
    EXTERNAL_TRANSFER,
    INVESTMENT_INCOME,
    COST,
    FX_CONVERSION,
    INTERNAL_TRANSFER,
)

# Classes whose legs only mean anything as a linked set: money leaves one side
# and arrives on the other, tied together by ``transfer_id``.  A lone leg of one
# of these is a recording error, not a flow.
LINKED_FLOW_CLASSES = (INTERNAL_TRANSFER, FX_CONVERSION)

# Cash-suspense labels an operator can assign to an unexplained ledger/broker
# difference.  ``settlement_candidate`` and ``unexplained`` describe timing or
# absence of knowledge rather than a cash flow, so they map to no flow class.
CASH_SUSPENSE_CLASSIFICATIONS: dict[str, str | None] = {
    "settlement_candidate": None,
    "unexplained": None,
    "transfer_candidate": EXTERNAL_TRANSFER,
    "dividend": INVESTMENT_INCOME,
    "interest": INVESTMENT_INCOME,
    "tax": COST,
    "fee": COST,
    "fx_conversion": FX_CONVERSION,
}


def flow_class_for_cash_suspense(classification: str) -> str | None:
    """Flow class implied by a cash-suspense classification, if any."""
    return CASH_SUSPENSE_CLASSIFICATIONS.get(str(classification).strip().lower())


def required_system_event_fields(event_type: SystemEventType | str) -> tuple[str, ...]:
    return SYSTEM_EVENT_REQUIRED_FIELDS.get(str(event_type), ())


def missing_system_event_required_fields(
    event_type: SystemEventType | str,
    payload: dict[str, Any],
) -> list[str]:
    return [
        field
        for field in required_system_event_fields(event_type)
        if field not in payload or payload[field] is None
    ]


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

from typing import Any, Literal

from pydantic import BaseModel, Field

from maestro.config.models import ReconciliationConfig
from maestro.core.clock import utc_now
from maestro.core.ids import new_run_id
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore

ReconciliationIssueType = Literal[
    "cash_mismatch",
    "position_quantity_mismatch",
    "unknown_broker_position",
    "missing_broker_position",
    "no_broker_snapshot",
]


class ReconciliationIssue(BaseModel):
    issue_type: ReconciliationIssueType
    symbol: str | None = None
    maestro_value: float | None = None
    broker_value: float | None = None
    difference: float | None = None
    tolerance: float
    message: str


class ReconciliationResult(BaseModel):
    run_id: str
    passed: bool
    checked_at: str
    cash_difference: float | None = None
    position_differences: dict[str, float] = Field(default_factory=dict)
    issues: list[ReconciliationIssue] = Field(default_factory=list)
    broker_snapshot_id: int | None = None
    broker_account_id: str | None = None
    tolerances: dict[str, float]


class BrokerReconciliationService:
    def __init__(
        self,
        config: ReconciliationConfig,
        state_store: StateStore,
        audit_logger: AuditLogger,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger

    def reconcile_latest(self) -> ReconciliationResult:
        run_id = new_run_id()
        portfolio_state = self.state_store.load_latest_portfolio_state()
        latest_snapshot = self.state_store.load_latest_broker_account_snapshot()
        if latest_snapshot is None:
            result = self._no_snapshot_result(run_id)
            self._persist(result)
            return result

        account = latest_snapshot["payload"]["account"]
        result = self._compare(
            run_id=run_id,
            portfolio_state=portfolio_state,
            broker_account=account,
            broker_snapshot_id=latest_snapshot["id"],
        )
        self._persist(result)
        return result

    def _compare(
        self,
        *,
        run_id: str,
        portfolio_state: PortfolioState,
        broker_account: dict[str, Any],
        broker_snapshot_id: int,
    ) -> ReconciliationResult:
        issues: list[ReconciliationIssue] = []
        broker_cash = float(broker_account.get("cash", 0.0))
        cash_difference = broker_cash - portfolio_state.cash
        if abs(cash_difference) > self.config.cash_tolerance:
            issues.append(
                ReconciliationIssue(
                    issue_type="cash_mismatch",
                    maestro_value=portfolio_state.cash,
                    broker_value=broker_cash,
                    difference=cash_difference,
                    tolerance=self.config.cash_tolerance,
                    message="Broker cash differs from Maestro cash.",
                )
            )

        broker_positions = _broker_positions_by_symbol(broker_account)
        position_differences: dict[str, float] = {}
        maestro_symbols = set(portfolio_state.positions)
        broker_symbols = set(broker_positions)

        for symbol in sorted(maestro_symbols & broker_symbols):
            maestro_quantity = portfolio_state.positions[symbol]
            broker_quantity = broker_positions[symbol]
            difference = broker_quantity - maestro_quantity
            position_differences[symbol] = difference
            if abs(difference) > self.config.position_quantity_tolerance:
                issues.append(
                    ReconciliationIssue(
                        issue_type="position_quantity_mismatch",
                        symbol=symbol,
                        maestro_value=maestro_quantity,
                        broker_value=broker_quantity,
                        difference=difference,
                        tolerance=self.config.position_quantity_tolerance,
                        message="Broker position quantity differs from Maestro quantity.",
                    )
                )

        for symbol in sorted(broker_symbols - maestro_symbols):
            broker_quantity = broker_positions[symbol]
            if abs(broker_quantity) > self.config.position_quantity_tolerance:
                position_differences[symbol] = broker_quantity
                issues.append(
                    ReconciliationIssue(
                        issue_type="unknown_broker_position",
                        symbol=symbol,
                        maestro_value=0.0,
                        broker_value=broker_quantity,
                        difference=broker_quantity,
                        tolerance=self.config.position_quantity_tolerance,
                        message="Broker has a position Maestro does not track.",
                    )
                )

        for symbol in sorted(maestro_symbols - broker_symbols):
            maestro_quantity = portfolio_state.positions[symbol]
            if abs(maestro_quantity) > self.config.position_quantity_tolerance:
                position_differences[symbol] = -maestro_quantity
                issues.append(
                    ReconciliationIssue(
                        issue_type="missing_broker_position",
                        symbol=symbol,
                        maestro_value=maestro_quantity,
                        broker_value=0.0,
                        difference=-maestro_quantity,
                        tolerance=self.config.position_quantity_tolerance,
                        message="Maestro tracks a position missing from broker snapshot.",
                    )
                )

        return ReconciliationResult(
            run_id=run_id,
            passed=not issues,
            checked_at=utc_now().isoformat(),
            cash_difference=cash_difference,
            position_differences=position_differences,
            issues=issues,
            broker_snapshot_id=broker_snapshot_id,
            broker_account_id=str(broker_account.get("account_id") or ""),
            tolerances=self._tolerances(),
        )

    def _no_snapshot_result(self, run_id: str) -> ReconciliationResult:
        issue = ReconciliationIssue(
            issue_type="no_broker_snapshot",
            tolerance=0.0,
            message="No broker account snapshot is available for reconciliation.",
        )
        return ReconciliationResult(
            run_id=run_id,
            passed=False,
            checked_at=utc_now().isoformat(),
            issues=[issue],
            tolerances=self._tolerances(),
        )

    def _persist(self, result: ReconciliationResult) -> None:
        payload = result.model_dump(mode="json")
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            result.run_id,
            SystemEventType.BROKER_RECONCILIATION,
            payload,
        )

    def _tolerances(self) -> dict[str, float]:
        return {
            "cash_tolerance": self.config.cash_tolerance,
            "position_quantity_tolerance": self.config.position_quantity_tolerance,
            "value_tolerance": self.config.value_tolerance,
        }


def _broker_positions_by_symbol(broker_account: dict[str, Any]) -> dict[str, float]:
    positions: dict[str, float] = {}
    for position in broker_account.get("positions", []):
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or "")
        if not symbol:
            continue
        positions[symbol] = positions.get(symbol, 0.0) + float(position.get("quantity", 0.0))
    return positions

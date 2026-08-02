from collections.abc import Callable
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
    "no_portfolio_snapshot",
    "buying_power_drift",
    "order_history_unverified",
    "order_history_mismatch",
]


class ReconciliationIssue(BaseModel):
    issue_type: ReconciliationIssueType
    account_id: str | None = None
    broker_snapshot_id: int | None = None
    symbol: str | None = None
    maestro_value: float | None = None
    broker_value: float | None = None
    difference: float | None = None
    tolerance: float
    drift_level: str | None = None
    drift_nav_ratio: float | None = None
    drift_recent_fill_ratio: float | None = None
    drift_settlement_elapsed_days: float | None = None
    drift_persistence_count: int | None = None
    drift_stable: bool | None = None
    message: str


class ReconciliationResult(BaseModel):
    run_id: str
    passed: bool
    checked_at: str
    cash_difference: float | None = None
    position_differences: dict[str, float] = Field(default_factory=dict)
    issues: list[ReconciliationIssue] = Field(default_factory=list)
    observations: list[ReconciliationIssue] = Field(default_factory=list)
    broker_snapshot_id: int | None = None
    broker_account_id: str | None = None
    account_results: list[dict[str, Any]] = Field(default_factory=list)
    tolerances: dict[str, float]


class BrokerReconciliationService:
    def __init__(
        self,
        config: ReconciliationConfig,
        state_store: StateStore,
        audit_logger: AuditLogger,
        snapshot_refresher: Callable[[], None] | None = None,
        signal_run_id: str | None = None,
        account_ids: list[str] | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.snapshot_refresher = snapshot_refresher
        self.signal_run_id = signal_run_id
        self.account_ids = list(account_ids or [])

    def reconcile_latest(
        self,
        run_id: str | None = None,
        signal_run_id: str | None = None,
    ) -> ReconciliationResult:
        run_id = run_id or new_run_id()
        effective_signal_run_id = signal_run_id or self.signal_run_id
        if self.snapshot_refresher is not None:
            self.snapshot_refresher()
        if self.account_ids:
            result = self._reconcile_accounts(run_id)
            self._persist(result, signal_run_id=effective_signal_run_id)
            return result
        portfolio_state = self.state_store.load_latest_portfolio_state()
        latest_snapshot = self.state_store.load_latest_broker_account_snapshot()
        if latest_snapshot is None:
            result = self._no_snapshot_result(run_id)
            self._persist(result, signal_run_id=effective_signal_run_id)
            return result

        account = _account_with_observation_context(latest_snapshot)
        result = self._compare(
            run_id=run_id,
            portfolio_state=portfolio_state,
            broker_account=account,
            broker_snapshot_id=latest_snapshot["id"],
            logical_account_id=str(
                (latest_snapshot.get("payload") or {}).get("account_id")
                or latest_snapshot.get("account_id")
                or ""
            ),
        )
        self._persist(result, signal_run_id=effective_signal_run_id)
        return result

    def _reconcile_accounts(self, run_id: str) -> ReconciliationResult:
        account_results: list[dict[str, Any]] = []
        issues: list[ReconciliationIssue] = []
        observations: list[ReconciliationIssue] = []
        cash_difference = 0.0
        position_differences: dict[str, float] = {}
        broker_snapshot_ids: list[int] = []

        latest_broker_snapshots = _latest_broker_snapshots_by_account(
            self.state_store.list_broker_account_snapshots(
                limit=max(100, len(self.account_ids) * 10)
            )
        )
        for account_id in self.account_ids:
            portfolio_state = self.state_store.load_latest_account_portfolio_state(account_id)
            latest_snapshot = latest_broker_snapshots.get(account_id)
            if portfolio_state is None:
                issue = ReconciliationIssue(
                    issue_type="no_portfolio_snapshot",
                    tolerance=0.0,
                    message=(
                        "No Maestro portfolio snapshot is available for "
                        f"account_id={account_id}."
                    ),
                )
                issues.append(issue)
                account_results.append(
                    {
                        "account_id": account_id,
                        "passed": False,
                        "issues": [issue.model_dump(mode="json")],
                    }
                )
                continue
            if latest_snapshot is None:
                issue = ReconciliationIssue(
                    issue_type="no_broker_snapshot",
                    tolerance=0.0,
                    message=f"No broker account snapshot is available for account_id={account_id}.",
                )
                issues.append(issue)
                account_results.append(
                    {
                        "account_id": account_id,
                        "passed": False,
                        "issues": [issue.model_dump(mode="json")],
                    }
                )
                continue

            account = _account_with_observation_context(latest_snapshot)
            account_result = self._compare(
                run_id=run_id,
                portfolio_state=portfolio_state,
                broker_account=account,
                broker_snapshot_id=latest_snapshot["id"],
                logical_account_id=account_id,
            )
            account_issues = [
                _issue_for_account(issue, account_id) for issue in account_result.issues
            ]
            account_observations = [
                _issue_for_account(issue, account_id)
                for issue in account_result.observations
            ]
            issues.extend(account_issues)
            observations.extend(account_observations)
            cash_difference += account_result.cash_difference or 0.0
            for symbol, difference in account_result.position_differences.items():
                position_differences[symbol] = position_differences.get(symbol, 0.0) + difference
            broker_snapshot_ids.append(latest_snapshot["id"])
            account_results.append(
                {
                    "account_id": account_id,
                    "passed": account_result.passed,
                    "cash_difference": account_result.cash_difference,
                    "position_differences": account_result.position_differences,
                    "issues": [issue.model_dump(mode="json") for issue in account_issues],
                    "observations": [
                        issue.model_dump(mode="json") for issue in account_observations
                    ],
                    "broker_snapshot_id": latest_snapshot["id"],
                    "broker_account_id": account_result.broker_account_id,
                }
            )

        return ReconciliationResult(
            run_id=run_id,
            passed=not issues and not any(
                observation.drift_level == "L3" for observation in observations
            ),
            checked_at=utc_now().isoformat(),
            cash_difference=cash_difference,
            position_differences=position_differences,
            issues=issues,
            observations=observations,
            broker_snapshot_id=max(broker_snapshot_ids) if broker_snapshot_ids else None,
            broker_account_id="aggregate:" + ",".join(self.account_ids),
            account_results=account_results,
            tolerances=self._tolerances(),
        )

    def _compare(
        self,
        *,
        run_id: str,
        portfolio_state: PortfolioState,
        broker_account: dict[str, Any],
        broker_snapshot_id: int,
        logical_account_id: str | None = None,
    ) -> ReconciliationResult:
        issues: list[ReconciliationIssue] = []
        observations: list[ReconciliationIssue] = []
        if broker_account.get("_order_history_backfill_run_id"):
            history = next(
                (
                    row
                    for row in self.state_store.list_system_events_by_type(
                        SystemEventType.BROKER_ORDER_HISTORY_BACKFILL,
                        limit=2000,
                    )
                    if str(row.get("run_id") or "")
                    == str(broker_account.get("_order_history_backfill_run_id") or "")
                    and str((row.get("payload") or {}).get("account_id") or "")
                    == str(logical_account_id or broker_account.get("account_id") or "")
                ),
                None,
            )
            if history is None or int(
                (history.get("payload") or {}).get("missing_ledger_count") or 0
            ) > 0:
                issues.append(
                    ReconciliationIssue(
                        issue_type="order_history_unverified",
                        tolerance=0.0,
                        message=(
                            "Toss OPEN/CLOSED order history was not successfully "
                            "backfilled for this reconciliation run."
                        ),
                    )
                )
            elif missing_order_ids := self._missing_toss_order_ids(
                logical_account_id=str(
                    logical_account_id or broker_account.get("account_id") or ""
                ),
                history_payload=history.get("payload") or {},
            ):
                issues.append(
                    ReconciliationIssue(
                        issue_type="order_history_mismatch",
                        tolerance=0.0,
                        message=(
                            "Maestro-submitted broker order(s) are absent from Toss "
                            "OPEN/CLOSED history: " + ", ".join(missing_order_ids)
                        ),
                    )
                )
        cash_difference = self._compare_cash(
            portfolio_state,
            broker_account,
            issues,
            observations,
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

        if any(issue.issue_type in {
            "position_quantity_mismatch",
            "unknown_broker_position",
            "missing_broker_position",
        } for issue in issues):
            for observation in observations:
                observation.drift_level = "L3"
                observation.message = (
                    observation.message + " Position/fill mismatch makes this L3."
                )

        account_id = str(broker_account.get("account_id") or "") or None
        for observation in observations:
            observation.account_id = account_id
            observation.broker_snapshot_id = broker_snapshot_id

        return ReconciliationResult(
            run_id=run_id,
            passed=not issues and not any(
                observation.drift_level == "L3" for observation in observations
            ),
            checked_at=utc_now().isoformat(),
            cash_difference=cash_difference,
            position_differences=position_differences,
            issues=issues,
            observations=observations,
            broker_snapshot_id=broker_snapshot_id,
            broker_account_id=str(broker_account.get("account_id") or ""),
            tolerances=self._tolerances(),
        )

    def _missing_toss_order_ids(
        self,
        *,
        logical_account_id: str,
        history_payload: dict[str, Any],
    ) -> list[str]:
        present = {str(value) for value in history_payload.get("broker_order_ids") or []}
        from_date = str(history_payload.get("from_date") or "")
        to_date = str(history_payload.get("to_date") or "")
        expected: set[str] = set()
        for row in self.state_store.list_system_events_by_type(
            SystemEventType.LIVE_ORDER_RESULT,
            limit=5000,
        ):
            payload = row.get("payload") or {}
            request = payload.get("request") or {}
            if str(request.get("account_id") or "") != logical_account_id:
                continue
            submitted_date = str(payload.get("submitted_date") or "")
            if from_date and submitted_date < from_date:
                continue
            if to_date and submitted_date > to_date:
                continue
            broker_order = (payload.get("result") or {}).get("broker_order") or {}
            broker_order_id = str(broker_order.get("broker_order_id") or "")
            if broker_order_id:
                expected.add(broker_order_id)
        return sorted(expected - present)

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

    def _persist(self, result: ReconciliationResult, *, signal_run_id: str | None = None) -> None:
        payload = result.model_dump(mode="json")
        if signal_run_id:
            payload["signal_run_id"] = signal_run_id
        save_audited_system_event(
            self.state_store,
            self.audit_logger,
            result.run_id,
            SystemEventType.BROKER_RECONCILIATION,
            payload,
        )
        # Buying-power drift is deliberately an observation, not a
        # reconciliation failure. Persist each observation separately so the
        # ledger/audit timeline can classify it later without turning it into
        # an accounting cash flow or a return.
        for observation in result.observations:
            if observation.issue_type != "buying_power_drift":
                continue
            currency = str(observation.symbol or "").removeprefix("CASH_") or "KRW"
            snapshot_id = observation.broker_snapshot_id or result.broker_snapshot_id
            observation_payload = {
                "account_id": observation.account_id or result.broker_account_id,
                "currency": currency,
                "difference": observation.difference,
                "snapshot_id": snapshot_id,
                "observed_at": result.checked_at,
                "classification": "unclassified",
                "duplicate_key": (
                    f"cash-drift:{observation.account_id or result.broker_account_id}:"
                    f"{snapshot_id}:{currency}"
                ),
            }
            self.state_store.upsert_cash_suspense(
                account_id=str(observation_payload["account_id"]),
                currency=currency,
                amount=float(observation.difference or 0.0),
                snapshot_id=snapshot_id,
                observed_at=result.checked_at,
            )
            if self.state_store.duplicate_key_exists(observation_payload["duplicate_key"]):
                continue
            save_audited_system_event(
                self.state_store,
                self.audit_logger,
                result.run_id,
                SystemEventType.CASH_DRIFT_OBSERVED,
                observation_payload,
            )

    def _tolerances(self) -> dict[str, float]:
        return {
            "cash_tolerance": self.config.cash_tolerance,
            "position_quantity_tolerance": self.config.position_quantity_tolerance,
            "value_tolerance": self.config.value_tolerance,
        }

    def _compare_cash(
        self,
        portfolio_state: PortfolioState,
        broker_account: dict[str, Any],
        issues: list[ReconciliationIssue],
        observations: list[ReconciliationIssue],
    ) -> float:
        explicit_ledger_cash = broker_account.get("ledger_cash_by_currency")
        # A null explicit ledger means this broker has no authoritative cash
        # balance. Its cash fields are an observational buying-power proxy.
        source = str(broker_account.get("source") or "")
        ledgerless_contract = (
            explicit_ledger_cash is None
            and "ledger_cash_by_currency" in broker_account
            and source.startswith("toss_")
        )
        if ledgerless_contract:
            self._observe_buying_power_drift(
                portfolio_state,
                broker_account,
                observations,
            )
            return 0.0
        broker_cash_by_currency = (
            explicit_ledger_cash
            if explicit_ledger_cash is not None
            else broker_account.get("cash_by_currency") or {}
        )
        if portfolio_state.cash_by_currency:
            total_difference = 0.0
            currencies = set(portfolio_state.cash_by_currency) | set(broker_cash_by_currency)
            for currency in sorted(currencies):
                maestro_cash = float(portfolio_state.cash_by_currency.get(currency, 0.0))
                broker_cash = float(broker_cash_by_currency.get(currency, 0.0))
                difference = broker_cash - maestro_cash
                total_difference += difference
                tolerance = self.config.ledger_cash_tolerance_by_currency.get(
                    currency,
                    self.config.cash_tolerance,
                )
                if abs(difference) > tolerance:
                    issues.append(
                        ReconciliationIssue(
                            issue_type="cash_mismatch",
                            symbol=f"CASH_{currency}",
                            maestro_value=maestro_cash,
                            broker_value=broker_cash,
                            difference=difference,
                            tolerance=tolerance,
                            message="Broker cash differs from Maestro cash.",
                        )
                    )
            return total_difference

        broker_cash = float(broker_account.get("cash", 0.0))
        difference = broker_cash - portfolio_state.cash
        if abs(difference) > self.config.cash_tolerance:
            issues.append(
                ReconciliationIssue(
                    issue_type="cash_mismatch",
                    maestro_value=portfolio_state.cash,
                    broker_value=broker_cash,
                    difference=difference,
                    tolerance=self.config.cash_tolerance,
                    message="Broker cash differs from Maestro cash.",
                )
            )
        return difference

    def _observe_buying_power_drift(
        self,
        portfolio_state: PortfolioState,
        broker_account: dict[str, Any],
        observations: list[ReconciliationIssue],
    ) -> None:
        ledger = portfolio_state.cash_by_currency or {"KRW": portfolio_state.cash}
        buying_power = broker_account.get("buying_power_by_currency") or {}
        for currency in sorted(set(ledger) | set(buying_power)):
            ledger_value = float(ledger.get(currency, 0.0))
            broker_value = float(buying_power.get(currency, 0.0))
            difference = broker_value - ledger_value
            drift_config = self.config.buying_power_drift
            budget_by_currency = (
                self.config.buying_power_drift_budget_by_currency
                or drift_config.budget_by_currency
            )
            budget = budget_by_currency.get(currency, 0.0)
            if abs(difference) <= budget:
                continue
            minor_unit = 1.0 if currency.upper() == "KRW" else 0.01
            nav_ratio = abs(difference) / max(abs(ledger_value), minor_unit)
            recent_fill_notional = _recent_fill_notional(broker_account)
            recent_fill_ratio = (
                abs(difference) / max(recent_fill_notional, minor_unit)
                if recent_fill_notional > 0
                else None
            )
            settlement_elapsed_days = _settlement_elapsed_days(broker_account)
            persistence_count, drift_stable = _drift_stability(
                self.state_store,
                account_id=str(broker_account.get("account_id") or ""),
                currency=currency,
                current_difference=difference,
                budget=max(budget, minor_unit),
            )
            drift_config = self.config.buying_power_drift
            recent_ratio_budget = (
                self.config.buying_power_drift_recent_fill_ratio_budget
                if self.config.buying_power_drift_recent_fill_ratio_budget != 0.01
                else drift_config.recent_fill_ratio_budget
            )
            drift_level = _buying_power_drift_level(
                abs_difference=abs(difference),
                budget=max(budget, minor_unit),
                nav_ratio=nav_ratio,
                nav_ratio_budget=(
                    self.config.buying_power_drift_nav_ratio_budget
                    if self.config.buying_power_drift_nav_ratio_budget != 0.0005
                    else drift_config.nav_ratio_budget
                ),
                recent_fill_ratio=recent_fill_ratio,
                recent_fill_ratio_budget=recent_ratio_budget,
                settlement_elapsed_days=settlement_elapsed_days,
                settlement_grace_days=drift_config.settlement_grace_days,
                persistence_count=persistence_count,
                drift_stable=drift_stable,
            )
            observations.append(
                ReconciliationIssue(
                    issue_type="buying_power_drift",
                    symbol=f"CASH_{currency}",
                    maestro_value=ledger_value,
                    broker_value=broker_value,
                    difference=difference,
                    tolerance=budget,
                    drift_level=drift_level,
                    drift_nav_ratio=nav_ratio,
                    drift_recent_fill_ratio=recent_fill_ratio,
                    drift_settlement_elapsed_days=settlement_elapsed_days,
                    drift_persistence_count=persistence_count,
                    drift_stable=drift_stable,
                    message=(
                        "Broker buying power differs from the Maestro cash ledger; "
                        f"this is observational drift {drift_level} and does not "
                        "change accounting cash."
                    ),
                )
            )


def _buying_power_drift_level(
    *,
    abs_difference: float,
    budget: float,
    nav_ratio: float,
    nav_ratio_budget: float,
    recent_fill_ratio: float | None,
    recent_fill_ratio_budget: float,
    settlement_elapsed_days: float | None,
    settlement_grace_days: int,
    persistence_count: int,
    drift_stable: bool,
) -> str:
    if abs_difference <= budget:
        return "L0"
    if nav_ratio <= nav_ratio_budget:
        return "L1" if persistence_count < 2 or drift_stable else "L2"
    if (
        recent_fill_ratio is not None
        and recent_fill_ratio <= recent_fill_ratio_budget
        and (
            settlement_elapsed_days is None
            or settlement_elapsed_days <= float(settlement_grace_days)
        )
    ):
        return "L1" if persistence_count < 2 or drift_stable else "L2"
    if nav_ratio <= max(nav_ratio_budget * 20.0, 0.01):
        return "L2"
    return "L3"


def _recent_fill_notional(broker_account: dict[str, Any]) -> float:
    total = 0.0
    for fill in broker_account.get("_order_fills") or []:
        if not isinstance(fill, dict):
            continue
        quantity = float(fill.get("filled_quantity") or fill.get("quantity") or 0.0)
        price = float(
            fill.get("average_fill_price")
            or fill.get("limit_price")
            or fill.get("price")
            or 0.0
        )
        total += abs(quantity * price)
    return total


def _settlement_elapsed_days(broker_account: dict[str, Any]) -> float | None:
    submitted = [
        str(fill.get("submitted_at") or "")
        for fill in broker_account.get("_order_fills") or []
        if isinstance(fill, dict) and fill.get("submitted_at")
    ]
    observed_at = str(broker_account.get("_snapshot_created_at") or "")
    if not submitted or not observed_at:
        return None
    try:
        from datetime import datetime

        latest_fill = datetime.fromisoformat(max(submitted).replace("Z", "+00:00"))
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        if latest_fill.tzinfo is None:
            latest_fill = latest_fill.replace(tzinfo=observed.tzinfo)
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=latest_fill.tzinfo)
        return max((observed - latest_fill).total_seconds() / 86400.0, 0.0)
    except ValueError:
        return None


def _drift_stability(
    store: StateStore,
    *,
    account_id: str,
    currency: str,
    current_difference: float,
    budget: float,
) -> tuple[int, bool]:
    differences: list[float] = []
    for row in store.list_system_events_by_type(SystemEventType.CASH_DRIFT_OBSERVED, limit=20):
        payload = row.get("payload") or {}
        if (
            str(payload.get("account_id") or "") == account_id
            and str(payload.get("currency") or "").upper() == currency.upper()
        ):
            value = payload.get("difference")
            if value is not None:
                differences.append(float(value))
    recent = differences[:3]
    values = [current_difference, *recent]
    same_sign = all(value == 0 or value * current_difference > 0 for value in recent)
    stable_magnitude = max(values) - min(values) <= max(budget, abs(current_difference) * 0.2)
    return len(differences), same_sign and stable_magnitude


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


def _account_with_observation_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload = snapshot.get("payload") or {}
    account = dict(payload.get("account") or {})
    account["_order_fills"] = payload.get("order_fills") or []
    account["_unfilled_orders"] = payload.get("unfilled_orders") or []
    account["_snapshot_created_at"] = snapshot.get("created_at")
    account["_order_history_backfill_run_id"] = payload.get(
        "order_history_backfill_run_id"
    )
    return account


def _latest_broker_snapshots_by_account(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for row in rows:
        payload = row["payload"]
        account_id = str(payload.get("account_id") or row.get("account_id") or "")
        if not account_id or account_id in snapshots:
            continue
        snapshots[account_id] = row
    return snapshots


def _issue_for_account(issue: ReconciliationIssue, account_id: str) -> ReconciliationIssue:
    payload = issue.model_dump()
    payload["account_id"] = account_id
    payload["message"] = f"account_id={account_id}: {issue.message}"
    return ReconciliationIssue.model_validate(payload)

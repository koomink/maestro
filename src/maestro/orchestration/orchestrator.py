import json
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import sleep
from typing import Any

from pydantic import BaseModel

from maestro.approval.manager import ApprovalManager
from maestro.approval.models import (
    ApprovalDecision,
    ApprovalDispatchResult,
    PendingApprovalEnvelope,
)
from maestro.config.execution import ExecutionConfig
from maestro.config.identity import ConfigIdentity
from maestro.config.models import MaestroConfig
from maestro.config.multi_account_contributions import is_multi_account_contribution_account_id
from maestro.core.clock import utc_now
from maestro.core.enums import OrderStatus, OrderType, RunMode
from maestro.core.ids import new_run_id, new_signal_run_id
from maestro.core.provenance import current_deployment_identity
from maestro.core.symbols import is_cash_symbol
from maestro.datahub.base import BaseDataProvider, build_data_provider
from maestro.execution.base import OrderIntent
from maestro.execution.broker_capacity_lookup import (
    check_capacity_currency,
    get_order_buying_power,
    resolve_order_currency,
)
from maestro.execution.broker_router import (
    PAPER_DEFAULT_ACCOUNT_ID,
    BrokerAccountRouter,
    UnsupportedBrokerOperation,
)
from maestro.execution.broker_state import portfolio_state_from_broker_account
from maestro.execution.brokers.readonly import (
    BrokerBuyingPower,
    BuyingPowerCurrencyUnavailable,
)
from maestro.execution.brokers.readonly_factory import build_broker_readonly_service
from maestro.execution.budget_requests import (
    ContributionBudgetRequest,
    build_contribution_budget_request,
)
from maestro.execution.execution_sleeves import (
    AllocatedExecutionScope,
    ExecutionScopeDraft,
    allocate_cash_rebalanced_scope_states,
)
from maestro.execution.factory import build_execution_engine
from maestro.execution.funding_requests import (
    ContributionFundingRequest,
    build_contribution_funding_request,
    contribution_available_cash,
)
from maestro.execution.live_order_batch import (
    BatchOrderDependencies,
    LiveOrderBatchLifecycleService,
)
from maestro.execution.live_order_factory import (
    LiveApprovalDependencies,
    build_live_approval_dependencies,
)
from maestro.execution.live_order_models import (
    AppliedFill,
    FillReconciliationResult,
    LiveOrderStatusSnapshot,
)
from maestro.execution.live_order_safety import build_live_order_idempotency_key
from maestro.execution.live_orders import (
    BrokerOrderId,
    BrokerReconciliationRunner,
    LiveOrderCancelRequest,
    LiveOrderClient,
    LiveOrderLifecycleResult,
    LiveOrderNotificationClient,
    LiveOrderRequest,
    LiveOrderStatusClient,
)
from maestro.execution.order_builder import round_price_to_tick
from maestro.execution.order_capacity import OrderCapacityBlock, OrderCapacityService
from maestro.execution.reconciliation import BrokerReconciliationService
from maestro.execution.rotation_cohort import (
    RotationCohort,
    SellPhaseOutcome,
    evaluate_sell_phase,
    rescale_buys_to_cash,
    split_rotation_cohorts,
)
from maestro.fx.service import ConfiguredFXRefreshService
from maestro.integrations.telegram.bot import TelegramBotAPIClient
from maestro.integrations.telegram.ui.cards import render_approval_stage_card
from maestro.integrations.telegram.ui.lifecycle import CardLifecycleManager
from maestro.monitoring.audit_logger import AuditLogger
from maestro.ops.readonly_refresh import (
    latest_snapshot_for_account,
    refresh_readonly_accounts,
    required_account_ids_for_strategies,
)
from maestro.orchestration.data_quality import (
    data_quality_issues as collect_data_quality_issues,
)
from maestro.orchestration.data_quality import (
    prices_from_bundle,
)
from maestro.orchestration.dispatch_group import dispatch_group_id
from maestro.orchestration.live_gates import LiveExecutionGateService
from maestro.plugins.registry import PluginRegistry
from maestro.portfolio.account_attribution import (
    AccountAttributionReconciliationService,
)
from maestro.portfolio.manager import PortfolioManager, PortfolioTarget
from maestro.portfolio.strategy_books import build_strategy_book_snapshots
from maestro.risk.manager import RiskDecision, RiskManager
from maestro.safety.controls import SafetyControlService
from maestro.sdk import (
    CandidateInstrumentRequest,
    StrategyContext,
    StrategyRuntime,
    StrategySignalResult,
    TargetAllocationResult,
)
from maestro.signals.converter import normalize_strategy_result
from maestro.signals.validator import SignalValidator
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.funding_workflow import (
    child_key,
    load_migration_cutoff,
    load_workflow_child,
    plan_contribution_request,
    request_terminal_state,
)
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore
from maestro.universe.dynamic import DynamicUniverseService, InstrumentResolver


class RunOnceSummary(BaseModel):
    run_id: str
    loaded_strategies: list[str]
    orders_created: int
    total_value: float
    cash: float


class SignalRunSummary(BaseModel):
    signal_run_id: str
    loaded_strategies: list[str]
    action_required: bool
    orders_preview_count: int
    contribution_override: bool = False
    no_order_reasons: list[str] = []


class SignalApprovalSummary(BaseModel):
    signal_run_id: str
    run_id: str
    orders_created: int
    approval_status: str
    orders_planned: int = 0
    orders_capacity_blocked: int = 0
    approvals_pending: int = 0
    orders_submitted: int = 0
    orders_accepted: int = 0
    orders_filled: int = 0
    orders_failed: int = 0


@dataclass(frozen=True)
class ScopedOrderTarget:
    account_id: str | None
    execution_sleeve: str | None
    target: PortfolioTarget
    execution_config: ExecutionConfig
    state: PortfolioState
    contribution_group_id: str | None = None
    allocated_cash: float = 0.0
    current_value: float = 0.0
    current_weight: float = 0.0
    target_weight: float = 1.0
    drift: float = 0.0
    requires_budget_request: bool = False


@dataclass(frozen=True)
class _CancelResolution:
    resolved_statuses: dict[str, OrderStatus]
    canceled: list[dict[str, Any]]
    cancel_failures: list[dict[str, Any]]
    cancel_unconfirmed: list[dict[str, Any]]
    reconciliation_failed: bool
    snapshots: dict[str, list[LiveOrderStatusSnapshot]] = field(default_factory=dict)
    fill_result: FillReconciliationResult | None = None

    @property
    def halt_reason(self) -> str | None:
        """Why it is no longer safe to submit more orders under this approval.

        The two causes need different responses from the operator, so they must
        not be reported as the same thing: a stale ledger means later sizing
        cannot be trusted, while an unconfirmed cancel means an order is still
        working and unknown quantity may yet fill while we send more.
        """
        if self.reconciliation_failed:
            return "ledger_disagrees_with_broker"
        if self.cancel_failures or self.cancel_unconfirmed:
            return "unresolved_broker_order"
        return None

    @property
    def blocks_further_cohorts(self) -> bool:
        return self.halt_reason is not None


_CANCEL_TERMINAL_STATUSES = {
    OrderStatus.CANCELED,
    OrderStatus.FILLED,
    OrderStatus.REJECTED,
    OrderStatus.FAILED,
}


class MaestroOrchestrator:
    def __init__(
        self,
        config: MaestroConfig,
        *,
        live_order_client: LiveOrderClient | None = None,
        live_order_status_client: LiveOrderStatusClient | None = None,
        live_order_notification_client: LiveOrderNotificationClient | None = None,
        broker_reconciliation_service: BrokerReconciliationRunner | None = None,
        telegram_client=None,
        config_identity: ConfigIdentity | None = None,
        order_capacity_lookup: Callable[[OrderIntent], BrokerBuyingPower] | None = None,
    ) -> None:
        self.config = config
        signal_strategies = [strategy for strategy in config.strategies if strategy.signal_enabled]
        self.registry = PluginRegistry.from_configs(signal_strategies, run_mode=config.mode)
        self.datahub: BaseDataProvider = build_data_provider(config.datahub)
        self.portfolio_manager = PortfolioManager(signal_strategies)
        self.risk_manager = RiskManager(
            config.portfolio.allowed_symbols,
            max_position_weight=config.risk.max_position_weight,
        )
        self.execution = build_execution_engine(
            config.execution,
            instruments=config.universe.instruments,
            currency_sleeves=config.portfolio.currency_sleeves,
        )
        self.approval_manager = ApprovalManager(
            config.approval,
            run_mode=config.mode,
            instruments=config.universe.instruments,
            telegram_client=telegram_client,
            profile_name=_profile_name(config_identity),
        )
        self.state_store = StateStore(
            config.state.sqlite_path,
            config.portfolio.initial_cash,
            config.portfolio.cash_by_currency,
            config_identity=config_identity,
        )
        self.audit = AuditLogger(config.audit.jsonl_path)
        self.safety = SafetyControlService(self.state_store, self.audit)
        self.live_order_client = live_order_client
        self.live_order_status_client = live_order_status_client
        self.live_order_notification_client = live_order_notification_client
        self.broker_reconciliation_service = broker_reconciliation_service
        self.telegram_client = telegram_client
        self.account_router = BrokerAccountRouter(config)
        self.fx_service = ConfiguredFXRefreshService(config, self.state_store)
        self.config_identity = config_identity
        self.deployment_identity = current_deployment_identity()
        self.order_capacity_lookup = order_capacity_lookup
        self._order_capacity_clients: dict[str | None, Any] = {}

    def run_once(self) -> RunOnceSummary:
        # live_order_lock outermost: _run_once_locked can reach
        # _execute_live_approval_orders -> submit_approved_order, which takes it.
        with self.state_store.live_order_lock("run_once"):
            with self.state_store.writer_lock("run_once"):
                return self._run_once_locked()

    def run_signal(
        self,
        strategy_ids: list[str] | None = None,
        *,
        contribution_override: bool = False,
        source_request_id: str | None = None,
        source_workflow_id: str | None = None,
        source_phase: str | None = None,
    ) -> SignalRunSummary:
        # Fencing (claim_workflow_attempt) only refuses a late *event commit*;
        # it does nothing to stop a stalled attempt that is still inside
        # _run_signal_locked from finishing and building its own signal
        # package and approval flow. If lineage were looked up before this
        # lock, a stalled first attempt and a resumed second attempt could
        # both observe "no child yet" and both build one. So the lookup and
        # the child-creation write must happen inside the same lock
        # acquisition, and the child event is committed under a unique
        # duplicate_key so the database itself caps it at one, even if two
        # threads both got past the in-process check.
        #
        # The lock does not survive a crash, though, and the lineage record is
        # the only thing that tells the next process this request already has
        # a child. That is why it is not written here after the run returns:
        # it goes into the same transaction as the signal package itself (see
        # save_signal_package), so no interruption can leave a package that
        # nothing points at.
        with self.state_store.writer_lock("run_signal"):
            if source_request_id is not None:
                existing = load_workflow_child(
                    self.state_store, source_request_id, source_phase or "funding"
                )
                if existing is not None:
                    return self._reload_signal_summary(existing)
            return self._run_signal_locked(
                strategy_ids=strategy_ids,
                contribution_override=contribution_override,
                source_request_id=source_request_id,
                source_workflow_id=source_workflow_id,
                source_phase=source_phase,
            )

    def _reload_signal_summary(self, signal_run_id: str) -> SignalRunSummary:
        """Rebuild a summary for a child that already exists, without running
        run_signal's side effects (a new signal package, a new approval flow)
        a second time."""
        package = self.state_store.load_signal_package(signal_run_id)
        if package is None:
            # Lineage named this run, so a missing package is an
            # inconsistency, not an empty result. Reporting it as "nothing
            # required" would let a resumed transition silently skip work
            # the original run had queued.
            raise ValueError(f"Child signal run has no package: {signal_run_id}")
        return SignalRunSummary(
            signal_run_id=signal_run_id,
            loaded_strategies=list(package.get("loaded_strategies") or []),
            action_required=bool(package.get("action_required")),
            orders_preview_count=int(package.get("orders_preview_count") or 0),
            contribution_override=bool(package.get("contribution_override")),
            no_order_reasons=list(package.get("no_order_reasons") or []),
        )

    def approve_signal(self, signal_run_id: str) -> SignalApprovalSummary:
        # live_order_lock outermost: _approve_signal_locked can reach
        # _execute_live_approval_orders -> submit_approved_order, which takes it.
        with self.state_store.live_order_lock("approve_signal"):
            with self.state_store.writer_lock("approve_signal"):
                return self._approve_signal_locked(signal_run_id)

    def dispatch_signal_approval(self, signal_run_id: str) -> ApprovalDispatchResult:
        """Persist and send live Telegram approvals without polling for a decision."""
        with self.state_store.writer_lock("dispatch_signal_approval"):
            return self._dispatch_signal_approval_locked(signal_run_id)

    def resolve_pending_signal_approval(
        self,
        envelope: PendingApprovalEnvelope,
        decision: ApprovalDecision,
    ) -> SignalApprovalSummary:
        """Apply one terminal decision loaded by the long-running Telegram operator."""
        with self.state_store.live_order_lock("resolve_pending_signal_approval"):
            if self.state_store.approval_resolution_exists(envelope.approval_id):
                # Somebody already closed this approval -- in practice an
                # operator settling a half-executed batch by hand. A resume
                # that claimed its attempt before that settlement landed is
                # otherwise committed to executing, and would send orders
                # against a batch the operator has been told is closed and is
                # by now replacing themselves. Read under the lock settlement
                # also holds, and before save_approval, which is what precedes
                # every submission.
                raise ValueError(
                    f"Approval {envelope.approval_id} is already closed; not executing"
                )
            package = self.state_store.load_signal_package(envelope.signal_run_id)
            if package is None:
                raise ValueError(f"Unknown signal_run_id: {envelope.signal_run_id}")
            orders = [OrderIntent.model_validate(item) for item in envelope.orders]
            if decision.status == "approved":
                self._validate_signal_package_for_approval(package)
                self._validate_signal_approval_preconditions(envelope.run_id, package)
                # Pure, not the side-effecting _partition_orders_by_capacity:
                # this order already belongs to a pending approval envelope,
                # so it is approval-owned. Writing live_order_capacity_blocked
                # here (the old behavior) made it *also* recovery-owned --
                # visible to the operator retry/recovery flow under a new
                # order_id -- while the approval itself stayed durably
                # resumable (save_approval has not run yet), so the same
                # trade could execute twice once capacity recovered. A
                # resolution-time capacity failure instead fails closed and
                # leaves the ORIGINAL approval the only path back to this
                # order: capacity is rechecked, not re-owned, on the next
                # resume.
                orders, capacity_blocks = self._partition_orders_by_capacity_pure(orders)
                if capacity_blocks or not orders:
                    raise ValueError("Pending approval is blocked by current broker capacity")
                self._validate_signal_approval_gates(envelope.run_id, orders, package)

            approval_payload = {
                "signal_run_id": envelope.signal_run_id,
                "source_strategy_ids": envelope.source_strategy_ids,
                "request": envelope.request.model_dump(mode="json"),
                "decision": decision.model_dump(mode="json"),
                "message": envelope.message,
                "account_ids": envelope.account_ids,
            }
            if len(envelope.account_ids) == 1:
                approval_payload["account_id"] = envelope.account_ids[0]
            self.state_store.save_approval(
                envelope.run_id,
                envelope.approval_id,
                approval_payload,
            )
            self.audit.log(envelope.run_id, "approval_decision", approval_payload)

            lifecycle_results: list[LiveOrderLifecycleResult] = []
            if decision.status == "approved":
                lifecycle_results, next_state = self._execute_live_approval_orders(
                    envelope.run_id,
                    orders,
                    envelope.approval_id,
                    decision,
                    signal_run_id=envelope.signal_run_id,
                )
                if self.config.mode != RunMode.LIVE_APPROVAL:
                    self.state_store.save_portfolio_snapshot(envelope.run_id, next_state)
            else:
                self.state_store.save_system_event(
                    envelope.run_id,
                    SystemEventType.EXECUTION_SKIPPED,
                    {
                        "signal_run_id": envelope.signal_run_id,
                        "source_strategy_ids": envelope.source_strategy_ids,
                        "approval_status": decision.status,
                    },
                )

            for order in orders:
                order_payload = order.model_dump(mode="json")
                order_payload["signal_run_id"] = envelope.signal_run_id
                order_payload["approval_status"] = decision.status
                if not (
                    self.config.mode == RunMode.LIVE_APPROVAL
                    and self._effective_order_posture(order) == "dry_run"
                ):
                    self.state_store.save_order(
                        envelope.run_id,
                        order.order_id,
                        order_payload,
                    )
                if decision.status == "expired":
                    duplicate_key = f"live-order-recovery-candidate:{order.order_id}"
                    if not self.state_store.duplicate_key_exists(duplicate_key):
                        self._record_event(
                            envelope.run_id,
                            "live_order_recovery_candidate",
                            {
                                "source_order_id": order.order_id,
                                "order": order.model_dump(mode="json"),
                                "source_type": "approval_expired",
                                "reason": "telegram_approval_expired_before_submit",
                                "signal_run_id": envelope.signal_run_id,
                                "created_at": decision.decided_at.isoformat(),
                                "status": "pending",
                                "duplicate_key": duplicate_key,
                            },
                        )
                        self._notify_recovery_order(
                            envelope.run_id,
                            order,
                            "telegram_approval_expired_before_submit",
                        )
            self.state_store.save_system_event(
                envelope.run_id,
                "signal_approval_completed",
                {
                    "approval_id": envelope.approval_id,
                    "signal_run_id": envelope.signal_run_id,
                    "orders_created": len(orders),
                    "orders_planned": len(envelope.orders),
                    "orders_submitted": sum(
                        result.submitted_order is not None for result in lifecycle_results
                    ),
                    "orders_accepted": sum(
                        result.broker_order_id is not None for result in lifecycle_results
                    ),
                    "orders_filled": sum(
                        result.final_status.value == "filled" for result in lifecycle_results
                    ),
                    "orders_failed": sum(
                        result.final_status.value in {"failed", "rejected", "halted"}
                        for result in lifecycle_results
                    ),
                    "approval_status": decision.status,
                    "approval_count": 1,
                    "approval_statuses": [decision.status],
                },
            )
            return SignalApprovalSummary(
                signal_run_id=envelope.signal_run_id,
                run_id=envelope.run_id,
                orders_created=len(orders),
                approval_status=decision.status,
                orders_planned=len(envelope.orders),
                orders_submitted=sum(
                    result.submitted_order is not None for result in lifecycle_results
                ),
                orders_accepted=sum(
                    result.broker_order_id is not None for result in lifecycle_results
                ),
                orders_filled=sum(
                    result.final_status.value == "filled" for result in lifecycle_results
                ),
                orders_failed=sum(
                    result.final_status.value in {"failed", "rejected", "halted"}
                    for result in lifecycle_results
                ),
            )

    def _run_signal_locked(
        self,
        *,
        strategy_ids: list[str] | None = None,
        contribution_override: bool = False,
        source_request_id: str | None = None,
        source_workflow_id: str | None = None,
        source_phase: str | None = None,
    ) -> SignalRunSummary:
        signal_run_id = new_signal_run_id()
        self._record_run_provenance(signal_run_id, "signal")
        selected_strategy_ids = set(strategy_ids or [])
        if selected_strategy_ids:
            unknown = selected_strategy_ids - self.registry.strategy_ids
            if unknown:
                raise ValueError(
                    "Unknown or disabled signal strategy id(s): " + ", ".join(sorted(unknown))
                )
        required_account_ids = required_account_ids_for_strategies(
            self.config,
            selected_strategy_ids or None,
        )
        if self.config.mode == RunMode.LIVE_APPROVAL and required_account_ids:
            bootstrap_account_ids = {
                account_id
                for account_id in required_account_ids
                if self.state_store.load_latest_account_portfolio_state(account_id) is None
            }
            report = refresh_readonly_accounts(
                self.config,
                self.config_identity,
                account_ids=required_account_ids,
                source="signal_preflight",
                max_snapshot_age_seconds=(
                    self.config.reconciliation.signal_snapshot_max_age_seconds
                ),
                state_store=self.state_store,
                audit_logger=self.audit,
            )
            blocking_account_ids = sorted(set(report.failed_account_ids) - bootstrap_account_ids)
            if blocking_account_ids:
                raise ValueError(
                    "signal readonly preflight failed for required account(s): "
                    + ", ".join(blocking_account_ids)
                )
        current_state = self._load_run_portfolio_state(
            signal_run_id,
            account_ids=required_account_ids or None,
            use_cached_broker_snapshots=bool(required_account_ids),
        )
        broker_snapshot_refs = self._broker_snapshot_refs(required_account_ids)
        dynamic_symbols = self._evaluate_dynamic_universe(signal_run_id, current_state)
        run_allowed_symbols = set(self.config.portfolio.allowed_symbols) | dynamic_symbols
        risk_manager = (
            RiskManager(
                sorted(run_allowed_symbols),
                max_position_weight=self.config.risk.max_position_weight,
            )
            if dynamic_symbols
            else self.risk_manager
        )
        valid_results, data_requests_by_strategy, data_quality_issues, prices = (
            self._collect_strategy_results(
                signal_run_id,
                current_state,
                strategy_ids=selected_strategy_ids or None,
                allowed_symbols=run_allowed_symbols,
            )
        )
        native_prices = self._enrich_sleeve_prices(prices)
        valuation_prices = self._apply_fx_prices(signal_run_id, native_prices)
        target, risk_decision, order_targets = self._build_account_scoped_targets(
            signal_run_id,
            valid_results,
            risk_manager,
            current_state,
            valuation_prices,
        )
        if not risk_decision.approved:
            raise ValueError(f"Risk check failed: {risk_decision.violations}")
        order_generation_time = utc_now()
        order_prices = self._order_generation_prices(native_prices)
        orders = []
        funding_requests: list[ContributionFundingRequest] = []
        budget_requests: list[ContributionBudgetRequest] = []
        no_order_reasons: list[str] = []
        for order_scope in order_targets:
            scoped_execution = build_execution_engine(
                order_scope.execution_config,
                instruments=self.config.universe.instruments,
                currency_sleeves=self.config.portfolio.currency_sleeves,
            )
            contribution_already_executed = self._contribution_already_executed(
                order_generation_time,
                order_scope.execution_config,
                execution_sleeve=order_scope.execution_sleeve,
                account_id=order_scope.account_id,
            )
            if order_scope.requires_budget_request:
                budget_request = self._contribution_budget_request(
                    signal_run_id,
                    order_scope,
                    scoped_execution,
                    order_generation_time,
                    contribution_already_executed=contribution_already_executed,
                    contribution_override=contribution_override,
                )
                if budget_request is not None:
                    budget_requests.append(budget_request)
                else:
                    no_order_reasons.append(
                        self._no_order_reason(
                            order_scope,
                            scoped_execution,
                            order_generation_time,
                            contribution_already_executed=contribution_already_executed,
                            contribution_override=contribution_override,
                        )
                    )
                continue
            account_orders = scoped_execution.propose_orders(
                order_scope.state,
                order_scope.target,
                # Order sizing must use native prices: a currency sleeve holds a
                # single currency and its cash stays in that currency, so pricing it
                # in the base currency scales buys by the FX rate and rounds them to
                # zero. Base-currency prices are for cross-currency valuation only.
                native_prices,
                as_of=order_generation_time,
                contribution_already_executed=contribution_already_executed,
                contribution_override=contribution_override,
            )
            if not account_orders:
                funding_request = self._contribution_funding_request(
                    signal_run_id,
                    order_scope,
                    scoped_execution,
                    order_generation_time,
                    contribution_already_executed=contribution_already_executed,
                    contribution_override=contribution_override,
                )
                if funding_request is not None:
                    funding_requests.append(funding_request)
                else:
                    no_order_reasons.append(
                        self._no_order_reason(
                            order_scope,
                            scoped_execution,
                            order_generation_time,
                            contribution_already_executed=contribution_already_executed,
                            contribution_override=contribution_override,
                        )
                    )
            orders.extend(
                self._apply_native_order_prices(
                    self._stamp_orders_with_account_id(
                        account_orders,
                        order_scope.target.source_strategy_ids,
                        account_id=order_scope.account_id,
                        execution_sleeve=order_scope.execution_sleeve,
                        contribution_group_id=order_scope.contribution_group_id,
                        signal_preview=True,
                    ),
                    order_prices,
                )
            )
        approval_orders = self._signal_orders_requiring_approval(orders)
        lineage_events: list[dict[str, Any]] = []
        if source_request_id is not None:
            lineage_events.append(
                {
                    "event_type": "funding_workflow_child_created",
                    "payload": {
                        "duplicate_key": child_key(source_request_id, source_phase or "funding"),
                        "workflow_id": source_workflow_id,
                        "request_id": source_request_id,
                        "phase": source_phase or "funding",
                        "signal_run_id": signal_run_id,
                    },
                }
            )

        def build_package_payload(
            live_funding: list[ContributionFundingRequest],
            live_budget: list[ContributionBudgetRequest],
        ) -> dict[str, Any]:
            if live_budget:
                package_status = "budget_required"
            elif approval_orders:
                package_status = "action_required"
            elif live_funding:
                package_status = "funding_required"
            else:
                package_status = "no_action"
            package = {
                "signal_run_id": signal_run_id,
                "status": package_status,
                "approval_consumed": False,
                "generated_at": utc_now().isoformat(),
                "loaded_strategies": [result.strategy_id for result in valid_results],
                "strategy_account_mappings": self._strategy_account_mappings(),
                "strategy_phase_controls": self._strategy_phase_controls(),
                "data_requests": data_requests_by_strategy,
                "data_quality_issues": data_quality_issues,
                "datahub_evidence": self._datahub_evidence(
                    data_requests_by_strategy,
                    data_quality_issues,
                    prices,
                ),
                "broker_snapshot_refs": broker_snapshot_refs,
                "required_account_ids": required_account_ids,
                "prices": prices,
                "strategy_results": [result.model_dump(mode="json") for result in valid_results],
                "portfolio_target": target.model_dump(mode="json"),
                "risk_decision": risk_decision.model_dump(mode="json"),
                "orders_preview": [order.model_dump(mode="json") for order in orders],
                "orders_preview_count": len(orders),
                "funding_requests": [item.model_dump(mode="json") for item in live_funding],
                "funding_requests_count": len(live_funding),
                "budget_requests": [item.model_dump(mode="json") for item in live_budget],
                "budget_requests_count": len(live_budget),
                "action_required": bool(approval_orders) and not live_budget,
                "contribution_override": contribution_override,
                "no_order_reasons": no_order_reasons,
            }
            package["config_signal_contract_fingerprint"] = _signal_contract_fingerprint(
                self.config
            )
            if self.config_identity is not None:
                package["config_runtime_fingerprint"] = self.config_identity.runtime_fingerprint
            return package

        funding_requests, budget_requests, payload = self._commit_signal_package(
            signal_run_id,
            funding_requests,
            budget_requests,
            build_package_payload,
            lineage_events,
            source_request_id=source_request_id,
            source_phase=source_phase,
        )
        status = str(payload["status"])
        self.audit.log(signal_run_id, "signal_package", payload)
        self.state_store.save_system_event(
            signal_run_id,
            "signal_run_completed",
            {
                "signal_run_id": signal_run_id,
                "status": status,
                "orders_preview_count": len(orders),
                "funding_requests_count": len(funding_requests),
                "budget_requests_count": len(budget_requests),
                "action_required": bool(approval_orders) and not budget_requests,
            },
        )
        return SignalRunSummary(
            signal_run_id=signal_run_id,
            loaded_strategies=[result.strategy_id for result in valid_results],
            action_required=bool(approval_orders) and not budget_requests,
            orders_preview_count=len(orders),
            contribution_override=contribution_override,
            no_order_reasons=no_order_reasons,
        )

    def _approve_signal_locked(self, signal_run_id: str) -> SignalApprovalSummary:
        package = self.state_store.load_signal_package(signal_run_id)
        if package is None:
            raise ValueError(f"Unknown signal_run_id: {signal_run_id}")
        if package.get("approval_consumed"):
            raise ValueError(f"Signal package already consumed: {signal_run_id}")
        orders = [
            OrderIntent.model_validate(order_payload)
            for order_payload in package.get("orders_preview", [])
        ]
        approval_orders = self._orders_requiring_approval(orders)
        run_id = new_run_id()
        self._record_run_provenance(run_id, "approval", signal_run_id=signal_run_id)
        if not approval_orders:
            self.state_store.mark_signal_package_consumed(signal_run_id, run_id)
            self.state_store.save_system_event(
                run_id,
                "signal_approval_completed",
                {
                    "signal_run_id": signal_run_id,
                    "orders_created": 0,
                    "approval_status": "not_required",
                },
            )
            return SignalApprovalSummary(
                signal_run_id=signal_run_id,
                run_id=run_id,
                orders_created=0,
                approval_status="not_required",
            )
        signal_account_mappings = package.get("strategy_account_mappings")
        if (
            signal_account_mappings is not None
            and signal_account_mappings != self._strategy_account_mappings()
        ):
            raise ValueError(
                "Signal package account mapping mismatch: "
                f"signal={signal_account_mappings} current={self._strategy_account_mappings()}"
            )
        baseline_refs = package.get("broker_snapshot_refs") or []
        if self.config.mode == RunMode.LIVE_APPROVAL and baseline_refs:
            self._validate_signal_broker_baseline(baseline_refs)
        required_account_ids = [
            str(account_id) for account_id in package.get("required_account_ids") or []
        ]
        if self.config.mode == RunMode.LIVE_APPROVAL and required_account_ids:
            report = refresh_readonly_accounts(
                self.config,
                self.config_identity,
                account_ids=required_account_ids,
                source="approval_preflight",
                max_snapshot_age_seconds=(
                    self.config.reconciliation.approval_snapshot_max_age_seconds
                ),
                state_store=self.state_store,
                audit_logger=self.audit,
            )
            if report.failed_account_ids:
                raise ValueError(
                    "approval readonly preflight failed for required account(s): "
                    + ", ".join(report.failed_account_ids)
                )
        self._validate_signal_package_for_approval(package)
        self._validate_signal_approval_preconditions(run_id, package)
        approval_orders, capacity_blocks = self._partition_orders_by_capacity(
            run_id,
            approval_orders,
            signal_run_id=signal_run_id,
            package=package,
        )
        if not approval_orders:
            self.state_store.mark_signal_package_consumed(signal_run_id, run_id)
            self.state_store.save_system_event(
                run_id,
                "signal_approval_completed",
                {
                    "signal_run_id": signal_run_id,
                    "orders_created": 0,
                    "approval_status": "capacity_blocked",
                    "capacity_blocked_count": len(capacity_blocks),
                },
            )
            return SignalApprovalSummary(
                signal_run_id=signal_run_id,
                run_id=run_id,
                orders_created=0,
                approval_status="capacity_blocked",
            )
        self._validate_signal_approval_gates(run_id, approval_orders, package)
        self.state_store.mark_signal_package_consumed(signal_run_id, run_id)

        risk_violations = package.get("risk_decision", {}).get("violations", [])
        current_state = self.state_store.load_latest_portfolio_state()
        next_state = current_state
        approval_statuses: list[str] = []
        approval_count = 0
        for source_strategy_ids, group_orders in self._approval_order_groups(
            approval_orders,
            package,
        ):
            approval_request = None
            approval_decision = None
            approval_message = None
            if group_orders:
                approval_request, approval_decision, approval_message = (
                    self.approval_manager.request_approval(
                        run_id,
                        group_orders,
                        risk_violations,
                        source_strategy_ids,
                    )
                )
            if approval_request and approval_decision:
                approval_count += 1
                approval_statuses.append(approval_decision.status)
                approval_payload = {
                    "signal_run_id": signal_run_id,
                    "source_strategy_ids": list(source_strategy_ids),
                    "request": approval_request.model_dump(mode="json"),
                    "decision": approval_decision.model_dump(mode="json"),
                    "message": approval_message,
                    "account_ids": sorted(
                        {order.account_id for order in group_orders if order.account_id}
                    ),
                }
                if len(approval_payload["account_ids"]) == 1:
                    approval_payload["account_id"] = approval_payload["account_ids"][0]
                self.state_store.save_approval(
                    run_id,
                    approval_request.approval_id,
                    approval_payload,
                )
                self.audit.log(run_id, "approval_decision", approval_payload)
                if approval_decision.status != "approved":
                    self.state_store.save_system_event(
                        run_id,
                        SystemEventType.EXECUTION_SKIPPED,
                        {
                            "signal_run_id": signal_run_id,
                            "source_strategy_ids": list(source_strategy_ids),
                            "approval_status": approval_decision.status,
                        },
                    )
                elif self.config.mode == RunMode.LIVE_APPROVAL:
                    _, next_state = self._execute_live_approval_orders(
                        run_id,
                        group_orders,
                        approval_request.approval_id,
                        approval_decision,
                        signal_run_id=signal_run_id,
                    )
                else:
                    _, next_state = self.execution.execute_orders(next_state, group_orders)
            else:
                approval_statuses.append("not_required")
                if self.config.mode == RunMode.LIVE_APPROVAL:
                    raise ValueError("live_approval mode requires an approval decision")
                _, next_state = self.execution.execute_orders(next_state, group_orders)

            approval_status = approval_decision.status if approval_decision else "not_required"
            for order in group_orders:
                order_payload = order.model_dump(mode="json")
                order_payload["signal_run_id"] = signal_run_id
                order_payload["approval_status"] = approval_status
                if not (
                    self.config.mode == RunMode.LIVE_APPROVAL
                    and self._effective_order_posture(order) == "dry_run"
                ):
                    self.state_store.save_order(run_id, order.order_id, order_payload)

        approval_status = _combined_approval_status(approval_statuses)
        if self.config.mode != RunMode.LIVE_APPROVAL:
            self.state_store.save_portfolio_snapshot(run_id, next_state)
        self.state_store.save_system_event(
            run_id,
            "signal_approval_completed",
            {
                "signal_run_id": signal_run_id,
                "orders_created": len(approval_orders),
                "approval_status": approval_status,
                "approval_count": approval_count,
                "approval_statuses": approval_statuses,
            },
        )
        return SignalApprovalSummary(
            signal_run_id=signal_run_id,
            run_id=run_id,
            orders_created=len(approval_orders),
            approval_status=approval_status,
        )

    def _dispatch_signal_approval_locked(
        self,
        signal_run_id: str,
    ) -> ApprovalDispatchResult:
        if self.config.mode != RunMode.LIVE_APPROVAL:
            raise ValueError("Async Telegram dispatch requires live_approval mode")
        if self.config.approval.provider != "telegram":
            raise ValueError("Async approval dispatch requires Telegram approval")
        package = self.state_store.load_signal_package(signal_run_id)
        if package is None:
            raise ValueError(f"Unknown signal_run_id: {signal_run_id}")
        already_consumed = bool(package.get("approval_consumed"))
        if already_consumed and self.state_store.signal_dispatch_settled(signal_run_id):
            raise ValueError(f"Signal package already consumed: {signal_run_id}")
        # Consumed but never settled means a dispatch died inside the group
        # loop below. Refusing here -- which is what used to happen -- left the
        # run stranded forever: some groups had approvals and the rest never
        # would. Fall through and re-enter instead. Every group is now filed
        # under a stable key, so the ones already dispatched are adopted rather
        # than duplicated.
        orders = [
            OrderIntent.model_validate(order_payload)
            for order_payload in package.get("orders_preview", [])
        ]
        approval_orders = self._orders_requiring_approval(orders)
        run_id = new_run_id()
        self._record_run_provenance(run_id, "approval_dispatch", signal_run_id=signal_run_id)
        if not approval_orders:
            if not already_consumed:
                # A resume re-enters this method; marking again would stack
                # a second consumed row for the same package.
                self.state_store.mark_signal_package_consumed(signal_run_id, run_id)
            self.state_store.save_system_event(
                run_id,
                "signal_approval_completed",
                {
                    "signal_run_id": signal_run_id,
                    "orders_created": 0,
                    "orders_planned": 0,
                    "approval_status": "not_required",
                },
            )
            return ApprovalDispatchResult(
                signal_run_id=signal_run_id,
                run_id=run_id,
                orders_planned=0,
                approval_status="not_required",
            )

        signal_account_mappings = package.get("strategy_account_mappings")
        if (
            signal_account_mappings is not None
            and signal_account_mappings != self._strategy_account_mappings()
        ):
            raise ValueError(
                "Signal package account mapping mismatch: "
                f"signal={signal_account_mappings} current={self._strategy_account_mappings()}"
            )
        baseline_refs = package.get("broker_snapshot_refs") or []
        if baseline_refs:
            self._validate_signal_broker_baseline(baseline_refs)
        required_account_ids = [
            str(account_id) for account_id in package.get("required_account_ids") or []
        ]
        if required_account_ids:
            report = refresh_readonly_accounts(
                self.config,
                self.config_identity,
                account_ids=required_account_ids,
                source="approval_preflight",
                max_snapshot_age_seconds=(
                    self.config.reconciliation.approval_snapshot_max_age_seconds
                ),
                state_store=self.state_store,
                audit_logger=self.audit,
            )
            if report.failed_account_ids:
                raise ValueError(
                    "approval readonly preflight failed for required account(s): "
                    + ", ".join(report.failed_account_ids)
                )
        self._validate_signal_package_for_approval(package)
        self._validate_signal_approval_preconditions(run_id, package)
        # The manifest -- not a fresh capacity partition -- is what decides
        # which groups this dispatch is obligated to resolve. Recomputing
        # groups from live capacity on every resume let capacity that
        # tightened between attempts make a group vanish from the
        # computation entirely: never dispatched, never recorded as blocked,
        # simply absent, while the settled event still fired over it. The
        # manifest is built once, from the same posture-filtered orders a
        # capacity check would have started from, and is what every later
        # call -- including this one, if it already exists -- iterates
        # instead. Capacity is still checked, per group, below; what it can
        # no longer do is make a group disappear without a trace.
        manifest = self._load_or_build_dispatch_manifest(signal_run_id, approval_orders, package)
        self._validate_signal_approval_gates(run_id, approval_orders, package)
        if not already_consumed:
            # A resume re-enters this method; marking again would stack
            # a second consumed row for the same package.
            self.state_store.mark_signal_package_consumed(signal_run_id, run_id)

        client = self.telegram_client or TelegramBotAPIClient(
            token_env=self.config.approval.telegram_bot_token_env,
            timeout_seconds=10.0,
        )
        risk_violations = package.get("risk_decision", {}).get("violations", [])
        orders_by_id = {order.order_id: order for order in approval_orders}
        pending_count = 0
        orders_created = 0
        orders_capacity_blocked = 0
        for group in manifest["groups"]:
            group_id = str(group["group_id"])
            source_strategy_ids = [str(item) for item in group["source_strategy_ids"]]
            try:
                manifest_orders = [orders_by_id[str(order_id)] for order_id in group["order_ids"]]
            except KeyError as exc:
                raise ValueError(
                    f"Dispatch manifest for {signal_run_id} names order {exc} that the "
                    "package no longer carries"
                ) from exc
            manifest_order_ids = {order.order_id for order in manifest_orders}
            blocked_key = f"{group_id}:capacity_blocked"
            # 이 그룹의 승인이 이미 있으면 그것이 권위다. 새로 만들면
            # create_request가 새 approval_id와 utc_now 기준 만료시각을
            # 발급하므로(approval/manager.py), 같은 주문에 버튼 달린 카드가
            # 두 장 생기고 마감 시한이 재개마다 연장된다.
            stored = self.state_store.load_system_event_payload_by_duplicate_key(group_id)
            blocked_disposition = self.state_store.load_system_event_payload_by_duplicate_key(
                blocked_key
            )
            if stored is None:
                if blocked_disposition is None:
                    # The one and only time capacity is ever consulted for
                    # this group -- see _load_or_build_dispatch_manifest for
                    # why group *membership* never gets recomputed; this is
                    # the same guarantee applied to which of that membership
                    # is blocked. Once this decision is durable, a later
                    # call derives the accepted complement from it instead
                    # of asking capacity again, which could disagree with
                    # what was already decided.
                    #
                    # The evaluation itself (_pure) has no side effects: no
                    # durable record and no notification exist yet. Both are
                    # only ever committed together with the group
                    # disposition below, atomically -- a crash between
                    # evaluating capacity and here leaves nothing durable at
                    # all, rather than a recovery-visible record for a block
                    # the group disposition never actually committed to.
                    accepted, blocked = self._partition_orders_by_capacity_pure(manifest_orders)
                    blocked_order_ids = sorted(item.order.order_id for item in blocked)
                    if blocked_order_ids:
                        group_payload = {
                            "signal_run_id": signal_run_id,
                            "group_id": group_id,
                            "source_strategy_ids": source_strategy_ids,
                            # The exact orders blocked, not just the fact
                            # that some were: this is what lets a later
                            # call verify that every manifest order has
                            # exactly one disposition, and what lets the
                            # accepted complement below be derived
                            # without re-consulting capacity.
                            "blocked_order_ids": blocked_order_ids,
                            "duplicate_key": blocked_key,
                        }
                        live_events = [
                            {
                                "payload": {
                                    **item.model_dump(mode="json"),
                                    "blocked_order_id": item.order.order_id,
                                    "signal_run_id": signal_run_id,
                                    "status": "pending",
                                    "config_signal_contract_fingerprint": (
                                        package or {}
                                    ).get("config_signal_contract_fingerprint"),
                                    "config_runtime_fingerprint": (package or {}).get(
                                        "config_runtime_fingerprint"
                                    ),
                                },
                                "duplicate_key": f"{blocked_key}:live:{item.order.order_id}",
                            }
                            for item in blocked
                        ]
                        blocked_disposition, live_payloads, created = (
                            self.state_store.insert_or_load_dispatch_group_capacity_block(
                                run_id, group_payload, blocked_key, live_events
                            )
                        )
                        if created:
                            # The atomic store primitive writes system_events
                            # directly, bypassing save_audited_system_event --
                            # mirror its audit.log call here, and only for
                            # the batch this call actually committed, so a
                            # replay (adopted from a prior attempt or a
                            # concurrent winner) does not duplicate the
                            # hash-chained audit trail for content that was
                            # already logged when it first landed.
                            self.audit.log(
                                run_id, "dispatch_group_capacity_blocked", blocked_disposition
                            )
                            for live_payload in live_payloads:
                                self.audit.log(run_id, "live_order_capacity_blocked", live_payload)
                        # This call's own submission is not guaranteed to be
                        # what actually landed -- a concurrent writer's
                        # decision for this exact key may have won instead.
                        # accepted must reflect whichever content is now
                        # durable, not this call's own (possibly stale)
                        # local partition, or an order that content calls
                        # blocked could still reach an approval envelope.
                        winning_blocked_ids = set(
                            blocked_disposition.get("blocked_order_ids") or []
                        )
                        accepted = [
                            order
                            for order in manifest_orders
                            if order.order_id not in winning_blocked_ids
                        ]
                else:
                    # A prior attempt already decided and durably recorded
                    # this group's blocked subset (in full or in part).
                    # Capacity is not re-consulted -- the accepted
                    # complement is derived from that record alone, so a
                    # resume can never disagree with what dispatching
                    # already committed to.
                    already_blocked_ids = set(blocked_disposition.get("blocked_order_ids") or [])
                    accepted = [
                        order
                        for order in manifest_orders
                        if order.order_id not in already_blocked_ids
                    ]
                if accepted:
                    stored = self._create_pending_approval_envelope(
                        run_id,
                        signal_run_id,
                        group_id,
                        source_strategy_ids,
                        accepted,
                        risk_violations,
                    )

            envelope_order_ids = (
                {str(order.get("order_id")) for order in stored.get("orders") or []}
                if stored is not None
                else set()
            )
            blocked_order_ids_for_group = set(
                (blocked_disposition or {}).get("blocked_order_ids") or []
            )
            # Every manifest order must land in exactly one of the two sets
            # above -- never neither (silently unaccounted), never both.
            # Checked on every visit, not only the one that just decided
            # it, so an adopted envelope or disposition from a prior
            # attempt is held to the same durable proof a fresh one is.
            self._verify_group_disposition(
                group_id, manifest_order_ids, envelope_order_ids, blocked_order_ids_for_group
            )
            # Only after the disposition above is proven durable does the
            # operator ever hear about it -- on every visit, not only the
            # one that just committed it, so a crash between the commit and
            # this delivery loses nothing but the notification, which is
            # retried here, not the disposition itself.
            for blocked_order_id in sorted(blocked_order_ids_for_group):
                self._deliver_capacity_block_notification(
                    run_id, blocked_key, blocked_order_id
                )
            orders_capacity_blocked += len(blocked_order_ids_for_group)
            if stored is None:
                # Every manifest order for this group is durably blocked;
                # _verify_group_disposition above already proved it, so
                # there is nothing left to create or deliver.
                continue

            envelope = PendingApprovalEnvelope.model_validate(stored)
            # The manifest's own (capacity-independent) membership, not a
            # fresh capacity computation: an envelope adopted from a prior
            # attempt may legitimately hold fewer orders than that if some
            # were capacity-blocked when it was created, and re-checking
            # capacity here would only recompute what dispatching already
            # settled for this group.
            self._verify_reused_envelope(
                envelope, signal_run_id, source_strategy_ids, manifest_orders
            )
            request = envelope.request
            card = render_approval_stage_card(request, "pending")
            # 카드는 태어날 때부터 lifecycle이 소유한다. 여기서 직접 보내면
            # message_id가 어디에도 남지 않아 이후 단계 변화를 그 메시지에
            # 반영할 수 없고, sweep이 두 번째 카드를 새로 보내게 된다 — 한
            # 승인에 버튼 달린 카드가 두 장 생기는 바로 그 상태다.
            delivery = CardLifecycleManager(
                self.state_store,
                self.audit,
                client,
                chat_ids=[
                    int(chat_id) for chat_id in self.config.approval.telegram_allowed_chat_ids
                ],
            ).deliver(run_id, f"approval:{request.approval_id}", "pending", card)
            if not delivery["sent"] and not delivery["unknown"]:
                # 모든 채팅이 명시적으로 거절됐다 — 승인 요청이 아무에게도
                # 닿지 않았음이 확정이다. 전송 불명(unknown)은 여기 해당하지
                # 않는다: 텔레그램이 이미 받았을 수 있고, 그것을 실패로 접으면
                # 재전송으로 이어진다.
                raise RuntimeError(
                    f"Telegram refused the approval card for every chat: {request.approval_id}"
                )
            pending_count += 1
            orders_created += len(envelope.orders)

        if pending_count > 0:
            self.state_store.save_system_event(
                run_id,
                "signal_approval_pending",
                {
                    "signal_run_id": signal_run_id,
                    "orders_created": orders_created,
                    "orders_planned": len(orders),
                    "orders_capacity_blocked": orders_capacity_blocked,
                    "approvals_pending": pending_count,
                    "approval_status": "pending",
                },
            )
            return ApprovalDispatchResult(
                signal_run_id=signal_run_id,
                run_id=run_id,
                orders_planned=len(orders),
                orders_capacity_blocked=orders_capacity_blocked,
                approvals_pending=pending_count,
                approval_status="pending",
            )
        # Every manifest group resolved to capacity_blocked -- none newly,
        # none in a prior attempt. Settling here (rather than leaving the
        # dispatch open) matches the old all-blocked outcome: nothing is
        # pending an operator decision, so there is nothing left to resume.
        self.state_store.save_system_event(
            run_id,
            "signal_approval_completed",
            {
                "signal_run_id": signal_run_id,
                "orders_created": 0,
                "orders_planned": len(orders),
                "orders_capacity_blocked": orders_capacity_blocked,
                "approval_status": "capacity_blocked",
            },
        )
        return ApprovalDispatchResult(
            signal_run_id=signal_run_id,
            run_id=run_id,
            orders_planned=len(orders),
            orders_capacity_blocked=orders_capacity_blocked,
            approval_status="capacity_blocked",
        )

    def _create_pending_approval_envelope(
        self,
        run_id: str,
        signal_run_id: str,
        group_id: str,
        source_strategy_ids: list[str],
        accepted: list[OrderIntent],
        risk_violations: list[Any],
    ) -> dict[str, Any]:
        """Create (or adopt, if a race already filed one) this group's envelope.

        Split out of the dispatch loop because both places that reach a
        group's first-ever envelope -- capacity never having been checked
        for it yet, and capacity already having been durably decided by an
        earlier attempt -- need the exact same construction, and this is the
        one place ``insert_or_load_system_event`` is called for it.
        """
        request = self.approval_manager.create_request(
            run_id, accepted, risk_violations, source_strategy_ids
        )
        if request is None:
            raise ValueError("live_approval mode requires an approval request")
        candidate = PendingApprovalEnvelope(
            approval_id=request.approval_id,
            run_id=run_id,
            signal_run_id=signal_run_id,
            request=request,
            orders=[order.model_dump(mode="json") for order in accepted],
            message=render_approval_stage_card(request, "pending").text,
            source_strategy_ids=source_strategy_ids,
            account_ids=sorted({order.account_id for order in accepted if order.account_id}),
            reminder_seconds=self.config.approval.telegram_reminder_seconds,
            created_at=request.created_at,
            expires_at=request.expires_at,
            duplicate_key=group_id,
            # 카드 전송은 바로 아래에서 lifecycle이 한다. 이 표시가 있어야
            # sweep이 "전송 전에 죽은 승인"을 "구 코드가 이미 보낸 승인"과
            # 구분해 살려낼 수 있다.
            card_delivery_version=1,
        )
        stored, created = self.state_store.insert_or_load_system_event(
            run_id,
            "telegram_approval_pending",
            candidate.model_dump(mode="json"),
            group_id,
        )
        if created:
            self.audit.log(run_id, "telegram_approval_pending", stored)
        return stored

    def _load_or_build_dispatch_manifest(
        self,
        signal_run_id: str,
        approval_orders: list[OrderIntent],
        package: dict[str, Any],
    ) -> dict[str, Any]:
        """The durable, authoritative list of groups this dispatch must resolve.

        Built once, from ``approval_orders`` before any capacity check -- the
        same canonical grouping ``_approval_order_groups`` always produces
        from the package's own posture-filtered orders, so it is a function
        of data that does not change between attempts. A later call that
        finds one already on record loads it rather than recomputing: not
        because recomputing would usually differ (it would not, from the
        same inputs), but because loading is what makes "the same groups
        every time" a property of the record instead of an assumption about
        every future caller's inputs staying identical.
        """
        manifest_key = f"dispatch-manifest:{signal_run_id}"
        stored = self.state_store.load_system_event_payload_by_duplicate_key(manifest_key)
        if stored is not None:
            return stored
        groups = [
            {
                "group_id": dispatch_group_id(signal_run_id, source_strategy_ids),
                "source_strategy_ids": list(source_strategy_ids),
                "order_ids": [order.order_id for order in group_orders],
            }
            for source_strategy_ids, group_orders in self._approval_order_groups(
                approval_orders, package
            )
        ]
        stored, _ = self.state_store.insert_or_load_system_event(
            new_run_id(),
            "signal_dispatch_manifest",
            {"signal_run_id": signal_run_id, "groups": groups},
            manifest_key,
        )
        return stored

    def _run_once_locked(self) -> RunOnceSummary:
        run_id = new_run_id()
        self._record_run_provenance(run_id, "run_once")
        current_state = self._load_run_portfolio_state(run_id)
        valid_results: list[TargetAllocationResult] = []
        data_requests_by_strategy: dict[str, Any] = {}
        data_quality_issues: list[dict[str, Any]] = []
        prices = self._initial_prices()

        try:
            self._record_event(
                run_id,
                SystemEventType.MAESTRO_HEARTBEAT,
                {"mode": self.config.mode.value, "phase": "run_once_started"},
            )
            dynamic_symbols = self._evaluate_dynamic_universe(run_id, current_state)
            run_allowed_symbols = set(self.config.portfolio.allowed_symbols) | dynamic_symbols
            risk_manager = (
                RiskManager(
                    sorted(run_allowed_symbols),
                    max_position_weight=self.config.risk.max_position_weight,
                )
                if dynamic_symbols
                else self.risk_manager
            )
            valid_results, data_requests_by_strategy, data_quality_issues, prices = (
                self._collect_strategy_results(
                    run_id,
                    current_state,
                    allowed_symbols=run_allowed_symbols,
                    data_requests_by_strategy=data_requests_by_strategy,
                )
            )

            native_prices = self._enrich_sleeve_prices(prices)
            valuation_prices = self._apply_fx_prices(run_id, native_prices)
            target, risk_decision, order_targets = self._build_account_scoped_targets(
                run_id,
                valid_results,
                risk_manager,
                current_state,
                valuation_prices,
            )
            if not risk_decision.approved:
                raise ValueError(f"Risk check failed: {risk_decision.violations}")

            safety_state = self.safety.current_state()
            if self.config.mode == RunMode.LIVE_APPROVAL and data_quality_issues:
                live_blocks = self._live_execution_blocks(run_id, [], data_quality_issues)
                if live_blocks:
                    self.safety.halt(
                        run_id,
                        "Live approval blocked by production hardening gate.",
                        source="system",
                    )
                return self._finish_live_blocked_run(
                    run_id,
                    current_state,
                    valuation_prices,
                    data_requests_by_strategy,
                    valid_results,
                    target,
                    risk_decision,
                    safety_state,
                    live_blocks,
                    "before_order_generation",
                )

            order_generation_time = utc_now()
            order_prices = self._order_generation_prices(native_prices)
            orders = []
            for order_scope in order_targets:
                scoped_execution = build_execution_engine(
                    order_scope.execution_config,
                    instruments=self.config.universe.instruments,
                    currency_sleeves=self.config.portfolio.currency_sleeves,
                )
                account_orders = scoped_execution.propose_orders(
                    order_scope.state,
                    order_scope.target,
                    # Native prices, for the same reason as the signal preview path.
                    native_prices,
                    as_of=order_generation_time,
                    contribution_already_executed=self._contribution_already_executed(
                        order_generation_time,
                        order_scope.execution_config,
                        execution_sleeve=order_scope.execution_sleeve,
                        account_id=order_scope.account_id,
                    ),
                )
                orders.extend(
                    self._apply_native_order_prices(
                        self._stamp_orders_with_account_id(
                            account_orders,
                            order_scope.target.source_strategy_ids,
                            account_id=order_scope.account_id,
                            execution_sleeve=order_scope.execution_sleeve,
                        ),
                        order_prices,
                    )
                )
            approval_orders = self._orders_requiring_approval(orders)
            if orders and self.config.mode == RunMode.LIVE_APPROVAL:
                self._record_live_proposal_data_snapshot(
                    run_id,
                    orders,
                    data_requests_by_strategy,
                    order_prices,
                    data_quality_issues,
                    target,
                    risk_decision,
                )
            capacity_blocks = []
            if not safety_state.blocks_live_execution:
                approval_orders, capacity_blocks = self._partition_orders_by_capacity(
                    run_id,
                    approval_orders,
                    signal_run_id=None,
                )
            live_blocks = self._live_execution_blocks(
                run_id,
                approval_orders,
                data_quality_issues,
            )
            if (
                approval_orders
                and self.config.mode == RunMode.LIVE_APPROVAL
                and (safety_state.blocks_live_execution or live_blocks)
            ):
                if live_blocks:
                    self.safety.halt(
                        run_id,
                        "Live approval blocked by production hardening gate.",
                        source="system",
                    )
                return self._finish_live_blocked_run(
                    run_id,
                    current_state,
                    valuation_prices,
                    data_requests_by_strategy,
                    valid_results,
                    target,
                    risk_decision,
                    safety_state,
                    live_blocks,
                    "before_approval",
                )
            if orders and self.config.mode == RunMode.PAPER and safety_state.blocks_live_execution:
                self.safety.record_warning(
                    run_id,
                    self.config.mode.value,
                    safety_state,
                    "before_paper_execution",
                )
            if orders and self.config.mode == RunMode.PAPER and data_quality_issues:
                self._record_event(
                    run_id,
                    SystemEventType.STALE_DATA_WARNING,
                    {"issues": data_quality_issues, "mode": self.config.mode.value},
                )
            if approval_orders:
                approval_request, approval_decision, approval_message = (
                    self.approval_manager.request_approval(
                        run_id,
                        approval_orders,
                        risk_decision.violations,
                        target.source_strategy_ids,
                    )
                )
            else:
                approval_request = None
                approval_decision = None
                approval_message = None

            if approval_orders:
                if approval_request and approval_decision:
                    approval_payload = {
                        "request": approval_request.model_dump(mode="json"),
                        "decision": approval_decision.model_dump(mode="json"),
                        "message": approval_message,
                        "account_ids": sorted(
                            {order.account_id for order in approval_orders if order.account_id}
                        ),
                    }
                    if len(approval_payload["account_ids"]) == 1:
                        approval_payload["account_id"] = approval_payload["account_ids"][0]
                    self.state_store.save_approval(
                        run_id,
                        approval_request.approval_id,
                        approval_payload,
                    )
                    self.audit.log(
                        run_id,
                        "approval_decision",
                        approval_payload,
                    )
                    if approval_decision.status != "approved":
                        next_state = current_state
                        execution_results = []
                        self.state_store.save_system_event(
                            run_id,
                            SystemEventType.EXECUTION_SKIPPED,
                            {"approval_status": approval_decision.status},
                        )
                    elif self.config.mode == RunMode.LIVE_APPROVAL:
                        execution_results, next_state = self._execute_live_approval_orders(
                            run_id,
                            approval_orders,
                            approval_request.approval_id,
                            approval_decision,
                        )
                    else:
                        execution_results, next_state = self.execution.execute_orders(
                            current_state, approval_orders
                        )
                else:
                    if self.config.mode == RunMode.LIVE_APPROVAL:
                        raise ValueError("live_approval mode requires an approval decision")
                    execution_results, next_state = self.execution.execute_orders(
                        current_state, approval_orders
                    )
            else:
                execution_results = []
                next_state = current_state

            for order in approval_orders:
                order_payload = order.model_dump(mode="json")
                order_payload["approval_status"] = (
                    approval_decision.status if approval_decision else "not_required"
                )
                if not (
                    self.config.mode == RunMode.LIVE_APPROVAL
                    and self._effective_order_posture(order) == "dry_run"
                ):
                    self.state_store.save_order(run_id, order.order_id, order_payload)
            if self.config.mode != RunMode.LIVE_APPROVAL:
                self.state_store.save_portfolio_snapshot(run_id, next_state)
            strategy_book_snapshots = self._save_strategy_book_snapshots(
                run_id,
                valid_results,
                next_state,
                valuation_prices,
            )

            summary = RunOnceSummary(
                run_id=run_id,
                loaded_strategies=[strategy.config.id for strategy in self.registry.strategies],
                orders_created=len(approval_orders),
                total_value=next_state.total_value(valuation_prices),
                cash=next_state.cash,
            )
            self.audit.log(
                run_id,
                "run_once_completed",
                {
                    "loaded_strategies": summary.loaded_strategies,
                    "data_requests": data_requests_by_strategy,
                    "strategy_results": [
                        result.model_dump(mode="json") for result in valid_results
                    ],
                    "strategy_book_snapshots": strategy_book_snapshots,
                    "portfolio_target": target.model_dump(mode="json"),
                    "risk_decision": risk_decision.model_dump(mode="json"),
                    "approval_request": approval_request.model_dump(mode="json")
                    if approval_request
                    else None,
                    "approval_decision": approval_decision.model_dump(mode="json")
                    if approval_decision
                    else None,
                    "paper_orders": [order.model_dump(mode="json") for order in orders],
                    "approval_orders": [order.model_dump(mode="json") for order in approval_orders],
                    "execution_results": [
                        result.model_dump(mode="json") for result in execution_results
                    ],
                    "state_summary": next_state.summary(valuation_prices),
                },
            )
            self.state_store.save_system_event(
                run_id,
                "run_once_completed",
                {
                    "orders_created": summary.orders_created,
                    "total_value": summary.total_value,
                    "cash": summary.cash,
                },
            )
            return summary
        except Exception as exc:
            self.audit.log(
                run_id,
                "run_once_failed",
                {
                    "loaded_strategies": [
                        strategy.config.id for strategy in self.registry.strategies
                    ],
                    "data_requests": data_requests_by_strategy,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=8),
                },
            )
            self.state_store.save_system_event(
                run_id,
                "run_once_failed",
                {"error_type": type(exc).__name__, "error_message": str(exc)},
            )
            raise

    def _collect_strategy_results(
        self,
        run_id: str,
        current_state: PortfolioState,
        *,
        strategy_ids: set[str] | None = None,
        allowed_symbols: set[str] | None = None,
        data_requests_by_strategy: dict[str, Any] | None = None,
    ) -> tuple[
        list[TargetAllocationResult],
        dict[str, Any],
        list[dict[str, Any]],
        dict[str, float],
    ]:
        valid_results: list[TargetAllocationResult] = []
        # Caller-supplied accumulator so run_once can include partially collected
        # request audit data in its run_once_failed event when a strategy raises.
        if data_requests_by_strategy is None:
            data_requests_by_strategy = {}
        data_quality_issues: list[dict[str, Any]] = []
        prices = self._initial_prices()
        validator_strategy_ids = strategy_ids or self.registry.strategy_ids
        validator = SignalValidator.with_universe_boundaries(
            tradable_symbols=allowed_symbols or set(self.config.portfolio.allowed_symbols),
            research_only_symbols=set(self.config.universe.research_symbols),
            strategy_ids=validator_strategy_ids,
        )
        for loaded in self.registry.strategies:
            if strategy_ids and loaded.config.id not in strategy_ids:
                continue
            context = self._strategy_context(run_id, loaded, current_state)
            requests = loaded.plugin.build_data_requests(context)
            strategy_data_requests = {
                "prefetch": [request.model_dump(mode="json") for request in requests],
                "runtime": {"requests": [], "bundles": [], "errors": []},
            }
            data_requests_by_strategy[loaded.config.id] = strategy_data_requests
            data_bundle = self.datahub.get_data(requests)
            data_quality_issues.extend(collect_data_quality_issues(data_bundle))
            prices.update(prices_from_bundle(data_bundle))
            runtime = StrategyRuntime(self.datahub.get_data, context=context)
            try:
                raw_result = loaded.plugin.run_with_runtime(data_bundle, context, runtime)
            finally:
                strategy_data_requests["runtime"] = runtime.audit_payload()
            for runtime_bundle in runtime.bundles:
                data_quality_issues.extend(collect_data_quality_issues(runtime_bundle))
                prices.update(prices_from_bundle(runtime_bundle))
            result = normalize_strategy_result(
                raw_result,
                loaded.config.signal_to_allocation,
            )
            validation = validator.validate(result)
            strategy_run_payload = {
                "account_id": self._strategy_account_id(loaded.config.id),
                "signal_run_id": run_id,
                "result": result.model_dump(mode="json"),
                "validation": {"ok": validation.ok, "errors": validation.errors},
            }
            if isinstance(raw_result, StrategySignalResult):
                strategy_run_payload["source_signal"] = raw_result.model_dump(mode="json")
            self.state_store.save_strategy_run(
                run_id,
                loaded.config.id,
                strategy_run_payload,
            )
            if not validation.ok:
                raise ValueError(
                    f"Invalid strategy result for {loaded.config.id}: {validation.errors}"
                )
            valid_results.append(result)
        return valid_results, data_requests_by_strategy, data_quality_issues, prices

    def _strategy_context(
        self, run_id: str, loaded, current_state: PortfolioState
    ) -> StrategyContext:
        return StrategyContext(
            cycle_id=run_id,
            timestamp=utc_now(),
            run_mode=self.config.mode,
            strategy_id=loaded.config.id,
            portfolio_state=current_state.model_dump(mode="json"),
            config=loaded.config.config,
        )

    def _contribution_already_executed(
        self,
        as_of,
        execution_config: ExecutionConfig | None = None,
        *,
        execution_sleeve: str | None = None,
        account_id: str | None = None,
    ) -> bool:
        config = execution_config or self.config.execution
        if config.order_generation_mode != "buy_only_contribution":
            return False
        engine = build_execution_engine(
            config,
            instruments=self.config.universe.instruments,
            currency_sleeves=self.config.portfolio.currency_sleeves,
        )
        month_key = engine.contribution_month_key(as_of)
        if self.config.mode == RunMode.LIVE_APPROVAL:
            return self.state_store.monthly_live_contribution_order_exists(
                month_key,
                config.contribution.sleeve,
                execution_sleeve=execution_sleeve,
                account_id=account_id,
            )
        return self.state_store.monthly_contribution_order_exists(
            month_key,
            config.contribution.sleeve,
            execution_sleeve=execution_sleeve,
            account_id=account_id,
        )

    def _evaluate_dynamic_universe(
        self,
        run_id: str,
        current_state: PortfolioState,
    ) -> set[str]:
        requests_by_strategy: dict[str, list[CandidateInstrumentRequest]] = {}
        for loaded in self.registry.strategies:
            if not loaded.manifest.supports_dynamic_universe:
                continue
            requests = loaded.plugin.build_candidate_requests(
                self._strategy_context(run_id, loaded, current_state)
            )
            max_candidates = loaded.manifest.max_candidate_symbols
            if max_candidates is not None and len(requests) > max_candidates:
                raise ValueError(
                    f"{loaded.config.id} produced too many candidate symbols: "
                    f"{len(requests)} > {max_candidates}"
                )
            if requests:
                requests_by_strategy[loaded.config.id] = requests
        if not requests_by_strategy:
            return set()

        service = DynamicUniverseService(
            self.config.universe.policy,
            InstrumentResolver(self.config.universe.instruments),
        )
        approved_symbols: set[str] = set()
        evaluations_payload = {}
        for strategy_id, requests in requests_by_strategy.items():
            evaluations = service.evaluate(requests)
            evaluations_payload[strategy_id] = [
                evaluation.model_dump(mode="json") for evaluation in evaluations
            ]
            approved_symbols.update(
                evaluation.instrument.symbol
                for evaluation in evaluations
                if evaluation.tradable and evaluation.instrument is not None
            )
        self._record_event(
            run_id,
            SystemEventType.DYNAMIC_UNIVERSE_EVALUATION,
            {
                "strategies": evaluations_payload,
                "approved_symbols": sorted(approved_symbols),
                "persistent": False,
            },
        )
        return approved_symbols

    def _live_execution_blocks(
        self,
        run_id: str,
        orders: list[OrderIntent],
        data_quality_issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return LiveExecutionGateService(
            self.config,
            self.state_store,
            self.audit,
            now_fn=utc_now,
        ).evaluate(
            run_id,
            orders,
            data_quality_issues,
        )

    def _prices_from_bundle(self, data_bundle) -> dict[str, float]:
        return prices_from_bundle(data_bundle)

    def _data_quality_issues(self, data_bundle) -> list[dict[str, Any]]:
        return collect_data_quality_issues(data_bundle)

    def _order_generation_prices(self, prices: dict[str, float]) -> dict[str, float]:
        if self.config.mode != RunMode.LIVE_APPROVAL:
            return prices
        latest = self.state_store.load_latest_broker_account_snapshot()
        if latest is None:
            return prices
        broker_prices = _broker_snapshot_prices(latest["payload"])
        if not broker_prices:
            return prices
        if self.config.execution.broker_validation.require_quote_validation:
            return {**prices, **broker_prices}
        return {**broker_prices, **prices}

    def _enrich_sleeve_prices(self, prices: dict[str, float]) -> dict[str, float]:
        """Merge broker snapshot prices into prices for sleeve capacity calculation.

        This is a lightweight enrichment that only adds position prices, without
        triggering full order-generation price validation.
        """
        latest = self.state_store.load_latest_broker_account_snapshot()
        if latest is None:
            return prices
        broker_prices = _broker_snapshot_prices(latest["payload"])
        if not broker_prices:
            return prices
        return {**broker_prices, **prices}

    def _currency_for_symbol(self, symbol: str) -> str:
        """Return the trading currency for a symbol based on instrument config."""
        for instrument in self.config.universe.instruments:
            if instrument.symbol == symbol:
                return instrument.currency
        return self.config.portfolio.base_currency

    def _apply_fx_prices(self, run_id: str, prices: dict[str, float]) -> dict[str, float]:
        """Convert USD prices to KRW using FX rates."""
        converted_prices = dict(prices)
        if self.config.portfolio.base_currency != "KRW":
            return converted_prices

        usd_symbols = [
            symbol
            for symbol in prices
            if not symbol.startswith("CASH_") and self._currency_for_symbol(symbol) == "USD"
        ]
        if not usd_symbols:
            return converted_prices

        def handle_unavailable_rate(reason: str, exc: Exception) -> dict[str, float]:
            payload = {
                "reason": reason,
                "error_type": type(exc).__name__,
                "error_message": str(exc).strip("'"),
                "symbols": sorted(usd_symbols),
            }
            if self.config.mode == RunMode.LIVE_APPROVAL:
                self._record_event(run_id, "fx_conversion_halt", payload)
                raise RuntimeError(
                    f"FX rate unavailable for USD->KRW conversion: {reason}"
                ) from exc
            self._record_event(run_id, "fx_conversion_warning", payload)
            return converted_prices

        try:
            fx_result = self.fx_service.refresh_from_config()
        except Exception as exc:
            return handle_unavailable_rate("refresh_failed", exc)

        if "USD/KRW" not in fx_result.rates:
            return handle_unavailable_rate("missing_rate", KeyError("USD/KRW"))
        try:
            usd_to_krw = float(fx_result.rates["USD/KRW"])
        except Exception as exc:
            return handle_unavailable_rate("invalid_rate", exc)
        if usd_to_krw <= 0:
            return handle_unavailable_rate(
                "non_positive_rate",
                ValueError("USD/KRW must be positive"),
            )

        for symbol in usd_symbols:
            converted_prices[symbol] = prices[symbol] * usd_to_krw

        return converted_prices

    def _initial_prices(self) -> dict[str, float]:
        cash_symbols = self._configured_cash_symbols()
        return {symbol: 1.0 for symbol in cash_symbols} or {"CASH": 1.0}

    def _configured_cash_symbols(self) -> list[str]:
        if self.config.portfolio.allocation_mode == "currency_sleeves":
            return [
                sleeve.cash_symbol
                for _, sleeve in sorted(self.config.portfolio.currency_sleeves.items())
            ]
        return [
            symbol for symbol in self.config.portfolio.allowed_symbols if is_cash_symbol(symbol)
        ]

    def _target_with_configured_cash(
        self,
        target: PortfolioTarget,
        valid_results: list[TargetAllocationResult],
    ) -> PortfolioTarget:
        if valid_results or self.config.portfolio.allocation_mode != "currency_sleeves":
            return target
        sleeves = {
            sleeve_name: {sleeve.cash_symbol: 1.0}
            for sleeve_name, sleeve in self.config.portfolio.currency_sleeves.items()
        }
        if not sleeves:
            return target
        return PortfolioTarget(
            timestamp=target.timestamp,
            allocations={},
            allocation_sleeves=sleeves,
            source_strategy_ids=target.source_strategy_ids,
        )

    def _record_event(
        self,
        run_id: str,
        event_type: SystemEventType | str,
        payload: dict[str, Any],
    ) -> None:
        save_audited_system_event(self.state_store, self.audit, run_id, event_type, payload)

    def _commit_signal_package(
        self,
        signal_run_id: str,
        funding_requests: list[ContributionFundingRequest],
        budget_requests: list[ContributionBudgetRequest],
        build_payload: Callable[[list[Any], list[Any]], dict[str, Any]],
        lineage_events: Sequence[Mapping[str, Any]],
        *,
        source_request_id: str | None = None,
        source_phase: str | None = None,
    ) -> tuple[list[Any], list[Any], dict[str, Any]]:
        """Commit the requests, their heads, the package and the lineage together.

        These only mean anything as a set. A request published on its own is
        live -- it is at the head of its workflow and can be claimed -- while
        the package that would have put a card in front of the operator does
        not exist, so nobody can act on it and the next run mints a new
        request id instead of adopting it. A package written on its own would
        advertise a decision with no workflow behind it. One transaction is
        the only arrangement where neither can happen.

        Losing the head CAS still drops just the one request rather than the
        run: the conflict names the head slot that was taken, so the request
        that wanted it is dropped, the package is rebuilt without it and the
        batch is retried. Every pass drops at least one request, which is what
        bounds the loop.

        ``source_request_id``/``source_phase`` are this run's own child
        lineage (see ``run_signal``), and are what let a request published
        here supersede a head that is still claimed: they are the legitimate
        successor declaration ``plan_contribution_request`` requires for
        that. A run with no source (not a child of any claimed transition)
        passes ``None``, exactly like an independent publish must.
        """
        plans: list[tuple[str, Any, dict[str, Any]]] = []
        for phase, requests in (("funding", funding_requests), ("budget", budget_requests)):
            for request in requests:
                plan = plan_contribution_request(
                    self.state_store,
                    request.model_dump(mode="json"),
                    phase=phase,
                    successor_of_request_id=source_request_id,
                    successor_of_phase=source_phase,
                )
                if plan["refusal"] is not None:
                    self._audit_request_conflict(signal_run_id, phase, plan, plan["refusal"])
                    continue
                plans.append((phase, request, plan))

        for _ in range(len(plans) + 1):
            live_funding = [request for phase, request, _ in plans if phase == "funding"]
            live_budget = [request for phase, request, _ in plans if phase == "budget"]
            payload = build_payload(live_funding, live_budget)
            outcome = self.state_store.save_signal_package(
                signal_run_id,
                payload,
                together_with=[
                    *(event for _, _, plan in plans for event in plan["events"]),
                    *lineage_events,
                ],
                require_duplicate_keys=[
                    key for _, _, plan in plans for key in plan["extra_require_keys"]
                ],
                forbid_duplicate_keys=[
                    key
                    for _, _, plan in plans
                    for key in (plan["head_key"], *plan["extra_forbid_keys"])
                ],
            )
            if outcome["committed"]:
                for phase, _, plan in plans:
                    # The planned payload, not the caller's: it carries the
                    # funding_workflow_id, without which the audit trail
                    # cannot say which workflow this request belonged to.
                    self.audit.log(
                        signal_run_id, f"contribution_{phase}_request", plan["payload"]
                    )
                return live_funding, live_budget, payload
            blocked = set(outcome.get("conflicting_keys") or ())
            survivors = []
            lost = False
            for item in plans:
                plan_keys = {
                    item[2]["head_key"],
                    *item[2]["extra_require_keys"],
                    *item[2]["extra_forbid_keys"],
                }
                if plan_keys & blocked:
                    self._audit_request_conflict(
                        signal_run_id, item[0], item[2], str(outcome["conflict"])
                    )
                    lost = True
                    continue
                survivors.append(item)
            if not lost:
                # Nothing in this batch explains the refusal, so retrying it
                # would loop on the same answer. Fail loudly with nothing
                # written rather than publish a package we cannot back.
                raise ValueError(
                    f"signal package {signal_run_id} was refused: {outcome['conflict']} "
                    f"on {sorted(blocked)}"
                )
            plans = survivors
        raise ValueError(f"signal package {signal_run_id} could not be committed")

    def _audit_request_conflict(
        self,
        signal_run_id: str,
        phase: str,
        plan: Mapping[str, Any],
        conflict: str,
    ) -> None:
        self.audit.log(
            signal_run_id,
            "funding_workflow_head_conflict",
            {
                "signal_run_id": signal_run_id,
                "workflow_id": plan["workflow_id"],
                "request_id": (plan["payload"] or {}).get("request_id"),
                "phase": phase,
                "conflict": conflict,
            },
        )

    def _record_run_provenance(
        self,
        run_id: str,
        run_kind: str,
        *,
        signal_run_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "run_kind": run_kind,
            "signal_run_id": signal_run_id,
            "deployment_commit": self.deployment_identity.commit,
            "deployment_source_fingerprint": self.deployment_identity.source_fingerprint,
            "deployment_dirty": self.deployment_identity.dirty,
            "config_path": self.config_identity.path if self.config_identity else None,
            "config_fingerprint": (
                self.config_identity.fingerprint if self.config_identity else None
            ),
            "config_runtime_fingerprint": (
                self.config_identity.runtime_fingerprint if self.config_identity else None
            ),
        }
        self._record_event(run_id, SystemEventType.RUN_PROVENANCE, payload)

    def _load_run_portfolio_state(
        self,
        run_id: str,
        *,
        account_ids: list[str] | None = None,
        use_cached_broker_snapshots: bool = False,
    ) -> PortfolioState:
        if self.config.mode != RunMode.LIVE_APPROVAL:
            return self.state_store.load_latest_portfolio_state()
        if not self.registry.strategies and self.state_store.has_portfolio_snapshot():
            return self.state_store.load_latest_portfolio_state()
        live_account_ids = account_ids or self._live_account_ids()
        if use_cached_broker_snapshots and live_account_ids:
            return self._load_cached_broker_portfolio_state(run_id, live_account_ids)
        if len(live_account_ids) > 1:
            return self._load_multi_account_live_portfolio_state(run_id, live_account_ids)
        baseline_account_id = self._single_live_account_id()
        if self.config.kis.enabled and self.config.kis.provider != "kis":
            if self.state_store.has_portfolio_snapshot():
                return self.state_store.load_latest_portfolio_state()
        try:
            readonly_service = build_broker_readonly_service(
                self.config,
                self.state_store,
                self.audit,
                account_id=baseline_account_id,
            )
            snapshot = readonly_service.fetch_and_store_snapshot(
                self.config.portfolio.allowed_symbols
            )
            ledger_state = (
                self.state_store.load_latest_account_portfolio_state(baseline_account_id)
                if baseline_account_id
                else None
            )
            state_kwargs = {
                "allowed_symbols": self.config.portfolio.allowed_symbols,
                "universe": self.config.universe,
            }
            if ledger_state is not None:
                state_kwargs["ledger_state"] = ledger_state
            elif str(snapshot.account.source).startswith("toss_"):
                state_kwargs["allow_proxy_cash"] = False
            state = portfolio_state_from_broker_account(
                snapshot.account.model_dump(mode="json"),
                **state_kwargs,
            )
        except (RuntimeError, TimeoutError, ValueError) as exc:
            self._record_event(
                run_id,
                SystemEventType.BROKER_BASELINE_REQUIRED,
                {
                    "mode": self.config.mode.value,
                    "reason": "live_approval could not refresh broker snapshot before run_once",
                    "error": str(exc),
                },
            )
            raise ValueError(
                f"live_approval could not refresh broker snapshot before run_once: {exc}"
            ) from exc
        self._record_event(
            run_id,
            SystemEventType.BROKER_SNAPSHOT_ADOPTED,
            {
                "reason": "live_approval refreshed broker snapshot before run_once",
                "account_id": snapshot.account.account_id,
                "cash": state.cash,
                "cash_by_currency": state.cash_by_currency,
                "positions": state.positions,
                "source": snapshot.account.source,
                "fetched_at": snapshot.account.fetched_at.isoformat(),
            },
        )
        self._auto_reconcile_live_baseline(run_id)
        return state

    def _load_cached_broker_portfolio_state(
        self,
        run_id: str,
        account_ids: list[str],
    ) -> PortfolioState:
        states: list[PortfolioState] = []
        adopted: list[dict[str, Any]] = []
        for account_id in account_ids:
            row = latest_snapshot_for_account(self.state_store, account_id)
            if row is None:
                raise ValueError(f"missing broker snapshot for required account: {account_id}")
            payload = row.get("payload") or {}
            account = payload.get("account") or {}
            ledger_state = self.state_store.load_latest_account_portfolio_state(account_id)
            state = portfolio_state_from_broker_account(
                account,
                allowed_symbols=self.config.portfolio.allowed_symbols,
                universe=self.config.universe,
                ledger_state=ledger_state,
                allow_proxy_cash=not str(account.get("source") or "").startswith("toss_"),
            )
            states.append(state)
            adopted.append(
                {
                    "account_id": account_id,
                    "snapshot_id": row.get("id"),
                    "created_at": row.get("created_at"),
                }
            )
        merged = _merge_portfolio_states(states)
        self._record_event(
            run_id,
            SystemEventType.BROKER_SNAPSHOT_ADOPTED,
            {"reason": "account-scoped signal preflight", "accounts": adopted},
        )
        reconciliation = BrokerReconciliationService(
            self.config.reconciliation,
            self.state_store,
            self.audit,
            account_ids=account_ids,
        ).reconcile_latest(run_id=run_id)
        if not reconciliation.passed:
            raise ValueError(
                "account-scoped broker baseline reconciliation failed: " + ", ".join(account_ids)
            )
        return merged

    def _load_multi_account_live_portfolio_state(
        self,
        run_id: str,
        account_ids: list[str],
    ) -> PortfolioState:
        states = []
        snapshot_events = []
        try:
            for account_id in account_ids:
                service = build_broker_readonly_service(
                    self.config,
                    self.state_store,
                    self.audit,
                    account_id=account_id,
                )
                snapshot = service.fetch_and_store_snapshot(self.config.portfolio.allowed_symbols)
                state = portfolio_state_from_broker_account(
                    snapshot.account.model_dump(mode="json"),
                    allowed_symbols=self.config.portfolio.allowed_symbols,
                    universe=self.config.universe,
                    ledger_state=self.state_store.load_latest_account_portfolio_state(
                        account_id
                    ),
                    allow_proxy_cash=not str(snapshot.account.source).startswith("toss_"),
                )
                states.append(state)
                snapshot_events.append(
                    {
                        "account_id": account_id,
                        "broker_account_id": snapshot.account.account_id,
                        "cash": state.cash,
                        "cash_by_currency": state.cash_by_currency,
                        "positions": state.positions,
                        "source": snapshot.account.source,
                        "fetched_at": snapshot.account.fetched_at.isoformat(),
                    }
                )
        except (RuntimeError, TimeoutError, ValueError, UnsupportedBrokerOperation) as exc:
            self._record_event(
                run_id,
                SystemEventType.BROKER_BASELINE_REQUIRED,
                {
                    "mode": self.config.mode.value,
                    "reason": (
                        "live_approval could not refresh multi-account broker snapshots "
                        "before run_once"
                    ),
                    "account_ids": account_ids,
                    "error": str(exc),
                },
            )
            raise ValueError(
                "live_approval could not refresh multi-account broker snapshots before "
                f"run_once: {exc}"
            ) from exc
        state = _merge_portfolio_states(states)
        self._record_event(
            run_id,
            SystemEventType.BROKER_SNAPSHOT_ADOPTED,
            {
                "reason": "live_approval refreshed multi-account broker snapshots before run_once",
                "accounts": snapshot_events,
                "cash": state.cash,
                "cash_by_currency": state.cash_by_currency,
                "positions": state.positions,
            },
        )
        self._auto_reconcile_live_baseline(run_id)
        return state

    def _auto_reconcile_live_baseline(self, run_id: str) -> None:
        if not self.config.execution.require_reconciliation_pass:
            return
        if self.state_store.load_latest_broker_account_snapshot() is None:
            return
        BrokerReconciliationService(
            self.config.reconciliation,
            self.state_store,
            self.audit,
            account_ids=self._live_account_ids(),
        ).reconcile_latest(run_id=run_id)

    def _broker_snapshot_refs(
        self,
        account_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        if self.config.mode != RunMode.LIVE_APPROVAL:
            return []
        expected_account_ids = set(account_ids or self._live_account_ids())
        if not expected_account_ids:
            return []
        refs_by_account: dict[str, dict[str, Any]] = {}
        for row in self.state_store.list_broker_account_snapshots(
            limit=max(10, len(expected_account_ids) * 5)
        ):
            payload = row["payload"]
            account_id = payload.get("account_id") or row.get("account_id")
            if account_id not in expected_account_ids or account_id in refs_by_account:
                continue
            refs_by_account[account_id] = {
                "id": row["id"],
                "run_id": row["run_id"],
                "account_id": account_id,
                "broker_account_id": payload.get("broker_account_id")
                or (payload.get("account") or {}).get("account_id")
                or row.get("account_id"),
                "created_at": row["created_at"],
                "fetched_at": (payload.get("account") or {}).get("fetched_at"),
            }
        return [
            refs_by_account[account_id]
            for account_id in sorted(expected_account_ids)
            if account_id in refs_by_account
        ]

    def _verify_reused_envelope(
        self,
        envelope: PendingApprovalEnvelope,
        signal_run_id: str,
        source_strategy_ids: list[str],
        group_orders: list[OrderIntent],
    ) -> None:
        """Refuse an envelope that is not the one this group wrote.

        The dispatch group id is the whole scope rather than a digest, so two
        unrelated groups cannot collide by accident. This checks the case that
        remains: a key written by something other than this code path, a
        record altered by hand, or two in-memory groups that canonicalize to
        the same durable id. Adopting such an envelope would send the
        operator a card for orders they were never shown and bind the approval
        buttons to it, so stop instead of continuing on a guess.

        Strategies are compared as sets -- already canonicalized (sorted,
        deduplicated) before they name a group, so set equality is exactly
        "same group", and strategy membership does not change with capacity.
        ``group_orders`` here is the group's full, capacity-independent
        membership (the dispatch manifest's), not whatever this particular
        call's capacity check happened to accept -- an envelope created while
        some of the group's orders were capacity-blocked legitimately holds
        fewer orders (and fewer account ids, if blocking emptied one account
        entirely) than that full membership. So orders and account ids are
        checked as subset, not equality: every order and account id the
        envelope names must belong to the group, but the group may have more
        than the envelope happened to carry.
        """
        expected_strategies = sorted(set(source_strategy_ids))
        recorded_strategies = sorted(set(envelope.source_strategy_ids))
        expected_account_ids = {str(order.account_id) for order in group_orders if order.account_id}
        recorded_account_ids = set(envelope.account_ids)
        expected_order_ids = {order.order_id for order in group_orders}
        recorded_order_ids = {str(order.get("order_id")) for order in envelope.orders}
        if (
            envelope.signal_run_id == signal_run_id
            and recorded_strategies == expected_strategies
            and recorded_account_ids <= expected_account_ids
            and recorded_order_ids
            and recorded_order_ids <= expected_order_ids
        ):
            return
        raise ValueError(
            "Stored approval envelope does not match the group it was loaded for: "
            f"expected signal_run_id={signal_run_id} strategies={expected_strategies} "
            f"account_ids<={sorted(expected_account_ids)} order_ids<={sorted(expected_order_ids)}, "
            f"found signal_run_id={envelope.signal_run_id} strategies={recorded_strategies} "
            f"account_ids={sorted(recorded_account_ids)} order_ids={sorted(recorded_order_ids)}"
        )

    @staticmethod
    def _verify_group_disposition(
        group_id: str,
        manifest_order_ids: set[str],
        envelope_order_ids: set[str],
        blocked_order_ids: set[str],
    ) -> None:
        """Every manifest order must have exactly one accounted disposition.

        The dispatch manifest freezes a group's order roster once; from then
        on, each of those orders must end up either in an approval envelope
        or in a durable capacity-block record -- never both, never neither.
        ``_verify_reused_envelope`` only proves the envelope carries no
        *unknown* order (a subset check); it says nothing about whether the
        orders missing from it were ever durably recorded as blocked, so a
        manifest order could otherwise vanish -- present in neither the
        envelope nor any blocked disposition -- without either check
        noticing. This is what closes that gap: it is called with the
        group's actual current envelope and blocked-disposition order ids,
        never recomputed from live capacity, so it verifies durable state
        as it stands rather than re-deriving what it should be.
        """
        foreign_envelope = envelope_order_ids - manifest_order_ids
        if foreign_envelope:
            raise ValueError(
                f"Group {group_id} envelope names order(s) not in the manifest: "
                f"{sorted(foreign_envelope)}"
            )
        foreign_blocked = blocked_order_ids - manifest_order_ids
        if foreign_blocked:
            raise ValueError(
                f"Group {group_id} blocked disposition names order(s) not in the "
                f"manifest: {sorted(foreign_blocked)}"
            )
        overlap = envelope_order_ids & blocked_order_ids
        if overlap:
            raise ValueError(
                f"Group {group_id} has order(s) marked both approved and capacity-"
                f"blocked: {sorted(overlap)}"
            )
        unaccounted = manifest_order_ids - envelope_order_ids - blocked_order_ids
        if unaccounted:
            raise ValueError(
                f"Group {group_id} has manifest order(s) with no disposition -- "
                f"neither approved nor durably capacity-blocked: {sorted(unaccounted)}"
            )

    def _approval_order_groups(
        self,
        orders: list[OrderIntent],
        package: dict[str, Any],
    ) -> list[tuple[list[str], list[OrderIntent]]]:
        fallback_source_strategy_ids = package.get("portfolio_target", {}).get(
            "source_strategy_ids", []
        )
        groups: dict[tuple[str, ...], list[OrderIntent]] = {}
        for order in orders:
            source_strategy_ids = order.metadata.get("source_strategy_ids")
            if not source_strategy_ids:
                source_strategy_ids = fallback_source_strategy_ids
            # Canonicalized the same way dispatch_group_id canonicalizes its
            # scope (sorted, deduplicated): that id is what durably names this
            # group, so two orders naming the same strategies in a different
            # order -- or with a repeat -- must land in the same in-memory
            # group here, or they would split into two groups that then
            # collide on one durable id, and the second write would be
            # evaluated as a resume of the first group's envelope.
            key = tuple(
                sorted({str(strategy_id) for strategy_id in source_strategy_ids if strategy_id})
            )
            if not key:
                key = ("unknown",)
            groups.setdefault(key, []).append(order)
        # Sorted, not insertion-ordered. A resumed dispatch has to rebuild the
        # same groups the interrupted one built, and dict insertion order here
        # follows the order of `orders` -- which nothing pins. Sorting makes
        # the grouping a function of the package alone.
        return [(list(key), groups[key]) for key in sorted(groups)]

    def _validate_signal_package_for_approval(self, package: dict[str, Any]) -> None:
        if self.config.mode != RunMode.LIVE_APPROVAL:
            return
        generated_at = _parse_signal_ref_time(str(package.get("generated_at") or ""))
        signal_age_seconds = (utc_now() - generated_at).total_seconds()
        if signal_age_seconds > self.config.approval.signal_max_age_seconds:
            raise ValueError(
                "Signal package expired signal package: "
                f"age_seconds={signal_age_seconds:.0f} "
                f"max_age_seconds={self.config.approval.signal_max_age_seconds}"
            )
        signal_account_mappings = package.get("strategy_account_mappings")
        if signal_account_mappings is not None:
            current_mappings = self._strategy_account_mappings()
            if signal_account_mappings != current_mappings:
                raise ValueError(
                    "Signal package account mapping mismatch: "
                    f"signal={signal_account_mappings} current={current_mappings}"
                )
        signal_contract_fingerprint = package.get("config_signal_contract_fingerprint")
        if signal_contract_fingerprint:
            current_contract_fingerprint = _signal_contract_fingerprint(self.config)
            if signal_contract_fingerprint != current_contract_fingerprint:
                raise ValueError(
                    "Signal package config runtime mismatch: "
                    f"signal_fingerprint={str(signal_contract_fingerprint)[:8]} "
                    f"current_fingerprint={current_contract_fingerprint[:8]}; "
                    "run 'maestro profile-diff --left <signal-config> "
                    "--right <approval-config>' to inspect"
                )
        else:
            signal_fingerprint = package.get("config_runtime_fingerprint")
            current_fingerprint = (
                self.config_identity.runtime_fingerprint
                if self.config_identity is not None
                else None
            )
            if (
                signal_fingerprint
                and current_fingerprint
                and signal_fingerprint != current_fingerprint
            ):
                raise ValueError("Signal package config runtime mismatch")
        if not package.get("datahub_evidence"):
            raise ValueError("Signal package missing DataHub evidence")
        expected_account_ids = set(
            str(account_id)
            for account_id in package.get("required_account_ids") or self._live_account_ids()
        )
        if not expected_account_ids:
            return
        refs = package.get("broker_snapshot_refs") or []
        actual_account_ids = {str(ref.get("account_id")) for ref in refs}
        missing_account_ids = sorted(expected_account_ids - actual_account_ids)
        if missing_account_ids:
            raise ValueError(
                "Signal package missing broker_snapshot_refs for account_id: "
                + ", ".join(missing_account_ids)
            )
        now = utc_now()
        for ref in refs:
            created_at = _parse_signal_ref_time(str(ref.get("created_at") or ""))
            age_seconds = (now - created_at).total_seconds()
            if age_seconds > self.config.reconciliation.signal_snapshot_max_age_seconds:
                raise ValueError(
                    "Signal package stale broker snapshot: "
                    f"account_id={ref.get('account_id')} "
                    f"age_seconds={age_seconds:.0f} "
                    "max_age_seconds="
                    f"{self.config.reconciliation.signal_snapshot_max_age_seconds}"
                )
        self._validate_signal_broker_baseline(refs)

    def _validate_signal_broker_baseline(self, refs: list[dict[str, Any]]) -> None:
        snapshot_rows = self.state_store.list_broker_account_snapshots(limit=1000)
        rows_by_id = {int(row["id"]): row for row in snapshot_rows}
        latest_by_account: dict[str, dict[str, Any]] = {}
        for row in snapshot_rows:
            payload = row["payload"]
            account_id = payload.get("account_id") or row.get("account_id")
            if account_id and account_id not in latest_by_account:
                latest_by_account[str(account_id)] = row
        for ref in refs:
            account_id = str(ref.get("account_id") or "")
            baseline = rows_by_id.get(int(ref.get("id") or 0))
            latest = latest_by_account.get(account_id)
            if baseline is None or latest is None:
                raise ValueError(f"Signal package broker snapshot changed: account_id={account_id}")
            if int(latest["id"]) == int(baseline["id"]):
                continue
            difference = _broker_snapshot_material_difference(
                baseline["payload"],
                latest["payload"],
                cash_tolerance=self.config.reconciliation.cash_tolerance,
                position_tolerance=self.config.reconciliation.position_quantity_tolerance,
            )
            if difference is not None:
                raise ValueError(
                    "Signal package broker snapshot changed: "
                    f"account_id={account_id} reason={difference['reason']}"
                )

    def _datahub_evidence(
        self,
        data_requests_by_strategy: dict[str, Any],
        data_quality_issues: list[dict[str, Any]],
        prices: dict[str, float],
    ) -> dict[str, Any]:
        return {
            "generated_at": utc_now().isoformat(),
            "strategies": data_requests_by_strategy,
            "issue_count": len(data_quality_issues),
            "issues": data_quality_issues,
            "price_symbols": sorted(prices),
        }

    def _validate_signal_approval_gates(
        self,
        run_id: str,
        orders: list[OrderIntent],
        package: dict[str, Any],
    ) -> None:
        if self.config.mode != RunMode.LIVE_APPROVAL:
            return
        safety_state = self.safety.current_state()
        if safety_state.blocks_live_execution:
            self.safety.record_blocked_execution(
                run_id,
                self.config.mode.value,
                safety_state,
                "approve_signal",
            )
            raise ValueError(
                "Signal approval safety state blocks live execution: "
                f"state={safety_state.state.value}"
            )
        live_blocks = self._live_execution_blocks(
            run_id,
            orders,
            list(package.get("data_quality_issues") or []),
        )
        if live_blocks:
            self.safety.halt(
                run_id,
                "Signal approval blocked by production hardening gate.",
                source="system",
            )
            reasons = ", ".join(str(block.get("reason")) for block in live_blocks)
            raise ValueError(f"Signal approval blocked by live execution gate: {reasons}")

    def _validate_signal_approval_preconditions(
        self,
        run_id: str,
        package: dict[str, Any],
    ) -> None:
        safety_state = self.safety.current_state()
        if safety_state.blocks_live_execution:
            self.safety.record_blocked_execution(
                run_id,
                self.config.mode.value,
                safety_state,
                "approve_signal",
            )
            raise ValueError(
                "Signal approval safety state blocks live execution: "
                f"state={safety_state.state.value}"
            )
        data_quality_issues = list(package.get("data_quality_issues") or [])
        if data_quality_issues:
            payload = {"issues": data_quality_issues, "mode": self.config.mode.value}
            self._record_event(run_id, SystemEventType.STALE_DATA_HALT, payload)
            self.safety.halt(
                run_id,
                "Signal approval blocked by production hardening gate.",
                source="system",
            )
            raise ValueError("Signal approval blocked by live execution gate: stale_data")

    def _partition_orders_by_capacity_pure(
        self,
        orders: list[OrderIntent],
    ) -> tuple[list[OrderIntent], list[OrderCapacityBlock]]:
        """Decide capacity with no durable write and no notification.

        A caller that must commit the block disposition atomically (the
        dispatch manifest loop, via ``insert_or_load_dispatch_group_capacity_block``)
        partitions here first, so nothing durable or operator-visible exists
        until that atomic commit lands. Doing the evaluation and the
        side effects together -- the old shape of
        ``_partition_orders_by_capacity`` below -- left a crash window where
        a recovery-visible record could be written and a notification sent
        for a block that the authoritative group disposition never
        committed, letting the order execute through both the recovered
        normal-approval path and an operator retry.
        """
        if self.config.mode != RunMode.LIVE_APPROVAL or not orders:
            return orders, []
        armed = [order for order in orders if self._effective_order_posture(order) == "armed"]
        if not armed:
            return orders, []
        service = OrderCapacityService(
            self.order_capacity_lookup or self._lookup_order_capacity,
            quantity_step=self._order_quantity_step,
        )
        accepted_armed, blocked = service.partition(armed)
        accepted_ids = {order.order_id for order in accepted_armed}
        accepted = [
            order
            for order in orders
            if self._effective_order_posture(order) != "armed" or order.order_id in accepted_ids
        ]
        return accepted, blocked

    def _partition_orders_by_capacity(
        self,
        run_id: str,
        orders: list[OrderIntent],
        *,
        signal_run_id: str | None,
        package: dict[str, Any] | None = None,
    ) -> tuple[list[OrderIntent], list[OrderCapacityBlock]]:
        accepted, blocked = self._partition_orders_by_capacity_pure(orders)
        for item in blocked:
            payload = item.model_dump(mode="json")
            payload.update(
                {
                    "blocked_order_id": item.order.order_id,
                    "signal_run_id": signal_run_id,
                    "status": "pending",
                    "config_signal_contract_fingerprint": (package or {}).get(
                        "config_signal_contract_fingerprint"
                    ),
                    "config_runtime_fingerprint": (package or {}).get("config_runtime_fingerprint"),
                }
            )
            self._record_event(run_id, "live_order_capacity_blocked", payload)
            self._notify_capacity_block(run_id, item)
        return accepted, blocked

    def _order_quantity_step(self, order: OrderIntent) -> float:
        """Tradable step for the quantity quoted in a capacity block alert.

        Unknown instruments fall back to whole units, the same assumption the
        Telegram retry review makes, so the two quote the same maximum.
        """
        instrument = self.config.universe.get(order.symbol)
        return float(instrument.quantity_step) if instrument is not None else 1.0

    def _lookup_order_capacity(self, order: OrderIntent) -> BrokerBuyingPower:
        if self.live_order_client is not None and hasattr(
            self.live_order_client, "get_buying_power"
        ):
            currency = resolve_order_currency(self.config, order)
            return check_capacity_currency(
                self.live_order_client.get_buying_power(
                    order.symbol,
                    order.price,
                    currency=currency.value,
                ),
                currency,
            )

        account_id = order.account_id
        service = self._order_capacity_clients.get(account_id)
        if service is None:
            service = build_broker_readonly_service(
                self.config,
                self.state_store,
                self.audit,
                account_id=account_id,
            )
            while hasattr(service, "inner"):
                service = service.inner
            self._order_capacity_clients[account_id] = service
        client = getattr(service, "client", None)
        if client is None:
            # A KIS service spanning several broker products keeps no single
            # client. Ask the one that owns this order's product: the merged
            # snapshot has only per-currency cash, and dropping the per-symbol
            # quantity cap lets the post-sell partition approve a size the
            # product's own pre-submit check then rejects.
            return self._capacity_from_product_client(service, order)
        account = self.account_router.account(account_id)
        broker = account.broker if account is not None else "kis"
        return get_order_buying_power(client, self.config, broker, order)

    def _capacity_from_product_client(
        self,
        service: Any,
        order: OrderIntent,
    ) -> BrokerBuyingPower:
        """Price the order through the read-only client that owns its product."""
        currency = resolve_order_currency(self.config, order)
        per_product = getattr(service, "get_buying_power_for_product", None)
        if per_product is not None:
            instrument = self.config.universe.get(order.symbol)
            symbol = (
                instrument.symbol_for_broker("kis") if instrument is not None else order.symbol
            )
            return check_capacity_currency(
                per_product(
                    symbol,
                    order.price,
                    currency=currency.value,
                    broker_product=order.broker_product,
                ),
                currency,
            )
        return self._capacity_from_composite_snapshot(service, order)

    def _capacity_from_composite_snapshot(
        self,
        service: Any,
        order: OrderIntent,
    ) -> BrokerBuyingPower:
        """Fall back to per-currency cash from a merged snapshot.

        Carries no quantity cap, so it is a last resort for a service that
        cannot price a single product.
        """
        currency = resolve_order_currency(self.config, order)
        snapshot = service.fetch_and_store_snapshot([order.symbol])
        by_currency = dict(getattr(snapshot.account, "buying_power_by_currency", {}) or {})
        if currency.value not in by_currency:
            raise BuyingPowerCurrencyUnavailable(currency.value, sorted(by_currency))
        return BrokerBuyingPower(
            symbol=order.symbol,
            order_price=order.price,
            cash_buying_power=float(by_currency[currency.value]),
            currency=currency.value,
            source="broker_composite_snapshot",
        )

    def _deliver_capacity_block_notification(
        self, run_id: str, blocked_key: str, order_id: str
    ) -> None:
        """Send this durably blocked order's operator notification at most once.

        Runs on every visit to a group with a durable disposition, not only
        the one that just committed it, so a crash between the disposition
        committing and this firing loses only the notification -- a resume
        retries it here instead of re-consulting capacity. The order's own
        ``live_order_capacity_blocked`` record (written atomically with the
        group disposition) is the source for what to send, since a later
        resume that reaches this without ever having recomputed the block
        itself still needs the original reason and figures.
        """
        notified_key = f"{blocked_key}:notified:{order_id}"
        if self.state_store.load_system_event_payload_by_duplicate_key(notified_key) is not None:
            return
        live_payload = self.state_store.load_system_event_payload_by_duplicate_key(
            f"{blocked_key}:live:{order_id}"
        )
        if live_payload is None:
            raise ValueError(
                f"Order {order_id} is durably capacity-blocked under {blocked_key} but has "
                "no live_order_capacity_blocked record to notify from"
            )
        sent = self._notify_capacity_block(run_id, OrderCapacityBlock.model_validate(live_payload))
        if not sent:
            # Every chat failed -- _notify_capacity_block already recorded
            # why. Leaving the marker unwritten means the next resume tries
            # again instead of silently losing the notification forever.
            return
        self.state_store.insert_or_load_system_event(
            run_id,
            "live_order_capacity_block_notified",
            {"blocked_key": blocked_key, "order_id": order_id, "duplicate_key": notified_key},
            notified_key,
        )

    def _notify_capacity_block(self, run_id: str, block: OrderCapacityBlock) -> bool:
        """Send the capacity-block notification; return whether any chat got it.

        A caller that tracks "notified" as a durable, one-time marker (see
        ``_deliver_capacity_block_notification``) must only commit that
        marker once something actually went out -- every ``send_message``
        failing here is caught per chat (so one bad chat id does not stop
        the rest), never re-raised, and previously left the caller with no
        way to tell "delivered" from "silently failed everywhere," writing
        the marker either way and losing the notification for good.
        """
        client = self.telegram_client or TelegramBotAPIClient(
            token_env=self.config.approval.telegram_bot_token_env,
            timeout_seconds=10.0,
        )
        maximum = "unknown" if block.max_buy_quantity is None else f"{block.max_buy_quantity:g}"
        text = "\n".join(
            [
                "Maestro order blocked before approval",
                f"order_id: {block.order.order_id}",
                f"account_id: {block.order.account_id or 'default'}",
                f"symbol: {block.order.symbol}",
                f"planned_quantity: {block.requested_quantity:g}",
                f"max_buy_quantity: {maximum}",
                f"reason: {block.reason}",
                "Tap below to review the current retry quantities.",
            ]
        )
        sent = False
        for chat_id in self.config.approval.telegram_allowed_chat_ids:
            try:
                client.send_message(
                    chat_id,
                    text,
                    reply_markup={
                        "inline_keyboard": [
                            [
                                {
                                    "text": "재주문 검토",
                                    "callback_data": (
                                        f"operator:recover:review:{block.order.order_id}"
                                    ),
                                }
                            ]
                        ]
                    },
                )
                sent = True
            except Exception as exc:
                self._record_event(
                    run_id,
                    "live_order_notification_failed",
                    {
                        "order_id": block.order.order_id,
                        "status": "capacity_blocked",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )
        return sent

    def _notify_recovery_order(
        self,
        run_id: str,
        order: OrderIntent,
        reason: str,
    ) -> None:
        try:
            client = self.telegram_client or TelegramBotAPIClient(
                token_env=self.config.approval.telegram_bot_token_env,
                timeout_seconds=10.0,
            )
        except Exception as exc:
            self._record_event(
                run_id,
                "live_order_notification_failed",
                {
                    "order_id": order.order_id,
                    "status": "recoverable",
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return
        text = "\n".join(
            [
                "Maestro recoverable order",
                f"order_id: {order.order_id}",
                f"account_id: {order.account_id or 'default'}",
                f"symbol: {order.symbol}",
                f"quantity: {order.quantity:g}",
                f"reason: {reason}",
                "Tap below to review the current retry quantities.",
            ]
        )
        for chat_id in self.config.approval.telegram_allowed_chat_ids:
            try:
                client.send_message(
                    chat_id,
                    text,
                    reply_markup={
                        "inline_keyboard": [
                            [
                                {
                                    "text": "재주문 검토",
                                    "callback_data": (f"operator:recover:review:{order.order_id}"),
                                }
                            ]
                        ]
                    },
                )
            except Exception as exc:
                self._record_event(
                    run_id,
                    "live_order_notification_failed",
                    {
                        "order_id": order.order_id,
                        "status": "recoverable",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )

    def _strategy_account_mappings(self) -> list[dict[str, Any]]:
        mappings = [
            {
                "strategy_id": loaded.config.id,
                "account_id": self.account_router.account_id_for_strategy(loaded.config),
                "execution_sleeve": loaded.config.execution_sleeve,
                "order_generation_mode": self.config.effective_strategy_order_generation_mode(
                    loaded.config
                ),
            }
            for loaded in self.registry.strategies
            if loaded.config.enabled
            and self.config.multi_account_contribution_group_for_strategy(loaded.config.id) is None
        ]
        for group_id, group in self.config.multi_account_contributions.items():
            for target in group.account_targets:
                mappings.append(
                    {
                        "strategy_id": group.strategy_id,
                        "contribution_group_id": group_id,
                        "account_id": target.account_id,
                        "execution_sleeve": target.execution_sleeve,
                        "order_generation_mode": group.order_generation_mode,
                    }
                )
        return mappings

    def _strategy_phase_controls(self) -> list[dict[str, Any]]:
        return [
            {
                "strategy_id": strategy.id,
                "account_id": strategy.account_id,
                "readonly_enabled": strategy.readonly_enabled,
                "signal_enabled": strategy.signal_enabled,
                "order_posture": self._effective_strategy_order_posture(strategy.id),
                "execution_sleeve": strategy.execution_sleeve,
                "order_generation_mode": self.config.effective_strategy_order_generation_mode(
                    strategy
                ),
            }
            for strategy in self.config.strategies
            if strategy.enabled
        ]

    def _signal_orders_requiring_approval(self, orders: list[OrderIntent]) -> list[OrderIntent]:
        return [
            order
            for order in orders
            if str(order.metadata.get("order_posture") or self.config.execution.order_posture)
            != "disabled"
        ]

    def _orders_requiring_approval(self, orders: list[OrderIntent]) -> list[OrderIntent]:
        return [order for order in orders if self._effective_order_posture(order) != "disabled"]

    def _effective_order_posture(self, order: OrderIntent) -> str:
        posture = str(order.metadata.get("order_posture") or self.config.execution.order_posture)
        if self.config.mode != RunMode.LIVE_APPROVAL and "order_posture" not in order.metadata:
            return "dry_run"
        if (
            self.config.execution.order_posture == "disabled"
            and self.config.mode == RunMode.LIVE_APPROVAL
        ):
            return "disabled"
        if self.config.execution.order_posture == "dry_run" and posture == "armed":
            return "dry_run"
        return posture

    def _effective_signal_strategy_order_posture(self, strategy_id: str) -> str:
        strategy = next(
            (strategy for strategy in self.config.strategies if strategy.id == strategy_id),
            None,
        )
        if strategy is not None and strategy.order_posture is not None:
            return strategy.order_posture
        return self._effective_strategy_order_posture(strategy_id)

    def _effective_strategy_order_posture(self, strategy_id: str) -> str:
        strategy = next(
            (strategy for strategy in self.config.strategies if strategy.id == strategy_id),
            None,
        )
        posture = (
            strategy.order_posture
            if strategy is not None and strategy.order_posture is not None
            else self.config.execution.order_posture
        )
        if self.config.mode != RunMode.LIVE_APPROVAL and (
            strategy is None or strategy.order_posture is None
        ):
            return "dry_run"
        if (
            self.config.execution.order_posture == "disabled"
            and self.config.mode == RunMode.LIVE_APPROVAL
        ):
            return "disabled"
        if self.config.execution.order_posture == "dry_run" and posture == "armed":
            return "dry_run"
        return posture

    def _record_live_proposal_data_snapshot(
        self,
        run_id: str,
        orders: list[OrderIntent],
        data_requests_by_strategy: dict[str, Any],
        prices: dict[str, float],
        data_quality_issues: list[dict[str, Any]],
        target,
        risk_decision,
    ) -> None:
        order_symbols = {order.symbol for order in orders}
        payload = {
            "data_requests": data_requests_by_strategy,
            "prices": {symbol: prices[symbol] for symbol in sorted(prices) if symbol in prices},
            "order_prices": {
                symbol: prices[symbol] for symbol in sorted(order_symbols) if symbol in prices
            },
            "data_quality_issues": data_quality_issues,
            "portfolio_target": target.model_dump(mode="json"),
            "risk_decision": risk_decision.model_dump(mode="json"),
            "proposed_orders": [order.model_dump(mode="json") for order in orders],
        }
        self._record_event(run_id, "live_proposal_data_snapshot", payload)

    def _finish_live_blocked_run(
        self,
        run_id: str,
        current_state: PortfolioState,
        prices: dict[str, float],
        data_requests_by_strategy: dict[str, Any],
        valid_results: list[TargetAllocationResult],
        target,
        risk_decision,
        safety_state,
        live_blocks: list[dict[str, Any]],
        blocked_at: str,
    ) -> RunOnceSummary:
        self.safety.record_blocked_execution(
            run_id,
            self.config.mode.value,
            safety_state,
            blocked_at,
        )
        strategy_book_snapshots = self._save_strategy_book_snapshots(
            run_id,
            valid_results,
            current_state,
            prices,
        )
        summary = RunOnceSummary(
            run_id=run_id,
            loaded_strategies=[strategy.config.id for strategy in self.registry.strategies],
            orders_created=0,
            total_value=current_state.total_value(prices),
            cash=current_state.cash,
        )
        self.audit.log(
            run_id,
            "run_once_completed",
            {
                "loaded_strategies": summary.loaded_strategies,
                "data_requests": data_requests_by_strategy,
                "strategy_results": [result.model_dump(mode="json") for result in valid_results],
                "strategy_book_snapshots": strategy_book_snapshots,
                "portfolio_target": target.model_dump(mode="json"),
                "risk_decision": risk_decision.model_dump(mode="json"),
                "approval_request": None,
                "approval_decision": None,
                "paper_orders": [],
                "execution_results": [],
                "safety_state": safety_state.model_dump(mode="json"),
                "live_blocks": live_blocks,
                "execution_skipped": True,
                "state_summary": current_state.summary(prices),
            },
        )
        self.state_store.save_system_event(
            run_id,
            "run_once_completed",
            {
                "orders_created": summary.orders_created,
                "total_value": summary.total_value,
                "cash": summary.cash,
                "execution_skipped": True,
            },
        )
        return summary

    def _save_strategy_book_snapshots(
        self,
        run_id: str,
        valid_results: list[TargetAllocationResult],
        state: PortfolioState,
        prices: dict[str, float],
    ) -> list[dict[str, Any]]:
        snapshots = build_strategy_book_snapshots(
            results=valid_results,
            strategy_weights=self.portfolio_manager.strategy_weights,
            state=state,
            prices=prices,
        )
        self.state_store.save_strategy_book_snapshots(run_id, snapshots)
        return snapshots

    def _build_batch_items(
        self,
        orders: list[OrderIntent],
        *,
        run_id: str,
        approval_id: str,
        signal_run_id: str | None,
        dependencies_by_account: dict[str | None, LiveApprovalDependencies],
    ) -> list[tuple[LiveOrderRequest, BatchOrderDependencies]]:
        """Turn order intents into submittable batch items.

        `dependencies_by_account` is both cache and output: a rotation runs its
        sells and its buys as separate batches, and both need the same per-account
        services.
        """
        batch_items: list[tuple[LiveOrderRequest, BatchOrderDependencies]] = []
        for order in orders:
            dependencies = dependencies_by_account.get(order.account_id)
            if dependencies is None:
                dependencies = build_live_approval_dependencies(
                    self.config,
                    self.state_store,
                    self.audit,
                    live_order_client=self.live_order_client,
                    status_client=self.live_order_status_client,
                    broker_reconciliation_service=self.broker_reconciliation_service,
                    notification_client=self.live_order_notification_client,
                    telegram_client=self.telegram_client,
                    account_id=order.account_id,
                    signal_run_id=signal_run_id,
                )
                dependencies_by_account[order.account_id] = dependencies
            request = LiveOrderRequest(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                limit_price=order.price,
                order_type=OrderType.LIMIT,
                approval_id=approval_id,
                run_id=run_id,
                duplicate_key=build_live_order_idempotency_key(
                    signal_run_id=signal_run_id,
                    account_id=order.account_id,
                    order_intent_id=order.order_id,
                    fallback_run_id=run_id,
                ),
                currency=order.currency,
                sleeve=order.sleeve,
                execution_sleeve=order.execution_sleeve,
                account_id=order.account_id,
                broker_product=order.broker_product,
                signal_run_id=signal_run_id,
            )
            batch_items.append(
                (
                    request,
                    BatchOrderDependencies(
                        safety_service=dependencies.safety_service,
                        status_service=dependencies.status_service,
                        fill_reconciliation_service=dependencies.fill_reconciliation_service,
                        broker_reconciliation_service=(dependencies.broker_reconciliation_service),
                    ),
                )
            )
        return batch_items

    def _batch_notification_client(
        self,
        dependencies_by_account: dict[str | None, LiveApprovalDependencies],
    ) -> LiveOrderNotificationClient | None:
        if self.live_order_notification_client is not None:
            return self.live_order_notification_client
        for dependencies in dependencies_by_account.values():
            if dependencies.notification_client is not None:
                return dependencies.notification_client
        return None

    def _execute_live_approval_orders(
        self,
        run_id: str,
        orders: list[OrderIntent],
        approval_id: str,
        approval_decision: ApprovalDecision,
        *,
        signal_run_id: str | None = None,
    ) -> tuple[list[LiveOrderLifecycleResult], PortfolioState]:
        safety_state = self.safety.current_state()
        if safety_state.blocks_live_execution:
            self.safety.record_blocked_execution(
                run_id,
                self.config.mode.value,
                safety_state,
                "before_lifecycle",
            )
            return [], self.state_store.load_latest_portfolio_state()

        dry_run_orders = [
            order for order in orders if self._effective_order_posture(order) == "dry_run"
        ]
        armed_orders = [
            order for order in orders if self._effective_order_posture(order) == "armed"
        ]
        if dry_run_orders:
            self._record_live_order_dry_run(
                run_id,
                dry_run_orders,
                approval_id,
                approval_decision,
                signal_run_id=signal_run_id,
            )
        if not armed_orders:
            return [], self.state_store.load_latest_portfolio_state()
        if self.config.execution.live_order_dry_run:
            return self._record_live_order_dry_run(
                run_id,
                armed_orders,
                approval_id,
                approval_decision,
                signal_run_id=signal_run_id,
            )

        dependencies_by_account: dict[str | None, LiveApprovalDependencies] = {}
        lifecycle_results: list[LiveOrderLifecycleResult] = []
        for cohort in split_rotation_cohorts(armed_orders):
            cohort_results, halt_reason = self._run_cohort_phases(
                cohort,
                run_id=run_id,
                approval_id=approval_id,
                approval_decision=approval_decision,
                signal_run_id=signal_run_id,
                dependencies_by_account=dependencies_by_account,
            )
            lifecycle_results.extend(cohort_results)
            if halt_reason is not None:
                self._record_event(
                    run_id,
                    "rotation_cohorts_halted",
                    {"reason": halt_reason, "after_cohort": cohort.account_id},
                )
                break
        return lifecycle_results, self.state_store.load_latest_portfolio_state()

    def _run_cohort_phases(
        self,
        cohort: RotationCohort,
        *,
        run_id: str,
        approval_id: str,
        approval_decision: ApprovalDecision,
        signal_run_id: str | None,
        dependencies_by_account: dict[str | None, LiveApprovalDependencies],
    ) -> tuple[list[LiveOrderLifecycleResult], str | None]:
        """Sell, wait for the fills, then buy against the cash they raised.

        The broker re-checks buying power against its own live balance when the
        order is submitted, so a buy can only be sized once its funding sell has
        actually settled into cash. Sizing it any earlier is what left the book
        sitting in cash for a whole rebalance cycle.
        """
        results: list[LiveOrderLifecycleResult] = []
        stop_remaining_cohorts: str | None = None
        sell_results, sell_requests = self._run_batch_phase(
            list(cohort.sells),
            run_id=run_id,
            approval_id=approval_id,
            approval_decision=approval_decision,
            signal_run_id=signal_run_id,
            dependencies_by_account=dependencies_by_account,
        )
        results.extend(sell_results)
        outcome = evaluate_sell_phase(sell_results)
        self._record_event(
            run_id,
            "rotation_cohort_phase",
            {
                "account_id": cohort.account_id,
                "currency": cohort.currency,
                "phase": "sell",
                "complete": outcome.complete,
                "reason": outcome.reason,
                "sell_order_ids": [order.order_id for order in cohort.sells],
                "buy_order_ids": [order.order_id for order in cohort.buys],
            },
        )
        if not outcome.complete:
            results, stop_remaining_cohorts = self._abort_cohort(
                cohort,
                outcome,
                results,
                run_id=run_id,
                approval_decision=approval_decision,
                dependencies_by_account=dependencies_by_account,
                requests_by_order_id=sell_requests,
            )
            return results, stop_remaining_cohorts
        if not cohort.buys:
            return results, stop_remaining_cohorts
        # Only a cohort whose buys were funded by its own sells needs resizing.
        # A buy-only run — a monthly contribution, say — was already sized and
        # gated against real cash at approval time, and nothing has moved since.
        buys = list(cohort.buys)
        resized_ids = [order.order_id for order in buys]
        if cohort.sells:
            try:
                available_cash = self._cohort_available_cash(cohort)
            except Exception as exc:
                # The sells already filled, so the book is sitting in cash. Letting
                # this unwind the run would leave that state with no event and no
                # alert — the same silent drift this flow exists to remove.
                results, stop_remaining_cohorts = self._abort_cohort(
                    cohort,
                    SellPhaseOutcome(
                        complete=False,
                        reason=f"buying_power_unavailable:{type(exc).__name__}",
                        unfilled=(),
                    ),
                    results,
                    run_id=run_id,
                    approval_decision=approval_decision,
                    dependencies_by_account=dependencies_by_account,
                    requests_by_order_id=sell_requests,
                )
                return results, stop_remaining_cohorts
            buys = rescale_buys_to_cash(
                buys,
                available_cash,
                {instrument.symbol: instrument for instrument in self.config.universe.instruments},
            )
            resized_ids = [order.order_id for order in buys]
            # Now that the sells have settled, this is the authoritative capacity
            # ruling. The partition is strict here precisely because the batch
            # holds no sells: every dimension it reads — per-symbol quantity caps,
            # per-order lookups, running cash reservations — is a settled figure.
            buys, capacity_blocks = self._partition_orders_by_capacity(
                run_id,
                buys,
                signal_run_id=signal_run_id,
            )
            if capacity_blocks:
                self._record_event(
                    run_id,
                    "rotation_cohort_buys_capacity_blocked",
                    {
                        "account_id": cohort.account_id,
                        "currency": cohort.currency,
                        "blocked": [
                            {
                                "order_id": block.order.order_id,
                                "reason": block.reason,
                                "requested_quantity": block.requested_quantity,
                            }
                            for block in capacity_blocks
                        ],
                    },
                )
        buy_results, buy_requests = self._run_batch_phase(
            buys,
            run_id=run_id,
            approval_id=approval_id,
            approval_decision=approval_decision,
            signal_run_id=signal_run_id,
            dependencies_by_account=dependencies_by_account,
        )
        results.extend(buy_results)
        # Every approved buy has to be accounted for, and a leg can go missing at
        # four distinct points. Keeping the stages apart is what lets an operator
        # tell "the cash would not stretch" from "the broker refused it".
        original_ids = [order.order_id for order in cohort.buys]
        capacity_accepted_ids = [order.order_id for order in buys]
        submitted_ids = [
            result.order_id for result in buy_results if result.submitted_order is not None
        ]
        filled_ids = [
            result.order_id
            for result in buy_results
            if result.final_status == OrderStatus.FILLED
        ]
        if len(filled_ids) < len(original_ids):
            # Take any still-working buy off the broker's book first: the attempt
            # can reveal that one of them actually filled, which changes who is
            # missing.
            dependencies = dependencies_by_account.get(cohort.account_id)
            resolution = (
                self._resolve_working_orders(
                    buy_results,
                    run_id=run_id,
                    approval_decision=approval_decision,
                    dependencies=dependencies,
                    account_id=cohort.account_id,
                    requests_by_order_id=buy_requests,
                    side_label="buy",
                )
                if dependencies is not None
                else _CancelResolution({}, [], [], [], False)
            )
            resolved_statuses = resolution.resolved_statuses
            stop_remaining_cohorts = resolution.halt_reason
            # The lifecycle the batch recorded predates these polls. Anything
            # reading the run — orders_filled, the dashboard, the audit trail —
            # goes through that object, so it has to carry the status the broker
            # actually ended on rather than the one polling gave up at.
            buy_results = self._with_resolved_statuses(buy_results, resolution)
            results = self._with_resolved_statuses(results, resolution, run_id=run_id)
            filled_ids = sorted(
                set(filled_ids)
                | {
                    order_id
                    for order_id, status in resolved_statuses.items()
                    if status == OrderStatus.FILLED
                }
            )
        omitted_ids = [order_id for order_id in original_ids if order_id not in filled_ids]
        if omitted_ids:
            buy_outcome = evaluate_sell_phase(buy_results)
            self._report_incomplete_buys(
                cohort,
                run_id=run_id,
                reason=buy_outcome.reason or "buy_legs_omitted",
                original_buy_order_ids=original_ids,
                resized_buy_order_ids=resized_ids,
                capacity_accepted_buy_order_ids=capacity_accepted_ids,
                submitted_buy_order_ids=submitted_ids,
                filled_buy_order_ids=filled_ids,
                omitted_buy_order_ids=omitted_ids,
            )
        return results, stop_remaining_cohorts

    def _resolve_working_orders(
        self,
        results: list[LiveOrderLifecycleResult],
        *,
        run_id: str,
        approval_decision: ApprovalDecision,
        dependencies: LiveApprovalDependencies,
        account_id: str | None,
        requests_by_order_id: dict[str, LiveOrderRequest],
        side_label: str,
    ) -> "_CancelResolution":
        """Take unfinished orders off the broker's book, or raise a blocker.

        An order still OPEN or PARTIALLY_FILLED when polling ran out is live at
        the broker, not finished. Left alone it collides with the next run and
        trips the pending-order gate. Anything that cannot be confirmed gone
        becomes a LIVE_ORDER_RECOVERY_REQUIRED blocker, which stops the next live
        execution until an operator resolves it. Broker-terminal states such as
        REJECTED leave nothing working and need neither.

        Sells and buys go through here identically: a cancellation race can
        reveal fresh fill on either side, and either way the ledger has to learn
        about it.
        """
        canceled: list[dict[str, Any]] = []
        cancel_failures: list[dict[str, Any]] = []
        cancel_unconfirmed: list[dict[str, Any]] = []
        resolved_statuses: dict[str, OrderStatus] = {}
        polled_orders: dict[str, BrokerOrderId] = {}
        snapshots: dict[str, list[LiveOrderStatusSnapshot]] = {}
        for result in results:
            if result.final_status not in {OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}:
                continue
            broker_order = result.submitted_order.broker_order if result.submitted_order else None
            if broker_order is None:
                continue
            self._cancel_and_confirm(
                result,
                broker_order,
                run_id=run_id,
                approval_decision=approval_decision,
                dependencies=dependencies,
                canceled=canceled,
                cancel_failures=cancel_failures,
                cancel_unconfirmed=cancel_unconfirmed,
                resolved_statuses=resolved_statuses,
                polled_orders=polled_orders,
                snapshots=snapshots,
            )
        for order_id, status in sorted(resolved_statuses.items()):
            self._record_event(
                run_id,
                f"rotation_{side_label}_resolved",
                {
                    "order_id": order_id,
                    "final_status": status.value,
                    "account_id": account_id,
                },
            )
        reconciliation_error: Exception | None = None
        fill_result: FillReconciliationResult | None = None
        if polled_orders:
            # Any of these polls can have seen fill quantity the batch's earlier
            # reconciliation pass never had — a partial fill counts as much as a
            # terminal one — so replay whenever one happened at all.
            fill_service = dependencies.fill_reconciliation_service
            if fill_service is not None:
                try:
                    fill_result = fill_service.reconcile_latest(run_id)
                except Exception as exc:
                    reconciliation_error = exc
                    self._record_event(
                        run_id,
                        f"rotation_{side_label}_fill_reconciliation_failed",
                        {
                            "account_id": account_id,
                            "error_type": type(exc).__name__,
                            "error_message": str(exc),
                        },
                    )
        # One blocker per order: a stale ledger and an unconfirmed cancel on the
        # same order are one problem for the operator, and WorkflowRecoveryService
        # does not deduplicate explicit required events.
        blocked_order_ids: set[str] = set()
        if reconciliation_error is not None:
            for order_id in sorted(polled_orders):
                self._save_rotation_recovery_blocker(
                    run_id,
                    order_id=order_id,
                    request=requests_by_order_id.get(order_id),
                    broker_order=polled_orders[order_id],
                    status=resolved_statuses.get(order_id, OrderStatus.UNKNOWN).value,
                    reason=f"rotation_{side_label}_fill_reconciliation_failed",
                    message=str(reconciliation_error),
                    blocked_order_ids=blocked_order_ids,
                    side_label=side_label,
                )
        for entry in cancel_failures + cancel_unconfirmed:
            self._save_rotation_recovery_blocker(
                run_id,
                order_id=entry["order_id"],
                request=requests_by_order_id.get(entry["order_id"]),
                broker_order=entry.get("broker_order"),
                status=entry.get("observed_status") or "unknown",
                reason=f"rotation_{side_label}_unresolved_at_broker",
                message=entry.get("error_message"),
                blocked_order_ids=blocked_order_ids,
                side_label=side_label,
            )
        return _CancelResolution(
            resolved_statuses=resolved_statuses,
            canceled=canceled,
            cancel_failures=cancel_failures,
            cancel_unconfirmed=cancel_unconfirmed,
            reconciliation_failed=reconciliation_error is not None,
            snapshots=snapshots,
            fill_result=fill_result,
        )

    def _save_rotation_recovery_blocker(
        self,
        run_id: str,
        *,
        order_id: str,
        request: LiveOrderRequest | None,
        broker_order: BrokerOrderId | None,
        status: str,
        reason: str,
        message: str | None,
        blocked_order_ids: set[str],
        side_label: str,
    ) -> None:
        if order_id in blocked_order_ids:
            return
        if request is None or broker_order is None:
            # Without both, /recovery cannot rebuild the order or look it up at
            # the broker, and the blocker would gate the next run forever.
            self._record_event(
                run_id,
                f"rotation_{side_label}_unresolvable",
                {
                    "order_id": order_id,
                    "broker_order_id": broker_order.broker_order_id if broker_order else None,
                    "observed_status": status,
                },
            )
            return
        blocked_order_ids.add(order_id)
        save_audited_system_event(
            self.state_store,
            self.audit,
            run_id,
            SystemEventType.LIVE_ORDER_RECOVERY_REQUIRED,
            {
                # /recovery rebuilds a LiveOrderRequest from `request` and reads
                # the broker id from `result.broker_order`, so both must be the
                # complete objects LiveOrderSafetyService records.
                "reason": reason,
                "order_id": order_id,
                "signal_run_id": request.signal_run_id,
                "request": request.model_dump(mode="json"),
                "result": {
                    "order_id": order_id,
                    "status": status,
                    "broker_order": broker_order.model_dump(mode="json"),
                    "message": message,
                },
            },
        )

    def _report_incomplete_buys(
        self,
        cohort: RotationCohort,
        *,
        run_id: str,
        reason: str,
        original_buy_order_ids: list[str],
        resized_buy_order_ids: list[str],
        capacity_accepted_buy_order_ids: list[str],
        submitted_buy_order_ids: list[str],
        filled_buy_order_ids: list[str],
        omitted_buy_order_ids: list[str],
    ) -> None:
        """Record and announce a rotation that sold but could not fully buy back."""
        self._record_event(
            run_id,
            "rotation_cohort_incomplete",
            {
                "account_id": cohort.account_id,
                "currency": cohort.currency,
                "reason": reason,
                "original_buy_order_ids": original_buy_order_ids,
                "resized_buy_order_ids": resized_buy_order_ids,
                "capacity_accepted_buy_order_ids": capacity_accepted_buy_order_ids,
                "submitted_buy_order_ids": submitted_buy_order_ids,
                "filled_buy_order_ids": filled_buy_order_ids,
                "omitted_buy_order_ids": omitted_buy_order_ids,
                "sell_order_ids": [order.order_id for order in cohort.sells],
            },
        )
        never_submitted = [
            order_id
            for order_id in omitted_buy_order_ids
            if order_id not in submitted_buy_order_ids
        ]
        self._notify_rotation_stopped(
            run_id,
            cohort,
            [
                "Maestro rotation incomplete",
                f"account_id: {cohort.account_id or 'default'}",
                f"currency: {cohort.currency or 'default'}",
                f"reason: {reason}",
                "sells: filled",
                f"buys filled: {', '.join(filled_buy_order_ids) or 'none'}",
                f"buys missing: {', '.join(omitted_buy_order_ids) or 'none'}",
                f"never submitted: {', '.join(never_submitted) or 'none'}",
                "The account is holding cash it was meant to deploy.",
                "Re-run the rebalance to resize against current holdings.",
            ],
        )

    def _run_batch_phase(
        self,
        orders: list[OrderIntent],
        *,
        run_id: str,
        approval_id: str,
        approval_decision: ApprovalDecision,
        signal_run_id: str | None,
        dependencies_by_account: dict[str | None, LiveApprovalDependencies],
    ) -> tuple[list[LiveOrderLifecycleResult], dict[str, LiveOrderRequest]]:
        if not orders:
            return [], {}
        batch_items = self._build_batch_items(
            orders,
            run_id=run_id,
            approval_id=approval_id,
            signal_run_id=signal_run_id,
            dependencies_by_account=dependencies_by_account,
        )
        batch = LiveOrderBatchLifecycleService(
            self.config.execution,
            self.state_store,
            self.audit,
            self._batch_notification_client(dependencies_by_account),
        ).run(batch_items, approval_decision)
        lifecycles = [item.lifecycle for item in batch.items]
        self._record_lifecycle_recovery_candidates(
            run_id,
            lifecycles,
            {order.order_id: order for order in orders},
            signal_run_id=signal_run_id,
        )
        return lifecycles, {item.request.order_id: item.request for item in batch.items}

    def _cohort_available_cash(self, cohort: RotationCohort) -> float:
        """Broker buying power once the sells settled, net of the fee buffer."""
        lookup = self.order_capacity_lookup or self._lookup_order_capacity
        capacity = lookup(cohort.buys[0])
        buffer = 1.0 - self.config.execution.live_order_limits.fee_buffer_pct
        return max(0.0, capacity.cash_buying_power) * max(0.0, buffer)

    def _record_lifecycle_recovery_candidates(
        self,
        run_id: str,
        lifecycles: list[LiveOrderLifecycleResult],
        orders_by_id: dict[str, OrderIntent],
        *,
        signal_run_id: str | None,
    ) -> None:
        for lifecycle in lifecycles:
            if (
                lifecycle.final_status.value not in {"failed", "rejected", "halted"}
                or lifecycle.broker_order_id is not None
            ):
                continue
            order = orders_by_id.get(lifecycle.order_id)
            if order is None:
                continue
            self._record_event(
                run_id,
                "live_order_recovery_candidate",
                {
                    "source_order_id": order.order_id,
                    "order": order.model_dump(mode="json"),
                    "source_type": f"lifecycle_{lifecycle.final_status.value}",
                    "reason": lifecycle.failed_reason
                    or lifecycle.halt_reason
                    or lifecycle.final_status.value,
                    "signal_run_id": signal_run_id,
                    "created_at": lifecycle.checked_at,
                    "status": "pending",
                    "duplicate_key": f"live-order-recovery-candidate:{order.order_id}",
                },
            )
            self._notify_recovery_order(
                run_id,
                order,
                lifecycle.failed_reason or lifecycle.halt_reason or lifecycle.final_status.value,
            )

    def _abort_cohort(
        self,
        cohort: RotationCohort,
        outcome: SellPhaseOutcome,
        results: list[LiveOrderLifecycleResult],
        *,
        run_id: str,
        approval_decision: ApprovalDecision,
        dependencies_by_account: dict[str | None, LiveApprovalDependencies],
        requests_by_order_id: dict[str, LiveOrderRequest],
    ) -> tuple[list[LiveOrderLifecycleResult], str | None]:
        """Stop the rotation and hand the account back in a retryable state.

        An order still working at the broker trips the pending-orders gate and
        blocks the operator's entire next run, so the remaining sell quantity has
        to come off the book before we hand back. Once it is gone the operator can
        press rebalance again: the next run regenerates the signal and sizes
        orders from current holdings, so it converges on the same target.

        Cancelling can itself reveal fresh fill, so the sell side gets the same
        ledger replay and lifecycle correction the buy side does.
        """
        dependencies = dependencies_by_account.get(cohort.account_id)
        if dependencies is None:
            resolution = _CancelResolution({}, [], [], [], False)
        else:
            resolution = self._resolve_working_orders(
                list(outcome.unfilled),
                run_id=run_id,
                approval_decision=approval_decision,
                dependencies=dependencies,
                account_id=cohort.account_id,
                requests_by_order_id=requests_by_order_id,
                side_label="sell",
            )
        results = self._with_resolved_statuses(results, resolution, run_id=run_id)
        self._record_event(
            run_id,
            "rotation_cohort_aborted",
            {
                "account_id": cohort.account_id,
                "currency": cohort.currency,
                "reason": outcome.reason,
                "unfilled_order_ids": [result.order_id for result in outcome.unfilled],
                "skipped_buy_order_ids": [order.order_id for order in cohort.buys],
                "canceled": resolution.canceled,
                "cancel_failures": resolution.cancel_failures,
                "cancel_unconfirmed": resolution.cancel_unconfirmed,
            },
        )
        self._notify_cohort_abort(
            cohort,
            outcome,
            run_id,
            resolution.canceled,
            resolution.cancel_failures + resolution.cancel_unconfirmed,
        )
        return results, resolution.halt_reason

    def _with_resolved_statuses(
        self,
        results: list[LiveOrderLifecycleResult],
        resolution: "_CancelResolution",
        *,
        run_id: str | None = None,
    ) -> list[LiveOrderLifecycleResult]:
        """Fold everything the cancel polls learned into the canonical results.

        The batch wrote its lifecycle record before these polls happened, and the
        dashboard and audit trail read that record rather than any side event.
        The trigger is fresh evidence, not a status change: an order that stayed
        PARTIALLY_FILLED can still have filled further, and those are precisely
        the orders an operator has to chase.
        """
        fills_by_broker_order: dict[str, list[AppliedFill]] = {}
        if resolution.fill_result is not None:
            for fill in resolution.fill_result.applied_fills:
                fills_by_broker_order.setdefault(fill.broker_order_id, []).append(fill)
        if not resolution.snapshots and not fills_by_broker_order:
            return results
        corrected: list[LiveOrderLifecycleResult] = []
        for result in results:
            snapshots = resolution.snapshots.get(result.order_id, [])
            applied = (
                fills_by_broker_order.get(result.broker_order_id, [])
                if result.broker_order_id
                else []
            )
            if not snapshots and not applied:
                corrected.append(result)
                continue
            # The newest reading is the truth about this order, terminal or not.
            # Falling back to the pre-cancel status would leave the record
            # claiming OPEN while carrying a PARTIALLY_FILLED snapshot.
            if snapshots:
                status = snapshots[-1].status
            else:
                status = resolution.resolved_statuses.get(result.order_id, result.final_status)
            update: dict[str, Any] = {
                "final_status": status,
                "status_snapshots": [*result.status_snapshots, *snapshots],
                "poll_count": result.poll_count + len(snapshots),
                "applied_fills": [*result.applied_fills, *applied],
                "checked_at": utc_now().isoformat(),
            }
            if result.order_id in resolution.resolved_statuses:
                # Polling did conclude — that is how we learned the new status.
                update["max_polls_reached"] = False
            if resolution.fill_result is not None:
                update["fill_reconciliations"] = [
                    *result.fill_reconciliations,
                    resolution.fill_result,
                ]
            updated = result.model_copy(update=update)
            corrected.append(updated)
            if run_id is not None:
                save_audited_system_event(
                    self.state_store,
                    self.audit,
                    run_id,
                    SystemEventType.LIVE_ORDER_LIFECYCLE,
                    {
                        **updated.model_dump(mode="json"),
                        "supersedes_final_status": result.final_status.value,
                    },
                )
        return corrected

    def _notify_cohort_abort(
        self,
        cohort: RotationCohort,
        outcome: SellPhaseOutcome,
        run_id: str,
        canceled: list[dict[str, Any]],
        cancel_failures: list[dict[str, Any]],
    ) -> None:
        """Tell the operator the rotation stopped and what state it left behind.

        An abort leaves the book part-rotated, which looks exactly like the bug
        this two-phase flow exists to fix. It must never pass silently.
        """
        unfilled = ", ".join(result.order_id for result in outcome.unfilled) or "none"
        skipped = ", ".join(order.order_id for order in cohort.buys) or "none"
        canceled_text = ", ".join(entry["order_id"] for entry in canceled) or "none"
        lines = [
            "Maestro rotation stopped",
            f"account_id: {cohort.account_id or 'default'}",
            f"currency: {cohort.currency or 'default'}",
            f"reason: {outcome.reason}",
            f"sells not filled: {unfilled}",
            f"buys skipped: {skipped}",
            f"canceled: {canceled_text}",
        ]
        if cancel_failures:
            failures = ", ".join(entry["order_id"] for entry in cancel_failures)
            lines.append(f"cancel FAILED (still live at broker): {failures}")
        lines.append("Re-run the rebalance to resize against current holdings.")
        self._notify_rotation_stopped(run_id, cohort, lines)

    def _notify_rotation_stopped(
        self,
        run_id: str,
        cohort: RotationCohort,
        lines: list[str],
    ) -> None:
        try:
            client = self.telegram_client or TelegramBotAPIClient(
                token_env=self.config.approval.telegram_bot_token_env,
                timeout_seconds=10.0,
            )
        except Exception as exc:
            self._record_event(
                run_id,
                "live_order_notification_failed",
                {
                    "status": "rotation_stopped",
                    "account_id": cohort.account_id,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return
        text = "\n".join(lines)
        for chat_id in self.config.approval.telegram_allowed_chat_ids:
            try:
                client.send_message(chat_id, text)
            except Exception as exc:
                self._record_event(
                    run_id,
                    "live_order_notification_failed",
                    {
                        "status": "rotation_stopped",
                        "account_id": cohort.account_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )

    def _cancel_and_confirm(
        self,
        result: LiveOrderLifecycleResult,
        broker_order: BrokerOrderId,
        *,
        run_id: str,
        approval_decision: ApprovalDecision,
        dependencies: LiveApprovalDependencies,
        canceled: list[dict[str, Any]],
        cancel_failures: list[dict[str, Any]],
        cancel_unconfirmed: list[dict[str, Any]],
        resolved_statuses: dict[str, OrderStatus] | None = None,
        polled_orders: dict[str, BrokerOrderId] | None = None,
        snapshots: dict[str, list[LiveOrderStatusSnapshot]] | None = None,
    ) -> None:
        """Cancel a working order and re-poll until the broker agrees it is gone.

        Broker cancel endpoints acknowledge the request, not the outcome — the
        Toss adapter reports CANCELED straight from the POST response. Trusting
        that would tell the operator the book is clear while the order is still
        working, and a working order blocks their entire next run.
        """
        cancel_service = dependencies.cancel_service
        if cancel_service is None:
            # Multi-product KIS routing supplies no cancel client. The order is
            # still working at the broker; saying nothing would strand it.
            cancel_unconfirmed.append(
                {
                    "order_id": result.order_id,
                    "broker_order_id": broker_order.broker_order_id,
                    "broker_order": broker_order,
                    "observed_status": result.final_status.value,
                    "error_message": "no cancel client is configured for this account",
                }
            )
            return
        try:
            cancel_service.cancel_order(
                LiveOrderCancelRequest(
                    run_id=run_id,
                    approval_id=approval_decision.approval_id,
                    broker_order=broker_order,
                    reason="rotation_cohort_aborted",
                ),
                approval_decision,
            )
        except Exception as exc:
            cancel_failures.append(
                {
                    "order_id": result.order_id,
                    "broker_order_id": broker_order.broker_order_id,
                    "broker_order": broker_order,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
            return
        status_service = dependencies.status_service
        if status_service is None:
            cancel_unconfirmed.append(
                {
                    "order_id": result.order_id,
                    "broker_order_id": broker_order.broker_order_id,
                    "broker_order": broker_order,
                    "observed_status": "unknown",
                }
            )
            return
        # Brokers settle a cancellation asynchronously, so the order can keep
        # reporting its old status for a poll or two. Give it the same bounded
        # budget the lifecycle poller uses rather than judging on one reading.
        snapshot = None
        for attempt in range(max(1, self.config.execution.order_status_max_polls)):
            if attempt > 0:
                self._sleep_between_cancel_polls()
            try:
                snapshot = status_service.poll_order_status(run_id, broker_order)
                if polled_orders is not None:
                    polled_orders[result.order_id] = broker_order
                if snapshots is not None:
                    snapshots.setdefault(result.order_id, []).append(snapshot)
            except Exception as exc:
                cancel_unconfirmed.append(
                    {
                        "order_id": result.order_id,
                        "broker_order_id": broker_order.broker_order_id,
                        "broker_order": broker_order,
                        "observed_status": "unknown",
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                return
            if snapshot.status in _CANCEL_TERMINAL_STATUSES:
                break
        if snapshot is not None and snapshot.status in _CANCEL_TERMINAL_STATUSES:
            # A cancel can lose the race to a fill or a rejection. Either way the
            # order stopped working, so it needs no recovery blocker — it is the
            # still-open ones that strand the operator.
            canceled.append(
                {
                    "order_id": result.order_id,
                    "broker_order_id": broker_order.broker_order_id,
                    "status": snapshot.status.value,
                    "canceled_quantity": (
                        snapshot.partial_fill.remaining_quantity
                        if snapshot.status == OrderStatus.CANCELED
                        else 0.0
                    ),
                }
            )
            if resolved_statuses is not None:
                resolved_statuses[result.order_id] = snapshot.status
            return
        cancel_unconfirmed.append(
            {
                "order_id": result.order_id,
                "broker_order_id": broker_order.broker_order_id,
                "broker_order": broker_order,
                "observed_status": snapshot.status.value if snapshot else "unknown",
            }
        )

    def _sleep_between_cancel_polls(self) -> None:
        interval = self.config.execution.order_status_poll_interval_seconds
        if interval > 0:
            sleep(interval)

    def _notify_cohort_abort(
        self,
        cohort: RotationCohort,
        outcome: SellPhaseOutcome,
        run_id: str,
        canceled: list[dict[str, Any]],
        cancel_failures: list[dict[str, Any]],
    ) -> None:
        """Tell the operator the rotation stopped and what state it left behind.

        An abort leaves the book part-rotated, which looks exactly like the bug
        this two-phase flow exists to fix. It must never pass silently.
        """
        unfilled = ", ".join(result.order_id for result in outcome.unfilled) or "none"
        skipped = ", ".join(order.order_id for order in cohort.buys) or "none"
        canceled_text = ", ".join(entry["order_id"] for entry in canceled) or "none"
        lines = [
            "Maestro rotation stopped",
            f"account_id: {cohort.account_id or 'default'}",
            f"currency: {cohort.currency or 'default'}",
            f"reason: {outcome.reason}",
            f"sells not filled: {unfilled}",
            f"buys skipped: {skipped}",
            f"canceled: {canceled_text}",
        ]
        if cancel_failures:
            failures = ", ".join(entry["order_id"] for entry in cancel_failures)
            lines.append(f"cancel FAILED (still live at broker): {failures}")
        lines.append("Re-run the rebalance to resize against current holdings.")
        self._notify_rotation_stopped(run_id, cohort, lines)

    def _notify_rotation_stopped(
        self,
        run_id: str,
        cohort: RotationCohort,
        lines: list[str],
    ) -> None:
        try:
            client = self.telegram_client or TelegramBotAPIClient(
                token_env=self.config.approval.telegram_bot_token_env,
                timeout_seconds=10.0,
            )
        except Exception as exc:
            self._record_event(
                run_id,
                "live_order_notification_failed",
                {
                    "status": "rotation_stopped",
                    "account_id": cohort.account_id,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            )
            return
        text = "\n".join(lines)
        for chat_id in self.config.approval.telegram_allowed_chat_ids:
            try:
                client.send_message(chat_id, text)
            except Exception as exc:
                self._record_event(
                    run_id,
                    "live_order_notification_failed",
                    {
                        "status": "rotation_stopped",
                        "account_id": cohort.account_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                )

    def _record_live_order_dry_run(
        self,
        run_id: str,
        orders: list[OrderIntent],
        approval_id: str,
        approval_decision: ApprovalDecision,
        *,
        signal_run_id: str | None = None,
    ) -> tuple[list[LiveOrderLifecycleResult], PortfolioState]:
        for order in orders:
            request = LiveOrderRequest(
                order_id=order.order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                limit_price=order.price,
                order_type=OrderType.LIMIT,
                approval_id=approval_id,
                run_id=run_id,
                duplicate_key=build_live_order_idempotency_key(
                    signal_run_id=signal_run_id,
                    account_id=order.account_id,
                    order_intent_id=order.order_id,
                    fallback_run_id=run_id,
                ),
                currency=order.currency,
                sleeve=order.sleeve,
                execution_sleeve=order.execution_sleeve,
                account_id=order.account_id,
                broker_product=order.broker_product,
                signal_run_id=signal_run_id,
            )
            event = {
                "signal_run_id": signal_run_id,
                "request": request.model_dump(mode="json"),
                "approval_decision": approval_decision.model_dump(mode="json"),
                "notional": request.notional,
                "reason": "live_order_dry_run",
                "broker_submit_skipped": True,
            }
            self._record_event(run_id, "live_order_dry_run", event)
        return [], self.state_store.load_latest_portfolio_state()

    def _contribution_funding_request(
        self,
        signal_run_id: str,
        order_scope: ScopedOrderTarget,
        scoped_execution,
        as_of,
        *,
        contribution_already_executed: bool,
        contribution_override: bool = False,
    ) -> ContributionFundingRequest | None:
        config = order_scope.execution_config
        if config.order_generation_mode != "buy_only_contribution":
            return None
        if contribution_already_executed:
            return None
        if not contribution_override and not scoped_execution.contribution_is_due(as_of):
            return None
        return build_contribution_funding_request(
            source_signal_run_id=signal_run_id,
            strategy_ids=order_scope.target.source_strategy_ids,
            contribution_group_id=order_scope.contribution_group_id,
            account_id=order_scope.account_id,
            execution_sleeve=order_scope.execution_sleeve,
            execution_config=config,
            state=order_scope.state,
            month_key=scoped_execution.contribution_month_key(as_of),
            created_at=as_of,
            expires_after_seconds=self.config.approval.signal_max_age_seconds,
        )

    def _no_order_reason(
        self,
        order_scope: ScopedOrderTarget,
        scoped_execution,
        as_of,
        *,
        contribution_already_executed: bool,
        contribution_override: bool,
    ) -> str:
        strategy_ids = ",".join(order_scope.target.source_strategy_ids) or "unknown"
        account = order_scope.account_id or "default"
        label = f"{strategy_ids}@{account}"
        config = order_scope.execution_config
        if config.order_generation_mode == "buy_only_contribution":
            contribution = config.contribution
            if not contribution.enabled:
                return f"{label}: contribution disabled"
            if contribution_already_executed:
                month_key = scoped_execution.contribution_month_key(as_of)
                return f"{label}: monthly contribution already executed for {month_key}"
            if not contribution_override and not scoped_execution.contribution_is_due(as_of):
                next_date = scoped_execution.next_contribution_date(as_of)
                return (
                    f"{label}: contribution not due until {next_date.isoformat()} "
                    f"(buy_day {contribution.buy_day}); manual /rebalance overrides this"
                )
            available = scoped_execution.contribution_available_cash(order_scope.state)
            if available < contribution.min_monthly_budget:
                return (
                    f"{label}: available cash {available:,.0f} {contribution.currency.value} "
                    f"is below min_monthly_budget "
                    f"{contribution.min_monthly_budget:,.0f}"
                )
            return f"{label}: contribution produced no orders (check instrument minimums)"
        has_positions = any(order_scope.state.positions.values())
        if not has_positions and order_scope.allocated_cash <= 0:
            return f"{label}: sleeve has no attributed holdings and no available cash"
        return f"{label}: holdings already at target or deltas below minimum order size"

    def _contribution_budget_request(
        self,
        signal_run_id: str,
        order_scope: ScopedOrderTarget,
        scoped_execution,
        as_of,
        *,
        contribution_already_executed: bool,
        contribution_override: bool = False,
    ) -> ContributionBudgetRequest | None:
        config = order_scope.execution_config
        if config.order_generation_mode != "buy_only_contribution":
            return None
        if contribution_already_executed:
            return None
        if not contribution_override and not scoped_execution.contribution_is_due(as_of):
            return None
        return build_contribution_budget_request(
            source_signal_run_id=signal_run_id,
            strategy_ids=order_scope.target.source_strategy_ids,
            contribution_group_id=order_scope.contribution_group_id,
            account_id=order_scope.account_id,
            execution_sleeve=order_scope.execution_sleeve,
            execution_config=config,
            state=order_scope.state,
            month_key=scoped_execution.contribution_month_key(as_of),
            created_at=as_of,
            expires_after_seconds=self.config.approval.signal_max_age_seconds,
        )

    def _build_account_scoped_targets(
        self,
        run_id: str,
        valid_results: list[TargetAllocationResult],
        risk_manager: RiskManager,
        current_state: PortfolioState,
        prices: dict[str, float],
    ) -> tuple[PortfolioTarget, RiskDecision, list[ScopedOrderTarget]]:
        if not self.config.execution_sleeves.has_sleeves():
            return self._build_legacy_account_scoped_targets(
                run_id,
                valid_results,
                risk_manager,
                current_state,
            )

        (
            multi_account_order_targets,
            multi_account_risk_decisions,
            remaining_results,
        ) = self._build_multi_account_contribution_targets(
            run_id,
            valid_results,
            risk_manager,
            current_state,
            prices,
        )

        scope_results = self._results_by_execution_scope(remaining_results)
        if not scope_results:
            if multi_account_order_targets:
                aggregate_target = self.portfolio_manager.build_target(valid_results)
                aggregate_risk = RiskDecision(
                    approved=all(decision.approved for decision in multi_account_risk_decisions),
                    target=aggregate_target,
                    violations=[
                        violation
                        for decision in multi_account_risk_decisions
                        for violation in decision.violations
                    ],
                )
                return aggregate_target, aggregate_risk, multi_account_order_targets
            return self._build_legacy_account_scoped_targets(
                run_id,
                remaining_results,
                risk_manager,
                current_state,
            )

        drafts_by_account: dict[str | None, list[ExecutionScopeDraft]] = {}
        risk_decisions: list[RiskDecision] = list(multi_account_risk_decisions)
        execution_configs: dict[tuple[str | None, str | None], ExecutionConfig] = {}
        for (account_id, execution_sleeve), results in scope_results.items():
            strategy_configs = [self._strategy_config(result.strategy_id) for result in results]
            manager = PortfolioManager(strategy_configs)
            scope_target = self._target_with_configured_cash(
                manager.build_target(results),
                results,
            )
            scope_risk = risk_manager.check(scope_target)
            self._save_account_risk_decision(
                run_id,
                scope_risk,
                scope_target.source_strategy_ids,
                account_id=account_id,
            )
            risk_decisions.append(scope_risk)
            sleeve = self.config.execution_sleeves.sleeve(account_id, execution_sleeve)
            if sleeve is None:
                raise ValueError(
                    f"Unknown execution_sleeve for account_id={account_id}: {execution_sleeve}"
                )
            drafts_by_account.setdefault(account_id, []).append(
                ExecutionScopeDraft(
                    account_id=account_id,
                    execution_sleeve=execution_sleeve,
                    currency_sleeve=sleeve.currency_sleeve,
                    target_weight=self._account_scope_target_weight(
                        account_id,
                        execution_sleeve,
                        sleeve.target_weight,
                    ),
                    target=scope_risk.target,
                    attributed_positions=self._attributed_positions_for_scope(
                        account_id,
                        execution_sleeve,
                    ),
                )
            )
            execution_configs[(account_id, execution_sleeve)] = (
                self.config.effective_execution_config_for_strategy(strategy_configs[0])
            )

        order_targets: list[ScopedOrderTarget] = list(multi_account_order_targets)
        for account_id, drafts in drafts_by_account.items():
            allocated_scopes = allocate_cash_rebalanced_scope_states(
                current_state=current_state,
                scopes=drafts,
                prices=prices,
            )
            for allocated in allocated_scopes:
                order_targets.append(
                    self._scoped_order_target_from_allocated(
                        allocated,
                        execution_configs[(account_id, allocated.execution_sleeve)],
                    )
                )

        aggregate_target = self.portfolio_manager.build_target(valid_results)
        aggregate_risk = RiskDecision(
            approved=all(decision.approved for decision in risk_decisions),
            target=aggregate_target,
            violations=[
                violation for decision in risk_decisions for violation in decision.violations
            ],
        )
        return aggregate_target, aggregate_risk, order_targets

    def _build_multi_account_contribution_targets(
        self,
        run_id: str,
        valid_results: list[TargetAllocationResult],
        risk_manager: RiskManager,
        current_state: PortfolioState,
        prices: dict[str, float],
    ) -> tuple[list[ScopedOrderTarget], list[RiskDecision], list[TargetAllocationResult]]:
        groups_by_strategy_id = {
            group.strategy_id: (group_id, group)
            for group_id, group in self.config.multi_account_contributions.items()
        }
        if not groups_by_strategy_id:
            return [], [], valid_results

        order_targets: list[ScopedOrderTarget] = []
        risk_decisions: list[RiskDecision] = []
        remaining_results: list[TargetAllocationResult] = []
        for result in valid_results:
            group_item = groups_by_strategy_id.get(result.strategy_id)
            if group_item is None:
                remaining_results.append(result)
                continue
            group_id, group = group_item
            account_states = self._latest_account_states(
                [target.account_id for target in group.account_targets],
                current_state,
            )
            target_allocations = self._multi_account_target_allocations(result, group)
            month_key = utc_now().strftime("%Y-%m")
            planned_allocations = self._planned_multi_account_allocations(
                group_id,
                group,
                target_allocations,
                account_states,
                prices,
                month_key,
            )
            for target_config in group.account_targets:
                execution_config = self._multi_account_execution_config(target_config)
                contribution = execution_config.contribution
                account_state = account_states[target_config.account_id]
                available_cash = contribution_available_cash(
                    execution_config,
                    account_state,
                )
                planned = planned_allocations.get(
                    (target_config.account_id, target_config.execution_sleeve),
                    {},
                )
                planned_cash = sum(planned.values())
                state_cash = planned_cash if planned_cash > 0 else available_cash
                selected_budget = self._selected_contribution_budget(
                    group_id,
                    group.strategy_id,
                    target_config.account_id,
                    target_config.execution_sleeve,
                    month_key,
                )
                requires_budget_request = (
                    contribution.budget_request.enabled
                    and selected_budget is None
                    and available_cash >= contribution.min_monthly_budget
                )
                if selected_budget is not None:
                    execution_config = self._execution_config_with_contribution_budget(
                        execution_config,
                        selected_budget,
                    )
                execution_config = self._execution_config_for_spendable_cash(execution_config)
                scope_state = PortfolioState(
                    cash=state_cash,
                    cash_by_currency={contribution.currency.value: state_cash},
                    positions={},
                )
                allocations = self._normalize_allocations(
                    planned
                    if planned
                    else {
                        symbol: target_allocations[symbol]
                        for symbol in target_config.allowed_symbols
                        if symbol in target_allocations
                    }
                )
                scope_target = PortfolioTarget(
                    timestamp=utc_now(),
                    allocations={},
                    allocation_sleeves={contribution.sleeve: allocations},
                    source_strategy_ids=[group.strategy_id],
                )
                scope_risk = risk_manager.check(scope_target)
                self._save_account_risk_decision(
                    run_id,
                    scope_risk,
                    scope_target.source_strategy_ids,
                    account_id=target_config.account_id,
                )
                risk_decisions.append(scope_risk)
                order_targets.append(
                    ScopedOrderTarget(
                        account_id=target_config.account_id,
                        execution_sleeve=target_config.execution_sleeve,
                        target=scope_risk.target,
                        execution_config=execution_config,
                        state=scope_state,
                        contribution_group_id=group_id,
                        allocated_cash=planned_cash,
                        requires_budget_request=requires_budget_request,
                    )
                )
        return order_targets, risk_decisions, remaining_results

    def _multi_account_target_allocations(self, result, group) -> dict[str, float]:
        first_target = group.account_targets[0]
        first_config = self._multi_account_execution_config(first_target)
        sleeve_name = first_config.contribution.sleeve
        if result.allocation_sleeves:
            allocations = result.allocation_sleeves.get(sleeve_name)
            if allocations is None:
                raise ValueError(
                    f"multi_account_contributions {group.strategy_id} result missing "
                    f"allocation sleeve: {sleeve_name}"
                )
            return self._normalize_allocations(allocations)
        return self._normalize_allocations(result.allocations)

    def _planned_multi_account_allocations(
        self,
        group_id: str,
        group,
        target_allocations: dict[str, float],
        account_states: dict[str, PortfolioState],
        prices: dict[str, float],
        month_key: str,
    ) -> dict[tuple[str, str], dict[str, float]]:
        target_symbols = list(target_allocations)
        current_values = {
            symbol: sum(
                state.positions.get(symbol, 0.0) * prices[symbol]
                for state in account_states.values()
            )
            for symbol in target_symbols
        }
        planned: dict[tuple[str, str], dict[str, float]] = {}
        variable_targets = []
        for target_config in group.account_targets:
            execution_config = self._multi_account_execution_config(target_config)
            available_cash = contribution_available_cash(
                execution_config,
                account_states[target_config.account_id],
            )
            selected_budget = self._selected_contribution_budget(
                group_id,
                group.strategy_id,
                target_config.account_id,
                target_config.execution_sleeve,
                month_key,
            )
            budget = self._target_contribution_budget(
                target_config,
                execution_config,
                available_cash,
                selected_budget,
            )
            key = (target_config.account_id, target_config.execution_sleeve)
            if budget <= 0:
                planned[key] = {}
                continue
            if target_config.monthly_budget and len(target_config.allowed_symbols) == 1:
                symbol = target_config.allowed_symbols[0]
                planned[key] = {symbol: budget}
                current_values[symbol] = current_values.get(symbol, 0.0) + budget
                continue
            variable_targets.append((target_config, budget, key))

        for target_config, budget, key in variable_targets:
            allocation = self._budget_toward_aggregate_target(
                current_values,
                target_allocations,
                set(target_config.allowed_symbols),
                budget,
            )
            planned[key] = allocation
            for symbol, amount in allocation.items():
                current_values[symbol] = current_values.get(symbol, 0.0) + amount
        return planned

    def _target_contribution_budget(
        self,
        target_config,
        execution_config: ExecutionConfig,
        available_cash: float,
        selected_budget: float | None,
    ) -> float:
        if selected_budget is not None:
            if (
                selected_budget < target_config.min_monthly_budget
                or selected_budget > available_cash
            ):
                raise ValueError(
                    "contribution budget decision is outside available range for "
                    f"{target_config.account_id}/{target_config.execution_sleeve}"
                )
            return selected_budget
        if available_cash < target_config.min_monthly_budget:
            return 0.0
        if execution_config.contribution.budget_request.enabled:
            return 0.0
        if target_config.monthly_budget:
            return min(available_cash, target_config.monthly_budget)
        return available_cash

    def _selected_contribution_budget(
        self,
        group_id: str,
        strategy_id: str,
        account_id: str,
        execution_sleeve: str,
        month_key: str,
    ) -> float | None:
        """The amount the operator chose for this scope's month, if any.

        ``contribution_budget_request_decision`` is the rollback compatibility
        projection, and it is the only place ``selected_budget`` is recorded --
        ``funding_workflow_completed`` has no field for it. Rather than invent
        a second record of the amount, which would be a new source of truth,
        the *lifecycle* judgement is made authoritative here and the amount
        keeps coming from the row ``complete_workflow`` writes in the same
        transaction, so the two can never disagree.

        Above the migration cutoff, a decision with no completion behind it can
        only have come from an older binary or a manual mutation, and this
        refuses rather than skipping it: skipping falls through to
        ``available_cash`` and invests *more* than the operator selected, which
        is the wrong direction to fail in. Below the cutoff it is legitimate
        pre-3a-4 history. With no cutoff at all the two are indistinguishable,
        so behaviour is left exactly as it was -- guessing would be worse than
        the status quo, and the migration gate is what keeps the system from
        running in that state for long.
        """
        cutoff = load_migration_cutoff(self.state_store)
        for row in self.state_store.list_system_events_by_type(
            "contribution_budget_request_decision",
            limit=1000,
        ):
            payload = row.get("payload") or {}
            if payload.get("status") != "selected":
                continue
            if payload.get("contribution_group_id") != group_id:
                continue
            if strategy_id not in [str(item) for item in payload.get("strategy_ids") or []]:
                continue
            if payload.get("account_id") != account_id:
                continue
            if payload.get("execution_sleeve") != execution_sleeve:
                continue
            if payload.get("month_key") != month_key:
                continue
            request_id = str(payload.get("request_id") or "")
            if cutoff is not None and int(row.get("id") or 0) > cutoff:
                if (
                    request_terminal_state(self.state_store, request_id, "budget")
                    != "completed"
                ):
                    raise ValueError(
                        "uncorroborated contribution budget decision for "
                        f"request_id={request_id}: no funding_workflow_completed backs "
                        "it. Run `maestro rollback-preflight` and check for old-binary "
                        "writes before trading against this amount."
                    )
            return float(payload["selected_budget"])
        return None

    def _budget_toward_aggregate_target(
        self,
        current_values: dict[str, float],
        target_allocations: dict[str, float],
        allowed_symbols: set[str],
        budget: float,
    ) -> dict[str, float]:
        current_total = sum(current_values.values())
        final_total = current_total + budget
        shortfalls = {
            symbol: max(
                0.0,
                target_allocations[symbol] * final_total - current_values.get(symbol, 0.0),
            )
            for symbol in allowed_symbols
            if symbol in target_allocations
        }
        shortfall_total = sum(shortfalls.values())
        if shortfall_total > 0:
            return {
                symbol: budget * shortfall / shortfall_total
                for symbol, shortfall in shortfalls.items()
                if shortfall > 0
            }
        fallback = {
            symbol: target_allocations[symbol]
            for symbol in allowed_symbols
            if symbol in target_allocations
        }
        return {
            symbol: budget * weight
            for symbol, weight in self._normalize_allocations(fallback).items()
        }

    def _multi_account_execution_config(self, target_config) -> ExecutionConfig:
        sleeve = self.config.execution_sleeves.sleeve(
            target_config.account_id,
            target_config.execution_sleeve,
        )
        if sleeve is None:
            raise ValueError(
                "Unknown multi-account execution_sleeve: "
                f"{target_config.account_id}/{target_config.execution_sleeve}"
            )
        values = self.config.execution.model_dump(mode="python")
        values["order_generation_mode"] = sleeve.order_generation_mode
        contribution = sleeve.contribution.model_dump(mode="python")
        if target_config.monthly_budget:
            contribution["monthly_budget"] = target_config.monthly_budget
        contribution["min_monthly_budget"] = target_config.min_monthly_budget
        contribution["max_monthly_budget"] = target_config.max_monthly_budget
        values["contribution"] = contribution
        return ExecutionConfig.model_validate(values)

    def _execution_config_with_contribution_budget(
        self,
        execution_config: ExecutionConfig,
        budget: float,
    ) -> ExecutionConfig:
        values = execution_config.model_dump(mode="python")
        contribution = values["contribution"]
        contribution["monthly_budget"] = budget
        values["contribution"] = contribution
        return ExecutionConfig.model_validate(values)

    def _execution_config_for_spendable_cash(
        self,
        execution_config: ExecutionConfig,
    ) -> ExecutionConfig:
        values = execution_config.model_dump(mode="python")
        values["live_order_limits"]["fee_buffer_pct"] = 0.0
        return ExecutionConfig.model_validate(values)

    def _latest_account_states(
        self,
        account_ids: list[str],
        fallback_state: PortfolioState,
    ) -> dict[str, PortfolioState]:
        account_id_set = set(account_ids)
        states: dict[str, PortfolioState] = {}
        for row in self.state_store.list_broker_account_snapshots(
            limit=max(10, len(account_id_set) * 5)
        ):
            payload = row["payload"]
            account_id = payload.get("account_id") or row.get("account_id")
            if account_id not in account_id_set or account_id in states:
                continue
            account_payload = payload.get("account") or payload
            ledger_state = self.state_store.load_latest_account_portfolio_state(account_id)
            states[account_id] = portfolio_state_from_broker_account(
                account_payload,
                allowed_symbols=self.config.portfolio.allowed_symbols,
                universe=self.config.universe,
                ledger_state=ledger_state,
                allow_proxy_cash=not str(account_payload.get("source") or "").startswith("toss_"),
            )
        if len(account_id_set) == 1 and not states:
            states[next(iter(account_id_set))] = fallback_state
        missing = sorted(account_id_set - set(states))
        if missing:
            raise ValueError(
                "multi_account_contributions require broker snapshots for account_id: "
                + ", ".join(missing)
            )
        return states

    def _normalize_allocations(self, allocations: dict[str, float]) -> dict[str, float]:
        investable = {
            symbol: weight
            for symbol, weight in allocations.items()
            if not is_cash_symbol(symbol) and weight > 0
        }
        total = sum(investable.values())
        if total <= 0:
            return {}
        return {symbol: weight / total for symbol, weight in investable.items()}

    def _build_legacy_account_scoped_targets(
        self,
        run_id: str,
        valid_results: list[TargetAllocationResult],
        risk_manager: RiskManager,
        current_state: PortfolioState,
    ) -> tuple[PortfolioTarget, RiskDecision, list[ScopedOrderTarget]]:
        account_results = self._results_by_account(valid_results)
        if len(account_results) <= 1:
            target = self._target_with_configured_cash(
                self.portfolio_manager.build_target(valid_results),
                valid_results,
            )
            risk_decision = risk_manager.check(target)
            self._save_account_risk_decision(run_id, risk_decision, target.source_strategy_ids)
            account_id = next(iter(account_results), None)
            return (
                target,
                risk_decision,
                [
                    ScopedOrderTarget(
                        account_id=account_id,
                        execution_sleeve=None,
                        target=risk_decision.target,
                        execution_config=self.config.execution,
                        state=current_state,
                    )
                ],
            )

        order_targets: list[ScopedOrderTarget] = []
        risk_decisions = []
        for account_id, results in account_results.items():
            strategy_configs = [
                loaded.config
                for loaded in self.registry.strategies
                if self.account_router.account_id_for_strategy(loaded.config) == account_id
            ]
            account_manager = PortfolioManager(strategy_configs)
            account_target = self._target_with_configured_cash(
                account_manager.build_target(results),
                results,
            )
            account_risk = risk_manager.check(account_target)
            self._save_account_risk_decision(
                run_id,
                account_risk,
                account_target.source_strategy_ids,
                account_id=account_id,
            )
            risk_decisions.append(account_risk)
            order_targets.append(
                ScopedOrderTarget(
                    account_id=account_id,
                    execution_sleeve=None,
                    target=account_risk.target,
                    execution_config=self.config.execution,
                    state=current_state,
                )
            )

        aggregate_target = self.portfolio_manager.build_target(valid_results)
        aggregate_risk = RiskDecision(
            approved=all(decision.approved for decision in risk_decisions),
            target=aggregate_target,
            violations=[
                violation for decision in risk_decisions for violation in decision.violations
            ],
        )
        return aggregate_target, aggregate_risk, order_targets

    def _scoped_order_target_from_allocated(
        self,
        allocated: AllocatedExecutionScope,
        execution_config: ExecutionConfig,
    ) -> ScopedOrderTarget:
        return ScopedOrderTarget(
            account_id=allocated.account_id,
            execution_sleeve=allocated.execution_sleeve,
            target=allocated.target,
            execution_config=execution_config,
            state=allocated.state,
            allocated_cash=allocated.allocated_cash,
            current_value=allocated.current_value,
            current_weight=allocated.current_weight,
            target_weight=allocated.target_weight,
            drift=allocated.drift,
        )

    def _attributed_positions_for_scope(
        self,
        account_id: str | None,
        execution_sleeve: str | None,
    ) -> dict[str, float] | None:
        if not account_id or not execution_sleeve:
            return None
        if account_id not in self.config.account_strategy_targets:
            return None
        if execution_sleeve not in self.config.account_strategy_targets[account_id]:
            return None
        snapshot = self._latest_broker_snapshot_for_account(account_id)
        if snapshot is None:
            raise ValueError(
                f"account attribution requires a broker snapshot for account_id={account_id}"
            )
        account = snapshot["payload"]["account"]
        attributed = AccountAttributionReconciliationService(
            self.state_store,
            self.audit,
        ).require_ready(
            account_id=account_id,
            broker_snapshot_id=int(snapshot["id"]),
            broker_positions={
                str(position["symbol"]): float(position["quantity"])
                for position in account.get("positions", [])
            },
        )
        positions: dict[str, float] = {}
        for position in attributed:
            if position.bucket_id != execution_sleeve:
                continue
            positions[position.symbol] = positions.get(position.symbol, 0.0) + position.quantity
        return positions

    def _account_scope_target_weight(
        self,
        account_id: str | None,
        execution_sleeve: str | None,
        fallback: float,
    ) -> float:
        if not account_id or not execution_sleeve:
            return fallback
        target = self.config.account_strategy_targets.get(account_id, {}).get(execution_sleeve)
        return target.target_weight if target is not None else fallback

    def _latest_broker_snapshot_for_account(
        self,
        account_id: str,
    ) -> dict[str, Any] | None:
        for row in self.state_store.list_broker_account_snapshots(limit=1000):
            payload = row.get("payload") or {}
            logical_account_id = str(payload.get("account_id") or row.get("account_id") or "")
            if logical_account_id == account_id:
                return row
        return None

    def _results_by_account(
        self,
        valid_results: list[TargetAllocationResult],
    ) -> dict[str, list[TargetAllocationResult]]:
        results_by_account: dict[str, list[TargetAllocationResult]] = {}
        for result in valid_results:
            account_id = self._strategy_account_id(result.strategy_id)
            results_by_account.setdefault(account_id, []).append(result)
        if not results_by_account:
            results_by_account[PAPER_DEFAULT_ACCOUNT_ID] = []
        return results_by_account

    def _results_by_execution_scope(
        self,
        valid_results: list[TargetAllocationResult],
    ) -> dict[tuple[str | None, str | None], list[TargetAllocationResult]]:
        results_by_scope: dict[tuple[str | None, str | None], list[TargetAllocationResult]] = {}
        for result in valid_results:
            strategy = self._strategy_config(result.strategy_id)
            account_id = self.account_router.account_id_for_strategy(strategy)
            key = (account_id, strategy.execution_sleeve)
            results_by_scope.setdefault(key, []).append(result)
        return results_by_scope

    def _strategy_config(self, strategy_id: str):
        for loaded in self.registry.strategies:
            if loaded.config.id == strategy_id:
                return loaded.config
        for strategy in self.config.strategies:
            if strategy.id == strategy_id:
                return strategy
        raise ValueError(f"Unknown strategy id: {strategy_id}")

    def _save_account_risk_decision(
        self,
        run_id: str,
        risk_decision: RiskDecision,
        source_strategy_ids: list[str],
        *,
        account_id: str | None = None,
    ) -> None:
        account_ids = sorted(
            {
                account_id
                for strategy_id in source_strategy_ids
                for account_id in self._strategy_account_ids(strategy_id)
            }
        )
        if account_id and account_id not in account_ids:
            account_ids.append(account_id)
            account_ids.sort()
        risk_payload = risk_decision.model_dump(mode="json")
        risk_payload["account_ids"] = account_ids
        if len(account_ids) == 1:
            risk_payload["account_id"] = account_ids[0]
        self.state_store.save_risk_decision(
            run_id,
            risk_decision.approved,
            risk_payload,
        )

    def _strategy_account_id(self, strategy_id: str) -> str:
        for loaded in self.registry.strategies:
            if loaded.config.id == strategy_id:
                return self.account_router.account_id_for_strategy(loaded.config)
        return PAPER_DEFAULT_ACCOUNT_ID

    def _strategy_account_ids(self, strategy_id: str) -> list[str]:
        group = self.config.multi_account_contribution_group_for_strategy(strategy_id)
        if group is not None:
            return [target.account_id for target in group.account_targets]
        return [self._strategy_account_id(strategy_id)]

    def _single_live_account_id(self) -> str | None:
        account_ids = set(self._live_account_ids())
        if len(account_ids) == 1:
            return next(iter(account_ids))
        return None

    def _live_account_ids(self) -> list[str]:
        account_ids = {
            self.account_router.account_id_for_strategy(loaded.config)
            for loaded in self.registry.strategies
            if loaded.config.enabled
            and loaded.config.account_id
            and not is_multi_account_contribution_account_id(loaded.config.account_id)
            and (
                self.account_router.account(loaded.config.account_id) is None
                or self.account_router.account(loaded.config.account_id).broker != "sandbox"
            )
        }
        for group in self.config.multi_account_contributions.values():
            for target in group.account_targets:
                account = self.account_router.account(target.account_id)
                if account is None or account.broker != "sandbox":
                    account_ids.add(target.account_id)
        return sorted(account_ids)

    def _apply_native_order_prices(
        self,
        orders: list[OrderIntent],
        prices: dict[str, float],
    ) -> list[OrderIntent]:
        instruments = {
            instrument.symbol: instrument for instrument in self.config.universe.instruments
        }
        repriced_orders: list[OrderIntent] = []
        for order in orders:
            price = prices.get(order.symbol)
            if price is None:
                repriced_orders.append(order)
                continue
            # These prices prefer the broker's quote, which is not guaranteed to sit
            # on a tick, so re-snap it. Without this the substitution silently
            # discards the tick rounding the order builder already did and the live
            # execution gate rejects the whole run on price_tick.
            price = round_price_to_tick(price, instruments.get(order.symbol))
            repriced_orders.append(
                order.model_copy(
                    update={
                        "price": price,
                        "notional": abs(order.quantity) * price,
                    }
                )
            )
        return repriced_orders

    def _stamp_orders_with_account_id(
        self,
        orders: list[OrderIntent],
        source_strategy_ids: list[str],
        *,
        account_id: str | None = None,
        execution_sleeve: str | None = None,
        contribution_group_id: str | None = None,
        signal_preview: bool = False,
    ) -> list[OrderIntent]:
        account_ids = {
            account_id
            for strategy_id in source_strategy_ids
            if strategy_id
            for account_id in self._strategy_account_ids(strategy_id)
        }
        posture_for_strategy = (
            self._effective_signal_strategy_order_posture
            if signal_preview
            else self._effective_strategy_order_posture
        )
        order_postures = {
            posture_for_strategy(strategy_id) for strategy_id in source_strategy_ids if strategy_id
        }
        resolved_account_id = account_id or (
            next(iter(account_ids)) if len(account_ids) == 1 else None
        )
        resolved_order_posture = next(iter(order_postures)) if len(order_postures) == 1 else None
        return [
            order.model_copy(
                update={
                    "account_id": order.account_id or resolved_account_id,
                    "metadata": {
                        **order.metadata,
                        "account_id": order.account_id or resolved_account_id,
                        "source_strategy_ids": list(source_strategy_ids),
                        "order_posture": resolved_order_posture,
                        "execution_sleeve": execution_sleeve,
                        **(
                            {"contribution_group_id": contribution_group_id}
                            if contribution_group_id
                            else {}
                        ),
                    },
                }
            )
            for order in orders
        ]


def _signal_contract_fingerprint(config: MaestroConfig) -> str:
    payload = _signal_contract_payload(config)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def signal_contract_fingerprint_diff(left: MaestroConfig, right: MaestroConfig) -> list[str]:
    left_payload = _signal_contract_payload(left)
    right_payload = _signal_contract_payload(right)
    changed: list[str] = []
    for key in sorted(set(left_payload) | set(right_payload)):
        left_value = left_payload.get(key)
        right_value = right_payload.get(key)
        if _canonical_json(left_value) == _canonical_json(right_value):
            continue
        if isinstance(left_value, dict) and isinstance(right_value, dict):
            for child_key in sorted(set(left_value) | set(right_value)):
                if _canonical_json(left_value.get(child_key)) != _canonical_json(
                    right_value.get(child_key)
                ):
                    changed.append(f"{key}.{child_key}")
            continue
        changed.append(key)
    return changed


def _signal_contract_payload(config: MaestroConfig) -> dict[str, Any]:
    payload = config.model_dump(mode="json")
    payload.pop("approval", None)
    execution = dict(payload.get("execution") or {})
    execution.pop("order_posture", None)
    execution.pop("live_order_enabled", None)
    execution.pop("live_order_dry_run", None)
    payload["execution"] = execution
    return payload


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _combined_approval_status(statuses: list[str]) -> str:
    if not statuses:
        return "not_required"
    unique_statuses = set(statuses)
    if len(unique_statuses) == 1:
        return statuses[0]
    return "mixed"


def _broker_snapshot_prices(snapshot: dict[str, Any]) -> dict[str, float]:
    prices = {
        symbol: float(price)
        for symbol, price in (snapshot.get("current_prices") or {}).items()
        if float(price) > 0
    }
    account = snapshot.get("account") or {}
    for position in account.get("positions") or []:
        symbol = position.get("symbol")
        current_price = position.get("current_price")
        if symbol and current_price is not None and float(current_price) > 0:
            prices[symbol] = float(current_price)
    return prices


def _merge_portfolio_states(states: list[PortfolioState]) -> PortfolioState:
    if not states:
        return PortfolioState(cash=0.0, cash_by_currency={}, positions={})
    cash_by_currency: dict[str, float] = {}
    positions: dict[str, float] = {}
    for state in states:
        if state.cash_by_currency:
            for currency, cash in state.cash_by_currency.items():
                cash_by_currency[currency] = cash_by_currency.get(currency, 0.0) + cash
        else:
            cash_by_currency["CASH"] = cash_by_currency.get("CASH", 0.0) + state.cash
        for symbol, quantity in state.positions.items():
            positions[symbol] = positions.get(symbol, 0.0) + quantity
    return PortfolioState(
        cash=sum(cash_by_currency.values()),
        cash_by_currency=cash_by_currency,
        positions=positions,
    )


def _broker_snapshot_material_difference(
    baseline_payload: dict[str, Any],
    latest_payload: dict[str, Any],
    *,
    cash_tolerance: float,
    position_tolerance: float,
) -> dict[str, Any] | None:
    baseline_account = baseline_payload.get("account") or {}
    latest_account = latest_payload.get("account") or {}
    baseline_broker_id = baseline_account.get("account_id")
    latest_broker_id = latest_account.get("account_id")
    if baseline_broker_id != latest_broker_id:
        return {
            "reason": "broker_account_id_changed",
            "baseline": baseline_broker_id,
            "latest": latest_broker_id,
        }
    baseline_cash = _account_cash_by_currency(baseline_account)
    latest_cash = _account_cash_by_currency(latest_account)
    for currency in sorted(set(baseline_cash) | set(latest_cash)):
        difference = latest_cash.get(currency, 0.0) - baseline_cash.get(currency, 0.0)
        if abs(difference) > cash_tolerance:
            return {
                "reason": "cash_changed",
                "currency": currency,
                "difference": difference,
                "tolerance": cash_tolerance,
            }
    baseline_positions = _account_position_quantities(baseline_account)
    latest_positions = _account_position_quantities(latest_account)
    for symbol in sorted(set(baseline_positions) | set(latest_positions)):
        difference = latest_positions.get(symbol, 0.0) - baseline_positions.get(symbol, 0.0)
        if abs(difference) > position_tolerance:
            return {
                "reason": "position_changed",
                "symbol": symbol,
                "difference": difference,
                "tolerance": position_tolerance,
            }
    return None


def _account_cash_by_currency(account: dict[str, Any]) -> dict[str, float]:
    cash_by_currency = account.get("cash_by_currency") or {}
    if isinstance(cash_by_currency, dict) and cash_by_currency:
        return {str(currency): float(value) for currency, value in cash_by_currency.items()}
    return {"CASH": float(account.get("cash", 0.0))}


def _account_position_quantities(account: dict[str, Any]) -> dict[str, float]:
    quantities: dict[str, float] = {}
    for position in account.get("positions") or []:
        if not isinstance(position, dict):
            continue
        symbol = str(position.get("symbol") or "")
        if not symbol:
            continue
        quantities[symbol] = quantities.get(symbol, 0.0) + float(position.get("quantity", 0.0))
    return quantities


def _profile_name(config_identity: ConfigIdentity | None) -> str | None:
    if config_identity is None:
        return None
    return Path(config_identity.path).stem


def _parse_signal_ref_time(value: str) -> datetime:
    if not value:
        raise ValueError("Signal package broker snapshot ref missing created_at")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

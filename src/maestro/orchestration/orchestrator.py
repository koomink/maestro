import traceback
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from maestro.approval.manager import ApprovalManager
from maestro.approval.models import ApprovalDecision
from maestro.config.identity import ConfigIdentity
from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderType, RunMode
from maestro.core.ids import new_run_id
from maestro.core.symbols import is_cash_symbol
from maestro.datahub.base import BaseDataProvider, build_data_provider
from maestro.execution.base import OrderIntent
from maestro.execution.broker_state import portfolio_state_from_broker_account
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.factory import build_execution_engine
from maestro.execution.live_order_factory import build_live_approval_dependencies
from maestro.execution.live_orders import (
    BrokerReconciliationRunner,
    LiveOrderClient,
    LiveOrderLifecycleResult,
    LiveOrderNotificationClient,
    LiveOrderRequest,
    LiveOrderStatusClient,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.data_quality import (
    data_quality_issues as collect_data_quality_issues,
)
from maestro.orchestration.data_quality import (
    prices_from_bundle,
)
from maestro.orchestration.live_gates import LiveExecutionGateService
from maestro.plugins.registry import PluginRegistry
from maestro.portfolio.manager import PortfolioManager, PortfolioTarget
from maestro.portfolio.strategy_books import build_strategy_book_snapshots
from maestro.risk.manager import RiskManager
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
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore
from maestro.universe.dynamic import DynamicUniverseService, InstrumentResolver


class RunOnceSummary(BaseModel):
    run_id: str
    loaded_strategies: list[str]
    orders_created: int
    total_value: float
    cash: float


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
    ) -> None:
        self.config = config
        self.registry = PluginRegistry.from_configs(config.strategies, run_mode=config.mode)
        self.datahub: BaseDataProvider = build_data_provider(config.datahub)
        self.portfolio_manager = PortfolioManager(config.strategies)
        self.risk_manager = RiskManager(config.portfolio.allowed_symbols, config.risk)
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

    def run_once(self) -> RunOnceSummary:
        with self.state_store.writer_lock("run_once"):
            return self._run_once_locked()

    def _run_once_locked(self) -> RunOnceSummary:
        run_id = new_run_id()
        current_state = self._load_run_portfolio_state(run_id)
        valid_results: list[TargetAllocationResult] = []
        strategy_ids = self.registry.strategy_ids
        data_requests_by_strategy = {}
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
            validator = SignalValidator.with_universe_boundaries(
                tradable_symbols=run_allowed_symbols,
                research_only_symbols=set(self.config.universe.research_symbols),
                strategy_ids=strategy_ids,
            )
            risk_manager = (
                RiskManager(sorted(run_allowed_symbols), self.config.risk)
                if dynamic_symbols
                else self.risk_manager
            )
            for loaded in self.registry.strategies:
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

            target = self._target_with_configured_cash(
                self.portfolio_manager.build_target(valid_results),
                valid_results,
            )
            risk_decision = risk_manager.check(target)
            self.state_store.save_risk_decision(
                run_id,
                risk_decision.approved,
                risk_decision.model_dump(mode="json"),
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
                    prices,
                    data_requests_by_strategy,
                    valid_results,
                    target,
                    risk_decision,
                    safety_state,
                    live_blocks,
                    "before_order_generation",
                )

            order_generation_time = utc_now()
            contribution_already_executed = self._contribution_already_executed(
                order_generation_time
            )
            prices = self._order_generation_prices(prices)
            orders = self.execution.propose_orders(
                current_state,
                risk_decision.target,
                prices,
                as_of=order_generation_time,
                contribution_already_executed=contribution_already_executed,
            )
            if orders and self.config.mode == RunMode.LIVE_APPROVAL:
                self._record_live_proposal_data_snapshot(
                    run_id,
                    orders,
                    data_requests_by_strategy,
                    prices,
                    data_quality_issues,
                    target,
                    risk_decision,
                )
            live_blocks = self._live_execution_blocks(run_id, orders, data_quality_issues)
            if (
                orders
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
                    prices,
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
            if orders:
                approval_request, approval_decision, approval_message = (
                    self.approval_manager.request_approval(
                        run_id,
                        orders,
                        risk_decision.modifications,
                        risk_decision.violations,
                        target.source_strategy_ids,
                    )
                )
            else:
                approval_request = None
                approval_decision = None
                approval_message = None

            if orders:
                if approval_request and approval_decision:
                    self.state_store.save_approval(
                        run_id,
                        approval_request.approval_id,
                        {
                            "request": approval_request.model_dump(mode="json"),
                            "decision": approval_decision.model_dump(mode="json"),
                            "message": approval_message,
                        },
                    )
                    self.audit.log(
                        run_id,
                        "approval_decision",
                        {
                            "request": approval_request.model_dump(mode="json"),
                            "decision": approval_decision.model_dump(mode="json"),
                            "message": approval_message,
                        },
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
                            orders,
                            approval_request.approval_id,
                            approval_decision,
                        )
                    else:
                        execution_results, next_state = self.execution.execute_orders(
                            current_state, orders
                        )
                else:
                    if self.config.mode == RunMode.LIVE_APPROVAL:
                        raise ValueError("live_approval mode requires an approval decision")
                    execution_results, next_state = self.execution.execute_orders(
                        current_state, orders
                    )
            else:
                execution_results = []
                next_state = current_state

            for order in orders:
                order_payload = order.model_dump(mode="json")
                order_payload["approval_status"] = (
                    approval_decision.status if approval_decision else "not_required"
                )
                if not (
                    self.config.mode == RunMode.LIVE_APPROVAL
                    and self.config.execution.live_order_dry_run
                ):
                    self.state_store.save_order(run_id, order.order_id, order_payload)
            self.state_store.save_portfolio_snapshot(run_id, next_state)
            strategy_book_snapshots = self._save_strategy_book_snapshots(
                run_id,
                valid_results,
                next_state,
                prices,
            )

            summary = RunOnceSummary(
                run_id=run_id,
                loaded_strategies=[strategy.config.id for strategy in self.registry.strategies],
                orders_created=len(orders),
                total_value=next_state.total_value(prices),
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
                    "execution_results": [
                        result.model_dump(mode="json") for result in execution_results
                    ],
                    "state_summary": next_state.summary(prices),
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

    def _contribution_already_executed(self, as_of) -> bool:
        if self.config.execution.order_generation_mode != "buy_only_contribution":
            return False
        month_key = self.execution.contribution_month_key(as_of)
        if self.config.mode == RunMode.LIVE_APPROVAL:
            return self.state_store.monthly_live_contribution_order_exists(
                month_key,
                self.config.execution.contribution.sleeve,
            )
        return self.state_store.monthly_contribution_order_exists(
            month_key,
            self.config.execution.contribution.sleeve,
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

    def _load_run_portfolio_state(self, run_id: str) -> PortfolioState:
        if self.config.mode != RunMode.LIVE_APPROVAL:
            return self.state_store.load_latest_portfolio_state()
        if not self.registry.strategies and self.state_store.has_portfolio_snapshot():
            return self.state_store.load_latest_portfolio_state()
        if self.config.kis.provider != "kis":
            if self.state_store.has_portfolio_snapshot():
                return self.state_store.load_latest_portfolio_state()
            self._record_event(
                run_id,
                SystemEventType.BROKER_BASELINE_REQUIRED,
                {
                    "mode": self.config.mode.value,
                    "reason": "live_approval requires an adopted broker snapshot before run_once",
                },
            )
            raise ValueError(
                "live_approval requires an adopted broker snapshot before run_once; "
                "run kis-sync and adopt-broker-snapshot first"
            )
        try:
            snapshot = KISReadOnlyService(
                self.config.kis,
                self.state_store,
                self.audit,
                instruments=self.config.universe.instruments,
            ).fetch_and_store_snapshot(self.config.portfolio.allowed_symbols)
            state = portfolio_state_from_broker_account(
                snapshot.account.model_dump(mode="json"),
                allowed_symbols=self.config.portfolio.allowed_symbols,
                universe=self.config.universe,
            )
        except (RuntimeError, TimeoutError, ValueError) as exc:
            self._record_event(
                run_id,
                SystemEventType.BROKER_BASELINE_REQUIRED,
                {
                    "mode": self.config.mode.value,
                    "reason": "live_approval could not refresh KIS broker snapshot before run_once",
                    "error": str(exc),
                },
            )
            raise ValueError(
                "live_approval could not refresh KIS broker snapshot before run_once: "
                f"{exc}"
            ) from exc
        self.state_store.save_portfolio_snapshot(run_id, state)
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
        return state

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
        self.state_store.save_portfolio_snapshot(run_id, current_state)
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

    def _execute_live_approval_orders(
        self,
        run_id: str,
        orders: list[OrderIntent],
        approval_id: str,
        approval_decision: ApprovalDecision,
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

        if self.config.execution.live_order_dry_run:
            return self._record_live_order_dry_run(
                run_id,
                orders,
                approval_id,
                approval_decision,
            )

        dependencies = build_live_approval_dependencies(
            self.config,
            self.state_store,
            self.audit,
            live_order_client=self.live_order_client,
            status_client=self.live_order_status_client,
            broker_reconciliation_service=self.broker_reconciliation_service,
            notification_client=self.live_order_notification_client,
            telegram_client=self.telegram_client,
        )
        lifecycle_results = []
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
                duplicate_key=f"{run_id}:{order.order_id}",
                currency=order.currency,
                sleeve=order.sleeve,
                broker_product=order.broker_product,
            )
            lifecycle_results.append(dependencies.lifecycle_service.run(request, approval_decision))
        return lifecycle_results, self.state_store.load_latest_portfolio_state()

    def _record_live_order_dry_run(
        self,
        run_id: str,
        orders: list[OrderIntent],
        approval_id: str,
        approval_decision: ApprovalDecision,
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
                duplicate_key=f"{run_id}:{order.order_id}",
                currency=order.currency,
                sleeve=order.sleeve,
                broker_product=order.broker_product,
            )
            event = {
                "request": request.model_dump(mode="json"),
                "approval_decision": approval_decision.model_dump(mode="json"),
                "notional": request.notional,
                "reason": "live_order_dry_run",
                "broker_submit_skipped": True,
            }
            self._record_event(run_id, "live_order_dry_run", event)
        return [], self.state_store.load_latest_portfolio_state()


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
            prices.setdefault(str(symbol), float(current_price))
    return prices


def _profile_name(config_identity: ConfigIdentity | None) -> str | None:
    if config_identity is None:
        return None
    return Path(config_identity.path).stem

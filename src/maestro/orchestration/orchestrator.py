import traceback

from pydantic import BaseModel

from maestro.approval.manager import ApprovalManager
from maestro.approval.models import ApprovalDecision
from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderType, RunMode
from maestro.core.ids import new_run_id
from maestro.datahub.base import BaseDataProvider, build_data_provider
from maestro.execution.base import OrderIntent
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
from maestro.plugins.registry import PluginRegistry
from maestro.portfolio.manager import PortfolioManager
from maestro.risk.manager import RiskManager
from maestro.sdk import StrategyContext, TargetAllocationResult
from maestro.signals.validator import SignalValidator
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


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
    ) -> None:
        self.config = config
        self.registry = PluginRegistry.from_configs(config.strategies)
        self.datahub: BaseDataProvider = build_data_provider(config.datahub)
        self.portfolio_manager = PortfolioManager(config.strategies)
        self.risk_manager = RiskManager(config.portfolio.allowed_symbols, config.risk)
        self.execution = build_execution_engine(config.execution)
        self.approval_manager = ApprovalManager(
            config.approval,
            run_mode=config.mode,
            telegram_client=telegram_client,
        )
        self.state_store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
        self.audit = AuditLogger(config.audit.jsonl_path)
        self.live_order_client = live_order_client
        self.live_order_status_client = live_order_status_client
        self.live_order_notification_client = live_order_notification_client
        self.broker_reconciliation_service = broker_reconciliation_service
        self.telegram_client = telegram_client

    def run_once(self) -> RunOnceSummary:
        run_id = new_run_id()
        current_state = self.state_store.load_latest_portfolio_state()
        valid_results: list[TargetAllocationResult] = []
        strategy_ids = self.registry.strategy_ids
        validator = SignalValidator(self.config.portfolio.allowed_symbols, strategy_ids)
        data_requests_by_strategy = {}
        prices = {"CASH": 1.0}

        try:
            for loaded in self.registry.strategies:
                context = StrategyContext(
                    cycle_id=run_id,
                    timestamp=utc_now(),
                    run_mode=self.config.mode,
                    strategy_id=loaded.config.id,
                    portfolio_state=current_state.model_dump(mode="json"),
                    config=loaded.config.config,
                )
                requests = loaded.plugin.build_data_requests(context)
                data_requests_by_strategy[loaded.config.id] = [
                    request.model_dump(mode="json") for request in requests
                ]
                data_bundle = self.datahub.get_data(requests)
                prices.update(self._prices_from_bundle(data_bundle))
                result = loaded.plugin.run(data_bundle, context)
                validation = validator.validate(result)
                self.state_store.save_strategy_run(
                    run_id,
                    loaded.config.id,
                    {
                        "result": result.model_dump(mode="json"),
                        "validation": {"ok": validation.ok, "errors": validation.errors},
                    },
                )
                if not validation.ok:
                    raise ValueError(
                        f"Invalid strategy result for {loaded.config.id}: {validation.errors}"
                    )
                valid_results.append(result)

            target = self.portfolio_manager.build_target(valid_results)
            risk_decision = self.risk_manager.check(target)
            self.state_store.save_risk_decision(
                run_id,
                risk_decision.approved,
                risk_decision.model_dump(mode="json"),
            )
            if not risk_decision.approved:
                raise ValueError(f"Risk check failed: {risk_decision.violations}")

            orders = self.execution.propose_orders(current_state, risk_decision.target, prices)
            approval_request, approval_decision, approval_message = (
                self.approval_manager.request_approval(
                    run_id,
                    orders,
                    risk_decision.modifications,
                    risk_decision.violations,
                )
            )
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
                        "execution_skipped",
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
                execution_results, next_state = self.execution.execute_orders(current_state, orders)

            for order in orders:
                order_payload = order.model_dump(mode="json")
                order_payload["approval_status"] = (
                    approval_decision.status if approval_decision else "not_required"
                )
                self.state_store.save_order(run_id, order.order_id, order_payload)
            self.state_store.save_portfolio_snapshot(run_id, next_state)

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

    def _prices_from_bundle(self, data_bundle) -> dict[str, float]:
        prices = {}
        for symbol, payload in data_bundle.data.items():
            if isinstance(payload, dict) and "price" in payload:
                prices[symbol] = float(payload["price"])
            elif isinstance(payload, dict) and isinstance(payload.get("latest_price"), dict):
                prices[symbol] = float(payload["latest_price"]["price"])
            elif getattr(payload, "latest_price", None) is not None:
                prices[symbol] = float(payload.latest_price.price)
        return prices

    def _execute_live_approval_orders(
        self,
        run_id: str,
        orders: list[OrderIntent],
        approval_id: str,
        approval_decision: ApprovalDecision,
    ) -> tuple[list[LiveOrderLifecycleResult], PortfolioState]:
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
            )
            lifecycle_results.append(dependencies.lifecycle_service.run(request, approval_decision))
        return lifecycle_results, self.state_store.load_latest_portfolio_state()

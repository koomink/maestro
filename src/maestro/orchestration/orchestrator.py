import traceback
from datetime import UTC, datetime
from typing import Any

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
from maestro.safety.controls import SafetyControlService
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
        self.safety = SafetyControlService(self.state_store, self.audit)
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
        data_quality_issues: list[dict[str, Any]] = []
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
                data_quality_issues.extend(self._data_quality_issues(data_bundle))
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
            safety_state = self.safety.current_state()
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
                self.safety.record_blocked_execution(
                    run_id,
                    self.config.mode.value,
                    safety_state,
                    "before_approval",
                )
                next_state = current_state
                self.state_store.save_portfolio_snapshot(run_id, next_state)
                summary = RunOnceSummary(
                    run_id=run_id,
                    loaded_strategies=[strategy.config.id for strategy in self.registry.strategies],
                    orders_created=0,
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
                        "approval_request": None,
                        "approval_decision": None,
                        "paper_orders": [],
                        "execution_results": [],
                        "safety_state": safety_state.model_dump(mode="json"),
                        "live_blocks": live_blocks,
                        "execution_skipped": True,
                        "state_summary": next_state.summary(prices),
                    },
                )
                return summary
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
                    "stale_data_warning",
                    {"issues": data_quality_issues, "mode": self.config.mode.value},
                )
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

    def _data_quality_issues(self, data_bundle) -> list[dict[str, Any]]:
        issues = []
        for request in data_bundle.requests:
            payload = data_bundle.data.get(request.symbol)
            if not isinstance(payload, dict):
                issues.append(
                    {
                        "symbol": request.symbol,
                        "data_type": request.data_type,
                        "source": data_bundle.source,
                        "timestamp": None,
                        "reason": "missing_payload",
                    }
                )
                continue
            latest_price = payload.get("latest_price")
            timestamp = None
            source = data_bundle.source
            if isinstance(latest_price, dict):
                timestamp = latest_price.get("timestamp")
                source = latest_price.get("source") or source
            if payload.get("is_stale"):
                issues.append(
                    {
                        "symbol": request.symbol,
                        "data_type": request.data_type,
                        "source": source,
                        "timestamp": timestamp,
                        "reason": "stale",
                    }
                )
            if request.data_type == "price" and latest_price is None:
                issues.append(
                    {
                        "symbol": request.symbol,
                        "data_type": request.data_type,
                        "source": source,
                        "timestamp": timestamp,
                        "reason": "missing_latest_price",
                    }
                )
        return issues

    def _live_execution_blocks(
        self,
        run_id: str,
        orders: list[OrderIntent],
        data_quality_issues: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if self.config.mode != RunMode.LIVE_APPROVAL or not orders:
            return []
        blocks = []
        if data_quality_issues:
            payload = {"issues": data_quality_issues, "mode": self.config.mode.value}
            self._record_event(run_id, "stale_data_halt", payload)
            blocks.append({"event_type": "stale_data_halt", **payload})

        reconciliation_block = self._reconciliation_block()
        if reconciliation_block is not None:
            self._record_event(run_id, "broker_reconciliation_halt", reconciliation_block)
            blocks.append({"event_type": "broker_reconciliation_halt", **reconciliation_block})

        limit_block = self._daily_limit_block(orders)
        if limit_block is not None:
            self._record_event(run_id, "live_order_limit_halt", limit_block)
            blocks.append({"event_type": "live_order_limit_halt", **limit_block})

        instrument_block = self._instrument_validation_block(orders)
        if instrument_block is not None:
            self._record_event(run_id, "instrument_validation_halt", instrument_block)
            blocks.append({"event_type": "instrument_validation_halt", **instrument_block})

        if self.config.execution.daily_loss_limit is not None:
            payload = {
                "reason": "broker_pnl_normalization_unavailable",
                "daily_loss_limit": self.config.execution.daily_loss_limit,
            }
            self._record_event(run_id, "daily_loss_limit_halt", payload)
            blocks.append({"event_type": "daily_loss_limit_halt", **payload})
        return blocks

    def _reconciliation_block(self) -> dict[str, Any] | None:
        if not self.config.execution.require_reconciliation_pass:
            return None
        latest = self.state_store.load_latest_system_event("broker_reconciliation")
        if latest is None:
            return {"reason": "missing_reconciliation"}
        if latest["payload"].get("passed") is not True:
            return {
                "reason": "failed_reconciliation",
                "reconciliation": latest["payload"],
            }
        created_at = self._parse_store_created_at(latest["created_at"])
        age_seconds = (utc_now() - created_at).total_seconds()
        if age_seconds > self.config.reconciliation.max_age_seconds:
            return {
                "reason": "stale_reconciliation",
                "created_at": latest["created_at"],
                "age_seconds": age_seconds,
                "max_age_seconds": self.config.reconciliation.max_age_seconds,
            }
        return None

    def _daily_limit_block(self, orders: list[OrderIntent]) -> dict[str, Any] | None:
        today = utc_now().date().isoformat()
        existing_notional = 0.0
        existing_count = 0
        for row in self.state_store.list_system_events_by_type("live_order_result", limit=1000):
            payload = row["payload"]
            if payload.get("submitted_date") == today:
                existing_count += 1
                existing_notional += float(payload.get("notional", 0.0))
        proposed_notional = sum(order.notional for order in orders)
        proposed_count = len(orders)
        if existing_notional + proposed_notional > self.config.execution.max_daily_live_notional:
            return {
                "reason": "daily_notional_exceeded",
                "existing_notional": existing_notional,
                "proposed_notional": proposed_notional,
                "max_daily_live_notional": self.config.execution.max_daily_live_notional,
            }
        max_count = self.config.execution.max_daily_live_order_count
        if max_count > 0 and existing_count + proposed_count > max_count:
            return {
                "reason": "daily_order_count_exceeded",
                "existing_count": existing_count,
                "proposed_count": proposed_count,
                "max_daily_live_order_count": max_count,
            }
        return None

    def _instrument_validation_block(self, orders: list[OrderIntent]) -> dict[str, Any] | None:
        instruments = {
            instrument.symbol: instrument for instrument in self.config.universe.instruments
        }
        if not instruments:
            return None
        for order in orders:
            instrument = instruments.get(order.symbol)
            if instrument is None:
                return {"reason": "missing_instrument", "symbol": order.symbol}
            if instrument.currency.value != self.config.portfolio.base_currency:
                return {"reason": "currency_mismatch", "symbol": order.symbol}
            if instrument.broker_product != self.config.kis.broker_product:
                return {"reason": "broker_product_mismatch", "symbol": order.symbol}
            if order.quantity < instrument.min_order_quantity:
                return {"reason": "min_order_quantity", "symbol": order.symbol}
            if order.notional < instrument.min_order_notional:
                return {"reason": "min_order_notional", "symbol": order.symbol}
            if not self._is_step_multiple(order.quantity, instrument.quantity_step):
                return {"reason": "quantity_step", "symbol": order.symbol}
            if not self._is_step_multiple(order.price, instrument.price_tick):
                return {"reason": "price_tick", "symbol": order.symbol}
        return None

    def _record_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.state_store.save_system_event(run_id, event_type, payload)
        self.audit.log(run_id, event_type, payload)

    def _parse_store_created_at(self, value: str) -> datetime:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)

    def _is_step_multiple(self, value: float, step: float) -> bool:
        scaled = value / step
        return abs(scaled - round(scaled)) < 1e-9

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

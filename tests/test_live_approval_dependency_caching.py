from pathlib import Path
from typing import Any

import yaml

from maestro.approval.models import ApprovalDecision
from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.core.enums import Currency, OrderSide
from maestro.execution.base import OrderIntent
from maestro.execution.live_order_factory import LiveApprovalDependencies
from maestro.orchestration import orchestrator as orchestrator_module
from maestro.orchestration.orchestrator import MaestroOrchestrator


def test_dependencies_built_once_per_account_for_same_account_orders(tmp_path, monkeypatch):
    orchestrator = _orchestrator(tmp_path)
    factory = _CountingDependenciesFactory()
    monkeypatch.setattr(orchestrator_module, "build_live_approval_dependencies", factory)
    orders = [_order(f"ord-{i}", account_id="acct-a") for i in range(3)]
    approval_decision = _approval_decision("run-1")

    lifecycle_results, _ = orchestrator._execute_live_approval_orders(
        "run-1", orders, "appr-1", approval_decision
    )

    assert factory.calls == ["acct-a"]
    assert len(lifecycle_results) == 3


def test_dependencies_built_once_per_distinct_account(tmp_path, monkeypatch):
    orchestrator = _orchestrator(tmp_path)
    factory = _CountingDependenciesFactory()
    monkeypatch.setattr(orchestrator_module, "build_live_approval_dependencies", factory)
    orders = [
        _order("ord-a", account_id="acct-a"),
        _order("ord-b", account_id="acct-b"),
    ]
    approval_decision = _approval_decision("run-1")

    lifecycle_results, _ = orchestrator._execute_live_approval_orders(
        "run-1", orders, "appr-1", approval_decision
    )

    assert factory.calls == ["acct-a", "acct-b"]
    assert len(lifecycle_results) == 2


class _CountingDependenciesFactory:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def __call__(self, config, state_store, audit_logger, **kwargs) -> LiveApprovalDependencies:
        del config
        self.calls.append(kwargs.get("account_id"))
        return LiveApprovalDependencies(
            state_store=state_store,
            audit_logger=audit_logger,
            safety_service=None,
            status_service=None,
            fill_reconciliation_service=None,
            workflow_service=None,
            lifecycle_service=_FakeLifecycleService(),
        )


class _FakeLifecycleService:
    def run(self, request, approval_decision):
        del approval_decision
        return {"order_id": request.order_id, "account_id": request.account_id}


def _order(order_id: str, *, account_id: str | None) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        symbol="MOCK_ETF_A",
        side=OrderSide.BUY,
        quantity=1,
        price=100.0,
        notional=100.0,
        currency=Currency.KRW,
        account_id=account_id,
    )


def _approval_decision(run_id: str) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id="appr-1",
        run_id=run_id,
        status="approved",
        decided_at=utc_now(),
        decided_by="telegram:fake",
    )


def _orchestrator(tmp_path) -> MaestroOrchestrator:
    raw: dict[str, Any] = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["mode"] = "live_approval"
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    del raw["portfolio"]["initial_cash"]
    raw["portfolio"]["allowed_symbols"] = ["CASH", "MOCK_ETF_A", "MOCK_ETF_B"]
    raw["execution"] = {"engine": "paper"}
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "timeout_seconds": 1,
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
        "telegram_poll_interval_seconds": 0.0,
    }
    raw["kis"] = {
        "enabled": True,
        "provider": "mock",
        "account_id": "MOCK",
        "broker_products": ["kis_domestic_stock"],
    }
    config_path = tmp_path / "live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    config = load_config(config_path)
    config.execution.order_posture = "armed"
    config.execution.live_order_enabled = True
    config.execution.live_order_dry_run = False

    orchestrator = MaestroOrchestrator(config)
    orchestrator.state_store.save_portfolio_snapshot(
        "run_adopted_broker_baseline",
        orchestrator.state_store.load_latest_portfolio_state(),
    )
    return orchestrator

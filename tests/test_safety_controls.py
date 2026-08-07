from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus, SafetyState
from maestro.core.ids import new_run_id
from maestro.execution.brokers.readonly import BrokerBuyingPower
from maestro.execution.live_orders import (
    BrokerOrderId,
    BrokerReconciliationRunner,
    LiveOrderClient,
    LiveOrderLifecycleNotification,
    LiveOrderNotificationClient,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusClient,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.execution.reconciliation import ReconciliationResult
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.safety.controls import SafetyControlService
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


def test_default_safety_state_allows_paper_run(tmp_path):
    config = load_config(_paper_config_path(tmp_path))
    summary = MaestroOrchestrator(config).run_once()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)

    assert SafetyControlService(store, audit).current_state().state == SafetyState.ACTIVE
    assert summary.orders_created == 2


def test_paused_state_blocks_live_approval_before_approval(tmp_path):
    config = load_config(_live_approval_config_path(tmp_path))
    orchestrator = _live_orchestrator(config)
    SafetyControlService(orchestrator.state_store, orchestrator.audit).pause(
        new_run_id(),
        "operator pause",
    )
    orchestrator.state_store.save_system_event(
        "run_reconcile_initial",
        "broker_reconciliation",
        {"passed": True},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    assert orchestrator.telegram_client.sent_messages == []
    assert orchestrator.live_order_client.requests == []
    blocked = orchestrator.state_store.list_system_events_by_type("safety_execution_blocked")
    assert blocked[0]["payload"]["state"] == "paused"
    assert blocked[0]["payload"]["phase"] == "before_approval"


def test_killed_state_blocks_live_approval_before_approval(tmp_path):
    config = load_config(_live_approval_config_path(tmp_path))
    orchestrator = _live_orchestrator(config)
    SafetyControlService(orchestrator.state_store, orchestrator.audit).kill_switch(
        new_run_id(),
        "operator kill",
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 0
    assert orchestrator.telegram_client.sent_messages == []
    assert orchestrator.live_order_client.requests == []
    blocked = orchestrator.state_store.list_system_events_by_type("safety_execution_blocked")
    assert blocked[0]["payload"]["state"] == "killed"


def test_resume_from_paused_allows_live_approval_execution(tmp_path, monkeypatch):
    monkeypatch.setattr("maestro.approval.manager.new_approval_id", lambda: "appr_safety")
    config = load_config(_live_approval_config_path(tmp_path))
    orchestrator = _live_orchestrator(config, approval_id="appr_safety")
    safety = SafetyControlService(orchestrator.state_store, orchestrator.audit)
    safety.pause(new_run_id(), "operator pause")
    safety.resume(new_run_id(), "operator resume")
    orchestrator.state_store.save_system_event(
        "run_reconcile_initial",
        "broker_reconciliation",
        {"passed": True},
    )

    summary = orchestrator.run_once()

    assert summary.orders_created == 2
    assert len(orchestrator.live_order_client.requests) == 2
    assert not orchestrator.state_store.list_system_events_by_type("safety_execution_blocked")


def test_kill_switch_cannot_be_reset_by_resume(tmp_path):
    config = load_config(_paper_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    safety = SafetyControlService(store, audit)

    safety.kill_switch(new_run_id(), "operator kill")

    with pytest.raises(ValueError, match="cannot be reset"):
        safety.resume(new_run_id(), "operator resume")
    assert safety.current_state().state == SafetyState.KILLED


def test_release_kill_transitions_to_active_with_audit_event(tmp_path):
    config = load_config(_paper_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    safety = SafetyControlService(store, audit)

    killed = safety.kill_switch(new_run_id(), "operator kill")
    current = safety.release_kill(new_run_id(), "operator reviewed")

    events = store.list_system_events_by_type("safety_kill_released")
    assert current.state == SafetyState.ACTIVE
    assert events[0]["payload"]["reason"] == "operator reviewed"
    assert events[0]["payload"]["source"] == "cli"
    assert events[0]["payload"]["previous_reason"] == "operator kill"
    assert events[0]["payload"]["killed_at"] == killed.created_at
    assert "safety_kill_released" in audit.path.read_text()


def test_release_kill_does_not_record_release_event_if_transition_fails(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    config = load_config(_paper_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    safety = SafetyControlService(store, audit)
    safety.kill_switch(new_run_id(), "operator kill")

    def fail_transition(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("transition failed")

    monkeypatch.setattr(safety, "_transition", fail_transition)

    with pytest.raises(RuntimeError, match="transition failed"):
        safety.release_kill(new_run_id(), "operator reviewed")

    assert store.list_system_events_by_type("safety_kill_released") == []


def test_release_kill_requires_killed_state(tmp_path):
    config = load_config(_paper_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    safety = SafetyControlService(store, audit)

    with pytest.raises(ValueError, match="requires current state to be killed"):
        safety.release_kill(new_run_id(), "operator reviewed")

    assert safety.current_state().state == SafetyState.ACTIVE


def test_clear_halt_requires_halted_state_and_reason(tmp_path):
    config = load_config(_paper_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    safety = SafetyControlService(store, audit)

    with pytest.raises(ValueError, match="halted"):
        safety.clear_halt(new_run_id(), "not halted")

    safety.halt(new_run_id(), "unknown broker state")
    current = safety.clear_halt(new_run_id(), "broker state reconciled")

    assert current.state == SafetyState.ACTIVE
    events = store.list_system_events_by_type("safety_state", limit=2)
    assert events[0]["payload"]["reason"] == "broker state reconciled"


def test_clear_halt_cannot_reset_kill_switch(tmp_path):
    config = load_config(_paper_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    safety = SafetyControlService(store, audit)

    safety.kill_switch(new_run_id(), "operator kill")

    with pytest.raises(ValueError, match="cannot be reset"):
        safety.clear_halt(new_run_id(), "unsafe")


def test_safety_cli_transitions_are_persisted(tmp_path):
    config_path = _paper_config_path(tmp_path)
    runner = CliRunner()

    pause = runner.invoke(app, ["pause", "--config", str(config_path), "--reason", "maintenance"])
    status = runner.invoke(app, ["safety-status", "--config", str(config_path)])
    resume = runner.invoke(app, ["resume", "--config", str(config_path), "--reason", "done"])

    assert pause.exit_code == 0
    assert "state=paused" in pause.output
    assert status.exit_code == 0
    assert "state=paused" in status.output
    assert resume.exit_code == 0
    assert "state=active" in resume.output


def test_clear_halt_cli_blocks_when_recovery_health_has_failures(tmp_path):
    config_path = _paper_config_path(tmp_path)
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    SafetyControlService(store, audit).halt(new_run_id(), "production hardening gate")
    store.save_system_event(new_run_id(), "broker_reconciliation", {"passed": False})

    result = CliRunner().invoke(
        app,
        ["clear-halt", "--config", str(config_path), "--reason", "operator reviewed"],
    )

    assert result.exit_code != 0
    assert "recovery_preflight_failed" in result.output
    assert "reconciliation" in result.output
    assert SafetyControlService(store, audit).current_state().state == SafetyState.HALTED


def test_clear_halt_cli_allows_recovery_when_only_safety_state_is_failed(tmp_path):
    config_path = _paper_config_path(tmp_path)
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    SafetyControlService(store, audit).halt(new_run_id(), "manual review needed")

    result = CliRunner().invoke(
        app,
        ["clear-halt", "--config", str(config_path), "--reason", "operator reviewed"],
    )

    assert result.exit_code == 0
    assert "state=active" in result.output
    assert SafetyControlService(store, audit).current_state().state == SafetyState.ACTIVE


def test_release_kill_cli_rejects_bad_confirmation_and_keeps_state(tmp_path):
    config_path = _paper_config_path(tmp_path)
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    SafetyControlService(store, audit).kill_switch(new_run_id(), "operator kill")

    result = CliRunner().invoke(
        app,
        [
            "release-kill",
            "--config",
            str(config_path),
            "--reason",
            "operator reviewed",
            "--confirm",
            "RELEASE_KILL",
        ],
    )

    assert result.exit_code != 0
    assert "release-kill requires --confirm RELEASE-KILL" in result.output
    assert SafetyControlService(store, audit).current_state().state == SafetyState.KILLED


def test_release_kill_cli_allows_recovery_when_only_safety_state_is_failed(tmp_path):
    config_path = _paper_config_path(tmp_path)
    config = load_config(config_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    SafetyControlService(store, audit).kill_switch(new_run_id(), "operator kill")

    result = CliRunner().invoke(
        app,
        [
            "release-kill",
            "--config",
            str(config_path),
            "--reason",
            "operator reviewed",
            "--confirm",
            "RELEASE-KILL",
        ],
    )

    assert result.exit_code == 0
    assert "state=active" in result.output
    assert SafetyControlService(store, audit).current_state().state == SafetyState.ACTIVE


class FakeTelegramClient:
    def __init__(self, approval_id: str) -> None:
        self.approval_id = approval_id
        self.sent_messages: list[dict[str, Any]] = []

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.sent_messages.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return {"ok": True, "result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, *, offset: int | None, timeout_seconds: int) -> dict[str, Any]:
        return {
            "ok": True,
            "result": [
                {
                    "update_id": 1,
                    "callback_query": {
                        "id": "callback-1",
                        "data": f"approve:{self.approval_id}",
                        "message": {
                            "chat": {"id": 100},
                            "message_id": 1,
                            "text": "Maestro approval request",
                        },
                        "from": {"id": 100, "username": "approver"},
                    },
                }
            ],
        }


class FakeLiveOrderClient(LiveOrderClient):
    def __init__(self) -> None:
        self.requests: list[LiveOrderRequest] = []

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        self.requests.append(request)
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            broker_order=BrokerOrderId(
                broker="fake",
                order_id=request.order_id,
                broker_order_id=f"broker:{request.order_id}",
                submitted_at=utc_now().isoformat(),
            ),
        )

    def get_buying_power(self, symbol, order_price, currency=None):
        return BrokerBuyingPower(
            symbol=symbol,
            order_price=order_price,
            cash_buying_power=1_000_000_000,
            currency=currency,
            max_buy_quantity=1_000_000,
            source="test",
        )


class FakeStatusClient(LiveOrderStatusClient):
    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        return LiveOrderStatusSnapshot(
            broker_order=broker_order_id,
            status=OrderStatus.FILLED,
            checked_at=utc_now().isoformat(),
            symbol="MOCK_ETF_A",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=1.0,
                filled_quantity=1.0,
                remaining_quantity=0.0,
                average_fill_price=1.0,
                fill_count=1,
            ),
        )


class FakeNotificationClient(LiveOrderNotificationClient):
    def notify(self, event: LiveOrderLifecycleNotification) -> None:
        pass


class FakeBrokerReconciliation(BrokerReconciliationRunner):
    def reconcile_latest(self, run_id: str | None = None) -> ReconciliationResult:
        return ReconciliationResult(
            run_id=run_id or "run_reconcile",
            passed=True,
            issues=[],
            cash_difference=0.0,
            value_difference=0.0,
            broker_account_id="MOCK",
        )


def _paper_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    config_path = tmp_path / "paper.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _live_approval_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["mode"] = "live_approval"
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    del raw["portfolio"]["initial_cash"]
    raw["portfolio"]["allowed_symbols"] = ["CASH", "MOCK_ETF_A", "MOCK_ETF_B"]
    raw["execution"] = {
        "engine": "paper",
        "live_order_enabled": True,
        "require_reconciliation_pass": True,
        "live_order_limits": {
            "max_order_notional": 5_000_000.0,
            "max_daily_notional": 10_000_000.0,
        },
        "order_status_max_polls": 1,
        "order_status_poll_interval_seconds": 0.0,
    }
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "timeout_seconds": 1,
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
        "telegram_poll_interval_seconds": 0.0,
    }
    raw["kis"] = {"enabled": True, "provider": "mock", "account_id": "MOCK"}
    config_path = tmp_path / "live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _live_orchestrator(config, approval_id: str = "appr_safety") -> MaestroOrchestrator:
    orchestrator = MaestroOrchestrator(
        config,
        live_order_client=FakeLiveOrderClient(),
        live_order_status_client=FakeStatusClient(),
        live_order_notification_client=FakeNotificationClient(),
        broker_reconciliation_service=FakeBrokerReconciliation(),
        telegram_client=FakeTelegramClient(approval_id),
    )
    orchestrator.state_store.save_portfolio_snapshot(
        "run_adopted_broker_baseline",
        PortfolioState(cash=10_000_000.0, positions={}),
    )
    return orchestrator

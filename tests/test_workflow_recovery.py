from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.core.enums import Currency, OrderSide, OrderStatus
from maestro.execution.live_order_models import (
    BrokerOrderId,
    FillEvent,
    LiveOrderRequest,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.ops.workflow_recovery import WorkflowRecoveryService
from maestro.state.store import StateStore


def test_workflow_recovery_matches_toss_history_and_applies_fill(monkeypatch, tmp_path):
    config = _config(tmp_path)
    store = StateStore(config.state.sqlite_path, initial_cash=1_000)
    audit = AuditLogger(config.audit.jsonl_path)
    request = _request()
    store.save_system_event(
        request.run_id,
        "live_order_recovery_required",
        {
            "reason": "ambiguous_submit",
            "order_id": request.order_id,
            "request": request.model_dump(mode="json"),
            "result": {"status": "halted"},
        },
    )
    _install_fake_recovery_dependencies(monkeypatch, matched=True)
    service = WorkflowRecoveryService(config, store, audit)
    monkeypatch.setattr(service, "_refresh_snapshots", lambda run_id, account_ids: [11])

    result = service.recover_live_orders(
        reason="automatic Telegram recovery",
        decided_by="telegram:test",
    )

    assert result.status == "completed"
    assert result.applied_fill_count == 1
    assert result.resolved_orders[0]["broker_order_id"] == "TOSS-RECOVERED"
    assert store.load_latest_portfolio_state().positions["AAPL"] == 2
    assert store.list_system_events_by_type("live_order_recovery_resolution")
    assert store.list_system_events_by_type("live_order_recovery_completed")


def test_workflow_recovery_requires_attestation_for_unmatched_toss_order(
    monkeypatch,
    tmp_path,
):
    config = _config(tmp_path)
    store = StateStore(config.state.sqlite_path, initial_cash=1_000)
    audit = AuditLogger(config.audit.jsonl_path)
    request = _request()
    store.save_system_event(
        request.run_id,
        "live_order_recovery_required",
        {
            "reason": "ambiguous_submit",
            "order_id": request.order_id,
            "request": request.model_dump(mode="json"),
            "result": {"status": "halted"},
        },
    )
    _install_fake_recovery_dependencies(monkeypatch, matched=False)
    service = WorkflowRecoveryService(config, store, audit)
    monkeypatch.setattr(service, "_refresh_snapshots", lambda run_id, account_ids: [12])

    first = service.recover_live_orders(
        reason="automatic Telegram recovery",
        decided_by="telegram:test",
    )

    assert first.status == "attestation_required"
    assert not store.list_system_events_by_type("live_order_recovery_completed")

    completed = service.recover_live_orders(
        reason="broker app verified",
        decided_by="telegram:test",
        expected_fingerprint=first.fingerprint,
        manual_attestation=True,
    )

    assert completed.status == "completed"
    assert store.list_system_events_by_type("live_order_recovery_attestation")


def test_workflow_recovery_rejects_stale_fingerprint(tmp_path):
    config = _config(tmp_path)
    store = StateStore(config.state.sqlite_path, initial_cash=1_000)
    audit = AuditLogger(config.audit.jsonl_path)
    service = WorkflowRecoveryService(config, store, audit)

    try:
        service.recover_live_orders(
            reason="stale callback",
            decided_by="telegram:test",
            expected_fingerprint="stale",
        )
    except ValueError as exc:
        assert "state changed" in str(exc)
    else:
        raise AssertionError("stale recovery callback was not rejected")


def test_workflow_recovery_keeps_block_when_reconciliation_fails(monkeypatch, tmp_path):
    config = _config(tmp_path)
    store = StateStore(config.state.sqlite_path, initial_cash=1_000)
    audit = AuditLogger(config.audit.jsonl_path)
    request = _request()
    store.save_system_event(
        request.run_id,
        "live_order_recovery_required",
        {
            "reason": "ambiguous_submit",
            "order_id": request.order_id,
            "request": request.model_dump(mode="json"),
            "result": {"status": "halted"},
        },
    )
    _install_fake_recovery_dependencies(
        monkeypatch,
        matched=True,
        reconciliation_passed=False,
    )
    service = WorkflowRecoveryService(config, store, audit)
    monkeypatch.setattr(service, "_refresh_snapshots", lambda run_id, account_ids: [13])

    with pytest.raises(ValueError, match="Broker reconciliation failed"):
        service.recover_live_orders(
            reason="automatic Telegram recovery",
            decided_by="telegram:test",
        )

    assert not store.list_system_events_by_type("live_order_recovery_completed")
    assert service.preview().blockers


def _install_fake_recovery_dependencies(
    monkeypatch,
    *,
    matched: bool,
    reconciliation_passed: bool = True,
):
    class FakeTossClient:
        def __init__(self, account, instruments):
            del account, instruments

        def _broker_symbol(self, symbol):
            return symbol

        def list_orders(self, *, status, **kwargs):
            del kwargs
            if not matched or status == "OPEN":
                return []
            return [_filled_snapshot()]

    class FakeReconciliationService:
        def __init__(self, config, state_store, audit, account_ids=None):
            del config, audit, account_ids
            self.store = state_store

        def reconcile_latest(self, run_id):
            self.store.save_system_event(
                run_id,
                "broker_reconciliation",
                {
                    "passed": reconciliation_passed,
                    "checked_at": utc_now().isoformat(),
                    "issues": [] if reconciliation_passed else [{"issue_type": "cash_mismatch"}],
                },
            )
            return SimpleNamespace(
                passed=reconciliation_passed,
                issues=[] if reconciliation_passed else ["cash_mismatch"],
            )

    monkeypatch.setattr(
        "maestro.ops.workflow_recovery.TossLiveOrderClient",
        FakeTossClient,
    )
    monkeypatch.setattr(
        "maestro.ops.workflow_recovery.BrokerReconciliationService",
        FakeReconciliationService,
    )


def _filled_snapshot() -> LiveOrderStatusSnapshot:
    now = utc_now().isoformat()
    return LiveOrderStatusSnapshot(
        broker_order=BrokerOrderId(
            broker="toss",
            broker_order_id="TOSS-RECOVERED",
            order_id="",
            submitted_at=now,
            account_id="toss_brokerage",
        ),
        status=OrderStatus.FILLED,
        checked_at=now,
        symbol="AAPL",
        side=OrderSide.BUY,
        partial_fill=PartialFillSummary(
            ordered_quantity=2,
            filled_quantity=2,
            remaining_quantity=0,
            average_fill_price=100,
            fill_count=1,
        ),
        fills=[
            FillEvent(
                broker_order_id="TOSS-RECOVERED",
                symbol="AAPL",
                quantity=2,
                price=100,
                filled_at=now,
            )
        ],
        currency="USD",
        cumulative_filled_amount=200,
        raw_status="FILLED",
        raw={
            "result": {
                "orderId": "TOSS-RECOVERED",
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": "2",
                "price": "100",
                "orderedAt": now,
            }
        },
    )


def _request() -> LiveOrderRequest:
    return LiveOrderRequest(
        order_id="ord_ambiguous",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=2,
        limit_price=100,
        approval_id="appr_1",
        run_id="run_1",
        currency=Currency.USD,
        account_id="toss_brokerage",
    )


def _config(tmp_path):
    raw = yaml.safe_load(
        Path("tests/fixtures/configs/live_approval_us_etf.yaml").read_text()
    )
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["accounts"] = [
        {
            "id": "toss_brokerage",
            "broker": "toss",
            "enabled": True,
            "client_id_env": "TOSS_CLIENT_ID",
            "client_secret_env": "TOSS_CLIENT_SECRET",
            "account_seq": 7,
        }
    ]
    path = tmp_path / "recovery.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_config(path)

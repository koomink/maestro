import pytest

from maestro.approval.models import ApprovalDecision
from maestro.config.loader import load_config
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus, OrderType
from maestro.core.ids import new_run_id
from maestro.execution.live_order_safety import _duplicate_key
from maestro.execution.live_orders import (
    BrokerOrderId,
    LiveOrderClient,
    LiveOrderPreSubmitValidator,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderSafetyService,
    LiveOrderStatusClient,
    LiveOrderStatusService,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.live_gates import LiveExecutionGateService
from maestro.state.store import StateStore


def test_live_order_requires_enabled_config(tmp_path):
    service, request, approval, broker = _context(tmp_path, enabled=False)
    _save_passed_reconciliation(service.state_store, request.run_id)

    with pytest.raises(ValueError, match="disabled"):
        service.submit_approved_order(request, approval)

    assert broker.requests == []


def test_live_order_requires_telegram_approval(tmp_path):
    service, request, _, broker = _context(tmp_path)
    _save_passed_reconciliation(service.state_store, request.run_id)
    rejected = _approval(request, status="rejected")

    with pytest.raises(ValueError, match="Telegram approval"):
        service.submit_approved_order(request, rejected)

    assert broker.requests == []


def test_live_order_rejects_non_telegram_approval_source(tmp_path):
    service, request, _, broker = _context(tmp_path)
    _save_passed_reconciliation(service.state_store, request.run_id)
    approval = _approval(request, decided_by="console:default_decision")

    with pytest.raises(ValueError, match="Telegram approval"):
        service.submit_approved_order(request, approval)

    assert broker.requests == []


def test_live_order_requires_latest_reconciliation_pass(tmp_path):
    service, request, approval, broker = _context(tmp_path)
    service.state_store.save_system_event(
        request.run_id,
        "broker_reconciliation",
        {"passed": False, "issues": [{"issue_type": "cash_mismatch"}]},
    )

    with pytest.raises(ValueError, match="reconciliation"):
        service.submit_approved_order(request, approval)

    assert broker.requests == []


def test_live_order_rejects_market_orders(tmp_path):
    service, _, _, broker = _context(tmp_path)

    with pytest.raises(ValueError, match="limit"):
        LiveOrderRequest(
            order_id="ord_live_1",
            symbol="005930",
            side=OrderSide.BUY,
            quantity=1,
            limit_price=70_000,
            order_type=OrderType.MARKET,
            approval_id="appr_1",
            run_id=new_run_id(),
        )

    assert broker.requests == []


def test_live_order_enforces_per_order_cap(tmp_path):
    service, request, approval, broker = _context(tmp_path, max_order=10_000)
    _save_passed_reconciliation(service.state_store, request.run_id)

    with pytest.raises(ValueError, match="per-order cap"):
        service.submit_approved_order(request, approval)

    assert broker.requests == []


def test_live_order_enforces_daily_cap(tmp_path):
    service, request, approval, broker = _context(tmp_path, max_daily=200_000)
    _save_passed_reconciliation(service.state_store, request.run_id)
    first = request.model_copy(update={"order_id": "ord_live_first", "duplicate_key": "first"})
    service.submit_approved_order(first, approval)

    with pytest.raises(ValueError, match="daily cap"):
        service.submit_approved_order(request, approval)

    assert len(broker.requests) == 1


def test_live_order_daily_cap_counts_recovery_required_submission_after_recovery(tmp_path):
    service, request, approval, broker = _context(
        tmp_path,
        max_daily=200_000,
        broker=FakeFailingLiveOrderClient(),
    )
    _save_passed_reconciliation(service.state_store, request.run_id)

    first = service.submit_approved_order(request, approval)
    service.state_store.save_system_event(
        "run_recovery",
        "live_order_recovery_completed",
        {"reason": "operator reconciled ambiguous submit"},
    )
    retry = request.model_copy(update={"order_id": "ord_live_retry", "duplicate_key": "retry"})

    with pytest.raises(ValueError, match="daily cap"):
        service.submit_approved_order(retry, _approval(retry))

    assert first.status == OrderStatus.HALTED
    assert len(broker.requests) == 1


def test_live_order_daily_cap_ignores_non_submit_recovery_required_events(tmp_path):
    service, request, approval, broker = _context(tmp_path, max_daily=200_000)
    _save_passed_reconciliation(service.state_store, request.run_id)
    service.state_store.save_system_event(
        "run_status_poll",
        "live_order_recovery_required",
        {
            "reason": "unknown_broker_order_status",
            "request": {"order_id": "ord_status_poll", "currency": "KRW"},
            "notional": 140_000.0,
        },
    )

    result = service.submit_approved_order(request, approval)

    assert result.status == OrderStatus.ACCEPTED_BY_BROKER
    assert broker.requests == [request]


def test_live_order_prevents_duplicates(tmp_path):
    service, request, approval, broker = _context(tmp_path)
    _save_passed_reconciliation(service.state_store, request.run_id)

    service.submit_approved_order(request, approval)

    with pytest.raises(ValueError, match="Duplicate"):
        service.submit_approved_order(request, approval)

    assert len(broker.requests) == 1


def test_live_order_duplicate_key_fallback_is_stable_without_timestamp(tmp_path):
    _, request, _, _ = _context(tmp_path)
    request = request.model_copy(update={"duplicate_key": None})
    retry = request.model_copy(update={"run_id": new_run_id(), "duplicate_key": None})

    assert _duplicate_key(request) == _duplicate_key(retry)
    assert _duplicate_key(request) == "appr_1:005930:buy:2.0:70000.0"


def test_live_order_persists_accepted_result(tmp_path):
    service, request, approval, broker = _context(tmp_path)
    _save_passed_reconciliation(service.state_store, request.run_id)

    result = service.submit_approved_order(request, approval)

    intent_events = service.state_store.list_system_events_by_type("live_order_submit_intent")
    events = service.state_store.list_system_events_by_type("live_order_result")
    assert result.status == OrderStatus.ACCEPTED_BY_BROKER
    assert broker.requests == [request]
    assert intent_events[0]["payload"]["phase"] == "submit_intent"
    assert intent_events[0]["payload"]["duplicate_key"] == "intent:strategy-signal-1"
    assert events[0]["payload"]["request"]["order_id"] == request.order_id
    assert events[0]["payload"]["result"]["broker_order"]["broker_order_id"] == "KIS-1"
    service.state_store.save_system_event(
        request.run_id,
        "live_order_lifecycle",
        {"order_id": request.order_id, "final_status": "accepted_by_broker"},
    )
    assert _recovery_block(tmp_path, service.state_store) is None


def test_live_order_runs_pre_submit_validation_before_broker_submit(tmp_path):
    service, request, approval, broker = _context(tmp_path, broker=FakePreSubmitRejectingClient())
    _save_passed_reconciliation(service.state_store, request.run_id)

    with pytest.raises(ValueError, match="pre-submit"):
        service.submit_approved_order(request, approval)

    assert broker.requests == []
    intent_events = service.state_store.list_system_events_by_type("live_order_submit_intent")
    assert intent_events == []
    assert service.state_store.list_system_events_by_type("live_order_recovery_required") == []
    assert _recovery_block(tmp_path, service.state_store) is None


def test_live_order_submit_intent_without_result_blocks_recovery_gate(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    request = _live_request()
    store.save_system_event(
        request.run_id,
        "live_order_submit_intent",
        _submit_intent_payload(request),
    )

    block = _recovery_block(tmp_path, store)

    assert block is not None
    assert block["reason"] == "live_order_intent_without_result"
    assert block["order_id"] == request.order_id


def test_live_order_recovery_gate_checks_older_unresolved_submit_intents(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    orphan = _live_request().model_copy(
        update={"order_id": "ord_orphan", "duplicate_key": "orphan"}
    )
    resolved = _live_request().model_copy(
        update={"order_id": "ord_resolved", "duplicate_key": "resolved"}
    )
    store.save_system_event(
        orphan.run_id,
        "live_order_submit_intent",
        _submit_intent_payload(orphan),
    )
    store.save_system_event(
        resolved.run_id,
        "live_order_submit_intent",
        _submit_intent_payload(resolved),
    )
    store.save_system_event(
        resolved.run_id,
        "live_order_result",
        {
            "request": resolved.model_dump(mode="json"),
            "duplicate_key": _duplicate_key(resolved),
            "result": {"status": "accepted_by_broker"},
        },
    )

    block = _recovery_block(tmp_path, store)

    assert block is not None
    assert block["reason"] == "live_order_intent_without_result"
    assert block["order_id"] == "ord_orphan"


def test_live_order_submit_intent_recovery_completion_clears_gate(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    request = _live_request()
    store.save_system_event(
        request.run_id,
        "live_order_submit_intent",
        _submit_intent_payload(request),
    )
    store.save_system_event(
        "run_recover",
        "live_order_recovery_completed",
        {"broker_reconciliation_event_id": 1, "fill_reconciliation": {}},
    )

    assert _recovery_block(tmp_path, store) is None


def test_live_order_requires_recovery_on_unknown_submit_response(tmp_path):
    service, request, approval, broker = _context(tmp_path, broker_status=OrderStatus.UNKNOWN)
    _save_passed_reconciliation(service.state_store, request.run_id)

    result = service.submit_approved_order(request, approval)

    events = service.state_store.list_system_events_by_type("live_order_recovery_required")
    assert result.status == OrderStatus.HALTED
    assert len(events) == 1
    assert events[0]["payload"]["result"]["status"] == "halted"
    assert events[0]["payload"]["result"]["raw"]["recovery_required"] is True
    assert events[0]["payload"]["result"]["raw"]["reason"] == "unknown_broker_submit_response"
    assert "operator recovery is required" in events[0]["payload"]["result"]["message"]
    assert broker.requests == [request]


def test_live_order_status_service_persists_status_snapshot(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    broker_order = _broker_order()
    service = LiveOrderStatusService(
        store,
        audit,
        FakeLiveOrderStatusClient(OrderStatus.PARTIALLY_FILLED),
    )

    snapshot = service.poll_order_status("run_1", broker_order)

    events = store.list_system_events_by_type("live_order_status")
    assert snapshot.status == OrderStatus.PARTIALLY_FILLED
    assert events[0]["payload"]["status"] == "partially_filled"
    assert events[0]["payload"]["broker_order"]["broker_order_id"] == "KIS-1"
    assert events[0]["payload"]["partial_fill"]["filled_quantity"] == 1.0


def test_live_order_status_service_halts_unknown_status(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    service = LiveOrderStatusService(
        store,
        audit,
        FakeLiveOrderStatusClient(OrderStatus.UNKNOWN),
    )

    snapshot = service.poll_order_status("run_1", _broker_order())

    events = store.list_system_events_by_type("live_order_status")
    assert snapshot.status == OrderStatus.HALTED
    assert "unknown order state" in (snapshot.message or "")
    assert events[0]["payload"]["status"] == "halted"


def _context(
    tmp_path,
    *,
    enabled: bool = True,
    max_order: float = 200_000,
    max_daily: float = 300_000,
    broker_status: OrderStatus = OrderStatus.ACCEPTED_BY_BROKER,
    broker: "FakeLiveOrderClient | None" = None,
):
    run_id = new_run_id()
    request = _live_request(run_id=run_id)
    approval = _approval(request)
    store = StateStore(str(tmp_path / "state.db"), initial_cash=1_000_000)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    broker = broker or FakeLiveOrderClient(broker_status)
    service = LiveOrderSafetyService(
        ExecutionConfig(
            live_order_enabled=enabled,
            max_live_order_notional=max_order,
            max_daily_live_notional=max_daily,
        ),
        store,
        audit,
        broker,
    )
    return service, request, approval, broker


def _live_request(run_id: str | None = None) -> LiveOrderRequest:
    return LiveOrderRequest(
        order_id="ord_live_1",
        symbol="005930",
        side=OrderSide.BUY,
        quantity=2,
        limit_price=70_000,
        approval_id="appr_1",
        run_id=run_id or new_run_id(),
        duplicate_key="strategy-signal-1",
    )


def _submit_intent_payload(request: LiveOrderRequest) -> dict:
    return {
        "signal_run_id": request.signal_run_id,
        "request": request.model_dump(mode="json"),
        "duplicate_key": f"intent:{_duplicate_key(request)}",
        "notional": request.notional,
        "submitted_date": utc_now().date().isoformat(),
        "phase": "submit_intent",
    }


def _recovery_block(tmp_path, store: StateStore) -> dict | None:
    config = load_config("configs/live_approval.yaml")
    audit = AuditLogger(str(tmp_path / "gate_audit.jsonl"))
    return LiveExecutionGateService(config, store, audit)._live_recovery_block()


def _broker_order() -> BrokerOrderId:
    return BrokerOrderId(
        broker="kis",
        broker_order_id="KIS-1",
        broker_order_org_no="KRX",
        order_id="ord_live_1",
        submitted_at=utc_now().isoformat(),
    )


def _approval(
    request: LiveOrderRequest,
    *,
    status: str = "approved",
    decided_by: str = "telegram:fake",
) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id=request.approval_id,
        run_id=request.run_id,
        status=status,
        decided_at=utc_now(),
        decided_by=decided_by,
    )


def _save_passed_reconciliation(store: StateStore, run_id: str) -> None:
    store.save_system_event(run_id, "broker_reconciliation", {"passed": True, "issues": []})


class FakeLiveOrderClient(LiveOrderClient):
    def __init__(self, status: OrderStatus) -> None:
        self.status = status
        self.requests: list[LiveOrderRequest] = []

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        self.requests.append(request)
        broker_order = None
        if self.status != OrderStatus.UNKNOWN:
            broker_order = BrokerOrderId(
                broker="kis",
                broker_order_id="KIS-1",
                broker_order_org_no="KRX",
                order_id=request.order_id,
                submitted_at=utc_now().isoformat(),
            )
        return LiveOrderResult(
            order_id=request.order_id,
            status=self.status,
            broker_order=broker_order,
        )


class FakeFailingLiveOrderClient(LiveOrderClient):
    def __init__(self) -> None:
        self.requests: list[LiveOrderRequest] = []

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        self.requests.append(request)
        raise RuntimeError("broker submit timed out")


class FakePreSubmitRejectingClient(FakeLiveOrderClient, LiveOrderPreSubmitValidator):
    def __init__(self) -> None:
        super().__init__(OrderStatus.ACCEPTED_BY_BROKER)

    def validate_pre_submit_order(self, request: LiveOrderRequest) -> None:
        raise ValueError("pre-submit rejected")


class FakeLiveOrderStatusClient(LiveOrderStatusClient):
    def __init__(self, status: OrderStatus) -> None:
        self.status = status

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        return LiveOrderStatusSnapshot(
            broker_order=broker_order_id,
            status=self.status,
            checked_at=utc_now().isoformat(),
            symbol="005930",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=2.0,
                filled_quantity=1.0,
                remaining_quantity=1.0,
                average_fill_price=70_000.0,
                fill_count=1,
            ),
            raw_status=self.status.value,
        )

import pytest

from maestro.approval.models import ApprovalDecision
from maestro.config.models import ExecutionConfig
from maestro.core.clock import utc_now
from maestro.core.enums import Currency, OrderSide, OrderStatus
from maestro.execution.live_order_models import (
    BrokerOrderId,
    LiveOrderModifyRequest,
    LiveOrderModifyResult,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.execution.live_order_modification import LiveOrderModificationService
from maestro.execution.live_order_ports import LiveOrderModifyClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def test_approved_open_order_modification_is_persisted(tmp_path):
    service, store, client, request, approval = _context(tmp_path)

    result = service.modify_order(request, approval)

    assert result.broker_order.broker_order_id == "TOSS-2"
    assert client.requests == [request]
    event = store.load_latest_system_event("live_order_modify")
    assert event["payload"]["previous_broker_order_id"] == "TOSS-1"
    assert event["payload"]["replacement_broker_order_id"] == "TOSS-2"


def test_modification_requires_telegram_approval(tmp_path):
    service, _, client, request, approval = _context(tmp_path)
    approval = approval.model_copy(update={"decided_by": "cli"})

    with pytest.raises(ValueError, match="Telegram approval"):
        service.modify_order(request, approval)

    assert client.requests == []


def test_modification_revalidates_remaining_notional(tmp_path):
    service, _, client, request, approval = _context(tmp_path, max_order_notional=100.0)

    with pytest.raises(ValueError, match="per-order cap"):
        service.modify_order(request, approval)

    assert client.requests == []


def test_modification_rejects_quantity_above_latest_remaining(tmp_path):
    service, _, client, request, approval = _context(tmp_path)
    request = request.model_copy(update={"quantity": 3.0})

    with pytest.raises(ValueError, match="exceeds remaining quantity"):
        service.modify_order(request, approval)

    assert client.requests == []


def _context(tmp_path, *, max_order_notional=1_000.0):
    store = StateStore(str(tmp_path / "state.db"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    broker_order = BrokerOrderId(
        broker="toss",
        broker_order_id="TOSS-1",
        order_id="ord_1",
        submitted_at=utc_now().isoformat(),
        account_id="toss_brokerage",
    )
    request = LiveOrderModifyRequest(
        run_id="run_modify",
        approval_id="appr_modify",
        broker_order=broker_order,
        symbol="AAPL",
        limit_price=186.0,
        currency=Currency.USD,
    )
    approval = ApprovalDecision(
        approval_id=request.approval_id,
        run_id=request.run_id,
        status="approved",
        decided_at=utc_now(),
        decided_by="telegram:42",
    )
    store.save_system_event(
        "run_reconcile",
        "broker_reconciliation",
        {"passed": True},
    )
    store.save_system_event(
        "run_status",
        "live_order_status",
        LiveOrderStatusSnapshot(
            broker_order=broker_order,
            status=OrderStatus.OPEN,
            checked_at=utc_now().isoformat(),
            symbol="AAPL",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=2.0,
                filled_quantity=0.0,
                remaining_quantity=2.0,
            ),
        ).model_dump(mode="json"),
    )
    client = FakeModifyClient()
    service = LiveOrderModificationService(
        ExecutionConfig(
            live_order_enabled=True,
            max_live_order_notional=max_order_notional,
            max_daily_live_notional=10_000.0,
        ),
        store,
        audit,
        client,
    )
    return service, store, client, request, approval


class FakeModifyClient(LiveOrderModifyClient):
    def __init__(self):
        self.requests = []

    def modify_order(self, request):
        self.requests.append(request)
        return LiveOrderModifyResult(
            broker_order=request.broker_order.model_copy(
                update={
                    "broker_order_id": "TOSS-2",
                    "parent_broker_order_id": request.broker_order.broker_order_id,
                }
            ),
            previous_broker_order=request.broker_order,
            status=OrderStatus.ACCEPTED_BY_BROKER,
        )

"""A rotation must sell, wait for the fills, and only then buy.

The broker re-checks buying power against its own live balance at submission
time, so a buy filed alongside its funding sell is rejected and the book sits in
cash for a whole cycle. These tests pin the ordering and the barrier.
"""

from pathlib import Path
from typing import Any

import pytest
import yaml

from maestro.approval.models import ApprovalDecision
from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.core.enums import (
    AssetType,
    BrokerProduct,
    Currency,
    MarketRegion,
    OrderSide,
    OrderStatus,
)
from maestro.core.instruments import TradableInstrument
from maestro.execution.base import OrderIntent
from maestro.execution.brokers.readonly import BrokerBuyingPower
from maestro.execution.live_order_factory import LiveApprovalDependencies
from maestro.execution.live_order_models import (
    BrokerOrderId,
    FillReconciliationResult,
    LiveOrderCancelResult,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.ops.workflow_recovery import WorkflowRecoveryService
from maestro.orchestration import orchestrator as orchestrator_module
from maestro.orchestration.live_gates import LiveExecutionGateService
from maestro.orchestration.orchestrator import MaestroOrchestrator


def test_buys_are_submitted_only_after_every_sell_filled(tmp_path, monkeypatch):
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    _install_fakes(monkeypatch, calls, {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.FILLED})

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    # The sell is submitted and confirmed filled before the buy is even sent.
    assert calls == ["submit:sell_a", "poll:sell_a", "submit:buy_b", "poll:buy_b"]


def test_partially_filled_sell_blocks_every_buy(tmp_path, monkeypatch):
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.PARTIALLY_FILLED, "buy_b": OrderStatus.FILLED},
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert "submit:buy_b" not in calls


def test_buys_shrink_to_the_cash_the_sells_actually_raised(tmp_path, monkeypatch):
    submitted: list[tuple[str, float]] = []
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=5_000.0)
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.FILLED},
        submitted=submitted,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    # The buy was approved for 100 @ 100 = $10,000 but only $5,000 came back.
    assert dict(submitted)["buy_b"] == 50.0


def test_abort_cancels_the_outstanding_sell(tmp_path, monkeypatch):
    """A working sell must come off the book so the operator can retry today.

    An unfilled order left at the broker trips the pending_broker_orders gate,
    which blocks the whole next run until the DAY order expires at the close.
    """
    calls: list[str] = []
    cancels: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.PARTIALLY_FILLED, "buy_b": OrderStatus.FILLED},
        cancels=cancels,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert cancels == ["sell_a"]
    aborted = orchestrator.state_store.list_system_events_by_type("rotation_cohort_aborted")
    # The broker confirms the order is gone on the follow-up poll.
    assert aborted[0]["payload"]["canceled"][0]["order_id"] == "sell_a"
    assert aborted[0]["payload"]["cancel_unconfirmed"] == []


def test_abort_skips_cancel_when_the_sell_never_reached_the_broker(tmp_path, monkeypatch):
    """A rejected pre-submit order has nothing at the broker to cancel."""
    calls: list[str] = []
    cancels: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    _install_fakes(
        monkeypatch,
        calls,
        {"buy_b": OrderStatus.FILLED},
        cancels=cancels,
        reject_submits={"sell_a"},
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert cancels == []
    assert "submit:buy_b" not in calls


def test_buy_only_run_is_not_resized_against_buying_power(tmp_path, monkeypatch):
    """A contribution has no sells to wait on, so nothing has moved since approval.

    Re-querying and rescaling it would silently shrink a run that was already
    sized and gated against real cash.
    """
    submitted: list[tuple[str, float]] = []
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=1.0)

    def _explode(order):
        raise AssertionError(f"buying power must not be consulted for {order.order_id}")

    orchestrator.order_capacity_lookup = _explode
    _install_fakes(monkeypatch, calls, {"buy_b": OrderStatus.FILLED}, submitted=submitted)

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert dict(submitted)["buy_b"] == 100.0


def test_rerun_reuses_the_same_idempotency_key(tmp_path, monkeypatch):
    """Two-phase submission must not mint a fresh duplicate_key per run.

    LiveOrderSafetyService refuses a submission whose duplicate_key it has already
    seen. That guard is only worth anything if the key is stable for a given
    signal run and order, so a retry cannot double-fill.
    """
    keys: list[tuple[str, str | None]] = []
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.FILLED},
        keys=keys,
    )

    for run_id in ("run-1", "run-2"):
        orchestrator._execute_live_approval_orders(
            run_id,
            [_sell("sell_a"), _buy("buy_b")],
            "appr-1",
            _approval_decision(run_id),
            signal_run_id="sig-1",
        )

    by_order: dict[str, set[str | None]] = {}
    for order_id, key in keys:
        by_order.setdefault(order_id, set()).add(key)
    assert all(len(seen) == 1 for seen in by_order.values()), by_order


def test_abort_notifies_the_operator(tmp_path, monkeypatch):
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    telegram = _TelegramClient()
    orchestrator.telegram_client = telegram
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.PARTIALLY_FILLED, "buy_b": OrderStatus.FILLED},
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert telegram.messages, "operator was never told the rotation stopped"
    text = telegram.messages[0][1]
    assert "sell_a" in text
    assert "buy_b" in text


class _TelegramClient:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []

    def send_message(self, chat_id, text, **kwargs):
        del kwargs
        self.messages.append((chat_id, text))


def _install_fakes(
    monkeypatch,
    calls: list[str],
    status_by_order: dict[str, OrderStatus],
    submitted: list[tuple[str, float]] | None = None,
    cancels: list[str] | None = None,
    reject_submits: set[str] | None = None,
    keys: list[tuple[str, str | None]] | None = None,
    cancel_confirms: bool = True,
    cancel_confirms_after: int = 1,
    cancel_service: str | None = "fake",
    fills_during_cancel: set[str] | None = None,
    poll_raises_after_cancel: set[str] | None = None,
    presubmit_raises: set[str] | None = None,
    reconciles: list[str] | None = None,
) -> None:
    def factory(config, state_store, audit_logger, **kwargs) -> LiveApprovalDependencies:
        del config, kwargs
        status = _StatusService(calls, status_by_order, poll_raises_after_cancel or set())
        return LiveApprovalDependencies(
            state_store=state_store,
            audit_logger=audit_logger,
            safety_service=_SafetyService(
                calls, submitted, reject_submits or set(), keys, presubmit_raises or set()
            ),
            status_service=status,
            fill_reconciliation_service=_FillService(reconciles),
            workflow_service=None,
            lifecycle_service=None,
            cancel_service=(
                _CancelService(
                    cancels if cancels is not None else [],
                    status_by_order,
                    cancel_confirms,
                    cancel_confirms_after,
                    fills_during_cancel or set(),
                    status,
                )
                if cancel_service is not None
                else None
            ),
        )

    monkeypatch.setattr(orchestrator_module, "build_live_approval_dependencies", factory)


class _SafetyService:
    def __init__(
        self,
        calls: list[str],
        submitted: list[tuple[str, float]] | None,
        reject_submits: set[str],
        keys: list[tuple[str, str | None]] | None = None,
        presubmit_raises: set[str] | None = None,
    ) -> None:
        self.calls = calls
        self.submitted = submitted
        self.reject_submits = reject_submits
        self.keys = keys
        self.presubmit_raises = presubmit_raises or set()

    def submit_approved_order(self, request, approval):
        del approval
        self.calls.append(f"submit:{request.order_id}")
        if self.keys is not None:
            self.keys.append((request.order_id, request.duplicate_key))
        if self.submitted is not None:
            self.submitted.append((request.order_id, request.quantity))
        if request.order_id in self.presubmit_raises:
            raise ValueError("pre-submit validation failed")
        if request.order_id in self.reject_submits:
            return LiveOrderResult(
                order_id=request.order_id,
                status=OrderStatus.REJECTED,
                message="broker rejected before acceptance",
            )
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            broker_order=BrokerOrderId(
                broker="toss",
                broker_order_id=request.order_id,
                order_id=request.order_id,
                submitted_at=utc_now().isoformat(),
                account_id=request.account_id,
            ),
        )


class _CancelService:
    def __init__(
        self,
        cancels: list[str],
        status_by_order: dict[str, OrderStatus],
        confirms: bool,
        confirms_after: int = 1,
        fills_during_cancel: set[str] | None = None,
        status_service: "_StatusService | None" = None,
    ) -> None:
        self.cancels = cancels
        self.status_by_order = status_by_order
        self.confirms = confirms
        self.confirms_after = confirms_after
        self.fills_during_cancel = fills_during_cancel or set()
        self.status_service = status_service

    def cancel_order(self, request, approval_decision):
        del approval_decision
        order_id = request.broker_order.order_id
        self.cancels.append(order_id)
        if self.status_service is not None:
            self.status_service.cancelled.add(order_id)
        if order_id in self.fills_during_cancel:
            # The broker filled it before the cancel landed.
            self.status_by_order[order_id] = OrderStatus.FILLED
        elif self.confirms:
            # A real broker settles the cancellation asynchronously: the order
            # keeps reporting its old status for a poll or two first.
            self.status_by_order[order_id] = _DelayedCancel(
                self.status_by_order[order_id], self.confirms_after
            )
        return LiveOrderCancelResult(
            broker_order=request.broker_order,
            status=OrderStatus.CANCELED,
            canceled_quantity=50.0,
        )


class _StatusService:
    def __init__(
        self,
        calls: list[str],
        status_by_order: dict[str, OrderStatus],
        raises_after_cancel: set[str] | None = None,
    ) -> None:
        self.calls = calls
        self.status_by_order = status_by_order
        self.raises_after_cancel = raises_after_cancel or set()
        self.cancelled: set[str] = set()

    def poll_order_status(self, run_id, broker_order):
        del run_id
        self.calls.append(f"poll:{broker_order.broker_order_id}")
        if broker_order.order_id in self.raises_after_cancel and broker_order.order_id in (
            self.cancelled
        ):
            raise TimeoutError("broker status unavailable")
        status = self.status_by_order[broker_order.order_id]
        if isinstance(status, _DelayedCancel):
            status = status.next_status()
        filled = 100.0 if status == OrderStatus.FILLED else 50.0
        return LiveOrderStatusSnapshot(
            broker_order=broker_order,
            status=status,
            checked_at=utc_now().isoformat(),
            symbol="MOCK_ETF_A",
            side=OrderSide.SELL,
            partial_fill=PartialFillSummary(
                ordered_quantity=100.0,
                filled_quantity=filled,
                remaining_quantity=100.0 - filled,
            ),
        )


class _DelayedCancel:
    """A broker whose cancellation only shows up after `remaining` polls."""

    def __init__(self, current: OrderStatus, remaining: int) -> None:
        self.current = current
        self.remaining = remaining

    def next_status(self) -> OrderStatus:
        self.remaining -= 1
        if self.remaining <= 0:
            return OrderStatus.CANCELED
        return self.current


class _FillService:
    def __init__(self, reconciles: list[str] | None = None) -> None:
        self.reconciles = reconciles

    def reconcile_latest(self, run_id):
        if self.reconciles is not None:
            self.reconciles.append(run_id)
        return FillReconciliationResult(
            run_id=run_id,
            checked_at=utc_now().isoformat(),
            cash=1_000_000,
            positions={},
        )


def _sell(order_id: str) -> OrderIntent:
    return _intent(order_id, "MOCK_ETF_A", OrderSide.SELL)


def _buy(order_id: str) -> OrderIntent:
    return _intent(order_id, "MOCK_ETF_B", OrderSide.BUY)


def _intent(order_id: str, symbol: str, side: OrderSide) -> OrderIntent:
    return OrderIntent(
        order_id=order_id,
        symbol=symbol,
        side=side,
        quantity=100,
        price=100.0,
        notional=10_000.0,
        currency=Currency.KRW,
        account_id=None,
    )


def _approval_decision(run_id: str) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id="appr-1",
        run_id=run_id,
        status="approved",
        decided_at=utc_now(),
        decided_by="telegram:fake",
    )


def _orchestrator(
    tmp_path,
    *,
    buying_power: float,
    max_buy_quantity: float | None = None,
    min_order_quantity: float | None = None,
    max_polls: int = 1,
) -> MaestroOrchestrator:
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
    config.execution.order_status_max_polls = max_polls
    config.execution.order_status_poll_interval_seconds = 0

    if min_order_quantity is not None:
        config.universe.instruments = [
            TradableInstrument(
                symbol=symbol,
                asset_type=AssetType.ETF,
                region=MarketRegion.KR,
                currency=Currency.KRW,
                broker="toss",
                broker_product=BrokerProduct.KIS_DOMESTIC_STOCK,
                broker_symbol=symbol,
                exchange_code="KRX",
                quantity_step=1,
                price_tick=1,
                min_order_quantity=min_order_quantity,
                min_order_notional=0,
            )
            for symbol in ("MOCK_ETF_A", "MOCK_ETF_B")
        ]

    orchestrator = MaestroOrchestrator(config)
    orchestrator.order_capacity_lookup = lambda order: BrokerBuyingPower(
        symbol=order.symbol,
        order_price=order.price,
        cash_buying_power=buying_power,
        max_buy_quantity=max_buy_quantity,
        source="fake",
    )
    orchestrator.state_store.save_portfolio_snapshot(
        "run_adopted_broker_baseline",
        orchestrator.state_store.load_latest_portfolio_state(),
    )
    return orchestrator


def test_buying_power_lookup_failure_after_fills_is_reported_not_raised(tmp_path, monkeypatch):
    """The sells already filled, so the book is in cash and the operator must know.

    Letting the broker lookup raise here unwinds the run with no notification and
    no event — indistinguishable from the silent cash-drift bug this flow fixes.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    telegram = _TelegramClient()
    orchestrator.telegram_client = telegram

    def _unavailable(order):
        raise TimeoutError(f"broker capacity unavailable for {order.order_id}")

    orchestrator.order_capacity_lookup = _unavailable
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.FILLED},
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert "submit:buy_b" not in calls
    aborted = orchestrator.state_store.list_system_events_by_type("rotation_cohort_aborted")
    assert "buying_power_unavailable" in aborted[0]["payload"]["reason"]
    assert telegram.messages


def test_second_buy_is_checked_against_capacity_not_just_the_first(tmp_path, monkeypatch):
    """Every buy gets its own post-sell capacity ruling, with cash reserved.

    Sizing the whole cohort off one lookup for buys[0] let a later buy through on
    cash the earlier one had already spent, and the broker rejected it — landing
    the book back in cash for the part that failed.
    """
    calls: list[str] = []
    submitted: list[tuple[str, float]] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    _install_fakes(
        monkeypatch,
        calls,
        {
            "sell_a": OrderStatus.FILLED,
            "buy_b": OrderStatus.FILLED,
            "buy_c": OrderStatus.FILLED,
        },
        submitted=submitted,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b"), _intent("buy_c", "MOCK_ETF_B", OrderSide.BUY)],
        "appr-1",
        _approval_decision("run-1"),
    )

    # $10,000 of buying power against two $10,000 buys. Both go out, and their
    # combined notional stays inside the balance — sizing either as if it had the
    # whole balance to itself would have the second rejected by the broker.
    submitted_buys = {
        order_id: quantity for order_id, quantity in submitted if order_id.startswith("buy")
    }
    assert set(submitted_buys) == {"buy_b", "buy_c"}
    assert sum(quantity * 100.0 for quantity in submitted_buys.values()) <= 10_000.0


def test_max_buy_quantity_is_enforced_after_the_sells_fill(tmp_path, monkeypatch):
    """The post-sell check is the authoritative one, so it must be a full check.

    Only re-reading cash_buying_power let a buy through that the broker's own
    per-symbol quantity cap would reject.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0, max_buy_quantity=0.0)
    telegram = _TelegramClient()
    orchestrator.telegram_client = telegram
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.FILLED},
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert "submit:buy_b" not in calls
    assert telegram.messages, "a rotation that could not buy must not end silently"


def test_rejected_buy_after_filled_sells_is_reported(tmp_path, monkeypatch):
    """The sells are done, so a failed buy leaves the book in cash.

    Only the sell phase was evaluated, so a rejected or partially filled buy
    ended the run looking like a success.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    telegram = _TelegramClient()
    orchestrator.telegram_client = telegram
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED},
        reject_submits={"buy_b"},
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    incomplete = orchestrator.state_store.list_system_events_by_type("rotation_cohort_incomplete")
    assert incomplete, "a rotation whose buy failed must record it"
    assert incomplete[0]["payload"]["omitted_buy_order_ids"] == ["buy_b"]
    assert incomplete[0]["payload"]["filled_buy_order_ids"] == []
    assert telegram.messages


def test_buys_rescaled_out_of_existence_are_reported(tmp_path, monkeypatch):
    """Zero realized cash leaves the sells done and nothing bought.

    An empty buy batch returned like a success — the same silent cash drift the
    two-phase flow exists to remove.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=0.0)
    telegram = _TelegramClient()
    orchestrator.telegram_client = telegram
    _install_fakes(monkeypatch, calls, {"sell_a": OrderStatus.FILLED})

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert "submit:buy_b" not in calls
    incomplete = orchestrator.state_store.list_system_events_by_type("rotation_cohort_incomplete")
    assert incomplete[0]["payload"]["omitted_buy_order_ids"] == ["buy_b"]
    assert incomplete[0]["payload"]["submitted_buy_order_ids"] == []
    assert telegram.messages


def test_cancel_is_confirmed_by_polling_not_by_the_api_ack(tmp_path, monkeypatch):
    """The Toss adapter returns CANCELED straight from the POST acknowledgement.

    Recording that as a confirmed cancellation tells the operator the book is
    clear when the order may still be working — and a working order blocks their
    whole next run.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        # Still PARTIALLY_FILLED when re-polled after the cancel: not confirmed.
        {"sell_a": OrderStatus.PARTIALLY_FILLED, "buy_b": OrderStatus.FILLED},
        cancels=[],
        cancel_confirms=False,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    payload = orchestrator.state_store.list_system_events_by_type("rotation_cohort_aborted")[0][
        "payload"
    ]
    assert payload["canceled"] == []
    assert payload["cancel_unconfirmed"][0]["order_id"] == "sell_a"
    assert payload["cancel_unconfirmed"][0]["observed_status"] == "partially_filled"


def test_one_buy_dropped_by_resizing_still_marks_the_cohort_incomplete(tmp_path, monkeypatch):
    """A rotation that bought back only part of what was approved is not a success.

    Reporting only the all-buys-vanished case let a cohort finish 'complete' while
    one approved leg was silently dropped by resizing.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0, min_order_quantity=60.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {
            "sell_a": OrderStatus.FILLED,
            "buy_b": OrderStatus.FILLED,
            "buy_c": OrderStatus.FILLED,
        },
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b"), _intent("buy_c", "MOCK_ETF_B", OrderSide.BUY)],
        "appr-1",
        _approval_decision("run-1"),
    )

    # $10,000 over two $10,000 buys scales each to 50 shares, under the 60-share
    # minimum, so both drop. Whatever survives, nothing may be reported complete
    # while an approved leg went missing.
    incomplete = orchestrator.state_store.list_system_events_by_type("rotation_cohort_incomplete")
    assert incomplete, "an omitted buy leg must be recorded"
    payload = incomplete[0]["payload"]
    assert set(payload["original_buy_order_ids"]) == {"buy_b", "buy_c"}
    assert set(payload["omitted_buy_order_ids"]) == {"buy_b", "buy_c"}
    assert payload["filled_buy_order_ids"] == []


def test_one_buy_blocked_by_capacity_marks_the_cohort_incomplete(tmp_path, monkeypatch):
    """One leg blocked post-fill, the other filled: still an incomplete rotation."""
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.order_capacity_lookup = lambda order: BrokerBuyingPower(
        symbol=order.symbol,
        order_price=order.price,
        cash_buying_power=10_000.0,
        max_buy_quantity=1.0 if order.symbol == "MOCK_ETF_B" else None,
        source="fake",
    )
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {
            "sell_a": OrderStatus.FILLED,
            "buy_b": OrderStatus.FILLED,
            "buy_c": OrderStatus.FILLED,
        },
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b"), _intent("buy_c", "MOCK_ETF_B", OrderSide.BUY)],
        "appr-1",
        _approval_decision("run-1"),
    )

    incomplete = orchestrator.state_store.list_system_events_by_type("rotation_cohort_incomplete")
    assert incomplete
    payload = incomplete[0]["payload"]
    assert set(payload["original_buy_order_ids"]) == {"buy_b", "buy_c"}
    assert payload["omitted_buy_order_ids"]
    assert set(payload["filled_buy_order_ids"]) | set(payload["omitted_buy_order_ids"]) == {
        "buy_b",
        "buy_c",
    }


def test_working_buy_is_cancelled_and_confirmed(tmp_path, monkeypatch):
    """A buy still OPEN at the poll limit is live at the broker, not finished."""
    calls: list[str] = []
    cancels: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.OPEN},
        cancels=cancels,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert cancels == ["buy_b"]
    blockers = orchestrator.state_store.list_system_events_by_type("live_order_recovery_required")
    assert blockers == [], "a confirmed cancellation leaves nothing to recover"


def test_unconfirmed_buy_cancel_raises_a_recovery_blocker(tmp_path, monkeypatch):
    """Cancel not confirmed means an order may still be working.

    Ending the run without a blocker lets the next execution collide with it, and
    the runbook would wrongly tell the operator to just re-run.
    """
    calls: list[str] = []
    cancels: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.PARTIALLY_FILLED},
        cancels=cancels,
        cancel_confirms=False,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert cancels == ["buy_b"]
    blockers = orchestrator.state_store.list_system_events_by_type("live_order_recovery_required")
    assert [row["payload"]["order_id"] for row in blockers] == ["buy_b"]
    assert blockers[0]["payload"]["reason"] == "rotation_buy_unresolved_at_broker"


def test_rejected_buy_raises_no_recovery_blocker(tmp_path, monkeypatch):
    """A broker-terminal rejection leaves nothing working, so re-running is safe."""
    calls: list[str] = []
    cancels: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED},
        cancels=cancels,
        reject_submits={"buy_b"},
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert cancels == []
    assert orchestrator.state_store.list_system_events_by_type("live_order_recovery_required") == []


def test_unresolved_buy_blocker_stops_the_next_live_execution(tmp_path, monkeypatch):
    """The blocker is only worth recording if it actually gates the next run."""
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.PARTIALLY_FILLED},
        cancel_confirms=False,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    blocks = LiveExecutionGateService(
        orchestrator.config,
        orchestrator.state_store,
        orchestrator.audit,
    ).evaluate("run-2", [_buy("buy_next")], [])

    recovery_blocks = [
        block for block in blocks if block.get("reason") == "live_order_recovery_required"
    ]
    assert recovery_blocks, "the unresolved buy must gate the next live execution"
    assert recovery_blocks[0]["order_id"] == "buy_b"


def test_cancel_confirmation_polls_until_terminal(tmp_path, monkeypatch):
    """Broker cancellation is asynchronous; one poll is not an answer.

    A normal cancel that clears on the second poll was being filed as
    cancel_unconfirmed, raising a recovery blocker the operator did not need.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0, max_polls=3)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.PARTIALLY_FILLED, "buy_b": OrderStatus.FILLED},
        # Still working on the first confirmation poll, CANCELED on the second.
        cancel_confirms_after=2,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    payload = orchestrator.state_store.list_system_events_by_type("rotation_cohort_aborted")[0][
        "payload"
    ]
    assert payload["canceled"][0]["order_id"] == "sell_a"
    assert payload["cancel_unconfirmed"] == []


def test_cancel_confirmation_gives_up_at_the_poll_limit(tmp_path, monkeypatch):
    """Bounded, not unbounded: an order that never clears still ends the run."""
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.PARTIALLY_FILLED, "buy_b": OrderStatus.FILLED},
        cancel_confirms=False,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    payload = orchestrator.state_store.list_system_events_by_type("rotation_cohort_aborted")[0][
        "payload"
    ]
    assert payload["cancel_unconfirmed"][0]["order_id"] == "sell_a"


def test_unresolved_buy_blocker_can_actually_be_recovered(tmp_path, monkeypatch):
    """A blocker that gates but cannot be matched leaves the operator stuck.

    The recovery parser rebuilds a full LiveOrderRequest from the payload and
    reads the broker id from result.broker_order. A payload that carries only an
    order id gates the next run and then lands in `unmatched`.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.PARTIALLY_FILLED},
        cancel_confirms=False,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    blockers = WorkflowRecoveryService(
        orchestrator.config,
        orchestrator.state_store,
        orchestrator.audit,
    ).preview().blockers
    blocker = next(item for item in blockers if item.order_id == "buy_b")

    assert blocker.broker_order_id == "buy_b", "recovery cannot look the order up"
    # The parser must be able to rebuild the request it will re-poll with.
    assert LiveOrderRequest.model_validate(blocker.request).symbol == "MOCK_ETF_B"

    # And the real recovery run must match it against the broker rather than
    # filing it unmatched, then clear it.
    def _recovery_service() -> WorkflowRecoveryService:
        return WorkflowRecoveryService(
            orchestrator.config,
            orchestrator.state_store,
            orchestrator.audit,
            status_client_for_account=lambda account_id: _RecoveryStatusClient(),
        )

    # recover_live_orders returns attestation_required the moment a blocker
    # cannot be matched, before it touches any broker infrastructure. Getting
    # past that point to the reconciliation step is proof it was matched.
    with pytest.raises(ValueError, match="Broker reconciliation"):
        _recovery_service().recover_live_orders(
            reason="operator verified the broker book",
            decided_by="telegram:test",
        )


class _RecoveryStatusClient:
    """Broker that reports the working buy as cancelled when recovery re-polls."""

    def get_order_status(self, broker_order):
        return LiveOrderStatusSnapshot(
            broker_order=broker_order,
            status=OrderStatus.CANCELED,
            checked_at=utc_now().isoformat(),
            symbol="MOCK_ETF_B",
            side=OrderSide.BUY,
            partial_fill=PartialFillSummary(
                ordered_quantity=100.0,
                filled_quantity=0.0,
                remaining_quantity=100.0,
            ),
        )


def test_missing_cancel_client_still_raises_a_blocker(tmp_path, monkeypatch):
    """Multi-product KIS routing supplies no cancel client.

    Returning quietly there leaves a working buy at the broker with nothing
    recorded — the exact state the blocker exists to prevent.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.PARTIALLY_FILLED},
        cancel_service=None,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    blockers = orchestrator.state_store.list_system_events_by_type("live_order_recovery_required")
    assert [row["payload"]["order_id"] for row in blockers] == ["buy_b"]


def test_buy_that_fills_during_the_cancel_race_is_not_a_blocker(tmp_path, monkeypatch):
    """Losing the race to a fill is a completed buy, not an unresolved order.

    It is no longer working at the broker, so it must not gate the next run — and
    it has to count as filled rather than as a missing leg.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0, max_polls=3)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.OPEN},
        fills_during_cancel={"buy_b"},
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    assert orchestrator.state_store.list_system_events_by_type("live_order_recovery_required") == []
    incomplete = orchestrator.state_store.list_system_events_by_type("rotation_cohort_incomplete")
    assert incomplete == [], "a buy that filled is not a missing leg"


def test_resize_and_capacity_omissions_are_distinguishable(tmp_path, monkeypatch):
    """An operator has to tell 'the cash would not stretch' from 'the broker refused'.

    Recording one flat submitted set collapsed those into the same report.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.order_capacity_lookup = lambda order: BrokerBuyingPower(
        symbol=order.symbol,
        order_price=order.price,
        cash_buying_power=10_000.0,
        # Only buy_c's symbol carries a quantity cap below the resized size.
        max_buy_quantity=1.0 if order.symbol == "MOCK_ETF_A" else None,
        source="fake",
    )
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {
            "sell_a": OrderStatus.FILLED,
            "buy_b": OrderStatus.FILLED,
            "buy_c": OrderStatus.FILLED,
        },
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b"), _intent("buy_c", "MOCK_ETF_A", OrderSide.BUY)],
        "appr-1",
        _approval_decision("run-1"),
    )

    payload = orchestrator.state_store.list_system_events_by_type("rotation_cohort_incomplete")[0][
        "payload"
    ]
    # buy_c survived resizing but the broker's quantity cap rejected it, so the
    # stage where it dropped out is visible.
    assert "buy_c" in payload["resized_buy_order_ids"]
    assert "buy_c" not in payload["capacity_accepted_buy_order_ids"]
    assert "buy_c" not in payload["submitted_buy_order_ids"]
    assert "buy_c" in payload["omitted_buy_order_ids"]
    assert "buy_b" in payload["submitted_buy_order_ids"]


def test_status_poll_failure_still_raises_a_recovery_blocker(tmp_path, monkeypatch):
    """The cancel went out but we never confirmed it — that order is still live.

    Dropping the broker order from the failure entry made the blocker
    unresolvable, so it was skipped entirely and the next run was left unguarded.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.PARTIALLY_FILLED},
        poll_raises_after_cancel={"buy_b"},
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    blockers = orchestrator.state_store.list_system_events_by_type("live_order_recovery_required")
    assert [row["payload"]["order_id"] for row in blockers] == ["buy_b"]
    assert orchestrator.state_store.list_system_events_by_type("rotation_buy_unresolvable") == []


def test_submitted_ids_exclude_orders_that_never_reached_the_broker(tmp_path, monkeypatch):
    """Pre-submit failures were counted as submitted.

    That made submitted_buy_order_ids identical to the capacity-accepted set and
    hid the stage a leg actually dropped out at.
    """
    calls: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED},
        presubmit_raises={"buy_b"},
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    payload = orchestrator.state_store.list_system_events_by_type("rotation_cohort_incomplete")[0][
        "payload"
    ]
    assert payload["capacity_accepted_buy_order_ids"] == ["buy_b"]
    assert payload["submitted_buy_order_ids"] == []


def test_fill_found_while_confirming_a_cancel_is_reconciled(tmp_path, monkeypatch):
    """The batch already reconciled fills before this poll happened.

    Counting the fill in filled_ids without replaying it leaves the ledger and
    the recorded lifecycle believing the order is still open, so positions and
    cash drift from broker truth.
    """
    calls: list[str] = []
    reconciles: list[str] = []
    orchestrator = _orchestrator(tmp_path, buying_power=10_000.0, max_polls=3)
    orchestrator.telegram_client = _TelegramClient()
    _install_fakes(
        monkeypatch,
        calls,
        {"sell_a": OrderStatus.FILLED, "buy_b": OrderStatus.OPEN},
        fills_during_cancel={"buy_b"},
        reconciles=reconciles,
    )

    orchestrator._execute_live_approval_orders(
        "run-1",
        [_sell("sell_a"), _buy("buy_b")],
        "appr-1",
        _approval_decision("run-1"),
    )

    # The cancel poll observed the fill, so reconciliation must run again after
    # it rather than leaving the batch's earlier pass as the last word.
    assert reconciles.count("run-1") >= 2
    resolved = orchestrator.state_store.list_system_events_by_type("rotation_buy_resolved")
    assert resolved[0]["payload"]["order_id"] == "buy_b"
    assert resolved[0]["payload"]["final_status"] == "filled"

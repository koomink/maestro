"""A rotation must sell, wait for the fills, and only then buy.

The broker re-checks buying power against its own live balance at submission
time, so a buy filed alongside its funding sell is rejected and the book sits in
cash for a whole cycle. These tests pin the ordering and the barrier.
"""

from pathlib import Path
from typing import Any

import yaml

from maestro.approval.models import ApprovalDecision
from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.core.enums import Currency, OrderSide, OrderStatus
from maestro.execution.base import OrderIntent
from maestro.execution.brokers.readonly import BrokerBuyingPower
from maestro.execution.live_order_factory import LiveApprovalDependencies
from maestro.execution.live_order_models import (
    BrokerOrderId,
    FillReconciliationResult,
    LiveOrderCancelResult,
    LiveOrderResult,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.orchestration import orchestrator as orchestrator_module
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
    assert aborted[0]["payload"]["canceled"][0]["order_id"] == "sell_a"


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
) -> None:
    def factory(config, state_store, audit_logger, **kwargs) -> LiveApprovalDependencies:
        del config, kwargs
        return LiveApprovalDependencies(
            state_store=state_store,
            audit_logger=audit_logger,
            safety_service=_SafetyService(calls, submitted, reject_submits or set(), keys),
            status_service=_StatusService(calls, status_by_order),
            fill_reconciliation_service=_FillService(),
            workflow_service=None,
            lifecycle_service=None,
            cancel_service=_CancelService(cancels if cancels is not None else []),
        )

    monkeypatch.setattr(orchestrator_module, "build_live_approval_dependencies", factory)


class _SafetyService:
    def __init__(
        self,
        calls: list[str],
        submitted: list[tuple[str, float]] | None,
        reject_submits: set[str],
        keys: list[tuple[str, str | None]] | None = None,
    ) -> None:
        self.calls = calls
        self.submitted = submitted
        self.reject_submits = reject_submits
        self.keys = keys

    def submit_approved_order(self, request, approval):
        del approval
        self.calls.append(f"submit:{request.order_id}")
        if self.keys is not None:
            self.keys.append((request.order_id, request.duplicate_key))
        if self.submitted is not None:
            self.submitted.append((request.order_id, request.quantity))
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
    def __init__(self, cancels: list[str]) -> None:
        self.cancels = cancels

    def cancel_order(self, request, approval_decision):
        del approval_decision
        self.cancels.append(request.broker_order.order_id)
        return LiveOrderCancelResult(
            broker_order=request.broker_order,
            status=OrderStatus.CANCELED,
            canceled_quantity=50.0,
        )


class _StatusService:
    def __init__(self, calls: list[str], status_by_order: dict[str, OrderStatus]) -> None:
        self.calls = calls
        self.status_by_order = status_by_order

    def poll_order_status(self, run_id, broker_order):
        del run_id
        self.calls.append(f"poll:{broker_order.broker_order_id}")
        status = self.status_by_order[broker_order.order_id]
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


class _FillService:
    def reconcile_latest(self, run_id):
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
        account_id="acct-a",
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
    config.execution.order_status_max_polls = 1
    config.execution.order_status_poll_interval_seconds = 0

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
    assert incomplete[0]["payload"]["unfilled_buy_order_ids"] == ["buy_b"]
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
    assert incomplete[0]["payload"]["reason"] == "no_buys_survived_resizing"
    assert telegram.messages

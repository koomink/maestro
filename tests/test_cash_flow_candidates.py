import sqlite3

import pytest

from maestro.execution.account_cash_flows import (
    AccountCashFlowService,
    account_cash_flow_leg_duplicate_key,
)
from maestro.execution.cash_flow_candidates import TossCashFlowCandidateDetector
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


def _save_toss_snapshot(
    store: StateStore,
    run_id: str,
    buying_power: float,
    *,
    positions: list[dict] | None = None,
    unfilled_orders: list[dict] | None = None,
) -> None:
    store.save_broker_account_snapshot(
        run_id,
        "toss_brokerage",
        {
            "account_id": "toss_brokerage",
            "account": {
                "account_id": "toss_brokerage",
                "source": "toss_openapi_readonly",
                "cash": buying_power,
                "cash_by_currency": {"KRW": buying_power},
                "buying_power_by_currency": {"KRW": buying_power},
                "ledger_cash_by_currency": None,
                "positions": positions or [],
            },
            "unfilled_orders": unfilled_orders or [],
            "order_fills": [],
        },
    )


def test_detects_stable_toss_buying_power_step(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _save_toss_snapshot(store, "baseline", 1_000_000)
    _save_toss_snapshot(store, "changed-1", 2_000_000)
    _save_toss_snapshot(store, "changed-2", 2_000_000)
    _save_toss_snapshot(store, "changed-3", 2_000_000)

    candidate = TossCashFlowCandidateDetector(store).detect("toss_brokerage")

    assert candidate is not None
    assert candidate.flow_type == "deposit"
    assert candidate.amount == 1_000_000
    assert len(candidate.stable_snapshot_ids) == 3


def test_rejects_candidate_when_orders_change(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _save_toss_snapshot(store, "baseline", 1_000_000)
    _save_toss_snapshot(store, "changed-1", 2_000_000)
    _save_toss_snapshot(
        store,
        "changed-2",
        2_000_000,
        unfilled_orders=[{"order_id": "broker-order-1"}],
    )
    _save_toss_snapshot(store, "changed-3", 2_000_000)

    assert TossCashFlowCandidateDetector(store).detect("toss_brokerage") is None


def test_account_cash_flow_service_is_idempotent(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
        account_id="toss_brokerage",
    )
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))

    first = service.record(
        account_id="toss_brokerage",
        amount=1_000_000,
        currency="KRW",
        flow_type="deposit",
        effective_at="2026-08-02T12:00:00+00:00",
        source="telegram_toss_cash_flow_confirmation",
        verification="operator_verified",
        duplicate_key="candidate:one",
    )
    second = service.record(
        account_id="toss_brokerage",
        amount=1_000_000,
        currency="KRW",
        flow_type="deposit",
        effective_at="2026-08-02T12:00:00+00:00",
        source="telegram_toss_cash_flow_confirmation",
        verification="operator_verified",
        duplicate_key="candidate:one",
    )

    assert first.created is True
    assert second.created is False
    state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert state is not None
    assert state.cash_by_currency["KRW"] == 2_000_000


def _save_toss_snapshot_multi(
    store: StateStore,
    run_id: str,
    buying_power_by_currency: dict[str, float],
) -> None:
    store.save_broker_account_snapshot(
        run_id,
        "toss_brokerage",
        {
            "account_id": "toss_brokerage",
            "account": {
                "account_id": "toss_brokerage",
                "source": "toss_openapi_readonly",
                "cash": 0.0,
                "cash_by_currency": dict(buying_power_by_currency),
                "buying_power_by_currency": dict(buying_power_by_currency),
                "ledger_cash_by_currency": None,
                "positions": [],
            },
            "unfilled_orders": [],
            "order_fills": [],
        },
    )


def test_opposite_sign_currency_moves_are_not_offered_as_a_cash_flow(tmp_path):
    """A conversion is one currency falling while another rises.

    Confirming either leg on its own would record investor money entering or
    leaving an account whose total never moved.
    """
    store = StateStore(str(tmp_path / "state.db"))
    _save_toss_snapshot_multi(store, "baseline", {"KRW": 1_400_000.0, "USD": 100.0})
    for run_id in ("changed-1", "changed-2", "changed-3"):
        _save_toss_snapshot_multi(store, run_id, {"KRW": 100_000.0, "USD": 1_100.0})

    assert TossCashFlowCandidateDetector(store).detect("toss_brokerage") is None


def test_same_sign_currency_moves_still_produce_a_candidate(tmp_path):
    """The conversion guard must not swallow a genuine multi-currency deposit."""
    store = StateStore(str(tmp_path / "state.db"))
    _save_toss_snapshot_multi(store, "baseline", {"KRW": 100_000.0, "USD": 100.0})
    for run_id in ("changed-1", "changed-2", "changed-3"):
        _save_toss_snapshot_multi(store, run_id, {"KRW": 200_000.0, "USD": 200.0})

    candidate = TossCashFlowCandidateDetector(store).detect("toss_brokerage")

    assert candidate is not None
    assert candidate.flow_type == "deposit"


class _EventInsertFailingConnection:
    """Connection proxy that fails exactly on the cash-flow event insert."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *args):
        return self._conn.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        if "INSERT INTO system_events" in sql:
            raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)


def test_ledger_does_not_move_without_its_cash_flow_event(tmp_path, monkeypatch):
    """The ledger and the event that explains it are written together.

    If the ledger could advance without its event, the duplicate-key retry
    would find no record of the flow and apply the same money a second time.
    """
    store = StateStore(str(tmp_path / "state.db"))
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}),
        account_id="toss_brokerage",
    )
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))
    real_connect = StateStore._connect

    def failing_connect(self):
        return _EventInsertFailingConnection(real_connect(self))

    monkeypatch.setattr(StateStore, "_connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError):
        service.record(
            account_id="toss_brokerage",
            amount=500_000,
            currency="KRW",
            flow_type="deposit",
            effective_at="2026-08-02T12:00:00+00:00",
            source="telegram_toss_cash_flow_confirmation",
            duplicate_key="candidate:crash",
        )
    monkeypatch.undo()

    state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert state is not None
    assert state.cash_by_currency["KRW"] == 1_000_000
    assert store.list_system_events_by_type("account_cash_flow", limit=10) == []


def test_unknown_flow_class_is_rejected(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=1_000, cash_by_currency={"KRW": 1_000}),
        account_id="toss_brokerage",
    )
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))

    with pytest.raises(ValueError, match="flow_class"):
        service.record(
            account_id="toss_brokerage",
            amount=100,
            currency="KRW",
            flow_type="deposit",
            effective_at="2026-08-02T12:00:00+00:00",
            source="test",
            flow_class="dividend",
        )


def _ledger_krw(store: StateStore, account_id: str) -> float:
    state = store.load_latest_account_portfolio_state(account_id)
    assert state is not None
    return state.cash_by_currency["KRW"]


def _open_ledger(store: StateStore, account_id: str, cash: float) -> None:
    store.save_portfolio_snapshot(
        f"baseline_{account_id}",
        PortfolioState(cash=cash, cash_by_currency={"KRW": cash}),
        account_id=account_id,
    )


def test_both_legs_of_one_transfer_are_recorded(tmp_path):
    """A transfer has two sides and both have to survive.

    Keying idempotency on the transfer alone made the second leg collide with
    the first, so an internal transfer arrived as a single outbound leg that
    read as money leaving the portfolio.
    """
    store = StateStore(str(tmp_path / "state.db"))
    _open_ledger(store, "acct_from", 1_000_000)
    _open_ledger(store, "acct_to", 0)
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))

    legs = [
        ("acct_from", "withdrawal"),
        ("acct_to", "deposit"),
    ]
    results = [
        service.record(
            account_id=account_id,
            amount=250_000,
            currency="KRW",
            flow_type=flow_type,
            effective_at="2026-08-02T12:00:00+00:00",
            source="operator_cli",
            transfer_id="move-1",
            flow_class="internal_transfer",
            duplicate_key=account_cash_flow_leg_duplicate_key(
                "move-1", account_id, "KRW", flow_type
            ),
        )
        for account_id, flow_type in legs
    ]

    assert [result.created for result in results] == [True, True]
    events = store.list_system_events_by_type("account_cash_flow", limit=10)
    assert len(events) == 2
    assert {event["payload"]["account_id"] for event in events} == {
        "acct_from",
        "acct_to",
    }
    assert _ledger_krw(store, "acct_from") == 750_000
    assert _ledger_krw(store, "acct_to") == 250_000


def test_replaying_one_leg_does_not_apply_it_twice(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _open_ledger(store, "acct_from", 1_000_000)
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))
    key = account_cash_flow_leg_duplicate_key("move-1", "acct_from", "KRW", "withdrawal")

    first = service.record(
        account_id="acct_from",
        amount=250_000,
        currency="KRW",
        flow_type="withdrawal",
        effective_at="2026-08-02T12:00:00+00:00",
        source="operator_cli",
        transfer_id="move-1",
        flow_class="internal_transfer",
        duplicate_key=key,
    )
    second = service.record(
        account_id="acct_from",
        amount=250_000,
        currency="KRW",
        flow_type="withdrawal",
        effective_at="2026-08-02T12:00:00+00:00",
        source="operator_cli",
        transfer_id="move-1",
        flow_class="internal_transfer",
        duplicate_key=key,
    )

    assert first.created is True
    assert second.created is False
    assert _ledger_krw(store, "acct_from") == 750_000


def test_leg_duplicate_keys_separate_the_sides_of_a_transfer():
    outbound = account_cash_flow_leg_duplicate_key("t1", "acct_from", "krw", "WITHDRAWAL")
    inbound = account_cash_flow_leg_duplicate_key("t1", "acct_to", "KRW", "deposit")

    assert outbound != inbound
    # Case differences must not create a second key for the same leg.
    normalized = account_cash_flow_leg_duplicate_key("t1", "acct_from", "KRW", "withdrawal")
    assert outbound == normalized

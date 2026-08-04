import sqlite3
from datetime import timedelta

import pytest

from maestro.core.clock import utc_now
from maestro.execution.account_cash_flows import (
    AccountCashFlowService,
    account_cash_flow_leg_duplicate_key,
)
from maestro.execution.cash_flow_candidates import (
    CashFlowCandidateDetector,
    FxConversionCandidate,
)
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

    candidate = CashFlowCandidateDetector(store).detect("toss_brokerage")

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

    assert CashFlowCandidateDetector(store).detect("toss_brokerage") is None


def test_market_price_changes_do_not_hide_an_unchanged_position(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _save_toss_snapshot(
        store,
        "baseline",
        1_000_000,
        positions=[
            {
                "symbol": "QQQ",
                "quantity": 2.0,
                "average_price": 100.0,
                "current_price": 101.0,
                "unrealized_pnl": 2.0,
                "currency": "USD",
            }
        ],
    )
    for index, current_price in enumerate((102.0, 103.0, 104.0), start=1):
        _save_toss_snapshot(
            store,
            f"changed-{index}",
            2_000_000,
            positions=[
                {
                    "symbol": "QQQ",
                    "quantity": 2.0,
                    "average_price": 100.0,
                    "current_price": current_price,
                    "unrealized_pnl": (current_price - 100.0) * 2.0,
                    "currency": "USD",
                }
            ],
        )

    assert CashFlowCandidateDetector(store).detect("toss_brokerage") is not None


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
    *,
    order_fills: list[dict] | None = None,
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
            "order_fills": order_fills or [],
        },
    )


def test_opposite_sign_toss_currency_moves_are_offered_as_one_conversion(tmp_path):
    """A conversion is one currency falling while another rises.

    Confirming either leg on its own would record investor money entering or
    leaving an account whose total never moved.
    """
    store = StateStore(str(tmp_path / "state.db"))
    _save_toss_snapshot_multi(store, "baseline", {"KRW": 1_400_000.0, "USD": 100.0})
    for run_id in ("changed-1", "changed-2", "changed-3"):
        _save_toss_snapshot_multi(store, run_id, {"KRW": 100_000.0, "USD": 1_100.0})

    candidate = CashFlowCandidateDetector(store).detect("toss_brokerage")

    assert isinstance(candidate, FxConversionCandidate)
    assert candidate.from_currency == "KRW"
    assert candidate.from_amount == 1_300_000.0
    assert candidate.to_currency == "USD"
    assert candidate.to_amount == 1_000.0
    assert candidate.evidence()["paired_opposite_currency_moves"] is True


def test_recent_fills_do_not_hide_a_stable_toss_conversion_candidate(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    recent_fill = [{"symbol": "QQQ", "submitted_at": utc_now().isoformat()}]
    _save_toss_snapshot_multi(
        store,
        "baseline",
        {"KRW": 1_400_000.0, "USD": 100.0},
        order_fills=recent_fill,
    )
    for run_id in ("changed-1", "changed-2", "changed-3"):
        _save_toss_snapshot_multi(
            store,
            run_id,
            {"KRW": 100_000.0, "USD": 1_100.0},
            order_fills=recent_fill,
        )

    assert isinstance(
        CashFlowCandidateDetector(store).detect("toss_brokerage"),
        FxConversionCandidate,
    )


def test_same_sign_currency_moves_still_produce_a_candidate(tmp_path):
    """The conversion guard must not swallow a genuine multi-currency deposit."""
    store = StateStore(str(tmp_path / "state.db"))
    _save_toss_snapshot_multi(store, "baseline", {"KRW": 100_000.0, "USD": 100.0})
    for run_id in ("changed-1", "changed-2", "changed-3"):
        _save_toss_snapshot_multi(store, run_id, {"KRW": 200_000.0, "USD": 200.0})

    candidate = CashFlowCandidateDetector(store).detect("toss_brokerage")

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

    result = service.record_internal_transfer(
        from_account_id="acct_from",
        from_currency="KRW",
        from_amount=250_000,
        to_account_id="acct_to",
        to_currency="KRW",
        to_amount=250_000,
        effective_at="2026-08-02T12:00:00+00:00",
        source="operator_cli",
        transfer_id="move-1",
    )

    assert result.created is True
    events = store.list_system_events_by_type("account_cash_flow", limit=10)
    assert len(events) == 2
    assert {event["payload"]["account_id"] for event in events} == {
        "acct_from",
        "acct_to",
    }
    assert _ledger_krw(store, "acct_from") == 750_000
    assert _ledger_krw(store, "acct_to") == 250_000


def test_replaying_an_internal_transfer_does_not_apply_it_twice(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _open_ledger(store, "acct_from", 1_000_000)
    _open_ledger(store, "acct_to", 0)
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))

    kwargs = {
        "from_account_id": "acct_from",
        "from_currency": "KRW",
        "from_amount": 250_000,
        "to_account_id": "acct_to",
        "to_currency": "KRW",
        "to_amount": 250_000,
        "effective_at": "2026-08-02T12:00:00+00:00",
        "source": "operator_cli",
        "transfer_id": "move-1",
    }
    first = service.record_internal_transfer(**kwargs)
    second = service.record_internal_transfer(**kwargs)

    assert first.created is True
    assert second.created is False
    assert _ledger_krw(store, "acct_from") == 750_000
    assert _ledger_krw(store, "acct_to") == 250_000


def test_leg_duplicate_keys_separate_the_sides_of_a_transfer():
    outbound = account_cash_flow_leg_duplicate_key("t1", "acct_from", "krw", "WITHDRAWAL")
    inbound = account_cash_flow_leg_duplicate_key("t1", "acct_to", "KRW", "deposit")

    assert outbound != inbound
    # Case differences must not create a second key for the same leg.
    normalized = account_cash_flow_leg_duplicate_key("t1", "acct_from", "KRW", "withdrawal")
    assert outbound == normalized


class _NthEventInsertFailingConnection:
    """Connection proxy that fails on the Nth cash-flow event insert."""

    def __init__(self, conn: sqlite3.Connection, counter: list[int], fail_on: int) -> None:
        self._conn = conn
        self._counter = counter
        self._fail_on = fail_on

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *args):
        return self._conn.__exit__(*args)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args):
        if "INSERT INTO system_events" in sql:
            self._counter[0] += 1
            if self._counter[0] == self._fail_on:
                raise sqlite3.OperationalError("disk I/O error")
        return self._conn.execute(sql, *args)


def _conversion_legs(account_id: str) -> list[dict]:
    """One account converting KRW into USD, as two linked legs."""
    return [
        {
            "account_id": account_id,
            "amount": -1_400_000.0,
            "currency": "KRW",
            "event_payload": {
                "account_id": account_id,
                "amount": -1_400_000.0,
                "currency": "KRW",
                "flow_type": "withdrawal",
                "flow_class": "fx_conversion",
                "effective_at": "2026-08-02T12:00:00+00:00",
                "source": "operator_cli",
                "transfer_id": "fx-1",
                "duplicate_key": account_cash_flow_leg_duplicate_key(
                    "fx-1", account_id, "KRW", "withdrawal"
                ),
            },
        },
        {
            "account_id": account_id,
            "amount": 1_000.0,
            "currency": "USD",
            "event_payload": {
                "account_id": account_id,
                "amount": 1_000.0,
                "currency": "USD",
                "flow_type": "deposit",
                "flow_class": "fx_conversion",
                "effective_at": "2026-08-02T12:00:00+00:00",
                "source": "operator_cli",
                "transfer_id": "fx-1",
                "duplicate_key": account_cash_flow_leg_duplicate_key(
                    "fx-1", account_id, "USD", "deposit"
                ),
            },
        },
    ]


def test_linked_legs_land_on_one_ledger_state(tmp_path):
    """Both currency deltas of a conversion advance the account together."""
    store = StateStore(str(tmp_path / "state.db"))
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=1_400_000, cash_by_currency={"KRW": 1_400_000, "USD": 0.0}),
        account_id="toss_brokerage",
    )

    result = store.apply_account_cash_flows("run_1", _conversion_legs("toss_brokerage"))

    assert result["created"] is True
    state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert state.cash_by_currency["KRW"] == 0.0
    assert state.cash_by_currency["USD"] == 1_000.0
    assert len(store.list_system_events_by_type("account_cash_flow", limit=10)) == 2


def test_a_failed_leg_rolls_back_every_other_leg(tmp_path, monkeypatch):
    """Half a conversion is money vanishing, so no leg may survive alone."""
    store = StateStore(str(tmp_path / "state.db"))
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=1_400_000, cash_by_currency={"KRW": 1_400_000, "USD": 0.0}),
        account_id="toss_brokerage",
    )
    real_connect = StateStore._connect
    counter = [0]

    def failing_connect(self):
        return _NthEventInsertFailingConnection(real_connect(self), counter, fail_on=2)

    monkeypatch.setattr(StateStore, "_connect", failing_connect)
    with pytest.raises(sqlite3.OperationalError):
        store.apply_account_cash_flows("run_1", _conversion_legs("toss_brokerage"))
    monkeypatch.undo()

    state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert state.cash_by_currency["KRW"] == 1_400_000
    assert state.cash_by_currency["USD"] == 0.0
    assert store.list_system_events_by_type("account_cash_flow", limit=10) == []


def test_replaying_a_whole_linked_set_applies_nothing_new(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=1_400_000, cash_by_currency={"KRW": 1_400_000, "USD": 0.0}),
        account_id="toss_brokerage",
    )
    legs = _conversion_legs("toss_brokerage")

    first = store.apply_account_cash_flows("run_1", legs)
    second = store.apply_account_cash_flows("run_2", legs)

    assert first["created"] is True
    assert second["created"] is False
    assert second["run_id"] == "run_1"
    state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert state.cash_by_currency["KRW"] == 0.0
    assert state.cash_by_currency["USD"] == 1_000.0


def test_a_partially_recorded_linked_set_is_refused(tmp_path):
    """Completing a half-recorded set would apply the missing side unasked."""
    store = StateStore(str(tmp_path / "state.db"))
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=1_400_000, cash_by_currency={"KRW": 1_400_000, "USD": 0.0}),
        account_id="toss_brokerage",
    )
    legs = _conversion_legs("toss_brokerage")
    store.apply_account_cash_flows("run_1", legs[:1])

    with pytest.raises(ValueError, match="partial record"):
        store.apply_account_cash_flows("run_2", legs)


def test_a_leg_on_an_unopened_ledger_names_the_account(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _open_ledger(store, "acct_from", 1_000_000)
    legs = [
        {
            "account_id": "acct_from",
            "amount": -250_000.0,
            "currency": "KRW",
            "event_payload": {"account_id": "acct_from", "duplicate_key": "leg:a"},
        },
        {
            "account_id": "acct_missing",
            "amount": 250_000.0,
            "currency": "KRW",
            "event_payload": {"account_id": "acct_missing", "duplicate_key": "leg:b"},
        },
    ]

    result = store.apply_account_cash_flows("run_1", legs)

    assert result["ledger_established"] is False
    assert result["missing_account_id"] == "acct_missing"
    assert _ledger_krw(store, "acct_from") == 1_000_000


def _conversion_service(tmp_path, krw=1_400_000.0, usd=0.0):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=krw, cash_by_currency={"KRW": krw, "USD": usd}),
        account_id="toss_brokerage",
    )
    return store, AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))


def _convert(service, **overrides):
    kwargs = {
        "account_id": "toss_brokerage",
        "from_currency": "KRW",
        "from_amount": 1_400_000.0,
        "to_currency": "USD",
        "to_amount": 995.0,
        "fee": 5.0,
        "transfer_id": "fx-1",
        "effective_at": "2026-08-02T12:00:00+00:00",
        "source": "operator_cli",
    }
    kwargs.update(overrides)
    return service.record_currency_conversion(**kwargs)


def test_a_conversion_books_the_spread_apart_from_the_principal(tmp_path):
    """The spread has to stay visible as a cost.

    A currency sleeve neutralises the conversion itself, so folding the spread
    into the converted amount would let both sleeves neutralise their whole leg
    and a real loss would disappear from every return.
    """
    store, service = _conversion_service(tmp_path)

    result = _convert(service)

    assert result.created is True
    state = store.load_latest_account_portfolio_state("toss_brokerage")
    # The ledger reflects what actually happened: 995 arrived, not 1000.
    assert state.cash_by_currency["KRW"] == 0.0
    assert state.cash_by_currency["USD"] == 995.0
    events = store.list_system_events_by_type("account_cash_flow", limit=10)
    by_class = {}
    for event in events:
        by_class.setdefault(event["payload"]["flow_class"], []).append(event["payload"])
    assert sorted(by_class) == ["cost", "fx_conversion"]
    assert len(by_class["fx_conversion"]) == 2
    assert by_class["cost"][0]["amount"] == -5.0
    assert by_class["cost"][0]["currency"] == "USD"
    # Every leg lands in the same performance period.
    assert {event["payload"]["effective_at"] for event in events} == {"2026-08-02T12:00:00+00:00"}


def test_a_conversion_without_a_fee_records_only_its_two_legs(tmp_path):
    store, service = _conversion_service(tmp_path)
    for currency, amount in (("KRW", -1_400_000.0), ("USD", 1_000.0)):
        store.upsert_cash_suspense(
            account_id="toss_brokerage",
            currency=currency,
            amount=amount,
            snapshot_id=1,
            observed_at="2026-08-02T12:00:00+00:00",
        )

    _convert(service, to_amount=1_000.0, fee=0.0)

    events = store.list_system_events_by_type("account_cash_flow", limit=10)
    assert len(events) == 2
    assert {event["payload"]["flow_class"] for event in events} == {"fx_conversion"}
    state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert state.cash_by_currency["USD"] == 1_000.0
    suspense = store.list_cash_suspense(account_id="toss_brokerage")
    assert {row["candidate_label"] for row in suspense} == {"fx_conversion"}
    assert {row["status"] for row in suspense} == {"classified"}


def test_a_conversion_whose_numbers_disagree_is_refused(tmp_path):
    """A mistyped figure must not reach the ledger."""
    store, service = _conversion_service(tmp_path)

    with pytest.raises(ValueError, match="does not add up"):
        # 1,400,000 KRW at 1/1400 is 1000 USD, not 995 + 20.
        _convert(service, to_amount=995.0, fee=20.0, rate=1 / 1400)

    assert store.list_system_events_by_type("account_cash_flow", limit=10) == []


def test_a_consistent_rate_is_accepted(tmp_path):
    store, service = _conversion_service(tmp_path)

    _convert(service, to_amount=995.0, fee=5.0, rate=1 / 1400)

    assert len(store.list_system_events_by_type("account_cash_flow", limit=10)) == 3


def test_replaying_a_conversion_applies_it_once(tmp_path):
    store, service = _conversion_service(tmp_path)

    first = _convert(service)
    second = _convert(service)

    assert first.created is True
    assert second.created is False
    state = store.load_latest_account_portfolio_state("toss_brokerage")
    assert state.cash_by_currency["USD"] == 995.0
    assert state.cash_by_currency["KRW"] == 0.0


def test_a_conversion_needs_two_currencies_and_an_id(tmp_path):
    store, service = _conversion_service(tmp_path)

    with pytest.raises(ValueError, match="two different currencies"):
        _convert(service, to_currency="KRW")
    with pytest.raises(ValueError, match="transfer_id"):
        _convert(service, transfer_id="")
    with pytest.raises(ValueError, match="fee cannot be negative"):
        _convert(service, fee=-1.0)
    assert store.list_system_events_by_type("account_cash_flow", limit=10) == []


def test_a_linked_flow_cannot_be_recorded_one_leg_at_a_time(tmp_path):
    """Linked movements must use the atomic transfer or conversion producer."""
    store = StateStore(str(tmp_path / "state.db"))
    _open_ledger(store, "acct_from", 1_000_000)
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))

    for flow_class in ("internal_transfer", "fx_conversion"):
        with pytest.raises(ValueError, match="atomically"):
            service.record(
                account_id="acct_from",
                amount=250_000,
                currency="KRW",
                flow_type="withdrawal",
                effective_at="2026-08-02T12:00:00+00:00",
                source="operator_cli",
                flow_class=flow_class,
            )
    assert store.list_system_events_by_type("account_cash_flow", limit=10) == []


def test_income_and_cost_can_be_recorded_without_a_transfer_id(tmp_path):
    """These stand alone: nothing moved between accounts."""
    store = StateStore(str(tmp_path / "state.db"))
    _open_ledger(store, "acct", 1_000)
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))

    service.record(
        account_id="acct",
        amount=50,
        currency="KRW",
        flow_type="deposit",
        effective_at="2026-08-02T12:00:00+00:00",
        source="operator_cli",
        flow_class="investment_income",
    )
    service.record(
        account_id="acct",
        amount=10,
        currency="KRW",
        flow_type="withdrawal",
        effective_at="2026-08-02T13:00:00+00:00",
        source="operator_cli",
        flow_class="cost",
    )

    classes = [
        event["payload"]["flow_class"]
        for event in store.list_system_events_by_type("account_cash_flow", limit=10)
    ]
    assert sorted(classes) == ["cost", "investment_income"]
    assert _ledger_krw(store, "acct") == 1_040


def test_an_internal_transfer_pair_is_recorded_as_internal(tmp_path):
    """Both legs classified, so the pairing check can actually see them."""
    store = StateStore(str(tmp_path / "state.db"))
    _open_ledger(store, "acct_from", 1_000_000)
    _open_ledger(store, "acct_to", 0)
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))

    service.record_internal_transfer(
        from_account_id="acct_from",
        from_currency="KRW",
        from_amount=250_000,
        to_account_id="acct_to",
        to_currency="KRW",
        to_amount=250_000,
        effective_at="2026-08-02T12:00:00+00:00",
        source="operator_cli",
        transfer_id="move-1",
    )

    events = store.list_system_events_by_type("account_cash_flow", limit=10)
    assert len(events) == 2
    assert {event["payload"]["flow_class"] for event in events} == {"internal_transfer"}
    assert _ledger_krw(store, "acct_from") == 750_000
    assert _ledger_krw(store, "acct_to") == 250_000


def test_internal_transfer_is_all_or_nothing_when_destination_ledger_is_missing(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _open_ledger(store, "acct_from", 1_000_000)
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))

    with pytest.raises(ValueError, match="ledger is not established"):
        service.record_internal_transfer(
            from_account_id="acct_from",
            from_currency="KRW",
            from_amount=250_000,
            to_account_id="acct_missing",
            to_currency="KRW",
            to_amount=250_000,
            effective_at="2026-08-02T12:00:00+00:00",
            source="operator_cli",
            transfer_id="move-missing",
        )

    assert _ledger_krw(store, "acct_from") == 1_000_000
    assert store.list_system_events_by_type("account_cash_flow", limit=10) == []


def test_same_currency_internal_transfer_requires_matching_amounts(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _open_ledger(store, "acct_from", 1_000_000)
    _open_ledger(store, "acct_to", 0)
    service = AccountCashFlowService(store, AuditLogger(tmp_path / "audit.jsonl"))

    with pytest.raises(ValueError, match="amounts must match"):
        service.record_internal_transfer(
            from_account_id="acct_from",
            from_currency="KRW",
            from_amount=250_000,
            to_account_id="acct_to",
            to_currency="KRW",
            to_amount=249_000,
            effective_at="2026-08-02T12:00:00+00:00",
            source="operator_cli",
            transfer_id="move-mismatch",
        )

    assert _ledger_krw(store, "acct_from") == 1_000_000
    assert _ledger_krw(store, "acct_to") == 0


def _save_kis_snapshot(
    store: StateStore,
    run_id: str,
    cash_by_currency: dict[str, float],
    *,
    positions: list[dict] | None = None,
    order_fills: list[dict] | None = None,
    account_id: str = "kis_brokerage",
) -> None:
    store.save_broker_account_snapshot(
        run_id,
        account_id,
        {
            "account_id": account_id,
            "account": {
                "account_id": account_id,
                "source": "kis_domestic_readonly",
                "cash": float(cash_by_currency.get("KRW", 0.0)),
                "cash_by_currency": dict(cash_by_currency),
                "positions": positions or [],
            },
            "unfilled_orders": [],
            "order_fills": order_fills or [],
        },
    )


def test_broker_reported_cash_needs_the_same_stability_as_a_proxy(tmp_path):
    """One snapshot's difference is not evidence of an external flow.

    KIS reports real deposits, but a real balance still moves for settlement
    and fees, so a single step is not something to ask an operator to confirm.
    """
    store = StateStore(str(tmp_path / "state.db"))
    _save_kis_snapshot(store, "baseline", {"KRW": 1_000_000.0})
    _save_kis_snapshot(store, "changed-1", {"KRW": 2_000_000.0})
    _save_kis_snapshot(store, "changed-2", {"KRW": 1_500_000.0})
    _save_kis_snapshot(store, "changed-3", {"KRW": 2_000_000.0})

    assert CashFlowCandidateDetector(store).detect("kis_brokerage") is None


def test_a_stable_broker_reported_change_is_offered(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _save_kis_snapshot(store, "baseline", {"KRW": 1_000_000.0})
    for run_id in ("changed-1", "changed-2", "changed-3"):
        _save_kis_snapshot(store, run_id, {"KRW": 2_000_000.0})

    candidate = CashFlowCandidateDetector(store).detect("kis_brokerage")

    assert candidate is not None
    assert candidate.cash_basis == "broker_reported"
    assert candidate.flow_type == "deposit"
    assert candidate.amount == 1_000_000.0
    assert candidate.evidence()["kind"] == "stable_broker_reported_cash_change"


def test_a_recently_settling_fill_blocks_a_candidate(tmp_path):
    """A fill from before the window can still be moving cash now.

    Settlement is the account's own trading catching up, not investor money
    arriving, so it must never be offered as a deposit.
    """
    store = StateStore(str(tmp_path / "state.db"))
    recent_fill = [{"symbol": "005930", "submitted_at": utc_now().isoformat()}]
    _save_kis_snapshot(store, "baseline", {"KRW": 1_000_000.0}, order_fills=recent_fill)
    for run_id in ("changed-1", "changed-2", "changed-3"):
        _save_kis_snapshot(store, run_id, {"KRW": 2_000_000.0}, order_fills=recent_fill)

    assert CashFlowCandidateDetector(store).detect("kis_brokerage") is None


def test_a_long_settled_fill_does_not_block_a_candidate(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    old_fill = [{"symbol": "005930", "submitted_at": (utc_now() - timedelta(days=30)).isoformat()}]
    _save_kis_snapshot(store, "baseline", {"KRW": 1_000_000.0}, order_fills=old_fill)
    for run_id in ("changed-1", "changed-2", "changed-3"):
        _save_kis_snapshot(store, run_id, {"KRW": 2_000_000.0}, order_fills=old_fill)

    assert CashFlowCandidateDetector(store).detect("kis_brokerage") is not None


def test_broker_reported_position_changes_block_a_candidate(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _save_kis_snapshot(store, "baseline", {"KRW": 1_000_000.0})
    _save_kis_snapshot(
        store,
        "changed-1",
        {"KRW": 2_000_000.0},
        positions=[{"symbol": "005930", "quantity": 1.0}],
    )
    for run_id in ("changed-2", "changed-3"):
        _save_kis_snapshot(store, run_id, {"KRW": 2_000_000.0})

    assert CashFlowCandidateDetector(store).detect("kis_brokerage") is None


def test_broker_reported_conversion_is_not_offered_as_a_flow(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    _save_kis_snapshot(store, "baseline", {"KRW": 1_400_000.0, "USD": 100.0})
    for run_id in ("changed-1", "changed-2", "changed-3"):
        _save_kis_snapshot(store, run_id, {"KRW": 100_000.0, "USD": 1_100.0})

    assert CashFlowCandidateDetector(store).detect("kis_brokerage") is None


def test_detection_reaches_past_other_accounts_snapshots(tmp_path):
    """Filtering after the query made detection fail as accounts were added."""
    store = StateStore(str(tmp_path / "state.db"))
    _save_kis_snapshot(store, "baseline", {"KRW": 1_000_000.0})
    for index in range(120):
        _save_kis_snapshot(store, f"noise-{index}", {"KRW": 5.0}, account_id="other_account")
    for run_id in ("changed-1", "changed-2", "changed-3"):
        _save_kis_snapshot(store, run_id, {"KRW": 2_000_000.0})

    assert CashFlowCandidateDetector(store).detect("kis_brokerage") is not None

from maestro.execution.account_cash_flows import AccountCashFlowService
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

import pytest

from maestro.dashboard.read_models import build_account_bucket_attribution_table
from maestro.monitoring.audit_logger import AuditLogger
from maestro.portfolio.account_attribution import (
    AccountAttributionReconciliationService,
    AttributionPosition,
    AttributionValidationError,
    apply_broker_snapshot_delta,
    bucket_portfolio_state,
    build_auto_baseline,
)
from maestro.state.store import StateStore


def test_auto_baseline_assigns_strategy_symbols_to_strategy_bucket():
    positions = build_auto_baseline(
        account_id="toss_brokerage",
        broker_positions={"QQQ": 4.0, "AAPL": 3.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ", "SPY"}},
    )

    assert positions == [
        AttributionPosition(
            account_id="toss_brokerage",
            symbol="AAPL",
            bucket_id="manual",
            quantity=3.0,
            source="auto_baseline",
            confidence="medium",
        ),
        AttributionPosition(
            account_id="toss_brokerage",
            symbol="QQQ",
            bucket_id="crescendo_us",
            quantity=4.0,
            source="auto_baseline",
            confidence="medium",
        ),
    ]


def test_external_buy_increases_manual_bucket_even_for_strategy_symbol():
    positions, events = apply_broker_snapshot_delta(
        previous=[
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="crescendo_us",
                quantity=4.0,
            )
        ],
        broker_positions={"QQQ": 5.0},
    )

    assert events == [
        {
            "event_type": "external_manual_buy",
            "account_id": "toss_brokerage",
            "symbol": "QQQ",
            "bucket_id": "manual",
            "quantity": 1.0,
        }
    ]
    assert _quantity(positions, "manual", "QQQ") == 1.0
    assert _quantity(positions, "crescendo_us", "QQQ") == 4.0


def test_external_sell_reduces_manual_before_strategy_bucket():
    positions, events = apply_broker_snapshot_delta(
        previous=[
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="manual",
                quantity=3.0,
            ),
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="crescendo_us",
                quantity=5.0,
            ),
        ],
        broker_positions={"QQQ": 6.0},
    )

    assert _quantity(positions, "manual", "QQQ") == 1.0
    assert _quantity(positions, "crescendo_us", "QQQ") == 5.0
    assert events[0]["event_type"] == "external_manual_sell"
    assert events[0]["bucket_id"] == "manual"


def test_external_sell_warns_when_manual_bucket_is_insufficient():
    positions, events = apply_broker_snapshot_delta(
        previous=[
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="manual",
                quantity=1.0,
            ),
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="crescendo_us",
                quantity=5.0,
            ),
        ],
        broker_positions={"QQQ": 3.0},
    )

    assert _quantity(positions, "manual", "QQQ") == 0.0
    assert _quantity(positions, "crescendo_us", "QQQ") == 3.0
    assert events[-1]["event_type"] == "external_strategy_reduction_warning"
    assert events[-1]["bucket_id"] == "crescendo_us"
    assert events[-1]["quantity"] == pytest.approx(2.0)


def test_state_store_persists_account_attribution_snapshots(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))

    store.save_account_attribution_snapshot(
        "run_1",
        [
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="crescendo_us",
                quantity=4.0,
            ),
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="manual",
                quantity=1.0,
            ),
        ],
    )

    rows = store.list_account_attribution_snapshots()

    assert [
        (
            row["run_id"],
            row["account_id"],
            row["symbol"],
            row["bucket_id"],
            row["payload"]["quantity"],
        )
        for row in rows
    ] == [
        ("run_1", "toss_brokerage", "QQQ", "manual", 1.0),
        ("run_1", "toss_brokerage", "QQQ", "crescendo_us", 4.0),
    ]


def test_account_bucket_attribution_read_model_reports_actual_weights(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    store.save_account_attribution_snapshot(
        "run_1",
        [
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="crescendo_us",
                quantity=4.0,
            ),
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="AAPL",
                bucket_id="manual",
                quantity=2.0,
            ),
        ],
    )

    rows = build_account_bucket_attribution_table(
        store,
        prices={"QQQ": 100.0, "AAPL": 50.0},
        target_weights={
            "toss_brokerage": {
                "crescendo_us": 0.7,
                "manual": 0.3,
            }
        },
    )

    assert [
        (
            row["account_id"],
            row["bucket_id"],
            row["market_value"],
            row["target_weight"],
            row["actual_weight"],
            row["status"],
        )
        for row in rows
    ] == [
        ("toss_brokerage", "crescendo_us", 400.0, 0.7, 0.8, "over_target"),
        ("toss_brokerage", "manual", 100.0, 0.3, 0.2, "under_target"),
    ]


def test_bucket_portfolio_state_uses_strategy_quantity_not_manual_quantity():
    state = bucket_portfolio_state(
        positions=[
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="crescendo_us",
                quantity=4.0,
            ),
            AttributionPosition(
                account_id="toss_brokerage",
                symbol="QQQ",
                bucket_id="manual",
                quantity=6.0,
            ),
        ],
        account_id="toss_brokerage",
        bucket_id="crescendo_us",
        cash=100.0,
        cash_by_currency={"USD": 100.0},
    )

    assert state.positions == {"QQQ": 4.0}
    assert state.cash_by_currency == {"USD": 100.0}


def test_broker_sync_creates_unapproved_auto_baseline(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    service = AccountAttributionReconciliationService(store, audit)

    positions = service.reconcile_broker_snapshot(
        run_id="run_sync_1",
        account_id="toss_brokerage",
        broker_snapshot_id=11,
        broker_positions={"QQQ": 4.0, "AAPL": 2.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ", "SPY"}},
    )

    assert _quantity(positions, "crescendo_us", "QQQ") == 4.0
    assert _quantity(positions, "manual", "AAPL") == 2.0
    assert {position.approved for position in positions} == {False}
    assert {position.broker_snapshot_id for position in positions} == {11}
    assert {position.version for position in positions} == {1}
    assert (
        store.load_latest_system_event("account_attribution_reconciliation")["payload"][
            "status"
        ]
        == "baseline_pending_approval"
    )


def test_adopted_attribution_reconciles_external_changes_manual_first(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    service = AccountAttributionReconciliationService(store, audit)
    service.reconcile_broker_snapshot(
        run_id="run_sync_1",
        account_id="toss_brokerage",
        broker_snapshot_id=11,
        broker_positions={"QQQ": 4.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ"}},
    )
    service.adopt_latest(
        run_id="run_adopt",
        account_id="toss_brokerage",
        reason="operator verified",
        adopted_by="cli",
    )

    positions = service.reconcile_broker_snapshot(
        run_id="run_sync_2",
        account_id="toss_brokerage",
        broker_snapshot_id=12,
        broker_positions={"QQQ": 5.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ"}},
    )

    assert _quantity(positions, "crescendo_us", "QQQ") == 4.0
    assert _quantity(positions, "manual", "QQQ") == 1.0
    assert {position.approved for position in positions} == {True}
    assert {position.broker_snapshot_id for position in positions} == {12}
    assert {position.version for position in positions} == {3}


def test_operator_reclassifies_adopted_position_between_buckets(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    service = AccountAttributionReconciliationService(store, audit)
    service.reconcile_broker_snapshot(
        run_id="run_sync_1",
        account_id="toss_brokerage",
        broker_snapshot_id=11,
        broker_positions={"QQQM": 22.0},
        strategy_symbols_by_bucket={},
    )
    service.adopt_latest(
        run_id="run_adopt",
        account_id="toss_brokerage",
        reason="operator verified",
        adopted_by="cli",
    )

    positions = service.reclassify_position(
        run_id="run_reclassify",
        account_id="toss_brokerage",
        symbol="QQQM",
        from_bucket_id="manual",
        to_bucket_id="crescendo_us",
        quantity=22.0,
        reason="QQQM is a QQQ substitute",
        reclassified_by="cli",
    )

    assert _quantity(positions, "manual", "QQQM") == 0.0
    assert _quantity(positions, "crescendo_us", "QQQM") == 22.0
    assert {position.broker_snapshot_id for position in positions} == {11}
    assert {position.approved for position in positions} == {True}
    event = store.load_latest_system_event("account_attribution_reconciliation")
    assert event["payload"]["status"] == "operator_reclassified"
    assert event["payload"]["reason"] == "QQQM is a QQQ substitute"


def test_operator_reclassification_rejects_excess_quantity(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    service = AccountAttributionReconciliationService(store, audit)
    service.reconcile_broker_snapshot(
        run_id="run_sync_1",
        account_id="toss_brokerage",
        broker_snapshot_id=11,
        broker_positions={"QQQM": 22.0},
        strategy_symbols_by_bucket={},
    )
    service.adopt_latest(
        run_id="run_adopt",
        account_id="toss_brokerage",
        reason="operator verified",
        adopted_by="cli",
    )

    with pytest.raises(AttributionValidationError, match="exceeds source quantity"):
        service.reclassify_position(
            run_id="run_reclassify",
            account_id="toss_brokerage",
            symbol="QQQM",
            from_bucket_id="manual",
            to_bucket_id="crescendo_us",
            quantity=23.0,
            reason="QQQM is a QQQ substitute",
            reclassified_by="cli",
        )


def test_operator_restores_warning_backed_reduction_before_delayed_sell_fill(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    service = AccountAttributionReconciliationService(store, audit)
    service.reconcile_broker_snapshot(
        run_id="run_sync_1",
        account_id="toss_brokerage",
        broker_snapshot_id=11,
        broker_positions={"QQQM": 22.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQM"}},
    )
    service.adopt_latest(
        run_id="run_adopt",
        account_id="toss_brokerage",
        reason="operator verified",
        adopted_by="cli",
    )
    reduced = service.reconcile_broker_snapshot(
        run_id="run_sync_after_sell",
        account_id="toss_brokerage",
        broker_snapshot_id=12,
        broker_positions={},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQM"}},
    )
    assert _quantity(reduced, "crescendo_us", "QQQM") == 0.0

    restored = service.restore_pending_maestro_sell(
        run_id="run_restore",
        account_id="toss_brokerage",
        symbol="QQQM",
        bucket_id="crescendo_us",
        quantity=22.0,
        reason="broker snapshot preceded fill reconciliation",
        restored_by="cli",
    )
    final = service.apply_maestro_fill(
        run_id="run_fill",
        account_id="toss_brokerage",
        bucket_id="crescendo_us",
        symbol="QQQM",
        side="sell",
        quantity=22.0,
        fill_key="TOSS-SELL:22",
    )

    assert _quantity(restored, "crescendo_us", "QQQM") == 22.0
    assert _quantity(final, "crescendo_us", "QQQM") == 0.0
    with pytest.raises(AttributionValidationError, match="warning-backed"):
        service.restore_pending_maestro_sell(
            run_id="run_restore_again",
            account_id="toss_brokerage",
            symbol="QQQM",
            bucket_id="crescendo_us",
            quantity=22.0,
            reason="duplicate",
            restored_by="cli",
        )


def test_attribution_gate_requires_adoption_matching_snapshot_and_quantities(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    service = AccountAttributionReconciliationService(store, audit)
    service.reconcile_broker_snapshot(
        run_id="run_sync_1",
        account_id="toss_brokerage",
        broker_snapshot_id=11,
        broker_positions={"QQQ": 4.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ"}},
    )

    with pytest.raises(AttributionValidationError, match="not adopted"):
        service.require_ready(
            account_id="toss_brokerage",
            broker_snapshot_id=11,
            broker_positions={"QQQ": 4.0},
        )

    service.adopt_latest(
        run_id="run_adopt",
        account_id="toss_brokerage",
        reason="operator verified",
        adopted_by="telegram:42",
    )

    with pytest.raises(AttributionValidationError, match="broker snapshot"):
        service.require_ready(
            account_id="toss_brokerage",
            broker_snapshot_id=12,
            broker_positions={"QQQ": 4.0},
        )
    with pytest.raises(AttributionValidationError, match="quantity mismatch"):
        service.require_ready(
            account_id="toss_brokerage",
            broker_snapshot_id=11,
            broker_positions={"QQQ": 5.0},
        )

    ready = service.require_ready(
        account_id="toss_brokerage",
        broker_snapshot_id=11,
        broker_positions={"QQQ": 4.0},
    )
    assert _quantity(ready, "crescendo_us", "QQQ") == 4.0


def test_maestro_fill_reclassifies_manual_quantity_to_strategy_bucket(tmp_path):
    store = StateStore(str(tmp_path / "state.db"))
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    service = AccountAttributionReconciliationService(store, audit)
    service.reconcile_broker_snapshot(
        run_id="run_sync_1",
        account_id="toss_brokerage",
        broker_snapshot_id=11,
        broker_positions={"QQQ": 4.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ"}},
    )
    service.adopt_latest(
        run_id="run_adopt",
        account_id="toss_brokerage",
        reason="operator verified",
        adopted_by="cli",
    )
    service.reconcile_broker_snapshot(
        run_id="run_sync_after_fill",
        account_id="toss_brokerage",
        broker_snapshot_id=12,
        broker_positions={"QQQ": 5.0},
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ"}},
    )

    positions = service.apply_maestro_fill(
        run_id="run_fill",
        account_id="toss_brokerage",
        bucket_id="crescendo_us",
        symbol="QQQ",
        side="buy",
        quantity=1.0,
        fill_key="TOSS-1:1",
    )
    duplicate = service.apply_maestro_fill(
        run_id="run_fill_duplicate",
        account_id="toss_brokerage",
        bucket_id="crescendo_us",
        symbol="QQQ",
        side="buy",
        quantity=1.0,
        fill_key="TOSS-1:1",
    )

    assert _quantity(positions, "manual", "QQQ") == 0.0
    assert _quantity(positions, "crescendo_us", "QQQ") == 5.0
    assert duplicate == positions


def _quantity(
    positions: list[AttributionPosition],
    bucket_id: str,
    symbol: str,
) -> float:
    for position in positions:
        if position.bucket_id == bucket_id and position.symbol == symbol:
            return position.quantity
    return 0.0

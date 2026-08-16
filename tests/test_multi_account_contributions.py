import pytest
import yaml
from pydantic import ValidationError

from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from tests.contribution_fixtures import _multi_account_config, _multi_account_raw


def test_multi_account_contribution_config_loads_group(tmp_path):
    config = _multi_account_config(tmp_path)

    group = config.multi_account_contributions["tranquillo"]
    assert group.strategy_id == "tranquillo"
    assert group.allocation_basis == "aggregate_current_holdings"
    assert [target.account_id for target in group.account_targets] == ["kis_ps", "kis_isa"]


def test_multi_account_contribution_uses_virtual_strategy_account_id(tmp_path):
    config = _multi_account_config(tmp_path)
    strategy = next(strategy for strategy in config.strategies if strategy.id == "tranquillo")

    assert strategy.account_id == "multi_account_contributions.tranquillo"
    assert strategy.execution_sleeve is None
    assert config.effective_strategy_order_generation_mode(strategy) == "buy_only_contribution"


def test_multi_account_contribution_rejects_direct_strategy_account_id(tmp_path):
    raw = _multi_account_raw(tmp_path)
    raw["accounts"].append(
        {"id": "kis_mock", "broker": "sandbox", "environment": "paper_trading", "enabled": True}
    )
    raw["strategies"][0]["account_id"] = "kis_mock"
    config_path = tmp_path / "invalid_direct_strategy_account.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="account_id must be"):
        load_config(config_path)


def test_multi_account_contribution_rejects_strategy_execution_sleeve(tmp_path):
    raw = _multi_account_raw(tmp_path)
    raw["strategies"][0]["execution_sleeve"] = "tranquillo_isa"
    config_path = tmp_path / "invalid_virtual_strategy_sleeve.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="must not set execution_sleeve"):
        load_config(config_path)


def test_multi_account_contribution_rejects_disallowed_symbol(tmp_path):
    raw = _multi_account_raw(tmp_path)
    raw["multi_account_contributions"]["tranquillo"]["account_targets"][0]["allowed_symbols"] = [
        "MISSING"
    ]
    config_path = tmp_path / "invalid_multi_account.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="unsupported allowed_symbols"):
        load_config(config_path)


def test_tranquillo_multi_account_allocation_uses_ps_schd_then_isa_to_restore_ratio(
    funding_orchestrator,
):
    orchestrator, store = funding_orchestrator(
        isa_cash=2_000_000,
        ps_cash=500_000,
        ps_positions=[{"symbol": "MOCK_ETF_B", "quantity": 60_000}],
        isa_positions=[{"symbol": "MOCK_ETF_A", "quantity": 10_000}],
    )

    summary = orchestrator.run_signal(strategy_ids=["tranquillo"])

    signal = store.load_signal_package(summary.signal_run_id)
    orders = signal["orders_preview"]
    ps_orders = [order for order in orders if order["account_id"] == "kis_ps"]
    isa_orders = [order for order in orders if order["account_id"] == "kis_isa"]
    assert signal["status"] == "action_required"
    assert len(ps_orders) == 1
    assert ps_orders[0]["symbol"] == "MOCK_ETF_B"
    assert ps_orders[0]["notional"] == pytest.approx(500_000)
    assert [order["symbol"] for order in isa_orders] == ["MOCK_ETF_A"]
    assert isa_orders[0]["notional"] == pytest.approx(2_000_000)
    assert all(order["side"] == "buy" for order in orders)
    assert {order["metadata"]["contribution_group_id"] for order in orders} == {"tranquillo"}


def test_tranquillo_multi_account_isa_cash_below_minimum_creates_range_funding_request(
    funding_orchestrator,
):
    orchestrator, store = funding_orchestrator(isa_cash=1_000_000, ps_cash=500_000)

    summary = orchestrator.run_signal(strategy_ids=["tranquillo"])

    signal = store.load_signal_package(summary.signal_run_id)
    requests = signal["funding_requests"]
    isa_request = next(request for request in requests if request["account_id"] == "kis_isa")
    assert signal["status"] == "action_required"
    assert isa_request["execution_sleeve"] == "tranquillo_isa"
    assert isa_request["available_cash"] == 1_000_000
    assert isa_request["min_monthly_budget"] == 1_660_000
    assert isa_request["required_shortfall"] == 660_000
    assert isa_request["max_monthly_budget"] == 4_000_000
    assert isa_request["recommended_top_up"] == 660_000


def test_tranquillo_multi_account_isa_budget_request_blocks_isa_orders_only(
    funding_orchestrator,
):
    orchestrator, store = funding_orchestrator(
        isa_cash=8_000_000,
        ps_cash=500_000,
        isa_budget_request=True,
    )

    summary = orchestrator.run_signal(strategy_ids=["tranquillo"])

    signal = store.load_signal_package(summary.signal_run_id)
    orders = signal["orders_preview"]
    requests = signal["budget_requests"]
    assert signal["status"] == "budget_required"
    assert signal["action_required"] is False
    assert signal["budget_requests_count"] == 1
    assert [order["account_id"] for order in orders] == ["kis_ps"]
    assert orders[0]["notional"] == pytest.approx(500_000)
    assert requests[0]["account_id"] == "kis_isa"
    assert requests[0]["execution_sleeve"] == "tranquillo_isa"
    assert requests[0]["available_cash"] == 8_000_000
    assert requests[0]["min_monthly_budget"] == 1_660_000
    assert requests[0]["recommended_budget"] == 4_000_000
    assert requests[0]["selectable_max_budget"] == 8_000_000


def test_multi_account_contribution_applies_fee_buffer_once(funding_orchestrator):
    orchestrator, store = funding_orchestrator(
        isa_cash=2_000_000,
        ps_cash=501_003,
        isa_budget_request=True,
        fee_buffer_pct=0.002,
    )

    summary = orchestrator.run_signal(strategy_ids=["tranquillo"])

    signal = store.load_signal_package(summary.signal_run_id)
    ps_orders = [order for order in signal["orders_preview"] if order["account_id"] == "kis_ps"]
    assert len(ps_orders) == 1
    assert ps_orders[0]["notional"] == pytest.approx(500_000)
    assert signal["budget_requests"][0]["available_cash"] == pytest.approx(1_996_000)


def test_tranquillo_multi_account_budget_decision_can_exceed_legacy_max(
    funding_orchestrator,
):
    orchestrator, store = funding_orchestrator(
        isa_cash=8_000_000,
        ps_cash=500_000,
        isa_budget_request=True,
    )
    store.save_system_event(
        "operator_budget",
        "contribution_budget_request_decision",
        {
            "request_id": "budget_req_manual",
            "status": "selected",
            "strategy_ids": ["tranquillo"],
            "contribution_group_id": "tranquillo",
            "account_id": "kis_isa",
            "execution_sleeve": "tranquillo_isa",
            "currency": "KRW",
            "selected_budget": 8_000_000,
            "month_key": utc_now().strftime("%Y-%m"),
        },
    )

    summary = orchestrator.run_signal(strategy_ids=["tranquillo"])

    signal = store.load_signal_package(summary.signal_run_id)
    isa_orders = [order for order in signal["orders_preview"] if order["account_id"] == "kis_isa"]
    assert signal["budget_requests"] == []
    assert sum(order["notional"] for order in isa_orders) == pytest.approx(8_000_000)
    assert sum(order["notional"] for order in signal["orders_preview"]) == pytest.approx(8_500_000)


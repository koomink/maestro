from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from maestro.config.loader import load_config
from maestro.core.clock import utc_now
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.sdk import (
    BaseStrategyPlugin,
    DataBundle,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    TargetAllocationResult,
)
from maestro.state.store import StateStore


class MultiAccountTranquilloTestStrategy(BaseStrategyPlugin):
    def manifest(self) -> StrategyManifest:
        return StrategyManifest(
            strategy_id="tranquillo",
            name="Tranquillo Test",
            version="0.1.0",
            description="Multi-account contribution test strategy.",
            supported_modes=["paper", "live_approval"],
            supported_asset_types=["cash", "domestic_etf"],
            result_type="target_allocation",
            requires_data=["price"],
            can_run_live=True,
        )

    def build_data_requests(self, context: StrategyContext) -> list[DataRequest]:
        del context
        return [
            DataRequest(symbol="MOCK_ETF_A", asset_type="domestic_etf", data_type="price"),
            DataRequest(symbol="MOCK_ETF_B", asset_type="domestic_etf", data_type="price"),
        ]

    def run(self, data_bundle: DataBundle, context: StrategyContext) -> TargetAllocationResult:
        del data_bundle
        return TargetAllocationResult(
            strategy_id=context.strategy_id,
            strategy_version="0.1.0",
            timestamp=context.timestamp,
            allocations={},
            allocation_sleeves={"KRW": {"MOCK_ETF_A": 0.6, "MOCK_ETF_B": 0.4}},
            confidence=1.0,
            time_horizon="monthly",
            rationale="Aggregate 60/40 contribution target.",
        )


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
    tmp_path,
):
    config = _multi_account_config(tmp_path, isa_cash=2_000_000, ps_cash=500_000)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_account_snapshot(
        store,
        "kis_ps",
        cash=500_000,
        positions=[{"symbol": "MOCK_ETF_B", "quantity": 60_000}],
    )
    _save_account_snapshot(
        store,
        "kis_isa",
        cash=2_000_000,
        positions=[{"symbol": "MOCK_ETF_A", "quantity": 10_000}],
    )

    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["tranquillo"])

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
    tmp_path,
):
    config = _multi_account_config(tmp_path, isa_cash=1_000_000, ps_cash=500_000)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_account_snapshot(store, "kis_ps", cash=500_000, positions=[])
    _save_account_snapshot(store, "kis_isa", cash=1_000_000, positions=[])

    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["tranquillo"])

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
    tmp_path,
):
    config = _multi_account_config(
        tmp_path,
        isa_cash=8_000_000,
        ps_cash=500_000,
        isa_budget_request=True,
    )
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_account_snapshot(store, "kis_ps", cash=500_000, positions=[])
    _save_account_snapshot(store, "kis_isa", cash=8_000_000, positions=[])

    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["tranquillo"])

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


def test_multi_account_contribution_applies_fee_buffer_once(tmp_path):
    config = _multi_account_config(
        tmp_path,
        isa_cash=2_000_000,
        ps_cash=501_003,
        isa_budget_request=True,
        fee_buffer_pct=0.002,
    )
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_account_snapshot(store, "kis_ps", cash=501_003, positions=[])
    _save_account_snapshot(store, "kis_isa", cash=2_000_000, positions=[])

    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["tranquillo"])

    signal = store.load_signal_package(summary.signal_run_id)
    ps_orders = [order for order in signal["orders_preview"] if order["account_id"] == "kis_ps"]
    assert len(ps_orders) == 1
    assert ps_orders[0]["notional"] == pytest.approx(500_000)
    assert signal["budget_requests"][0]["available_cash"] == pytest.approx(1_996_000)


def test_tranquillo_multi_account_budget_decision_can_exceed_legacy_max(
    tmp_path,
):
    config = _multi_account_config(
        tmp_path,
        isa_cash=8_000_000,
        ps_cash=500_000,
        isa_budget_request=True,
    )
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    _save_account_snapshot(store, "kis_ps", cash=500_000, positions=[])
    _save_account_snapshot(store, "kis_isa", cash=8_000_000, positions=[])
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

    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["tranquillo"])

    signal = store.load_signal_package(summary.signal_run_id)
    isa_orders = [order for order in signal["orders_preview"] if order["account_id"] == "kis_isa"]
    assert signal["budget_requests"] == []
    assert sum(order["notional"] for order in isa_orders) == pytest.approx(8_000_000)
    assert sum(order["notional"] for order in signal["orders_preview"]) == pytest.approx(8_500_000)


def _multi_account_config(
    tmp_path,
    *,
    isa_cash=2_000_000,
    ps_cash=500_000,
    isa_budget_request=False,
    fee_buffer_pct=0.0,
):
    raw = _multi_account_raw(tmp_path)
    raw["state"]["sqlite_path"] = str(tmp_path / f"state_{isa_cash}_{ps_cash}.db")
    raw["execution_sleeves"]["accounts"]["kis_isa"]["tranquillo_isa"]["contribution"][
        "monthly_budget"
    ] = 4_000_000
    raw["execution_sleeves"]["accounts"]["kis_ps"]["tranquillo_ps"]["contribution"][
        "monthly_budget"
    ] = 500_000
    if isa_budget_request:
        raw["execution_sleeves"]["accounts"]["kis_isa"]["tranquillo_isa"]["contribution"][
            "budget_request"
        ] = {"enabled": True}
    raw["execution"]["live_order_limits"] = {"fee_buffer_pct": fee_buffer_pct}
    config_path = tmp_path / f"multi_account_{isa_cash}_{ps_cash}.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path)


def _multi_account_raw(tmp_path):
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_approval_console.yaml").read_text())
    raw["portfolio"] = {
        "base_currency": "KRW",
        "initial_cash": 1,
        "allowed_symbols": ["CASH_KRW", "MOCK_ETF_A", "MOCK_ETF_B"],
        "allocation_mode": "currency_sleeves",
        "cash_by_currency": {"KRW": 0},
        "currency_sleeves": {
            "KRW": {
                "cash_symbol": "CASH_KRW",
                "symbols": ["MOCK_ETF_A", "MOCK_ETF_B"],
            }
        },
    }
    raw["universe"] = {
        "instruments": [
            _instrument("CASH_KRW", "cash", "KRW", "kis_domestic_stock"),
            _instrument("MOCK_ETF_A", "domestic_etf", "KRW", "kis_domestic_stock"),
            _instrument("MOCK_ETF_B", "domestic_etf", "KRW", "kis_domestic_stock"),
        ]
    }
    raw["strategies"] = [
        {
            "id": "tranquillo",
            "enabled": True,
            "signal_enabled": True,
            "weight": 1.0,
            "account_id": "multi_account_contributions.tranquillo",
            "order_posture": "dry_run",
            "entrypoint": f"{__name__}:MultiAccountTranquilloTestStrategy",
            "config": {},
        }
    ]
    raw["accounts"] = [
        {"id": "kis_ps", "broker": "sandbox", "environment": "paper_trading", "enabled": True},
        {"id": "kis_isa", "broker": "sandbox", "environment": "paper_trading", "enabled": True},
    ]
    raw["execution_sleeves"] = {
        "accounts": {
            "kis_ps": {
                "tranquillo_ps": {
                    "currency_sleeve": "KRW",
                    "target_weight": 1.0,
                    "order_generation_mode": "buy_only_contribution",
                    "contribution": _contribution(
                        monthly_budget=500_000,
                        min_monthly_budget=500_000,
                        max_monthly_budget=500_000,
                    ),
                }
            },
            "kis_isa": {
                "tranquillo_isa": {
                    "currency_sleeve": "KRW",
                    "target_weight": 1.0,
                    "order_generation_mode": "buy_only_contribution",
                    "contribution": _contribution(
                        monthly_budget=4_000_000,
                        min_monthly_budget=1_660_000,
                        max_monthly_budget=4_000_000,
                    ),
                }
            },
        }
    }
    raw["multi_account_contributions"] = {
        "tranquillo": {
            "strategy_id": "tranquillo",
            "allocation_basis": "aggregate_current_holdings",
            "order_generation_mode": "buy_only_contribution",
            "account_targets": [
                {
                    "account_id": "kis_ps",
                    "execution_sleeve": "tranquillo_ps",
                    "allowed_symbols": ["MOCK_ETF_B"],
                    "monthly_budget": 500_000,
                },
                {
                    "account_id": "kis_isa",
                    "execution_sleeve": "tranquillo_isa",
                    "allowed_symbols": ["MOCK_ETF_A", "MOCK_ETF_B"],
                    "min_monthly_budget": 1_660_000,
                    "max_monthly_budget": 4_000_000,
                },
            ],
        }
    }
    raw["datahub"] = {"provider": "mock"}
    raw["execution"]["order_posture"] = "dry_run"
    raw["execution"]["market_session"] = {
        "required": False,
        "timezone": "Asia/Seoul",
        "open": "09:00",
        "close": "15:30",
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
        "holidays": [],
    }
    raw["execution"]["live_order_limits"] = {"fee_buffer_pct": 0.0}
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    return raw


def _contribution(*, monthly_budget, min_monthly_budget, max_monthly_budget):
    return {
        "enabled": True,
        "currency": "KRW",
        "sleeve": "KRW",
        "monthly_budget": monthly_budget,
        "min_monthly_budget": min_monthly_budget,
        "max_monthly_budget": max_monthly_budget,
        "buy_day": 1,
        "non_trading_day_policy": "next_trading_day",
        "target_policy": "buy_only_toward_target",
        "funding_request": {"enabled": True},
    }


def _instrument(symbol, asset_type, currency, broker_product):
    return {
        "symbol": symbol,
        "asset_type": asset_type,
        "region": "KR",
        "currency": currency,
        "broker": "kis",
        "broker_product": broker_product,
        "broker_symbol": symbol,
        "exchange_code": "KRX",
        "quantity_step": 1,
        "price_tick": 1,
        "min_order_quantity": 1,
        "min_order_notional": 1 if asset_type != "cash" else 0,
    }


def _save_account_snapshot(store, account_id, *, cash, positions):
    store.save_broker_account_snapshot(
        f"snapshot_{account_id}",
        account_id,
        {
            "account_id": account_id,
            "broker_account_id": account_id,
            "account": {
                "account_id": account_id,
                "cash": cash,
                "cash_by_currency": {"KRW": cash},
                "positions": positions,
                "source": "test",
                "fetched_at": "2026-06-03T00:00:00+00:00",
            },
            "current_prices": {"MOCK_ETF_A": 100.0, "MOCK_ETF_B": 50.0},
        },
    )

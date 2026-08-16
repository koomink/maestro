"""Shared wiring for the multi-account "tranquillo" contribution scenario.

This lived inline in tests/test_multi_account_contributions.py until the
workflow-head tests needed a signal run that actually emits a contribution
funding request. Re-deriving the config in a second file would let the two
drift apart, and a scenario this fiddly (two sleeves, a min/max budget range,
a cash shortfall on exactly one of them) is not worth getting subtly
different in two places.
"""

from pathlib import Path

import yaml

from maestro.config.loader import load_config
from maestro.sdk import (
    BaseStrategyPlugin,
    DataBundle,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    TargetAllocationResult,
)


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

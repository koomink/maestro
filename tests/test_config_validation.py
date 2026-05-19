from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from maestro.config.identity import config_identity
from maestro.config.loader import load_config
from maestro.config.models import StrategyPluginConfig
from maestro.core.enums import BrokerProduct, ProfileStage
from maestro.datahub.base import build_data_provider
from maestro.datahub.router import DataHubRouter
from maestro.sdk import DataRequest

LEGACY_EXECUTION_CONFIG_KEYS = {
    "require_market_session",
    "market_session_timezone",
    "market_session_open",
    "market_session_close",
    "market_session_weekdays",
    "market_session_holidays",
    "require_broker_quote_validation",
    "max_broker_quote_deviation_pct",
    "require_broker_risk_validation",
    "max_live_order_notional",
    "max_daily_live_notional",
    "max_daily_live_order_count",
    "daily_loss_limit",
    "live_order_fee_buffer_pct",
    "live_order_enabled",
    "live_order_dry_run",
    "heartbeat_max_age_seconds",
    "scheduled_run_max_age_seconds",
    "engine",
}


def test_invalid_mode_fails(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["mode"] = "live_auto"
    config_path = tmp_path / "invalid_mode.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_enabled_strategy_requires_entrypoint_format(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["strategies"][0]["entrypoint"] = "not-a-module-path"
    config_path = tmp_path / "invalid_entrypoint.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="entrypoint"):
        load_config(config_path)


def test_unknown_execution_field_fails(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["allow_market_orders"] = False
    config_path = tmp_path / "unknown_execution.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="allow_market_orders"):
        load_config(config_path)


def test_live_order_config_defaults_are_safe():
    config = load_config("configs/paper.yaml")

    assert config.execution.order_posture == "disabled"
    assert config.execution.live_order_enabled is False
    assert config.execution.live_order_dry_run is False
    assert config.execution.require_reconciliation_pass is True
    assert config.execution.live_order_limits.max_order_notional == 0.0
    assert config.execution.live_order_limits.max_daily_notional == 0.0
    assert config.execution.live_order_limits.max_daily_order_count == 0
    assert config.execution.live_order_limits.daily_loss_limit is None
    assert config.execution.allowed_order_type == "limit"
    assert config.execution.order_status_poll_interval_seconds == 30.0
    assert config.execution.order_status_max_polls == 20
    assert config.execution.order_status_terminal_timeout_seconds == 1800.0
    assert config.execution.market_session.required is False
    assert config.execution.market_session.timezone == "America/New_York"
    assert config.execution.market_session.open == "09:30"
    assert config.execution.market_session.close == "16:00"
    assert config.execution.market_session.weekdays == [0, 1, 2, 3, 4]
    assert config.execution.market_session.holidays == []
    assert config.execution.broker_validation.require_quote_validation is False
    assert config.execution.broker_validation.max_quote_deviation_pct == 0.05
    assert config.execution.broker_validation.require_risk_validation is False
    assert config.execution.live_order_limits.fee_buffer_pct == 0.0
    assert config.monitoring.heartbeat_max_age_seconds == 0
    assert config.monitoring.scheduled_run_max_age_seconds == 0
    assert config.universe.policy.max_new_symbols_per_run == 1
    assert [item.value for item in config.universe.policy.allowed_regions] == ["US"]
    assert [item.value for item in config.universe.policy.allowed_currencies] == ["USD"]
    assert [item.value for item in config.universe.policy.allowed_broker_products] == [
        "kis_overseas_stock"
    ]
    assert [item.value for item in config.universe.policy.allowed_exchange_codes] == [
        "NASD",
        "NYSE",
        "AMEX",
    ]


def test_live_order_lifecycle_config_validates_positive_max_polls(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["order_status_max_polls"] = 0
    config_path = tmp_path / "invalid_max_polls.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="order_status_max_polls"):
        load_config(config_path)


def test_live_order_config_rejects_invalid_market_session_time(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["market_session"] = {"open": "25:00"}
    config_path = tmp_path / "invalid_market_time.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="market_session"):
        load_config(config_path)


def test_execution_nested_blocks_load_canonical_schema(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["market_session"] = {
        "required": True,
        "timezone": "Asia/Seoul",
        "open": "09:00",
        "close": "15:30",
        "weekdays": [0, 1, 2, 3, 4],
        "holidays": ["2026-05-05"],
    }
    raw["execution"]["broker_validation"] = {
        "require_quote_validation": True,
        "max_quote_deviation_pct": 0.02,
        "require_risk_validation": True,
    }
    raw["execution"]["live_order_limits"] = {
        "max_order_notional": 100,
        "max_daily_notional": 300,
        "max_daily_order_count": 3,
        "daily_loss_limit": 50,
        "fee_buffer_pct": 0.01,
    }
    config_path = tmp_path / "nested_execution.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.execution.market_session.required is True
    assert config.execution.market_session.timezone == "Asia/Seoul"
    assert config.execution.market_session.open == "09:00"
    assert config.execution.market_session.close == "15:30"
    assert config.execution.market_session.weekdays == [0, 1, 2, 3, 4]
    assert config.execution.market_session.holidays == ["2026-05-05"]
    assert config.execution.broker_validation.require_quote_validation is True
    assert config.execution.broker_validation.max_quote_deviation_pct == 0.02
    assert config.execution.broker_validation.require_risk_validation is True
    assert config.execution.live_order_limits.max_order_notional == 100
    assert config.execution.live_order_limits.max_daily_notional == 300
    assert config.execution.live_order_limits.max_daily_order_count == 3
    assert config.execution.live_order_limits.daily_loss_limit == 50
    assert config.execution.live_order_limits.fee_buffer_pct == 0.01


def test_execution_proposal_engine_alias_loads_canonical_schema(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["engine"] = raw["execution"].pop("proposal_engine")
    config_path = tmp_path / "proposal_engine.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.execution.proposal_engine == "paper"
    assert config.execution.engine == "paper"


def test_execution_rejects_conflicting_engine_alias(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["proposal_engine"] = "paper"
    raw["execution"]["engine"] = "other"
    config_path = tmp_path / "conflicting_engine_alias.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="proposal_engine"):
        load_config(config_path)


def test_execution_order_posture_derives_live_order_flags(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    raw["execution"]["order_posture"] = "armed"
    config_path = tmp_path / "armed.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.execution.order_posture == "armed"
    assert config.execution.live_order_enabled is True
    assert config.execution.live_order_dry_run is False


def test_execution_legacy_live_order_flags_derive_order_posture(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    del raw["execution"]["order_posture"]
    raw["execution"]["live_order_enabled"] = False
    raw["execution"]["live_order_dry_run"] = True
    config_path = tmp_path / "legacy_dry_run.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.execution.order_posture == "dry_run"
    assert config.execution.live_order_enabled is False
    assert config.execution.live_order_dry_run is True


def test_execution_order_posture_rejects_conflicting_legacy_flags(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    raw["execution"]["order_posture"] = "armed"
    raw["execution"]["live_order_dry_run"] = True
    config_path = tmp_path / "conflicting_order_posture.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="order_posture conflicts"):
        load_config(config_path)


def test_execution_legacy_flat_blocks_migrate_for_one_release(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["require_market_session"] = True
    raw["execution"]["market_session_timezone"] = "Asia/Seoul"
    raw["execution"]["market_session_open"] = "09:00"
    raw["execution"]["market_session_close"] = "15:30"
    raw["execution"]["market_session_weekdays"] = [0, 1, 2, 3, 4]
    raw["execution"]["market_session_holidays"] = ["2026-05-05"]
    raw["execution"]["require_broker_quote_validation"] = True
    raw["execution"]["max_broker_quote_deviation_pct"] = 0.02
    raw["execution"]["require_broker_risk_validation"] = True
    raw["execution"]["max_live_order_notional"] = 100
    raw["execution"]["max_daily_live_notional"] = 300
    raw["execution"]["max_daily_live_order_count"] = 3
    raw["execution"]["daily_loss_limit"] = 50
    raw["execution"]["live_order_fee_buffer_pct"] = 0.01
    config_path = tmp_path / "legacy_execution.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.execution.market_session.required is True
    assert config.execution.market_session.timezone == "Asia/Seoul"
    assert config.execution.market_session.holidays == ["2026-05-05"]
    assert config.execution.broker_validation.require_quote_validation is True
    assert config.execution.broker_validation.max_quote_deviation_pct == 0.02
    assert config.execution.broker_validation.require_risk_validation is True
    assert config.execution.live_order_limits.max_order_notional == 100
    assert config.execution.live_order_limits.max_daily_notional == 300
    assert config.execution.live_order_limits.max_daily_order_count == 3
    assert config.execution.live_order_limits.daily_loss_limit == 50
    assert config.execution.live_order_limits.fee_buffer_pct == 0.01


def test_execution_rejects_mixed_legacy_and_nested_blocks(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["market_session"] = {"required": True}
    raw["execution"]["require_market_session"] = True
    config_path = tmp_path / "mixed_market_session.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="execution.market_session cannot be mixed"):
        load_config(config_path)

    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["broker_validation"] = {"require_quote_validation": True}
    raw["execution"]["require_broker_quote_validation"] = True
    config_path = tmp_path / "mixed_broker_validation.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="execution.broker_validation cannot be mixed"):
        load_config(config_path)

    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["live_order_limits"] = {"max_order_notional": 100}
    raw["execution"]["max_live_order_notional"] = 100
    config_path = tmp_path / "mixed_live_order_limits.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="execution.live_order_limits cannot be mixed"):
        load_config(config_path)


def test_monitoring_loads_top_level_schema(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["monitoring"] = {
        "heartbeat_max_age_seconds": 60,
        "scheduled_run_max_age_seconds": 120,
    }
    config_path = tmp_path / "monitoring.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.monitoring.heartbeat_max_age_seconds == 60
    assert config.monitoring.scheduled_run_max_age_seconds == 120


def test_execution_legacy_monitoring_fields_migrate_for_one_release(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["heartbeat_max_age_seconds"] = 60
    raw["execution"]["scheduled_run_max_age_seconds"] = 120
    config_path = tmp_path / "legacy_monitoring.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.monitoring.heartbeat_max_age_seconds == 60
    assert config.monitoring.scheduled_run_max_age_seconds == 120


def test_monitoring_rejects_mixed_legacy_execution_fields(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["monitoring"] = {"heartbeat_max_age_seconds": 60}
    raw["execution"]["heartbeat_max_age_seconds"] = 60
    config_path = tmp_path / "mixed_monitoring.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="monitoring cannot be mixed"):
        load_config(config_path)


def test_live_order_config_rejects_market_order_type(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["allowed_order_type"] = "market"
    config_path = tmp_path / "market_live_order.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="allowed_order_type"):
        load_config(config_path)


def test_unknown_risk_field_fails(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["risk"]["allow_short"] = False
    config_path = tmp_path / "unknown_risk.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="allow_short"):
        load_config(config_path)


def test_unknown_top_level_field_fails(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["unexpected"] = True
    config_path = tmp_path / "unknown_top_level.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="unexpected"):
        load_config(config_path)


def test_live_readonly_rejects_enabled_strategies(tmp_path):
    raw = yaml.safe_load(Path("configs/live_readonly.yaml").read_text())
    raw["strategies"] = [
        {
            "id": "sample_static_allocation",
            "enabled": True,
            "weight": 1.0,
            "entrypoint": "sample_static_allocation.strategy:SampleStaticAllocationStrategy",
        }
    ]
    config_path = tmp_path / "live_readonly_with_strategy.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="live_readonly mode does not run strategies"):
        load_config(config_path)


def test_live_readonly_rejects_approval_and_live_order_settings(tmp_path):
    raw = yaml.safe_load(Path("configs/live_readonly.yaml").read_text())
    raw["approval"]["enabled"] = True
    raw["approval"]["require_approval"] = True
    config_path = tmp_path / "live_readonly_with_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="live_readonly mode must not require approval"):
        load_config(config_path)

    raw = yaml.safe_load(Path("configs/live_readonly.yaml").read_text())
    raw["execution"]["order_posture"] = "dry_run"
    config_path = tmp_path / "live_readonly_with_live_orders.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="live_readonly mode requires"):
        load_config(config_path)


def test_paper_rejects_live_order_execution(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["order_posture"] = "armed"
    config_path = tmp_path / "paper_with_live_orders.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="paper mode must not arm live order"):
        load_config(config_path)


def test_paper_requires_initial_cash(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    del raw["portfolio"]["initial_cash"]
    config_path = tmp_path / "paper_without_initial_cash.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="paper mode requires portfolio.initial_cash"):
        load_config(config_path)


def test_live_modes_reject_initial_cash(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    raw["portfolio"]["initial_cash"] = 1_000_000
    config_path = tmp_path / "live_approval_with_initial_cash.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="uses broker snapshot cash"):
        load_config(config_path)

    raw = yaml.safe_load(Path("configs/live_readonly.yaml").read_text())
    raw["portfolio"]["initial_cash"] = 1_000_000
    config_path = tmp_path / "live_readonly_with_initial_cash.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="uses broker snapshot cash"):
        load_config(config_path)


def test_profile_stage_derives_from_existing_profiles(tmp_path):
    assert load_config("configs/paper.yaml").profile_stage == ProfileStage.PAPER
    assert (
        load_config("configs/examples/paper_yahoo_us_etf.yaml").profile_stage
        == ProfileStage.PAPER_REAL_DATA
    )
    assert load_config("configs/live_readonly.yaml").profile_stage == ProfileStage.LIVE_READONLY
    assert (
        load_config("configs/live_approval.yaml").profile_stage
        == ProfileStage.LIVE_APPROVAL_DRY_RUN
    )
    assert (
        load_config("configs/examples/live_approval_ataraxia_kis_paper_trading.yaml").profile_stage
        == ProfileStage.KIS_PAPER_TRADING
    )

    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    raw["execution"]["order_posture"] = "armed"
    raw["datahub"] = {"provider": "yahoo"}
    raw["kis"]["paper_trading"] = False
    config_path = tmp_path / "production_armed.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    assert load_config(config_path).profile_stage == ProfileStage.PRODUCTION_ARMED


def test_profile_stage_rejects_conflict(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    raw["profile_stage"] = "paper"
    config_path = tmp_path / "conflicting_profile_stage.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="profile_stage"):
        load_config(config_path)


def test_config_identity_splits_state_and_runtime_fingerprints(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    first_path = tmp_path / "first.yaml"
    first_path.write_text(yaml.safe_dump(raw))

    changed_runtime = yaml.safe_load(first_path.read_text())
    changed_runtime["monitoring"] = {"heartbeat_max_age_seconds": 60}
    runtime_path = tmp_path / "runtime_changed.yaml"
    runtime_path.write_text(yaml.safe_dump(changed_runtime))

    changed_state = yaml.safe_load(first_path.read_text())
    changed_state["datahub"] = {"provider": "yahoo"}
    state_path = tmp_path / "state_changed.yaml"
    state_path.write_text(yaml.safe_dump(changed_state))

    first_identity = config_identity(first_path)
    runtime_identity = config_identity(runtime_path)
    state_identity = config_identity(state_path)

    assert runtime_identity.fingerprint != first_identity.fingerprint
    assert runtime_identity.runtime_fingerprint != first_identity.runtime_fingerprint
    assert runtime_identity.state_fingerprint == first_identity.state_fingerprint
    assert state_identity.state_fingerprint != first_identity.state_fingerprint


def test_signal_to_allocation_type_is_restricted():
    with pytest.raises(ValidationError, match="single_symbol_action_map"):
        StrategyPluginConfig(
            id="signal_strategy",
            weight=1.0,
            entrypoint="sample_static_allocation.strategy:SampleStaticAllocationStrategy",
            signal_to_allocation={
                "type": "rating_map",
                "action_target_weights": {"buy": 0.3, "hold": 0.0, "sell": 0.0},
            },
        )


def test_signal_to_allocation_requires_all_action_weights():
    with pytest.raises(ValidationError, match="sell"):
        StrategyPluginConfig(
            id="signal_strategy",
            weight=1.0,
            entrypoint="sample_static_allocation.strategy:SampleStaticAllocationStrategy",
            signal_to_allocation={
                "type": "single_symbol_action_map",
                "action_target_weights": {"buy": 0.3, "hold": 0.0},
            },
        )


def test_signal_to_allocation_weight_bounds_are_validated():
    with pytest.raises(ValidationError, match="less than or equal to 1"):
        StrategyPluginConfig(
            id="signal_strategy",
            weight=1.0,
            entrypoint="sample_static_allocation.strategy:SampleStaticAllocationStrategy",
            signal_to_allocation={
                "type": "single_symbol_action_map",
                "action_target_weights": {"buy": 1.1, "hold": 0.0, "sell": 0.0},
            },
        )
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        StrategyPluginConfig(
            id="signal_strategy",
            weight=1.0,
            entrypoint="sample_static_allocation.strategy:SampleStaticAllocationStrategy",
            signal_to_allocation={
                "type": "single_symbol_action_map",
                "action_target_weights": {"buy": 0.3, "hold": -0.1, "sell": 0.0},
            },
        )


def test_signal_to_allocation_unknown_field_fails():
    with pytest.raises(ValidationError, match="unexpected"):
        StrategyPluginConfig(
            id="signal_strategy",
            weight=1.0,
            entrypoint="sample_static_allocation.strategy:SampleStaticAllocationStrategy",
            signal_to_allocation={
                "type": "single_symbol_action_map",
                "cash_symbol": "CASH",
                "unexpected": True,
                "action_target_weights": {"buy": 0.3, "hold": 0.0, "sell": 0.0},
            },
        )


def test_current_sample_configs_load():
    paths = [
        "configs/paper.yaml",
        "configs/live_readonly.yaml",
        "configs/live_approval.yaml",
        *(str(path) for path in sorted(Path("configs/examples").glob("*.yaml"))),
    ]
    assert len(paths) == 14
    for path in paths:
        assert load_config(path)


def test_example_configs_use_canonical_execution_schema():
    paths = sorted(Path("configs/examples").glob("*.yaml"))
    assert len(paths) == 11
    for path in paths:
        raw = yaml.safe_load(path.read_text())
        assert load_config(path)
        execution = raw.get("execution", {})
        legacy_keys = sorted(LEGACY_EXECUTION_CONFIG_KEYS & set(execution))
        assert legacy_keys == [], f"{path} still uses legacy execution keys: {legacy_keys}"


def test_ataraxia_contribution_config_declares_budget_range_and_domestic_universe():
    raw = yaml.safe_load(Path("configs/examples/paper_ataraxia_yahoo.yaml").read_text())
    config = load_config("configs/examples/paper_ataraxia_yahoo.yaml")

    assert "symbol_map" not in raw["datahub"]
    assert config.execution.order_generation_mode == "buy_only_contribution"
    assert config.execution.contribution.enabled is True
    assert config.execution.contribution.monthly_budget == 3_000_000
    assert config.execution.contribution.min_monthly_budget == 2_000_000
    assert config.execution.contribution.max_monthly_budget == 4_000_000
    assert config.execution.contribution.buy_day == 15
    assert config.execution.contribution.non_trading_day_policy == "next_trading_day"
    assert config.execution.contribution.target_policy == "buy_only_toward_target"
    assert config.datahub.symbol_map["TIGER_NASDAQ100_LEVERAGE"] == "418660.KS"
    assert config.datahub.symbol_map["KODEX_US_DIVIDEND_DOWJONES"] == "489250.KS"
    assert config.universe.get("TIGER_NASDAQ100_LEVERAGE").broker_symbol == "418660"
    assert config.universe.get("KODEX_US_DIVIDEND_DOWJONES").broker_symbol == "489250"
    assert config.universe.get("TIGER_NASDAQ100_LEVERAGE").broker_product == (
        BrokerProduct.KIS_DOMESTIC_STOCK
    )


def test_ataraxia_live_approval_example_uses_safe_domestic_kis_defaults():
    config = load_config("configs/examples/live_approval_ataraxia_kis_paper_trading.yaml")

    assert config.mode == "live_approval"
    assert config.portfolio.base_currency == "KRW"
    assert config.strategies[0].enabled is True
    assert config.execution.order_generation_mode == "buy_only_contribution"
    assert config.execution.order_posture == "dry_run"
    assert config.execution.live_order_enabled is False
    assert config.execution.live_order_dry_run is True
    assert config.execution.market_session.required is True
    assert config.execution.require_reconciliation_pass is True
    assert config.execution.broker_validation.require_quote_validation is True
    assert config.execution.broker_validation.require_risk_validation is True
    assert config.execution.live_order_limits.daily_loss_limit == 100_000
    assert config.monitoring.heartbeat_max_age_seconds == 3600
    assert config.monitoring.scheduled_run_max_age_seconds == 86400
    assert config.approval.provider == "telegram"
    assert config.approval.require_approval is True
    assert config.kis.broker_product == BrokerProduct.KIS_DOMESTIC_STOCK
    assert [item.value for item in config.kis.effective_broker_products()] == ["kis_domestic_stock"]
    assert config.kis.paper_trading is True
    assert config.kis.account_id is None
    assert config.kis.account_id_env == "KIS_ACCOUNT_ID"
    assert config.universe.get("TIGER_NASDAQ100_LEVERAGE").broker_symbol == "418660"
    assert config.universe.get("KODEX_US_DIVIDEND_DOWJONES").broker_symbol == "489250"


def test_ataraxia_live_approval_submit_pilot_keeps_kis_paper_trading(tmp_path):
    raw = yaml.safe_load(
        Path("configs/examples/live_approval_ataraxia_kis_paper_trading.yaml").read_text()
    )
    raw["execution"]["order_posture"] = "armed"
    raw["approval"]["telegram_allowed_chat_ids"] = [100]
    raw["approval"]["whitelisted_user_ids"] = [100]
    config_path = tmp_path / "ataraxia_kis_paper_submit.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.mode == "live_approval"
    assert config.execution.live_order_enabled is True
    assert config.execution.live_order_dry_run is False
    assert config.execution.allowed_order_type == "limit"
    assert config.execution.require_reconciliation_pass is True
    assert config.execution.market_session.required is True
    assert config.execution.broker_validation.require_quote_validation is True
    assert config.execution.broker_validation.require_risk_validation is True
    assert config.approval.provider == "telegram"
    assert config.approval.require_approval is True
    assert config.kis.provider == "kis"
    assert config.kis.broker_product == BrokerProduct.KIS_DOMESTIC_STOCK
    assert config.kis.paper_trading is True
    assert [item.value for item in config.kis.effective_broker_products()] == ["kis_domestic_stock"]


def test_contribution_config_rejects_invalid_buy_day(tmp_path):
    raw = yaml.safe_load(Path("configs/examples/paper_ataraxia_yahoo.yaml").read_text())
    raw["execution"]["contribution"]["buy_day"] = 32
    config_path = tmp_path / "invalid_buy_day.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="buy_day"):
        load_config(config_path)


def test_contribution_config_rejects_budget_outside_range(tmp_path):
    raw = yaml.safe_load(Path("configs/examples/paper_ataraxia_yahoo.yaml").read_text())
    raw["execution"]["contribution"]["monthly_budget"] = 5_000_000
    config_path = tmp_path / "invalid_monthly_budget.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="monthly_budget"):
        load_config(config_path)


def test_contribution_config_rejects_unsupported_policy(tmp_path):
    raw = yaml.safe_load(Path("configs/examples/paper_ataraxia_yahoo.yaml").read_text())
    raw["execution"]["contribution"]["non_trading_day_policy"] = "skip"
    config_path = tmp_path / "invalid_policy.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="non_trading_day_policy"):
        load_config(config_path)


def test_multi_asset_readonly_example_uses_env_var_names_only():
    raw_text = Path("configs/examples/live_readonly_multi_asset_kis.yaml").read_text()
    config = load_config("configs/examples/live_readonly_multi_asset_kis.yaml")

    assert config.mode == "live_readonly"
    assert config.portfolio.base_currency == "KRW"
    assert config.portfolio.allocation_mode == "currency_sleeves"
    assert config.portfolio.cash_by_currency == {"KRW": 1_000_000.0, "USD": 10_000.0}
    assert config.kis.enabled is True
    assert config.kis.provider == "kis"
    assert config.kis.effective_broker_products() == [
        BrokerProduct.KIS_DOMESTIC_STOCK,
        BrokerProduct.KIS_OVERSEAS_STOCK,
    ]
    assert config.kis.account_id is None
    assert config.kis.account_id_env == "KIS_ACCOUNT_ID"
    assert config.kis.app_key_env == "KIS_APP_KEY"
    assert config.kis.app_secret_env == "KIS_APP_SECRET"
    assert config.kis.access_token_env == "KIS_ACCESS_TOKEN"
    assert config.kis.approval_key_env == "KIS_APPROVAL_KEY"
    assert config.state.sqlite_path == "var/multi_asset_readonly_state.db"
    assert config.audit.jsonl_path == "var/multi_asset_readonly_audit.jsonl"
    assert config.universe.get("SAMSUNG").broker_product == BrokerProduct.KIS_DOMESTIC_STOCK
    assert config.universe.get("KODEX200").broker_product == BrokerProduct.KIS_DOMESTIC_STOCK
    assert config.universe.get("AAPL").exchange_code == "NASD"
    assert config.universe.get("VOO").exchange_code == "AMEX"
    assert "12345678" not in raw_text
    assert "app-key" not in raw_text
    assert "app-secret" not in raw_text
    assert "access-token" not in raw_text


def test_live_approval_root_config_is_minimal_operator_skeleton():
    raw = yaml.safe_load(Path("configs/live_approval.yaml").read_text())
    config = load_config("configs/live_approval.yaml")

    assert "reconciliation" not in raw
    assert "account_id_env" not in raw["kis"]
    assert "token_cache_path" not in raw["kis"]
    assert "instruments" not in raw["universe"]
    assert config.mode == "live_approval"
    assert config.portfolio.base_currency == "KRW"
    assert config.portfolio.initial_cash is None
    assert config.portfolio.allowed_symbols == ["CASH_KRW"]
    assert config.execution.live_order_enabled is False
    assert config.execution.live_order_dry_run is True
    assert config.execution.require_reconciliation_pass is True
    assert config.execution.allowed_order_type == "limit"
    assert config.execution.live_order_limits.max_order_notional == 100.0
    assert config.execution.live_order_limits.max_daily_notional == 300.0
    assert config.execution.live_order_limits.max_daily_order_count == 3
    assert config.execution.live_order_limits.daily_loss_limit is None
    assert config.approval.enabled is True
    assert config.approval.provider == "telegram"
    assert config.approval.require_approval is True
    assert config.approval.default_decision == "expired"
    assert config.approval.telegram_allowed_chat_ids == []
    assert config.approval.whitelisted_user_ids == []
    assert config.kis.provider == "kis"
    assert config.kis.broker_product == "kis_domestic_stock"
    assert config.kis.app_key_env == "KIS_APP_KEY"
    assert config.kis.app_secret_env == "KIS_APP_SECRET"
    assert config.kis.access_token_env == "KIS_ACCESS_TOKEN"
    assert config.kis.token_cache_path == "var/kis_access_token.json"
    cash = config.universe.get("CASH_KRW")
    assert cash is not None
    assert cash.broker_product == BrokerProduct.KIS_DOMESTIC_STOCK
    assert cash.exchange_code == "KRX"


def test_live_readonly_root_config_uses_broker_cash_baseline():
    raw = yaml.safe_load(Path("configs/live_readonly.yaml").read_text())
    config = load_config("configs/live_readonly.yaml")

    assert "universe" not in raw
    assert "reconciliation" not in raw
    assert "account_id_env" not in raw["kis"]
    assert config.mode == "live_readonly"
    assert config.portfolio.initial_cash is None
    assert config.portfolio.allowed_symbols == ["CASH_KRW"]
    assert config.universe.get("CASH_KRW") is not None
    assert config.strategies == []
    assert config.approval.enabled is False
    assert config.execution.live_order_enabled is False
    assert config.kis.enabled is True


def test_live_approval_us_etf_example_keeps_concrete_universe_out_of_root_config():
    config = load_config("configs/examples/live_approval_us_etf.yaml")

    assert config.mode == "live_approval"
    assert config.portfolio.base_currency == "USD"
    assert config.portfolio.initial_cash is None
    assert config.portfolio.allowed_symbols == ["CASH_USD", "AAPL", "MSFT", "VOO", "QQQ"]
    assert config.kis.broker_product == "kis_overseas_stock"
    assert config.universe.get("AAPL").broker_product == BrokerProduct.KIS_OVERSEAS_STOCK
    assert config.universe.get("AAPL").exchange_code == "NASD"
    assert config.universe.get("VOO").asset_type == "etf"


def test_kis_multi_asset_live_approval_uses_yahoo_multi_provider_without_mock_fallback():
    raw = yaml.safe_load(Path("configs/examples/live_approval_kis_multi_asset.yaml").read_text())
    config = load_config("configs/examples/live_approval_kis_multi_asset.yaml")

    assert "allowed_symbols" not in raw["portfolio"]
    assert all(instrument["asset_type"] != "cash" for instrument in raw["universe"]["instruments"])
    assert "reconciliation" not in raw
    assert "token_cache_path" not in raw["kis"]
    assert config.portfolio.allowed_symbols == [
        "CASH_KRW",
        "SAMSUNG",
        "KODEX200",
        "CASH_USD",
        "AAPL",
        "VOO",
    ]
    assert config.datahub.provider == "mock"
    assert config.datahub.symbol_map == {}
    assert len(config.datahub.providers) == 1
    provider = config.datahub.providers[0]
    assert provider.name == "yahoo_market"
    assert provider.provider == "yahoo"
    assert provider.priority == 10
    assert provider.data_types == ["price", "ohlcv", "technical_indicators"]
    assert provider.timeout_seconds == 5
    assert provider.stale_after_seconds == 604800
    assert "symbol_map" not in raw["datahub"]["providers"][0]
    assert provider.symbol_map == {
        "SAMSUNG": "005930.KS",
        "KODEX200": "069500.KS",
        "AAPL": "AAPL",
        "VOO": "VOO",
    }
    assert all(item.provider != "mock" for item in config.datahub.providers)
    assert all(item.provider != "csv" for item in config.datahub.providers)


def test_allowed_symbols_can_be_derived_from_universe_when_omitted(tmp_path):
    raw = yaml.safe_load(Path("configs/examples/live_approval_us_etf.yaml").read_text())
    del raw["portfolio"]["allowed_symbols"]
    config_path = tmp_path / "live_approval_us_etf_without_allowed_symbols.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.portfolio.allowed_symbols == [
        instrument.symbol for instrument in config.universe.instruments
    ]


def test_yahoo_symbol_map_explicit_values_override_universe_derivation(tmp_path):
    raw = yaml.safe_load(Path("configs/examples/paper_yahoo_us_etf.yaml").read_text())
    raw["datahub"]["symbol_map"] = {"AAPL": "AAPL-CUSTOM"}
    config_path = tmp_path / "paper_yahoo_us_etf_custom_symbol_map.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.datahub.symbol_map["AAPL"] == "AAPL-CUSTOM"
    assert config.datahub.symbol_map["VOO"] == "VOO"


def test_research_multi_provider_example_registers_research_data_types():
    config = load_config("configs/examples/paper_research_multi_provider.yaml")
    provider_names = [provider.name for provider in config.datahub.providers]

    assert provider_names == [
        "yahoo_market",
        "fred_macro",
        "gdelt_news",
        "rss_news",
        "rule_sentiment",
        "newsapi_research",
    ]
    assert config.datahub.providers[-1].enabled is False
    assert isinstance(build_data_provider(config.datahub), DataHubRouter)

    router = build_data_provider(config.datahub)
    assert router.registry.registrations_for(
        DataRequest(symbol="AAPL", asset_type="stock", data_type="price")
    )
    assert router.registry.registrations_for(
        DataRequest(symbol="AAPL", asset_type="stock", data_type="fundamental")
    )
    assert router.registry.registrations_for(
        DataRequest(symbol="FED_FUNDS", asset_type="cash", data_type="macro")
    )
    assert router.registry.registrations_for(
        DataRequest(symbol="MARKET", asset_type="cash", data_type="news")
    )
    assert router.registry.registrations_for(
        DataRequest(symbol="AAPL", asset_type="stock", data_type="sentiment")
    )


def test_universe_requires_portfolio_symbols_to_be_declared(tmp_path):
    raw = yaml.safe_load(Path("configs/examples/live_approval_us_etf.yaml").read_text())
    raw["portfolio"]["allowed_symbols"].append("TSLA")
    config_path = tmp_path / "missing_universe_symbol.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="TSLA"):
        load_config(config_path)


def test_us_etf_yahoo_paper_config_declares_usd_universe_and_symbol_map():
    raw = yaml.safe_load(Path("configs/examples/paper_yahoo_us_etf.yaml").read_text())
    config = load_config("configs/examples/paper_yahoo_us_etf.yaml")

    assert "symbol_map" not in raw["datahub"]
    assert config.mode == "paper"
    assert config.portfolio.base_currency == "USD"
    assert config.datahub.provider == "yahoo"
    assert config.execution.engine == "paper"
    assert config.portfolio.allowed_symbols == [
        "CASH_USD",
        "AAPL",
        "MSFT",
        "VOO",
        "QQQ",
        "SGOV",
    ]
    universe_symbols = {instrument.symbol for instrument in config.universe.instruments}
    assert set(config.portfolio.allowed_symbols) == universe_symbols
    for symbol in ["AAPL", "MSFT", "VOO", "QQQ", "SGOV"]:
        assert config.datahub.symbol_map[symbol] == symbol
        assert config.universe.get(symbol).broker_product == BrokerProduct.KIS_OVERSEAS_STOCK
        assert config.universe.get(symbol).currency == "USD"
        assert config.universe.get(symbol).quantity_step == 1
        assert config.universe.get(symbol).price_tick == 0.01
    assert "CASH_USD" not in config.datahub.symbol_map
    assert config.universe.get("AAPL").exchange_code == "NASD"
    assert config.universe.get("MSFT").exchange_code == "NASD"
    assert config.universe.get("QQQ").exchange_code == "NASD"
    assert config.universe.get("VOO").exchange_code == "AMEX"
    assert config.universe.get("SGOV").exchange_code == "AMEX"


def test_live_approval_example_config_has_no_hardcoded_secrets(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("KIS_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_APP_SECRET", raising=False)
    monkeypatch.delenv("KIS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("KIS_APPROVAL_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    raw_text = Path("configs/live_approval.yaml").read_text()
    config = load_config("configs/live_approval.yaml")

    assert config.kis.app_key_env == "KIS_APP_KEY"
    assert config.kis.app_secret_env == "KIS_APP_SECRET"
    assert config.kis.access_token_env == "KIS_ACCESS_TOKEN"
    assert config.kis.approval_key_env == "KIS_APPROVAL_KEY"
    assert config.approval.telegram_bot_token_env == "TELEGRAM_BOT_TOKEN"
    assert "xoxb-" not in raw_text
    assert "Bearer " not in raw_text
    assert "ghp_" not in raw_text
    assert "telegram.org/bot" not in raw_text
    assert "KIS_APP_KEY:" not in raw_text
    assert "KIS_APP_SECRET:" not in raw_text
    assert "KIS_ACCESS_TOKEN:" not in raw_text
    assert "KIS_APPROVAL_KEY:" not in raw_text
    assert "TELEGRAM_BOT_TOKEN:" not in raw_text

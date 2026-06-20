from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from maestro.config.identity import config_identity
from maestro.config.loader import load_config
from maestro.config.models import BrokerAccountConfig, KISConfig, StrategyPluginConfig
from maestro.core.enums import BrokerProduct, Currency, ProfileStage
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

ATARAXIA_LIVE_APPROVAL_CONFIG = Path(
    "tests/fixtures/configs/live_approval_tranquillo_kis_paper_trading.yaml"
)


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


def test_removed_risk_weight_fields_fail(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["risk"] = {"max_single_asset_weight": 0.4, "min_cash_weight": 0.05}
    config_path = tmp_path / "removed_risk_fields.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="max_single_asset_weight"):
        load_config(config_path)


def test_kis_config_rejects_legacy_single_broker_product():
    with pytest.raises(ValidationError, match="broker_product"):
        KISConfig(
            enabled=True,
            provider="kis",
            broker_product="kis_domestic_stock",
            broker_products=["kis_domestic_stock"],
        )


def test_broker_account_config_rejects_legacy_single_broker_product():
    with pytest.raises(ValidationError, match="broker_product"):
        BrokerAccountConfig(
            id="domestic",
            broker="kis",
            broker_product="kis_domestic_stock",
            broker_products=["kis_domestic_stock"],
        )


def test_telegram_approval_ids_fall_back_to_maestro_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_TELEGRAM_ALLOWED_CHAT_IDS", "1001, 1002")
    monkeypatch.setenv("MAESTRO_TELEGRAM_WHITELISTED_USER_IDS", "2001")
    raw = yaml.safe_load(ATARAXIA_LIVE_APPROVAL_CONFIG.read_text())
    raw["approval"]["telegram_allowed_chat_ids"] = []
    raw["approval"]["whitelisted_user_ids"] = []
    config_path = tmp_path / "telegram_env_fallback.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.approval.telegram_allowed_chat_ids == [1001, 1002]
    assert config.approval.whitelisted_user_ids == [2001]


def test_telegram_approval_config_ids_override_maestro_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAESTRO_TELEGRAM_ALLOWED_CHAT_IDS", "1001")
    monkeypatch.setenv("MAESTRO_TELEGRAM_WHITELISTED_USER_IDS", "2001")
    raw = yaml.safe_load(ATARAXIA_LIVE_APPROVAL_CONFIG.read_text())
    raw["approval"]["telegram_allowed_chat_ids"] = [3001]
    raw["approval"]["whitelisted_user_ids"] = [4001]
    config_path = tmp_path / "telegram_config_override.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.approval.telegram_allowed_chat_ids == [3001]
    assert config.approval.whitelisted_user_ids == [4001]


def test_legacy_kis_live_strategy_derives_default_account_id():
    config = load_config(ATARAXIA_LIVE_APPROVAL_CONFIG)

    assert [account.id for account in config.accounts] == ["default_kis"]
    assert config.strategies[0].account_id == "default_kis"


def test_explicit_live_accounts_require_strategy_account_id(tmp_path):
    raw = yaml.safe_load(ATARAXIA_LIVE_APPROVAL_CONFIG.read_text())
    raw["accounts"] = [
        {
            "id": "kis_isa",
            "broker": "kis",
            "environment": "real",
            "enabled": True,
            "broker_products": ["kis_domestic_stock"],
        }
    ]
    config_path = tmp_path / "missing_strategy_account.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="requires strategy.account_id"):
        load_config(config_path)


def test_explicit_live_accounts_reject_unknown_strategy_account_id(tmp_path):
    raw = yaml.safe_load(ATARAXIA_LIVE_APPROVAL_CONFIG.read_text())
    raw["accounts"] = [
        {
            "id": "kis_isa",
            "broker": "kis",
            "environment": "real",
            "enabled": True,
            "broker_products": ["kis_domestic_stock"],
        }
    ]
    raw["strategies"][0]["account_id"] = "missing_account"
    config_path = tmp_path / "unknown_strategy_account.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="unknown or disabled account_id"):
        load_config(config_path)


def test_multiple_accounts_allow_shared_strategy_mapping(tmp_path):
    raw = yaml.safe_load(ATARAXIA_LIVE_APPROVAL_CONFIG.read_text())
    raw["accounts"] = [
        {
            "id": "kis_isa",
            "broker": "kis",
            "environment": "real",
            "enabled": True,
            "broker_products": ["kis_domestic_stock"],
        },
        {
            "id": "toss_brokerage",
            "broker": "toss",
            "environment": "real",
            "enabled": True,
        },
    ]
    raw["strategies"][0]["account_id"] = "kis_isa"
    config_path = tmp_path / "multi_account.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert [account.id for account in config.accounts] == ["kis_isa", "toss_brokerage"]
    assert config.strategies[0].account_id == "kis_isa"


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


def test_live_order_limits_accept_currency_specific_caps(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["live_order_limits"] = {
        "max_order_notional_by_currency": {"KRW": 1_000_000, "USD": 1_000},
        "max_daily_notional_by_currency": {"KRW": 10_000_000, "USD": 10_000},
        "max_daily_order_count": 3,
        "daily_loss_limit_by_currency": {"KRW": 100_000, "USD": 100},
        "fee_buffer_pct": 0.01,
    }
    config_path = tmp_path / "currency_live_order_limits.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    limits = config.execution.live_order_limits
    assert limits.max_order_notional_by_currency == {
        Currency.KRW: 1_000_000.0,
        Currency.USD: 1_000.0,
    }
    assert limits.max_daily_notional_by_currency == {
        Currency.KRW: 10_000_000.0,
        Currency.USD: 10_000.0,
    }
    assert limits.daily_loss_limit_by_currency == {
        Currency.KRW: 100_000.0,
        Currency.USD: 100.0,
    }


def test_live_order_limits_reject_invalid_currency_specific_caps(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["live_order_limits"] = {
        "max_order_notional_by_currency": {"KRW": -1},
        "max_daily_notional_by_currency": {"KRW": 10_000_000},
        "daily_loss_limit_by_currency": {"KRW": 100_000},
    }
    config_path = tmp_path / "negative_currency_live_order_limits.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValueError, match="currency-specific notional limits"):
        load_config(config_path)

    raw["execution"]["live_order_limits"] = {
        "max_order_notional_by_currency": {"KRW": 1_000_000},
        "max_daily_notional_by_currency": {"KRW": 10_000_000},
        "daily_loss_limit_by_currency": {"KRW": 0},
    }
    config_path = tmp_path / "zero_currency_daily_loss_limit.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValueError, match="currency-specific daily loss limits"):
        load_config(config_path)


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
    raw["risk"] = {"allow_short": False}
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


def test_live_readonly_allows_enabled_strategies_for_operator_views(tmp_path):
    raw = yaml.safe_load(Path("configs/live_readonly.yaml").read_text())
    raw["strategies"] = [
        {
            "id": "sample_static_allocation",
            "enabled": True,
            "readonly_enabled": True,
            "signal_enabled": False,
            "weight": 1.0,
            "entrypoint": "sample_static_allocation.strategy:SampleStaticAllocationStrategy",
        }
    ]
    config_path = tmp_path / "live_readonly_with_strategy.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert [(strategy.id, strategy.enabled) for strategy in config.strategies] == [
        ("sample_static_allocation", True)
    ]


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
        load_config("tests/fixtures/configs/paper_yahoo_us_etf.yaml").profile_stage
        == ProfileStage.PAPER_REAL_DATA
    )
    assert load_config("configs/live_readonly.yaml").profile_stage == ProfileStage.LIVE_READONLY
    assert (
        load_config("configs/live_approval.yaml").profile_stage
        == ProfileStage.LIVE_APPROVAL_DRY_RUN
    )
    assert (
        load_config(
            "tests/fixtures/configs/live_approval_tranquillo_kis_paper_trading.yaml"
        ).profile_stage
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


def test_app_fragment_composes_tranquillo_defaults(tmp_path):
    fragment_path = tmp_path / "tranquillo.yaml"
    fragment_path.write_text(yaml.safe_dump(_tranquillo_fragment()), encoding="utf-8")
    raw = yaml.safe_load(ATARAXIA_LIVE_APPROVAL_CONFIG.read_text())
    raw["app_fragment_paths"] = ["tranquillo.yaml"]
    raw["strategies"] = [{"id": "tranquillo", "enabled": True, "weight": 1.0}]
    raw["portfolio"].pop("currency_sleeves", None)
    raw["universe"]["instruments"] = []
    raw["datahub"].pop("symbol_map", None)
    config_path = tmp_path / "composed_tranquillo.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(config_path)

    assert config.app_fragment_paths == ["tranquillo.yaml"]
    assert config.strategies[0].entrypoint == "tranquillo.strategy:TranquilloStrategy"
    assert config.strategies[0].config["allocations"] == {
        "TIGER_NASDAQ100_LEVERAGE": 0.60,
        "KODEX_US_DIVIDEND_DOWJONES": 0.40,
    }
    assert config.portfolio.currency_sleeves["KRW"].symbols == [
        "TIGER_NASDAQ100_LEVERAGE",
        "KODEX_US_DIVIDEND_DOWJONES",
    ]
    assert config.universe.get("TIGER_NASDAQ100_LEVERAGE").broker_symbol == "418660"
    assert config.datahub.symbol_map["TIGER_NASDAQ100_LEVERAGE"] == "418660.KS"
    assert config.app_fragment_recommendations == {
        "execution": {"order_generation_mode": "buy_only_contribution"}
    }


def test_app_fragment_rejects_operator_owned_keys(tmp_path):
    fragment = _tranquillo_fragment()
    fragment["execution"] = {"order_generation_mode": "buy_only_contribution"}
    fragment_path = tmp_path / "bad_fragment.yaml"
    fragment_path.write_text(yaml.safe_dump(fragment), encoding="utf-8")
    raw = yaml.safe_load(ATARAXIA_LIVE_APPROVAL_CONFIG.read_text())
    raw["app_fragment_paths"] = ["bad_fragment.yaml"]
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="operator-owned"):
        load_config(config_path)


def test_app_fragment_rejects_conflicting_duplicate_instrument(tmp_path):
    fragment_path = tmp_path / "tranquillo.yaml"
    fragment_path.write_text(yaml.safe_dump(_tranquillo_fragment()), encoding="utf-8")
    raw = yaml.safe_load(ATARAXIA_LIVE_APPROVAL_CONFIG.read_text())
    raw["app_fragment_paths"] = ["tranquillo.yaml"]
    raw["universe"]["instruments"][0]["broker_symbol"] = "999999"
    config_path = tmp_path / "conflicting_instrument.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="Conflicting app fragment instrument"):
        load_config(config_path)


def test_app_fragment_allows_operator_strategy_config_overrides(tmp_path):
    fragment_path = tmp_path / "tranquillo.yaml"
    fragment_path.write_text(yaml.safe_dump(_tranquillo_fragment()), encoding="utf-8")
    raw = yaml.safe_load(ATARAXIA_LIVE_APPROVAL_CONFIG.read_text())
    raw["app_fragment_paths"] = ["tranquillo.yaml"]
    raw["strategies"] = [
        {
            "id": "tranquillo",
            "enabled": True,
            "weight": 1.0,
            "config": {
                "allocations": {
                    "TIGER_NASDAQ100_LEVERAGE": 0.50,
                    "KODEX_US_DIVIDEND_DOWJONES": 0.50,
                }
            },
        }
    ]
    config_path = tmp_path / "operator_override.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    config = load_config(config_path)

    assert config.strategies[0].entrypoint == "tranquillo.strategy:TranquilloStrategy"
    assert config.strategies[0].config == {
        "sleeve": "KRW",
        "allocations": {
            "TIGER_NASDAQ100_LEVERAGE": 0.50,
            "KODEX_US_DIVIDEND_DOWJONES": 0.50,
        },
    }


def test_app_fragment_identity_changes_when_fragment_changes(tmp_path):
    fragment_path = tmp_path / "tranquillo.yaml"
    fragment = _tranquillo_fragment()
    fragment_path.write_text(yaml.safe_dump(fragment), encoding="utf-8")
    raw = yaml.safe_load(ATARAXIA_LIVE_APPROVAL_CONFIG.read_text())
    raw["app_fragment_paths"] = ["tranquillo.yaml"]
    config_path = tmp_path / "composed_tranquillo.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    before_identity = config_identity(config_path)
    fragment["strategy"]["config"]["allocations"]["TIGER_NASDAQ100_LEVERAGE"] = 0.61
    fragment_path.write_text(yaml.safe_dump(fragment), encoding="utf-8")
    after_identity = config_identity(config_path)

    assert after_identity.fingerprint != before_identity.fingerprint


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


def test_current_runtime_configs_load():
    paths = [
        "configs/paper.yaml",
        "configs/live_readonly.yaml",
        "configs/live_approval.yaml",
        *(
            str(path)
            for path in sorted(Path("configs/operator").glob("*.yaml"))
            if path.name not in {"strategy_accounts.yaml", "broker_accounts.yaml"}
        ),
    ]
    assert len(paths) == 6
    for path in paths:
        assert load_config(path)


def test_operator_symphony_phase_configs_share_state_and_route_strategies():
    readonly = load_config("configs/operator/symphony_readonly.yaml")
    signal = load_config("configs/operator/symphony_signal.yaml")
    approval = load_config("configs/operator/symphony_approval.yaml")
    signal_raw = yaml.safe_load(Path("configs/operator/symphony_signal.yaml").read_text())
    approval_raw = yaml.safe_load(Path("configs/operator/symphony_approval.yaml").read_text())

    assert readonly.mode == "live_readonly"
    assert signal.mode == "live_approval"
    assert approval.mode == "live_approval"
    assert readonly.strategy_account_map_path == "strategy_accounts.yaml"
    assert signal.strategy_account_map_path == "strategy_accounts.yaml"
    assert approval.strategy_account_map_path == "strategy_accounts.yaml"
    assert readonly.broker_accounts_path == "broker_accounts.yaml"
    assert signal.broker_accounts_path == "broker_accounts.yaml"
    assert approval.broker_accounts_path == "broker_accounts.yaml"
    assert [strategy.get("account_id") for strategy in signal_raw["strategies"]] == [
        None,
        None,
        None,
    ]
    assert [strategy.get("account_id") for strategy in approval_raw["strategies"]] == [
        None,
        None,
        None,
    ]
    assert [
        (strategy.id, strategy.enabled, strategy.readonly_enabled)
        for strategy in readonly.strategies
    ] == [
        ("tranquillo", True, True),
        ("crescendo_us", True, True),
        ("fugue", False, True),
    ]
    assert signal.execution.order_posture == "disabled"
    assert approval.execution.order_posture == "armed"
    assert approval.execution.live_order_enabled is True
    assert readonly.execution.order_posture == "disabled"
    assert readonly.approval.enabled is False
    assert signal.approval.enabled is True
    assert approval.approval.enabled is True

    assert {
        readonly.state.sqlite_path,
        signal.state.sqlite_path,
        approval.state.sqlite_path,
    } == {"var/symphony_state.db"}
    assert {
        readonly.state.identity_group,
        signal.state.identity_group,
        approval.state.identity_group,
    } == {"symphony"}
    assert {
        readonly.audit.jsonl_path,
        signal.audit.jsonl_path,
        approval.audit.jsonl_path,
    } == {"var/symphony_audit.jsonl"}

    assert {
        config_identity("configs/operator/symphony_readonly.yaml").state_fingerprint,
        config_identity("configs/operator/symphony_signal.yaml").state_fingerprint,
        config_identity("configs/operator/symphony_approval.yaml").state_fingerprint,
    } == {config_identity("configs/operator/symphony_readonly.yaml").state_fingerprint}

    assert [(account.id, account.broker, account.environment) for account in signal.accounts] == [
        ("kis_mock", "kis", "paper_trading"),
        ("kis_isa", "kis", "real"),
        ("kis_brokerage", "kis", "real"),
        ("kis_ps", "kis", "real"),
        ("dev_sandbox", "sandbox", "paper_trading"),
        ("toss_brokerage", "toss", "real"),
    ]
    assert [
        (account.id, account.account_id_env, account.app_key_env, account.app_secret_env)
        for account in signal.accounts
    ] == [
        ("kis_mock", "KIS_MOCK_ACCOUNT_ID", "KIS_MOCK_APP_KEY", "KIS_MOCK_APP_SECRET"),
        ("kis_isa", "KIS_ISA_ACCOUNT_ID", "KIS_ISA_APP_KEY", "KIS_ISA_APP_SECRET"),
        (
            "kis_brokerage",
            "KIS_BROKERAGE_ACCOUNT_ID",
            "KIS_BROKERAGE_APP_KEY",
            "KIS_BROKERAGE_APP_SECRET",
        ),
        ("kis_ps", "KIS_PS_ACCOUNT_ID", "KIS_PS_APP_KEY", "KIS_PS_APP_SECRET"),
        ("dev_sandbox", None, "KIS_MOCK_APP_KEY", "KIS_MOCK_APP_SECRET"),
        ("toss_brokerage", None, None, None),
    ]
    assert [
        (strategy.id, strategy.account_id, strategy.signal_enabled, strategy.order_posture)
        for strategy in signal.strategies
    ] == [
        ("tranquillo", "multi_account_contributions.tranquillo", True, "dry_run"),
        ("crescendo_us", "toss_brokerage", True, "armed"),
        ("fugue", "dev_sandbox", False, "disabled"),
    ]
    assert signal.account_strategy_targets["toss_brokerage"]["crescendo_us"].target_weight == 0.7
    assert signal.account_strategy_targets["toss_brokerage"]["manual"].target_weight == 0.3
    tranquillo_group = signal.multi_account_contributions["tranquillo"]
    assert tranquillo_group.strategy_id == "tranquillo"
    assert [
        (
            target.account_id,
            target.execution_sleeve,
            target.allowed_symbols,
            target.min_monthly_budget,
            target.max_monthly_budget,
        )
        for target in tranquillo_group.account_targets
    ] == [
        (
            "kis_ps",
            "tranquillo_ps",
            ["KODEX_US_DIVIDEND_DOWJONES"],
            500000,
            500000,
        ),
            (
                "kis_isa",
                "tranquillo_isa",
                ["TIGER_NASDAQ100_LEVERAGE", "KODEX_US_DIVIDEND_DOWJONES"],
                1660000,
                0,
            ),
        ]
    assert [
        (strategy.id, strategy.account_id, strategy.signal_enabled, strategy.order_posture)
        for strategy in approval.strategies
    ] == [
        ("tranquillo", "multi_account_contributions.tranquillo", True, "dry_run"),
        ("crescendo_us", "toss_brokerage", True, "armed"),
        ("fugue", "dev_sandbox", False, "disabled"),
    ]


def test_shared_strategy_account_map_routes_strategies_and_affects_identity(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump(
            {
                "strategies": {
                    "tranquillo": "kis_isa",
                    "crescendo_us": "kis_brokerage",
                    "fugue": {
                        "account_id": "dev_sandbox",
                        "signal": False,
                        "order_posture": "disabled",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)
    before_identity = config_identity(config_path)

    assert [(strategy.id, strategy.account_id) for strategy in config.strategies] == [
        ("tranquillo", "kis_isa"),
        ("crescendo_us", "kis_brokerage"),
        ("fugue", "dev_sandbox"),
    ]

    map_path.write_text(
        yaml.safe_dump(
            {
                "strategies": {
                    "tranquillo": "kis_mock",
                    "crescendo_us": "kis_brokerage",
                    "fugue": {
                        "account_id": "dev_sandbox",
                        "signal": False,
                        "order_posture": "disabled",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    after_identity = config_identity(config_path)

    assert load_config(config_path).strategies[0].account_id == "kis_mock"
    assert after_identity.fingerprint != before_identity.fingerprint
    assert after_identity.runtime_fingerprint != before_identity.runtime_fingerprint
    assert after_identity.state_fingerprint == before_identity.state_fingerprint


def test_shared_strategy_account_map_applies_phase_controls(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump(
            {
                "strategies": {
                    "tranquillo": {
                        "account_id": "kis_isa",
                        "readonly": True,
                        "signal": True,
                        "order_posture": "dry_run",
                    },
                    "crescendo_us": {
                        "account_id": "dev_sandbox",
                        "readonly": True,
                        "signal": True,
                        "order_posture": "dry_run",
                    },
                    "fugue": {
                        "account_id": "dev_sandbox",
                        "readonly": True,
                        "signal": False,
                        "order_posture": "disabled",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert [
        (
            strategy.id,
            strategy.enabled,
            strategy.account_id,
            strategy.readonly_enabled,
            strategy.signal_enabled,
            strategy.order_posture,
        )
        for strategy in config.strategies
    ] == [
        ("tranquillo", True, "kis_isa", True, True, "dry_run"),
        ("crescendo_us", True, "dev_sandbox", True, True, "dry_run"),
        ("fugue", True, "dev_sandbox", True, False, "disabled"),
    ]


def test_shared_strategy_account_map_applies_execution_sleeves(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump(
            {
                "execution_sleeves": {
                    "accounts": {
                        "kis_isa": {
                            "tranquillo_isa": {
                                "currency_sleeve": "KRW",
                                "target_weight": 1.0,
                                "order_generation_mode": "buy_only_contribution",
                                "contribution": {
                                    "enabled": True,
                                    "currency": "KRW",
                                    "sleeve": "KRW",
                                    "monthly_budget": 3_000_000,
                                    "min_monthly_budget": 2_000_000,
                                    "max_monthly_budget": 4_000_000,
                                    "buy_day": 15,
                                },
                            }
                        },
                        "dev_sandbox": {
                            "crescendo_us": {
                                "currency_sleeve": "USD",
                                "target_weight": 1.0,
                                "order_generation_mode": "target_rebalance",
                            }
                        },
                    }
                },
                "strategies": {
                    "tranquillo": {
                        "account_id": "kis_isa",
                        "execution_sleeve": "tranquillo_isa",
                        "readonly": True,
                        "signal": True,
                        "order_posture": "dry_run",
                    },
                    "crescendo_us": {
                        "account_id": "dev_sandbox",
                        "execution_sleeve": "crescendo_us",
                        "readonly": True,
                        "signal": True,
                        "order_posture": "dry_run",
                    },
                    "fugue": {
                        "account_id": "dev_sandbox",
                        "readonly": True,
                        "signal": False,
                        "order_posture": "disabled",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert [
        (strategy.id, strategy.account_id, strategy.execution_sleeve)
        for strategy in config.strategies
    ] == [
        ("tranquillo", "kis_isa", "tranquillo_isa"),
        ("crescendo_us", "dev_sandbox", "crescendo_us"),
        ("fugue", "dev_sandbox", None),
    ]
    assert (
        config.execution_sleeves.accounts["kis_isa"]["tranquillo_isa"].order_generation_mode
        == "buy_only_contribution"
    )
    assert (
        config.effective_strategy_order_generation_mode(config.strategies[0])
        == "buy_only_contribution"
    )
    assert (
        config.effective_strategy_order_generation_mode(config.strategies[1]) == "target_rebalance"
    )


def test_shared_strategy_account_map_applies_account_strategy_targets(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    broker_path = tmp_path / "broker_accounts.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    raw["broker_accounts_path"] = "broker_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    _write_operator_broker_accounts_with_toss_enabled(broker_path)
    map_path.write_text(
        yaml.safe_dump(
            {
                "execution_sleeves": {
                    "accounts": {
                        "toss_brokerage": {
                            "crescendo_us": {
                                "currency_sleeve": "USD",
                                "target_weight": 1.0,
                                "order_generation_mode": "target_rebalance",
                            }
                        }
                    }
                },
                "account_strategy_targets": {
                    "toss_brokerage": {
                        "crescendo_us": {"target_weight": 0.7},
                        "manual": {"target_weight": 0.3},
                    }
                },
                "strategies": {
                    "tranquillo": {
                        "account_id": "kis_isa",
                        "signal": False,
                    },
                    "crescendo_us": {
                        "account_id": "toss_brokerage",
                        "execution_sleeve": "crescendo_us",
                        "signal": True,
                    },
                    "fugue": {
                        "account_id": "dev_sandbox",
                        "signal": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.account_strategy_targets["toss_brokerage"]["crescendo_us"].target_weight == 0.7
    assert config.account_strategy_targets["toss_brokerage"]["manual"].target_weight == 0.3


def test_account_strategy_targets_reject_account_weight_mismatch(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    broker_path = tmp_path / "broker_accounts.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    raw["broker_accounts_path"] = "broker_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    _write_operator_broker_accounts_with_toss_enabled(broker_path)
    map_path.write_text(
        yaml.safe_dump(
            {
                "execution_sleeves": {
                    "accounts": {
                        "toss_brokerage": {
                            "crescendo_us": {
                                "currency_sleeve": "USD",
                                "target_weight": 1.0,
                                "order_generation_mode": "target_rebalance",
                            }
                        }
                    }
                },
                "account_strategy_targets": {
                    "toss_brokerage": {
                        "crescendo_us": {"target_weight": 0.7},
                        "manual": {"target_weight": 0.2},
                    }
                },
                "strategies": {
                    "tranquillo": {
                        "account_id": "kis_isa",
                        "signal": False,
                    },
                    "crescendo_us": {
                        "account_id": "toss_brokerage",
                        "execution_sleeve": "crescendo_us",
                        "signal": True,
                    },
                    "fugue": {
                        "account_id": "dev_sandbox",
                        "signal": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="account_strategy_targets target_weight"):
        load_config(config_path)


def test_execution_sleeves_reject_missing_strategy_sleeve(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump(
            {
                "execution_sleeves": {
                    "accounts": {
                        "kis_isa": {
                            "tranquillo_isa": {
                                "currency_sleeve": "KRW",
                                "target_weight": 1.0,
                                "order_generation_mode": "target_rebalance",
                            }
                        }
                    }
                },
                "strategies": {
                    "tranquillo": {
                        "account_id": "kis_isa",
                        "execution_sleeve": "missing",
                        "signal": True,
                    },
                    "crescendo_us": {
                        "account_id": "dev_sandbox",
                        "signal": False,
                    },
                    "fugue": {
                        "account_id": "dev_sandbox",
                        "signal": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="unknown execution_sleeve"):
        load_config(config_path)


def test_execution_sleeves_reject_account_weight_mismatch(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump(
            {
                "execution_sleeves": {
                    "accounts": {
                        "dev_sandbox": {
                            "tranquillo_book": {
                                "currency_sleeve": "KRW",
                                "target_weight": 0.7,
                                "order_generation_mode": "target_rebalance",
                            },
                            "crescendo_book": {
                                "currency_sleeve": "USD",
                                "target_weight": 0.2,
                                "order_generation_mode": "target_rebalance",
                            },
                        }
                    }
                },
                "strategies": {
                    "tranquillo": {
                        "account_id": "dev_sandbox",
                        "execution_sleeve": "tranquillo_book",
                        "signal": True,
                    },
                    "crescendo_us": {
                        "account_id": "dev_sandbox",
                        "execution_sleeve": "crescendo_book",
                        "signal": True,
                    },
                    "fugue": {
                        "account_id": "dev_sandbox",
                        "signal": False,
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="execution_sleeves target_weight"):
        load_config(config_path)


def test_shared_strategy_account_map_overrides_strategy_enabled(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy["enabled"] = False
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump(
            {
                "strategies": {
                    "tranquillo": {
                        "enabled": True,
                        "account_id": "kis_isa",
                        "readonly": True,
                        "signal": True,
                        "order_posture": "dry_run",
                    },
                    "crescendo_us": {
                        "enabled": True,
                        "account_id": "dev_sandbox",
                        "readonly": True,
                        "signal": True,
                        "order_posture": "dry_run",
                    },
                    "fugue": {
                        "enabled": False,
                        "account_id": "dev_sandbox",
                        "readonly": True,
                        "signal": False,
                        "order_posture": "disabled",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert [(strategy.id, strategy.enabled) for strategy in config.strategies] == [
        ("tranquillo", True),
        ("crescendo_us", True),
        ("fugue", False),
    ]


def test_strategy_phase_controls_reject_sandbox_armed(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump(
            {
                "strategies": {
                    "tranquillo": {"account_id": "kis_isa", "order_posture": "dry_run"},
                    "crescendo_us": {"account_id": "dev_sandbox", "order_posture": "armed"},
                    "fugue": {
                        "account_id": "dev_sandbox",
                        "signal": False,
                        "order_posture": "disabled",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="sandbox.*armed"):
        load_config(config_path)


def test_strategy_phase_controls_reject_mixed_posture_in_same_account(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    for strategy in raw["strategies"]:
        strategy.pop("account_id", None)
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump(
            {
                "strategies": {
                    "tranquillo": {"account_id": "kis_isa", "order_posture": "dry_run"},
                    "crescendo_us": {"account_id": "kis_isa", "order_posture": "armed"},
                    "fugue": {
                        "account_id": "dev_sandbox",
                        "signal": False,
                        "order_posture": "disabled",
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="mixed order_posture"):
        load_config(config_path)


def test_shared_strategy_account_map_rejects_unknown_strategy(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump(
            {
                "strategies": {
                    "tranquillo": "kis_isa",
                    "crescendo_us": "kis_brokerage",
                    "ghost": "kis_mock",
                }
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown strategy ids"):
        load_config(config_path)


def test_shared_strategy_account_map_rejects_inline_mismatch(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["strategy_account_map_path"] = "strategy_accounts.yaml"
    raw["strategies"][0]["account_id"] = "kis_isa"
    config_path = tmp_path / "symphony_signal.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    map_path.write_text(
        yaml.safe_dump({"strategies": {"tranquillo": "kis_mock", "crescendo_us": "kis_brokerage"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="inline account_id"):
        load_config(config_path)


def test_fixture_configs_use_canonical_execution_schema():
    paths = sorted(Path("tests/fixtures/configs").glob("*.yaml"))
    assert len(paths) == 11
    for path in paths:
        raw = yaml.safe_load(path.read_text())
        assert load_config(path)
        execution = raw.get("execution", {})
        legacy_keys = sorted(LEGACY_EXECUTION_CONFIG_KEYS & set(execution))
        assert legacy_keys == [], f"{path} still uses legacy execution keys: {legacy_keys}"


def test_tranquillo_contribution_config_declares_budget_range_and_domestic_universe():
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_tranquillo_yahoo.yaml").read_text())
    config = load_config("tests/fixtures/configs/paper_tranquillo_yahoo.yaml")

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


def test_tranquillo_live_approval_example_uses_safe_domestic_kis_defaults():
    config = load_config("tests/fixtures/configs/live_approval_tranquillo_kis_paper_trading.yaml")

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
    assert [item.value for item in config.kis.effective_broker_products()] == ["kis_domestic_stock"]
    assert config.kis.paper_trading is True
    assert config.kis.account_id is None
    assert config.kis.account_id_env == "KIS_MOCK_ACCOUNT_ID"
    assert config.universe.get("TIGER_NASDAQ100_LEVERAGE").broker_symbol == "418660"
    assert config.universe.get("KODEX_US_DIVIDEND_DOWJONES").broker_symbol == "489250"


def test_tranquillo_live_approval_submit_pilot_keeps_kis_paper_trading(tmp_path):
    raw = yaml.safe_load(
        Path("tests/fixtures/configs/live_approval_tranquillo_kis_paper_trading.yaml").read_text()
    )
    raw["execution"]["order_posture"] = "armed"
    raw["approval"]["telegram_allowed_chat_ids"] = [100]
    raw["approval"]["whitelisted_user_ids"] = [100]
    config_path = tmp_path / "tranquillo_kis_paper_submit.yaml"
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
    assert config.kis.paper_trading is True
    assert [item.value for item in config.kis.effective_broker_products()] == ["kis_domestic_stock"]


def test_contribution_config_rejects_invalid_buy_day(tmp_path):
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_tranquillo_yahoo.yaml").read_text())
    raw["execution"]["contribution"]["buy_day"] = 32
    config_path = tmp_path / "invalid_buy_day.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="buy_day"):
        load_config(config_path)


def test_contribution_config_allows_budget_above_legacy_max(tmp_path):
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_tranquillo_yahoo.yaml").read_text())
    raw["execution"]["contribution"]["monthly_budget"] = 5_000_000
    config_path = tmp_path / "legacy_max_not_a_cap.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.execution.contribution.monthly_budget == 5_000_000


def test_contribution_config_rejects_unsupported_policy(tmp_path):
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_tranquillo_yahoo.yaml").read_text())
    raw["execution"]["contribution"]["non_trading_day_policy"] = "skip"
    config_path = tmp_path / "invalid_policy.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="non_trading_day_policy"):
        load_config(config_path)


def _tranquillo_fragment() -> dict:
    return {
        "fragment_version": 1,
        "strategy": {
            "id": "tranquillo",
            "enabled": True,
            "weight": 1.0,
            "entrypoint": "tranquillo.strategy:TranquilloStrategy",
            "config": {
                "sleeve": "KRW",
                "allocations": {
                    "TIGER_NASDAQ100_LEVERAGE": 0.60,
                    "KODEX_US_DIVIDEND_DOWJONES": 0.40,
                },
            },
        },
        "portfolio": {
            "currency_sleeves": {
                "KRW": {
                    "cash_symbol": "CASH_KRW",
                    "symbols": [
                        "TIGER_NASDAQ100_LEVERAGE",
                        "KODEX_US_DIVIDEND_DOWJONES",
                    ],
                }
            }
        },
        "universe": {
            "instruments": [
                {
                    "symbol": "TIGER_NASDAQ100_LEVERAGE",
                    "asset_type": "domestic_etf",
                    "region": "KR",
                    "currency": "KRW",
                    "broker": "kis",
                    "broker_product": "kis_domestic_stock",
                    "broker_symbol": "418660",
                    "exchange_code": "KRX",
                    "quantity_step": 1,
                    "price_tick": 1,
                    "min_order_quantity": 1,
                    "min_order_notional": 1,
                },
                {
                    "symbol": "KODEX_US_DIVIDEND_DOWJONES",
                    "asset_type": "domestic_etf",
                    "region": "KR",
                    "currency": "KRW",
                    "broker": "kis",
                    "broker_product": "kis_domestic_stock",
                    "broker_symbol": "489250",
                    "exchange_code": "KRX",
                    "quantity_step": 1,
                    "price_tick": 1,
                    "min_order_quantity": 1,
                    "min_order_notional": 1,
                },
            ]
        },
        "datahub": {
            "symbol_map": {
                "TIGER_NASDAQ100_LEVERAGE": "418660.KS",
                "KODEX_US_DIVIDEND_DOWJONES": "489250.KS",
            }
        },
        "recommendations": {"execution": {"order_generation_mode": "buy_only_contribution"}},
    }


def _operator_signal_raw_with_absolute_fragments() -> dict:
    config_path = Path("configs/operator/symphony_signal.yaml")
    raw = yaml.safe_load(config_path.read_text())
    raw["app_fragment_paths"] = [
        str((config_path.parent / path).resolve()) for path in raw.get("app_fragment_paths", [])
    ]
    if raw.get("broker_accounts_path"):
        raw["broker_accounts_path"] = str(
            (config_path.parent / raw["broker_accounts_path"]).resolve()
        )
    return raw


def _write_operator_broker_accounts_with_toss_enabled(path: Path) -> None:
    raw = yaml.safe_load(Path("configs/operator/broker_accounts.yaml").read_text())
    for account in raw["accounts"]:
        if account["id"] == "toss_brokerage":
            account["enabled"] = True
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")


def test_multi_asset_readonly_example_uses_env_var_names_only():
    raw_text = Path("tests/fixtures/configs/live_readonly_multi_asset_kis.yaml").read_text()
    config = load_config("tests/fixtures/configs/live_readonly_multi_asset_kis.yaml")

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
    assert config.kis.account_id_env == "KIS_MOCK_ACCOUNT_ID"
    assert config.kis.app_key_env == "KIS_MOCK_APP_KEY"
    assert config.kis.app_secret_env == "KIS_MOCK_APP_SECRET"
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


def test_live_approval_root_config_is_minimal_operator_skeleton(monkeypatch):
    monkeypatch.delenv("MAESTRO_TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("MAESTRO_TELEGRAM_WHITELISTED_USER_IDS", raising=False)
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
    assert [item.value for item in config.kis.effective_broker_products()] == ["kis_domestic_stock"]
    assert config.kis.app_key_env == "KIS_MOCK_APP_KEY"
    assert config.kis.app_secret_env == "KIS_MOCK_APP_SECRET"
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
    config = load_config("tests/fixtures/configs/live_approval_us_etf.yaml")

    assert config.mode == "live_approval"
    assert config.portfolio.base_currency == "USD"
    assert config.portfolio.initial_cash is None
    assert config.portfolio.allowed_symbols == ["CASH_USD", "AAPL", "MSFT", "VOO", "QQQ"]
    assert [item.value for item in config.kis.effective_broker_products()] == ["kis_overseas_stock"]
    assert config.universe.get("AAPL").broker_product == BrokerProduct.KIS_OVERSEAS_STOCK
    assert config.universe.get("AAPL").exchange_code == "NASD"
    assert config.universe.get("VOO").asset_type == "etf"


def test_kis_multi_asset_live_approval_uses_yahoo_multi_provider_without_mock_fallback():
    raw = yaml.safe_load(
        Path("tests/fixtures/configs/live_approval_kis_multi_asset.yaml").read_text()
    )
    config = load_config("tests/fixtures/configs/live_approval_kis_multi_asset.yaml")

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
    raw = yaml.safe_load(Path("tests/fixtures/configs/live_approval_us_etf.yaml").read_text())
    del raw["portfolio"]["allowed_symbols"]
    config_path = tmp_path / "live_approval_us_etf_without_allowed_symbols.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.portfolio.allowed_symbols == [
        instrument.symbol for instrument in config.universe.instruments
    ]


def test_yahoo_symbol_map_explicit_values_override_universe_derivation(tmp_path):
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_yahoo_us_etf.yaml").read_text())
    raw["datahub"]["symbol_map"] = {"AAPL": "AAPL-CUSTOM"}
    config_path = tmp_path / "paper_yahoo_us_etf_custom_symbol_map.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    config = load_config(config_path)

    assert config.datahub.symbol_map["AAPL"] == "AAPL-CUSTOM"
    assert config.datahub.symbol_map["VOO"] == "VOO"


def test_research_multi_provider_example_registers_research_data_types():
    config = load_config("tests/fixtures/configs/paper_research_multi_provider.yaml")
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
    raw = yaml.safe_load(Path("tests/fixtures/configs/live_approval_us_etf.yaml").read_text())
    raw["portfolio"]["allowed_symbols"].append("TSLA")
    config_path = tmp_path / "missing_universe_symbol.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="TSLA"):
        load_config(config_path)


def test_us_etf_yahoo_paper_config_declares_usd_universe_and_symbol_map():
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_yahoo_us_etf.yaml").read_text())
    config = load_config("tests/fixtures/configs/paper_yahoo_us_etf.yaml")

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
    monkeypatch.delenv("KIS_MOCK_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_MOCK_APP_SECRET", raising=False)
    monkeypatch.delenv("KIS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("KIS_APPROVAL_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    raw_text = Path("configs/live_approval.yaml").read_text()
    config = load_config("configs/live_approval.yaml")

    assert config.kis.app_key_env == "KIS_MOCK_APP_KEY"
    assert config.kis.app_secret_env == "KIS_MOCK_APP_SECRET"
    assert config.kis.access_token_env == "KIS_ACCESS_TOKEN"
    assert config.kis.approval_key_env == "KIS_APPROVAL_KEY"
    assert config.approval.telegram_bot_token_env == "TELEGRAM_BOT_TOKEN"
    assert "xoxb-" not in raw_text
    assert "Bearer " not in raw_text
    assert "ghp_" not in raw_text
    assert "telegram.org/bot" not in raw_text
    assert "KIS_MOCK_APP_KEY:" not in raw_text
    assert "KIS_MOCK_APP_SECRET:" not in raw_text
    assert "KIS_ACCESS_TOKEN:" not in raw_text
    assert "KIS_APPROVAL_KEY:" not in raw_text
    assert "TELEGRAM_BOT_TOKEN:" not in raw_text

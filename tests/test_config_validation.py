from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from maestro.config.loader import load_config
from maestro.core.enums import BrokerProduct


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

    assert config.execution.live_order_enabled is False
    assert config.execution.require_reconciliation_pass is True
    assert config.execution.max_live_order_notional == 0.0
    assert config.execution.max_daily_live_notional == 0.0
    assert config.execution.max_daily_live_order_count == 0
    assert config.execution.daily_loss_limit is None
    assert config.execution.allowed_order_type == "limit"
    assert config.execution.order_status_poll_interval_seconds == 30.0
    assert config.execution.order_status_max_polls == 20
    assert config.execution.order_status_terminal_timeout_seconds == 1800.0


def test_live_order_lifecycle_config_validates_positive_max_polls(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["order_status_max_polls"] = 0
    config_path = tmp_path / "invalid_max_polls.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="order_status_max_polls"):
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


def test_current_sample_configs_load():
    for path in [
        "configs/paper.yaml",
        "configs/csv_paper.yaml",
        "configs/approval_paper.yaml",
        "configs/telegram_approval_paper.yaml",
        "configs/live_readonly.yaml",
        "configs/kis_live_readonly.example.yaml",
        "configs/live_approval.example.yaml",
        "configs/us_etf_yahoo_paper.yaml",
    ]:
        assert load_config(path)


def test_kis_live_readonly_example_config_uses_real_readonly_provider():
    config = load_config("configs/kis_live_readonly.example.yaml")

    assert config.mode == "live_readonly"
    assert config.portfolio.base_currency == "USD"
    assert config.kis.enabled is True
    assert config.kis.provider == "kis"
    assert config.kis.broker_product == "kis_overseas_stock"
    assert config.kis.account_id == "12345678-01"
    assert config.kis.app_key_env == "KIS_APP_KEY"
    assert config.kis.app_secret_env == "KIS_APP_SECRET"
    assert config.kis.access_token_env == "KIS_ACCESS_TOKEN"
    assert config.kis.token_cache_path == "var/kis_access_token.json"
    assert config.kis.paper_trading is False


def test_live_approval_example_config_is_safe_by_default():
    config = load_config("configs/live_approval.example.yaml")

    assert config.mode == "live_approval"
    assert config.portfolio.base_currency == "USD"
    assert config.portfolio.allowed_symbols == ["CASH_USD", "AAPL", "MSFT", "VOO", "QQQ"]
    assert config.execution.live_order_enabled is False
    assert config.execution.require_reconciliation_pass is True
    assert config.execution.allowed_order_type == "limit"
    assert config.execution.max_live_order_notional == 100.0
    assert config.execution.max_daily_live_notional == 300.0
    assert config.execution.max_daily_live_order_count == 3
    assert config.execution.daily_loss_limit is None
    assert config.approval.enabled is True
    assert config.approval.provider == "telegram"
    assert config.approval.require_approval is True
    assert config.approval.default_decision == "expired"
    assert config.kis.provider == "kis"
    assert config.kis.broker_product == "kis_overseas_stock"
    assert config.kis.app_key_env == "KIS_APP_KEY"
    assert config.kis.app_secret_env == "KIS_APP_SECRET"
    assert config.kis.access_token_env == "KIS_ACCESS_TOKEN"
    aapl = config.universe.get("AAPL")
    voo = config.universe.get("VOO")
    assert aapl is not None
    assert aapl.broker_product == BrokerProduct.KIS_OVERSEAS_STOCK
    assert aapl.exchange_code == "NASD"
    assert aapl.price_tick == 0.01
    assert aapl.quantity_step == 1
    assert voo is not None
    assert voo.asset_type == "etf"


def test_universe_requires_portfolio_symbols_to_be_declared(tmp_path):
    raw = yaml.safe_load(Path("configs/live_approval.example.yaml").read_text())
    raw["portfolio"]["allowed_symbols"].append("TSLA")
    config_path = tmp_path / "missing_universe_symbol.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="TSLA"):
        load_config(config_path)


def test_us_etf_yahoo_paper_config_declares_usd_universe_and_symbol_map():
    config = load_config("configs/us_etf_yahoo_paper.yaml")

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
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    raw_text = Path("configs/live_approval.example.yaml").read_text()
    config = load_config("configs/live_approval.example.yaml")

    assert config.kis.app_key_env == "KIS_APP_KEY"
    assert config.kis.app_secret_env == "KIS_APP_SECRET"
    assert config.kis.access_token_env == "KIS_ACCESS_TOKEN"
    assert config.approval.telegram_bot_token_env == "TELEGRAM_BOT_TOKEN"
    assert "xoxb-" not in raw_text
    assert "Bearer " not in raw_text
    assert "ghp_" not in raw_text
    assert "telegram.org/bot" not in raw_text
    assert "KIS_APP_KEY:" not in raw_text
    assert "KIS_APP_SECRET:" not in raw_text
    assert "KIS_ACCESS_TOKEN:" not in raw_text
    assert "TELEGRAM_BOT_TOKEN:" not in raw_text

from pathlib import Path

import pytest
import yaml

from maestro.config.identity import config_identity
from maestro.config.loader import load_config


def test_shared_broker_accounts_path_loads_accounts_and_affects_identity(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["broker_accounts_path"] = "broker_accounts.yaml"
    raw.pop("accounts", None)
    config_path = tmp_path / "symphony_signal.yaml"
    accounts_path = tmp_path / "broker_accounts.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    accounts_path.write_text(yaml.safe_dump({"accounts": _broker_accounts()}), encoding="utf-8")
    map_path.write_text(yaml.safe_dump(_strategy_accounts()), encoding="utf-8")

    config = load_config(config_path)
    before_identity = config_identity(config_path)

    assert config.broker_accounts_path == "broker_accounts.yaml"
    assert [(account.id, account.broker, account.environment) for account in config.accounts] == [
        ("kis_mock", "kis", "paper_trading"),
        ("kis_isa", "kis", "real"),
        ("kis_brokerage", "kis", "real"),
        ("dev_sandbox", "sandbox", "paper_trading"),
    ]

    updated = yaml.safe_load(accounts_path.read_text())
    updated["accounts"][1]["token_cache_path"] = "var/kis_isa_rotated_access_token.json"
    accounts_path.write_text(yaml.safe_dump(updated), encoding="utf-8")
    after_identity = config_identity(config_path)

    assert after_identity.fingerprint != before_identity.fingerprint
    assert after_identity.runtime_fingerprint != before_identity.runtime_fingerprint
    assert after_identity.state_fingerprint == before_identity.state_fingerprint


def test_shared_broker_accounts_path_rejects_inline_accounts(tmp_path):
    raw = _operator_signal_raw_with_absolute_fragments()
    raw["broker_accounts_path"] = "broker_accounts.yaml"
    config_path = tmp_path / "symphony_signal.yaml"
    accounts_path = tmp_path / "broker_accounts.yaml"
    map_path = tmp_path / "strategy_accounts.yaml"
    raw["accounts"] = _broker_accounts()
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    accounts_path.write_text(yaml.safe_dump({"accounts": raw["accounts"]}), encoding="utf-8")
    map_path.write_text(yaml.safe_dump(_strategy_accounts()), encoding="utf-8")

    with pytest.raises(ValueError, match="broker_accounts_path cannot be mixed"):
        load_config(config_path)


def _operator_signal_raw_with_absolute_fragments() -> dict:
    config_path = Path("configs/operator/symphony_signal.yaml")
    raw = yaml.safe_load(config_path.read_text())
    raw["app_fragment_paths"] = [
        str((config_path.parent / path).resolve()) for path in raw.get("app_fragment_paths", [])
    ]
    return raw


def _broker_accounts() -> list[dict]:
    return [
        {
            "id": "kis_mock",
            "broker": "kis",
            "environment": "paper_trading",
            "enabled": True,
            "provider": "kis",
            "broker_products": ["kis_domestic_stock"],
            "account_id_env": "KIS_MOCK_ACCOUNT_ID",
            "app_key_env": "KIS_MOCK_APP_KEY",
            "app_secret_env": "KIS_MOCK_APP_SECRET",
            "access_token_env": "KIS_ACCESS_TOKEN",
            "approval_key_env": "KIS_APPROVAL_KEY",
        },
        {
            "id": "kis_isa",
            "broker": "kis",
            "environment": "real",
            "enabled": True,
            "provider": "kis",
            "broker_products": ["kis_domestic_stock"],
            "account_id_env": "KIS_ISA_ACCOUNT_ID",
            "app_key_env": "KIS_ISA_APP_KEY",
            "app_secret_env": "KIS_ISA_APP_SECRET",
        },
        {
            "id": "kis_brokerage",
            "broker": "kis",
            "environment": "real",
            "enabled": True,
            "provider": "kis",
            "broker_products": ["kis_overseas_stock"],
            "account_id_env": "KIS_BROKERAGE_ACCOUNT_ID",
            "app_key_env": "KIS_BROKERAGE_APP_KEY",
            "app_secret_env": "KIS_BROKERAGE_APP_SECRET",
        },
        {
            "id": "dev_sandbox",
            "broker": "sandbox",
            "environment": "paper_trading",
            "enabled": True,
            "broker_products": ["kis_domestic_stock", "kis_overseas_stock"],
        },
    ]


def _strategy_accounts() -> dict:
    return {
        "strategies": {
            "tranquillo": {
                "account_id": "kis_isa",
                "readonly": True,
                "signal": True,
                "order_posture": "dry_run",
            },
            "crescendo_us": {
                "account_id": "kis_brokerage",
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

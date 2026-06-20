from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from maestro.config.app_fragment_composition import apply_app_fragments


@dataclass(frozen=True)
class PreparedConfig:
    raw: dict[str, Any]
    fingerprint_bytes: bytes


def prepare_config(path: str | Path) -> PreparedConfig:
    config_path = Path(path).expanduser().resolve()
    raw_bytes = config_path.read_bytes()
    raw = yaml.safe_load(raw_bytes)
    if not isinstance(raw, dict):
        raise ValueError(f"config must be a YAML mapping: {config_path}")
    values = dict(raw)
    fingerprint_bytes = raw_bytes
    values, app_fragment_fingerprint_parts = apply_app_fragments(
        values,
        config_path=config_path,
    )
    if app_fragment_fingerprint_parts:
        fingerprint_bytes = b"\0".join([fingerprint_bytes, *app_fragment_fingerprint_parts])
    accounts_path_value = values.get("broker_accounts_path")
    if accounts_path_value is not None:
        if not isinstance(accounts_path_value, str):
            raise ValueError("broker_accounts_path must be a string")
        accounts_path = _resolve_config_relative_path(accounts_path_value, config_path)
        accounts_bytes = accounts_path.read_bytes()
        values = _apply_broker_accounts(values, accounts_path, accounts_bytes)
        fingerprint_bytes = b"\0".join(
            [
                fingerprint_bytes,
                b"broker_accounts_path",
                str(accounts_path).encode("utf-8"),
                accounts_bytes,
            ]
        )
    map_path_value = values.get("strategy_account_map_path")
    if map_path_value is not None:
        if not isinstance(map_path_value, str):
            raise ValueError("strategy_account_map_path must be a string")
        map_path = _resolve_config_relative_path(map_path_value, config_path)
        map_bytes = map_path.read_bytes()
        values = _apply_strategy_account_map(values, map_path, map_bytes)
        fingerprint_bytes = b"\0".join(
            [
                fingerprint_bytes,
                b"strategy_account_map_path",
                str(map_path).encode("utf-8"),
                map_bytes,
            ]
        )
    return PreparedConfig(raw=values, fingerprint_bytes=fingerprint_bytes)


def _resolve_config_relative_path(path_value: str, config_path: Path) -> Path:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def _apply_broker_accounts(
    raw: dict[str, Any], accounts_path: Path, accounts_bytes: bytes
) -> dict[str, Any]:
    if "accounts" in raw:
        raise ValueError("broker_accounts_path cannot be mixed with inline accounts")
    accounts_raw = yaml.safe_load(accounts_bytes)
    if not isinstance(accounts_raw, dict):
        raise ValueError(f"broker accounts config must be a YAML mapping: {accounts_path}")
    accounts = accounts_raw.get("accounts")
    if not isinstance(accounts, list):
        raise ValueError(f"broker accounts config requires an accounts list: {accounts_path}")
    values = dict(raw)
    values["accounts"] = accounts
    return values


def _apply_strategy_account_map(
    raw: dict[str, Any], map_path: Path, map_bytes: bytes
) -> dict[str, Any]:
    mapping_raw = yaml.safe_load(map_bytes)
    if not isinstance(mapping_raw, dict):
        raise ValueError(f"strategy account map must be a YAML mapping: {map_path}")
    strategy_map = mapping_raw.get("strategies")
    if not isinstance(strategy_map, dict):
        raise ValueError(f"strategy account map requires a strategies mapping: {map_path}")
    if not all(isinstance(key, str) for key in strategy_map):
        raise ValueError(f"strategy account map strategy ids must be strings: {map_path}")

    strategies = raw.get("strategies", [])
    if not isinstance(strategies, list):
        return raw
    strategy_ids = {strategy.get("id") for strategy in strategies if isinstance(strategy, dict)}
    known_ids = {strategy_id for strategy_id in strategy_ids if strategy_id}
    unknown_ids = sorted(set(strategy_map) - known_ids)
    if unknown_ids:
        raise ValueError(
            "strategy_account_map_path contains unknown strategy ids: " + ", ".join(unknown_ids)
        )

    mapped_strategies = []
    missing_ids = []
    for strategy in strategies:
        if not isinstance(strategy, dict):
            mapped_strategies.append(strategy)
            continue
        strategy_values = dict(strategy)
        strategy_id = strategy_values.get("id")
        if not isinstance(strategy_id, str):
            mapped_strategies.append(strategy_values)
            continue
        if strategy_id not in strategy_map:
            if strategy_values.get("enabled", True) and strategy_values.get("signal_enabled", True):
                missing_ids.append(strategy_id)
            mapped_strategies.append(strategy_values)
            continue
        mapped_config = strategy_map[strategy_id]
        mapped_account_id = _mapped_account_id(mapped_config)
        inline_account_id = strategy_values.get("account_id")
        if inline_account_id and inline_account_id != mapped_account_id:
            raise ValueError(
                f"strategy {strategy_id} inline account_id {inline_account_id} "
                f"conflicts with strategy_account_map_path account_id {mapped_account_id}"
            )
        if mapped_account_id:
            strategy_values["account_id"] = mapped_account_id
        if isinstance(mapped_config, dict):
            if "execution_sleeve" in mapped_config:
                strategy_values["execution_sleeve"] = mapped_config["execution_sleeve"]
            if "enabled" in mapped_config:
                strategy_values["enabled"] = bool(mapped_config["enabled"])
            if "readonly" in mapped_config:
                strategy_values["readonly_enabled"] = bool(mapped_config["readonly"])
            if "signal" in mapped_config:
                strategy_values["signal_enabled"] = bool(mapped_config["signal"])
            if "order_posture" in mapped_config:
                strategy_values["order_posture"] = mapped_config["order_posture"]
        mapped_strategies.append(strategy_values)
    if missing_ids:
        raise ValueError(
            "strategy_account_map_path is missing enabled strategy ids: "
            + ", ".join(sorted(missing_ids))
        )

    values = dict(raw)
    values["strategies"] = mapped_strategies
    if "execution_sleeves" in mapping_raw:
        if (
            "execution_sleeves" in values
            and values["execution_sleeves"] != mapping_raw["execution_sleeves"]
        ):
            raise ValueError("strategy_account_map_path execution_sleeves conflicts with config")
        values["execution_sleeves"] = mapping_raw["execution_sleeves"]
    if "multi_account_contributions" in mapping_raw:
        if (
            "multi_account_contributions" in values
            and values["multi_account_contributions"] != mapping_raw["multi_account_contributions"]
        ):
            raise ValueError(
                "strategy_account_map_path multi_account_contributions conflicts with config"
            )
        values["multi_account_contributions"] = mapping_raw["multi_account_contributions"]
    if "account_strategy_targets" in mapping_raw:
        if (
            "account_strategy_targets" in values
            and values["account_strategy_targets"] != mapping_raw["account_strategy_targets"]
        ):
            raise ValueError(
                "strategy_account_map_path account_strategy_targets conflicts with config"
            )
        values["account_strategy_targets"] = mapping_raw["account_strategy_targets"]
    return values


def _mapped_account_id(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        account_id = value.get("account_id")
        if account_id is not None and not isinstance(account_id, str):
            raise ValueError("strategy account map account_id must be a string")
        return account_id
    raise ValueError("strategy account map values must be strings or mappings")


__all__ = ["PreparedConfig", "prepare_config"]

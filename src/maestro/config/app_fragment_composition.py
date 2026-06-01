from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

APP_FRAGMENT_ALLOWED_TOP_LEVEL_KEYS = {
    "fragment_version",
    "strategy",
    "portfolio",
    "universe",
    "datahub",
    "recommendations",
}
APP_FRAGMENT_OPERATOR_OWNED_KEYS = {
    "mode",
    "accounts",
    "execution",
    "approval",
    "risk",
    "state",
    "audit",
    "kis",
    "reconciliation",
    "monitoring",
    "profile_stage",
    "strategy_account_map_path",
    "broker_accounts_path",
    "app_fragment_paths",
    "app_fragment_recommendations",
}
APP_FRAGMENT_STRATEGY_OPERATOR_OVERRIDE_KEYS = {
    "enabled",
    "weight",
    "account_id",
    "readonly_enabled",
    "signal_enabled",
    "order_posture",
    "config",
}


def apply_app_fragments(
    raw: dict[str, Any],
    *,
    config_path: Path,
) -> tuple[dict[str, Any], list[bytes]]:
    path_values = raw.get("app_fragment_paths")
    if path_values is None:
        return raw, []
    if not isinstance(path_values, list) or not all(
        isinstance(path_value, str) for path_value in path_values
    ):
        raise ValueError("app_fragment_paths must be a list of strings")
    if "app_fragment_recommendations" in raw:
        raise ValueError("app_fragment_recommendations is internal and must not be configured")
    if "app_fragment_strategy_recommendations" in raw:
        raise ValueError(
            "app_fragment_strategy_recommendations is internal and must not be configured"
        )

    values = dict(raw)
    fingerprint_parts: list[bytes] = []
    recommendations: dict[str, Any] = {}
    strategy_recommendations: dict[str, Any] = {}
    for path_value in path_values:
        fragment_path = _resolve_fragment_path(path_value, config_path)
        fragment_bytes = fragment_path.read_bytes()
        fragment = yaml.safe_load(fragment_bytes)
        if not isinstance(fragment, dict):
            raise ValueError(f"app fragment must be a YAML mapping: {fragment_path}")
        _validate_app_fragment(fragment, fragment_path)
        values = _merge_app_fragment(values, fragment, fragment_path)
        fragment_recommendations = deepcopy(fragment.get("recommendations", {}))
        recommendations = _merge_nested_dicts(
            recommendations,
            fragment_recommendations,
            label=f"app fragment recommendations in {fragment_path}",
        )
        fragment_strategy = fragment.get("strategy")
        if isinstance(fragment_strategy, dict) and isinstance(fragment_strategy.get("id"), str):
            strategy_recommendations[fragment_strategy["id"]] = _merge_nested_dicts(
                strategy_recommendations.get(fragment_strategy["id"], {}),
                fragment_recommendations,
                label=f"app fragment strategy recommendations in {fragment_path}",
            )
        fingerprint_parts.extend(
            [
                b"app_fragment_path",
                str(fragment_path).encode("utf-8"),
                fragment_bytes,
            ]
        )
    if recommendations:
        values["app_fragment_recommendations"] = recommendations
    if strategy_recommendations:
        values["app_fragment_strategy_recommendations"] = strategy_recommendations
    return values, fingerprint_parts


def app_fragment_recommendation_failures(config: Any) -> list[str]:
    recommendations = getattr(config, "app_fragment_recommendations", {}) or {}
    strategy_recommendations = (
        getattr(config, "app_fragment_strategy_recommendations", {}) or {}
    )
    if not recommendations and not strategy_recommendations:
        return []
    payload = config.model_dump(mode="json")
    failures: list[str] = []
    strategy_paths = set()
    for strategy_id, recs in strategy_recommendations.items():
        strategy = _strategy_by_id(config, strategy_id)
        if strategy is None:
            continue
        for path, expected in _flatten_recommendations(recs):
            strategy_paths.add(tuple(path))
            if tuple(path) == ("execution", "order_generation_mode") and hasattr(
                config, "effective_strategy_order_generation_mode"
            ):
                actual = config.effective_strategy_order_generation_mode(strategy)
            else:
                actual = _nested_get(payload, path)
            if actual != expected:
                failures.append(
                    "app_fragment_recommendation_mismatch:"
                    f"{strategy_id}:{'.'.join(path)}:"
                    f"recommended={expected}:actual={actual}"
                )
    for path, expected in _flatten_recommendations(recommendations):
        if tuple(path) in strategy_paths:
            continue
        actual = _nested_get(payload, path)
        if actual != expected:
            failures.append(
                "app_fragment_recommendation_mismatch:"
                f"{'.'.join(path)}:recommended={expected}:actual={actual}"
            )
    return failures


def _strategy_by_id(config: Any, strategy_id: str) -> Any | None:
    for strategy in getattr(config, "strategies", []):
        if getattr(strategy, "id", None) == strategy_id:
            return strategy
    return None


def _resolve_fragment_path(path_value: str, config_path: Path) -> Path:
    fragment_path = Path(path_value).expanduser()
    if not fragment_path.is_absolute():
        fragment_path = config_path.parent / fragment_path
    return fragment_path.resolve()


def _validate_app_fragment(fragment: dict[str, Any], fragment_path: Path) -> None:
    operator_owned = sorted(set(fragment) & APP_FRAGMENT_OPERATOR_OWNED_KEYS)
    if operator_owned:
        raise ValueError(
            f"app fragment contains operator-owned keys in {fragment_path}: "
            + ", ".join(operator_owned)
        )
    unknown = sorted(set(fragment) - APP_FRAGMENT_ALLOWED_TOP_LEVEL_KEYS)
    if unknown:
        raise ValueError(
            f"app fragment contains unsupported keys in {fragment_path}: "
            + ", ".join(unknown)
        )
    _validate_nested_keys(fragment, fragment_path)


def _validate_nested_keys(fragment: dict[str, Any], fragment_path: Path) -> None:
    portfolio = fragment.get("portfolio", {})
    if portfolio and (
        not isinstance(portfolio, dict) or set(portfolio) - {"currency_sleeves"}
    ):
        raise ValueError(
            f"app fragment portfolio may only contain currency_sleeves: {fragment_path}"
        )
    universe = fragment.get("universe", {})
    if universe and (
        not isinstance(universe, dict) or set(universe) - {"instruments", "research_symbols"}
    ):
        raise ValueError(
            f"app fragment universe may only contain instruments and research_symbols: "
            f"{fragment_path}"
        )
    datahub = fragment.get("datahub", {})
    if datahub and (not isinstance(datahub, dict) or set(datahub) - {"symbol_map"}):
        raise ValueError(f"app fragment datahub may only contain symbol_map: {fragment_path}")
    if "strategy" in fragment and not isinstance(fragment["strategy"], dict):
        raise ValueError(f"app fragment strategy must be a mapping: {fragment_path}")
    if "recommendations" in fragment and not isinstance(fragment["recommendations"], dict):
        raise ValueError(f"app fragment recommendations must be a mapping: {fragment_path}")


def _merge_app_fragment(
    raw: dict[str, Any],
    fragment: dict[str, Any],
    fragment_path: Path,
) -> dict[str, Any]:
    values = deepcopy(raw)
    if strategy := fragment.get("strategy"):
        values = _merge_fragment_strategy(values, strategy, fragment_path)
    if portfolio := fragment.get("portfolio"):
        values = _merge_currency_sleeves(values, portfolio.get("currency_sleeves", {}))
    if universe := fragment.get("universe"):
        values = _merge_universe(values, universe)
    if datahub := fragment.get("datahub"):
        values = _merge_datahub(values, datahub)
    return values


def _merge_fragment_strategy(
    raw: dict[str, Any],
    fragment_strategy: dict[str, Any],
    fragment_path: Path,
) -> dict[str, Any]:
    strategy_id = fragment_strategy.get("id")
    if not isinstance(strategy_id, str) or not strategy_id:
        raise ValueError(f"app fragment strategy.id is required: {fragment_path}")

    values = dict(raw)
    strategies = list(values.get("strategies", []))
    if not all(isinstance(strategy, dict) for strategy in strategies):
        raise ValueError("strategies must be a list of mappings when app fragments are used")

    for index, operator_strategy in enumerate(strategies):
        if operator_strategy.get("id") != strategy_id:
            continue
        strategies[index] = _merge_strategy_dict(fragment_strategy, operator_strategy)
        values["strategies"] = strategies
        return values

    strategies.append(deepcopy(fragment_strategy))
    values["strategies"] = strategies
    return values


def _merge_strategy_dict(
    fragment_strategy: dict[str, Any],
    operator_strategy: dict[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(fragment_strategy)
    for key, operator_value in operator_strategy.items():
        if key == "config":
            merged["config"] = _merge_nested_dicts(
                deepcopy(merged.get("config", {})),
                deepcopy(operator_value or {}),
                label=f"strategy {operator_strategy.get('id')} config",
            )
            continue
        if key in APP_FRAGMENT_STRATEGY_OPERATOR_OVERRIDE_KEYS:
            merged[key] = deepcopy(operator_value)
            continue
        if key in merged and merged[key] != operator_value:
            raise ValueError(
                f"Conflicting app fragment strategy field for {operator_strategy.get('id')}: {key}"
            )
        merged[key] = deepcopy(operator_value)
    return merged


def _merge_currency_sleeves(
    raw: dict[str, Any],
    fragment_sleeves: dict[str, Any],
) -> dict[str, Any]:
    if not fragment_sleeves:
        return raw
    values = dict(raw)
    portfolio = dict(values.get("portfolio", {}))
    sleeves = dict(portfolio.get("currency_sleeves", {}))
    for sleeve_name, fragment_sleeve in fragment_sleeves.items():
        if sleeve_name in sleeves and sleeves[sleeve_name] != fragment_sleeve:
            raise ValueError(f"Conflicting app fragment currency sleeve: {sleeve_name}")
        sleeves[sleeve_name] = deepcopy(fragment_sleeve)
    portfolio["currency_sleeves"] = sleeves
    values["portfolio"] = portfolio
    return values


def _merge_universe(raw: dict[str, Any], fragment_universe: dict[str, Any]) -> dict[str, Any]:
    values = dict(raw)
    universe = dict(values.get("universe", {}))
    instruments = list(universe.get("instruments", []))
    instruments_by_symbol = {
        instrument.get("symbol"): instrument
        for instrument in instruments
        if isinstance(instrument, dict) and instrument.get("symbol")
    }
    for fragment_instrument in fragment_universe.get("instruments", []):
        symbol = fragment_instrument.get("symbol")
        if symbol in instruments_by_symbol:
            if instruments_by_symbol[symbol] != fragment_instrument:
                raise ValueError(f"Conflicting app fragment instrument: {symbol}")
            continue
        instruments.append(deepcopy(fragment_instrument))
    universe["instruments"] = instruments

    research_symbols = list(universe.get("research_symbols", []))
    for symbol in fragment_universe.get("research_symbols", []):
        if symbol not in research_symbols:
            research_symbols.append(symbol)
    if research_symbols:
        universe["research_symbols"] = research_symbols
    values["universe"] = universe
    return values


def _merge_datahub(raw: dict[str, Any], fragment_datahub: dict[str, Any]) -> dict[str, Any]:
    fragment_symbol_map = fragment_datahub.get("symbol_map", {})
    if not fragment_symbol_map:
        return raw
    values = dict(raw)
    datahub = dict(values.get("datahub", {}))
    symbol_map = dict(datahub.get("symbol_map", {}))
    for symbol, provider_symbol in fragment_symbol_map.items():
        if symbol in symbol_map and symbol_map[symbol] != provider_symbol:
            raise ValueError(f"Conflicting app fragment datahub symbol_map: {symbol}")
        symbol_map[symbol] = provider_symbol
    datahub["symbol_map"] = symbol_map
    values["datahub"] = datahub
    return values


def _merge_nested_dicts(
    base: dict[str, Any],
    override: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_nested_dicts(merged[key], value, label=label)
        else:
            merged[key] = deepcopy(value)
    return merged


def _flatten_recommendations(
    recommendations: dict[str, Any],
    prefix: tuple[str, ...] = (),
) -> list[tuple[tuple[str, ...], Any]]:
    flattened: list[tuple[tuple[str, ...], Any]] = []
    for key, value in recommendations.items():
        path = (*prefix, str(key))
        if isinstance(value, dict):
            flattened.extend(_flatten_recommendations(value, path))
        else:
            flattened.append((path, value))
    return flattened


def _nested_get(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


__all__ = [
    "apply_app_fragments",
    "app_fragment_recommendation_failures",
]

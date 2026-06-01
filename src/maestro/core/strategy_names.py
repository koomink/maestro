"""Canonical human-facing names for Virtuoso strategies."""

from __future__ import annotations

_DISPLAY_NAMES = {
    "ataraxia": "Tranquillo",
    "tranquillo": "Tranquillo",
    "snowball": "Crescendo",
    "snowball_us": "Crescendo",
    "crescendo": "Crescendo",
    "crescendo_us": "Crescendo",
    "tradingagents": "Fugue",
    "trading_agents": "Fugue",
    "fugue": "Fugue",
}

_COMMAND_SLUGS = {
    "ataraxia": "tranquillo",
    "tranquillo": "tranquillo",
    "snowball": "crescendo",
    "snowball_us": "crescendo",
    "crescendo": "crescendo",
    "crescendo_us": "crescendo",
    "tradingagents": "fugue",
    "trading_agents": "fugue",
    "fugue": "fugue",
}


def strategy_display_name(strategy_id: object) -> str:
    raw = str(strategy_id or "")
    normalized = raw.strip().lower().replace("-", "_")
    return _DISPLAY_NAMES.get(normalized, raw or "unknown")


def strategy_display_label(strategy_ids: list[str]) -> str:
    if not strategy_ids:
        return "unknown"
    return ", ".join(strategy_display_name(strategy_id) for strategy_id in strategy_ids)


def strategy_command_slug(strategy_id: object) -> str | None:
    raw = str(strategy_id or "")
    normalized = raw.strip().lower().replace("-", "_")
    return _COMMAND_SLUGS.get(normalized)

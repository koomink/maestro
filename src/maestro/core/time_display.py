from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def operator_timezone(config: Any, default: str = "UTC") -> str:
    market_session = getattr(getattr(config, "execution", None), "market_session", None)
    timezone = getattr(market_session, "timezone", None)
    return str(timezone or default)


def format_operator_time(value: object, timezone: str, *, default: str | None = None) -> str:
    parsed = parse_utc_time(value)
    if parsed is None:
        if value is None:
            return default or "none"
        return str(value)
    local = parsed.astimezone(_zoneinfo(timezone))
    suffix = local.tzname() or timezone
    return f"{local:%Y-%m-%d %H:%M:%S} {suffix}"


def parse_utc_time(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def add_operator_time_details(details: dict[str, Any], timezone: str) -> dict[str, Any]:
    output = dict(details)
    for key, value in list(details.items()):
        if _looks_like_time_key(key) and value:
            output[f"{key}_display"] = format_operator_time(value, timezone)
    return output


def add_operator_time_fields(value: Any, timezone: str) -> Any:
    if isinstance(value, list):
        return [add_operator_time_fields(item, timezone) for item in value]
    if not isinstance(value, dict):
        return value
    output = {key: add_operator_time_fields(item, timezone) for key, item in value.items()}
    for key, item in value.items():
        if _looks_like_time_key(str(key)) and item:
            output[f"{key}_display"] = format_operator_time(item, timezone)
    return output


def _looks_like_time_key(key: str) -> bool:
    return key.endswith("_at") or key.endswith("_time") or key in {"created_at", "updated_at"}


def _zoneinfo(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")

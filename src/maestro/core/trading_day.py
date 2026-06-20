from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from maestro.core.clock import utc_now


def _zoneinfo(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _as_utc(moment: datetime | None) -> datetime:
    moment = moment or utc_now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def trading_day(timezone: str, *, at: datetime | None = None) -> date:
    """Return the local calendar date for `at` (default now) in `timezone`."""
    return _as_utc(at).astimezone(_zoneinfo(timezone)).date()


def trading_day_bounds_utc(
    timezone: str,
    *,
    at: datetime | None = None,
) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` bounds of the trading day containing `at` in `timezone`."""
    tz = _zoneinfo(timezone)
    local = _as_utc(at).astimezone(tz)
    start_local = datetime(local.year, local.month, local.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def trading_day_bounds_utc_str(
    timezone: str,
    *,
    at: datetime | None = None,
) -> tuple[str, str]:
    """Trading-day UTC bounds formatted to match SQLite ``created_at`` strings."""
    start, end = trading_day_bounds_utc(timezone, at=at)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), end.strftime(fmt)


__all__ = ["trading_day", "trading_day_bounds_utc", "trading_day_bounds_utc_str"]

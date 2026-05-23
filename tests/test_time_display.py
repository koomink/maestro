from datetime import UTC, datetime

from maestro.core.time_display import format_operator_time, operator_timezone


def test_format_operator_time_converts_sqlite_utc_to_operator_timezone():
    assert format_operator_time("2026-05-23 06:43:23", "Asia/Seoul") == "2026-05-23 15:43:23 KST"


def test_format_operator_time_converts_iso_utc_to_operator_timezone():
    assert (
        format_operator_time("2026-05-23T06:43:23+00:00", "Asia/Seoul") == "2026-05-23 15:43:23 KST"
    )


def test_format_operator_time_leaves_unparseable_values_readable():
    assert format_operator_time("not-a-time", "Asia/Seoul") == "not-a-time"


def test_operator_timezone_uses_market_session_timezone():
    class Config:
        class Execution:
            class MarketSession:
                timezone = "Asia/Seoul"

            market_session = MarketSession()

        execution = Execution()

    assert operator_timezone(Config()) == "Asia/Seoul"


def test_format_operator_time_accepts_datetime():
    value = datetime(2026, 5, 23, 6, 43, 23, tzinfo=UTC)

    assert format_operator_time(value, "Asia/Seoul") == "2026-05-23 15:43:23 KST"

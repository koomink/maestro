from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from maestro.integrations.telegram.ui.format import (
    deadline_kr,
    money_full,
    money_kr,
    quantity_kr,
)


def test_money_kr_krw_over_10k_uses_manwon():
    assert money_kr(1_240_000, "KRW") == "124만원"
    assert money_kr(712_000, "KRW") == "71.2만원"


def test_money_kr_krw_under_10k_uses_won():
    assert money_kr(8_500, "KRW") == "8,500원"


def test_money_kr_usd():
    assert money_kr(1_240.5, "USD") == "$1,240.50"


def test_money_kr_unknown_currency_falls_back():
    assert money_kr(100.0, "JPY") == "100.00 JPY"
    assert money_kr(100.0, None) == "100.00"


def test_money_full_keeps_all_digits():
    assert money_full(1_240_000, "KRW") == "1,240,000원"
    assert money_full(1_240.5, "USD") == "$1,240.50"


def test_quantity_kr():
    assert quantity_kr(10) == "10주"
    assert quantity_kr(28.0) == "28주"
    assert quantity_kr(0.5) == "0.5주"


def test_deadline_kr_buckets():
    # 23:30 KST = 14:30 UTC
    dt = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
    assert deadline_kr(dt) == "밤 11시 30분"
    # 09:10 KST
    dt2 = datetime(2026, 8, 10, 0, 10, tzinfo=UTC)
    assert deadline_kr(dt2) == "오전 9시 10분"
    # 분이 0이면 생략, 15:00 KST → 오후 3시
    dt3 = datetime(2026, 8, 10, 6, 0, tzinfo=UTC)
    assert deadline_kr(dt3) == "오후 3시"


def test_deadline_kr_respects_explicit_tz():
    dt = datetime(2026, 8, 10, 14, 30, tzinfo=UTC)
    assert deadline_kr(dt, tz=ZoneInfo("UTC")) == "오후 2시 30분"

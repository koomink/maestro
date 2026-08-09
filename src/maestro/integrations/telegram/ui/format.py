"""한국어 표기 유틸. 순수 함수만 — 네트워크·DB·설정 접근 금지."""

from datetime import datetime
from zoneinfo import ZoneInfo

_KST = ZoneInfo("Asia/Seoul")

# (시작시, 끝시) → 접두어. 12시간제 변환 전 24시간 기준.
_HOUR_LABELS = (
    (0, 5, "새벽"),
    (6, 11, "오전"),
    (12, 17, "오후"),
    (18, 20, "저녁"),
    (21, 23, "밤"),
)


def money_kr(value: float, currency: str | None) -> str:
    if currency == "KRW":
        if abs(value) >= 10_000:
            man = round(value / 10_000, 1)
            label = f"{man:,.1f}".rstrip("0").rstrip(".")
            return f"{label}만원"
        return f"{value:,.0f}원"
    if currency == "USD":
        return f"${value:,.2f}"
    suffix = f" {currency}" if currency else ""
    return f"{value:,.2f}{suffix}"


def money_full(value: float, currency: str | None) -> str:
    if currency == "KRW":
        return f"{value:,.0f}원"
    if currency == "USD":
        return f"${value:,.2f}"
    suffix = f" {currency}" if currency else ""
    return f"{value:,.2f}{suffix}"


def quantity_kr(value: float) -> str:
    label = f"{value:,.4f}".rstrip("0").rstrip(".")
    return f"{label}주"


def deadline_kr(dt: datetime, tz: ZoneInfo | None = None) -> str:
    local = dt.astimezone(tz or _KST)
    prefix = next(
        label for start, end, label in _HOUR_LABELS if start <= local.hour <= end
    )
    hour12 = local.hour % 12
    if hour12 == 0:
        hour12 = 12
    if local.minute:
        return f"{prefix} {hour12}시 {local.minute}분"
    return f"{prefix} {hour12}시"

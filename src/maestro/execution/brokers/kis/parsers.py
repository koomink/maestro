from typing import Any

from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.brokers.kis.models import KISOrderSummary, KISPosition
from maestro.execution.live_order_models import (
    BrokerOrderId,
    FillEvent,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)


def _order_summary(row: dict[str, Any]) -> KISOrderSummary:
    return KISOrderSummary(
        order_id=str(row.get("odno") or row.get("orgn_odno") or ""),
        symbol=str(row.get("pdno") or ""),
        name=_optional_str(row.get("prdt_name")),
        side=_side(row.get("sll_buy_dvsn_cd") or row.get("sll_buy_dvsn_cd_name")),
        quantity=_required_first_float(row, "ord_qty", "tot_ccld_qty", context="order quantity"),
        filled_quantity=_required_first_float(
            row,
            "tot_ccld_qty",
            "ccld_qty",
            context="filled quantity",
            default=0.0,
        ),
        average_fill_price=_optional_first_float(row, "avg_prvs", "ord_unpr"),
        status=_status(row).value,
        raw_status=_raw_order_status(row),
        submitted_at=utc_now(),
    )


def _overseas_position(
    row: dict[str, Any],
    *,
    canonical_symbol,
) -> KISPosition:
    broker_symbol = str(row.get("ovrs_pdno") or row.get("pdno") or row.get("std_pdno") or "")
    return KISPosition(
        symbol=canonical_symbol(broker_symbol),
        name=_optional_str(row.get("ovrs_item_name") or row.get("prdt_name")),
        quantity=_first_float(row, "ovrs_cblc_qty", "ord_psbl_qty", "cblc_qty13"),
        average_price=_first_float(row, "pchs_avg_pric", "avg_unpr3"),
        current_price=_overseas_position_current_price(row),
        unrealized_pnl=_optional_first_float(row, "frcr_evlu_pfls_amt", "evlu_pfls_amt2"),
    )


def _overseas_order_summary(
    row: dict[str, Any],
    *,
    canonical_symbol,
) -> KISOrderSummary:
    broker_symbol = str(row.get("pdno") or row.get("ovrs_pdno") or "")
    return KISOrderSummary(
        order_id=str(row.get("odno") or row.get("orgn_odno") or ""),
        symbol=canonical_symbol(broker_symbol),
        name=_optional_str(row.get("prdt_name") or row.get("ovrs_item_name")),
        side=_side(row.get("sll_buy_dvsn_cd") or row.get("sll_buy_dvsn_cd_name")),
        quantity=_required_first_float(row, "ft_ord_qty", "ord_qty", context="order quantity"),
        filled_quantity=_required_first_float(
            row,
            "ft_ccld_qty",
            "tot_ccld_qty",
            "ccld_qty",
            context="filled quantity",
            default=0.0,
        ),
        average_fill_price=_optional_first_float(row, "ft_ccld_unpr3", "avg_prvs", "ft_ord_unpr3"),
        status=_status(row).value,
        raw_status=_raw_order_status(row),
        submitted_at=utc_now(),
    )


def _status_snapshot_from_summary(
    broker_order: BrokerOrderId,
    summary: KISOrderSummary,
) -> LiveOrderStatusSnapshot:
    status = _order_status_from_text(summary.status)
    fill_count = 1 if summary.filled_quantity > 0 else 0
    fills = []
    if summary.filled_quantity > 0 and summary.average_fill_price is not None:
        fills.append(
            FillEvent(
                broker_order_id=broker_order.broker_order_id,
                symbol=summary.symbol,
                quantity=summary.filled_quantity,
                price=summary.average_fill_price,
                filled_at=summary.submitted_at.isoformat(),
                raw={"raw_status": summary.raw_status},
            )
        )
    return LiveOrderStatusSnapshot(
        broker_order=broker_order,
        status=status,
        checked_at=utc_now().isoformat(),
        symbol=summary.symbol,
        side=_order_side_from_text(summary.side),
        partial_fill=PartialFillSummary(
            ordered_quantity=summary.quantity,
            filled_quantity=summary.filled_quantity,
            remaining_quantity=max(summary.quantity - summary.filled_quantity, 0.0),
            average_fill_price=summary.average_fill_price,
            fill_count=fill_count,
        ),
        fills=fills,
        raw_status=summary.raw_status or summary.status,
        raw=summary.model_dump(mode="json"),
    )


def _unknown_status_snapshot(
    broker_order: BrokerOrderId,
    *,
    message: str,
) -> LiveOrderStatusSnapshot:
    return LiveOrderStatusSnapshot(
        broker_order=broker_order,
        status=OrderStatus.UNKNOWN,
        checked_at=utc_now().isoformat(),
        partial_fill=PartialFillSummary(
            ordered_quantity=0.0,
            filled_quantity=0.0,
            remaining_quantity=0.0,
        ),
        message=message,
    )


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _first_item(value: Any) -> dict[str, Any]:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return _as_dict(value)


def _first_float(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    value = _optional_first_float(row, *keys)
    return default if value is None else value


def _optional_first_float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _optional_float(row.get(key))
        if value is not None:
            return value
    return None


def _required_first_float(
    row: dict[str, Any],
    *keys: str,
    context: str,
    default: float | None = None,
) -> float:
    for key in keys:
        raw_value = row.get(key)
        if raw_value in (None, ""):
            continue
        value = _optional_float(raw_value)
        if value is None:
            raise ValueError(f"Malformed KIS numeric field for {context}: {key}")
        return value
    if default is not None:
        return default
    raise ValueError(f"Missing KIS numeric field for {context}: {', '.join(keys)}")


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _optional_str(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _position_current_price(row: dict[str, Any]) -> float:
    price = _optional_first_float(row, "prpr", "now_pric", "stck_prpr")
    if price is not None:
        return price
    quantity = _optional_first_float(row, "hldg_qty", "ord_psbl_qty")
    market_value = _optional_float(row.get("evlu_amt"))
    if quantity and market_value is not None:
        return market_value / quantity
    return 0.0


def _overseas_position_current_price(row: dict[str, Any]) -> float:
    price = _optional_first_float(row, "now_pric2", "ovrs_now_pric1", "ovrs_now_pric")
    if price is not None:
        return price
    quantity = _optional_first_float(row, "ovrs_cblc_qty", "cblc_qty13")
    market_value = _optional_first_float(row, "ovrs_stck_evlu_amt", "frcr_evlu_amt2")
    if quantity and market_value is not None:
        return market_value / quantity
    return 0.0


def _side(value: Any) -> str:
    text = str(value or "")
    if text in {"01", "매도", "sell"}:
        return "sell"
    if text in {"02", "매수", "buy"}:
        return "buy"
    return text or "unknown"


def _status(row: dict[str, Any]) -> OrderStatus:
    raw_status = _raw_order_status(row)
    normalized = _order_status_from_text(raw_status or "")
    if normalized in {OrderStatus.REJECTED, OrderStatus.CANCELED}:
        return normalized
    ordered = _first_float(row, "ord_qty", "ft_ord_qty", default=0.0)
    filled = _first_float(row, "tot_ccld_qty", "ccld_qty", "ft_ccld_qty", default=0.0)
    if ordered > 0 and filled >= ordered:
        return OrderStatus.FILLED
    if filled > 0:
        return OrderStatus.PARTIALLY_FILLED
    return OrderStatus.OPEN


def _raw_order_status(row: dict[str, Any]) -> str | None:
    return _optional_str(
        row.get("ord_dvsn_name")
        or row.get("ccld_dvsn_name")
        or row.get("prcs_stat_name")
        or row.get("rjct_rson")
        or row.get("rjct_rson_name")
        or row.get("ord_tmd")
    )


def _order_status_from_text(value: str) -> OrderStatus:
    text = value.lower()
    if value in {"취소", "취소확인", "정정취소"} or "cancel" in text or "canceled" in text:
        return OrderStatus.CANCELED
    if value in {"거부", "주문거부"} or "reject" in text or "rejected" in text:
        return OrderStatus.REJECTED
    if value == OrderStatus.FILLED.value:
        return OrderStatus.FILLED
    if value == OrderStatus.PARTIALLY_FILLED.value:
        return OrderStatus.PARTIALLY_FILLED
    if value == OrderStatus.OPEN.value:
        return OrderStatus.OPEN
    if value == OrderStatus.ACCEPTED_BY_BROKER.value:
        return OrderStatus.ACCEPTED_BY_BROKER
    return OrderStatus.UNKNOWN


def _order_side_from_text(value: str) -> OrderSide | None:
    if value == OrderSide.BUY.value:
        return OrderSide.BUY
    if value == OrderSide.SELL.value:
        return OrderSide.SELL
    return None


def _kis_quantity(value: float) -> str:
    if not value.is_integer():
        raise ValueError("KIS domestic-stock adapter requires whole-share quantities")
    return str(int(value))


def _kis_price(value: float) -> str:
    if not value.is_integer():
        raise ValueError("KIS domestic-stock adapter requires whole-KRW limit prices")
    return str(int(value))


def _kis_overseas_quantity(value: float) -> str:
    if not value.is_integer():
        raise ValueError("KIS overseas-stock adapter requires whole-share quantities")
    return str(int(value))


def _kis_overseas_price(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _tr_cont(payload: dict[str, Any]) -> str:
    headers = _as_dict(payload.get("__headers__"))
    return str(
        headers.get("tr_cont")
        or headers.get("Tr_Cont")
        or headers.get("TR_CONT")
        or payload.get("tr_cont")
        or ""
    )


def _quote_exchange_code(exchange_code: str) -> str:
    # KIS overseas quote API uses shorter exchange codes than trading/account
    # APIs for US markets.
    return {
        "NASD": "NAS",
        "NYSE": "NYS",
        "AMEX": "AMS",
    }.get(exchange_code, exchange_code)

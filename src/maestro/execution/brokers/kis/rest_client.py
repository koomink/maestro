import json
from datetime import date
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from maestro.config.models import KISConfig
from maestro.core.clock import utc_now
from maestro.core.enums import OrderSide, OrderStatus
from maestro.execution.brokers.kis.auth import KISAuthManager, KISToken
from maestro.execution.brokers.kis.client import KISReadOnlyClient
from maestro.execution.brokers.kis.models import (
    KISAccountSnapshot,
    KISBuyingPower,
    KISCashBalance,
    KISOrderSummary,
    KISPosition,
)
from maestro.execution.live_orders import (
    BrokerOrderId,
    FillEvent,
    LiveOrderClient,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusClient,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)


class UrlLibKISTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout_seconds: float = 10.0,
    ) -> dict[str, Any]:
        target_url = url
        if params:
            target_url = f"{url}?{urlencode(params)}"
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        request = Request(target_url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                response_text = response.read().decode("utf-8")
        except HTTPError as exc:
            raise ValueError(f"KIS request failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise ValueError("KIS request failed before receiving a response") from exc
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise ValueError("KIS response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("KIS response JSON was not an object")
        return payload


class KISRestReadOnlyClient(KISReadOnlyClient):
    def __init__(
        self,
        config: KISConfig,
        *,
        transport: UrlLibKISTransport | None = None,
        auth_manager: KISAuthManager | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or UrlLibKISTransport()
        self.auth_manager = auth_manager or KISAuthManager(config, self.transport)
        self.credentials = self.auth_manager.get_credentials()

    def get_account_snapshot(self) -> KISAccountSnapshot:
        positions, cash_balance = self._fetch_balance()
        buying_power = self.get_buying_power()
        return KISAccountSnapshot(
            account_id=self.credentials.account_id,
            cash=cash_balance.cash,
            buying_power=buying_power.cash_buying_power,
            positions=positions,
            cash_balance=cash_balance,
            buying_power_detail=buying_power,
            fetched_at=utc_now(),
            source="kis_rest_readonly",
        )

    def get_positions(self) -> list[KISPosition]:
        positions, _ = self._fetch_balance()
        return positions

    def get_buying_power(self, symbol: str | None = None) -> KISBuyingPower:
        pdno = symbol or ""
        payload = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-order",
            self._tr_id(real="TTTC8908R", demo="VTTC8908R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "PDNO": pdno,
                "ORD_UNPR": "0",
                "ORD_DVSN": "01",
                "CMA_EVLU_AMT_ICLD_YN": "N",
                "OVRS_ICLD_YN": "N",
            },
        )
        output = _as_dict(payload.get("output"))
        return KISBuyingPower(
            symbol=symbol,
            order_price=_optional_float(output.get("ord_unpr")),
            cash_buying_power=_first_float(output, "nrcvb_buy_amt", "max_buy_amt", "ord_psbl_cash"),
            max_buy_quantity=_optional_first_float(output, "nrcvb_buy_qty", "max_buy_qty"),
            source="kis_rest_readonly",
        )

    def get_current_prices(self, symbols: list[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for symbol in symbols:
            if symbol == "CASH":
                prices[symbol] = 1.0
                continue
            payload = self._get(
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "FHKST01010100",
                {
                    "FID_COND_MRKT_DIV_CODE": self.config.quote_market_code,
                    "FID_INPUT_ISCD": symbol,
                },
            )
            output = _as_dict(payload.get("output"))
            prices[symbol] = _first_float(output, "stck_prpr", "ovrs_now_pric", "last")
        return prices

    def get_order_fills(self) -> list[KISOrderSummary]:
        return self._fetch_order_summaries(ccld_dvsn="00")

    def get_unfilled_orders(self) -> list[KISOrderSummary]:
        return self._fetch_order_summaries(ccld_dvsn="02")

    def _fetch_balance(self) -> tuple[list[KISPosition], KISCashBalance]:
        payload = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            self._tr_id(real="TTTC8434R", demo="VTTC8434R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "01",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        position_rows = _as_list(payload.get("output1"))
        summary = _first_item(payload.get("output2"))
        positions = [
            KISPosition(
                symbol=str(row.get("pdno") or row.get("prdt_code") or ""),
                name=_optional_str(row.get("prdt_name")),
                quantity=_first_float(row, "hldg_qty", "ord_psbl_qty"),
                average_price=_first_float(row, "pchs_avg_pric", "avg_unpr"),
                current_price=_position_current_price(row),
                unrealized_pnl=_optional_first_float(row, "evlu_pfls_amt", "evlu_pfls_rt"),
            )
            for row in position_rows
            if _first_float(row, "hldg_qty", "ord_psbl_qty", default=0.0) > 0
        ]
        cash = _first_float(summary, "dnca_tot_amt", "prvs_rcdl_excc_amt", "nxdy_excc_amt")
        cash_balance = KISCashBalance(
            cash=cash,
            total_asset_value=_optional_first_float(summary, "tot_evlu_amt", "nass_amt"),
            withdrawable_cash=_optional_first_float(summary, "dnca_tot_amt", "nxdy_excc_amt"),
        )
        return positions, cash_balance

    def _fetch_order_summaries(self, ccld_dvsn: str) -> list[KISOrderSummary]:
        today = date.today().strftime("%Y%m%d")
        payload = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            self._tr_id(real="TTTC0081R", demo="VTTC0081R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "INQR_STRT_DT": today,
                "INQR_END_DT": today,
                "SLL_BUY_DVSN_CD": "00",
                "PDNO": "",
                "CCLD_DVSN": ccld_dvsn,
                "INQR_DVSN": "00",
                "INQR_DVSN_3": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "EXCG_ID_DVSN_CD": "KRX",
            },
        )
        return [_order_summary(row) for row in _as_list(payload.get("output1"))]

    def _get(self, path: str, tr_id: str, params: dict[str, str]) -> dict[str, Any]:
        token = self.auth_manager.get_access_token()
        payload = self.transport.request(
            "GET",
            f"{self.config.resolved_base_url()}{path}",
            headers=self._headers(tr_id, token),
            params=params,
            timeout_seconds=self.config.timeout_seconds,
        )
        if payload.get("rt_cd") not in ("0", None):
            msg_cd = payload.get("msg_cd", "unknown")
            msg1 = payload.get("msg1", "KIS request failed")
            raise ValueError(f"KIS read-only request failed: {msg_cd} {msg1}")
        return payload

    def _headers(self, tr_id: str, token: KISToken) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "text/plain",
            "charset": "UTF-8",
            "authorization": f"Bearer {token.access_token}",
            "appkey": self.credentials.app_key,
            "appsecret": self.credentials.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
            "tr_cont": "",
        }

    def _tr_id(self, *, real: str, demo: str) -> str:
        return demo if self.config.paper_trading else real


class KISRestLiveOrderClient(KISRestReadOnlyClient, LiveOrderClient, LiveOrderStatusClient):
    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        payload = self._post(
            "/uapi/domestic-stock/v1/trading/order-cash",
            self._order_tr_id(request.side),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "PDNO": request.symbol,
                "ORD_DVSN": "00",
                "ORD_QTY": _kis_quantity(request.quantity),
                "ORD_UNPR": _kis_price(request.limit_price),
            },
        )
        output = _as_dict(payload.get("output"))
        broker_order_id = _optional_str(output.get("ODNO") or output.get("odno"))
        broker_order_org_no = _optional_str(
            output.get("KRX_FWDG_ORD_ORGNO") or output.get("krx_fwdg_ord_orgno")
        )
        broker_order = None
        if broker_order_id:
            broker_order = BrokerOrderId(
                broker="kis",
                broker_order_id=broker_order_id,
                broker_order_org_no=broker_order_org_no,
                order_id=request.order_id,
                submitted_at=utc_now().isoformat(),
            )
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER if broker_order else OrderStatus.UNKNOWN,
            broker_order=broker_order,
            message=_optional_str(payload.get("msg1")),
            raw=payload,
        )

    def _post(self, path: str, tr_id: str, json_body: dict[str, str]) -> dict[str, Any]:
        token = self.auth_manager.get_access_token()
        payload = self.transport.request(
            "POST",
            f"{self.config.resolved_base_url()}{path}",
            headers=self._headers(tr_id, token),
            json_body=json_body,
            timeout_seconds=self.config.timeout_seconds,
        )
        if payload.get("rt_cd") not in ("0", None):
            msg_cd = payload.get("msg_cd", "unknown")
            msg1 = payload.get("msg1", "KIS request failed")
            raise ValueError(f"KIS live order request failed: {msg_cd} {msg1}")
        return payload

    def _order_tr_id(self, side: OrderSide) -> str:
        if side == OrderSide.BUY:
            return self._tr_id(real="TTTC0802U", demo="VTTC0802U")
        return self._tr_id(real="TTTC0801U", demo="VTTC0801U")

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        matched = self._find_order_summary(broker_order_id.broker_order_id)
        if matched is None:
            return _unknown_status_snapshot(
                broker_order_id,
                message="KIS order status was not found in daily or unfilled order inquiry.",
            )
        return _status_snapshot_from_summary(broker_order_id, matched)

    def _find_order_summary(self, order_id: str) -> KISOrderSummary | None:
        summaries = [*self.get_unfilled_orders(), *self.get_order_fills()]
        for summary in summaries:
            if summary.order_id == order_id:
                return summary
        return None


# TODO(v0.7): Add a KIS LiveOrderCancelClient only after the domestic-stock
# cancel endpoint path, TR_IDs, and body fields are verified against project
# references. Cancellation remains available through injected fake clients and
# LiveOrderCancellationService policy tests only.


def _order_summary(row: dict[str, Any]) -> KISOrderSummary:
    return KISOrderSummary(
        order_id=str(row.get("odno") or row.get("orgn_odno") or ""),
        symbol=str(row.get("pdno") or ""),
        name=_optional_str(row.get("prdt_name")),
        side=_side(row.get("sll_buy_dvsn_cd") or row.get("sll_buy_dvsn_cd_name")),
        quantity=_first_float(row, "ord_qty", "tot_ccld_qty"),
        filled_quantity=_first_float(row, "tot_ccld_qty", "ccld_qty", default=0.0),
        average_fill_price=_optional_first_float(row, "avg_prvs", "ord_unpr"),
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
    ordered = _first_float(row, "ord_qty", default=0.0)
    filled = _first_float(row, "tot_ccld_qty", "ccld_qty", default=0.0)
    if ordered > 0 and filled >= ordered:
        return OrderStatus.FILLED
    if filled > 0:
        return OrderStatus.PARTIALLY_FILLED
    return OrderStatus.OPEN


def _raw_order_status(row: dict[str, Any]) -> str | None:
    return _optional_str(
        row.get("ord_dvsn_name")
        or row.get("ccld_dvsn_name")
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
        raise ValueError("KIS domestic-stock live orders require whole-share quantities")
    return str(int(value))


def _kis_price(value: float) -> str:
    if not value.is_integer():
        raise ValueError("KIS domestic-stock live orders require whole-KRW limit prices")
    return str(int(value))

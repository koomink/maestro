from decimal import Decimal

from maestro.core.clock import utc_now
from maestro.core.enums import BrokerProduct, OrderSide, OrderStatus
from maestro.execution.brokers.kis.base import KISAPIResponseError
from maestro.execution.brokers.kis.domestic_readonly import KISRestDomesticStockReadOnlyClient
from maestro.execution.brokers.kis.models import KISBuyingPower, KISOrderSummary
from maestro.execution.brokers.kis.parsers import (
    _as_dict,
    _as_list,
    _kis_price,
    _kis_quantity,
    _optional_str,
    _status_snapshot_from_summary,
    _unknown_status_snapshot,
)
from maestro.execution.live_order_errors import BrokerOrderRejectedError
from maestro.execution.live_order_models import (
    BrokerOrderId,
    LiveOrderModifyRequest,
    LiveOrderModifyResult,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusSnapshot,
)
from maestro.execution.live_order_ports import (
    LiveOrderClient,
    LiveOrderModifyClient,
    LiveOrderPreSubmitValidator,
    LiveOrderStatusClient,
)


class KISRestDomesticStockLiveOrderClient(
    KISRestDomesticStockReadOnlyClient,
    LiveOrderClient,
    LiveOrderStatusClient,
    LiveOrderPreSubmitValidator,
    LiveOrderModifyClient,
):
    post_error_context = "KIS live order request"

    def validate_pre_submit_order(self, request: LiveOrderRequest) -> None:
        if request.side != OrderSide.BUY:
            return
        buying_power = self.get_buying_power(
            self._broker_symbol(request.symbol), request.limit_price
        )
        _validate_buying_power(request, buying_power)

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        try:
            payload = self._post(
                "/uapi/domestic-stock/v1/trading/order-cash",
                self._order_tr_id(request.side),
                {
                    "CANO": self.credentials.cano,
                    "ACNT_PRDT_CD": self.credentials.account_product_code,
                    "PDNO": self._broker_symbol(request.symbol),
                    "ORD_DVSN": "00",
                    "ORD_QTY": _kis_quantity(request.quantity),
                    "ORD_UNPR": _kis_price(request.limit_price),
                },
            )
        except KISAPIResponseError as exc:
            raise BrokerOrderRejectedError("kis", exc.code, exc.message) from exc
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
                account_id=request.account_id,
                broker_product=BrokerProduct.KIS_DOMESTIC_STOCK,
            )
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER if broker_order else OrderStatus.UNKNOWN,
            broker_order=broker_order,
            message=_optional_str(payload.get("msg1")),
            raw=payload,
        )

    def _order_tr_id(self, side: OrderSide) -> str:
        if side == OrderSide.BUY:
            return self._tr_id(real="TTTC0012U", demo="VTTC0012U")
        return self._tr_id(real="TTTC0011U", demo="VTTC0011U")

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

    def modify_order(self, request: LiveOrderModifyRequest) -> LiveOrderModifyResult:
        instrument = self.instruments.get(request.symbol)
        if instrument is not None:
            price_steps = Decimal(str(request.limit_price)) / Decimal(
                str(instrument.price_tick)
            )
            if price_steps != price_steps.to_integral_value():
                raise ValueError("KIS domestic modification price does not match price tick")
        row = self._modifiable_order(request.broker_order.broker_order_id)
        if row is None:
            raise ValueError("KIS domestic modification requires a modifiable open order")
        possible_quantity = float(row.get("psbl_qty") or 0.0)
        quantity = request.quantity if request.quantity is not None else possible_quantity
        if quantity <= 0 or quantity > possible_quantity + 1e-9:
            raise ValueError(
                "KIS domestic modification quantity exceeds modifiable quantity: "
                f"quantity={quantity} psbl_qty={possible_quantity}"
            )
        if instrument is not None:
            quantity_steps = Decimal(str(quantity)) / Decimal(str(instrument.quantity_step))
            if quantity_steps != quantity_steps.to_integral_value():
                raise ValueError("KIS domestic modification quantity does not match quantity step")
        org_no = request.broker_order.broker_order_org_no or str(
            row.get("ord_gno_brno") or ""
        )
        if not org_no:
            raise ValueError("KIS domestic modification requires broker order organization number")
        payload = self._post(
            "/uapi/domestic-stock/v1/trading/order-rvsecncl",
            self._tr_id(real="TTTC0013U", demo="VTTC0013U"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "KRX_FWDG_ORD_ORGNO": org_no,
                "ORGN_ODNO": request.broker_order.broker_order_id,
                "ORD_DVSN": "00",
                "RVSE_CNCL_DVSN_CD": "01",
                "ORD_QTY": _kis_quantity(quantity),
                "ORD_UNPR": _kis_price(request.limit_price),
                "QTY_ALL_ORD_YN": "Y" if request.quantity is None else "N",
                "EXCG_ID_DVSN_CD": str(row.get("excg_id_dvsn_cd") or "KRX"),
            },
        )
        output = _as_dict(payload.get("output"))
        replacement_id = _optional_str(output.get("ODNO") or output.get("odno"))
        if replacement_id is None:
            raise ValueError("KIS domestic modification response is missing order number")
        replacement_org_no = _optional_str(
            output.get("KRX_FWDG_ORD_ORGNO")
            or output.get("krx_fwdg_ord_orgno")
            or org_no
        )
        replacement = BrokerOrderId(
            broker="kis",
            broker_order_id=replacement_id,
            broker_order_org_no=replacement_org_no,
            order_id=request.broker_order.order_id,
            submitted_at=utc_now().isoformat(),
            account_id=request.broker_order.account_id,
            broker_product=BrokerProduct.KIS_DOMESTIC_STOCK,
            parent_broker_order_id=request.broker_order.broker_order_id,
        )
        return LiveOrderModifyResult(
            broker_order=replacement,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            previous_broker_order=request.broker_order,
            message=_optional_str(payload.get("msg1")),
        )

    def _modifiable_order(self, broker_order_id: str) -> dict | None:
        payloads = self._get_pages(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            self._tr_id(real="TTTC0084R", demo="VTTC0084R"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "INQR_DVSN_1": "0",
                "INQR_DVSN_2": "0",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        for payload in payloads:
            rows = (
                payload.get("output1")
                if payload.get("output1") is not None
                else payload.get("output")
            )
            for row in _as_list(rows):
                if str(row.get("odno") or row.get("orgn_odno") or "") == broker_order_id:
                    return row
        return None

    def _broker_symbol(self, canonical_symbol: str) -> str:
        instrument = self.instruments.get(canonical_symbol)
        return instrument.broker_symbol if instrument else canonical_symbol


def _validate_buying_power(request: LiveOrderRequest, buying_power: KISBuyingPower) -> None:
    if request.notional > buying_power.cash_buying_power + 1e-9:
        raise ValueError(
            "KIS buying power is below requested live order notional: "
            f"symbol={request.symbol} notional={request.notional:.2f} "
            f"cash_buying_power={buying_power.cash_buying_power:.2f}"
        )
    if buying_power.max_buy_quantity is None:
        return
    if request.quantity > buying_power.max_buy_quantity + 1e-9:
        raise ValueError(
            "KIS max buy quantity is below requested live order quantity: "
            f"symbol={request.symbol} quantity={request.quantity} "
            f"max_buy_quantity={buying_power.max_buy_quantity}"
        )


__all__ = ["KISRestDomesticStockLiveOrderClient"]

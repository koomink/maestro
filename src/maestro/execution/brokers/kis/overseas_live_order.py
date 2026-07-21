from maestro.core.clock import utc_now
from maestro.core.enums import BrokerProduct, OrderSide, OrderStatus
from maestro.execution.brokers.kis.models import KISBuyingPower, KISOrderSummary
from maestro.execution.brokers.kis.overseas_readonly import (
    KISRestOverseasStockReadOnlyClient,
    overseas_order_status_date_range,
)
from maestro.execution.brokers.kis.parsers import (
    _as_dict,
    _kis_overseas_price,
    _kis_overseas_quantity,
    _optional_str,
    _status_snapshot_from_summary,
    _unknown_status_snapshot,
)
from maestro.execution.live_order_models import (
    BrokerOrderId,
    LiveOrderCancelRequest,
    LiveOrderCancelResult,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusSnapshot,
)
from maestro.execution.live_order_ports import (
    LiveOrderCancelClient,
    LiveOrderClient,
    LiveOrderPreSubmitValidator,
    LiveOrderStatusClient,
)


class KISRestOverseasStockLiveOrderClient(
    KISRestOverseasStockReadOnlyClient,
    LiveOrderClient,
    LiveOrderStatusClient,
    LiveOrderCancelClient,
    LiveOrderPreSubmitValidator,
):
    post_error_context = "KIS overseas live order request"

    def validate_pre_submit_order(self, request: LiveOrderRequest) -> None:
        if request.side != OrderSide.BUY:
            return
        buying_power = self.get_buying_power(request.symbol, request.limit_price)
        _validate_buying_power(request, buying_power)

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        instrument = self._instrument(request.symbol)
        payload = self._post(
            "/uapi/overseas-stock/v1/trading/order",
            self._order_tr_id(request.side, str(instrument.exchange_code or "")),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "OVRS_EXCG_CD": str(instrument.exchange_code or ""),
                "PDNO": instrument.broker_symbol,
                "ORD_QTY": _kis_overseas_quantity(request.quantity),
                "OVRS_ORD_UNPR": _kis_overseas_price(request.limit_price),
                "CTAC_TLNO": "",
                "MGCO_APTM_ODNO": "",
                "SLL_TYPE": "00" if request.side == OrderSide.SELL else "",
                "ORD_SVR_DVSN_CD": "0",
                "ORD_DVSN": "00",
            },
        )
        output = _as_dict(payload.get("output"))
        broker_order_id = _optional_str(output.get("ODNO") or output.get("odno"))
        broker_order_org_no = _optional_str(
            output.get("KRX_FWDG_ORD_ORGNO")
            or output.get("krx_fwdg_ord_orgno")
            or output.get("ORD_GNO_BRNO")
            or output.get("ord_gno_brno")
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
                broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
            )
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER if broker_order else OrderStatus.UNKNOWN,
            broker_order=broker_order,
            message=_optional_str(payload.get("msg1")),
            raw=payload,
        )

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        matched = self._find_order_summary(broker_order_id)
        if matched is None:
            return _unknown_status_snapshot(
                broker_order_id,
                message=(
                    "KIS overseas order status was not found in daily or unfilled order inquiry."
                ),
            )
        return _status_snapshot_from_summary(broker_order_id, matched)

    def cancel_order(self, request: LiveOrderCancelRequest) -> LiveOrderCancelResult:
        matched = self._find_order_summary(request.broker_order)
        if matched is None:
            raise ValueError("KIS overseas cancel requires a latest unfilled order summary")
        instrument = self._instrument(matched.symbol)
        remaining_quantity = max(matched.quantity - matched.filled_quantity, 0.0)
        if remaining_quantity <= 0:
            raise ValueError("KIS overseas cancel requires a remaining open quantity")
        payload = self._post(
            "/uapi/overseas-stock/v1/trading/order-rvsecncl",
            self._tr_id(real="TTTT1004U", demo="VTTT1004U"),
            {
                "CANO": self.credentials.cano,
                "ACNT_PRDT_CD": self.credentials.account_product_code,
                "OVRS_EXCG_CD": str(instrument.exchange_code or ""),
                "PDNO": instrument.broker_symbol,
                "ORGN_ODNO": request.broker_order.broker_order_id,
                "RVSE_CNCL_DVSN_CD": "02",
                "ORD_QTY": _kis_overseas_quantity(remaining_quantity),
                "OVRS_ORD_UNPR": "0",
                "MGCO_APTM_ODNO": "",
                "ORD_SVR_DVSN_CD": "0",
            },
        )
        return LiveOrderCancelResult(
            broker_order=request.broker_order,
            status=OrderStatus.CANCELED,
            canceled_quantity=remaining_quantity,
            message=_optional_str(payload.get("msg1")),
            raw=payload,
        )

    def _order_tr_id(self, side: OrderSide, exchange_code: str) -> str:
        if exchange_code not in {"NASD", "NYSE", "AMEX"}:
            raise ValueError(
                "KIS overseas live approval currently supports US exchanges only: NASD, NYSE, AMEX"
            )
        if side == OrderSide.BUY:
            return self._tr_id(real="TTTT1002U", demo="VTTT1002U")
        return self._tr_id(real="TTTT1006U", demo="VTTT1001U")

    def _find_order_summary(self, broker_order: BrokerOrderId) -> KISOrderSummary | None:
        start_date, end_date = overseas_order_status_date_range(broker_order.submitted_at)
        summaries = [
            *self.get_unfilled_orders(),
            *self._fetch_order_summaries(
                ccld_nccs_dvsn="00",
                start_date=start_date,
                end_date=end_date,
            ),
        ]
        for summary in summaries:
            if summary.order_id == broker_order.broker_order_id:
                return summary
        return None


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


__all__ = ["KISRestOverseasStockLiveOrderClient"]

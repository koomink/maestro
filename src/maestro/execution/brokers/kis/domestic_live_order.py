from typing import Any

from maestro.core.clock import utc_now
from maestro.core.enums import BrokerProduct, OrderSide, OrderStatus
from maestro.execution.brokers.kis.domestic_readonly import KISRestDomesticStockReadOnlyClient
from maestro.execution.brokers.kis.models import KISOrderSummary
from maestro.execution.brokers.kis.parsers import (
    _as_dict,
    _kis_price,
    _kis_quantity,
    _optional_str,
    _status_snapshot_from_summary,
    _unknown_status_snapshot,
)
from maestro.execution.live_order_models import (
    BrokerOrderId,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusSnapshot,
)
from maestro.execution.live_order_ports import LiveOrderClient, LiveOrderStatusClient


class KISRestDomesticStockLiveOrderClient(
    KISRestDomesticStockReadOnlyClient,
    LiveOrderClient,
    LiveOrderStatusClient,
):
    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
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
                broker_product=BrokerProduct.KIS_DOMESTIC_STOCK,
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

    def _broker_symbol(self, canonical_symbol: str) -> str:
        instrument = self.instruments.get(canonical_symbol)
        return instrument.broker_symbol if instrument else canonical_symbol


__all__ = ["KISRestDomesticStockLiveOrderClient"]

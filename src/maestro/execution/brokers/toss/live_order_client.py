import hashlib
import re
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from maestro.config.broker import BrokerAccountConfig
from maestro.core.clock import utc_now
from maestro.core.enums import Currency, OrderSide, OrderStatus, OrderType
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.toss.auth import TossAuthManager
from maestro.execution.brokers.toss.readonly_client import _resolve_account_seq
from maestro.execution.brokers.toss.transport import TossRestTransport
from maestro.execution.live_order_models import (
    BrokerOrderId,
    BrokerOrderRequest,
    FillEvent,
    LiveOrderCancelRequest,
    LiveOrderCancelResult,
    LiveOrderModifyRequest,
    LiveOrderModifyResult,
    LiveOrderRequest,
    LiveOrderResult,
    LiveOrderStatusSnapshot,
    PartialFillSummary,
)
from maestro.execution.live_order_ports import (
    LiveOrderCancelClient,
    LiveOrderClient,
    LiveOrderModifyClient,
    LiveOrderPreSubmitValidator,
    LiveOrderStatusClient,
)


class TossLiveOrderClient(
    LiveOrderClient,
    LiveOrderStatusClient,
    LiveOrderModifyClient,
    LiveOrderCancelClient,
    LiveOrderPreSubmitValidator,
):
    def __init__(
        self,
        config: BrokerAccountConfig,
        instruments: list[TradableInstrument] | None = None,
        *,
        transport: Any | None = None,
    ) -> None:
        if config.broker != "toss":
            raise ValueError(f"Account {config.id} is not a Toss account")
        self.config = config
        self.account_seq = _resolve_account_seq(config)
        self.instruments = {instrument.symbol: instrument for instrument in instruments or []}
        self._broker_symbol_to_canonical: dict[str, str] = {}
        for instrument in instruments or []:
            try:
                broker_symbol = instrument.symbol_for_broker("toss")
            except Exception:
                continue
            self._broker_symbol_to_canonical[broker_symbol] = instrument.symbol
        self.transport = transport or TossRestTransport(
            base_url=config.base_url or "https://openapi.tossinvest.com",
            access_token_provider=TossAuthManager(config).access_token,
            timeout_seconds=config.timeout_seconds,
        )

    def validate_pre_submit_order(self, request: LiveOrderRequest) -> None:
        if request.side != OrderSide.BUY:
            return
        if request.currency is None:
            raise ValueError("Toss buy order requires currency for buying-power validation")
        response = self.transport.get(
            "/api/v1/buying-power",
            {"currency": request.currency.value},
            account_seq=self.account_seq,
        )
        buying_power = _float(_result_dict(response).get("cashBuyingPower"))
        if request.notional > buying_power + 1e-9:
            raise ValueError(
                "Toss buying power is below requested live order notional: "
                f"notional={request.notional:.2f} cash_buying_power={buying_power:.2f}"
            )

    def submit_limit_order(self, request: LiveOrderRequest) -> LiveOrderResult:
        return self.submit_order(
            BrokerOrderRequest(
                order_id=request.order_id,
                symbol=request.symbol,
                side=request.side,
                order_type=OrderType.LIMIT,
                quantity=request.quantity,
                limit_price=request.limit_price,
                currency=request.currency,
                account_id=request.account_id,
            ),
            signal_run_id=request.signal_run_id,
        )

    def submit_order(
        self,
        request: BrokerOrderRequest,
        *,
        signal_run_id: str | None = None,
    ) -> LiveOrderResult:
        payload = {
            "clientOrderId": _client_order_id(request.order_id),
            "symbol": self._broker_symbol(request.symbol),
            "side": request.side.value.upper(),
            "orderType": request.order_type.value.upper(),
        }
        if request.time_in_force:
            payload["timeInForce"] = request.time_in_force
        if request.quantity is not None:
            payload["quantity"] = _integer_quantity(request.quantity)
        if request.order_amount is not None:
            payload["orderAmount"] = _decimal_string(request.order_amount)
        if request.limit_price is not None:
            payload["price"] = _decimal_string(request.limit_price)
        response = self.transport.post(
            "/api/v1/orders",
            payload,
            account_seq=self.account_seq,
        )
        result = _result_dict(response)
        broker_order_id = str(result.get("orderId") or "")
        broker_order = (
            self._broker_order(request.order_id, broker_order_id)
            if broker_order_id
            else None
        )
        return LiveOrderResult(
            order_id=request.order_id,
            status=OrderStatus.ACCEPTED_BY_BROKER if broker_order else OrderStatus.UNKNOWN,
            broker_order=broker_order,
            raw=response,
            signal_run_id=signal_run_id,
        )

    def get_order_status(self, broker_order_id: BrokerOrderId) -> LiveOrderStatusSnapshot:
        response = self.transport.get(
            f"/api/v1/orders/{broker_order_id.broker_order_id}",
            account_seq=self.account_seq,
        )
        return _status_snapshot(
            broker_order_id,
            _result_dict(response),
            response,
            canonical_symbol=self._canonical_symbol,
        )

    def get_open_orders(self, symbol: str | None = None) -> list[LiveOrderStatusSnapshot]:
        params = {"status": "OPEN"}
        if symbol:
            params["symbol"] = symbol
        response = self.transport.get(
            "/api/v1/orders",
            params,
            account_seq=self.account_seq,
        )
        result = _result_dict(response)
        output = []
        for order in result.get("orders", []):
            if not isinstance(order, dict):
                continue
            broker_order = self._broker_order("", str(order.get("orderId") or ""))
            output.append(
                _status_snapshot(
                    broker_order,
                    order,
                    {"result": order},
                    canonical_symbol=self._canonical_symbol,
                )
            )
        return output

    def modify_order(self, request: LiveOrderModifyRequest) -> LiveOrderModifyResult:
        payload: dict[str, Any] = {
            "orderType": "LIMIT",
            "price": _decimal_string(request.limit_price),
        }
        if request.currency == Currency.KRW:
            if request.quantity is None:
                raise ValueError("Toss KR order modification requires quantity")
            payload["quantity"] = _integer_quantity(request.quantity)
        elif request.quantity is not None:
            raise ValueError("Toss US order modification supports price only")
        response = self.transport.post(
            f"/api/v1/orders/{request.broker_order.broker_order_id}/modify",
            payload,
            account_seq=self.account_seq,
        )
        replacement_id = str(_result_dict(response).get("orderId") or "")
        if not replacement_id:
            raise ValueError("Toss modify response is missing orderId")
        replacement = self._broker_order(
            request.broker_order.order_id,
            replacement_id,
            parent_broker_order_id=request.broker_order.broker_order_id,
        )
        return LiveOrderModifyResult(
            broker_order=replacement,
            previous_broker_order=request.broker_order,
            status=OrderStatus.ACCEPTED_BY_BROKER,
            raw=response,
        )

    def cancel_order(self, request: LiveOrderCancelRequest) -> LiveOrderCancelResult:
        response = self.transport.post(
            f"/api/v1/orders/{request.broker_order.broker_order_id}/cancel",
            {},
            account_seq=self.account_seq,
        )
        cancellation_id = str(_result_dict(response).get("orderId") or "")
        if not cancellation_id:
            raise ValueError("Toss cancel response is missing orderId")
        cancellation = self._broker_order(
            request.broker_order.order_id,
            cancellation_id,
            parent_broker_order_id=request.broker_order.broker_order_id,
        )
        return LiveOrderCancelResult(
            broker_order=cancellation,
            status=OrderStatus.CANCELED,
            canceled_quantity=0.0,
            raw=response,
        )

    def _broker_order(
        self,
        order_id: str,
        broker_order_id: str,
        *,
        parent_broker_order_id: str | None = None,
    ) -> BrokerOrderId:
        return BrokerOrderId(
            broker="toss",
            broker_order_id=broker_order_id,
            order_id=order_id,
            submitted_at=utc_now().isoformat(),
            account_id=self.config.id,
            parent_broker_order_id=parent_broker_order_id,
        )

    def _broker_symbol(self, canonical_symbol: str) -> str:
        instrument = self.instruments.get(canonical_symbol)
        return (
            instrument.symbol_for_broker("toss")
            if instrument is not None
            else canonical_symbol
        )

    def _canonical_symbol(self, broker_symbol: str) -> str:
        return self._broker_symbol_to_canonical.get(broker_symbol, broker_symbol)


def _status_snapshot(
    broker_order: BrokerOrderId,
    order: dict[str, Any],
    raw: dict[str, Any],
    *,
    canonical_symbol: Callable[[str], str] = lambda symbol: symbol,
) -> LiveOrderStatusSnapshot:
    symbol = canonical_symbol(str(order.get("symbol") or ""))
    raw_status = str(order.get("status") or "")
    status = {
        "PENDING": OrderStatus.OPEN,
        "PENDING_CANCEL": OrderStatus.OPEN,
        "PENDING_REPLACE": OrderStatus.OPEN,
        "PARTIAL_FILLED": OrderStatus.PARTIALLY_FILLED,
        "FILLED": OrderStatus.FILLED,
        "CANCELED": OrderStatus.CANCELED,
        "REJECTED": OrderStatus.REJECTED,
        "CANCEL_REJECTED": OrderStatus.REJECTED,
        "REPLACE_REJECTED": OrderStatus.REJECTED,
        "REPLACED": OrderStatus.CANCELED,
    }.get(raw_status, OrderStatus.UNKNOWN)
    execution = order.get("execution")
    execution = execution if isinstance(execution, dict) else {}
    ordered_quantity = _float(order.get("quantity"))
    filled_quantity = _float(execution.get("filledQuantity"))
    average_price = _optional_float(execution.get("averageFilledPrice"))
    filled_at = str(execution.get("filledAt") or order.get("orderedAt") or utc_now().isoformat())
    fills = []
    if filled_quantity > 0 and average_price is not None:
        fills.append(
            FillEvent(
                broker_order_id=broker_order.broker_order_id,
                symbol=symbol,
                quantity=filled_quantity,
                price=average_price,
                filled_at=filled_at,
                raw=execution,
            )
        )
    side_value = str(order.get("side") or "").lower()
    side = OrderSide(side_value) if side_value in {"buy", "sell"} else None
    return LiveOrderStatusSnapshot(
        broker_order=broker_order,
        status=status,
        checked_at=utc_now().isoformat(),
        symbol=symbol or None,
        side=side,
        partial_fill=PartialFillSummary(
            ordered_quantity=ordered_quantity,
            filled_quantity=filled_quantity,
            remaining_quantity=max(ordered_quantity - filled_quantity, 0.0),
            average_fill_price=average_price,
            fill_count=1 if fills else 0,
        ),
        fills=fills,
        raw_status=raw_status or None,
        raw=raw,
    )


def _client_order_id(order_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]", "_", order_id)
    if len(normalized) <= 36:
        return normalized
    digest = hashlib.sha256(order_id.encode("utf-8")).hexdigest()[:12]
    return f"{normalized[:23]}-{digest}"


def _integer_quantity(value: float) -> str:
    if abs(value - round(value)) > 1e-9:
        raise ValueError("Toss quantity orders require an integer quantity")
    return str(int(round(value)))


def _decimal_string(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")


def _result_dict(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def _float(value: Any) -> float:
    return float(str(value or "0"))


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    return float(str(value))


__all__ = ["TossLiveOrderClient"]

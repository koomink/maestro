import pytest

from maestro.config.broker import BrokerAccountConfig
from maestro.core.enums import (
    AssetType,
    BrokerProduct,
    Currency,
    ExchangeCode,
    MarketRegion,
    OrderSide,
    OrderStatus,
    OrderType,
)
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.toss.live_order_client import TossLiveOrderClient
from maestro.execution.brokers.toss.transport import TossTransportError
from maestro.execution.live_order_errors import BrokerOrderRejectedError
from maestro.execution.live_order_models import (
    BrokerOrderRequest,
    LiveOrderCancelRequest,
    LiveOrderModifyRequest,
    LiveOrderRequest,
)


def test_toss_limit_order_uses_account_header_and_deterministic_client_order_id():
    transport = RecordingTransport(
        {
            ("POST", "/api/v1/orders"): {
                "result": {"orderId": "TOSS-1", "clientOrderId": "ord_live_1"}
            }
        }
    )
    client = TossLiveOrderClient(_account(), transport=transport)

    result = client.submit_limit_order(_live_request())

    assert result.status == OrderStatus.ACCEPTED_BY_BROKER
    assert result.broker_order.broker_order_id == "TOSS-1"
    assert transport.calls == [
        (
            "POST",
            "/api/v1/orders",
            {
                "clientOrderId": "ord_live_1",
                "symbol": "AAPL",
                "side": "BUY",
                "orderType": "LIMIT",
                "timeInForce": "DAY",
                "quantity": "2",
                "price": "185.5",
            },
            7,
        )
    ]


def test_toss_definitive_http_rejection_preserves_broker_evidence():
    transport = RecordingTransport(
        {
            ("POST", "/api/v1/orders"): TossTransportError(
                "위험고지 등록이 필요합니다",
                status_code=422,
                error_code="prerequisite-required",
                request_id="req-422",
                data={"prerequisite": "risk-disclosure"},
            )
        }
    )
    client = TossLiveOrderClient(_account(), transport=transport)

    with pytest.raises(BrokerOrderRejectedError) as exc_info:
        client.submit_limit_order(_live_request())

    assert exc_info.value.code == "prerequisite-required"
    assert exc_info.value.status_code == 422
    assert exc_info.value.request_id == "req-422"
    assert exc_info.value.data == {"prerequisite": "risk-disclosure"}


@pytest.mark.parametrize("status_code", [409, 500])
def test_toss_ambiguous_http_failure_is_not_converted_to_rejection(status_code):
    error = TossTransportError("ambiguous", status_code=status_code)
    transport = RecordingTransport({("POST", "/api/v1/orders"): error})
    client = TossLiveOrderClient(_account(), transport=transport)

    with pytest.raises(TossTransportError) as exc_info:
        client.submit_limit_order(_live_request())

    assert exc_info.value is error


def test_toss_adapter_supports_us_amount_market_order():
    transport = RecordingTransport(
        {
            ("POST", "/api/v1/orders"): {
                "result": {"orderId": "TOSS-AMOUNT", "clientOrderId": "ord_amount"}
            }
        }
    )
    client = TossLiveOrderClient(_account(), transport=transport)

    result = client.submit_order(
        BrokerOrderRequest(
            order_id="ord_amount",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            order_amount=100.5,
            currency=Currency.USD,
            account_id="toss_brokerage",
        )
    )

    assert result.broker_order.broker_order_id == "TOSS-AMOUNT"
    assert transport.calls[0][2]["orderAmount"] == "100.5"
    assert "quantity" not in transport.calls[0][2]


def test_toss_order_status_normalizes_partial_fill():
    transport = RecordingTransport(
        {
            ("GET", "/api/v1/orders/TOSS-1"): {
                "result": {
                    "orderId": "TOSS-1",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "status": "PARTIAL_FILLED",
                    "quantity": "2",
                    "currency": "USD",
                    "orderedAt": "2026-06-19T09:30:00+09:00",
                    "execution": {
                        "filledQuantity": "1",
                        "averageFilledPrice": "185.25",
                        "filledAmount": "185.25",
                        "commission": "0.18525",
                        "tax": "0",
                        "filledAt": "2026-06-19T09:31:00+09:00",
                        "settlementDate": "2026-06-20",
                    },
                }
            }
        }
    )
    client = TossLiveOrderClient(_account(), transport=transport)
    broker_order = client._broker_order("ord_live_1", "TOSS-1")

    status = client.get_order_status(broker_order)

    assert status.status == OrderStatus.PARTIALLY_FILLED
    assert status.partial_fill.filled_quantity == 1.0
    assert status.partial_fill.remaining_quantity == 1.0
    assert status.partial_fill.average_fill_price == 185.25
    assert status.currency == "USD"
    assert status.cumulative_filled_amount == 185.25
    assert status.cumulative_commission == 0.18525
    assert status.cumulative_tax == 0.0
    assert status.settlement_date == "2026-06-20"


def test_toss_order_status_maps_broker_symbol_to_canonical():
    transport = RecordingTransport(
        {
            ("GET", "/api/v1/orders/TOSS-1"): {
                "result": {
                    "orderId": "TOSS-1",
                    "symbol": "TOSS_AAPL",
                    "side": "BUY",
                    "status": "FILLED",
                    "quantity": "2",
                    "orderedAt": "2026-06-19T09:30:00+09:00",
                    "execution": {
                        "filledQuantity": "2",
                        "averageFilledPrice": "185.25",
                        "filledAmount": "370.5",
                        "filledAt": "2026-06-19T09:31:00+09:00",
                    },
                }
            }
        }
    )
    client = TossLiveOrderClient(_account(), instruments=[_instrument()], transport=transport)

    status = client.get_order_status(client._broker_order("ord_live_1", "TOSS-1"))

    assert status.symbol == "AAPL"
    assert status.fills[0].symbol == "AAPL"


def test_toss_order_status_keeps_unmapped_symbol():
    transport = RecordingTransport(
        {
            ("GET", "/api/v1/orders/TOSS-1"): {
                "result": {
                    "orderId": "TOSS-1",
                    "symbol": "UNMAPPED",
                    "side": "BUY",
                    "status": "FILLED",
                    "quantity": "1",
                    "execution": {
                        "filledQuantity": "1",
                        "averageFilledPrice": "10",
                        "filledAmount": "10",
                    },
                }
            }
        }
    )
    client = TossLiveOrderClient(_account(), instruments=[_instrument()], transport=transport)

    status = client.get_order_status(client._broker_order("ord_live_1", "TOSS-1"))

    assert status.symbol == "UNMAPPED"
    assert status.fills[0].symbol == "UNMAPPED"


def test_toss_modify_and_cancel_track_replacement_order_ids():
    transport = RecordingTransport(
        {
            ("POST", "/api/v1/orders/TOSS-1/modify"): {
                "result": {"orderId": "TOSS-2"}
            },
            ("POST", "/api/v1/orders/TOSS-2/cancel"): {
                "result": {"orderId": "TOSS-3"}
            },
        }
    )
    client = TossLiveOrderClient(_account(), transport=transport)
    original = client._broker_order("ord_live_1", "TOSS-1")

    modified = client.modify_order(
        LiveOrderModifyRequest(
            run_id="run_1",
            approval_id="appr_modify",
            broker_order=original,
            symbol="AAPL",
            limit_price=186.0,
            currency=Currency.USD,
        )
    )
    canceled = client.cancel_order(
        LiveOrderCancelRequest(
            run_id="run_1",
            approval_id="appr_cancel",
            broker_order=modified.broker_order,
        )
    )

    assert modified.broker_order.broker_order_id == "TOSS-2"
    assert modified.broker_order.parent_broker_order_id == "TOSS-1"
    assert canceled.broker_order.broker_order_id == "TOSS-3"
    assert canceled.broker_order.parent_broker_order_id == "TOSS-2"


def test_toss_unknown_status_maps_to_unknown():
    transport = RecordingTransport(
        {
            ("GET", "/api/v1/orders/TOSS-1"): {
                "result": {
                    "orderId": "TOSS-1",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "status": "NEW_FUTURE_STATUS",
                    "quantity": "2",
                    "execution": {"filledQuantity": "0"},
                }
            }
        }
    )
    client = TossLiveOrderClient(_account(), transport=transport)

    status = client.get_order_status(client._broker_order("ord_live_1", "TOSS-1"))

    assert status.status == OrderStatus.UNKNOWN
    assert status.raw_status == "NEW_FUTURE_STATUS"


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("FILLED", OrderStatus.FILLED),
        ("PARTIAL_FILLED", OrderStatus.PARTIALLY_FILLED),
        ("CANCELED", OrderStatus.CANCELED),
        ("REJECTED", OrderStatus.REJECTED),
    ],
)
def test_toss_order_history_normalizes_terminal_and_partial_statuses(
    raw_status,
    expected,
):
    transport = RecordingTransport(
        {
            ("GET", "/api/v1/orders"): {
                "result": {
                    "orders": [
                        {
                            "orderId": "TOSS-HISTORY-1",
                            "symbol": "AAPL",
                            "side": "BUY",
                            "status": raw_status,
                            "quantity": "2",
                            "price": "185.5",
                            "orderedAt": "2026-07-31T22:30:00+09:00",
                            "execution": {
                                "filledQuantity": (
                                    "2" if raw_status == "FILLED" else "1"
                                    if raw_status == "PARTIAL_FILLED"
                                    else "0"
                                ),
                                "averageFilledPrice": "185.25",
                                "filledAmount": "185.25",
                                "filledAt": "2026-07-31T22:31:00+09:00",
                            },
                        }
                    ],
                    "hasNext": False,
                }
            }
        }
    )
    client = TossLiveOrderClient(_account(), transport=transport)

    snapshots = client.list_orders(status="CLOSED")

    assert snapshots[0].status == expected


def test_toss_pre_submit_rejects_insufficient_buying_power():
    transport = RecordingTransport(
        {
            ("GET", "/api/v1/buying-power"): {
                "result": {"currency": "USD", "cashBuyingPower": "100"}
            }
        }
    )
    client = TossLiveOrderClient(_account(), transport=transport)

    try:
        client.validate_pre_submit_order(_live_request())
    except ValueError as exc:
        assert "buying power" in str(exc)
    else:
        raise AssertionError("insufficient Toss buying power was not rejected")


def _account() -> BrokerAccountConfig:
    return BrokerAccountConfig(
        id="toss_brokerage",
        broker="toss",
        account_seq=7,
        client_id_env="TOSS_CLIENT_ID",
        client_secret_env="TOSS_CLIENT_SECRET",
    )


def _live_request() -> LiveOrderRequest:
    return LiveOrderRequest(
        order_id="ord_live_1",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity=2,
        limit_price=185.5,
        approval_id="appr_1",
        run_id="run_1",
        currency=Currency.USD,
        account_id="toss_brokerage",
    )


def _instrument() -> TradableInstrument:
    return TradableInstrument(
        symbol="AAPL",
        name="Apple",
        asset_type=AssetType.STOCK,
        region=MarketRegion.US,
        currency=Currency.USD,
        broker="toss",
        broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
        broker_symbol="AAPL",
        broker_symbols={"toss": "TOSS_AAPL"},
        exchange_code=ExchangeCode.NASD,
        quantity_step=1,
        price_tick=0.01,
    )


class RecordingTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, path, params=None, *, account_seq=None):
        self.calls.append(("GET", path, dict(params or {}), account_seq))
        return self.responses[("GET", path)]

    def post(self, path, payload=None, *, account_seq=None):
        self.calls.append(("POST", path, dict(payload or {}), account_seq))
        response = self.responses[("POST", path)]
        if isinstance(response, Exception):
            raise response
        return response

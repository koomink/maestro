from maestro.config.broker import BrokerAccountConfig
from maestro.core.enums import Currency, OrderSide, OrderStatus, OrderType
from maestro.execution.brokers.toss.live_order_client import TossLiveOrderClient
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
                    "orderedAt": "2026-06-19T09:30:00+09:00",
                    "execution": {
                        "filledQuantity": "1",
                        "averageFilledPrice": "185.25",
                        "filledAmount": "185.25",
                        "filledAt": "2026-06-19T09:31:00+09:00",
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


class RecordingTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, path, params=None, *, account_seq=None):
        self.calls.append(("GET", path, dict(params or {}), account_seq))
        return self.responses[("GET", path)]

    def post(self, path, payload=None, *, account_seq=None):
        self.calls.append(("POST", path, dict(payload or {}), account_seq))
        return self.responses[("POST", path)]

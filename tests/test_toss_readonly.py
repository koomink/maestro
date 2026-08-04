from datetime import UTC, date, datetime
from io import BytesIO
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

import pytest

from maestro.config.broker import BrokerAccountConfig
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.brokers.readonly import BrokerReadOnlyService
from maestro.execution.brokers.readonly_factory import (
    AttributionAwareReadOnlyService,
    broker_readonly_account_ids,
    broker_readonly_accounts,
    build_broker_readonly_service,
)
from maestro.execution.brokers.toss.order_history import list_toss_orders
from maestro.execution.brokers.toss.parsers import toss_snapshot_from_payloads
from maestro.execution.brokers.toss.readonly_client import TossReadOnlyClient
from maestro.execution.brokers.toss.transport import (
    TossRateLimitError,
    TossRestTransport,
    TossTransportError,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def test_toss_account_config_does_not_inherit_kis_env_defaults():
    account = BrokerAccountConfig(
        id="toss_brokerage",
        broker="toss",
        client_id_env="TOSS_CLIENT_ID",
        client_secret_env="TOSS_CLIENT_SECRET",
        access_token_env="TOSS_ACCESS_TOKEN",
        account_seq_env="TOSS_ACCOUNT_SEQ",
    )

    assert account.app_key_env is None
    assert account.app_secret_env is None
    assert account.approval_key_env is None
    assert account.client_id_env == "TOSS_CLIENT_ID"
    assert account.client_secret_env == "TOSS_CLIENT_SECRET"
    assert account.account_seq_env == "TOSS_ACCOUNT_SEQ"


def test_toss_readonly_client_requires_account_seq_or_env(monkeypatch):
    monkeypatch.delenv("TOSS_ACCOUNT_SEQ", raising=False)
    account = BrokerAccountConfig(
        id="toss_brokerage",
        broker="toss",
        client_id_env="TOSS_CLIENT_ID",
        client_secret_env="TOSS_CLIENT_SECRET",
        account_seq_env="TOSS_ACCOUNT_SEQ",
    )

    with pytest.raises(ValueError, match="account_seq"):
        TossReadOnlyClient(account, transport=RecordingTossTransport({}))


def test_toss_payloads_normalize_to_broker_snapshot():
    snapshot = toss_snapshot_from_payloads(
        account={"accountNo": "12345678901", "accountSeq": 7, "accountType": "BROKERAGE"},
        holdings={
            "totalPurchaseAmount": {"krw": "6500000", "usd": "100.50"},
            "marketValue": {
                "amount": {"krw": "7200000", "usd": "185.70"},
                "amountAfterCost": {"krw": "7050000", "usd": "184.00"},
            },
            "profitLoss": {},
            "dailyProfitLoss": {},
            "items": [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "marketCountry": "KR",
                    "currency": "KRW",
                    "quantity": "100",
                    "lastPrice": "72000",
                    "averagePurchasePrice": "65000",
                    "marketValue": {
                        "purchaseAmount": "6500000",
                        "amount": "7200000",
                        "amountAfterCost": "7050000",
                    },
                    "profitLoss": {"amount": "700000"},
                    "dailyProfitLoss": {},
                    "cost": {"commission": "14400", "tax": "135600"},
                },
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "marketCountry": "US",
                    "currency": "USD",
                    "quantity": "1.5",
                    "lastPrice": "185.70",
                    "averagePurchasePrice": "100.50",
                    "marketValue": {
                        "purchaseAmount": "150.75",
                        "amount": "278.55",
                        "amountAfterCost": "277.00",
                    },
                    "profitLoss": {"amount": "127.80"},
                    "dailyProfitLoss": {},
                    "cost": {"commission": "1.55", "tax": None},
                },
            ],
        },
        buying_power={"currency": "KRW", "cashBuyingPower": "5000000"},
        prices=[
            {"symbol": "005930", "lastPrice": "72000", "currency": "KRW"},
            {"symbol": "AAPL", "lastPrice": "185.70", "currency": "USD"},
        ],
        fetched_at=datetime(2026, 6, 9, tzinfo=UTC),
    )

    assert snapshot.account.account_id == "12345678901"
    assert snapshot.account.cash == 5_000_000.0
    assert snapshot.account.buying_power == 5_000_000.0
    assert snapshot.account.cash_by_currency == {"KRW": 5_000_000.0}
    assert snapshot.account.ledger_cash_by_currency is None
    assert snapshot.account.buying_power_by_currency == {"KRW": 5_000_000.0}
    assert snapshot.current_prices == {"005930": 72000.0, "AAPL": 185.70}
    assert [position.symbol for position in snapshot.account.positions] == ["005930", "AAPL"]
    assert snapshot.account.positions[1].quantity == 1.5
    assert snapshot.account.positions[1].currency == "USD"
    assert snapshot.account.total_value == 12_200_278.55
    assert snapshot.account.daily_pnl_by_currency is None


def test_toss_daily_profit_loss_normalizes_to_daily_pnl_by_currency():
    snapshot = toss_snapshot_from_payloads(
        account={"accountNo": "12345678901", "accountSeq": 7, "accountType": "BROKERAGE"},
        holdings={
            "dailyProfitLoss": {"krw": "-15000", "usd": "1.50", "totalKrw": None},
            "items": [],
        },
        buying_power={"currency": "KRW", "cashBuyingPower": "5000000"},
        fetched_at=datetime(2026, 6, 9, tzinfo=UTC),
    )

    assert snapshot.account.positions == []
    assert snapshot.account.daily_pnl_by_currency == {"KRW": -15_000.0, "USD": 1.5}


def test_toss_snapshot_keeps_open_buy_reservations_out_of_cash_fields():
    snapshot = toss_snapshot_from_payloads(
        account={"accountNo": "12345678901", "accountSeq": 7},
        holdings={"items": []},
        buying_powers=[
            {"currency": "KRW", "cashBuyingPower": "4299895"},
            {"currency": "USD", "cashBuyingPower": "814.56475"},
        ],
        open_orders=[
            {
                "orderId": "KR-OPEN",
                "symbol": "005930",
                "side": "BUY",
                "status": "PENDING",
                "price": "70000",
                "quantity": "10",
                "currency": "KRW",
                "orderedAt": "2026-07-22T09:30:00+09:00",
                "execution": {"filledQuantity": "0"},
            },
            {
                "orderId": "US-OPEN",
                "symbol": "AAPL",
                "side": "BUY",
                "status": "PARTIAL_FILLED",
                "price": "185.25",
                "quantity": "2",
                "currency": "USD",
                "orderedAt": "2026-07-22T23:30:00+09:00",
                "execution": {
                    "filledQuantity": "1",
                    "averageFilledPrice": "185.25",
                },
            },
        ],
        commissions=[
            {"marketCountry": "KR", "commissionRate": "0.015"},
            {"marketCountry": "US", "commissionRate": "0.1"},
        ],
    )

    assert snapshot.account.cash_by_currency == {"KRW": 4_299_895.0, "USD": 814.56475}
    assert snapshot.account.buying_power == 4_299_895.0
    assert snapshot.account.cash_balance is not None
    assert snapshot.account.cash_balance.available_cash_by_currency == {
        "KRW": 4_299_895.0,
        "USD": 814.56475,
    }
    assert snapshot.account.cash_balance.reserved_cash_by_currency == {
        "KRW": 700_105.0,
        "USD": 185.43525,
    }
    assert [order.order_id for order in snapshot.unfilled_orders] == ["KR-OPEN", "US-OPEN"]
    assert [order.status for order in snapshot.unfilled_orders] == [
        "OPEN",
        "PARTIALLY_FILLED",
    ]
    assert [order.raw_status for order in snapshot.unfilled_orders] == [
        "PENDING",
        "PARTIAL_FILLED",
    ]


def test_toss_readonly_client_fetches_account_snapshot_with_account_header():
    transport = RecordingTossTransport(
        {
            ("/api/v1/accounts", None): {
                "result": [
                    {
                        "accountNo": "12345678901",
                        "accountSeq": 7,
                        "accountType": "BROKERAGE",
                    }
                ]
            },
            ("/api/v1/holdings", 7): {
                "result": {
                    "totalPurchaseAmount": {"krw": "0", "usd": None},
                    "marketValue": {
                        "amount": {"krw": "0", "usd": None},
                        "amountAfterCost": {"krw": "0", "usd": None},
                    },
                    "profitLoss": {},
                    "dailyProfitLoss": {},
                    "items": [],
                }
            },
            ("/api/v1/buying-power", 7): {
                "result": {"currency": "KRW", "cashBuyingPower": "5000000"}
            },
        }
    )
    account = BrokerAccountConfig(
        id="toss_brokerage",
        broker="toss",
        client_id_env="TOSS_CLIENT_ID",
        client_secret_env="TOSS_CLIENT_SECRET",
        account_seq=7,
    )

    account_snapshot = TossReadOnlyClient(account, transport=transport).get_account_snapshot()

    assert account_snapshot.account_id == "12345678901"
    today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    assert transport.calls == [
        ("/api/v1/accounts", {}, None),
        ("/api/v1/holdings", {}, 7),
        ("/api/v1/buying-power", {"currency": "KRW"}, 7),
        ("/api/v1/buying-power", {"currency": "USD"}, 7),
        ("/api/v1/orders", {"status": "OPEN"}, 7),
        (
            "/api/v1/orders",
            {
                "status": "CLOSED",
                "from": today,
                "to": today,
                "limit": 100,
            },
            7,
        ),
        ("/api/v1/commissions", {}, 7),
    ]


def test_toss_readonly_client_ignores_non_toss_symbols_for_price_refresh():
    transport = RecordingTossTransport(
        {
            ("/api/v1/accounts", None): {
                "result": [
                    {
                        "accountNo": "12345678901",
                        "accountSeq": 7,
                        "accountType": "BROKERAGE",
                    }
                ]
            },
            ("/api/v1/holdings", 7): {
                "result": {
                    "totalPurchaseAmount": {"krw": "0", "usd": None},
                    "marketValue": {
                        "amount": {"krw": "0", "usd": "185.70"},
                        "amountAfterCost": {"krw": "0", "usd": "185.70"},
                    },
                    "profitLoss": {},
                    "dailyProfitLoss": {},
                    "items": [
                        {
                            "symbol": "AAPL",
                            "name": "Apple",
                            "currency": "USD",
                            "quantity": "1",
                            "lastPrice": "185.70",
                            "averagePurchasePrice": "180.00",
                            "marketValue": {"amount": "185.70"},
                            "profitLoss": {},
                            "dailyProfitLoss": {},
                        }
                    ],
                }
            },
            ("/api/v1/buying-power", 7): {
                "result": {"currency": "KRW", "cashBuyingPower": "5000000"}
            },
            ("/api/v1/prices", None): {
                "result": [{"symbol": "AAPL", "lastPrice": "185.70", "currency": "USD"}]
            },
        }
    )
    account = BrokerAccountConfig(
        id="toss_brokerage",
        broker="toss",
        client_id_env="TOSS_CLIENT_ID",
        client_secret_env="TOSS_CLIENT_SECRET",
        account_seq=7,
    )

    prices = TossReadOnlyClient(account, transport=transport).get_current_prices(
        ["AAPL", "TIGER_NASDAQ100_LEVERAGE", "CASH_KRW"]
    )

    assert prices == {"AAPL": 185.70, "CASH_KRW": 1.0}
    today = datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()
    assert transport.calls == [
        ("/api/v1/accounts", {}, None),
        ("/api/v1/holdings", {}, 7),
        ("/api/v1/buying-power", {"currency": "KRW"}, 7),
        ("/api/v1/buying-power", {"currency": "USD"}, 7),
        ("/api/v1/orders", {"status": "OPEN"}, 7),
        (
            "/api/v1/orders",
            {
                "status": "CLOSED",
                "from": today,
                "to": today,
                "limit": 100,
            },
            7,
        ),
        ("/api/v1/commissions", {}, 7),
        ("/api/v1/prices", {"symbols": "AAPL"}, None),
    ]


def test_toss_closed_order_history_follows_cursor_pagination():
    transport = CursorOrderHistoryTransport()

    orders = list_toss_orders(
        transport,
        7,
        status="CLOSED",
        symbol="PDBC",
        from_date=date(2026, 7, 30),
        to_date=date(2026, 7, 31),
    )

    assert [order["orderId"] for order in orders] == ["TOSS-1", "TOSS-2"]
    assert transport.calls == [
        {
            "status": "CLOSED",
            "symbol": "PDBC",
            "from": "2026-07-30",
            "to": "2026-07-31",
            "limit": 100,
        },
        {
            "status": "CLOSED",
            "symbol": "PDBC",
            "from": "2026-07-30",
            "to": "2026-07-31",
            "limit": 100,
            "cursor": "next-1",
        },
    ]


def test_toss_transport_attaches_auth_and_account_headers():
    opener = RecordingOpener({"result": []})
    transport = TossRestTransport(
        base_url="https://openapi.tossinvest.com",
        access_token_provider=lambda: "token-123",
        timeout_seconds=3,
        opener=opener,
    )

    transport.get("/api/v1/holdings", account_seq=7)

    request = opener.requests[0]
    assert request.get_header("Authorization") == "Bearer token-123"
    assert request.get_header("X-tossinvest-account") == "7"


def test_toss_transport_raises_rate_limit_error_with_retry_after():
    opener = RateLimitOpener()
    transport = TossRestTransport(
        base_url="https://openapi.tossinvest.com",
        access_token_provider=lambda: "token-123",
        timeout_seconds=3,
        opener=opener,
    )

    with pytest.raises(TossRateLimitError) as exc_info:
        transport.get("/api/v1/holdings", account_seq=7)

    assert exc_info.value.retry_after == 1
    assert exc_info.value.rate_limit_remaining == 0


def test_toss_transport_preserves_http_error_evidence():
    opener = ErrorOpener(
        422,
        {
            "error": {
                "code": "prerequisite-required",
                "message": "위험고지 등록이 필요합니다",
                "data": {"prerequisite": "risk-disclosure"},
            }
        },
        {"X-Request-Id": "req-422"},
    )
    transport = TossRestTransport(
        base_url="https://openapi.tossinvest.com",
        access_token_provider=lambda: "token-123",
        timeout_seconds=3,
        opener=opener,
    )

    with pytest.raises(TossTransportError) as exc_info:
        transport.post("/api/v1/orders", {}, account_seq=7)

    assert exc_info.value.status_code == 422
    assert exc_info.value.error_code == "prerequisite-required"
    assert exc_info.value.request_id == "req-422"
    assert exc_info.value.data == {"prerequisite": "risk-disclosure"}


def test_broker_readonly_service_stores_toss_snapshot(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=None)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    client = StaticBrokerClient(
        toss_snapshot_from_payloads(
            account={
                "accountNo": "12345678901",
                "accountSeq": 7,
                "accountType": "BROKERAGE",
            },
            holdings={
                "totalPurchaseAmount": {"krw": "0", "usd": None},
                "marketValue": {
                    "amount": {"krw": "0", "usd": None},
                    "amountAfterCost": {"krw": "0", "usd": None},
                },
                "profitLoss": {},
                "dailyProfitLoss": {},
                "items": [],
            },
            buying_power={"currency": "KRW", "cashBuyingPower": "5000000"},
            prices=[],
        )
    )

    BrokerReadOnlyService(
        client,
        store,
        audit,
        logical_account_id="toss_brokerage",
        audit_event_type="toss_readonly_snapshot",
    ).fetch_and_store_snapshot(["005930"], run_id="run_toss_sync")

    latest = store.load_latest_broker_account_snapshot()
    assert latest is not None
    assert latest["run_id"] == "run_toss_sync"
    assert latest["account_id"] == "toss_brokerage"
    assert latest["payload"]["account_id"] == "toss_brokerage"
    assert latest["payload"]["broker_account_id"] == "12345678901"
    assert latest["payload"]["account"]["source"] == "toss_openapi_readonly"


def test_attribution_aware_readonly_service_reconciles_stored_snapshot(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), initial_cash=None)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))
    client = StaticBrokerClient(
        toss_snapshot_from_payloads(
            account={
                "accountNo": "12345678901",
                "accountSeq": 7,
                "accountType": "BROKERAGE",
            },
            holdings={
                "totalPurchaseAmount": {"krw": "0", "usd": "100"},
                "marketValue": {
                    "amount": {"krw": "0", "usd": "200"},
                    "amountAfterCost": {"krw": "0", "usd": "200"},
                },
                "profitLoss": {},
                "dailyProfitLoss": {},
                "items": [
                    {
                        "symbol": "QQQ",
                        "name": "QQQ",
                        "currency": "USD",
                        "quantity": "2",
                        "lastPrice": "100",
                        "averagePurchasePrice": "50",
                        "marketValue": {"amount": "200"},
                        "profitLoss": {},
                        "dailyProfitLoss": {},
                    }
                ],
            },
            buying_power={"currency": "KRW", "cashBuyingPower": "0"},
            prices=[{"symbol": "QQQ", "lastPrice": "100", "currency": "USD"}],
        )
    )
    inner = BrokerReadOnlyService(
        client,
        store,
        audit,
        logical_account_id="toss_brokerage",
        audit_event_type="toss_readonly_snapshot",
    )

    AttributionAwareReadOnlyService(
        inner,
        store,
        audit,
        account_id="toss_brokerage",
        strategy_symbols_by_bucket={"crescendo_us": {"QQQ", "SPY"}},
    ).fetch_and_store_snapshot(["QQQ"], run_id="run_toss_sync")

    rows = store.list_account_attribution_snapshots()
    assert rows[0]["payload"]["bucket_id"] == "crescendo_us"
    assert (
        rows[0]["payload"]["broker_snapshot_id"]
        == store.load_latest_broker_account_snapshot()["id"]
    )


def test_readonly_factory_routes_kis_and_toss_accounts(tmp_path):
    from maestro.config.models import (
        AuditConfig,
        MaestroConfig,
        PortfolioConfig,
        StateConfig,
    )
    from maestro.core.enums import RunMode

    config = MaestroConfig(
        mode=RunMode.LIVE_READONLY,
        portfolio=PortfolioConfig(allowed_symbols=["CASH"]),
        strategies=[],
        state=StateConfig(sqlite_path=str(tmp_path / "state.db")),
        audit=AuditConfig(jsonl_path=str(tmp_path / "audit.jsonl")),
        accounts=[
            BrokerAccountConfig(
                id="kis_mock",
                broker="kis",
                provider="mock",
                account_id="MOCK-ACCOUNT",
                broker_products=["kis_domestic_stock"],
                enabled=True,
            ),
            BrokerAccountConfig(
                id="toss_brokerage",
                broker="toss",
                client_id_env="TOSS_CLIENT_ID",
                client_secret_env="TOSS_CLIENT_SECRET",
                account_seq=7,
                enabled=True,
            ),
        ],
    )
    store = StateStore(str(tmp_path / "state.db"), initial_cash=None)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))

    assert [account_id for account_id, _ in broker_readonly_accounts(config)] == [
        "kis_mock",
        "toss_brokerage",
    ]
    assert broker_readonly_account_ids(config) == ["kis_mock", "toss_brokerage"]
    assert isinstance(
        build_broker_readonly_service(config, store, audit, account_id="kis_mock"),
        KISReadOnlyService,
    )
    assert isinstance(
        build_broker_readonly_service(config, store, audit, account_id="toss_brokerage"),
        BrokerReadOnlyService,
    )


class RecordingTossTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, path, params=None, *, account_seq=None):
        params = dict(params or {})
        self.calls.append((path, params, account_seq))
        if path == "/api/v1/orders" and (path, account_seq) not in self.responses:
            return {"result": {"orders": [], "nextCursor": None, "hasNext": False}}
        if path == "/api/v1/commissions" and (path, account_seq) not in self.responses:
            return {"result": []}
        if path == "/api/v1/buying-power" and params.get("currency") == "USD":
            response = self.responses[(path, account_seq)]
            result = dict(response["result"])
            if result.get("currency") != "USD":
                return {"result": {"currency": "USD", "cashBuyingPower": "0"}}
        return self.responses[(path, account_seq)]


class RecordingOpener:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        return JsonResponse(self.payload)


class CursorOrderHistoryTransport:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None, *, account_seq=None):
        assert path == "/api/v1/orders"
        assert account_seq == 7
        params = dict(params or {})
        self.calls.append(params)
        if "cursor" not in params:
            return {
                "result": {
                    "orders": [{"orderId": "TOSS-1"}],
                    "hasNext": True,
                    "nextCursor": "next-1",
                }
            }
        return {
            "result": {
                "orders": [{"orderId": "TOSS-2"}],
                "hasNext": False,
                "nextCursor": None,
            }
        }


class RateLimitOpener:
    def open(self, request, timeout):
        raise HTTPError(
            request.full_url,
            429,
            "Too Many Requests",
            {"Retry-After": "1", "X-RateLimit-Remaining": "0"},
            None,
        )


class ErrorOpener:
    def __init__(self, status_code, payload, headers):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers

    def open(self, request, timeout):
        import json

        raise HTTPError(
            request.full_url,
            self.status_code,
            "request failed",
            self.headers,
            BytesIO(json.dumps(self.payload).encode("utf-8")),
        )


class JsonResponse:
    def __init__(self, payload):
        self.payload = payload
        self.status = 200
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def read(self):
        import json

        return json.dumps(self.payload).encode("utf-8")


class StaticBrokerClient:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_account_snapshot(self):
        return self.snapshot.account

    def get_positions(self):
        return list(self.snapshot.account.positions)

    def get_buying_power(self, symbol=None, order_price=None):
        return self.snapshot.account.buying_power_detail

    def get_current_prices(self, symbols):
        return dict(self.snapshot.current_prices)

    def get_order_fills(self):
        return []

    def get_unfilled_orders(self):
        return []

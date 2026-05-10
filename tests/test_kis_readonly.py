from pathlib import Path

import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.config.models import KISConfig
from maestro.core.enums import BrokerProduct, OrderSide, OrderStatus
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.kis.auth import KISAuthManager
from maestro.execution.brokers.kis.rest_client import (
    KISRestDomesticStockLiveOrderClient,
    KISRestLiveOrderClient,
    KISRestOverseasStockLiveOrderClient,
    KISRestOverseasStockReadOnlyClient,
    KISRestReadOnlyClient,
    build_kis_rest_live_order_client,
)
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.live_orders import BrokerOrderId, LiveOrderRequest
from maestro.monitoring.audit_logger import AuditLogger
from maestro.state.store import StateStore


def test_kis_readonly_service_stores_snapshot(tmp_path):
    config = _live_readonly_config(tmp_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)

    snapshot = KISReadOnlyService(config.kis, store, audit).fetch_and_store_snapshot(
        config.portfolio.allowed_symbols
    )

    latest = store.load_latest_broker_account_snapshot()
    assert snapshot.account.account_id == "MOCK-ACCOUNT"
    assert snapshot.account.total_value == 10_000_000
    assert latest is not None
    assert latest["payload"]["account"]["account_id"] == "MOCK-ACCOUNT"
    assert store.status()["counts"]["broker_account_snapshots"] == 1


def test_kis_cli_sync_and_account(tmp_path):
    config = _live_readonly_config(tmp_path)
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()

    sync_result = runner.invoke(app, ["kis-sync", "--config", str(config_path)])
    account_result = runner.invoke(app, ["kis-account", "--config", str(config_path)])

    assert sync_result.exit_code == 0
    assert "account_id=MOCK-ACCOUNT" in sync_result.output
    assert account_result.exit_code == 0
    assert "positions=2" in account_result.output


def test_kis_rest_client_normalizes_readonly_responses(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    config = KISConfig(
        enabled=True,
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
    )
    transport = FakeKISTransport()

    client = KISRestReadOnlyClient(config, transport=transport)
    snapshot = client.get_account_snapshot()
    prices = client.get_current_prices(["005930", "CASH"])
    fills = client.get_order_fills()
    unfilled = client.get_unfilled_orders()

    assert snapshot.account_id == "12345678-01"
    assert snapshot.cash == 1_000_000
    assert snapshot.buying_power == 750_000
    assert snapshot.positions[0].symbol == "005930"
    assert snapshot.positions[0].market_value == 140_000
    assert prices == {"005930": 71000.0, "CASH": 1.0}
    assert fills[0].status == "filled"
    assert unfilled[0].status == "open"
    assert all(call["method"] == "GET" for call in transport.calls)
    assert not any("/order-cash" in call["url"] for call in transport.calls)


def test_kis_auth_manager_issues_and_caches_token(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.delenv("TEST_KIS_ACCESS_TOKEN", raising=False)
    token_cache_path = tmp_path / "kis_token.json"
    config = KISConfig(
        enabled=True,
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
        token_cache_path=str(token_cache_path),
    )
    transport = FakeKISTransport()

    token = KISAuthManager(config, transport).get_access_token()
    cached_token = KISAuthManager(config, transport).get_access_token()

    assert token.access_token == "issued-token"
    assert cached_token.access_token == "issued-token"
    assert token_cache_path.stat().st_mode & 0o777 == 0o600
    token_calls = [call for call in transport.calls if call["url"].endswith("/oauth2/tokenP")]
    assert len(token_calls) == 1


def test_kis_readonly_rest_client_exposes_no_order_submission_surface():
    client_source = Path("src/maestro/execution/brokers/kis/rest_client.py").read_text()
    forbidden_tokens = [
        "/order-credit",
        "/order-rvsecncl",
        "/order-resv",
        "def buy",
        "def sell",
        "def cancel",
        "def amend",
    ]

    for token in forbidden_tokens:
        assert token not in client_source


def test_kis_live_order_client_uses_domestic_limit_order_payload(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    config = KISConfig(
        enabled=True,
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
    )
    transport = FakeKISTransport()
    client = KISRestLiveOrderClient(config, transport=transport)

    result = client.submit_limit_order(
        LiveOrderRequest(
            order_id="ord_live_1",
            symbol="005930",
            side=OrderSide.BUY,
            quantity=2,
            limit_price=70000,
            approval_id="appr_1",
            run_id="run_1",
        )
    )

    order_call = [call for call in transport.calls if call["url"].endswith("/trading/order-cash")][
        0
    ]
    assert result.status == OrderStatus.ACCEPTED_BY_BROKER
    assert result.broker_order is not None
    assert result.broker_order.broker_order_id == "0000000001"
    assert order_call["method"] == "POST"
    assert order_call["headers"]["tr_id"] == "TTTC0802U"
    assert order_call["json_body"] == {
        "CANO": "12345678",
        "ACNT_PRDT_CD": "01",
        "PDNO": "005930",
        "ORD_DVSN": "00",
        "ORD_QTY": "2",
        "ORD_UNPR": "70000",
    }


def test_kis_domestic_adapter_maps_canonical_symbol_to_broker_symbol(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    config = KISConfig(
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
        broker_product=BrokerProduct.KIS_DOMESTIC_STOCK,
    )
    transport = FakeKISTransport()
    client = KISRestDomesticStockLiveOrderClient(
        config,
        transport=transport,
        instruments=[
            TradableInstrument(
                symbol="SAMSUNG",
                asset_type="stock",
                region="KR",
                currency="KRW",
                broker="kis",
                broker_product="kis_domestic_stock",
                broker_symbol="005930",
                exchange_code="KRX",
                quantity_step=1,
                price_tick=1,
            )
        ],
    )

    client.submit_limit_order(
        LiveOrderRequest(
            order_id="ord_live_1",
            symbol="SAMSUNG",
            side=OrderSide.BUY,
            quantity=2,
            limit_price=70000,
            approval_id="appr_1",
            run_id="run_1",
        )
    )

    order_call = [call for call in transport.calls if call["url"].endswith("/trading/order-cash")][
        0
    ]
    assert order_call["json_body"]["PDNO"] == "005930"


def test_kis_provider_defaults_to_overseas_stock_adapter(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    config = KISConfig(
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
    )

    client = build_kis_rest_live_order_client(config, [])

    assert isinstance(client, KISRestOverseasStockLiveOrderClient)


def test_kis_overseas_readonly_client_normalizes_us_account(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    config = KISConfig(
        enabled=True,
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
        broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
    )
    transport = FakeKISOverseasTransport()
    client = KISRestOverseasStockReadOnlyClient(
        config,
        transport=transport,
        instruments=_us_instruments(),
    )

    snapshot = client.get_account_snapshot()
    prices = client.get_current_prices(["AAPL", "VOO", "CASH_USD"])
    fills = client.get_order_fills()
    unfilled = client.get_unfilled_orders()

    assert snapshot.cash_balance is not None
    assert snapshot.account_id == "12345678-01"
    assert snapshot.cash == 2500.0
    assert snapshot.cash_balance.currency == "USD"
    assert snapshot.cash_balance.withdrawable_cash == 2400.0
    assert snapshot.buying_power == 1500.0
    assert snapshot.positions[0].symbol == "AAPL"
    assert snapshot.positions[0].quantity == 3.0
    assert snapshot.positions[0].market_value == 570.0
    assert prices == {"AAPL": 191.25, "VOO": 500.5, "CASH_USD": 1.0}
    assert fills[0].symbol == "AAPL"
    assert fills[0].status == "filled"
    assert fills[0].average_fill_price == 189.5
    assert unfilled[0].symbol == "VOO"
    assert unfilled[0].status == "open"
    assert any(call["headers"]["tr_id"] == "TTTS3012R" for call in transport.calls)
    assert any(call["headers"]["tr_id"] == "CTRP6504R" for call in transport.calls)
    assert any(call["headers"]["tr_id"] == "TTTS3007R" for call in transport.calls)
    assert any(call["headers"]["tr_id"] == "TTTS3035R" for call in transport.calls)
    assert any(call["headers"]["tr_id"] == "TTTS3018R" for call in transport.calls)
    assert not any(call["method"] != "GET" for call in transport.calls)
    assert not any(
        "/order" in call["url"] and "inquire" not in call["url"] for call in transport.calls
    )


def test_kis_overseas_readonly_requires_universe_metadata(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    config = KISConfig(
        enabled=True,
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
        broker_product=BrokerProduct.KIS_OVERSEAS_STOCK,
    )
    client = KISRestOverseasStockReadOnlyClient(
        config,
        transport=FakeKISOverseasTransport(),
        instruments=[],
    )

    try:
        client.get_current_prices(["AAPL"])
    except ValueError as exc:
        assert "requires universe metadata" in str(exc)
    else:
        raise AssertionError("Expected missing metadata to fail closed")


def test_kis_live_order_client_normalizes_open_status(monkeypatch):
    client = _kis_live_order_client(monkeypatch, FakeKISTransport())

    snapshot = client.get_order_status(_broker_order("0002"))

    assert snapshot.status == OrderStatus.OPEN
    assert snapshot.symbol == "005930"
    assert snapshot.side == OrderSide.BUY
    assert snapshot.partial_fill.ordered_quantity == 3.0
    assert snapshot.partial_fill.filled_quantity == 0.0
    assert snapshot.partial_fill.remaining_quantity == 3.0


def test_kis_live_order_client_normalizes_filled_status(monkeypatch):
    client = _kis_live_order_client(monkeypatch, FakeKISTransport())

    snapshot = client.get_order_status(_broker_order("0001"))

    assert snapshot.status == OrderStatus.FILLED
    assert snapshot.partial_fill.ordered_quantity == 2.0
    assert snapshot.partial_fill.filled_quantity == 2.0
    assert snapshot.partial_fill.remaining_quantity == 0.0
    assert snapshot.fills[0].quantity == 2.0
    assert snapshot.fills[0].price == 70_000.0


def test_kis_live_order_client_normalizes_partial_fill_status(monkeypatch):
    client = _kis_live_order_client(monkeypatch, FakeKISTransport(partial_order=True))

    snapshot = client.get_order_status(_broker_order("0003"))

    assert snapshot.status == OrderStatus.PARTIALLY_FILLED
    assert snapshot.partial_fill.ordered_quantity == 5.0
    assert snapshot.partial_fill.filled_quantity == 2.0
    assert snapshot.partial_fill.remaining_quantity == 3.0


def test_kis_live_order_client_normalizes_rejected_status(monkeypatch):
    client = _kis_live_order_client(monkeypatch, FakeKISTransport(rejected_order=True))

    snapshot = client.get_order_status(_broker_order("0004"))

    assert snapshot.status == OrderStatus.REJECTED
    assert snapshot.raw_status == "주문거부"


def test_kis_live_order_client_normalizes_canceled_status(monkeypatch):
    client = _kis_live_order_client(monkeypatch, FakeKISTransport(canceled_order=True))

    snapshot = client.get_order_status(_broker_order("0005"))

    assert snapshot.status == OrderStatus.CANCELED
    assert snapshot.raw_status == "취소"


def test_kis_live_order_client_returns_unknown_for_missing_order(monkeypatch):
    client = _kis_live_order_client(monkeypatch, FakeKISTransport())

    snapshot = client.get_order_status(_broker_order("missing"))

    assert snapshot.status == OrderStatus.UNKNOWN
    assert "not found" in (snapshot.message or "")


def _live_readonly_config(tmp_path):
    raw = yaml.safe_load(Path("configs/live_readonly.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "live_readonly.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "live_readonly.jsonl")
    config_path = tmp_path / "source_live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path)


def _kis_live_order_client(monkeypatch, transport: "FakeKISTransport") -> KISRestLiveOrderClient:
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    config = KISConfig(
        enabled=True,
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
    )
    return KISRestLiveOrderClient(config, transport=transport)


def _broker_order(order_id: str) -> BrokerOrderId:
    return BrokerOrderId(
        broker="kis",
        broker_order_id=order_id,
        broker_order_org_no="KRX",
        order_id="ord_live_1",
        submitted_at="2026-05-08T00:00:00+00:00",
    )


def _us_instruments() -> list[TradableInstrument]:
    return [
        TradableInstrument(
            symbol="CASH_USD",
            asset_type="cash",
            region="US",
            currency="USD",
            broker="kis",
            broker_product="kis_overseas_stock",
            broker_symbol="USD",
            quantity_step=0.01,
            price_tick=0.01,
        ),
        TradableInstrument(
            symbol="AAPL",
            asset_type="stock",
            region="US",
            currency="USD",
            broker="kis",
            broker_product="kis_overseas_stock",
            broker_symbol="AAPL",
            exchange_code="NASD",
            quantity_step=1,
            price_tick=0.01,
        ),
        TradableInstrument(
            symbol="VOO",
            asset_type="etf",
            region="US",
            currency="USD",
            broker="kis",
            broker_product="kis_overseas_stock",
            broker_symbol="VOO",
            exchange_code="AMEX",
            quantity_step=1,
            price_tick=0.01,
        ),
    ]


class FakeKISTransport:
    def __init__(
        self,
        *,
        partial_order: bool = False,
        rejected_order: bool = False,
        canceled_order: bool = False,
    ) -> None:
        self.calls = []
        self.partial_order = partial_order
        self.rejected_order = rejected_order
        self.canceled_order = canceled_order

    def request(self, method, url, *, headers, params=None, json_body=None, timeout_seconds=10.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params or {},
                "json_body": json_body or {},
                "timeout_seconds": timeout_seconds,
            }
        )
        if url.endswith("/oauth2/tokenP"):
            return {
                "access_token": "issued-token",
                "access_token_token_expired": "2099-01-01 00:00:00",
            }
        if url.endswith("/inquire-balance"):
            return {
                "rt_cd": "0",
                "output1": [
                    {
                        "pdno": "005930",
                        "prdt_name": "Samsung Electronics",
                        "hldg_qty": "2",
                        "pchs_avg_pric": "70000",
                        "prpr": "70000",
                        "evlu_pfls_amt": "0",
                    }
                ],
                "output2": [
                    {
                        "dnca_tot_amt": "1000000",
                        "tot_evlu_amt": "1140000",
                        "nxdy_excc_amt": "1000000",
                    }
                ],
            }
        if url.endswith("/inquire-psbl-order"):
            return {
                "rt_cd": "0",
                "output": {
                    "nrcvb_buy_amt": "750000",
                    "nrcvb_buy_qty": "10",
                },
            }
        if url.endswith("/inquire-price"):
            return {"rt_cd": "0", "output": {"stck_prpr": "71000"}}
        if url.endswith("/inquire-daily-ccld"):
            ccld_dvsn = (params or {}).get("CCLD_DVSN")
            if ccld_dvsn == "02":
                return {
                    "rt_cd": "0",
                    "output1": [
                        {
                            "odno": "0002",
                            "pdno": "005930",
                            "sll_buy_dvsn_cd": "02",
                            "ord_qty": "3",
                            "tot_ccld_qty": "0",
                            "ord_unpr": "70000",
                        }
                    ],
                }
            if self.partial_order:
                return {
                    "rt_cd": "0",
                    "output1": [
                        {
                            "odno": "0003",
                            "pdno": "005930",
                            "sll_buy_dvsn_cd": "02",
                            "ord_qty": "5",
                            "tot_ccld_qty": "2",
                            "avg_prvs": "70000",
                        }
                    ],
                }
            if self.rejected_order:
                return {
                    "rt_cd": "0",
                    "output1": [
                        {
                            "odno": "0004",
                            "pdno": "005930",
                            "sll_buy_dvsn_cd": "02",
                            "ord_qty": "5",
                            "tot_ccld_qty": "0",
                            "rjct_rson_name": "주문거부",
                        }
                    ],
                }
            if self.canceled_order:
                return {
                    "rt_cd": "0",
                    "output1": [
                        {
                            "odno": "0005",
                            "pdno": "005930",
                            "sll_buy_dvsn_cd": "01",
                            "ord_qty": "5",
                            "tot_ccld_qty": "0",
                            "ccld_dvsn_name": "취소",
                        }
                    ],
                }
            return {
                "rt_cd": "0",
                "output1": [
                    {
                        "odno": "0001",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",
                        "ord_qty": "2",
                        "tot_ccld_qty": "2",
                        "avg_prvs": "70000",
                    }
                ],
            }
        if url.endswith("/trading/order-cash"):
            return {
                "rt_cd": "0",
                "msg1": "order accepted",
                "output": {
                    "KRX_FWDG_ORD_ORGNO": "KRX",
                    "ODNO": "0000000001",
                    "ORD_TMD": "090001",
                },
            }
        raise AssertionError(f"Unexpected KIS fake URL: {url}")


class FakeKISOverseasTransport:
    def __init__(self) -> None:
        self.calls = []

    def request(self, method, url, *, headers, params=None, json_body=None, timeout_seconds=10.0):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "params": params or {},
                "json_body": json_body or {},
                "timeout_seconds": timeout_seconds,
            }
        )
        if url.endswith("/inquire-balance"):
            return {
                "rt_cd": "0",
                "output2": [
                    {
                        "ovrs_pdno": "AAPL",
                        "ovrs_item_name": "Apple Inc",
                        "ovrs_cblc_qty": "3",
                        "pchs_avg_pric": "180.00",
                        "now_pric2": "190.00",
                        "frcr_evlu_pfls_amt": "30.00",
                        "ovrs_excg_cd": "NASD",
                    }
                ],
            }
        if url.endswith("/inquire-present-balance"):
            return {
                "rt_cd": "0",
                "output2": {
                    "crcy_cd": "USD",
                    "frcr_use_psbl_amt": "2500.00",
                    "frcr_drwg_psbl_amt_1": "2400.00",
                },
                "output3": {
                    "tot_asst_amt": "3070.00",
                },
            }
        if url.endswith("/inquire-psamount"):
            assert (params or {})["ITEM_CD"] == "AAPL"
            return {
                "rt_cd": "0",
                "output": {
                    "ovrs_ord_psbl_amt": "1500.00",
                    "max_ord_psbl_qty": "7",
                },
            }
        if url.endswith("/quotations/price"):
            symbol = (params or {})["SYMB"]
            price = "191.25" if symbol == "AAPL" else "500.50"
            return {
                "rt_cd": "0",
                "output": {
                    "last": price,
                },
            }
        if url.endswith("/inquire-ccnl"):
            return {
                "rt_cd": "0",
                "output": [
                    {
                        "odno": "9001",
                        "pdno": "AAPL",
                        "prdt_name": "Apple Inc",
                        "sll_buy_dvsn_cd": "02",
                        "ft_ord_qty": "2",
                        "ft_ccld_qty": "2",
                        "ft_ccld_unpr3": "189.50",
                        "prcs_stat_name": "체결",
                    }
                ],
            }
        if url.endswith("/inquire-nccs"):
            return {
                "rt_cd": "0",
                "output": [
                    {
                        "odno": "9002",
                        "pdno": "VOO",
                        "sll_buy_dvsn_cd": "02",
                        "ft_ord_qty": "1",
                        "ft_ccld_qty": "0",
                        "nccs_qty": "1",
                        "ft_ord_unpr3": "500.00",
                    }
                ],
            }
        raise AssertionError(f"Unexpected KIS overseas fake URL: {url}")

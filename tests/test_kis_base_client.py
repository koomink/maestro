import pytest

from maestro.config.models import KISConfig
from maestro.core.enums import BrokerProduct
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.kis.auth import KISAuthManager
from maestro.execution.brokers.kis.base import KISRestBaseClient


def test_kis_rest_base_client_handles_headers_symbols_and_tr_ids(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    client = KISRestBaseClient(
        _config(),
        transport=FakeTransport([{"rt_cd": "0", "output": {}}]),
        instruments=[_instrument()],
    )

    payload = client._get("/uapi/test", "TR_ID", {"PDNO": client._broker_symbol("SAMSUNG")})
    headers = client.transport.calls[0]["headers"]

    assert payload["rt_cd"] == "0"
    assert headers["tr_id"] == "TR_ID"
    assert headers["authorization"] == "Bearer access-token"
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert client._broker_symbol("SAMSUNG") == "005930"
    assert client._canonical_symbol("005930") == "SAMSUNG"
    assert client._tr_id(real="REAL", demo="DEMO") == "REAL"


def test_kis_rest_base_client_uses_demo_tr_id_for_paper_trading(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    client = KISRestBaseClient(_config(paper_trading=True), transport=FakeTransport([]))

    assert client._tr_id(real="REAL", demo="DEMO") == "DEMO"


def test_kis_rest_base_client_paginates_with_configured_cursor_keys(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    client = KISRestBaseClient(
        _config(),
        transport=FakeTransport(
            [
                {
                    "rt_cd": "0",
                    "__headers__": {"tr_cont": "M"},
                    "ctx_area_fk100": "next-fk",
                    "ctx_area_nk100": "next-nk",
                },
                {"rt_cd": "0", "__headers__": {"tr_cont": ""}},
            ]
        ),
    )

    pages = client._get_pages(
        "/uapi/paged",
        "TR_ID",
        {"CTX_AREA_FK100": "", "CTX_AREA_NK100": ""},
        fk_key="CTX_AREA_FK100",
        nk_key="CTX_AREA_NK100",
        response_fk_key="ctx_area_fk100",
        response_nk_key="ctx_area_nk100",
    )

    assert len(pages) == 2
    assert client.transport.calls[1]["headers"]["tr_cont"] == "N"
    assert client.transport.calls[1]["params"]["CTX_AREA_FK100"] == "next-fk"
    assert client.transport.calls[1]["params"]["CTX_AREA_NK100"] == "next-nk"


def test_kis_rest_base_client_raises_consistent_request_errors(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    client = KISRestBaseClient(
        _config(),
        transport=FakeTransport([{"rt_cd": "1", "msg_cd": "EGW001", "msg1": "bad request"}]),
    )

    with pytest.raises(ValueError, match="KIS test request failed: EGW001 bad request"):
        client._get("/uapi/test", "TR_ID", {}, error_context="KIS test request")


def test_kis_auth_manager_uses_valid_cached_token_without_reissue(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    cache_path = tmp_path / "kis-token.json"
    cache_path.write_text(
        '{"access_token": "cached-token", "expires_at": "2099-01-01T00:00:00+09:00"}',
        encoding="utf-8",
    )
    transport = FakeTransport([])
    manager = KISAuthManager(_config(token_cache_path=str(cache_path)), transport=transport)

    token = manager.get_access_token()

    assert token.access_token == "cached-token"
    assert token.source == "cache"
    assert transport.calls == []


def test_kis_auth_manager_reissues_expired_cached_token(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    cache_path = tmp_path / "kis-token.json"
    cache_path.write_text(
        '{"access_token": "expired-token", "expires_at": "2000-01-01T00:00:00+09:00"}',
        encoding="utf-8",
    )
    transport = FakeTransport(
        [
            {
                "access_token": "issued-token",
                "access_token_token_expired": "2099-01-01 00:00:00",
            }
        ]
    )
    manager = KISAuthManager(_config(token_cache_path=str(cache_path)), transport=transport)

    token = manager.get_access_token()

    assert token.access_token == "issued-token"
    assert len(transport.calls) == 1
    assert '"issued-token"' in cache_path.read_text(encoding="utf-8")


def test_kis_auth_manager_rechecks_cache_after_lock_before_reissue(monkeypatch, tmp_path):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    cache_path = tmp_path / "kis-token.json"
    cache_path.write_text(
        '{"access_token": "expired-token", "expires_at": "2000-01-01T00:00:00+09:00"}',
        encoding="utf-8",
    )
    transport = FakeTransport(
        [
            {
                "access_token": "issued-token",
                "access_token_token_expired": "2099-01-01 00:00:00",
            }
        ]
    )
    manager = KISAuthManager(_config(token_cache_path=str(cache_path)), transport=transport)
    original_read_cached_token = manager._read_cached_token
    reads = 0

    def read_cached_token_once_expired_then_refreshed():
        nonlocal reads
        reads += 1
        if reads == 2:
            cache_path.write_text(
                '{"access_token": "other-process-token", '
                '"expires_at": "2099-01-01T00:00:00+09:00"}',
                encoding="utf-8",
            )
        return original_read_cached_token()

    monkeypatch.setattr(
        manager,
        "_read_cached_token",
        read_cached_token_once_expired_then_refreshed,
    )

    token = manager.get_access_token()

    assert token.access_token == "other-process-token"
    assert transport.calls == []


def _config(*, paper_trading: bool = False, token_cache_path: str | None = None) -> KISConfig:
    return KISConfig(
        enabled=True,
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
        token_cache_path=token_cache_path,
        broker_products=[BrokerProduct.KIS_DOMESTIC_STOCK],
        paper_trading=paper_trading,
    )


def _instrument() -> TradableInstrument:
    return TradableInstrument(
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
        min_order_quantity=1,
        min_order_notional=1,
    )


class FakeTransport:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def request(
        self,
        method,
        url,
        *,
        headers,
        params=None,
        json_body=None,
        timeout_seconds=10.0,
    ):
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
        return self.payloads.pop(0)

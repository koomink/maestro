from pathlib import Path

import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.config.models import KISConfig
from maestro.execution.brokers.kis.auth import KISAuthManager
from maestro.execution.brokers.kis.rest_client import KISRestReadOnlyClient
from maestro.execution.brokers.kis.service import KISReadOnlyService
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


def _live_readonly_config(tmp_path):
    raw = yaml.safe_load(Path("configs/live_readonly.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "live_readonly.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "live_readonly.jsonl")
    config_path = tmp_path / "source_live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path)


class FakeKISTransport:
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
        raise AssertionError(f"Unexpected KIS fake URL: {url}")

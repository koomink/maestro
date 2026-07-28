from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config
from maestro.config.models import KISConfig
from maestro.core.enums import BrokerProduct, OrderSide, OrderStatus
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.kis.auth import KISAuthManager, _parse_kis_datetime
from maestro.execution.brokers.kis.rest_client import (
    KISRestDomesticStockLiveOrderClient,
    KISRestDomesticStockReadOnlyClient,
    KISRestLiveOrderClient,
    KISRestOverseasStockLiveOrderClient,
    KISRestOverseasStockReadOnlyClient,
    KISRestReadOnlyClient,
    build_kis_rest_live_order_client,
)
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.live_orders import (
    BrokerOrderId,
    BrokerOrderRejectedError,
    LiveOrderCancelRequest,
    LiveOrderModifyRequest,
    LiveOrderRequest,
)
from maestro.monitoring.audit_logger import AuditLogger
from maestro.ops.readonly_refresh import refresh_readonly_accounts
from maestro.state.models import PortfolioState
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


def test_kis_readonly_service_can_store_snapshot_with_shared_run_id(tmp_path):
    config = _live_readonly_config(tmp_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)

    KISReadOnlyService(config.kis, store, audit).fetch_and_store_snapshot(
        config.portfolio.allowed_symbols,
        run_id="run_dashboard_refresh",
    )

    latest = store.load_latest_broker_account_snapshot()
    assert latest is not None
    assert latest["run_id"] == "run_dashboard_refresh"


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


def test_kis_cli_sync_fetches_each_configured_kis_account(tmp_path):
    config = _live_readonly_config(tmp_path)
    raw = config.model_dump(mode="json")
    raw["kis"]["enabled"] = False
    raw["accounts"] = [
        {
            "id": "kis_isa",
            "broker": "kis",
            "environment": "real",
            "enabled": True,
            "provider": "mock",
            "account_id": "MOCK-ISA",
            "broker_products": ["kis_domestic_stock"],
        },
        {
            "id": "kis_brokerage",
            "broker": "kis",
            "environment": "real",
            "enabled": True,
            "provider": "mock",
            "account_id": "MOCK-BROKERAGE",
            "broker_products": ["kis_overseas_stock"],
        },
    ]
    config_path = tmp_path / "multi_account_live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    result = CliRunner().invoke(app, ["kis-sync", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "account_id=kis_isa broker_account_id=MOCK-ISA" in result.output
    assert "account_id=kis_brokerage broker_account_id=MOCK-BROKERAGE" in result.output
    assert "accounts_synced=2" in result.output


def test_readonly_refresh_continues_after_one_account_failure(monkeypatch, tmp_path):
    config = _two_account_readonly_config(tmp_path)

    class Service:
        def __init__(self, account_id, store):
            self.account_id = account_id
            self.store = store

        def fetch_and_store_snapshot(self, symbols, run_id=None):
            del symbols
            if self.account_id == "kis_isa":
                raise ValueError("cash mismatch")
            self.store.save_broker_account_snapshot(
                run_id or "run",
                self.account_id,
                {
                    "account_id": self.account_id,
                    "account": {
                        "account_id": "MOCK-TOSS",
                        "cash": 100.0,
                        "buying_power": 100.0,
                        "positions": [],
                        "fetched_at": datetime.now(UTC).isoformat(),
                        "source": "fixture",
                    },
                },
            )

    monkeypatch.setattr(
        "maestro.ops.readonly_refresh.build_broker_readonly_service",
        lambda config, store, audit, account_id: Service(account_id, store),
    )

    report = refresh_readonly_accounts(config, None, source="test", attempts=1)

    assert [result.account_id for result in report.results] == ["kis_isa", "toss_brokerage"]
    assert report.results[0].snapshot_id is None
    assert report.results[1].snapshot_id is not None
    persisted = StateStore(config.state.sqlite_path, 0)
    assert len(persisted.list_broker_account_snapshots()) == 1
    provenance = persisted.list_system_events_by_type("run_provenance")[0]["payload"]
    assert provenance["run_kind"] == "readonly_refresh"
    assert provenance["deployment_commit"]
    assert provenance["account_ids"] == ["kis_isa", "toss_brokerage"]


def test_readonly_refresh_retries_timeout_but_not_business_error(monkeypatch, tmp_path):
    config = _two_account_readonly_config(tmp_path)
    calls = {"kis_isa": 0, "toss_brokerage": 0}

    class Service:
        def __init__(self, account_id):
            self.account_id = account_id

        def fetch_and_store_snapshot(self, symbols, run_id=None):
            del symbols, run_id
            calls[self.account_id] += 1
            if self.account_id == "kis_isa" and calls[self.account_id] == 1:
                raise TimeoutError("timed out")
            raise ValueError("HTTP 400 business error")

    monkeypatch.setattr(
        "maestro.ops.readonly_refresh.build_broker_readonly_service",
        lambda config, store, audit, account_id: Service(account_id),
    )
    monkeypatch.setattr("maestro.ops.readonly_refresh.time.sleep", lambda seconds: None)

    refresh_readonly_accounts(config, None, source="test", attempts=3)

    assert calls == {"kis_isa": 2, "toss_brokerage": 1}


def test_readonly_refresh_cache_boundary_uses_configured_age(monkeypatch, tmp_path):
    config = _two_account_readonly_config(tmp_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_broker_account_snapshot(
        "baseline",
        "kis_isa",
        {"account_id": "kis_isa", "account": {"account_id": "MOCK-ISA"}},
    )
    row = store.load_latest_broker_account_snapshot()
    store.save_system_event(
        "baseline",
        "broker_reconciliation",
        {
            "passed": True,
            "broker_snapshot_id": row["id"],
            "account_results": [
                {
                    "account_id": "kis_isa",
                    "passed": True,
                    "broker_snapshot_id": row["id"],
                }
            ],
        },
    )
    created_at = datetime.fromisoformat(row["created_at"]).replace(tzinfo=UTC)
    calls = 0

    class Service:
        def fetch_and_store_snapshot(self, symbols, run_id=None):
            nonlocal calls
            del symbols, run_id
            calls += 1
            raise ValueError("forced refresh")

    monkeypatch.setattr(
        "maestro.ops.readonly_refresh.build_broker_readonly_service",
        lambda *args, **kwargs: Service(),
    )
    monkeypatch.setattr(
        "maestro.ops.readonly_refresh.utc_now",
        lambda: created_at + timedelta(seconds=900),
    )
    cached = refresh_readonly_accounts(
        config,
        None,
        account_ids=["kis_isa"],
        source="signal",
        max_snapshot_age_seconds=900,
    )
    monkeypatch.setattr(
        "maestro.ops.readonly_refresh.utc_now",
        lambda: created_at + timedelta(seconds=901),
    )
    stale = refresh_readonly_accounts(
        config,
        None,
        account_ids=["kis_isa"],
        source="signal",
        max_snapshot_age_seconds=900,
        attempts=1,
    )

    assert cached.results[0].status == "cached"
    assert stale.results[0].status == "quarantined"
    assert calls == 1


def test_kis_cli_sync_reports_missing_credentials_without_traceback(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAESTRO_ENV_FILE", str(tmp_path / "missing_maestro.env"))
    monkeypatch.delenv("KIS_MOCK_APP_KEY", raising=False)
    monkeypatch.delenv("KIS_MOCK_APP_SECRET", raising=False)
    monkeypatch.delenv("KIS_MOCK_ACCOUNT_ID", raising=False)
    config_path = tmp_path / "maestro_personal.yaml"
    init_result = CliRunner().invoke(app, ["init-personal", "--output", str(config_path)])
    assert init_result.exit_code == 0, init_result.output

    result = CliRunner().invoke(app, ["kis-sync", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "kis-sync failed" in result.output
    assert "KIS_MOCK_APP_KEY" in result.output
    assert "KIS_MOCK_APP_SECRET" in result.output
    assert "Traceback" not in result.output


def test_live_smoke_kis_readonly_allows_mock_only_when_explicit(tmp_path):
    config = _live_readonly_config(tmp_path)
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))

    result = CliRunner().invoke(app, ["live-smoke", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "requires kis.provider=kis" in result.output
    assert "unless --allow-mock is set" in result.output


def test_live_smoke_kis_readonly_runs_sync_and_reconciliation_with_mock(tmp_path):
    config = _live_readonly_config(tmp_path)
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_portfolio_snapshot(
        "run_existing",
        PortfolioState(
            cash=5_000_000.0,
            positions={"MOCK_ETF_A": 30_000.0, "MOCK_ETF_B": 40_000.0},
        ),
    )
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))

    result = CliRunner().invoke(
        app,
        ["live-smoke", "--config", str(config_path), "--check", "kis-readonly", "--allow-mock"],
    )

    assert result.exit_code == 0
    assert "check=kis_readonly_snapshot status=ok provider=mock" in result.output
    assert "account_id=MOCK-ACCOUNT" in result.output
    assert "check=broker_reconciliation status=ok" in result.output


def test_live_approval_config_can_run_readonly_sync_and_reconciliation_with_mock(tmp_path):
    config = _live_readonly_config(tmp_path)
    raw = config.model_dump(mode="json")
    raw["mode"] = "live_approval"
    raw["approval"]["enabled"] = True
    raw["approval"]["require_approval"] = True
    config_path = tmp_path / "live_approval_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    runner = CliRunner()

    sync_result = runner.invoke(app, ["kis-sync", "--config", str(config_path)])
    adopt_result = runner.invoke(
        app,
        [
            "adopt-broker-snapshot",
            "--config",
            str(config_path),
            "--reason",
            "operator baseline rehearsal",
        ],
    )
    reconcile_result = runner.invoke(app, ["reconcile", "--config", str(config_path)])

    assert sync_result.exit_code == 0, sync_result.output
    assert adopt_result.exit_code == 0, adopt_result.output
    assert reconcile_result.exit_code == 0, reconcile_result.output
    assert "status=passed" in reconcile_result.output


def test_kis_cli_reconcile_refreshes_broker_snapshot_before_compare(tmp_path):
    config = _live_readonly_config(tmp_path)
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_portfolio_snapshot(
        "run_current_maestro",
        PortfolioState(
            cash=5_000_000.0,
            cash_by_currency={"KRW": 5_000_000.0},
            positions={"MOCK_ETF_A": 30_000.0, "MOCK_ETF_B": 40_000.0},
        ),
    )
    store.save_broker_account_snapshot(
        "run_stale_broker",
        "MOCK-ACCOUNT",
        {
            "account": {
                "account_id": "MOCK-ACCOUNT",
                "cash": 10_000_000.0,
                "cash_by_currency": {"KRW": 10_000_000.0},
                "buying_power": 10_000_000.0,
                "positions": [],
                "source": "stale_fixture",
            },
            "current_prices": {},
            "order_fills": [],
            "unfilled_orders": [],
        },
    )

    result = CliRunner().invoke(app, ["reconcile", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert "status=passed" in result.output
    assert store.status()["counts"]["broker_account_snapshots"] == 2
    latest = store.load_latest_broker_account_snapshot()
    assert latest is not None
    assert latest["payload"]["account"]["source"] == "kis_mock"


def test_single_broker_products_list_selects_readonly_product_without_broker_product_default(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("KIS_MOCK_APP_KEY", "app-key")
    monkeypatch.setenv("KIS_MOCK_APP_SECRET", "app-secret")
    monkeypatch.setenv("KIS_MOCK_ACCOUNT_ID", "12345678-01")
    config = load_config("tests/fixtures/configs/live_approval_tranquillo_kis_paper_trading.yaml")
    raw_kis = config.kis.model_dump(mode="json")
    assert "broker_product" not in raw_kis
    kis_config = KISConfig.model_validate(raw_kis)
    store = StateStore(str(tmp_path / "state.db"), config.portfolio.initial_cash)
    audit = AuditLogger(str(tmp_path / "audit.jsonl"))

    service = KISReadOnlyService(
        kis_config,
        store,
        audit,
        instruments=config.universe.instruments,
    )

    assert isinstance(service.client, KISRestDomesticStockReadOnlyClient)


def test_adopt_broker_snapshot_seeds_portfolio_for_reconciliation(tmp_path):
    config = _live_readonly_config(tmp_path)
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    runner = CliRunner()

    sync_result = runner.invoke(app, ["kis-sync", "--config", str(config_path)])
    adopt_result = runner.invoke(
        app,
        [
            "adopt-broker-snapshot",
            "--config",
            str(config_path),
            "--reason",
            "operator baseline rehearsal",
        ],
    )
    reconcile_result = runner.invoke(app, ["reconcile", "--config", str(config_path)])

    assert sync_result.exit_code == 0
    assert adopt_result.exit_code == 0
    assert "adopted run_id=" in adopt_result.output
    assert "broker_snapshot_id=1" in adopt_result.output
    assert reconcile_result.exit_code == 0
    assert "status=passed" in reconcile_result.output

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    state = store.load_latest_portfolio_state()
    assert state.cash == 5_000_000.0
    assert state.positions == {"MOCK_ETF_A": 30_000.0, "MOCK_ETF_B": 40_000.0}
    events = store.list_system_events_by_type("broker_snapshot_adopted")
    assert events[0]["payload"]["reason"] == "operator baseline rehearsal"


def test_adopt_broker_snapshot_can_scope_multi_account_baseline(tmp_path):
    config = _two_account_readonly_config(tmp_path)
    config_path = tmp_path / "multi_account_live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(config.model_dump(mode="json")))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_portfolio_snapshot(
        "run_aggregate",
        PortfolioState(cash=999.0, cash_by_currency={"KRW": 999.0}, positions={}),
    )
    store.save_broker_account_snapshot(
        "run_isa",
        "MOCK-ISA",
        {
            "account_id": "kis_isa",
            "account": {
                "account_id": "MOCK-ISA",
                "cash": 125.0,
                "cash_by_currency": {"KRW": 125.0},
                "positions": [],
            },
        },
    )
    store.save_broker_account_snapshot(
        "run_other",
        "MOCK-TOSS",
        {
            "account_id": "toss_brokerage",
            "account": {
                "account_id": "MOCK-TOSS",
                "cash": 500.0,
                "cash_by_currency": {"KRW": 500.0},
                "positions": [],
            },
        },
    )

    result = CliRunner().invoke(
        app,
        [
            "adopt-broker-snapshot",
            "--config",
            str(config_path),
            "--account-id",
            "kis_isa",
            "--reason",
            "settled cash accepted",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "account_id=kis_isa cash=125.00" in result.output
    assert store.load_latest_account_portfolio_state("kis_isa").cash == 125.0
    assert store.load_latest_portfolio_state().cash == 999.0
    event = store.list_system_events_by_type("broker_snapshot_adopted")[0]
    assert event["payload"]["account_id"] == "kis_isa"
    assert event["payload"]["broker_snapshot_id"] == 1


def test_adopt_broker_snapshot_rejects_positions_outside_allowed_symbols(tmp_path):
    config = _live_readonly_config(tmp_path)
    raw = config.model_dump(mode="json")
    raw["portfolio"]["allowed_symbols"] = ["CASH", "MOCK_ETF_A"]
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    runner = CliRunner()

    sync_result = runner.invoke(app, ["kis-sync", "--config", str(config_path)])
    adopt_result = runner.invoke(
        app,
        [
            "adopt-broker-snapshot",
            "--config",
            str(config_path),
            "--reason",
            "operator baseline rehearsal",
        ],
    )

    assert sync_result.exit_code == 0
    assert adopt_result.exit_code == 2
    assert "positions outside" in adopt_result.output
    assert "universe.instruments" in adopt_result.output
    assert "MOCK_ETF_B" in adopt_result.output


def test_adopt_broker_snapshot_can_include_unknown_readonly_positions(tmp_path):
    config = _live_readonly_config(tmp_path)
    raw = config.model_dump(mode="json")
    raw["portfolio"]["allowed_symbols"] = ["CASH", "MOCK_ETF_A"]
    raw["portfolio"]["unknown_broker_position_policy"] = "include_readonly"
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    runner = CliRunner()

    sync_result = runner.invoke(app, ["kis-sync", "--config", str(config_path)])
    adopt_result = runner.invoke(
        app,
        [
            "adopt-broker-snapshot",
            "--config",
            str(config_path),
            "--reason",
            "operator baseline rehearsal",
        ],
    )

    assert sync_result.exit_code == 0
    assert adopt_result.exit_code == 0, adopt_result.output
    store = StateStore(str(tmp_path / "live_readonly.db"), initial_cash=None)
    adopted = store.load_latest_portfolio_state()
    assert adopted.positions == {"MOCK_ETF_A": 30_000.0, "MOCK_ETF_B": 40_000.0}


def test_adopt_broker_snapshot_accepts_universe_position_outside_allowed_symbols(tmp_path):
    config = _live_readonly_config(tmp_path)
    raw = config.model_dump(mode="json")
    raw["portfolio"]["allowed_symbols"] = ["CASH", "MOCK_ETF_A"]
    raw["universe"] = {
        "instruments": [
            _instrument("CASH", "cash"),
            _instrument("MOCK_ETF_A", "etf"),
            _instrument("MOCK_ETF_B", "etf"),
        ]
    }
    config_path = tmp_path / "live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    runner = CliRunner()

    sync_result = runner.invoke(app, ["kis-sync", "--config", str(config_path)])
    adopt_result = runner.invoke(
        app,
        [
            "adopt-broker-snapshot",
            "--config",
            str(config_path),
            "--reason",
            "operator baseline rehearsal",
        ],
    )

    assert sync_result.exit_code == 0, sync_result.output
    assert adopt_result.exit_code == 0, adopt_result.output
    assert "positions=2" in adopt_result.output
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    adopted = store.load_latest_portfolio_state()
    assert adopted.positions == {"MOCK_ETF_A": 30_000.0, "MOCK_ETF_B": 40_000.0}


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
    assert snapshot.positions[0].currency == "KRW"
    assert snapshot.positions[0].market_value == 140_000
    assert snapshot.daily_pnl == -56_000.0
    assert snapshot.daily_pnl_by_currency == {"KRW": -56_000.0}
    assert prices == {"005930": 71000.0, "CASH": 1.0}
    assert fills[0].status == "filled"
    assert unfilled[0].status == "open"
    assert all(call["method"] == "GET" for call in transport.calls)
    assert not any("/order-cash" in call["url"] for call in transport.calls)


def test_kis_all_cash_account_snapshot_still_reports_account_level_daily_pnl(monkeypatch):
    """Regression test for the daily live_approval `broker_pnl_unavailable`
    halt: an all-cash KIS account has no positions to derive PnL from, so the
    snapshot must carry the account-level daily PnL (`asst_icdc_amt`) for the
    daily-loss gate to evaluate."""
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

    class AllCashKISTransport(FakeKISTransport):
        def request(self, method, url, **kwargs):
            payload = super().request(method, url, **kwargs)
            if url.endswith("/inquire-balance"):
                payload = dict(payload)
                payload["output1"] = []
            return payload

    client = KISRestReadOnlyClient(config, transport=AllCashKISTransport())
    snapshot = client.get_account_snapshot()

    assert snapshot.positions == []
    assert snapshot.daily_pnl == -56_000.0
    assert snapshot.daily_pnl_by_currency == {"KRW": -56_000.0}


def test_kis_domestic_cash_uses_d2_projection_until_trade_settles(monkeypatch):
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

    class UnsettledKISTransport(FakeKISTransport):
        def request(self, method, url, **kwargs):
            payload = super().request(method, url, **kwargs)
            if url.endswith("/inquire-balance"):
                payload = dict(payload)
                payload["output2"] = [
                    {
                        "dnca_tot_amt": "1000000",
                        "nxdy_excc_amt": "999000",
                        "prvs_rcdl_excc_amt": "929986",
                        "thdt_tlex_amt": "14",
                        "tot_evlu_amt": "999986",
                    }
                ]
            return payload

    snapshot = KISRestReadOnlyClient(
        config,
        transport=UnsettledKISTransport(),
    ).get_account_snapshot()

    assert snapshot.cash == 929_986.0
    assert snapshot.cash_by_currency == {"KRW": 929_986.0}
    assert snapshot.cash_balance is not None
    assert snapshot.cash_balance.withdrawable_cash == 1_000_000.0
    assert snapshot.cash_balance.settled_cash == 1_000_000.0
    assert snapshot.cash_balance.next_day_cash == 999_000.0
    assert snapshot.cash_balance.projected_settlement_cash == 929_986.0
    assert snapshot.cash_balance.transaction_costs_today == 14.0


def test_kis_token_expiry_without_timezone_is_interpreted_as_seoul_time():
    parsed = _parse_kis_datetime("2026-06-05 09:10:30")

    assert parsed == datetime(2026, 6, 5, 0, 10, 30, tzinfo=UTC)


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
        "/order-resv",
        "def buy",
        "def sell",
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
    assert order_call["headers"]["tr_id"] == "TTTC0012U"
    assert order_call["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert order_call["json_body"] == {
        "CANO": "12345678",
        "ACNT_PRDT_CD": "01",
        "PDNO": "005930",
        "ORD_DVSN": "00",
        "ORD_QTY": "2",
        "ORD_UNPR": "70000",
    }


def test_kis_domestic_live_order_raises_definitive_rejection(monkeypatch):
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
    client = KISRestLiveOrderClient(
        config,
        transport=FakeKISTransport(order_error=("APBK1497", "파생ETF 거래 불가")),
    )

    with pytest.raises(BrokerOrderRejectedError) as exc_info:
        client.submit_limit_order(
            LiveOrderRequest(
                order_id="ord_live_rejected",
                symbol="005930",
                side=OrderSide.BUY,
                quantity=2,
                limit_price=70000,
                approval_id="appr_1",
                run_id="run_1",
            )
        )

    assert exc_info.value.code == "APBK1497"
    assert exc_info.value.message == "파생ETF 거래 불가"


def test_kis_domestic_paper_trading_live_order_uses_vts_endpoint_and_demo_tr_id(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_DOMESTIC_STOCK],
        paper_trading=True,
    )
    transport = FakeKISTransport()
    client = KISRestDomesticStockLiveOrderClient(config, transport=transport)

    client.submit_limit_order(
        LiveOrderRequest(
            order_id="ord_live_paper_1",
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
    assert order_call["url"].startswith("https://openapivts.koreainvestment.com:29443/")
    assert order_call["headers"]["tr_id"] == "VTTC0012U"
    assert order_call["headers"]["Content-Type"] == "application/json; charset=utf-8"
    assert order_call["json_body"] == {
        "CANO": "12345678",
        "ACNT_PRDT_CD": "01",
        "PDNO": "005930",
        "ORD_DVSN": "00",
        "ORD_QTY": "2",
        "ORD_UNPR": "70000",
    }


def test_kis_domestic_paper_trading_sell_order_uses_demo_tr_id(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_DOMESTIC_STOCK],
        paper_trading=True,
    )
    transport = FakeKISTransport()
    client = KISRestDomesticStockLiveOrderClient(config, transport=transport)

    client.submit_limit_order(
        LiveOrderRequest(
            order_id="ord_live_paper_2",
            symbol="005930",
            side=OrderSide.SELL,
            quantity=1,
            limit_price=70000,
            approval_id="appr_1",
            run_id="run_1",
        )
    )

    order_call = [call for call in transport.calls if call["url"].endswith("/trading/order-cash")][
        0
    ]
    assert order_call["headers"]["tr_id"] == "VTTC0011U"


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
        broker_products=[BrokerProduct.KIS_DOMESTIC_STOCK],
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


def test_kis_domestic_adapter_maps_broker_symbol_to_canonical_symbol(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    config = KISConfig(
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
        broker_products=[BrokerProduct.KIS_DOMESTIC_STOCK],
    )
    client = KISRestDomesticStockLiveOrderClient(
        config,
        transport=FakeKISTransport(),
        instruments=[
            TradableInstrument(
                symbol="KODEX_US_DIVIDEND_DOWJONES",
                asset_type="domestic_etf",
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

    snapshot = client.get_account_snapshot()
    fills = client.get_order_fills()
    unfilled = client.get_unfilled_orders()
    status = client.get_order_status(_broker_order("0001"))

    assert snapshot.positions[0].symbol == "KODEX_US_DIVIDEND_DOWJONES"
    assert fills[0].symbol == "KODEX_US_DIVIDEND_DOWJONES"
    assert unfilled[0].symbol == "KODEX_US_DIVIDEND_DOWJONES"
    assert status.symbol == "KODEX_US_DIVIDEND_DOWJONES"
    assert status.fills[0].symbol == "KODEX_US_DIVIDEND_DOWJONES"


def test_kis_domestic_pre_submit_uses_broker_symbol_and_order_price(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    config = KISConfig(
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
        broker_products=[BrokerProduct.KIS_DOMESTIC_STOCK],
    )
    transport = FakeKISTransport()
    client = KISRestDomesticStockLiveOrderClient(
        config,
        transport=transport,
        instruments=[
            TradableInstrument(
                symbol="TIGER_NASDAQ100_LEVERAGE",
                asset_type="domestic_etf",
                region="KR",
                currency="KRW",
                broker="kis",
                broker_product="kis_domestic_stock",
                broker_symbol="418660",
                exchange_code="KRX",
                quantity_step=1,
                price_tick=1,
            )
        ],
    )

    client.validate_pre_submit_order(
        LiveOrderRequest(
            order_id="ord_live_kr_1",
            symbol="TIGER_NASDAQ100_LEVERAGE",
            side=OrderSide.BUY,
            quantity=2,
            limit_price=70000,
            approval_id="appr_1",
            run_id="run_1",
        )
    )

    buying_power_call = [
        call for call in transport.calls if call["url"].endswith("/inquire-psbl-order")
    ][0]
    assert buying_power_call["params"]["PDNO"] == "418660"
    assert buying_power_call["params"]["ORD_UNPR"] == "70000"
    assert buying_power_call["params"]["ORD_DVSN"] == "00"


def test_kis_domestic_pre_submit_rejects_insufficient_buying_power(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    config = KISConfig(
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
        broker_products=[BrokerProduct.KIS_DOMESTIC_STOCK],
    )
    client = KISRestDomesticStockLiveOrderClient(
        config,
        transport=FakeKISTransport(buying_power="100000", max_buy_quantity="1"),
    )

    try:
        client.validate_pre_submit_order(
            LiveOrderRequest(
                order_id="ord_live_kr_1",
                symbol="005930",
                side=OrderSide.BUY,
                quantity=2,
                limit_price=70000,
                approval_id="appr_1",
                run_id="run_1",
            )
        )
    except ValueError as exc:
        assert "buying power" in str(exc)
    else:
        raise AssertionError("Expected KIS domestic buying power validation to fail closed")


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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
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
    assert snapshot.positions[0].currency == "USD"
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
    price_calls = [call for call in transport.calls if call["url"].endswith("/quotations/price")]
    assert {call["params"]["EXCD"] for call in price_calls} == {"NAS", "AMS"}
    assert not any(call["method"] != "GET" for call in transport.calls)
    assert not any(
        "/order" in call["url"] and "inquire" not in call["url"] for call in transport.calls
    )


def test_kis_overseas_readonly_avoids_unsupported_demo_unfilled_order_api(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
        paper_trading=True,
    )
    transport = FakeKISOverseasTransport()
    client = KISRestOverseasStockReadOnlyClient(
        config,
        transport=transport,
        instruments=_us_instruments(),
    )

    client.get_unfilled_orders()

    assert any(call["headers"]["tr_id"] == "VTTS3035R" for call in transport.calls)
    assert not any(call["url"].endswith("/inquire-nccs") for call in transport.calls)


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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
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


def test_kis_overseas_live_order_client_uses_verified_us_limit_order_payload(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
    )
    transport = FakeKISOverseasTransport()
    client = KISRestOverseasStockLiveOrderClient(
        config,
        transport=transport,
        instruments=_us_instruments(),
    )

    result = client.submit_limit_order(
        LiveOrderRequest(
            order_id="ord_live_us_1",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=2,
            limit_price=191.25,
            approval_id="appr_1",
            run_id="run_1",
        )
    )

    order_call = [call for call in transport.calls if call["url"].endswith("/trading/order")][0]
    assert result.status == OrderStatus.ACCEPTED_BY_BROKER
    assert result.broker_order is not None
    assert result.broker_order.broker_order_id == "9003"
    assert order_call["method"] == "POST"
    assert order_call["headers"]["tr_id"] == "TTTT1002U"
    assert order_call["json_body"] == {
        "CANO": "12345678",
        "ACNT_PRDT_CD": "01",
        "OVRS_EXCG_CD": "NASD",
        "PDNO": "AAPL",
        "ORD_QTY": "2",
        "OVRS_ORD_UNPR": "191.25",
        "CTAC_TLNO": "",
        "MGCO_APTM_ODNO": "",
        "SLL_TYPE": "",
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": "00",
    }


def test_kis_overseas_pre_submit_uses_order_price_for_buying_power(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
    )
    transport = FakeKISOverseasTransport()
    client = KISRestOverseasStockLiveOrderClient(
        config,
        transport=transport,
        instruments=_us_instruments(),
    )

    client.validate_pre_submit_order(
        LiveOrderRequest(
            order_id="ord_live_us_1",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=2,
            limit_price=191.25,
            approval_id="appr_1",
            run_id="run_1",
        )
    )

    buying_power_call = [
        call for call in transport.calls if call["url"].endswith("/inquire-psamount")
    ][0]
    assert buying_power_call["params"]["OVRS_ORD_UNPR"] == "191.25"
    assert buying_power_call["params"]["ITEM_CD"] == "AAPL"


def test_kis_overseas_pre_submit_rejects_insufficient_max_buy_quantity(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
    )
    client = KISRestOverseasStockLiveOrderClient(
        config,
        transport=FakeKISOverseasTransport(max_buy_quantity="1"),
        instruments=_us_instruments(),
    )

    try:
        client.validate_pre_submit_order(
            LiveOrderRequest(
                order_id="ord_live_us_1",
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=2,
                limit_price=191.25,
                approval_id="appr_1",
                run_id="run_1",
            )
        )
    except ValueError as exc:
        assert "max buy quantity" in str(exc)
    else:
        raise AssertionError("Expected KIS max buy quantity to fail closed")


def test_kis_overseas_live_order_client_uses_demo_sell_tr_id(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
        paper_trading=True,
    )
    transport = FakeKISOverseasTransport()
    client = KISRestOverseasStockLiveOrderClient(
        config,
        transport=transport,
        instruments=_us_instruments(),
    )

    client.submit_limit_order(
        LiveOrderRequest(
            order_id="ord_live_us_2",
            symbol="VOO",
            side=OrderSide.SELL,
            quantity=1,
            limit_price=500.5,
            approval_id="appr_1",
            run_id="run_1",
        )
    )

    order_call = [call for call in transport.calls if call["url"].endswith("/trading/order")][0]
    assert order_call["headers"]["tr_id"] == "VTTT1001U"
    assert order_call["json_body"]["OVRS_EXCG_CD"] == "AMEX"
    assert order_call["json_body"]["SLL_TYPE"] == "00"


def test_kis_overseas_live_order_client_normalizes_order_status(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
    )
    client = KISRestOverseasStockLiveOrderClient(
        config,
        transport=FakeKISOverseasTransport(),
        instruments=_us_instruments(),
    )

    open_snapshot = client.get_order_status(_overseas_broker_order("9002"))
    filled_snapshot = client.get_order_status(_overseas_broker_order("9001"))

    assert open_snapshot.status == OrderStatus.OPEN
    assert open_snapshot.symbol == "VOO"
    assert open_snapshot.partial_fill.remaining_quantity == 1.0
    assert filled_snapshot.status == OrderStatus.FILLED
    assert filled_snapshot.symbol == "AAPL"
    assert filled_snapshot.fills[0].price == 189.5


def test_kis_overseas_order_status_uses_submitted_at_exchange_date_range(monkeypatch):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    monkeypatch.setattr(
        "maestro.execution.brokers.kis.overseas_readonly.utc_now",
        lambda: datetime(2026, 5, 11, 3, 0, tzinfo=UTC),
    )
    config = KISConfig(
        enabled=True,
        provider="kis",
        account_id="12345678-01",
        app_key_env="TEST_KIS_APP_KEY",
        app_secret_env="TEST_KIS_APP_SECRET",
        access_token_env="TEST_KIS_ACCESS_TOKEN",
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
    )
    transport = FakeKISOverseasTransport()
    client = KISRestOverseasStockLiveOrderClient(
        config,
        transport=transport,
        instruments=_us_instruments(),
    )

    client.get_order_status(
        _overseas_broker_order("9001").model_copy(
            update={"submitted_at": "2026-05-10T02:30:00+00:00"}
        )
    )

    ccnl_call = [call for call in transport.calls if call["url"].endswith("/inquire-ccnl")][0]
    assert ccnl_call["params"]["ORD_STRT_DT"] == "20260509"
    assert ccnl_call["params"]["ORD_END_DT"] == "20260510"
    assert ccnl_call["params"]["CCLD_NCCS_DVSN"] == "00"


def test_kis_overseas_live_order_client_fails_on_kis_error_response(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
    )
    client = KISRestOverseasStockLiveOrderClient(
        config,
        transport=FakeKISOverseasTransport(order_rt_cd="1"),
        instruments=_us_instruments(),
    )

    try:
        client.submit_limit_order(
            LiveOrderRequest(
                order_id="ord_live_us_1",
                symbol="AAPL",
                side=OrderSide.BUY,
                quantity=2,
                limit_price=191.25,
                approval_id="appr_1",
                run_id="run_1",
            )
        )
    except BrokerOrderRejectedError as exc:
        assert exc.code == "EGW001"
        assert exc.message == "rejected"
    else:
        raise AssertionError("Expected KIS error response to fail closed")


def test_kis_overseas_live_order_client_unknown_when_order_id_missing(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
    )
    client = KISRestOverseasStockLiveOrderClient(
        config,
        transport=FakeKISOverseasTransport(missing_order_id=True),
        instruments=_us_instruments(),
    )

    result = client.submit_limit_order(
        LiveOrderRequest(
            order_id="ord_live_us_1",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=2,
            limit_price=191.25,
            approval_id="appr_1",
            run_id="run_1",
        )
    )

    assert result.status == OrderStatus.UNKNOWN
    assert result.broker_order is None


def test_kis_overseas_order_status_fails_on_malformed_numeric_field(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
    )
    client = KISRestOverseasStockLiveOrderClient(
        config,
        transport=FakeKISOverseasTransport(malformed_order=True),
        instruments=_us_instruments(),
    )

    try:
        client.get_order_status(_overseas_broker_order("9002"))
    except ValueError as exc:
        assert "Malformed KIS numeric field" in str(exc)
    else:
        raise AssertionError("Expected malformed KIS numeric field to fail closed")


def test_kis_overseas_cancel_adapter_uses_verified_cancel_payload(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_OVERSEAS_STOCK],
    )
    transport = FakeKISOverseasTransport()
    client = KISRestOverseasStockLiveOrderClient(
        config,
        transport=transport,
        instruments=_us_instruments(),
    )

    result = client.cancel_order(
        LiveOrderCancelRequest(
            run_id="run_1",
            approval_id="appr_1",
            broker_order=_overseas_broker_order("9002"),
            reason="operator approved cancel",
        )
    )

    cancel_call = [
        call for call in transport.calls if call["url"].endswith("/trading/order-rvsecncl")
    ][0]
    assert result.status == OrderStatus.CANCELED
    assert result.canceled_quantity == 1.0
    assert cancel_call["method"] == "POST"
    assert cancel_call["headers"]["tr_id"] == "TTTT1004U"
    assert cancel_call["json_body"] == {
        "CANO": "12345678",
        "ACNT_PRDT_CD": "01",
        "OVRS_EXCG_CD": "AMEX",
        "PDNO": "VOO",
        "ORGN_ODNO": "9002",
        "RVSE_CNCL_DVSN_CD": "02",
        "ORD_QTY": "1",
        "OVRS_ORD_UNPR": "0",
        "MGCO_APTM_ODNO": "",
        "ORD_SVR_DVSN_CD": "0",
    }


def test_kis_domestic_modify_uses_verified_revision_payload(monkeypatch):
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
        broker_products=[BrokerProduct.KIS_DOMESTIC_STOCK],
    )
    transport = FakeKISTransport()
    client = KISRestDomesticStockLiveOrderClient(
        config,
        transport=transport,
        instruments=_domestic_instruments(),
    )
    original = BrokerOrderId(
        broker="kis",
        broker_order_id="0002",
        broker_order_org_no="91255",
        order_id="ord_1",
        submitted_at="2026-07-15T04:00:00+00:00",
        account_id="kis_ps",
        broker_product=BrokerProduct.KIS_DOMESTIC_STOCK,
    )

    result = client.modify_order(
        LiveOrderModifyRequest(
            run_id="run_modify",
            approval_id="appr_modify",
            broker_order=original,
            symbol="005930",
            limit_price=71000,
            quantity=2,
        )
    )

    call = [call for call in transport.calls if call["url"].endswith("/trading/order-rvsecncl")][0]
    assert call["headers"]["tr_id"] == "TTTC0013U"
    assert call["json_body"] == {
        "CANO": "12345678",
        "ACNT_PRDT_CD": "01",
        "KRX_FWDG_ORD_ORGNO": "91255",
        "ORGN_ODNO": "0002",
        "ORD_DVSN": "00",
        "RVSE_CNCL_DVSN_CD": "01",
        "ORD_QTY": "2",
        "ORD_UNPR": "71000",
        "QTY_ALL_ORD_YN": "N",
        "EXCG_ID_DVSN_CD": "KRX",
    }
    assert result.broker_order.account_id == "kis_ps"
    assert result.broker_order.parent_broker_order_id == "0002"


def test_kis_domestic_modify_omitted_quantity_revises_full_remainder(monkeypatch):
    client = _kis_domestic_modify_client(monkeypatch, FakeKISTransport())

    client.modify_order(
        LiveOrderModifyRequest(
            run_id="run_modify",
            approval_id="appr_modify",
            broker_order=_domestic_modifiable_broker_order(),
            symbol="005930",
            limit_price=71000,
        )
    )

    call = [
        call for call in client.transport.calls if call["url"].endswith("/trading/order-rvsecncl")
    ][0]
    assert call["json_body"]["ORD_QTY"] == "3"
    assert call["json_body"]["QTY_ALL_ORD_YN"] == "Y"


def _kis_domestic_modify_client(monkeypatch, transport):
    monkeypatch.setenv("TEST_KIS_APP_KEY", "app-key")
    monkeypatch.setenv("TEST_KIS_APP_SECRET", "app-secret")
    monkeypatch.setenv("TEST_KIS_ACCESS_TOKEN", "access-token")
    return KISRestDomesticStockLiveOrderClient(
        KISConfig(
            enabled=True,
            provider="kis",
            account_id="12345678-01",
            app_key_env="TEST_KIS_APP_KEY",
            app_secret_env="TEST_KIS_APP_SECRET",
            access_token_env="TEST_KIS_ACCESS_TOKEN",
            broker_products=[BrokerProduct.KIS_DOMESTIC_STOCK],
        ),
        transport=transport,
        instruments=_domestic_instruments(),
    )


def _domestic_modifiable_broker_order():
    return BrokerOrderId(
        broker="kis",
        broker_order_id="0002",
        broker_order_org_no="91255",
        order_id="ord_1",
        submitted_at="2026-07-15T04:00:00+00:00",
        account_id="kis_ps",
        broker_product=BrokerProduct.KIS_DOMESTIC_STOCK,
    )


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


def test_kis_domestic_order_summary_uses_continuation_pages(monkeypatch):
    client = _kis_live_order_client(monkeypatch, FakeKISTransport(paginated_daily=True))

    fills = client.get_order_fills()

    daily_calls = [
        call for call in client.transport.calls if call["url"].endswith("/inquire-daily-ccld")
    ]
    assert [summary.order_id for summary in fills] == ["0001", "0006"]
    assert daily_calls[0]["headers"]["tr_cont"] == ""
    assert daily_calls[1]["headers"]["tr_cont"] == "N"
    assert daily_calls[1]["params"]["CTX_AREA_FK100"] == "next-fk"
    assert daily_calls[1]["params"]["CTX_AREA_NK100"] == "next-nk"


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
    raw = yaml.safe_load(Path("tests/fixtures/configs/live_readonly_mock.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "live_readonly.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "live_readonly.jsonl")
    config_path = tmp_path / "source_live_readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return load_config(config_path)


def _two_account_readonly_config(tmp_path):
    config = _live_readonly_config(tmp_path)
    raw = config.model_dump(mode="json")
    raw["kis"]["enabled"] = False
    raw["accounts"] = [
        {
            "id": "kis_isa",
            "broker": "kis",
            "environment": "real",
            "enabled": True,
            "provider": "mock",
            "account_id": "MOCK-ISA",
            "broker_products": ["kis_domestic_stock"],
        },
        {
            "id": "toss_brokerage",
            "broker": "kis",
            "environment": "real",
            "enabled": True,
            "provider": "mock",
            "account_id": "MOCK-TOSS",
            "broker_products": ["kis_overseas_stock"],
        },
    ]
    path = tmp_path / "two_account.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_config(path)


def _instrument(symbol: str, asset_type: str) -> dict:
    return {
        "symbol": symbol,
        "asset_type": asset_type,
        "region": "US",
        "currency": "USD",
        "broker": "kis",
        "broker_product": "kis_overseas_stock",
        "broker_symbol": symbol,
        "exchange_code": "NASD" if asset_type != "cash" else None,
        "quantity_step": 1,
        "price_tick": 0.01,
    }


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


def _overseas_broker_order(order_id: str) -> BrokerOrderId:
    return BrokerOrderId(
        broker="kis",
        broker_order_id=order_id,
        broker_order_org_no="NASD",
        order_id="ord_live_us_1",
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


def _domestic_instruments() -> list[TradableInstrument]:
    return [
        TradableInstrument(
            symbol="005930",
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
    ]


class FakeKISTransport:
    def __init__(
        self,
        *,
        partial_order: bool = False,
        rejected_order: bool = False,
        canceled_order: bool = False,
        paginated_daily: bool = False,
        buying_power: str = "750000",
        max_buy_quantity: str = "10",
        order_error: tuple[str, str] | None = None,
    ) -> None:
        self.calls = []
        self.partial_order = partial_order
        self.rejected_order = rejected_order
        self.canceled_order = canceled_order
        self.paginated_daily = paginated_daily
        self.buying_power = buying_power
        self.max_buy_quantity = max_buy_quantity
        self.order_error = order_error

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
                        "prvs_rcdl_excc_amt": "1000000",
                        "thdt_tlex_amt": "0",
                        "asst_icdc_amt": "-56000",
                    }
                ],
            }
        if url.endswith("/inquire-psbl-order"):
            return {
                "rt_cd": "0",
                "output": {
                    "nrcvb_buy_amt": self.buying_power,
                    "nrcvb_buy_qty": self.max_buy_quantity,
                },
            }
        if url.endswith("/inquire-psbl-rvsecncl"):
            return {
                "rt_cd": "0",
                "output1": [
                    {
                        "odno": "0002",
                        "ord_gno_brno": "91255",
                        "psbl_qty": "3",
                        "excg_id_dvsn_cd": "KRX",
                    }
                ],
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
            if self.paginated_daily:
                if headers.get("tr_cont") == "N":
                    return {
                        "rt_cd": "0",
                        "__headers__": {"tr_cont": "D"},
                        "output1": [
                            {
                                "odno": "0006",
                                "pdno": "005930",
                                "sll_buy_dvsn_cd": "02",
                                "ord_qty": "1",
                                "tot_ccld_qty": "1",
                                "avg_prvs": "72000",
                            }
                        ],
                    }
                return {
                    "rt_cd": "0",
                    "__headers__": {"tr_cont": "M"},
                    "ctx_area_fk100": "next-fk",
                    "ctx_area_nk100": "next-nk",
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
            if self.order_error is not None:
                code, message = self.order_error
                return {"rt_cd": "1", "msg_cd": code, "msg1": message}
            return {
                "rt_cd": "0",
                "msg1": "order accepted",
                "output": {
                    "KRX_FWDG_ORD_ORGNO": "KRX",
                    "ODNO": "0000000001",
                    "ORD_TMD": "090001",
                },
            }
        if url.endswith("/trading/order-rvsecncl"):
            return {
                "rt_cd": "0",
                "msg1": "revision accepted",
                "output": {
                    "KRX_FWDG_ORD_ORGNO": "91255",
                    "ODNO": "0000000007",
                    "ORD_TMD": "090003",
                },
            }
        raise AssertionError(f"Unexpected KIS fake URL: {url}")


class FakeKISOverseasTransport:
    def __init__(
        self,
        *,
        order_rt_cd: str = "0",
        missing_order_id: bool = False,
        malformed_order: bool = False,
        buying_power: str = "1500.00",
        max_buy_quantity: str = "7",
    ) -> None:
        self.calls = []
        self.order_rt_cd = order_rt_cd
        self.missing_order_id = missing_order_id
        self.malformed_order = malformed_order
        self.buying_power = buying_power
        self.max_buy_quantity = max_buy_quantity

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
                    "ovrs_ord_psbl_amt": self.buying_power,
                    "max_ord_psbl_qty": self.max_buy_quantity,
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
            order_quantity = "bad" if self.malformed_order else "1"
            return {
                "rt_cd": "0",
                "output": [
                    {
                        "odno": "9002",
                        "pdno": "VOO",
                        "sll_buy_dvsn_cd": "02",
                        "ft_ord_qty": order_quantity,
                        "ft_ccld_qty": "0",
                        "nccs_qty": "1",
                        "ft_ord_unpr3": "500.00",
                    }
                ],
            }
        if url.endswith("/trading/order"):
            if self.order_rt_cd != "0":
                return {"rt_cd": self.order_rt_cd, "msg_cd": "EGW001", "msg1": "rejected"}
            return {
                "rt_cd": "0",
                "msg1": "overseas order accepted",
                "output": {}
                if self.missing_order_id
                else {
                    "ODNO": "9003",
                    "ORD_TMD": "093001",
                },
            }
        if url.endswith("/trading/order-rvsecncl"):
            return {
                "rt_cd": "0",
                "msg1": "cancel accepted",
                "output": {
                    "ODNO": "9002",
                },
            }
        raise AssertionError(f"Unexpected KIS overseas fake URL: {url}")

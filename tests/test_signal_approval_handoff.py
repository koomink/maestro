from pathlib import Path

import pytest
import yaml
from sample_static_allocation.strategy import SampleStaticAllocationStrategy
from typer.testing import CliRunner

from maestro.cli import app
from maestro.config.loader import load_config, load_config_with_identity
from maestro.core.clock import utc_now
from maestro.core.ids import new_run_id
from maestro.execution.brokers.kis.models import KISAccountSnapshot, KISReadOnlySnapshot
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.safety.controls import SafetyControlService
from maestro.state.store import StateStore


class SecondStaticAllocationStrategy(SampleStaticAllocationStrategy):
    def manifest(self):
        return super().manifest().model_copy(
            update={"strategy_id": "second_static", "name": "Second Static Allocation"}
        )

    def run(self, data_bundle, context):
        return super().run(data_bundle, context).model_copy(
            update={"strategy_id": context.strategy_id}
        )


def test_run_signal_persists_immutable_signal_package_without_approval(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    orchestrator = MaestroOrchestrator(config)

    summary = orchestrator.run_signal()

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(summary.signal_run_id)
    assert summary.action_required is True
    assert summary.orders_preview_count == 2
    assert signal["signal_run_id"] == summary.signal_run_id
    assert signal["status"] == "action_required"
    assert signal["orders_preview_count"] == 2
    assert signal["approval_consumed"] is False
    assert store.list_approvals() == []
    assert store.list_orders() == []


def test_approve_signal_uses_saved_package_without_rerunning_strategies(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    strategy_runs_before = store.status()["counts"]["strategy_runs"]

    approval_summary = MaestroOrchestrator(config).approve_signal(
        signal_summary.signal_run_id,
    )

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    assert approval_summary.signal_run_id == signal_summary.signal_run_id
    assert approval_summary.orders_created == 2
    assert store.status()["counts"]["strategy_runs"] == strategy_runs_before
    assert store.status()["counts"]["approvals"] == 1
    assert store.status()["counts"]["orders"] == 2
    assert signal["approval_consumed"] is True
    assert signal["approval_run_id"] == approval_summary.run_id


def test_approve_signal_propagates_signal_run_id_to_live_order_events(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()

    approval_summary = MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    events = store.list_system_events_by_type("live_order_dry_run", limit=10)
    assert events
    assert {event["payload"]["signal_run_id"] for event in events} == {
        signal_summary.signal_run_id
    }
    assert {
        event["payload"]["request"]["signal_run_id"] for event in events
    } == {signal_summary.signal_run_id}
    assert approval_summary.orders_created == 2


def test_approve_signal_excludes_disabled_posture_orders_from_approval(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    config.strategies[0].order_posture = "disabled"
    signal_summary = MaestroOrchestrator(config).run_signal()

    approval_summary = MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    assert approval_summary.orders_created == 0
    assert store.status()["counts"]["approvals"] == 0
    signal = store.load_signal_package(signal_summary.signal_run_id)
    assert signal["orders_preview_count"] == 2
    assert signal["approval_consumed"] is True


def test_signal_false_strategy_is_not_loaded(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    disabled_signal = config.strategies[0].model_copy(
        update={
            "id": "unimportable_dev_strategy",
            "entrypoint": "missing.strategy:MissingStrategy",
            "signal_enabled": False,
            "order_posture": "disabled",
        }
    )
    config.strategies.append(disabled_signal)

    summary = MaestroOrchestrator(config).run_signal()

    assert summary.loaded_strategies == ["sample_static_allocation"]


def test_run_signal_can_filter_to_one_strategy(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")
    second_strategy = config.strategies[0].model_copy(
        update={
            "id": "second_static",
            "entrypoint": f"{__name__}:SecondStaticAllocationStrategy",
            "weight": 1.0,
            "config": {"allocations": {"MOCK_ETF_B": 1.0}},
        }
    )
    config.strategies.append(second_strategy)

    summary = MaestroOrchestrator(config).run_signal(strategy_ids=["second_static"])

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(summary.signal_run_id)
    strategy_runs = store.list_strategy_runs(limit=10)
    assert summary.loaded_strategies == ["second_static"]
    assert signal["loaded_strategies"] == ["second_static"]
    assert [row["strategy_id"] for row in strategy_runs] == ["second_static"]
    assert signal["portfolio_target"]["source_strategy_ids"] == ["second_static"]


def test_approve_signal_rejects_unknown_signal_run_id(tmp_path):
    config = _paper_approval_config(tmp_path, "approved")

    with pytest.raises(ValueError, match="Unknown signal_run_id"):
        MaestroOrchestrator(config).approve_signal("signal_missing")


def test_run_signal_and_approve_signal_cli(tmp_path):
    config_path = _paper_approval_config_path(tmp_path, "approved")

    signal_result = CliRunner().invoke(app, ["run-signal", "--config", str(config_path)])

    assert signal_result.exit_code == 0
    assert "signal_run_id=" in signal_result.output
    signal_run_id = signal_result.output.split("signal_run_id=", 1)[1].split()[0]

    approval_result = CliRunner().invoke(
        app,
        [
            "approve-signal",
            "--config",
            str(config_path),
            "--signal-run-id",
            signal_run_id,
        ],
    )

    assert approval_result.exit_code == 0
    assert f"signal_run_id={signal_run_id}" in approval_result.output
    assert "orders=2" in approval_result.output


def test_live_run_signal_refreshes_broker_truth_and_records_snapshot_refs(
    monkeypatch,
    tmp_path,
):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    orchestrator = MaestroOrchestrator(config)

    summary = orchestrator.run_signal()

    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(summary.signal_run_id)
    snapshots = store.list_broker_account_snapshots()
    assert len(snapshots) == 1
    assert signal["broker_snapshot_refs"] == [
        {
            "id": snapshots[0]["id"],
            "run_id": snapshots[0]["run_id"],
            "account_id": "kis_paper",
            "broker_account_id": "MOCK-LIVE",
            "created_at": snapshots[0]["created_at"],
            "fetched_at": snapshots[0]["payload"]["account"]["fetched_at"],
        }
    ]
    assert signal["datahub_evidence"]["issue_count"] == 0
    assert signal["datahub_evidence"]["price_symbols"] == ["CASH", "MOCK_ETF_A", "MOCK_ETF_B"]
    assert "sample_static_allocation" in signal["datahub_evidence"]["strategies"]


def test_approve_signal_rejects_stale_broker_snapshot_refs(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    signal["broker_snapshot_refs"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
    store.save_signal_package(signal_summary.signal_run_id, signal)

    with pytest.raises(ValueError, match="stale broker snapshot"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_rejects_material_broker_snapshot_change(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    store.save_broker_account_snapshot(
        "run_new_broker_truth",
        "MOCK-LIVE",
        {
            "account_id": "kis_paper",
            "broker_account_id": "MOCK-LIVE",
            "account": {
                "account_id": "MOCK-LIVE",
                "cash": 9_000_000.0,
                "cash_by_currency": {"KRW": 9_000_000.0},
                "buying_power": 9_000_000.0,
                "positions": [],
                "fetched_at": utc_now().isoformat(),
                "source": "kis_mock",
            },
            "current_prices": {},
            "order_fills": [],
            "unfilled_orders": [],
        },
    )

    with pytest.raises(ValueError, match="broker snapshot changed"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_rejects_expired_signal_package(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    signal["generated_at"] = "2000-01-01T00:00:00+00:00"
    store.save_signal_package(signal_summary.signal_run_id, signal)

    with pytest.raises(ValueError, match="expired signal package"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_rejects_config_runtime_mismatch(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    signal_config_path = _live_signal_config_path(tmp_path, "approved")
    approval_config_path = _live_signal_config_path(
        tmp_path,
        "approved",
        filename="changed_signal_approval.yaml",
        strategy_weight=0.5,
    )
    signal_config, signal_identity = load_config_with_identity(signal_config_path)
    approval_config, approval_identity = load_config_with_identity(approval_config_path)
    signal_summary = MaestroOrchestrator(
        signal_config,
        config_identity=signal_identity,
    ).run_signal()

    with pytest.raises(ValueError, match="config runtime mismatch"):
        MaestroOrchestrator(
            approval_config,
            config_identity=approval_identity,
        ).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_rejects_account_mapping_mismatch(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    signal_config_path = _live_signal_config_path(tmp_path, "approved")
    approval_config_path = _live_signal_config_path(
        tmp_path,
        "approved",
        filename="remapped_signal_approval.yaml",
        account_id="kis_other",
    )
    signal_config, signal_identity = load_config_with_identity(signal_config_path)
    approval_config, approval_identity = load_config_with_identity(approval_config_path)
    signal_summary = MaestroOrchestrator(
        signal_config,
        config_identity=signal_identity,
    ).run_signal()
    store = StateStore(signal_config.state.sqlite_path, signal_config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    signal.pop("config_runtime_fingerprint")
    store.save_signal_package(signal_summary.signal_run_id, signal)

    with pytest.raises(ValueError, match="account mapping mismatch"):
        MaestroOrchestrator(
            approval_config,
            config_identity=approval_identity,
        ).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_rechecks_data_quality_gate(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    signal["data_quality_issues"] = [{"symbol": "MOCK_ETF_A", "reason": "stale_price"}]
    store.save_signal_package(signal_summary.signal_run_id, signal)

    with pytest.raises(ValueError, match="live execution gate"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)

    stale_events = store.list_system_events_by_type("stale_data_halt")
    assert stale_events[0]["payload"]["issues"][0]["reason"] == "stale_price"


def test_approve_signal_rejects_missing_datahub_evidence(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    signal = store.load_signal_package(signal_summary.signal_run_id)
    signal.pop("datahub_evidence")
    store.save_signal_package(signal_summary.signal_run_id, signal)

    with pytest.raises(ValueError, match="missing DataHub evidence"):
        MaestroOrchestrator(config).approve_signal(signal_summary.signal_run_id)


def test_approve_signal_rechecks_safety_state(monkeypatch, tmp_path):
    _mock_kis_snapshot_refresh(monkeypatch)
    config = _live_signal_config(tmp_path, "approved")
    signal_summary = MaestroOrchestrator(config).run_signal()
    orchestrator = MaestroOrchestrator(config)
    SafetyControlService(orchestrator.state_store, orchestrator.audit).kill_switch(
        new_run_id(),
        "operator kill",
    )

    with pytest.raises(ValueError, match="safety state blocks"):
        orchestrator.approve_signal(signal_summary.signal_run_id)

    blocked = orchestrator.state_store.list_system_events_by_type("safety_execution_blocked")
    assert blocked[0]["payload"]["state"] == "killed"
    assert blocked[0]["payload"]["phase"] == "approve_signal"


def _paper_approval_config(tmp_path, decision):
    return load_config(_paper_approval_config_path(tmp_path, decision))


def _paper_approval_config_path(tmp_path, decision) -> Path:
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_approval_console.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"]["default_decision"] = decision
    config_path = tmp_path / "signal_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _live_signal_config(tmp_path, decision):
    return load_config(_live_signal_config_path(tmp_path, decision))


def _live_signal_config_path(
    tmp_path,
    decision,
    *,
    filename: str = "live_signal_approval.yaml",
    account_id: str = "kis_paper",
    strategy_weight: float = 1.0,
) -> Path:
    raw = yaml.safe_load(Path("tests/fixtures/configs/paper_approval_console.yaml").read_text())
    raw["mode"] = "live_approval"
    raw["portfolio"].pop("initial_cash", None)
    raw["strategies"][0]["account_id"] = account_id
    raw["strategies"][0]["weight"] = strategy_weight
    raw["accounts"] = [
        {
            "id": account_id,
            "broker": "kis",
            "environment": "paper_trading",
            "enabled": True,
            "provider": "kis",
            "account_id": "MOCK-LIVE",
            "broker_product": "kis_overseas_stock",
        }
    ]
    raw["execution"]["order_posture"] = "dry_run"
    raw["execution"]["live_order_limits"] = {
        "max_order_notional": 10_000_000,
        "max_daily_notional": 20_000_000,
        "max_daily_order_count": 10,
        "daily_loss_limit": None,
        "fee_buffer_pct": 0.0,
    }
    raw["state"]["sqlite_path"] = str(tmp_path / "live_state.db")
    raw["state"]["identity_group"] = "test_signal_approval"
    raw["audit"]["jsonl_path"] = str(tmp_path / "live_audit.jsonl")
    raw["approval"]["default_decision"] = decision
    config_path = tmp_path / filename
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _mock_kis_snapshot_refresh(monkeypatch) -> None:
    def init_service(
        self: KISReadOnlyService,
        config,
        state_store,
        audit_logger,
        client=None,
        instruments=None,
        logical_account_id=None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.audit_logger = audit_logger
        self.instruments = instruments or []
        self.logical_account_id = logical_account_id
        self.client = client

    def fetch_snapshot(self: KISReadOnlyService, symbols: list[str]) -> KISReadOnlySnapshot:
        account = KISAccountSnapshot(
            account_id=self.config.account_id or "MOCK-LIVE",
            cash=10_000_000.0,
            cash_by_currency={"KRW": 10_000_000.0},
            buying_power=10_000_000.0,
            positions=[],
            fetched_at=utc_now(),
            source="kis_mock",
        )
        snapshot = KISReadOnlySnapshot(
            account=account,
            current_prices={symbol: 100.0 for symbol in symbols if symbol != "CASH"},
            order_fills=[],
            unfilled_orders=[],
        )
        payload = snapshot.model_dump(mode="json")
        payload["account_id"] = self.logical_account_id
        payload["broker_account_id"] = snapshot.account.account_id
        self.state_store.save_broker_account_snapshot(
            "run_mock_broker_snapshot",
            snapshot.account.account_id,
            payload,
        )
        return snapshot

    monkeypatch.setattr(KISReadOnlyService, "__init__", init_service)
    monkeypatch.setattr(KISReadOnlyService, "fetch_and_store_snapshot", fetch_snapshot)

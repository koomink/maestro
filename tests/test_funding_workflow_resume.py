"""Funding confirmation goes through claim -> child run -> completed (Task 8).

The router fixture below is built the same way as ``_router`` in
tests/test_telegram_approval_resume.py, adapted to also carry a
``signal_config_path``: _confirm_funding_request raises immediately without
one. The signal config is the buy-only contribution setup already proven out
in tests/test_telegram_operator_ui.py (account paper_cash / sleeve
krw_contribution / strategy tranquillo, sandbox broker, MOCK_ETF_A/B prices)
so run_signal() can actually produce a signal package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from maestro.config.loader import load_config
from maestro.integrations.telegram.handlers import TelegramOperatorCommandRouter
from maestro.monitoring.audit_logger import AuditLogger
from maestro.sdk import (
    BaseStrategyPlugin,
    DataBundle,
    DataRequest,
    StrategyContext,
    StrategyManifest,
    TargetAllocationResult,
)
from maestro.state.funding_workflow import (
    WorkflowClaimRefused,
    load_workflow_child,
    publish_contribution_request,
    workflow_id_from_request,
)
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore


class _FundingWorkflowStrategy(BaseStrategyPlugin):
    strategy_id = "tranquillo"

    def manifest(self) -> StrategyManifest:
        return StrategyManifest(
            strategy_id=self.strategy_id,
            name=self.strategy_id,
            version="0.1.0",
            description="Funding workflow resume test strategy.",
            supported_modes=["paper", "live_approval"],
            supported_asset_types=["cash", "etf"],
            result_type="target_allocation",
            requires_data=["price"],
            can_run_live=True,
        )

    def build_data_requests(self, context: StrategyContext) -> list[DataRequest]:
        del context
        return [
            DataRequest(symbol="MOCK_ETF_A", asset_type="etf", data_type="price"),
            DataRequest(symbol="MOCK_ETF_B", asset_type="etf", data_type="price"),
        ]

    def run(self, data_bundle: DataBundle, context: StrategyContext) -> TargetAllocationResult:
        del data_bundle
        return TargetAllocationResult(
            strategy_id=context.strategy_id,
            strategy_version=self.manifest().version,
            timestamp=context.timestamp,
            allocations={},
            allocation_sleeves={"KRW": {"MOCK_ETF_A": 0.6, "MOCK_ETF_B": 0.4}},
            confidence=1.0,
            time_horizon="funding-workflow-resume-test",
            rationale="Funding workflow resume retry target.",
        )


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict] = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent_messages.append({"chat_id": chat_id, "text": text})
        return {"result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, *, offset=None, timeout_seconds=0, allowed_updates=None):
        return {"ok": True, "result": []}

    def answer_callback_query(self, callback_query_id, text=""):
        return {"ok": True}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        return {"ok": True}


def _readonly_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["market_session"] = {
        "required": False,
        "timezone": "Asia/Seoul",
        "open": "09:00",
        "close": "15:30",
        "weekdays": [0, 1, 2, 3, 4],
        "holidays": [],
    }
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": True,
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
    }
    config_path = tmp_path / "readonly.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


def _signal_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["portfolio"]["initial_cash"] = 1_000_000
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["approval"] = {
        "enabled": True,
        "provider": "telegram",
        "require_approval": False,
        "default_decision": "approved",
        "telegram_allowed_chat_ids": [100],
        "whitelisted_user_ids": [100],
        "telegram_poll_interval_seconds": 0.0,
    }
    raw["strategies"] = [
        {
            "id": "tranquillo",
            "enabled": True,
            "signal_enabled": True,
            "weight": 1.0,
            "account_id": "paper_cash",
            "execution_sleeve": "krw_contribution",
            "order_posture": "dry_run",
            "entrypoint": f"{__name__}:_FundingWorkflowStrategy",
            "config": {},
        }
    ]
    raw["execution"]["order_posture"] = "dry_run"
    raw["execution"]["market_session"] = {
        "required": False,
        "timezone": "Asia/Seoul",
        "open": "09:00",
        "close": "15:30",
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
        "holidays": [],
    }
    raw["execution"]["live_order_limits"] = {"fee_buffer_pct": 0.0}
    raw["accounts"] = [
        {
            "id": "paper_cash",
            "broker": "sandbox",
            "environment": "paper_trading",
            "enabled": True,
        }
    ]
    raw["execution_sleeves"] = {
        "accounts": {
            "paper_cash": {
                "krw_contribution": {
                    "currency_sleeve": "KRW",
                    "target_weight": 1.0,
                    "order_generation_mode": "buy_only_contribution",
                    "contribution": {
                        "enabled": True,
                        "currency": "KRW",
                        "sleeve": "KRW",
                        "monthly_budget": 3_000_000,
                        "min_monthly_budget": 2_000_000,
                        "max_monthly_budget": 4_000_000,
                        "buy_day": 1,
                        "non_trading_day_policy": "next_trading_day",
                        "target_policy": "buy_only_toward_target",
                        "funding_request": {"enabled": True},
                    },
                }
            }
        }
    }
    config_path = tmp_path / "signal.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


@pytest.fixture
def operator_bot(tmp_path):
    config = load_config(_readonly_config_path(tmp_path))
    store = StateStore(config.state.sqlite_path, config.portfolio.initial_cash)
    audit = AuditLogger(config.audit.jsonl_path)
    client = FakeTelegramClient()
    return TelegramOperatorCommandRouter(
        config=config,
        store=store,
        audit=audit,
        client=client,
        signal_config_path=_signal_config_path(tmp_path),
    )


def _request(request_id: str) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "contribution_group_id": "core",
        "account_id": "paper_cash",
        "execution_sleeve": "krw_contribution",
        "currency": "KRW",
        "month_key": "2026-08",
        "status": "pending",
        "strategy_ids": ["tranquillo"],
        "required_shortfall": 1_000_000.0,
    }


def _workflow_id_of(request_id: str) -> str:
    return workflow_id_from_request(_request(request_id))


def test_a_confirmed_funding_request_records_claim_child_and_completed(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")

    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )

    types = [
        row["event_type"]
        for row in store.list_system_events(limit=100)
        if row["event_type"].startswith("funding_workflow_")
    ]
    assert "funding_workflow_claim" in types
    assert "funding_workflow_child_created" in types
    assert "funding_workflow_completed" in types


def test_a_duplicate_callback_is_refused_before_any_side_effect(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )

    with pytest.raises(WorkflowClaimRefused):
        operator_bot._confirm_funding_request(
            _request("req-1"), chat_id=1, user_id=2, username="op"
        )

    assert len(store.list_system_events_by_type("signal_package", limit=None)) == 1


def test_a_superseded_request_callback_is_refused(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")

    with pytest.raises(WorkflowClaimRefused) as excinfo:
        operator_bot._confirm_funding_request(
            _request("req-1"), chat_id=1, user_id=2, username="op"
        )

    assert excinfo.value.reason == "not_head"
    assert store.list_system_events_by_type("signal_package", limit=None) == []


def test_a_crash_after_the_child_run_resumes_without_a_second_child(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    # attempt 1 gets as far as the child run, then the process dies -- no
    # claim was ever recorded for this attempt.
    operator_bot._run_child_signal(_request("req-1"), _workflow_id_of("req-1"), attempt=1)
    child = load_workflow_child(store, "req-1", "funding")
    assert child is not None

    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op", attempt=2
    )

    assert load_workflow_child(store, "req-1", "funding") == child
    assert len(store.list_system_events_by_type("signal_package", limit=None)) == 1


def test_a_canceled_request_completes_through_the_same_claim_path(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")

    operator_bot._cancel_funding_request(_request("req-1"), user_id=2, username="op")

    types = [
        row["event_type"]
        for row in store.list_system_events(limit=100)
        if row["event_type"].startswith("funding_workflow_")
    ]
    assert "funding_workflow_claim" in types
    assert "funding_workflow_completed" in types
    ack_events = store.list_system_events_by_type("contribution_funding_request_ack", limit=10)
    assert ack_events[0]["payload"]["request_id"] == "req-1"
    assert ack_events[0]["payload"]["status"] == "canceled"
    assert operator_bot._load_pending_funding_request("req-1") is None


def test_a_canceled_request_is_refused_if_already_superseded(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")

    with pytest.raises(WorkflowClaimRefused) as excinfo:
        operator_bot._cancel_funding_request(_request("req-1"), user_id=2, username="op")

    assert excinfo.value.reason == "not_head"


def test_the_legacy_ack_is_written_so_a_rollback_sees_the_request_as_done(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")

    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )

    assert operator_bot._load_pending_funding_request("req-1") is None
    ack_events = store.list_system_events_by_type("contribution_funding_request_ack", limit=10)
    assert ack_events[0]["payload"]["request_id"] == "req-1"
    assert ack_events[0]["payload"]["status"] == "confirmed"


def test_the_cash_flow_record_is_not_duplicated_on_resume(operator_bot):
    """Pins existing behavior: AccountCashFlowService.record's duplicate_key
    (``account-cash-flow:funding:<request_id>``) already makes a second call
    for the same request a no-op. This is not new work from Task 8 -- the
    claim is what stops a real duplicate *callback* from ever reaching this
    method twice; this test just confirms the underlying record() call was
    already idempotent on its own, in case something upstream calls it twice
    anyway (e.g. a resumed attempt after a crash between the two legs).
    """
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    # Establish the account ledger baseline so record() doesn't skip on
    # "ledger not established" and so the account_id gate in
    # _record_account_cash_flow_from_funding_request passes.
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=0.0, cash_by_currency={"KRW": 0.0}, positions={}),
        account_id="paper_cash",
    )

    operator_bot._record_account_cash_flow_from_funding_request(
        _request("req-1"), user_id=2, username="op"
    )
    operator_bot._record_account_cash_flow_from_funding_request(
        _request("req-1"), user_id=2, username="op"
    )

    flows = store.list_system_events_by_type("account_cash_flow", limit=None)
    assert len(flows) == 1

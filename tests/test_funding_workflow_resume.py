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
from maestro.core.ids import new_run_id
from maestro.integrations.telegram.bot import TelegramApiRejected
from maestro.integrations.telegram.handlers import TelegramOperatorCommandRouter
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import SignalRunSummary
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
    claim_workflow_attempt,
    converge_workflow_invariants,
    head_key,
    list_incomplete_workflows,
    load_migration_cutoff,
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
        self.answered_callbacks: list[dict] = []
        self.edited_messages: list[dict] = []

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent_messages.append(
            {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        )
        return {"result": {"message_id": len(self.sent_messages)}}

    def get_updates(self, *, offset=None, timeout_seconds=0, allowed_updates=None):
        return {"ok": True, "result": []}

    def answer_callback_query(self, callback_query_id, text=""):
        self.answered_callbacks.append({"callback_query_id": callback_query_id, "text": text})
        return {"ok": True}

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited_messages.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return {"ok": True}


def callback_update(
    data: str,
    *,
    update_id: int = 2,
    chat_id: int = 100,
    user_id: int = 100,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "data": data,
            "message": {
                "chat": {"id": chat_id},
                "message_id": 10,
                "text": "Confirm operator command",
            },
            "from": {"id": user_id, "username": "operator"},
        },
    }


def message_update(
    text: str,
    *,
    update_id: int = 1,
    chat_id: int = 100,
    user_id: int = 100,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id},
            "from": {"id": user_id, "username": "operator"},
            "text": text,
        },
    }


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


def _budget_request(request_id: str) -> dict[str, Any]:
    # Mirrors the field set build_contribution_budget_request produces
    # (src/maestro/execution/budget_requests.py), not the funding shape:
    # validate_selected_budget and selected_budget_from_request read
    # min_monthly_budget/recommended_budget/selectable_max_budget.
    return {
        "request_id": request_id,
        "source_signal_run_id": "signal-old",
        "contribution_group_id": "core",
        "account_id": "paper_cash",
        "execution_sleeve": "krw_contribution",
        "currency": "KRW",
        "available_cash": 2_000_000.0,
        "min_monthly_budget": 200_000.0,
        "recommended_budget": 400_000.0,
        "selectable_max_budget": 1_000_000.0,
        "month_key": "2026-08",
        "status": "pending",
        "strategy_ids": ["tranquillo"],
    }


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
    workflow_id = _workflow_id_of("req-1")
    # attempt 1 claims (the real path always claims before touching
    # run_signal), gets as far as the child run, then the process dies
    # before completion.
    claim = claim_workflow_attempt(
        store,
        new_run_id(),
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
        extra={"intent": "confirm"},
    )
    assert claim["claimed"]
    operator_bot._run_child_signal(_request("req-1"), workflow_id, attempt=1)
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


def _funding_complete_statuses(store) -> list[str]:
    return [
        row["payload"]["status"]
        for row in store.list_system_events_by_type("telegram_command", limit=None)
        if row["payload"].get("command") == "/funding_complete"
    ]


def _funding_cancel_statuses(store) -> list[str]:
    return [
        row["payload"]["status"]
        for row in store.list_system_events_by_type("telegram_command", limit=None)
        if row["payload"].get("command") == "/funding_cancel"
    ]


def test_a_retry_after_a_stuck_claim_reports_in_flight_not_superseded(operator_bot):
    """Fix round 1, finding 1+2.

    The router always claims at attempt=1 (no resume mechanism yet -- that's
    Task 10), so a claim that commits and is then followed by a failure
    (exactly what test_telegram_operator_funding_complete_fails_when_readonly_refresh_fails
    exercises) leaves a claim on record. Retrying must not tell the operator
    the request was "already processed or superseded" -- it wasn't processed,
    it's stuck -- so the message and audit status have to say "in flight",
    not "superseded".
    """
    store = operator_bot.store
    request = _request("req-1")
    publish_contribution_request(store, "run-1", request, phase="funding")
    workflow_id = workflow_id_from_request(request)
    claim = claim_workflow_attempt(
        store,
        new_run_id(),
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        attempt=1,
    )
    assert claim["claimed"]

    assert operator_bot.process_update(callback_update("operator:funding:complete:req-1"))

    text = operator_bot.client.edited_messages[-1]["text"]
    assert "already being processed" in text
    assert "superseded" not in text
    assert _funding_complete_statuses(store) == ["claim_in_flight"]


def test_a_retry_of_a_genuinely_superseded_request_still_reports_superseded(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")

    assert operator_bot.process_update(callback_update("operator:funding:complete:req-1"))

    text = operator_bot.client.edited_messages[-1]["text"]
    assert "already processed or superseded" in text
    assert _funding_complete_statuses(store) == ["claim_superseded"]


def test_the_cancel_branch_answers_the_callback_on_a_generic_error(operator_bot, monkeypatch):
    """Fix round 1, finding 3.

    Before this fix, _process_funding_callback's cancel branch caught only
    WorkflowClaimRefused; any other exception (e.g. a ValueError raised by
    workflow_id_from_request on a malformed month_key, or a complete_workflow
    content mismatch) escaped uncaught and would leave the callback
    unanswered instead of reporting a failure to the operator.
    """
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")

    def failing_cancel(request, *, user_id, username, attempt=1):
        del request, user_id, username, attempt
        raise ValueError("boom")

    monkeypatch.setattr(operator_bot, "_cancel_funding_request", failing_cancel)

    assert operator_bot.process_update(callback_update("operator:funding:cancel:req-1"))

    assert operator_bot.client.answered_callbacks[-1]["text"] == "Funding cancellation failed."
    text = operator_bot.client.edited_messages[-1]["text"]
    assert "Funding cancellation failed" in text
    assert "boom" in text
    assert _funding_cancel_statuses(store) == ["failed"]


def test_a_budget_decision_alone_does_not_close_the_workflow(operator_bot, monkeypatch):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _budget_request("req-1"), phase="budget")

    def boom(*args, **kwargs):
        raise RuntimeError("config load failed")

    monkeypatch.setattr(operator_bot, "_run_child_signal", boom)
    with pytest.raises(RuntimeError, match="config load failed"):
        operator_bot._confirm_budget_request(
            _budget_request("req-1"),
            selected_budget=500000.0,
            chat_id=1,
            user_id=2,
            username="op",
        )
    # The workflow must still be recoverable: no terminal event was written.
    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    assert (
        store.list_system_events_by_type("contribution_budget_request_decision", limit=None) == []
    )
    # But the claim did land, carrying the amount for a future resume.
    claims = store.list_system_events_by_type("funding_workflow_claim", limit=None)
    assert claims[0]["payload"]["selected_budget"] == 500000.0


def test_a_completed_budget_workflow_writes_the_legacy_decision(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _budget_request("req-1"), phase="budget")
    operator_bot._confirm_budget_request(
        _budget_request("req-1"), selected_budget=500000.0, chat_id=1, user_id=2, username="op"
    )
    decisions = store.list_system_events_by_type(
        "contribution_budget_request_decision", limit=None
    )
    assert decisions[0]["payload"]["selected_budget"] == 500000.0
    assert operator_bot._load_pending_budget_request("req-1") is None


def test_resuming_a_budget_workflow_reuses_the_stored_amount(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _budget_request("req-1"), phase="budget")
    operator_bot._confirm_budget_request(
        _budget_request("req-1"),
        selected_budget=500000.0,
        chat_id=1,
        user_id=2,
        username="op",
        attempt=1,
    )
    # list_system_events_by_type orders newest-first (ORDER BY id DESC); this
    # request only ever gets one claim (attempt=1), so index 0 is it.
    claims = store.list_system_events_by_type("funding_workflow_claim", limit=None)
    assert claims[0]["payload"]["selected_budget"] == 500000.0


def _budget_statuses(store) -> list[str]:
    return [
        row["payload"]["status"]
        for row in store.list_system_events_by_type("telegram_command", limit=None)
        if row["payload"].get("command") == "/budget"
    ]


def test_a_superseded_budget_request_via_text_command_reports_superseded(operator_bot):
    """Fix round 1.

    _process_budget_command (the /budget <request_id> <amount> text entry
    point) caught only the generic (RuntimeError, TimeoutError, TypeError,
    ValueError) tuple, and WorkflowClaimRefused subclasses RuntimeError -- so
    a genuinely superseded request was reported as an invalid amount instead
    of "already processed or superseded", with audit status "failed" instead
    of "claim_superseded".
    """
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _budget_request("req-1"), phase="budget")
    publish_contribution_request(store, "run-2", _budget_request("req-2"), phase="budget")

    assert operator_bot.process_update(message_update("/budget req-1 500000"))

    text = operator_bot.client.sent_messages[-1]["text"]
    assert "already processed or superseded" in text
    assert "out of range or invalid" not in text
    assert _budget_statuses(store) == ["claim_superseded"]


def test_a_stuck_budget_claim_via_text_command_reports_in_flight(operator_bot):
    store = operator_bot.store
    request = _budget_request("req-1")
    publish_contribution_request(store, "run-1", request, phase="budget")
    workflow_id = workflow_id_from_request(request)
    claim = claim_workflow_attempt(
        store,
        new_run_id(),
        workflow_id=workflow_id,
        request_id="req-1",
        phase="budget",
        attempt=1,
        extra={"selected_budget": 500000.0},
    )
    assert claim["claimed"]

    assert operator_bot.process_update(message_update("/budget req-1 500000"))

    text = operator_bot.client.sent_messages[-1]["text"]
    assert "already being processed" in text
    assert "superseded" not in text
    assert _budget_statuses(store) == ["claim_in_flight"]


def _wfresume_statuses(store) -> list[str]:
    return [
        row["payload"]["status"]
        for row in store.list_system_events_by_type("telegram_command", limit=None)
        if row["payload"].get("command") == "/wfresume"
    ]


def test_a_claim_without_completion_shows_up_as_incomplete(operator_bot):
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert [row["request_id"] for row in list_incomplete_workflows(store)] == ["req-1"]


def test_a_completed_workflow_is_not_incomplete(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )
    assert list_incomplete_workflows(store) == []


def test_an_incomplete_workflow_is_never_resumed_automatically(operator_bot):
    """The sweep only surfaces; it must never re-enter the transition itself."""
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    operator_bot._sweep_incomplete_workflows()
    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    assert list_incomplete_workflows(store) == [
        {
            "workflow_id": workflow_id,
            "request_id": "req-1",
            "phase": "funding",
            "attempt": 1,
            "intent": "confirm",
            "selected_budget": None,
        }
    ]


def test_the_sweep_sends_a_resume_card_whose_button_fits_telegrams_limit(operator_bot):
    """Fix round 1 finding: the original version of this test asserted the
    byte budget against the fixture id ``"req-1"`` (5 bytes) instead of a
    real generated id, so it would have kept passing even if
    new_budget_request_id() grew and pushed callback_data past Telegram's
    64-byte cap -- exactly the boundary the length correction was about.
    This version builds callback_data from real ids for both phases.
    """
    from maestro.core.ids import new_budget_request_id, new_funding_request_id

    store = operator_bot.store
    funding_request_id = new_funding_request_id()
    budget_request_id = new_budget_request_id()

    workflow_id = publish_contribution_request(
        store, "run-1", _request(funding_request_id), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id=funding_request_id,
        phase="funding",
    )
    # A different month, so this budget request is its own workflow: funding
    # and budget in the *same* scope share one head, and publishing the second
    # would supersede the first -- which list_incomplete_workflows now filters
    # out (a request that is no longer head can never be resumed).
    budget_workflow_id = publish_contribution_request(
        store,
        "run-2",
        {**_budget_request(budget_request_id), "month_key": "2026-09"},
        phase="budget",
    )["workflow_id"]
    claim_workflow_attempt(
        store,
        "run-2",
        workflow_id=budget_workflow_id,
        request_id=budget_request_id,
        phase="budget",
    )

    operator_bot._sweep_incomplete_workflows()

    callback_data_by_phase = {
        markup_callback["callback_data"].split(":")[2]: markup_callback["callback_data"]
        for sent in operator_bot.client.sent_messages
        for markup_callback in [sent["reply_markup"]["inline_keyboard"][0][0]]
    }
    funding_callback_data = callback_data_by_phase["funding"]
    budget_callback_data = callback_data_by_phase["budget"]
    assert funding_callback_data == f"operator:wfresume:funding:{funding_request_id}"
    assert budget_callback_data == f"operator:wfresume:budget:{budget_request_id}"

    funding_bytes = len(funding_callback_data.encode("utf-8"))
    budget_bytes = len(budget_callback_data.encode("utf-8"))
    # Telegram rejects callback_data over 64 bytes outright -- if either of
    # these ever exceeds it, the Resume button silently fails to send or
    # route, with no other test catching it. The budget case (measured 64
    # bytes: "operator:wfresume:budget:" + "budget_" + 32 hex chars) sits
    # exactly at the cap with zero headroom -- a single extra character in
    # new_budget_request_id() would break it.
    assert funding_bytes <= 64, funding_bytes
    assert budget_bytes <= 64, budget_bytes


def test_a_second_sweep_does_not_resend_the_same_attempts_notice(operator_bot):
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )

    operator_bot._sweep_incomplete_workflows()
    operator_bot._sweep_incomplete_workflows()

    assert len(operator_bot.client.sent_messages) == 1


def test_a_new_attempt_after_a_failed_resume_notifies_again(operator_bot, monkeypatch):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")

    def boom(*args, **kwargs):
        raise RuntimeError("config load failed")

    monkeypatch.setattr(operator_bot, "_run_child_signal", boom)
    with pytest.raises(RuntimeError, match="config load failed"):
        operator_bot._confirm_funding_request(
            _request("req-1"), chat_id=1, user_id=2, username="op"
        )

    operator_bot._sweep_incomplete_workflows()
    assert len(operator_bot.client.sent_messages) == 1

    # A second attempt on the same request stalls again -- a *new* attempt
    # number, so it must produce a fresh notice, not be swallowed by the
    # first attempt's duplicate_key.
    with pytest.raises(RuntimeError, match="config load failed"):
        operator_bot._confirm_funding_request(
            _request("req-1"), chat_id=1, user_id=2, username="op", attempt=2
        )
    operator_bot._sweep_incomplete_workflows()
    assert len(operator_bot.client.sent_messages) == 2


def test_the_operator_resume_button_enters_exactly_once(operator_bot):
    import threading

    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    entered: list[bool] = []
    barrier = threading.Barrier(2)

    def press() -> None:
        barrier.wait()
        entered.append(
            claim_workflow_attempt(
                store,
                "run-1",
                workflow_id=workflow_id,
                request_id="req-1",
                phase="funding",
                attempt=2,
            )["claimed"]
        )

    threads = [threading.Thread(target=press) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(entered) == [False, True]


def test_a_double_tap_of_resume_enters_exactly_once_through_the_router(operator_bot):
    """Real contention through process_update, not just the claim primitive.

    Both taps hit the same stalled workflow, so both compute the same
    ``attempt + 1``. Exactly one of the two concurrent
    ``_process_workflow_resume_callback`` calls must win the claim; the
    other must see ``claim_in_flight`` (its own resume attempt already
    landed) rather than silently entering a second time.
    """
    import threading

    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    barrier = threading.Barrier(2)

    def press(update_id: int) -> None:
        barrier.wait()
        operator_bot.process_update(
            callback_update("operator:wfresume:funding:req-1", update_id=update_id)
        )

    threads = [threading.Thread(target=press, args=(10 + i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(_wfresume_statuses(store)) == ["claim_in_flight", "resumed"]
    completed = store.list_system_events_by_type("funding_workflow_completed", limit=None)
    assert len(completed) == 1
    assert completed[0]["payload"]["attempt"] == 2


def test_resuming_a_budget_workflow_reuses_the_stored_amount_without_asking_again(
    operator_bot, monkeypatch
):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _budget_request("req-1"), phase="budget")

    def boom(*args, **kwargs):
        raise RuntimeError("config load failed")

    monkeypatch.setattr(operator_bot, "_run_child_signal", boom)
    with pytest.raises(RuntimeError, match="config load failed"):
        operator_bot._confirm_budget_request(
            _budget_request("req-1"),
            selected_budget=500000.0,
            chat_id=1,
            user_id=2,
            username="op",
        )
    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []

    monkeypatch.undo()
    assert operator_bot.process_update(callback_update("operator:wfresume:budget:req-1"))

    decisions = store.list_system_events_by_type(
        "contribution_budget_request_decision", limit=None
    )
    assert decisions[0]["payload"]["selected_budget"] == 500000.0
    assert _wfresume_statuses(store) == ["resumed"]


def test_a_stale_resume_callback_for_a_non_stalled_workflow_is_refused(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")

    assert operator_bot.process_update(
        callback_update("operator:wfresume:funding:req-1")
    )

    text = operator_bot.client.answered_callbacks[-1]["text"]
    assert "no longer stalled" in text
    assert _wfresume_statuses(store) == ["stale_callback"]


def test_without_a_migration_cutoff_the_sweep_does_nothing(operator_bot):
    store = operator_bot.store
    store.save_system_event("run-1", "contribution_funding_request", dict(_request("legacy-1")))
    assert load_migration_cutoff(store) is None
    assert converge_workflow_invariants(store, cutoff=None) == {
        "orphans_superseded": 0,
        "heads_rolled_back": 0,
        "conflicts_skipped": 0,
    }
    assert store.list_system_events_by_type("funding_workflow_superseded", limit=None) == []


def test_a_pending_request_after_the_cutoff_without_a_head_is_superseded(operator_bot):
    store = operator_bot.store
    cutoff = 0
    store.save_system_event("run-1", "contribution_funding_request", dict(_request("orphan-1")))
    result = converge_workflow_invariants(store, cutoff=cutoff)
    assert result["orphans_superseded"] == 1
    superseded = store.list_system_events_by_type("funding_workflow_superseded", limit=None)
    assert [row["payload"]["request_id"] for row in superseded] == ["orphan-1"]


def test_a_pending_request_before_the_cutoff_is_left_alone(operator_bot):
    store = operator_bot.store
    store.save_system_event("run-1", "contribution_funding_request", dict(_request("legacy-1")))
    rows = store.list_system_events_by_type("contribution_funding_request", limit=None)
    cutoff = int(rows[0]["id"])
    assert converge_workflow_invariants(store, cutoff=cutoff)["orphans_superseded"] == 0


def test_a_head_pointing_at_nothing_falls_back_to_the_previous_version(operator_bot):
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    store.save_system_events_atomic(
        "run-2",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": head_key(workflow_id, 2),
                    "workflow_id": workflow_id,
                    "version": 2,
                    "request_id": "ghost-1",
                    "phase": "funding",
                    "status": "pending",
                },
            }
        ],
    )
    result = converge_workflow_invariants(store, cutoff=0)
    assert result["heads_rolled_back"] == 1
    head = store.load_funding_workflow_head(workflow_id)
    assert head["request_id"] == "req-1"
    assert head["version"] == 3


def test_converging_twice_changes_nothing_the_second_time(operator_bot):
    store = operator_bot.store
    store.save_system_event("run-1", "contribution_funding_request", dict(_request("orphan-1")))
    converge_workflow_invariants(store, cutoff=0)
    assert converge_workflow_invariants(store, cutoff=0)["orphans_superseded"] == 0


def test_a_normal_replacement_is_not_treated_as_an_orphan_by_the_sweep(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")
    result = converge_workflow_invariants(store, cutoff=0)
    assert result["orphans_superseded"] == 0
    superseded = store.list_system_events_by_type("funding_workflow_superseded", limit=None)
    assert [row["payload"]["request_id"] for row in superseded] == ["req-1"]


def test_converging_a_rolled_back_head_twice_changes_nothing_the_second_time(operator_bot):
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    store.save_system_events_atomic(
        "run-2",
        [
            {
                "event_type": "funding_workflow_head",
                "payload": {
                    "duplicate_key": head_key(workflow_id, 2),
                    "workflow_id": workflow_id,
                    "version": 2,
                    "request_id": "ghost-1",
                    "phase": "funding",
                    "status": "pending",
                },
            }
        ],
    )
    first = converge_workflow_invariants(store, cutoff=0)
    assert first["heads_rolled_back"] == 1
    second = converge_workflow_invariants(store, cutoff=0)
    assert second["heads_rolled_back"] == 0


def test_a_malformed_request_row_does_not_abort_the_sweep_for_others(operator_bot):
    store = operator_bot.store
    bad_payload = dict(_request("bad-1"))
    del bad_payload["month_key"]
    store.save_system_event("run-1", "contribution_funding_request", bad_payload)
    store.save_system_event("run-2", "contribution_funding_request", dict(_request("good-1")))
    result = converge_workflow_invariants(store, cutoff=0)
    assert result["orphans_superseded"] == 1
    superseded = store.list_system_events_by_type("funding_workflow_superseded", limit=None)
    assert [row["payload"]["request_id"] for row in superseded] == ["good-1"]



# --- final review fixes -------------------------------------------------


def _budget_statuses_for(store, command: str) -> list[str]:
    return [
        row["payload"]["status"]
        for row in store.list_system_events_by_type("telegram_command", limit=None)
        if row["payload"].get("command") == command
    ]


def test_a_second_budget_amount_on_the_same_attempt_is_refused_not_raised(
    operator_bot, monkeypatch
):
    """Final review F2.

    The amount rides in the claim payload but not in claim_key, so a second
    amount under the same attempt is a same-key/different-content collision.
    That has to surface as a claim refusal, not as a raw store ValueError.
    """
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _budget_request("req-1"), phase="budget")

    def boom(*args, **kwargs):
        raise RuntimeError("config load failed")

    monkeypatch.setattr(operator_bot, "_run_child_signal", boom)
    with pytest.raises(RuntimeError, match="config load failed"):
        operator_bot._confirm_budget_request(
            _budget_request("req-1"),
            selected_budget=500_000.0,
            chat_id=1,
            user_id=2,
            username="op",
        )

    with pytest.raises(WorkflowClaimRefused) as excinfo:
        operator_bot._confirm_budget_request(
            _budget_request("req-1"),
            selected_budget=1_000_000.0,
            chat_id=1,
            user_id=2,
            username="op",
        )
    assert excinfo.value.reason == "already_claimed"


def test_changing_the_budget_amount_on_the_same_card_reports_in_flight(
    operator_bot, monkeypatch
):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _budget_request("req-1"), phase="budget")

    def boom(*args, **kwargs):
        raise RuntimeError("config load failed")

    monkeypatch.setattr(operator_bot, "_run_child_signal", boom)
    assert operator_bot.process_update(callback_update("operator:budget:sel:req-1:r"))
    assert operator_bot.process_update(
        callback_update("operator:budget:sel:req-1:f", update_id=3)
    )

    text = operator_bot.client.edited_messages[-1]["text"]
    assert "already being processed" in text
    assert "different content" not in text
    # list_system_events_by_type is newest-first: the second tap's refusal
    # comes back before the first tap's failure.
    assert _budget_statuses_for(store, "/budget_select") == ["claim_in_flight", "failed"]


def test_resuming_a_stalled_cancel_cancels_instead_of_confirming(operator_bot, monkeypatch):
    """Final review F1+F3.

    The operator asked to cancel. The claim landed, complete_workflow did
    not. Resume must finish the *cancel* -- it must not book the cash flow
    or generate the orders the operator just declined.
    """
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    store.save_portfolio_snapshot(
        "baseline",
        PortfolioState(cash=0.0, cash_by_currency={"KRW": 0.0}, positions={}),
        account_id="paper_cash",
    )

    def boom(*args, **kwargs):
        raise RuntimeError("crashed between claim and completion")

    monkeypatch.setattr("maestro.integrations.telegram.handlers.complete_workflow", boom)
    assert operator_bot.process_update(callback_update("operator:funding:cancel:req-1"))
    assert _funding_cancel_statuses(store) == ["failed"]
    monkeypatch.undo()

    assert operator_bot.process_update(
        callback_update("operator:wfresume:funding:req-1", update_id=3)
    )

    assert _wfresume_statuses(store) == ["resumed"]
    assert store.list_system_events_by_type("account_cash_flow", limit=None) == []
    assert store.list_system_events_by_type("signal_package", limit=None) == []
    acks = store.list_system_events_by_type("contribution_funding_request_ack", limit=None)
    assert [row["payload"]["status"] for row in acks] == ["canceled"]
    # The resume had to fence itself with a new attempt, or cancel would be
    # permanently pinned at the attempt the stalled claim already holds.
    claims = store.list_system_events_by_type("funding_workflow_claim", limit=None)
    assert sorted(row["payload"]["attempt"] for row in claims) == [1, 2]
    assert {row["payload"]["intent"] for row in claims} == {"cancel"}


def test_resuming_a_stalled_budget_cancel_needs_no_stored_amount(operator_bot, monkeypatch):
    """Final review F1 (budget half) / deferred-minor #13.

    A cancel claim carries no selected_budget, and it never needed one --
    the resume must not demand it.
    """
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _budget_request("req-1"), phase="budget")

    def boom(*args, **kwargs):
        raise RuntimeError("crashed between claim and completion")

    monkeypatch.setattr("maestro.integrations.telegram.handlers.complete_workflow", boom)
    assert operator_bot.process_update(callback_update("operator:budget:cancel:req-1"))
    monkeypatch.undo()

    assert operator_bot.process_update(
        callback_update("operator:wfresume:budget:req-1", update_id=3)
    )

    assert _wfresume_statuses(store) == ["resumed"]
    decisions = store.list_system_events_by_type(
        "contribution_budget_request_decision", limit=None
    )
    assert [row["payload"]["status"] for row in decisions] == ["canceled"]
    assert store.list_system_events_by_type("signal_package", limit=None) == []


def test_a_claim_recorded_before_intent_existed_resumes_as_confirm(operator_bot):
    """Claims written by the previous release carry no intent key. Confirm is
    the only transition the old resume path could perform, so that is what a
    missing intent has to mean."""
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert list_incomplete_workflows(store)[0]["intent"] == "confirm"

    assert operator_bot.process_update(callback_update("operator:wfresume:funding:req-1"))

    assert _wfresume_statuses(store) == ["resumed"]
    acks = store.list_system_events_by_type("contribution_funding_request_ack", limit=None)
    assert [row["payload"]["status"] for row in acks] == ["confirmed"]


def test_an_unknown_recorded_intent_is_not_silently_treated_as_confirm(operator_bot):
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store,
        "run-1",
        workflow_id=workflow_id,
        request_id="req-1",
        phase="funding",
        extra={"intent": "sideways"},
    )

    assert operator_bot.process_update(callback_update("operator:wfresume:funding:req-1"))

    assert _wfresume_statuses(store) == ["failed"]
    assert "sideways" in operator_bot.client.edited_messages[-1]["text"]
    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    assert store.list_system_events_by_type("signal_package", limit=None) == []


def test_a_stalled_claim_on_a_superseded_request_is_no_longer_incomplete(operator_bot):
    """Final review F6: a request that is no longer head can never be resumed,
    so it must not sit in the list that means "needs action" forever."""
    store = operator_bot.store
    workflow_id = publish_contribution_request(
        store, "run-1", _request("req-1"), phase="funding"
    )["workflow_id"]
    claim_workflow_attempt(
        store, "run-1", workflow_id=workflow_id, request_id="req-1", phase="funding"
    )
    assert [row["request_id"] for row in list_incomplete_workflows(store)] == ["req-1"]

    publish_contribution_request(store, "run-2", _request("req-2"), phase="funding")

    assert list_incomplete_workflows(store) == []


def test_a_request_that_predates_the_workflow_upgrade_says_so(operator_bot):
    """Final review F4: no head is not the same as superseded."""
    store = operator_bot.store
    store.save_system_event("run-1", "contribution_funding_request", dict(_request("legacy-1")))

    assert operator_bot.process_update(callback_update("operator:funding:complete:legacy-1"))

    text = operator_bot.client.edited_messages[-1]["text"]
    assert "already processed or superseded" not in text
    assert "predates" in text
    assert _funding_complete_statuses(store) == ["claim_no_head"]


def test_an_unexpected_supersede_conflict_is_counted_and_logged_as_an_error(
    operator_bot, caplog
):
    """Final review section (d): a content conflict on a key this sweep
    believes it owns is a concurrent-writer disagreement, not routine noise."""
    import logging

    from maestro.state.funding_workflow import superseded_key

    store = operator_bot.store
    store.save_system_event("run-1", "contribution_funding_request", dict(_request("orphan-1")))
    # Same duplicate_key, different content, and not attributable to this
    # (workflow_id, request_id) pair -- so the sweep does not skip it as
    # already accounted for and walks straight into the collision.
    store.save_system_event(
        "run-x",
        "funding_workflow_superseded",
        {
            "duplicate_key": superseded_key(_workflow_id_of("orphan-1"), "orphan-1"),
            "workflow_id": "some-other-workflow",
            "request_id": "orphan-1",
            "reason": "written_by_someone_else",
        },
    )

    with caplog.at_level(logging.WARNING, logger="maestro.state.funding_workflow"):
        result = converge_workflow_invariants(store, cutoff=0)

    assert result["orphans_superseded"] == 0
    assert result["conflicts_skipped"] == 1
    levels = {record.levelno for record in caplog.records}
    assert levels == {logging.ERROR}


def _live_approval_config_path(tmp_path) -> Path:
    raw = yaml.safe_load(_signal_config_path(tmp_path).read_text())
    raw["mode"] = "live_approval"
    # live_approval takes cash from the broker snapshot, so the config is
    # rejected outright if it still declares a starting balance.
    raw["portfolio"].pop("initial_cash", None)
    raw["approval"] = {**raw["approval"], "enabled": True, "require_approval": True}
    config_path = tmp_path / "live_approval.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path

def test_a_crash_before_the_next_card_leaves_the_funding_workflow_recoverable(
    operator_bot, monkeypatch
):
    """Critical 2: completing first hid the failure from the recovery sweep.

    The workflow used to be closed the moment the child run existed, and only
    then was the approval dispatched or the next request sent. A crash in
    that gap left a request marked done, a child run nobody was looking at,
    and no card for the operator -- the month's investment stopped with
    nothing on record saying so, because a completed workflow is exactly what
    list_incomplete_workflows filters out.
    """
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")

    def boom(*args, **kwargs):
        raise RuntimeError("crashed before the operator got a card")

    monkeypatch.setattr(operator_bot, "_deliver_child_signal_outcome", boom)
    with pytest.raises(RuntimeError, match="crashed before the operator got a card"):
        operator_bot._confirm_funding_request(
            _request("req-1"), chat_id=1, user_id=2, username="op"
        )

    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    assert store.list_system_events_by_type("contribution_funding_request_ack", limit=None) == []
    assert [row["request_id"] for row in list_incomplete_workflows(store)] == ["req-1"]

    monkeypatch.undo()
    assert operator_bot.process_update(callback_update("operator:wfresume:funding:req-1"))

    assert _wfresume_statuses(store) == ["resumed"]
    assert list_incomplete_workflows(store) == []
    # The resume reuses the child the crashed attempt already built.
    assert len(store.list_system_events_by_type("signal_package", limit=None)) == 1

def test_a_crash_before_the_next_card_leaves_the_budget_workflow_recoverable(
    operator_bot, monkeypatch
):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _budget_request("req-1"), phase="budget")

    def boom(*args, **kwargs):
        raise RuntimeError("crashed before the operator got a card")

    monkeypatch.setattr(operator_bot, "_deliver_child_signal_outcome", boom)
    with pytest.raises(RuntimeError, match="crashed before the operator got a card"):
        operator_bot._confirm_budget_request(
            _budget_request("req-1"),
            selected_budget=500_000.0,
            chat_id=1,
            user_id=2,
            username="op",
        )

    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    assert [row["request_id"] for row in list_incomplete_workflows(store)] == ["req-1"]

    monkeypatch.undo()
    assert operator_bot.process_update(callback_update("operator:wfresume:budget:req-1"))

    decisions = store.list_system_events_by_type(
        "contribution_budget_request_decision", limit=None
    )
    assert [row["payload"]["selected_budget"] for row in decisions] == [500_000.0]
    assert list_incomplete_workflows(store) == []

def test_the_next_request_card_is_sent_before_the_workflow_is_closed(operator_bot, monkeypatch):
    """The ordering itself, not just its consequence under a crash."""
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    completed_when_delivering: list[int] = []
    original = operator_bot._deliver_child_signal_outcome

    def recording(*args, **kwargs):
        completed_when_delivering.append(
            len(store.list_system_events_by_type("funding_workflow_completed", limit=None))
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(operator_bot, "_deliver_child_signal_outcome", recording)
    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )

    assert completed_when_delivering == [0]
    assert len(store.list_system_events_by_type("funding_workflow_completed", limit=None)) == 1

def test_a_budget_selection_without_a_signal_config_is_refused_not_completed(operator_bot):
    """Critical 3: the selection is an input, so it cannot stand alone.

    Without a signal config there is no child run to feed the amount to.
    Completing anyway made the decision terminal -- the one thing this
    transition is defined not to be -- and told the operator their budget was
    selected while the month quietly went uninvested.
    """
    store = operator_bot.store
    operator_bot.signal_config_path = None
    publish_contribution_request(store, "run-1", _budget_request("req-1"), phase="budget")

    with pytest.raises(ValueError, match="signal-config"):
        operator_bot._confirm_budget_request(
            _budget_request("req-1"),
            selected_budget=500_000.0,
            chat_id=1,
            user_id=2,
            username="op",
        )

    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    assert (
        store.list_system_events_by_type("contribution_budget_request_decision", limit=None) == []
    )
    # Refused before the claim, so there is no half-entered transition to
    # recover either -- the request is simply still pending.
    assert store.list_system_events_by_type("funding_workflow_claim", limit=None) == []
    assert operator_bot._load_pending_budget_request("req-1") is not None

def test_a_resumed_attempt_adopts_an_approval_run_that_reported_completion(
    operator_bot, tmp_path
):
    """A resume re-enters the delivery step, and approve_signal refuses a
    package it already consumed. Without adopting the finished outcome the
    resume could never finish, and the workflow would stay stalled over work
    that was in fact complete."""
    store = operator_bot.store
    operator_bot.approval_config_path = _signal_config_path(tmp_path)
    store.save_signal_package("signal-child", {"orders_preview": []})
    store.mark_signal_package_consumed("signal-child", "approval-run-1")
    store.save_system_event(
        "approval-run-1",
        "signal_approval_completed",
        {"signal_run_id": "signal-child", "orders_created": 1},
    )

    assert operator_bot._dispatch_child_approval("signal-child") == [
        "approval_status: already_approved"
    ]

def test_a_consumed_approval_that_never_completed_is_not_adopted_as_success(
    operator_bot, tmp_path
):
    """Re-review Critical 2: consumed is not completed.

    approve_signal marks the package consumed before placing a single order
    and writes signal_approval_completed only after the last one. A crash in
    between can leave part of a batch at the broker. Treating that as
    "already approved" would close the funding workflow over a half-filled
    batch and drop it from the recovery list -- and it cannot simply be
    re-run either, so it has to surface.
    """
    store = operator_bot.store
    operator_bot.approval_config_path = _signal_config_path(tmp_path)
    store.save_signal_package("signal-child", {"orders_preview": []})
    store.mark_signal_package_consumed("signal-child", "approval-run-1")

    with pytest.raises(ValueError, match="never reported completion"):
        operator_bot._dispatch_child_approval("signal-child")

def test_a_child_needing_approval_without_an_approval_config_stays_recoverable(
    operator_bot, monkeypatch
):
    """Re-review Critical 1: a status string the caller reads as success.

    An action-required child with no approval config used to return
    "not_created_missing_approval_config", and the caller completed the
    workflow on that. The month then had a decision on record, a child run
    needing approval, no approval card and no resume card. Raising keeps the
    workflow claimed-but-incomplete so it can be resumed once the config is
    supplied.
    """
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    assert operator_bot.approval_config_path is None
    monkeypatch.setattr(
        operator_bot,
        "_run_child_signal",
        lambda *args, **kwargs: SignalRunSummary(
            signal_run_id="signal-child",
            loaded_strategies=["tranquillo"],
            action_required=True,
            orders_preview_count=3,
        ),
    )

    with pytest.raises(ValueError, match="--approval-config"):
        operator_bot._confirm_funding_request(
            _request("req-1"), chat_id=1, user_id=2, username="op"
        )

    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    assert [row["request_id"] for row in list_incomplete_workflows(store)] == ["req-1"]

def test_a_resumed_attempt_adopts_a_dispatch_that_already_settled(operator_bot, tmp_path):
    """The same adoption for the live_approval/Telegram path, where
    dispatch_signal_approval is what refuses a package whose approvals were
    already sent."""
    store = operator_bot.store
    operator_bot.approval_config_path = _live_approval_config_path(tmp_path)
    store.save_signal_package("signal-child", {"orders_preview": []})
    store.mark_signal_package_consumed("signal-child", "approval-run-1")
    store.save_system_event(
        "approval-run-1", "signal_approval_pending", {"signal_run_id": "signal-child"}
    )
    assert store.signal_dispatch_settled("signal-child")

    assert operator_bot._dispatch_child_approval("signal-child") == [
        "approval_status: already_dispatched"
    ]


def _request_cards(operator_bot, request_id: str) -> list[dict[str, Any]]:
    return [
        message
        for message in operator_bot.client.sent_messages
        if request_id in str(message.get("text") or "")
        and message.get("reply_markup") is not None
    ]

def test_a_follow_up_request_card_is_delivered_once_per_chat(operator_bot):
    """Re-review Important 3: the delivery is replayed, the card must not be.

    Completion happens after delivery, so a crash in between means a resume
    runs the delivery again. A plain send would leave two live decision
    buttons in the chat for a request that accepts exactly one decision --
    and the second tap is then refused by the claim, which reads as a
    malfunction rather than as the duplicate card it is.
    """
    request = _request("req-1")

    operator_bot._send_funding_request(100, request)
    operator_bot._send_funding_request(100, request)

    assert len(_request_cards(operator_bot, "req-1")) == 1

def test_the_same_request_still_reaches_a_second_chat(operator_bot):
    """Deduplicated per chat, not per request: a chat with no copy has not
    been told anything yet."""
    request = _request("req-1")

    operator_bot._send_funding_request(100, request)
    operator_bot._send_funding_request(200, request)

    assert [card["chat_id"] for card in _request_cards(operator_bot, "req-1")] == [100, 200]

def test_a_card_whose_delivery_is_unknown_is_not_sent_again(operator_bot):
    """A send that died mid-call is not proof of non-delivery, and Telegram
    offers no way to ask. Sending again is how the duplicate is created, so
    the copy stays unknown and the operator is told in plain text instead."""
    request = _request("req-1")
    original_send = operator_bot.client.send_message

    def die_mid_call(*args, **kwargs):
        raise TimeoutError("connection dropped after Telegram accepted it")

    operator_bot.client.send_message = die_mid_call
    operator_bot._send_funding_request(100, request)
    operator_bot.client.send_message = original_send

    operator_bot._send_funding_request(100, request)

    assert _request_cards(operator_bot, "req-1") == []
    notices = [
        message
        for message in operator_bot.client.sent_messages
        if message.get("reply_markup") is None
    ]
    assert notices, "the operator must hear about a copy we cannot account for"

def test_a_budget_card_is_also_delivered_only_once(operator_bot):
    request = _budget_request("req-1")

    operator_bot._send_budget_request(100, request)
    operator_bot._send_budget_request(100, request)

    assert len(_request_cards(operator_bot, "req-1")) == 1


def _stub_child_signal(monkeypatch, operator_bot, *, funding_requests=None, budget_requests=None):
    """Skip the real strategy run, but keep everything it publishes.

    The child run does two things the delivery step depends on: it writes
    the signal package, and it publishes the follow-up requests that package
    advertises. Both happen *inside* the stub rather than at setup time
    because ordering matters -- publishing a follow-up request supersedes
    the parent request in the same scope, so doing it before the parent
    claims would make the parent's own claim fail as not_head.
    """
    store = operator_bot.store
    funding_requests = funding_requests or []
    budget_requests = budget_requests or []

    def run_child(*args, **kwargs):
        for request in budget_requests:
            publish_contribution_request(store, "run-child", request, phase="budget")
        for request in funding_requests:
            publish_contribution_request(store, "run-child", request, phase="funding")
        store.save_signal_package(
            "signal-child",
            {
                "orders_preview": [],
                "funding_requests": funding_requests,
                "budget_requests": budget_requests,
            },
        )
        return SignalRunSummary(
            signal_run_id="signal-child",
            loaded_strategies=["tranquillo"],
            action_required=False,
            orders_preview_count=0,
        )

    monkeypatch.setattr(operator_bot, "_run_child_signal", run_child)

def test_a_telegram_rejected_follow_up_card_leaves_the_workflow_incomplete(
    operator_bot, monkeypatch
):
    """Re-review Critical 2: deliver_once's answer used to be discarded.

    _send_request_card threw away whether the follow-up card actually
    reached the operator, so _deliver_child_signal_outcome ran to completion
    and the caller went straight into complete_workflow regardless. Here
    Telegram explicitly refuses the very first send (not a manual second
    call to _send_funding_request, which is all the old test covered) --
    the real operator path (_confirm_funding_request) must now surface that
    as an exception and leave the workflow claimed-but-incomplete, the same
    way a crash in delivery already does.
    """
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    _stub_child_signal(monkeypatch, operator_bot, funding_requests=[_request("req-2")])

    def reject(chat_id, text, reply_markup=None):
        raise TelegramApiRejected("telegram refused the funding request card")

    monkeypatch.setattr(operator_bot.client, "send_message", reject)

    with pytest.raises(RuntimeError, match="refusing to complete the workflow"):
        operator_bot._confirm_funding_request(
            _request("req-1"), chat_id=1, user_id=2, username="op"
        )

    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    # The undelivered request is what the operator is told about, and it has
    # to be: publishing req-2 superseded req-1, and list_incomplete_workflows
    # drops a request that is no longer head -- so in exactly the case that
    # has a card to deliver, the parent cannot be surfaced by the resume
    # sweep. req-2 has no claim either (nobody could tap a card that never
    # arrived), so neither request reaches that list.
    assert list_incomplete_workflows(store) == []
    undelivered = store.list_system_events_by_type(
        "funding_request_card_undelivered", limit=None
    )
    assert [row["payload"]["request_id"] for row in undelivered] == ["req-2"]
    assert undelivered[0]["payload"]["delivery"] == "failed"

def test_a_telegram_timeout_on_a_follow_up_card_leaves_the_workflow_incomplete(
    operator_bot, monkeypatch
):
    """The unknown-outcome twin of the rejection case above: a timeout gives
    no proof of non-delivery either way, so it must be treated the same as a
    refusal for the purpose of completing the workflow -- and, unlike a
    plain refusal, it must also leave the operator an ambiguity notice."""
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="budget")
    _stub_child_signal(monkeypatch, operator_bot, budget_requests=[_budget_request("req-2")])

    # Only the very first call (the request card itself) times out; the
    # ambiguity notice deliver_once sends right after must still go through,
    # the same way a single dropped request does not mean the connection is
    # down for the rest of the process.
    original_send = operator_bot.client.send_message
    call_count = {"n": 0}

    def flaky_once(chat_id, text, reply_markup=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise TimeoutError("connection dropped after Telegram accepted it")
        return original_send(chat_id, text, reply_markup=reply_markup)

    monkeypatch.setattr(operator_bot.client, "send_message", flaky_once)

    with pytest.raises(RuntimeError, match="refusing to complete the workflow"):
        operator_bot._confirm_budget_request(
            _budget_request("req-1"),
            selected_budget=500_000.0,
            chat_id=1,
            user_id=2,
            username="op",
        )

    assert store.list_system_events_by_type("funding_workflow_completed", limit=None) == []
    undelivered = store.list_system_events_by_type(
        "funding_request_card_undelivered", limit=None
    )
    assert [row["payload"]["request_id"] for row in undelivered] == ["req-2"]
    assert undelivered[0]["payload"]["delivery"] == "unknown"
    notices = [
        message
        for message in operator_bot.client.sent_messages
        if message.get("reply_markup") is None
    ]
    assert notices, "the operator must be told a copy could not be accounted for"


def _other_scope_request(request_id: str) -> dict[str, Any]:
    """A follow-up request in a different account scope.

    Same-scope follow-ups supersede the parent (one head per scope+month),
    which takes the parent out of the resume sweep entirely; these tests are
    about the parent that is still resumable, so the follow-up has to live in
    its own workflow.
    """
    return {**_request(request_id), "account_id": "paper_cash_2"}


def test_a_follow_up_already_decided_is_not_re_delivered(operator_bot, monkeypatch):
    """A resume reaches the delivery step again, and by then the card it
    could not confirm has usually arrived and been acted on. Demanding
    delivery of a request that is already decided would keep the parent
    permanently uncompletable -- the card is never re-sent, so delivery can
    never be confirmed, so completion can never proceed."""
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    follow_up = _other_scope_request("req-2")
    _stub_child_signal(monkeypatch, operator_bot, funding_requests=[follow_up])
    # The operator already dealt with the follow-up card. (Publishing it here
    # rather than leaving it to the stub is what lets the claim below land;
    # the stub's own publish then reports it as already_published.)
    publish_contribution_request(store, "run-x", follow_up, phase="funding")
    workflow_id = workflow_id_from_request(follow_up)
    claim_workflow_attempt(
        store,
        new_run_id(),
        workflow_id=workflow_id,
        request_id="req-2",
        phase="funding",
        extra={"intent": "confirm"},
    )
    from maestro.state.funding_workflow import complete_workflow

    complete_workflow(
        store,
        new_run_id(),
        workflow_id=workflow_id,
        request_id="req-2",
        phase="funding",
        attempt=1,
        legacy_payload={"request_id": "req-2", "status": "confirmed", "decided_by": "op"},
    )
    before = len(operator_bot.client.sent_messages)

    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )

    assert operator_bot.client.sent_messages[before:] == []
    completed = store.list_system_events_by_type("funding_workflow_completed", limit=None)
    assert "req-1" in [row["payload"]["request_id"] for row in completed]


def test_a_follow_up_already_replaced_is_not_re_delivered(operator_bot, monkeypatch):
    """The other way a follow-up stops needing a card: a later run published
    a replacement, so the one this package names is no longer head."""
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    follow_up = _other_scope_request("req-2")
    _stub_child_signal(monkeypatch, operator_bot, funding_requests=[follow_up])
    publish_contribution_request(store, "run-x", follow_up, phase="funding")
    publish_contribution_request(
        store, "run-y", _other_scope_request("req-3"), phase="funding"
    )
    before = len(operator_bot.client.sent_messages)

    operator_bot._confirm_funding_request(
        _request("req-1"), chat_id=1, user_id=2, username="op"
    )

    assert operator_bot.client.sent_messages[before:] == []
    completed = store.list_system_events_by_type("funding_workflow_completed", limit=None)
    assert "req-1" in [row["payload"]["request_id"] for row in completed]


def _stall_at_attempt(store, request_id: str, attempt: int) -> str:
    """Leave a claim at ``attempt`` with no completion, the way a resume that
    died between claim and completion does."""
    workflow_id = _workflow_id_of(request_id)
    for number in range(1, attempt + 1):
        claim = claim_workflow_attempt(
            store,
            new_run_id(),
            workflow_id=workflow_id,
            request_id=request_id,
            phase="funding",
            attempt=number,
            extra={"intent": "confirm"},
        )
        assert claim["claimed"], number
    return workflow_id


def test_the_sweep_stops_offering_resume_once_the_budget_is_spent(operator_bot):
    """A resume that always fails the same way produced a fresh Resume card
    after every tap: each tap commits a new attempt, and the stall notice is
    keyed per attempt. Past the budget the transition needs a person."""
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    _stall_at_attempt(store, "req-1", 4)

    operator_bot._sweep_incomplete_workflows()

    assert len(operator_bot.client.sent_messages) == 1
    message = operator_bot.client.sent_messages[0]
    assert "재개를 여러 번 시도했지만" in message["text"]
    # No Resume button: tapping again is exactly what stopped working.
    assert message["reply_markup"] is None
    assert _wfresume_statuses(store) == []


def test_the_needs_attention_notice_is_sent_once_not_per_sweep(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    _stall_at_attempt(store, "req-1", 4)

    operator_bot._sweep_incomplete_workflows()
    operator_bot._sweep_incomplete_workflows()

    assert len(operator_bot.client.sent_messages) == 1


def test_a_stale_resume_card_cannot_burn_more_attempts(operator_bot):
    """The sweep stops sending Resume cards, but the ones already in the chat
    still carry a button. Tapping one must not commit another claim."""
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    _stall_at_attempt(store, "req-1", 4)

    assert operator_bot.process_update(callback_update("operator:wfresume:funding:req-1"))

    assert _wfresume_statuses(store) == ["resume_budget_exhausted"]
    claims = store.list_system_events_by_type("funding_workflow_claim", limit=None)
    assert sorted(row["payload"]["attempt"] for row in claims) == [1, 2, 3, 4]
    assert "재개를 여러 번 시도했지만" in operator_bot.client.edited_messages[-1]["text"]


def test_a_workflow_inside_the_budget_is_still_offered_resume(operator_bot):
    store = operator_bot.store
    publish_contribution_request(store, "run-1", _request("req-1"), phase="funding")
    _stall_at_attempt(store, "req-1", 3)

    operator_bot._sweep_incomplete_workflows()

    message = operator_bot.client.sent_messages[-1]
    assert message["reply_markup"]["inline_keyboard"][0][0]["text"] == "Resume"

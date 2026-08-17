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
    claim_workflow_attempt,
    list_incomplete_workflows,
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
    budget_workflow_id = publish_contribution_request(
        store, "run-2", _budget_request(budget_request_id), phase="budget"
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

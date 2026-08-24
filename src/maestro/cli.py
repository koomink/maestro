import fcntl
import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, NamedTuple, TypeVar
from zoneinfo import ZoneInfo

import typer
import yaml

from maestro.config.app_fragment_composition import app_fragment_recommendation_failures
from maestro.config.env import load_default_env_files, load_env_file
from maestro.config.identity import ConfigIdentity
from maestro.config.loader import load_config_with_identity
from maestro.config.models import MaestroConfig
from maestro.core.clock import utc_now
from maestro.core.enums import ProfileStage, RunMode
from maestro.core.ids import new_run_id
from maestro.core.time_display import format_operator_time, operator_timezone
from maestro.credentials import DEFAULT_CREDENTIAL_RESOLVER
from maestro.execution.account_cash_flows import (
    AccountCashFlowService,
    account_cash_flow_leg_duplicate_key,
)
from maestro.execution.broker_state import portfolio_state_from_broker_account
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.brokers.readonly_factory import (
    broker_readonly_account_ids,
    broker_readonly_accounts,
    build_broker_readonly_services,
)
from maestro.execution.brokers.toss.order_history_backfill import (
    TossOrderHistoryBackfillService,
)
from maestro.execution.budget_requests import (
    budget_request_reply_markup,
    format_contribution_budget_request,
)
from maestro.execution.funding_requests import (
    format_contribution_funding_request,
    funding_request_reply_markup,
)
from maestro.execution.live_order_factory import (
    build_live_order_notification_client,
    build_live_order_status_client,
)
from maestro.execution.live_order_ports import LiveOrderStatusClient
from maestro.execution.live_order_tracking import LiveOrderTrackingResumeService
from maestro.execution.live_orders import PartialFillReconciliationService
from maestro.execution.reconciliation import BrokerReconciliationService
from maestro.fx.service import ConfiguredFXRefreshService
from maestro.integrations.telegram.bot import TelegramBotAPIClient
from maestro.integrations.telegram.handlers import (
    TelegramOperatorCommandRouter,
    telegram_bot_commands,
)
from maestro.integrations.telegram.ui import catalog as ui_catalog
from maestro.monitoring.audit_logger import AuditLogger
from maestro.monitoring.health import HealthService
from maestro.monitoring.logging import configure_structured_logging
from maestro.ops import quiesce
from maestro.ops.batch_execution import (
    SettlementRefused,
    build_order_evidence,
    settle_approval,
    summarize_batch,
)
from maestro.ops.evidence import build_operator_evidence
from maestro.ops.preflight import private_beta_failures
from maestro.ops.readonly_refresh import latest_snapshot_for_account, refresh_readonly_accounts
from maestro.ops.workflow_recovery import WorkflowRecoveryService
from maestro.orchestration.orchestrator import (
    MaestroOrchestrator,
    signal_contract_fingerprint_diff,
)
from maestro.portfolio.account_attribution import AccountAttributionReconciliationService
from maestro.safety.controls import SafetyControlService
from maestro.scaffold import create_virtuoso_app_scaffold
from maestro.state.events import (
    CASH_SUSPENSE_CLASSIFICATIONS,
    EXTERNAL_TRANSFER,
    FX_CONVERSION,
    INTERNAL_TRANSFER,
    SystemEventType,
    flow_class_for_cash_suspense,
    save_audited_system_event,
)
from maestro.state.models import PortfolioState
from maestro.state.rollback_preflight import run_rollback_preflight
from maestro.state.store import StateStore
from maestro.state.upgrade_backfill import UpgradeResult, run_upgrade_backfill

app = typer.Typer()
performance_baseline_app = typer.Typer()
cash_flow_app = typer.Typer()
ledger_app = typer.Typer()
cash_drift_app = typer.Typer()
app.add_typer(performance_baseline_app, name="performance-baseline")
app.add_typer(cash_flow_app, name="cash-flow")
app.add_typer(ledger_app, name="ledger")
app.add_typer(cash_drift_app, name="cash-drift")

CONFIG_ENV_VAR = "MAESTRO_CONFIG"
CONFIG_OPTION = typer.Option(
    None,
    "--config",
    envvar=CONFIG_ENV_VAR,
    help=f"Path to operator config. Defaults to ${CONFIG_ENV_VAR}.",
)
T = TypeVar("T")


def _load_dotenv() -> None:
    load_default_env_files()


@app.callback()
def main() -> None:
    """Maestro command line interface."""
    _load_dotenv()
    configure_structured_logging()


def _resolve_config(config: Path | None) -> Path:
    if config is not None:
        return config
    env_config = os.getenv(CONFIG_ENV_VAR)
    if env_config:
        return Path(env_config)
    raise typer.BadParameter(f"--config is required or set {CONFIG_ENV_VAR}")


def _load_operator_config(config: Path | None) -> tuple[MaestroConfig, ConfigIdentity]:
    return load_config_with_identity(_resolve_config(config))


def _state_store(
    maestro_config: MaestroConfig,
    identity: ConfigIdentity,
) -> StateStore:
    return StateStore(
        maestro_config.state.sqlite_path,
        maestro_config.portfolio.initial_cash,
        maestro_config.portfolio.cash_by_currency,
        config_identity=identity,
    )


def _broker_snapshot_refresher(
    maestro_config: MaestroConfig,
    store: StateStore,
    audit: AuditLogger,
):
    readonly_services = build_broker_readonly_services(maestro_config, store, audit)
    if not readonly_services:
        return None

    def refresh() -> None:
        for _, service in readonly_services:
            service.fetch_and_store_snapshot(maestro_config.portfolio.allowed_symbols)

    return refresh


def _kis_readonly_accounts(maestro_config: MaestroConfig):
    return [
        (
            account_id,
            account.to_kis_config() if getattr(account, "broker", None) == "kis" else account,
        )
        for account_id, account in broker_readonly_accounts(maestro_config)
    ]


def _reconciliation_account_ids(maestro_config: MaestroConfig) -> list[str]:
    return broker_readonly_account_ids(maestro_config)


def _profile_datahub_providers(maestro_config: MaestroConfig) -> str:
    return ",".join(
        f"{provider.name}:{provider.provider}"
        for provider in maestro_config.datahub.effective_providers()
        if provider.enabled
    )


@app.command("run-once")
def run_once(
    config: Path | None = CONFIG_OPTION,
    stop_telegram_operator: bool = typer.Option(
        True,
        "--stop-telegram-operator/--keep-telegram-operator",
        help="Stop the polling Telegram operator while approval polling is active.",
    ),
    telegram_operator_service: str = typer.Option(
        "maestro-telegram-operator.service",
        "--telegram-operator-service",
        envvar="MAESTRO_TELEGRAM_OPERATOR_SERVICE",
    ),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.approval.provider != "telegram":
        stop_telegram_operator = False
    try:
        summary = _with_telegram_operator_stopped(
            stop_telegram_operator,
            telegram_operator_service,
            lambda: MaestroOrchestrator(maestro_config, config_identity=identity).run_once(),
        )
    except Exception as exc:
        _send_run_once_failure_notification(maestro_config, exc)
        raise
    typer.echo(
        f"run_id={summary.run_id} strategies={summary.loaded_strategies} "
        f"orders={summary.orders_created} total_value={summary.total_value:.2f} "
        f"cash={summary.cash:.2f}"
    )
    _send_run_once_success_notification(maestro_config, summary)


@app.command("run-signal")
def run_signal(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    summary = MaestroOrchestrator(maestro_config, config_identity=identity).run_signal()
    typer.echo(
        f"signal_run_id={summary.signal_run_id} "
        f"strategies={summary.loaded_strategies} "
        f"action_required={str(summary.action_required).lower()} "
        f"orders_preview={summary.orders_preview_count}"
    )


@app.command("approve-signal")
def approve_signal(
    config: Path | None = CONFIG_OPTION,
    signal_run_id: str = typer.Option(..., "--signal-run-id"),
    stop_telegram_operator: bool = typer.Option(
        True,
        "--stop-telegram-operator/--keep-telegram-operator",
        help="Deprecated; the Telegram operator remains active for async approvals.",
    ),
    telegram_operator_service: str = typer.Option(
        "maestro-telegram-operator.service",
        "--telegram-operator-service",
        envvar="MAESTRO_TELEGRAM_OPERATOR_SERVICE",
    ),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    orchestrator = MaestroOrchestrator(maestro_config, config_identity=identity)
    if (
        maestro_config.mode == RunMode.LIVE_APPROVAL
        and maestro_config.approval.provider == "telegram"
    ):
        if stop_telegram_operator:
            typer.echo("symphony_approve status=info reason=operator_kept_for_async_approval")
        summary = orchestrator.dispatch_signal_approval(signal_run_id)
        orders = summary.orders_planned
        pending = summary.approvals_pending
    else:
        summary = _with_telegram_operator_stopped(
            stop_telegram_operator,
            telegram_operator_service,
            lambda: orchestrator.approve_signal(signal_run_id),
        )
        orders = summary.orders_created
        pending = 0
    typer.echo(
        f"signal_run_id={summary.signal_run_id} run_id={summary.run_id} "
        f"orders={orders} approvals_pending={pending} "
        f"approval_status={summary.approval_status}"
    )


@app.command("daily-signal-approval")
def daily_signal_approval(
    readonly_config: Path | None = typer.Option(
        None,
        "--readonly-config",
        envvar="MAESTRO_READONLY_CONFIG",
        help="Read-only config used for broker snapshot and reconciliation refresh.",
    ),
    signal_config: Path | None = typer.Option(
        None,
        "--signal-config",
        envvar="MAESTRO_SIGNAL_CONFIG",
        help="Signal config used to generate the daily signal package.",
    ),
    approval_config: Path | None = typer.Option(
        None,
        "--approval-config",
        envvar="MAESTRO_APPROVAL_CONFIG",
        help="Approval config used to consume actionable signal packages.",
    ),
    stop_telegram_operator: bool = typer.Option(
        True,
        "--stop-telegram-operator/--keep-telegram-operator",
        help="Stop the polling Telegram operator while approval polling is active.",
    ),
    telegram_operator_service: str = typer.Option(
        "maestro-telegram-operator.service",
        "--telegram-operator-service",
        envvar="MAESTRO_TELEGRAM_OPERATOR_SERVICE",
    ),
    strategy_ids: str | None = typer.Option(
        None,
        "--strategy-ids",
        help=(
            "Comma-separated strategy ids to scope the daily signal run, "
            "e.g. per-market schedules that run KR and US strategies in "
            "their own market sessions."
        ),
    ),
    contribution_override: bool = typer.Option(
        False,
        "--contribution-override",
        help=(
            "Bypass the contribution buy_day schedule so manual rebalance "
            "runs can generate contribution orders immediately. Contributions "
            "already executed this month are still skipped."
        ),
    ),
    lock_path: Path = typer.Option(
        Path("/tmp/maestro-symphony-signal.lock"),
        "--lock-path",
        envvar="MAESTRO_SIGNAL_LOCK_PATH",
    ),
) -> None:
    selected_strategy_ids = _parse_strategy_ids(strategy_ids)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            typer.echo(f"symphony_daily status=locked lock_path={lock_path}")
            raise typer.Exit(75) from None
        try:
            _run_daily_signal_approval(
                readonly_config=readonly_config,
                signal_config=signal_config,
                approval_config=approval_config,
                stop_telegram_operator=stop_telegram_operator,
                telegram_operator_service=telegram_operator_service,
                strategy_ids=selected_strategy_ids,
                contribution_override=contribution_override,
            )
        except Exception as exc:
            failure_config = signal_config or approval_config or readonly_config
            _send_daily_failure_notification(failure_config, exc)
            raise
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _parse_strategy_ids(strategy_ids: str | None) -> list[str] | None:
    if strategy_ids is None:
        return None
    parsed = [item.strip() for item in strategy_ids.split(",") if item.strip()]
    if not parsed:
        raise typer.BadParameter("--strategy-ids requires at least one strategy id")
    return parsed


def _run_daily_signal_approval(
    *,
    readonly_config: Path | None,
    signal_config: Path | None,
    approval_config: Path | None,
    stop_telegram_operator: bool,
    telegram_operator_service: str,
    strategy_ids: list[str] | None = None,
    contribution_override: bool = False,
) -> None:
    # Account-scoped signal preflight is owned by MaestroOrchestrator.  Keeping it
    # there also protects manual/API signal entry points from bypassing freshness.
    _load_operator_config(readonly_config)

    signal_maestro_config, signal_identity = _load_operator_config(signal_config)
    approval_maestro_config, approval_identity = _load_operator_config(approval_config)
    contract_diff_keys = signal_contract_fingerprint_diff(
        signal_maestro_config,
        approval_maestro_config,
    )
    if contract_diff_keys:
        raise ValueError(
            "signal/approval config contract mismatch: " + ", ".join(contract_diff_keys)
        )

    signal_summary = MaestroOrchestrator(
        signal_maestro_config,
        config_identity=signal_identity,
    ).run_signal(
        strategy_ids=strategy_ids,
        contribution_override=contribution_override,
    )
    typer.echo(
        f"symphony_daily status=signal_completed "
        f"signal_run_id={signal_summary.signal_run_id} "
        f"action_required={str(signal_summary.action_required).lower()} "
        f"orders_preview={signal_summary.orders_preview_count} "
        f"contribution_override={str(signal_summary.contribution_override).lower()}"
    )
    _send_signal_summary_notification(signal_maestro_config, signal_summary)

    if not signal_summary.action_required:
        budget_sent = _send_signal_budget_request_notifications(
            signal_maestro_config,
            signal_summary.signal_run_id,
        )
        # Funding requests must go out even when a budget request was also
        # raised (e.g. kis_isa needs a budget pick while kis_ps needs a top-up);
        # otherwise the funding account silently drops out of the run.
        funding_sent = _send_signal_funding_request_notifications(
            signal_maestro_config,
            signal_summary.signal_run_id,
        )
        # 분기는 전달 성공이 아니라 **요청이 있었는지**로 가른다. 전송에
        # 실패한 날을 조용한 날로 보고하면, 입금이 필요한 날에 운영자가
        # "오늘은 매매할 것이 없어요"를 받는다 -- 카드가 안 오는 것보다 나쁘다.
        undelivered = [
            kind
            for kind, outcome in (("budget", budget_sent), ("funding", funding_sent))
            if outcome.failed
        ]
        if undelivered:
            typer.echo(
                f"symphony_daily status=request_delivery_failed "
                f"kinds={','.join(undelivered)} "
                f"signal_run_id={signal_summary.signal_run_id}"
            )
        if budget_sent:
            typer.echo(
                f"symphony_daily status=budget_required "
                f"signal_run_id={signal_summary.signal_run_id}"
            )
            return
        if funding_sent:
            typer.echo(
                f"symphony_daily status=funding_required "
                f"signal_run_id={signal_summary.signal_run_id}"
            )
        else:
            typer.echo(
                f"symphony_daily status=no_action signal_run_id={signal_summary.signal_run_id}"
            )
            # 아무 일도 없었다는 것도 소식이다. 침묵은 조용한 하루와 죽은 봇을
            # 구분해 주지 않는다. 카드가 아니라 한 줄이므로 lifecycle을 거치지
            # 않는다 -- 갱신할 상태가 없다.
            _send_no_action_notice(signal_maestro_config, signal_identity, signal_summary)
        return

    approval_orchestrator = MaestroOrchestrator(
        approval_maestro_config,
        config_identity=approval_identity,
    )
    if (
        approval_maestro_config.mode == RunMode.LIVE_APPROVAL
        and approval_maestro_config.approval.provider == "telegram"
    ):
        approval_summary = approval_orchestrator.dispatch_signal_approval(
            signal_summary.signal_run_id
        )
        orders = approval_summary.orders_planned
        pending = approval_summary.approvals_pending
        status_label = "approval_pending"
    else:
        approval_summary = _with_telegram_operator_stopped(
            stop_telegram_operator,
            telegram_operator_service,
            lambda: approval_orchestrator.approve_signal(signal_summary.signal_run_id),
        )
        orders = approval_summary.orders_created
        pending = 0
        status_label = "approval_completed"
    typer.echo(
        f"symphony_daily status={status_label} "
        f"signal_run_id={approval_summary.signal_run_id} "
        f"run_id={approval_summary.run_id} "
        f"orders={orders} "
        f"approvals_pending={pending} "
        f"approval_status={approval_summary.approval_status}"
    )


def _refresh_daily_readonly(
    maestro_config: MaestroConfig,
    identity: ConfigIdentity,
) -> None:
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        typer.echo(f"symphony_daily readonly=skipped reason=mode mode={maestro_config.mode.value}")
        return
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    readonly_services = build_broker_readonly_services(maestro_config, store, audit)
    if not readonly_services:
        typer.echo("symphony_daily readonly=skipped reason=no_broker_accounts")
        return
    for logical_account_id, service in readonly_services:
        account_label = logical_account_id or "default_kis"
        try:
            service.fetch_and_store_snapshot(maestro_config.portfolio.allowed_symbols)
        except (RuntimeError, TimeoutError, ValueError) as exc:
            message = f"readonly refresh failed for account {account_label}: {exc}"
            typer.echo(message)
            typer.echo(f"symphony_daily readonly=failed account={account_label} message={exc}")
            raise ValueError(message) from exc
    result = BrokerReconciliationService(
        maestro_config.reconciliation,
        store,
        audit,
        account_ids=_reconciliation_account_ids(maestro_config),
    ).reconcile_latest()
    status = "passed" if result.passed else "failed"
    typer.echo(
        f"symphony_daily readonly=refreshed accounts_synced={len(readonly_services)} "
        f"reconciliation={status} issues={len(result.issues)}"
    )
    if not result.passed:
        raise ValueError(_format_reconciliation_failure(result))


def _format_reconciliation_failure(result) -> str:
    issue_summaries = []
    for issue in result.issues[:3]:
        symbol = f":{issue.symbol}" if issue.symbol else ""
        issue_summaries.append(f"{issue.issue_type}{symbol}: {issue.message}")
    if len(result.issues) > 3:
        issue_summaries.append(f"+{len(result.issues) - 3} more")
    details = "; ".join(issue_summaries) if issue_summaries else "no issue details"
    return f"reconciliation failed with {len(result.issues)} issue(s): {details}"


def _send_daily_failure_notification(config_path: Path | None, exc: Exception) -> None:
    if config_path is None:
        return
    try:
        maestro_config, _ = _load_operator_config(config_path)
    except Exception as config_exc:
        typer.echo(f"telegram_daily_failure=warn message={config_exc}")
        return
    if maestro_config.approval.provider != "telegram":
        return
    chat_ids = maestro_config.approval.telegram_allowed_chat_ids
    if not chat_ids:
        return
    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        typer.echo("telegram_daily_failure=warn message=missing_bot_token")
        return
    error_message = _single_line_error(exc)
    message = "\n".join(
        [
            "Maestro daily briefing failed",
            f"stage: {_daily_failure_stage(error_message)}",
            f"error: {error_message}",
        ]
    )
    try:
        client = TelegramBotAPIClient(
            token_env=maestro_config.approval.telegram_bot_token_env,
            timeout_seconds=10.0,
        )
        for chat_id in chat_ids:
            client.send_message(chat_id, message)
    except (RuntimeError, TimeoutError, TypeError, ValueError) as send_exc:
        typer.echo(f"telegram_daily_failure=warn message={send_exc}")
        return
    typer.echo(f"telegram_daily_failure=sent chats={len(chat_ids)}")


def _daily_failure_stage(error_message: str) -> str:
    if "readonly refresh" in error_message:
        return "readonly_refresh"
    if "reconciliation" in error_message:
        return "reconciliation"
    if "signal" in error_message:
        return "signal"
    if "approval" in error_message:
        return "approval"
    return "daily_signal_approval"


def _single_line_error(exc: Exception) -> str:
    message = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    return message[:500]


class RequestNotification(NamedTuple):
    """What a day's funding/budget request notification actually did.

    A single count cannot carry this. The caller uses it to decide whether
    today was a quiet day, and "nothing to send" and "something to send that
    did not go out" are opposite answers to that question -- both of which a
    count collapses to zero. That collapse is what made a failed send
    announce itself to the operator as "오늘은 매매할 것이 없어요".
    """

    requested: int
    delivered: int
    failed: bool

    def __bool__(self) -> bool:
        """Truthy when the day raised something, delivered or not."""
        return self.requested > 0


_NOTHING_REQUESTED = RequestNotification(requested=0, delivered=0, failed=False)


def _send_signal_request_notifications(
    maestro_config: MaestroConfig,
    signal_run_id: str,
    *,
    kind: str,
    package_key: str,
    render: Callable[[Mapping[str, Any]], tuple[str, dict[str, Any]]],
) -> RequestNotification:
    """Send one day's funding or budget request cards.

    The package is read before the channel is checked, and that order is the
    point: ``requested`` has to be right even when there is no way to send.
    A missing bot token on a day with a funding request is not a quiet day,
    and reporting it as one is worse than sending nothing -- the operator is
    told there was nothing to do.

    ``delivered`` counts sends that actually returned. It used to be
    computed as requests x chats after the loop, so a partial send looked
    total and an exception midway discarded the sends that had succeeded.
    """
    store = StateStore(
        maestro_config.state.sqlite_path,
        maestro_config.portfolio.initial_cash,
        maestro_config.portfolio.cash_by_currency,
    )
    signal = store.load_signal_package(signal_run_id) or {}
    requests = signal.get(package_key) or []
    if not requests:
        return _NOTHING_REQUESTED
    requested = len(requests)

    def unsendable(reason: str) -> RequestNotification:
        typer.echo(f"telegram_{kind}_request=warn message={reason}")
        return RequestNotification(requested=requested, delivered=0, failed=True)

    if maestro_config.approval.provider != "telegram":
        return unsendable("approval_provider_not_telegram")
    chat_ids = maestro_config.approval.telegram_allowed_chat_ids
    if not chat_ids:
        return unsendable("no_allowed_chat_ids")
    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        return unsendable("missing_bot_token")

    delivered = 0
    try:
        client = TelegramBotAPIClient(
            token_env=maestro_config.approval.telegram_bot_token_env,
            timeout_seconds=10.0,
        )
        for request in requests:
            message, markup = render(request)
            for chat_id in chat_ids:
                client.send_message(chat_id, message, reply_markup=markup)
                delivered += 1
    except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
        typer.echo(
            f"telegram_{kind}_request=warn message={exc} "
            f"requested={requested} delivered={delivered}"
        )
        return RequestNotification(requested=requested, delivered=delivered, failed=True)
    typer.echo(f"telegram_{kind}_request=sent messages={delivered}")
    return RequestNotification(requested=requested, delivered=delivered, failed=False)


def _send_signal_funding_request_notifications(
    maestro_config: MaestroConfig,
    signal_run_id: str,
) -> RequestNotification:
    def render(request: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        request_id = str(request.get("request_id") or "")
        return (
            format_contribution_funding_request(request),
            funding_request_reply_markup(request_id),
        )

    return _send_signal_request_notifications(
        maestro_config,
        signal_run_id,
        kind="funding",
        package_key="funding_requests",
        render=render,
    )


def _send_signal_budget_request_notifications(
    maestro_config: MaestroConfig,
    signal_run_id: str,
) -> RequestNotification:
    def render(request: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        return (
            format_contribution_budget_request(request),
            budget_request_reply_markup(request),
        )

    return _send_signal_request_notifications(
        maestro_config,
        signal_run_id,
        kind="budget",
        package_key="budget_requests",
        render=render,
    )


def _send_signal_summary_notification(maestro_config: MaestroConfig, summary) -> None:
    if maestro_config.approval.provider != "telegram":
        return
    chat_ids = maestro_config.approval.telegram_allowed_chat_ids
    if not chat_ids:
        return
    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        typer.echo("telegram_signal_summary=warn message=missing_bot_token")
        return
    strategies = ", ".join(summary.loaded_strategies) if summary.loaded_strategies else "none"
    lines = [
        "Maestro daily signal summary",
        f"signal_run_id: {summary.signal_run_id}",
        f"strategies: {strategies}",
        f"action_required: {str(summary.action_required).lower()}",
        f"orders_preview: {summary.orders_preview_count}",
    ]
    if getattr(summary, "contribution_override", False):
        lines.append("contribution_override: true (manual rebalance)")
    no_order_reasons = getattr(summary, "no_order_reasons", None) or []
    if summary.orders_preview_count == 0 and no_order_reasons:
        lines.append("no orders were generated because:")
        lines.extend(f"- {reason}" for reason in no_order_reasons)
    message = "\n".join(lines)
    try:
        client = TelegramBotAPIClient(
            token_env=maestro_config.approval.telegram_bot_token_env,
            timeout_seconds=10.0,
        )
        for chat_id in chat_ids:
            client.send_message(chat_id, message)
    except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
        typer.echo(f"telegram_signal_summary=warn message={exc}")
        return
    typer.echo(f"telegram_signal_summary=sent chats={len(chat_ids)}")


def _send_no_action_notice(
    maestro_config: MaestroConfig,
    identity: ConfigIdentity,
    summary,
) -> None:
    """오늘 매매할 것이 없었다고 한 줄로 알린다.

    채팅마다 독립적으로 보내고 성공한 채팅만 완료로 기록한다 -- 리마인더
    sweep과 같은 규약이다. 하나의 try로 묶으면 첫 채팅이 닿지 않을 때 나머지는
    시도조차 되지 않고, 어느 채팅이 빠졌는지도 남지 않는다.

    duplicate_key는 (운영 시간대 날짜, 전략 묶음, 채팅)이다. 날짜만으로 접으면
    같은 날 따로 도는 KR/US 런 중 한쪽이 침묵하고, signal_run_id로 잡으면 런을
    다시 돌릴 때마다 같은 말을 다시 보낸다.

    토큰이 없거나 전송이 실패해도 일간 실행 자체를 실패시키지 않는다 -- 이
    시점에는 이미 아무 주문도 만들지 않기로 끝난 뒤다.
    """
    if maestro_config.approval.provider != "telegram":
        return
    chat_ids = maestro_config.approval.telegram_allowed_chat_ids
    if not chat_ids:
        return
    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        typer.echo("telegram_no_action=warn message=missing_bot_token")
        return
    timezone = operator_timezone(maestro_config)
    today = utc_now().astimezone(ZoneInfo(timezone)).date().isoformat()
    scope = ",".join(sorted(getattr(summary, "loaded_strategies", None) or ["all"]))
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    client = TelegramBotAPIClient(
        token_env=maestro_config.approval.telegram_bot_token_env,
        timeout_seconds=10.0,
    )
    sent = 0
    for chat_id in chat_ids:
        duplicate_key = f"telegram-no-action:{today}:{scope}:{chat_id}"
        try:
            # 전송 **전에** 원자적으로 자리를 잡는다. duplicate_key의 UNIQUE
            # 인덱스가 곧 claim이므로 조회 후 기록 사이의 틈이 없다. 보내고 나서
            # 기록하면 그 사이에 프로세스가 죽었을 때 다음 실행이 같은 알림을
            # 다시 보낸다 -- 정상 재실행 테스트만으로는 보이지 않는 구멍이다.
            #
            # 그 대가로 이 알림은 at-most-once다: 전송이 실패한 채팅은 그날
            # 알림을 잃는다. 승인 카드와 달리 버튼이 없어 놓친 쪽이 덜 위험하고,
            # 세 상태(intent/result/failure)를 한 줄짜리 알림에 들이는 것보다
            # 단순하다. 놓친 채팅은 아래 경고로 남는다.
            save_audited_system_event(
                store,
                audit,
                summary.signal_run_id,
                "telegram_no_action_notice",
                {
                    # 이 이벤트는 전송 완료가 아니라 **자리를 잡았다**는 기록이다.
                    # 전송 전에 쓰기 때문에, 바로 뒤 전송이 실패했거나 프로세스가
                    # 죽었더라도 이 행은 남는다. 감사 시 "보냈다"로 읽히면
                    # 사실과 어긋난다.
                    "status": "claimed",
                    "chat_id": int(chat_id),
                    "notice_date": today,
                    "strategy_scope": scope,
                    "duplicate_key": duplicate_key,
                },
            )
        except sqlite3.IntegrityError:
            continue  # 이미 보냈거나, 보내는 중에 중단된 실행이 자리를 잡아 두었다
        except Exception as exc:  # noqa: BLE001 - 채팅 하나가 나머지를 막지 않는다
            typer.echo(f"telegram_no_action=warn chat={chat_id} message={exc}")
            continue
        try:
            client.send_message(int(chat_id), ui_catalog.NO_ACTION_NOTICE)
        except Exception as exc:  # noqa: BLE001 - 채팅 하나가 나머지를 막지 않는다
            typer.echo(f"telegram_no_action=warn chat={chat_id} message={exc}")
            continue
        sent += 1
    typer.echo(f"telegram_no_action=sent chats={sent}")


def _systemctl(action: str, service: str) -> None:
    subprocess.run(["systemctl", action, service], check=True)


def _with_telegram_operator_stopped(stop: bool, service: str, fn: Callable[[], T]) -> T:
    stopped = False
    try:
        if stop:
            try:
                _systemctl("stop", service)
                stopped = True
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                returncode = getattr(exc, "returncode", "missing")
                typer.echo(
                    "symphony_approve status=warn "
                    f"reason=telegram_operator_stop_failed service={service} "
                    f"returncode={returncode}"
                )
        return fn()
    finally:
        if stopped:
            try:
                _systemctl("start", service)
            except (subprocess.CalledProcessError, FileNotFoundError) as exc:
                returncode = getattr(exc, "returncode", "missing")
                typer.echo(
                    "symphony_approve status=warn "
                    f"reason=telegram_operator_restart_failed service={service} "
                    f"returncode={returncode}"
                )


def _send_run_once_success_notification(maestro_config: MaestroConfig, summary) -> None:
    strategies = ", ".join(summary.loaded_strategies) if summary.loaded_strategies else "none"
    base_currency = maestro_config.portfolio.base_currency
    message = "\n".join(
        [
            "Maestro run-once completed",
            f"run_id: {summary.run_id}",
            f"mode: {maestro_config.mode.value}",
            f"strategies: {strategies}",
            f"orders: {summary.orders_created}",
            f"total_value: {summary.total_value:,.2f} {base_currency}",
            f"cash: {summary.cash:,.2f} {base_currency}",
        ]
    )
    _send_run_once_telegram_notification(maestro_config, message)


def _send_run_once_failure_notification(maestro_config: MaestroConfig, exc: Exception) -> None:
    message = "\n".join(
        [
            "Maestro run-once failed",
            f"mode: {maestro_config.mode.value}",
            f"error_type: {type(exc).__name__}",
            f"error: {exc}",
        ]
    )
    _send_run_once_telegram_notification(maestro_config, message)


def _send_run_once_telegram_notification(
    maestro_config: MaestroConfig,
    message: str,
) -> None:
    if maestro_config.approval.provider != "telegram":
        return
    chat_ids = maestro_config.approval.telegram_allowed_chat_ids
    if not chat_ids:
        return
    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        typer.echo("telegram_notification=warn message=missing_bot_token")
        return
    try:
        client = TelegramBotAPIClient(
            token_env=maestro_config.approval.telegram_bot_token_env,
            timeout_seconds=10.0,
        )
        for chat_id in chat_ids:
            client.send_message(chat_id, message)
    except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
        typer.echo(f"telegram_notification=warn message={exc}")
        return
    typer.echo(f"telegram_notification=sent chats={len(chat_ids)}")


@app.command("status")
def status(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    current = store.load_latest_portfolio_state()
    store_status = store.status()
    operator_config = store_status.get("operator_config") or {}
    typer.echo(
        f"cash={current.cash:.2f} positions={len(current.positions)} "
        f"strategy_runs={store_status['counts']['strategy_runs']} "
        f"orders={store_status['counts']['orders']} "
        f"approvals={store_status['counts']['approvals']} "
        f"broker_snapshots={store_status['counts']['broker_account_snapshots']} "
        f"mode={maestro_config.mode.value} "
        f"order_posture={maestro_config.execution.order_posture} "
        f"config_path={operator_config.get('path', identity.path)} "
        f"config_fingerprint={operator_config.get('fingerprint', 'none')}"
        f" state_path={Path(maestro_config.state.sqlite_path).expanduser().resolve()}"
        f" audit_path={Path(maestro_config.audit.jsonl_path).expanduser().resolve()}"
    )


@app.command("profile-diff")
def profile_diff(
    left: Path = typer.Option(..., "--left", help="Left operator config path."),
    right: Path = typer.Option(..., "--right", help="Right operator config path."),
) -> None:
    left_config, left_identity = load_config_with_identity(left)
    right_config, right_identity = load_config_with_identity(right)
    profile_fields = {
        "mode": (left_config.mode.value, right_config.mode.value),
        "profile_stage": (
            left_config.profile_stage.value,
            right_config.profile_stage.value,
        ),
        "proposal_engine": (
            left_config.execution.proposal_engine,
            right_config.execution.proposal_engine,
        ),
        "order_posture": (
            left_config.execution.order_posture,
            right_config.execution.order_posture,
        ),
        "datahub_providers": (
            _profile_datahub_providers(left_config),
            _profile_datahub_providers(right_config),
        ),
        "kis_provider": (left_config.kis.provider, right_config.kis.provider),
        "kis_paper_trading": (
            str(left_config.kis.paper_trading).lower(),
            str(right_config.kis.paper_trading).lower(),
        ),
        "state_path": (
            str(Path(left_config.state.sqlite_path).expanduser().resolve()),
            str(Path(right_config.state.sqlite_path).expanduser().resolve()),
        ),
        "audit_path": (
            str(Path(left_config.audit.jsonl_path).expanduser().resolve()),
            str(Path(right_config.audit.jsonl_path).expanduser().resolve()),
        ),
    }
    for field, (left_value, right_value) in profile_fields.items():
        changed = str(left_value != right_value).lower()
        typer.echo(f"{field} left={left_value} right={right_value} changed={changed}")
    typer.echo(
        "state_fingerprint_changed="
        + str(left_identity.state_fingerprint != right_identity.state_fingerprint).lower()
    )
    typer.echo(
        "runtime_fingerprint_changed="
        + str(left_identity.runtime_fingerprint != right_identity.runtime_fingerprint).lower()
    )
    signal_contract_diff_keys = signal_contract_fingerprint_diff(left_config, right_config)
    typer.echo(
        "signal_contract_fingerprint_changed="
        + str(bool(signal_contract_diff_keys)).lower()
    )
    if signal_contract_diff_keys:
        typer.echo("signal_contract_diff_keys=" + ",".join(signal_contract_diff_keys))


@app.command("profile-validate")
def profile_validate(
    config: Path | None = CONFIG_OPTION,
    target_stage: ProfileStage = typer.Option(..., "--target-stage"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    failures: list[str] = []
    if maestro_config.profile_stage != target_stage:
        failures.append(
            "target_stage_mismatch:"
            f"current={maestro_config.profile_stage.value}:target={target_stage.value}"
        )
    if target_stage in {
        ProfileStage.LIVE_APPROVAL_DISABLED,
        ProfileStage.LIVE_APPROVAL_DRY_RUN,
        ProfileStage.KIS_PAPER_TRADING,
        ProfileStage.PRODUCTION_ARMED,
    }:
        failures.extend(app_fragment_recommendation_failures(maestro_config))
    if target_stage == ProfileStage.PRODUCTION_ARMED:
        store = _state_store(maestro_config, identity)
        report = HealthService(maestro_config, store).run()
        failures.extend(private_beta_failures(maestro_config, report))
    if failures:
        typer.echo(
            f"profile_validate status=fail target_stage={target_stage.value} "
            f"failures={','.join(failures)}"
        )
        raise typer.Exit(1)
    typer.echo(f"profile_validate status=ok target_stage={target_stage.value}")


@app.command("health")
def health(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    report = HealthService(maestro_config, store).run()
    for line in report.text_lines(operator_timezone(maestro_config)):
        typer.echo(line)


@app.command("heartbeat")
def heartbeat(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    run_id = new_run_id()
    payload = {
        "mode": maestro_config.mode.value,
        "source": "cli",
        "config": identity.model_dump(),
        "state_path": str(Path(maestro_config.state.sqlite_path).expanduser().resolve()),
        "audit_path": str(Path(maestro_config.audit.jsonl_path).expanduser().resolve()),
    }
    store.save_system_event(run_id, "maestro_heartbeat", payload)
    audit.log(run_id, "maestro_heartbeat", payload)
    typer.echo(f"heartbeat run_id={run_id} mode={maestro_config.mode.value}")


@app.command("ops-alerts")
def ops_alerts(
    config: Path | None = CONFIG_OPTION,
    allow_mock: bool = typer.Option(False, "--allow-mock"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    report = HealthService(maestro_config, store).run()
    alert_checks = [check for check in report.checks if check.status in {"warn", "fail"}]
    if not alert_checks:
        typer.echo("ops_alerts status=ok message=no_alerts")
        return
    if maestro_config.approval.provider != "telegram":
        raise typer.BadParameter("ops-alerts requires approval.provider=telegram")
    if not maestro_config.approval.telegram_allowed_chat_ids:
        raise typer.BadParameter("ops-alerts requires telegram_allowed_chat_ids")

    message = _ops_alert_message(report, alert_checks)
    if allow_mock:
        typer.echo(
            f"ops_alerts status=ok mock=true alerts={len(alert_checks)} "
            f"chats={len(maestro_config.approval.telegram_allowed_chat_ids)}"
        )
        return

    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        typer.echo("ops_alerts status=fail message=missing_bot_token")
        raise typer.Exit(1)
    client = TelegramBotAPIClient(
        token_env=maestro_config.approval.telegram_bot_token_env,
        timeout_seconds=10.0,
    )
    for chat_id in maestro_config.approval.telegram_allowed_chat_ids:
        client.send_message(chat_id, message)
    typer.echo(
        f"ops_alerts status=ok mock=false alerts={len(alert_checks)} "
        f"chats={len(maestro_config.approval.telegram_allowed_chat_ids)}"
    )


@app.command("live-preflight")
def live_preflight(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode != RunMode.LIVE_APPROVAL:
        raise typer.BadParameter("live-preflight requires mode=live_approval")
    store = _state_store(maestro_config, identity)
    report = HealthService(maestro_config, store).run()
    preflight = next(check for check in report.checks if check.name == "live_approval_preflight")
    detail_text = " ".join(f"{key}={value}" for key, value in preflight.details.items())
    suffix = f" {detail_text}" if detail_text else ""
    typer.echo(
        f"check={preflight.name} status={preflight.status} message={preflight.message}{suffix}"
    )
    if preflight.status == "fail":
        raise typer.Exit(1)


@app.command("telegram-operator")
def telegram_operator(
    config: Path | None = CONFIG_OPTION,
    once: bool = typer.Option(False, "--once"),
    timeout_seconds: int = typer.Option(10, "--timeout-seconds"),
    signal_config: Path | None = typer.Option(
        None,
        "--signal-config",
        envvar="MAESTRO_SIGNAL_CONFIG",
        help="Signal config used for Telegram strategy signal generation commands.",
    ),
    approval_config: Path | None = typer.Option(
        None,
        "--approval-config",
        envvar="MAESTRO_APPROVAL_CONFIG",
        help="Approval config used after funding confirmation regenerates actionable orders.",
    ),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.approval.provider != "telegram":
        raise typer.BadParameter("telegram-operator requires approval.provider=telegram")
    if not maestro_config.approval.telegram_allowed_chat_ids:
        raise typer.BadParameter("telegram-operator requires telegram_allowed_chat_ids")
    if not maestro_config.approval.whitelisted_user_ids:
        raise typer.BadParameter("telegram-operator requires whitelisted_user_ids")
    if _uses_placeholder_telegram_ids(maestro_config):
        raise typer.BadParameter(
            "telegram-operator requires real Telegram chat/user IDs; replace placeholder "
            "123456789 in the operator-local config"
        )
    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        typer.echo("telegram_operator status=fail message=missing_bot_token")
        raise typer.Exit(1)

    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    router = TelegramOperatorCommandRouter(
        config=maestro_config,
        store=store,
        audit=audit,
        client=TelegramBotAPIClient(
            token_env=maestro_config.approval.telegram_bot_token_env,
            timeout_seconds=max(float(timeout_seconds) + 5.0, 10.0),
        ),
        signal_config_path=signal_config,
        approval_config_path=approval_config,
        config_identity=identity,
    )

    offset = None
    while True:
        try:
            offset = router.poll_once(offset=offset, timeout_seconds=timeout_seconds)
            notifier = getattr(router, "notify_pending_cash_flows", None)
            if callable(notifier):
                notifier()
            typer.echo(f"telegram_operator status=ok offset={offset or 'none'}")
            if once:
                return
        except (RuntimeError, TimeoutError, ValueError) as exc:
            typer.echo(f"telegram_operator status=warn message={exc}")
            if once:
                raise typer.Exit(1) from exc
        if maestro_config.approval.telegram_poll_interval_seconds > 0:
            time.sleep(maestro_config.approval.telegram_poll_interval_seconds)


@app.command("telegram-set-commands")
def telegram_set_commands(
    config: Path | None = CONFIG_OPTION,
    signal_config: Path | None = typer.Option(
        None,
        "--signal-config",
        envvar="MAESTRO_SIGNAL_CONFIG",
        help="Signal config used to add strategy signal generation commands.",
    ),
) -> None:
    maestro_config, _identity = _load_operator_config(config)
    if maestro_config.approval.provider != "telegram":
        raise typer.BadParameter("telegram-set-commands requires approval.provider=telegram")
    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        typer.echo("telegram_set_commands status=fail message=missing_bot_token")
        raise typer.Exit(1)
    signal_maestro_config = None
    if signal_config is not None:
        signal_maestro_config, _signal_identity = load_config_with_identity(signal_config)
    commands = telegram_bot_commands(signal_maestro_config)
    TelegramBotAPIClient(
        token_env=maestro_config.approval.telegram_bot_token_env,
        timeout_seconds=10.0,
    ).set_my_commands(commands)
    typer.echo(f"telegram_set_commands status=ok commands={len(commands)}")


@app.command("beta-preflight")
def beta_preflight(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode != RunMode.LIVE_APPROVAL:
        raise typer.BadParameter("beta-preflight requires mode=live_approval")
    store = _state_store(maestro_config, identity)
    report = HealthService(maestro_config, store).run()
    failures = private_beta_failures(maestro_config, report)
    if failures:
        typer.echo("check=private_beta_preflight status=fail failures=" + ",".join(failures))
        raise typer.Exit(1)
    typer.echo("check=private_beta_preflight status=ok message=ready")


@app.command("init-personal")
def init_personal(
    output: Path = typer.Option(..., "--output"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    if output.exists() and not force:
        raise typer.BadParameter("output already exists; pass --force to overwrite")
    raw = _personal_operator_config(output)
    MaestroConfig.model_validate(raw)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    typer.echo(f"created config={output}")
    typer.echo("next=edit Telegram chat/user IDs and set KIS/Telegram environment variables")


@app.command("init-virtuoso-app")
def init_virtuoso_app(
    output: Path = typer.Option(..., "--output"),
    package_name: str = typer.Option(..., "--package-name"),
    class_name: str = typer.Option(..., "--class-name"),
    strategy_id: str | None = typer.Option(None, "--strategy-id"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    try:
        create_virtuoso_app_scaffold(
            output=output,
            package_name=package_name,
            class_name=class_name,
            strategy_id=strategy_id,
            force=force,
        )
    except (FileExistsError, NotADirectoryError, ValueError) as exc:
        raise typer.BadParameter(str(exc)) from exc

    resolved_strategy_id = strategy_id or package_name
    typer.echo(f"created app={output}")
    typer.echo(f'next=uv pip install -e "{output}"')
    typer.echo(
        "next=add strategy config "
        f'entrypoint="{package_name}.strategy:{class_name}" id="{resolved_strategy_id}"'
    )
    typer.echo(f'next=cd "{output}" && pytest -q')


@app.command("personal-check")
def personal_check(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    report = HealthService(maestro_config, store).run()
    checks = {check.name: check for check in report.checks}

    stages = [
        _personal_stage(
            "paper_ready",
            _worst_status(
                checks,
                ["config", "state_db", "audit_path", "audit_integrity", "datahub"],
            ),
            "local config, state, audit, and DataHub checks are usable",
            f"maestro health --config {identity.path}",
        ),
        _personal_stage(
            "readonly_ready",
            _all_ok(checks, ["kis_env", "broker_snapshot", "reconciliation"]),
            "KIS env, broker snapshot, and reconciliation are ready",
            f"maestro live-smoke --config {identity.path} --check kis-readonly",
        ),
        _personal_stage(
            "telegram_ready",
            _telegram_personal_status(maestro_config),
            "Telegram approval config and token are ready",
            f"maestro live-smoke --config {identity.path} --check telegram-approval",
        ),
        _personal_stage(
            "dry_run_ready",
            _dry_run_personal_status(maestro_config, checks),
            "approval-gated dry-run config is ready",
            f"maestro live-smoke --config {identity.path} --check live-dry-run",
        ),
        _personal_stage(
            "minimum_live_ready",
            _minimum_live_personal_status(maestro_config, report),
            "minimum-size approval-gated live order gate is ready",
            f"maestro beta-preflight --config {identity.path}",
        ),
    ]
    typer.echo(f"personal_check status={_overall_personal_status(stages)} config={identity.path}")
    for stage in stages:
        typer.echo(
            f"stage={stage['stage']} status={stage['status']} "
            f'message={stage["message"]} next="{stage["next"]}"'
        )


@app.command("operator-evidence")
def operator_evidence(
    config: Path | None = CONFIG_OPTION,
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    evidence = build_operator_evidence(maestro_config, store, config_path=Path(identity.path))
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    output_text = str(output) if output is not None else "none"
    typer.echo(
        f"operator_evidence status={evidence['overall_status']} "
        f"config={identity.path} output={output_text}"
    )
    for stage in evidence["stages"]:
        typer.echo(
            f"stage={stage['stage']} status={stage['status']} "
            f'message={stage["message"]} next="{stage["next"]}"'
        )
    beta = evidence["private_beta"]
    failures = ",".join(beta["failures"]) if beta["failures"] else "none"
    typer.echo(f"private_beta status={beta['status']} failures={failures}")


@app.command("live-smoke")
def live_smoke(
    config: Path | None = CONFIG_OPTION,
    check: str = typer.Option("kis-readonly", "--check"),
    allow_mock: bool = typer.Option(False, "--allow-mock"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    if check == "kis-readonly":
        _run_kis_readonly_live_smoke(maestro_config, identity, allow_mock)
        return
    if check == "telegram-approval":
        _run_telegram_approval_live_smoke(maestro_config, allow_mock)
        return
    if check == "live-dry-run":
        _run_live_dry_run_smoke(maestro_config, identity, allow_mock)
        return
    raise typer.BadParameter("supported checks: kis-readonly, telegram-approval, live-dry-run")


def _run_kis_readonly_live_smoke(
    maestro_config,
    identity: ConfigIdentity,
    allow_mock: bool,
) -> None:
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter(
            "live-smoke --check kis-readonly requires mode=live_readonly or live_approval"
        )
    if not maestro_config.kis.enabled:
        raise typer.BadParameter("live-smoke --check kis-readonly requires kis.enabled=true")
    if maestro_config.kis.provider != "kis" and not allow_mock:
        raise typer.BadParameter(
            "live-smoke --check kis-readonly requires kis.provider=kis unless --allow-mock is set"
        )

    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    kis_env = next(
        check
        for check in HealthService(maestro_config, store).run().checks
        if check.name == "kis_env"
    )
    if kis_env.status != "ok" and maestro_config.kis.provider == "kis":
        typer.echo(f"check=kis_env status={kis_env.status} message={kis_env.message}")
        raise typer.Exit(1)

    snapshot = KISReadOnlyService(
        maestro_config.kis,
        store,
        audit,
        instruments=maestro_config.universe.instruments,
    ).fetch_and_store_snapshot(maestro_config.portfolio.allowed_symbols)
    _validate_live_smoke_snapshot(snapshot.model_dump(mode="json"), maestro_config)
    _validate_live_smoke_secret_redaction(maestro_config, snapshot.model_dump(mode="json"))

    typer.echo(
        f"check=kis_readonly_snapshot status=ok "
        f"provider={maestro_config.kis.provider} "
        f"account_id={snapshot.account.account_id} cash={snapshot.account.cash:.2f} "
        f"buying_power={snapshot.account.buying_power:.2f} "
        f"positions={len(snapshot.account.positions)} "
        f"prices={len(snapshot.current_prices)} "
        f"fills={len(snapshot.order_fills)} "
        f"unfilled_orders={len(snapshot.unfilled_orders)}"
    )

    result = BrokerReconciliationService(
        maestro_config.reconciliation,
        store,
        audit,
    ).reconcile_latest()
    status = "ok" if result.passed else "fail"
    typer.echo(
        f"check=broker_reconciliation status={status} issues={len(result.issues)} "
        f"cash_difference={result.cash_difference or 0.0:.2f} "
        f"broker_account_id={result.broker_account_id or 'none'}"
    )
    if not result.passed:
        for issue in result.issues:
            symbol = f" symbol={issue.symbol}" if issue.symbol else ""
            typer.echo(f"issue={issue.issue_type}{symbol} message={issue.message}")
        raise typer.Exit(1)


def _run_telegram_approval_live_smoke(maestro_config, allow_mock: bool) -> None:
    if maestro_config.mode not in {RunMode.PAPER, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter(
            "live-smoke --check telegram-approval requires paper or live_approval"
        )
    if not maestro_config.approval.enabled or not maestro_config.approval.require_approval:
        raise typer.BadParameter("live-smoke --check telegram-approval requires approval")
    if maestro_config.approval.provider != "telegram":
        raise typer.BadParameter("live-smoke --check telegram-approval requires provider=telegram")
    if not maestro_config.approval.telegram_allowed_chat_ids:
        raise typer.BadParameter("live-smoke --check telegram-approval requires chat IDs")
    if not maestro_config.approval.whitelisted_user_ids:
        raise typer.BadParameter("live-smoke --check telegram-approval requires whitelisted users")
    if _uses_placeholder_telegram_ids(maestro_config):
        raise typer.BadParameter(
            "live-smoke --check telegram-approval requires real Telegram chat/user IDs"
        )

    if allow_mock:
        typer.echo(
            f"check=telegram_approval status=ok provider=telegram mock=true "
            f"chats={len(maestro_config.approval.telegram_allowed_chat_ids)} "
            f"whitelisted_users={len(maestro_config.approval.whitelisted_user_ids)}"
        )
        return

    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        typer.echo("check=telegram_approval status=fail message=missing_bot_token")
        raise typer.Exit(1)

    client = TelegramBotAPIClient(
        token_env=maestro_config.approval.telegram_bot_token_env,
        timeout_seconds=10.0,
    )
    text = "\n".join(
        [
            "Maestro live smoke check",
            "check: telegram-approval",
            f"mode: {maestro_config.mode.value}",
            "No approval action is required.",
        ]
    )
    for chat_id in maestro_config.approval.telegram_allowed_chat_ids:
        client.send_message(chat_id, text)
    typer.echo(
        f"check=telegram_approval status=ok provider=telegram mock=false "
        f"chats={len(maestro_config.approval.telegram_allowed_chat_ids)} "
        f"whitelisted_users={len(maestro_config.approval.whitelisted_user_ids)}"
    )


def _run_live_dry_run_smoke(
    maestro_config,
    identity: ConfigIdentity,
    allow_mock: bool,
) -> None:
    if maestro_config.mode != RunMode.LIVE_APPROVAL:
        raise typer.BadParameter("live-smoke --check live-dry-run requires mode=live_approval")
    if maestro_config.execution.order_posture != "dry_run":
        raise typer.BadParameter(
            "live-smoke --check live-dry-run requires execution.order_posture=dry_run"
        )
    if not allow_mock:
        if maestro_config.approval.provider != "telegram":
            raise typer.BadParameter("live-smoke --check live-dry-run requires Telegram approval")
        if maestro_config.kis.provider != "kis":
            raise typer.BadParameter("live-smoke --check live-dry-run requires kis.provider=kis")
        store = _state_store(maestro_config, identity)
        preflight = next(
            check
            for check in HealthService(maestro_config, store).run().checks
            if check.name == "live_approval_preflight"
        )
        if preflight.status == "fail":
            detail_text = " ".join(f"{key}={value}" for key, value in preflight.details.items())
            suffix = f" {detail_text}" if detail_text else ""
            typer.echo(
                f"check=live_approval_preflight status=fail message={preflight.message}{suffix}"
            )
            raise typer.Exit(1)

    orchestrator = MaestroOrchestrator(maestro_config, config_identity=identity)
    summary = orchestrator.run_once()
    dry_run_events = [
        row
        for row in orchestrator.state_store.list_system_events_by_type(
            "live_order_dry_run",
            limit=100,
        )
        if row["run_id"] == summary.run_id
    ]
    if not dry_run_events:
        typer.echo(
            f"check=live_dry_run status=fail run_id={summary.run_id} "
            "message=no_live_order_dry_run_event"
        )
        raise typer.Exit(1)
    for row in dry_run_events:
        payload = row["payload"]
        request = payload["request"]
        if payload.get("broker_submit_skipped") is not True:
            typer.echo(
                f"check=live_dry_run status=fail run_id={summary.run_id} "
                f"order_id={request.get('order_id')} message=broker_submit_not_skipped"
            )
            raise typer.Exit(1)
        typer.echo(
            f"check=live_dry_run status=ok run_id={summary.run_id} "
            f"order_id={request['order_id']} symbol={request['symbol']} "
            f"side={request['side']} quantity={request['quantity']} "
            f"limit_price={request['limit_price']} notional={payload['notional']:.2f}"
        )


@app.command("approvals")
def approvals(
    config: Path | None = CONFIG_OPTION,
    limit: int = typer.Option(10, "--limit"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    for row in store.list_approvals(limit=limit):
        decision = row["payload"]["decision"]
        typer.echo(
            f"{format_operator_time(row['created_at'], operator_timezone(maestro_config))} "
            f"approval_id={row['approval_id']} "
            f"run_id={row['run_id']} status={decision['status']}"
        )


@app.command("safety-status")
def safety_status(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    current = SafetyControlService(store, audit).current_state()
    typer.echo(
        f"state={current.state.value} source={current.source} "
        f"reason={current.reason} "
        f"created_at={format_operator_time(current.created_at, operator_timezone(maestro_config))} "
        f"updated_at={format_operator_time(current.updated_at, operator_timezone(maestro_config))}"
    )


@app.command("pause")
def pause(
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
) -> None:
    current = _safety_service(config).pause(new_run_id(), reason)
    typer.echo(f"state={current.state.value} reason={current.reason}")


@app.command("resume")
def resume(
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
) -> None:
    current = _safety_service(config).resume(new_run_id(), reason)
    typer.echo(f"state={current.state.value} reason={current.reason}")


@app.command("kill-switch")
def kill_switch(
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
) -> None:
    current = _safety_service(config).kill_switch(new_run_id(), reason)
    typer.echo(f"state={current.state.value} reason={current.reason}")


@app.command("clear-halt")
def clear_halt(
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    report = HealthService(maestro_config, store).run()
    blocking_checks = [
        check for check in report.checks if check.status == "fail" and check.name != "safety_state"
    ]
    if blocking_checks:
        names = ",".join(check.name for check in blocking_checks)
        typer.echo(f"recovery_preflight_failed checks={names}")
        raise typer.Exit(1)
    current = SafetyControlService(store, audit).clear_halt(new_run_id(), reason)
    typer.echo(f"state={current.state.value} reason={current.reason}")


def _echo_upgrade_result(result: UpgradeResult) -> None:
    """One key=value line per category. Partial state is never hidden."""
    typer.echo(f"upgrade_backfill state={result.state.phase.value} cutoff={result.state.cutoff}")
    backfill = result.backfill
    if backfill is not None:
        typer.echo(
            "upgrade_backfill "
            f"legacy_rows_inspected={backfill.legacy_requests_inspected} "
            f"heads_created={backfill.heads_created} "
            f"heads_already_coherent={backfill.heads_already_coherent} "
            f"terminal_skipped={backfill.terminal_skipped}"
        )
    if result.approvals is not None:
        typer.echo(
            "upgrade_backfill "
            f"approval_acks_inspected={result.approvals.acks_inspected} "
            f"approvals_proven_complete={result.approvals.proven_complete}"
        )
    if result.dispatches is not None:
        typer.echo(
            "upgrade_backfill "
            f"dispatches_inspected={result.dispatches.dispatches_inspected} "
            f"dispatches_resumable={result.dispatches.resumable}"
        )
    for quarantine in result.quarantines:
        typer.echo(
            f"upgrade_backfill quarantine subsystem={quarantine.subsystem} "
            f"identifier={quarantine.identifier} reason={quarantine.reason} "
            f"blocking={quarantine.blocking} detail={quarantine.detail}"
        )
    for evidence in result.reupgrade_evidence:
        typer.echo(
            f"upgrade_backfill reupgrade_evidence detector={evidence['detector']} "
            f"event_id={evidence['event_id']} identifier={evidence['identifier']} "
            f"event_type={evidence['event_type']}"
        )
    if result.aborted_reason is not None:
        typer.echo(f"upgrade_backfill status=aborted reason={result.aborted_reason}")
        if result.aborted_reason == "blocking_quarantine":
            typer.echo(
                "upgrade_backfill next=resolve the blocking quarantines above and rerun. "
                "Do NOT restart services: funding ownership is unresolved."
            )
        if result.aborted_reason == "reupgrade_after_rollback":
            typer.echo(
                "upgrade_backfill next=this database was written by an older binary after "
                "the migration completed. See docs/rollback_and_upgrade_3a.md; do not force it."
            )
        return
    typer.echo("upgrade_backfill status=completed")


def _echo_quiesce_failure(command: str, report: quiesce.QuiesceReport) -> None:
    for unit in report.active_units:
        typer.echo(f"{command} status=fail reason=writer_active unit={unit}")
    for unit in report.queued_jobs:
        typer.echo(f"{command} status=fail reason=queued_job unit={unit}")
    for unit in report.autostart_units:
        typer.echo(
            f"{command} status=fail reason=reboot_autostart unit={unit} "
            "(disable or mask it: a reboot during the migration would start it again)"
        )


@app.command("quiesce-status")
def quiesce_status() -> None:
    """장벽이 서 있는지, 그리고 나중에 무엇을 원래대로 되돌려야 하는지 보여준다.

    아무것도 정지시키거나 활성화하지 않는다. `enable --now`로 일괄 복구하면
    운영자가 일부러 꺼 둔 writer까지 켜지므로, 복구는 여기 찍힌 원래 상태를
    보고 사람이 한다.
    """
    report = quiesce.verify_quiesced()
    typer.echo(f"quiesce quiesced={report.quiesced}")
    _echo_quiesce_failure("quiesce", report)
    for state in quiesce.capture_unit_states():
        typer.echo(f"quiesce unit={state.unit} active={state.active} enabled={state.enabled}")


@app.command("upgrade-backfill")
def upgrade_backfill(
    config: Path | None = CONFIG_OPTION,
    require_quiesce: bool = typer.Option(
        True,
        "--require-quiesce/--no-require-quiesce",
        help="Refuse to run unless every writer unit and activator is stopped.",
    ),
) -> None:
    """3a 업그레이드 backfill. quiesce 장벽 아래에서만 실행한다.

    브로커 주문 제출, 승인 재집행, 시그널 생성·재생, 과거 dispatch 의도의
    재계산, 현금흐름 기록 -- 어느 것도 하지 않는다. 쓰는 것은 마이그레이션
    소유권 마커, 결정적 v1 head, 격리 레코드뿐이며 `migration_completed`가
    마지막 쓰기다.

    막는 격리(funding 소유권 모호)가 남으면 완료하지 않고 MIGRATING 상태로
    끝낸다. 그러면 런타임 게이트가 계속 닫혀 있으므로, 소유권이 정리되기
    전에는 서비스를 재개하지 않는다.
    """
    if require_quiesce:
        report = quiesce.verify_quiesced()
        if not report.quiesced:
            _echo_quiesce_failure("upgrade_backfill", report)
            raise typer.Exit(1)
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    result = run_upgrade_backfill(store, new_run_id())
    _echo_upgrade_result(result)
    if result.aborted_reason is not None:
        raise typer.Exit(1)


@app.command("rollback-preflight")
def rollback_preflight(
    config: Path | None = CONFIG_OPTION,
    require_quiesce: bool = typer.Option(
        True,
        "--require-quiesce/--no-require-quiesce",
        help="Fail if any writer unit or activator is still running.",
    ),
) -> None:
    """구버전 배포 전 안전 검사. 하나라도 어긋나면 exit 1.

    R0 마이그레이션이 진행 중이거나 마커가 모순
    R1 claim은 있으나 completed가 없는 funding/budget 전이 -- 구 handler는
       claim을 읽지 않으므로 요청을 pending으로 보고 run_signal()을 다시 돌린다
    R2 consumed이지만 settled가 없는 signal package -- 구 코드는 consumed를
       영구로 취급해 승인 카드가 유실된다
    R3 schema_version ack은 있으나 resolution_completed가 없는 승인 -- 구
       handler는 ack만으로 종결로 보아 승인된 주문이 나가지 않는다
    R4 funding_workflow_completed에 대응하는 legacy 종결 이벤트가 없음

    **이 명령은 어떤 호환성 상태도 복구하지 않는다.** R4가 걸리면 그 자리에서
    실패한다 -- complete_workflow가 둘을 한 트랜잭션으로 쓰므로 없다는 것은
    손상·수동 변경·중간 빌드 중 하나라는 뜻이고, 여기서 지어내면 무엇이었는지
    알 수 없게 되며 이 코드가 알 수 없는 종결 상태를 단언하게 된다.

    판정 로직은 읽기 전용이다. 다만 `_state_store` 생성은 다른 모든 CLI 명령과
    동일하게 보류 중인 스키마 마이그레이션(`StateStore._init_db`)을 적용할 수
    있다 -- 모두 additive-only(`CREATE TABLE/INDEX IF NOT EXISTS`, `ALTER TABLE
    ADD COLUMN`)이며 롤백 대상인 구버전 코드도 기동 시 동일한 마이그레이션을
    실행하므로 롤백 안전성에는 영향이 없다.
    """
    if require_quiesce:
        report = quiesce.verify_quiesced()
        if not report.quiesced:
            _echo_quiesce_failure("rollback_preflight", report)
            raise typer.Exit(1)
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    result = run_rollback_preflight(store)
    if result.safe:
        typer.echo("rollback_preflight status=safe failures=0")
        return
    for failure in result.failures:
        event_ids = ",".join(str(item) for item in failure.event_ids)
        typer.echo(
            f"rollback_preflight status=unsafe invariant={failure.invariant} "
            f"identifier={failure.identifier} event_ids={event_ids} "
            f"detail={failure.detail}"
        )
    typer.echo(f"rollback_preflight status=unsafe failures={len(result.failures)}")
    raise typer.Exit(1)


#: The name the runbook and the operator's shell history already know. Same
#: command: the checks were never approval-specific, and renaming without an
#: alias is how a rollback gets attempted with no preflight at all.
app.command("approval-rollback-preflight")(rollback_preflight)


@app.command("approval-outcome")
def approval_outcome(
    config: Path | None = CONFIG_OPTION,
    approval_id: str = typer.Option(..., "--approval-id"),
) -> None:
    """한 승인의 주문들이 실제로 어디까지 갔는지 증거로 분류해 보여준다.

    아무것도 쓰지 않는다. `approval-settle`로 무엇을 닫는지 먼저 보는
    용도이며, 단계 4b의 카드가 렌더할 데이터와 같다.
    """
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    evidence = store.load_approval_execution_evidence(approval_id)
    if evidence["envelope"] is None:
        typer.echo(f"approval_outcome status=not_found approval_id={approval_id}")
        raise typer.Exit(1)
    outcome = summarize_batch(approval_id, build_order_evidence(evidence))
    audit.log(
        str(evidence["envelope"].get("run_id") or f"run_{approval_id}"),
        "approval_outcome_inspected",
        {"approval_id": approval_id, "counts": outcome.counts},
    )
    settled = evidence["resolution_completed"] is not None
    typer.echo(
        f"approval_outcome approval_id={approval_id} orders={len(outcome.orders)} "
        f"has_unknown={outcome.has_unknown} settled={settled}"
    )
    for line in outcome.orders:
        typer.echo(
            f"  {line.symbol} {line.side} {line.ordered_quantity:g} "
            f"filled={line.filled_quantity:g} -> {line.outcome}"
        )
    for name, count in sorted(outcome.counts.items()):
        typer.echo(f"  count {name}={count}")
    if outcome.has_unknown:
        typer.echo(
            "  주의: 브로커에 닿았는지 알 수 없는 주문이 있다. "
            "증권사 앱에서 먼저 확인할 것."
        )


@app.command("approval-settle")
def approval_settle(
    config: Path | None = CONFIG_OPTION,
    approval_id: str = typer.Option(..., "--approval-id"),
    reason: str = typer.Option(..., "--reason"),
    confirm: str = typer.Option("", "--confirm"),
    reconciled_with_broker: bool = typer.Option(
        False,
        "--i-have-reconciled-with-broker",
        help=(
            "Settle even though an order's fate is unknown. Only after "
            "checking the broker yourself; the override is recorded."
        ),
    ),
) -> None:
    """반쯤 집행된 승인을 사실대로 종결한다.

    주문을 내지 않는다. 미체결·미발주분의 재수행은 운영자가 `/rebalancing`
    으로 하거나 단계 4b가 맡는다.
    """
    if confirm != "SETTLE":
        raise typer.BadParameter("approval-settle requires --confirm SETTLE")
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    try:
        outcome = settle_approval(
            store,
            audit,
            approval_id,
            reason=reason,
            reconciled_with_broker=reconciled_with_broker,
        )
    except SettlementRefused as exc:
        typer.echo(f"approval_settle status=refused reason={exc}")
        raise typer.Exit(1) from exc
    except TimeoutError as exc:
        # 정산은 live_order_lock을 쥐고 증거를 읽는다. 그 락을 다른 쪽이 쥐고
        # 있다는 것은 이 승인이 지금 집행되고 있을 수 있다는 뜻이므로, 지금은
        # 종결할 대상 자체가 확정되지 않았다. 다시 시도하라고 말한다.
        typer.echo(f"approval_settle status=busy reason={exc}")
        raise typer.Exit(1) from exc
    counts = " ".join(f"{name}={count}" for name, count in sorted(outcome.counts.items()))
    typer.echo(f"approval_settle status=settled approval_id={approval_id} {counts}")


@app.command("release-kill")
def release_kill(
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
    confirm: str = typer.Option(..., "--confirm"),
) -> None:
    if confirm != "RELEASE-KILL":
        raise typer.BadParameter("release-kill requires --confirm RELEASE-KILL")
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    report = HealthService(maestro_config, store).run()
    blocking_checks = [
        check for check in report.checks if check.status == "fail" and check.name != "safety_state"
    ]
    if blocking_checks:
        names = ",".join(check.name for check in blocking_checks)
        typer.echo(f"recovery_preflight_failed checks={names}")
        raise typer.Exit(1)
    current = SafetyControlService(store, audit).release_kill(new_run_id(), reason)
    typer.echo(f"state={current.state.value} reason={current.reason}")


@app.command("kis-sync")
def kis_sync(
    config: Path | None = CONFIG_OPTION,
    account_ids: str | None = typer.Option(None, "--account-ids"),
    max_age_seconds: int | None = typer.Option(None, "--max-age-seconds", min=0),
    source: str = typer.Option("cli", "--source"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("kis-sync requires mode=live_readonly or live_approval")
    selected = None
    if account_ids is not None:
        selected = [value.strip() for value in account_ids.split(",") if value.strip()]
        if not selected:
            raise typer.BadParameter("--account-ids requires at least one account id")
    try:
        report = refresh_readonly_accounts(
            maestro_config,
            identity,
            account_ids=selected,
            source=source,
            max_snapshot_age_seconds=max_age_seconds,
        )
    except ValueError as exc:
        raise typer.BadParameter(f"kis-sync failed: {exc}") from exc
    store = _state_store(maestro_config, identity)
    for result in report.results:
        row = latest_snapshot_for_account(store, result.account_id)
        payload = (row or {}).get("payload") or {}
        broker_account_id = payload.get("broker_account_id") or (
            payload.get("account") or {}
        ).get("account_id")
        typer.echo(
            f"account_id={result.account_id} broker_account_id={broker_account_id or 'none'} "
            f"status={result.status} "
            f"snapshot_id={result.snapshot_id or 'none'} "
            f"age_seconds={result.age_seconds if result.age_seconds is not None else 'none'} "
            f"retries={result.retry_count} "
            f"reconciliation={result.reconciliation_passed}"
        )
    synced_count = sum(result.snapshot_id is not None for result in report.results)
    typer.echo(
        f"accounts_selected={len(report.results)} "
        f"accounts_synced={synced_count} "
        f"accounts_failed={len(report.failed_account_ids)}"
    )
    missing_snapshots = [
        result.account_id for result in report.results if result.snapshot_id is None
    ]
    if missing_snapshots:
        errors = "; ".join(
            result.error_message or result.error_type or "unknown error"
            for result in report.results
            if result.snapshot_id is None
        )
        raise typer.BadParameter(
            "kis-sync failed before storing snapshot for account(s): "
            + ", ".join(missing_snapshots)
            + f": {errors}"
        )


@app.command("fx-refresh")
def fx_refresh(
    config: Path | None = CONFIG_OPTION,
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass FX refresh throttling and call the provider.",
    ),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    try:
        result = ConfiguredFXRefreshService(maestro_config, store).refresh_from_config(force=force)
    except Exception as exc:
        raise typer.BadParameter(f"fx-refresh failed: {exc}") from exc
    if result.status == "skipped":
        typer.echo("fx_refresh=skipped")
        return
    rate = result.rates.get("USD/KRW")
    typer.echo(
        f"fx_refresh={result.status} source={result.source} USD/KRW={rate} as_of={result.as_of}"
    )


@app.command("kis-account")
def kis_account(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    latest = store.load_latest_broker_account_snapshot()
    if latest is None:
        typer.echo("No broker account snapshot found.")
        raise typer.Exit(1)
    account = latest["payload"]["account"]
    created_at = format_operator_time(latest["created_at"], operator_timezone(maestro_config))
    typer.echo(
        f"created_at={created_at} account_id={account['account_id']} "
        f"cash={account['cash']:.2f} buying_power={account['buying_power']:.2f} "
        f"positions={len(account['positions'])}"
    )


@app.command("reconcile")
def reconcile(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("reconcile requires mode=live_readonly or live_approval")
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    try:
        result = BrokerReconciliationService(
            maestro_config.reconciliation,
            store,
            audit,
            snapshot_refresher=_broker_snapshot_refresher(maestro_config, store, audit),
            account_ids=_reconciliation_account_ids(maestro_config),
        ).reconcile_latest()
    except ValueError as exc:
        raise typer.BadParameter(f"reconcile broker snapshot refresh failed: {exc}") from exc
    status = "passed" if result.passed else "failed"
    typer.echo(
        f"status={status} issues={len(result.issues)} "
        f"observations={len(result.observations)} "
        f"cash_difference={result.cash_difference or 0.0:.2f} "
        f"broker_account_id={result.broker_account_id or 'none'}"
    )
    if not result.passed:
        for issue in result.issues:
            symbol = f" symbol={issue.symbol}" if issue.symbol else ""
            typer.echo(f"issue={issue.issue_type}{symbol} message={issue.message}")
        for issue in result.observations:
            symbol = f" symbol={issue.symbol}" if issue.symbol else ""
            typer.echo(f"observation={issue.issue_type}{symbol} message={issue.message}")
        raise typer.Exit(1)


@performance_baseline_app.command("adopt")
def adopt_performance_baseline(
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
    max_age_seconds: int = typer.Option(1200, "--max-age-seconds", min=1),
) -> None:
    from maestro.dashboard.read_models import broker_snapshot_value_components

    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter(
            "performance-baseline adopt requires live_readonly or live_approval"
        )
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    report = refresh_readonly_accounts(
        maestro_config,
        identity,
        source="performance_baseline_adopt",
        max_snapshot_age_seconds=0,
        state_store=store,
        audit_logger=audit,
    )
    failures = [
        (
            f"{result.account_id}:status={result.status}:"
            f"age={result.age_seconds}:reconciled={result.reconciliation_passed}"
        )
        for result in report.results
        if result.status != "refreshed"
        or result.reconciliation_passed is not True
        or result.age_seconds is None
        or result.age_seconds > max_age_seconds
    ]
    if failures:
        raise typer.BadParameter(
            "performance baseline requires fresh reconciled snapshots: "
            + ", ".join(failures)
        )
    accounts: dict[str, dict[str, object]] = {}
    component_values: dict[str, float] = {}
    for result in report.results:
        snapshot = latest_snapshot_for_account(store, result.account_id)
        if snapshot is None:
            raise typer.BadParameter(
                f"performance baseline snapshot missing: account_id={result.account_id}"
            )
        components = broker_snapshot_value_components(
            snapshot,
            default_currency=maestro_config.portfolio.base_currency,
        )
        accounts[result.account_id] = {
            "snapshot_id": snapshot["id"],
            "components": components,
        }
        for currency, value in components.items():
            component_values[currency] = component_values.get(currency, 0.0) + value
    run_id = new_run_id()
    effective_at = max(
        str(latest_snapshot_for_account(store, result.account_id)["created_at"])
        for result in report.results
    )
    payload = {
        "baseline_id": run_id,
        "effective_at": effective_at,
        "accounts": accounts,
        "component_values": component_values,
        "base_currency": maestro_config.portfolio.base_currency,
        "reason": reason,
        "source_refresh_run_id": report.run_id,
    }
    save_audited_system_event(
        store,
        audit,
        run_id,
        SystemEventType.PERFORMANCE_BASELINE_ADOPTED,
        payload,
    )
    typer.echo(
        f"adopted baseline_id={run_id} effective_at={effective_at} "
        f"accounts={len(accounts)}"
    )


@cash_flow_app.command("record")
def record_account_cash_flow(
    account_id: str = typer.Option(..., "--account-id"),
    amount: float = typer.Option(..., "--amount", min=0.000001),
    currency: str = typer.Option(..., "--currency"),
    flow_type: str = typer.Option(..., "--flow-type"),
    reason: str = typer.Option(..., "--reason"),
    flow_class: str = typer.Option(
        EXTERNAL_TRANSFER,
        "--flow-class",
        help=(
            "What the money was: external_transfer, investment_income or cost. "
            "Use cash-flow transfer/convert for linked movements."
        ),
    ),
    effective_at: str | None = typer.Option(None, "--effective-at"),
    transfer_id: str | None = typer.Option(None, "--transfer-id"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    maestro_config, identity = _load_operator_config(config)
    known_accounts = set(broker_readonly_account_ids(maestro_config))
    if account_id not in known_accounts:
        raise typer.BadParameter(f"unknown account_id={account_id}")
    normalized_type = flow_type.strip().lower()
    if normalized_type not in {"deposit", "withdrawal"}:
        raise typer.BadParameter("--flow-type must be deposit or withdrawal")
    normalized_class = flow_class.strip().lower()
    if normalized_class == FX_CONVERSION:
        # A conversion is two legs whose amounts have to agree; recording one
        # side here would leave a permanently unpaired flow.
        raise typer.BadParameter("use `maestro cash-flow convert` to record a conversion")
    if normalized_class == INTERNAL_TRANSFER:
        raise typer.BadParameter(
            "use `maestro cash-flow transfer` to record both sides atomically"
        )
    normalized_currency = currency.strip().upper()
    timestamp = effective_at or utc_now().isoformat()
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    signed_amount = abs(amount) if normalized_type == "deposit" else -abs(amount)
    try:
        result = AccountCashFlowService(store, audit).record(
            account_id=account_id,
            amount=amount,
            currency=normalized_currency,
            flow_type=normalized_type,
            effective_at=timestamp,
            source="operator_cli",
            reason=reason,
            transfer_id=transfer_id,
            flow_class=normalized_class,
            decided_by="operator_cli",
            verification="operator_verified",
            duplicate_key=(
                account_cash_flow_leg_duplicate_key(
                    transfer_id,
                    account_id,
                    normalized_currency,
                    normalized_type,
                )
                if transfer_id
                else None
            ),
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        f"recorded cash_flow_id={result.run_id} account_id={account_id} "
        f"amount={signed_amount:.6f} currency={normalized_currency}"
    )


@cash_flow_app.command("convert")
def record_currency_conversion(
    account_id: str = typer.Option(..., "--account-id"),
    from_currency: str = typer.Option(..., "--from-currency"),
    from_amount: float = typer.Option(..., "--from-amount", min=0.000001),
    to_currency: str = typer.Option(..., "--to-currency"),
    to_amount: float = typer.Option(
        ...,
        "--to-amount",
        min=0.000001,
        help="Net target-currency amount that arrived after the stated fee.",
    ),
    transfer_id: str = typer.Option(
        ...,
        "--transfer-id",
        help="Names this conversion so re-running the command cannot apply it twice.",
    ),
    reason: str = typer.Option(..., "--reason"),
    fee: float = typer.Option(
        0.0,
        "--fee",
        min=0.0,
        help="Spread or commission, in the target currency, booked as a cost.",
    ),
    rate: float | None = typer.Option(
        None,
        "--rate",
        help="Target-currency units per source-currency unit; checked against the amounts.",
    ),
    effective_at: str | None = typer.Option(None, "--effective-at"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Record a currency conversion as linked legs plus its cost."""
    maestro_config, identity = _load_operator_config(config)
    known_accounts = set(broker_readonly_account_ids(maestro_config))
    if account_id not in known_accounts:
        raise typer.BadParameter(f"unknown account_id={account_id}")
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    try:
        result = AccountCashFlowService(store, audit).record_currency_conversion(
            account_id=account_id,
            from_currency=from_currency,
            from_amount=from_amount,
            to_currency=to_currency,
            to_amount=to_amount,
            fee=fee,
            rate=rate,
            transfer_id=transfer_id,
            effective_at=effective_at or utc_now().isoformat(),
            source="operator_cli",
            reason=reason,
            decided_by="operator_cli",
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        f"converted cash_flow_id={result.run_id} created={result.created} "
        f"account_id={account_id} "
        f"{from_amount:.6f} {from_currency.strip().upper()} -> "
        f"{to_amount:.6f} {to_currency.strip().upper()} fee={fee:.6f}"
    )


@cash_flow_app.command("transfer")
def record_internal_transfer(
    from_account_id: str = typer.Option(..., "--from-account-id"),
    from_currency: str = typer.Option(..., "--from-currency"),
    from_amount: float = typer.Option(..., "--from-amount", min=0.000001),
    to_account_id: str = typer.Option(..., "--to-account-id"),
    to_currency: str = typer.Option(..., "--to-currency"),
    to_amount: float = typer.Option(..., "--to-amount", min=0.000001),
    transfer_id: str = typer.Option(
        ...,
        "--transfer-id",
        help="Names both transfer legs so retrying the command is a no-op.",
    ),
    reason: str = typer.Option(..., "--reason"),
    effective_at: str | None = typer.Option(None, "--effective-at"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Record both sides of an account-to-account transfer atomically."""
    maestro_config, identity = _load_operator_config(config)
    known_accounts = set(broker_readonly_account_ids(maestro_config))
    unknown_accounts = sorted({from_account_id, to_account_id} - known_accounts)
    if unknown_accounts:
        raise typer.BadParameter(f"unknown account_id={','.join(unknown_accounts)}")
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    try:
        result = AccountCashFlowService(store, audit).record_internal_transfer(
            from_account_id=from_account_id,
            from_currency=from_currency,
            from_amount=from_amount,
            to_account_id=to_account_id,
            to_currency=to_currency,
            to_amount=to_amount,
            transfer_id=transfer_id,
            effective_at=effective_at or utc_now().isoformat(),
            source="operator_cli",
            reason=reason,
            decided_by="operator_cli",
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        f"transferred cash_flow_id={result.run_id} created={result.created} "
        f"{from_account_id}:{from_amount:.6f} {from_currency.strip().upper()} -> "
        f"{to_account_id}:{to_amount:.6f} {to_currency.strip().upper()}"
    )


@ledger_app.command("open-baseline")
def open_ledger_baseline(
    account_id: str = typer.Option(..., "--account-id"),
    reason: str = typer.Option(..., "--reason"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Create the one-time ledger opening baseline for an account."""
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("ledger open-baseline requires live_readonly or live_approval")
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    existing = store.list_system_events_by_type(
        str(SystemEventType.LEDGER_OPENING_BASELINE), limit=1000
    )
    if any(str(row["payload"].get("account_id")) == account_id for row in existing):
        raise typer.BadParameter(
            "ledger opening baseline already exists for "
            f"account_id={account_id}"
        )
    snapshot = latest_snapshot_for_account(store, account_id)
    if snapshot is None:
        raise typer.BadParameter(f"latest broker snapshot missing for account_id={account_id}")
    account = snapshot["payload"].get("account") or {}
    state = portfolio_state_from_broker_account(
        account,
        allowed_symbols=maestro_config.portfolio.allowed_symbols,
        universe=maestro_config.universe,
    )
    run_id = new_run_id()
    store.save_portfolio_snapshot(run_id, state, account_id=account_id)
    ledger_cash = state.cash_by_currency or {maestro_config.portfolio.base_currency: state.cash}
    effective_at = str(snapshot.get("created_at") or utc_now().isoformat())
    for currency, amount in sorted(ledger_cash.items()):
        save_audited_system_event(
            store,
            audit,
            run_id,
            SystemEventType.LEDGER_OPENING_BASELINE,
            {
                "account_id": account_id,
                "currency": currency,
                "amount": float(amount),
                "source": "buying_power_proxy",
                "provenance": "operator_confirmed_opening_baseline",
                "snapshot_id": snapshot["id"],
                "effective_at": effective_at,
                "reason": reason,
            },
        )
    typer.echo(
        f"opened ledger_baseline run_id={run_id} account_id={account_id} "
        f"snapshot_id={snapshot['id']} currencies={len(ledger_cash)} source=buying_power_proxy"
    )


@ledger_app.command("backfill-orders")
def ledger_backfill_orders(
    account_id: str = typer.Option(..., "--account-id"),
    from_date: str = typer.Option(..., "--from-date", help="YYYY-MM-DD"),
    to_date: str | None = typer.Option(None, "--to-date", help="YYYY-MM-DD"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Backfill Toss OPEN/CLOSED order history into the cash ledger."""
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    try:
        start = date.fromisoformat(from_date)
        end = date.fromisoformat(to_date) if to_date else date.today()
    except ValueError as exc:
        raise typer.BadParameter("order history dates must use YYYY-MM-DD") from exc
    services = dict(build_broker_readonly_services(maestro_config, store, audit))
    service = services.get(account_id)
    while service is not None and hasattr(service, "inner"):
        service = service.inner
    if service is None or not hasattr(service, "client"):
        raise typer.BadParameter(f"account_id={account_id} is not a configured readonly account")
    client = service.client
    if not hasattr(client, "list_orders"):
        raise typer.BadParameter("ledger order backfill is supported for Toss accounts only")
    payload = TossOrderHistoryBackfillService(client, store, audit).backfill(
        account_id,
        from_date=start,
        to_date=end,
    )
    typer.echo(json.dumps(payload, default=str))


@ledger_app.command("bookkeeping-correction")
def ledger_bookkeeping_correction(
    account_id: str = typer.Option(..., "--account-id"),
    currency: str = typer.Option(..., "--currency"),
    amount: float = typer.Option(..., "--amount"),
    correction_id: str = typer.Option(..., "--correction-id"),
    evidence: str = typer.Option(..., "--evidence"),
    reason: str = typer.Option(..., "--reason"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Apply an idempotent non-flow correction with explicit evidence."""
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter(
            "ledger bookkeeping-correction requires live_readonly or live_approval"
        )
    normalized_currency = currency.strip().upper()
    normalized_correction_id = correction_id.strip()
    if not normalized_currency or not normalized_correction_id:
        raise typer.BadParameter("currency and correction-id must be non-empty")
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    run_id = new_run_id()
    payload = {
        "account_id": account_id,
        "currency": normalized_currency,
        "amount": float(amount),
        "reason": reason,
        "evidence": evidence,
        "source": "operator_cli",
        "decided_by": "operator_cli",
        "effective_at": utc_now().isoformat(),
        "duplicate_key": f"ledger-bookkeeping-correction:{normalized_correction_id}",
    }
    try:
        created = store.apply_ledger_bookkeeping_correction(
            run_id,
            account_id=account_id,
            currency=normalized_currency,
            amount=amount,
            event_payload=payload,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if created:
        audit.log(run_id, str(SystemEventType.LEDGER_BOOKKEEPING_CORRECTION), payload)
    typer.echo(
        f"bookkeeping_correction run_id={run_id} created={str(created).lower()} "
        f"account_id={account_id} amount={amount:.12f} currency={normalized_currency}"
    )


@cash_drift_app.command("report")
def cash_drift_report(
    account_id: str = typer.Option(..., "--account-id"),
    days: int = typer.Option(7, "--days", min=1),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Report buying-power versus ledger drift without changing state."""
    from datetime import timedelta

    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    now = utc_now()
    since = (now - timedelta(days=days)).isoformat(sep=" ")
    rows = [
        row
        for row in store.list_broker_account_snapshots(limit=None, since=since)
        if str(row.get("account_id") or row["payload"].get("account_id") or "")
        == account_id
    ]
    rows.reverse()
    if not rows:
        raise typer.BadParameter(
            f"no broker snapshots for account_id={account_id} in last {days} days"
        )
    ledger_rows = sorted(
        [
            row
            for row in store.list_portfolio_snapshots(limit=None)
            if str(row.get("account_id") or "") == account_id
        ],
        key=lambda row: (str(row.get("created_at") or ""), int(row.get("id") or 0)),
    )
    print_rows = []
    for row in rows:
        account = row["payload"].get("account") or {}
        buying_power = account.get("buying_power_by_currency") or {}
        if not buying_power:
            buying_power = account.get("cash_by_currency") or {}
        broker_created_at = str(row.get("created_at") or "")
        ledger_payload = None
        for ledger_row in ledger_rows:
            if str(ledger_row.get("created_at") or "") > broker_created_at:
                break
            ledger_payload = ledger_row.get("payload") or {}
        ledger = {}
        if ledger_payload is not None:
            ledger = dict(ledger_payload.get("cash_by_currency") or {})
            if not ledger:
                ledger = {
                    maestro_config.portfolio.base_currency: float(
                        ledger_payload.get("cash") or 0.0
                    )
                }
        drift = (
            {
                currency: float(buying_power.get(currency, 0.0))
                - float(ledger.get(currency, 0.0))
                for currency in sorted(set(ledger) | set(buying_power))
            }
            if ledger_payload is not None
            else {}
        )
        fill_times = [
            str(item.get("submitted_at") or "")
            for item in row["payload"].get("order_fills") or []
            if item.get("submitted_at")
        ]
        last_fill_at = max(fill_times) if fill_times else None
        settlement_elapsed_days = None
        if last_fill_at:
            try:
                fill_timestamp = datetime.fromisoformat(last_fill_at.replace("Z", "+00:00"))
                if fill_timestamp.tzinfo is None:
                    fill_timestamp = fill_timestamp.replace(tzinfo=UTC)
                snapshot_timestamp = datetime.fromisoformat(
                    broker_created_at.replace("Z", "+00:00")
                )
                if snapshot_timestamp.tzinfo is None:
                    snapshot_timestamp = snapshot_timestamp.replace(tzinfo=UTC)
                settlement_elapsed_days = max(
                    (snapshot_timestamp - fill_timestamp).total_seconds() / 86400.0,
                    0.0,
                )
            except ValueError:
                settlement_elapsed_days = None
        print_rows.append(
            {
                "snapshot_id": row["id"],
                "created_at": row.get("created_at"),
                "ledger_available": ledger_payload is not None,
                "ledger_cash_by_currency": ledger,
                "buying_power_by_currency": buying_power,
                "drift_by_currency": drift,
                "fills": len(row["payload"].get("order_fills") or []),
                "last_fill_at": last_fill_at,
                "settlement_elapsed_days": settlement_elapsed_days,
                "unfilled_orders": len(row["payload"].get("unfilled_orders") or []),
            }
        )
    typer.echo(
        json.dumps(
            {
                "account_id": account_id,
                "days": days,
                "rows": print_rows,
                "suspense": store.list_cash_suspense(account_id=account_id),
            },
            default=str,
        )
    )


@cash_drift_app.command("classify")
def cash_drift_classify(
    account_id: str = typer.Option(..., "--account-id"),
    currency: str = typer.Option(..., "--currency"),
    classification: str = typer.Option(..., "--classification"),
    reason: str = typer.Option(..., "--reason"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Classify a suspense observation without silently changing the ledger."""
    normalized = classification.strip().lower()
    if normalized not in CASH_SUSPENSE_CLASSIFICATIONS:
        raise typer.BadParameter(
            f"--classification must be one of {sorted(CASH_SUSPENSE_CLASSIFICATIONS)}"
        )
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    if not store.classify_cash_suspense(
        account_id=account_id,
        currency=currency,
        classification=normalized,
    ):
        raise typer.BadParameter("no open cash suspense exists for that account/currency")
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    run_id = new_run_id()
    suspense = next(
        (
            row
            for row in store.list_cash_suspense(account_id=account_id)
            if str(row.get("currency")) == currency.strip().upper()
        ),
        None,
    )
    save_audited_system_event(
        store,
        audit,
        run_id,
        SystemEventType.CASH_DRIFT_CLASSIFIED,
        {
            "account_id": account_id,
            "currency": currency.strip().upper(),
            "classification": normalized,
            "flow_class": flow_class_for_cash_suspense(normalized),
            "snapshot_id": suspense.get("last_snapshot_id") if suspense else None,
            "decided_at": utc_now().isoformat(),
            "decided_by": "operator_cli",
            "reason": reason,
            "previous_amount": suspense.get("amount") if suspense else None,
        },
    )
    typer.echo(
        f"classified account_id={account_id} currency={currency.strip().upper()} "
        f"classification={normalized}"
    )


@app.command("adopt-broker-snapshot")
def adopt_broker_snapshot(
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
    account_id: str | None = typer.Option(None, "--account-id"),
    include_cash: bool = typer.Option(
        False,
        "--include-cash",
        help="Adopt broker cash only after explicitly classifying the drift.",
    ),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("adopt-broker-snapshot requires live_readonly or live_approval")
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    latest = (
        latest_snapshot_for_account(store, account_id)
        if account_id
        else store.load_latest_broker_account_snapshot()
    )
    if latest is None:
        scope = f" for account_id={account_id}" if account_id else ""
        raise typer.BadParameter(
            "adopt-broker-snapshot requires a latest broker snapshot" + scope
        )

    account = latest["payload"]["account"]
    adopted_account_id = str(
        latest["payload"].get("account_id") or latest.get("account_id") or ""
    )
    if not adopted_account_id:
        raise typer.BadParameter("broker snapshot is missing its Maestro account_id")
    existing_ledger = (
        store.load_latest_account_portfolio_state(adopted_account_id)
        if adopted_account_id
        else None
    )
    adopted_classification: str | None = None
    order_history_backfill_run_id = str(
        latest["payload"].get("order_history_backfill_run_id") or ""
    )
    if include_cash and adopted_account_id:
        suspense = store.list_cash_suspense(account_id=adopted_account_id)
        classified_by_snapshot_id = {
            int(row["last_snapshot_id"]): str(row.get("candidate_label") or "")
            for row in suspense
            if row.get("last_snapshot_id") is not None
            and row.get("status") == "classified"
            and row.get("candidate_label") != "unexplained"
        }
        if int(latest["id"]) not in classified_by_snapshot_id:
            raise typer.BadParameter(
                "--include-cash requires a non-unexplained cash-drift classification "
                "for the latest broker snapshot"
            )
        adopted_classification = classified_by_snapshot_id[int(latest["id"])]
        if str(account.get("source") or "").startswith("toss_"):
            verified_history = next(
                (
                    row
                    for row in store.list_system_events_by_type(
                        SystemEventType.BROKER_ORDER_HISTORY_BACKFILL,
                        limit=2000,
                    )
                    if str(row.get("run_id") or "") == order_history_backfill_run_id
                    and str((row.get("payload") or {}).get("account_id") or "")
                    == adopted_account_id
                    and int((row.get("payload") or {}).get("missing_ledger_count") or 0)
                    == 0
                ),
                None,
            )
            if not order_history_backfill_run_id or verified_history is None:
                raise typer.BadParameter(
                    "Toss cash adoption requires verified order-history coverage "
                    "for the selected broker snapshot"
                )
    if (
        str(account.get("source") or "").startswith("toss_")
        and "ledger_cash_by_currency" in account
        and account.get("ledger_cash_by_currency") is None
        and not include_cash
        and adopted_account_id
        and existing_ledger is None
    ):
        raise typer.BadParameter(
            "ledger cash is not established; run `maestro ledger open-baseline` "
            "or pass --include-cash after explicit classification"
        )
    state = _portfolio_state_from_broker_account(
        account,
        allowed_symbols=maestro_config.portfolio.allowed_symbols,
        universe=maestro_config.universe,
        unknown_symbol_policy=maestro_config.portfolio.unknown_broker_position_policy,
    )
    if existing_ledger is not None and not include_cash:
        state = PortfolioState(
            cash=existing_ledger.cash,
            cash_by_currency=dict(existing_ledger.cash_by_currency),
            positions=state.positions,
        )
    payload = {
        "schema_version": 2,
        "reason": reason,
        "include_cash": include_cash,
        # Adopting broker cash moves the ledger without writing an
        # account_cash_flow, so the change is never neutralised out of the
        # return.  Recording that intent here is what lets a later reader tell
        # an earned dividend apart from a bookkeeping correction instead of
        # having to parse the free-text reason.
        "ledger_effect": "cash_adopted_from_broker" if include_cash else "positions_only",
        "performance_effect": "retained_in_return" if include_cash else "none",
        "cash_drift_classification": adopted_classification,
        "order_history_backfill_run_id": order_history_backfill_run_id or None,
        "history_covered_through": (
            str(account.get("fetched_at") or latest.get("created_at") or "")
            if order_history_backfill_run_id
            else None
        ),
        "flow_class": (
            flow_class_for_cash_suspense(adopted_classification)
            if adopted_classification
            else None
        ),
        "broker_snapshot_id": latest["id"],
        "account_id": adopted_account_id or None,
        "broker_account_id": account.get("account_id"),
        "previous_ledger_cash_by_currency": (
            dict(existing_ledger.cash_by_currency) if existing_ledger is not None else None
        ),
        "broker_observed_cash_by_currency": dict(account.get("cash_by_currency") or {}),
        "decided_by": "operator_cli",
        "cash": state.cash,
        "cash_by_currency": state.cash_by_currency,
        "positions": state.positions,
    }
    run_id = new_run_id()
    store.save_portfolio_snapshot_with_event(
        run_id,
        state,
        account_id=adopted_account_id,
        event_type=str(SystemEventType.BROKER_SNAPSHOT_ADOPTED),
        event_payload=payload,
        save_global=account_id is None,
    )
    audit.log(run_id, str(SystemEventType.BROKER_SNAPSHOT_ADOPTED), payload)
    typer.echo(
        f"adopted run_id={run_id} broker_snapshot_id={latest['id']} "
        f"account_id={adopted_account_id or account.get('account_id') or 'none'} "
        f"cash={state.cash:.2f} "
        f"positions={len(state.positions)}"
    )


@app.command("adopt-account-attribution")
def adopt_account_attribution(
    account_id: str = typer.Option(..., "--account-id"),
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    if account_id not in maestro_config.account_strategy_targets:
        raise typer.BadParameter(
            f"account_strategy_targets is not configured for account_id={account_id}"
        )
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    positions = AccountAttributionReconciliationService(store, audit).adopt_latest(
        run_id=new_run_id(),
        account_id=account_id,
        reason=reason,
        adopted_by="cli",
    )
    typer.echo(
        f"adopted account_id={account_id} positions={len(positions)} "
        f"version={positions[0].version if positions else 1}"
    )


@app.command("reclassify-account-attribution")
def reclassify_account_attribution(
    account_id: str = typer.Option(..., "--account-id"),
    symbol: str = typer.Option(..., "--symbol"),
    from_bucket_id: str = typer.Option(..., "--from-bucket"),
    to_bucket_id: str = typer.Option(..., "--to-bucket"),
    quantity: float = typer.Option(..., "--quantity", min=0.000000001),
    reason: str = typer.Option(..., "--reason"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    maestro_config, identity = _load_operator_config(config)
    targets = maestro_config.account_strategy_targets.get(account_id, {})
    if to_bucket_id not in targets:
        raise typer.BadParameter(
            f"unknown attribution target bucket for account_id={account_id}: {to_bucket_id}"
        )
    allowed_symbols = targets[to_bucket_id].allowed_symbols
    if to_bucket_id != "manual" and symbol not in allowed_symbols:
        raise typer.BadParameter(
            f"symbol is not allowed for attribution bucket {to_bucket_id}: {symbol}"
        )
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    positions = AccountAttributionReconciliationService(store, audit).reclassify_position(
        run_id=new_run_id(),
        account_id=account_id,
        symbol=symbol,
        from_bucket_id=from_bucket_id,
        to_bucket_id=to_bucket_id,
        quantity=quantity,
        reason=reason,
        reclassified_by="cli",
    )
    version = positions[0].version if positions else 1
    typer.echo(
        f"reclassified account_id={account_id} symbol={symbol} quantity={quantity:g} "
        f"from_bucket={from_bucket_id} to_bucket={to_bucket_id} version={version}"
    )


@app.command("reconcile-fills")
def reconcile_fills(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("reconcile-fills requires mode=live_readonly or live_approval")
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    result = PartialFillReconciliationService(store, audit).reconcile_latest(new_run_id())
    typer.echo(
        f"applied_fills={len(result.applied_fills)} "
        f"skipped_fills={len(result.skipped_fills)} "
        f"portfolio_updated={str(result.portfolio_updated).lower()} "
        f"cash={result.cash:.2f} positions={len(result.positions)}"
    )


@app.command("restore-pending-maestro-sell-attribution")
def restore_pending_maestro_sell_attribution(
    account_id: str = typer.Option(..., "--account-id"),
    symbol: str = typer.Option(..., "--symbol"),
    bucket_id: str = typer.Option(..., "--bucket"),
    quantity: float = typer.Option(..., "--quantity", min=0.000000001),
    reason: str = typer.Option(..., "--reason"),
    config: Path | None = CONFIG_OPTION,
) -> None:
    """Restore warning-backed attribution before replaying a delayed sell fill."""
    maestro_config, identity = _load_operator_config(config)
    targets = maestro_config.account_strategy_targets.get(account_id, {})
    if bucket_id not in targets:
        raise typer.BadParameter(
            f"unknown attribution target bucket for account_id={account_id}: {bucket_id}"
        )
    if symbol not in targets[bucket_id].allowed_symbols:
        raise typer.BadParameter(
            f"symbol is not allowed for attribution bucket {bucket_id}: {symbol}"
        )
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    positions = AccountAttributionReconciliationService(
        store, audit
    ).restore_pending_maestro_sell(
        run_id=new_run_id(),
        account_id=account_id,
        symbol=symbol,
        bucket_id=bucket_id,
        quantity=quantity,
        reason=reason,
        restored_by="cli",
    )
    version = positions[0].version if positions else 1
    typer.echo(
        f"restored account_id={account_id} symbol={symbol} quantity={quantity:g} "
        f"bucket={bucket_id} version={version}"
    )


@app.command("resume-order-tracking")
def resume_order_tracking(
    config: Path | None = CONFIG_OPTION,
    limit: int = typer.Option(100, help="Maximum outstanding orders to inspect."),
) -> None:
    """Re-poll orders whose status poll window closed before a terminal state.

    The lifecycle poll loop is bounded, so an order still working at the last poll is
    left live at the broker with nobody watching it. Fill reconciliation replays
    recorded status snapshots, so without a fresh poll such a fill can never be
    applied. Run this on a timer to keep those orders tracked.
    """
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter(
            "resume-order-tracking requires mode=live_readonly or live_approval"
        )
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)

    def status_client_for(account_id: str | None) -> LiveOrderStatusClient:
        return build_live_order_status_client(maestro_config, account_id=account_id)

    service = LiveOrderTrackingResumeService(
        store,
        audit,
        status_client_for,
        notification_client=build_live_order_notification_client(maestro_config),
    )
    run_id = new_run_id()
    summary = service.resume(run_id, limit=limit)
    # Runs on a short timer, and the common case is nothing outstanding. Recording
    # those would bury the runs that actually did something.
    if summary["outstanding_orders"]:
        save_audited_system_event(
            store,
            audit,
            run_id,
            "live_order_tracking_resume",
            summary,
        )
    for entry in summary["polled"]:
        typer.echo(
            f"order_id={entry['order_id']} broker_order_id={entry['broker_order_id']} "
            f"{entry['previous_status']}->{entry['status']} "
            f"filled={entry['filled_quantity']}"
        )
    for failure in summary["failures"]:
        typer.echo(
            f"order_id={failure['order_id']} poll_failed "
            f"{failure['error_type']}: {failure['error_message']}"
        )
    typer.echo(
        f"outstanding_orders={summary['outstanding_orders']} "
        f"polled={len(summary['polled'])} "
        f"resolved={len(summary['resolved_order_ids'])} "
        f"still_open={len(summary['still_open_order_ids'])} "
        f"failed={len(summary['failures'])} "
        f"applied_fills={len(summary['applied_fills'])}"
    )
    if summary["failures"]:
        raise typer.Exit(code=1)


@app.command("recover-live-order")
def recover_live_order(
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    try:
        result = WorkflowRecoveryService(maestro_config, store, audit).recover_live_orders(
            reason=reason,
            decided_by="cli",
            manual_attestation=True,
            allow_without_blockers=True,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(
        f"recovery_completed run_id={result.run_id} "
        f"broker_snapshot_ids={','.join(map(str, result.broker_snapshot_ids)) or 'none'} "
        f"applied_fills={result.applied_fill_count}"
    )


@app.command("dashboard")
def dashboard(
    config: Path | None = CONFIG_OPTION,
    host: str = typer.Option("127.0.0.1", help="Dashboard bind host."),
    port: int = typer.Option(8503, help="Dashboard bind port."),
    env_file: Path | None = typer.Option(
        None,
        "--env-file",
        help="Optional operator environment file, for example /etc/maestro/maestro.env.",
    ),
    signal_config: Path | None = typer.Option(
        None,
        "--signal-config",
        help="Signal config for Virtuoso generate-signal actions.",
    ),
) -> None:
    if env_file is not None and not load_env_file(env_file):
        raise typer.BadParameter(f"env file not found or empty: {env_file}")
    resolved_config = _resolve_config(config)
    from maestro.dashboard.server import run_dashboard_server

    run_dashboard_server(
        resolved_config,
        host=host,
        port=port,
        signal_config_path=signal_config,
    )


def _safety_service(config: Path | None) -> SafetyControlService:
    maestro_config, identity = _load_operator_config(config)
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    return SafetyControlService(store, audit)


def _validate_live_smoke_snapshot(snapshot_payload: dict, maestro_config) -> None:
    account = snapshot_payload.get("account", {})
    if not account.get("account_id"):
        raise typer.BadParameter("KIS read-only smoke returned an empty account_id")
    if float(account.get("cash", -1)) < 0:
        raise typer.BadParameter("KIS read-only smoke returned negative cash")
    if float(account.get("buying_power", -1)) < 0:
        raise typer.BadParameter("KIS read-only smoke returned negative buying_power")
    prices = snapshot_payload.get("current_prices", {})
    missing_prices = [
        symbol for symbol in maestro_config.portfolio.allowed_symbols if symbol not in prices
    ]
    if missing_prices:
        raise typer.BadParameter(
            "KIS read-only smoke did not return prices for: " + ",".join(missing_prices)
        )


def _validate_live_smoke_secret_redaction(maestro_config, snapshot_payload: dict) -> None:
    secrets = _configured_secret_values(maestro_config)
    if not secrets:
        return
    snapshot_text = json.dumps(snapshot_payload, default=str)
    if any(secret in snapshot_text for secret in secrets):
        raise typer.BadParameter("KIS read-only smoke snapshot contains configured secret values")
    audit_path = Path(maestro_config.audit.jsonl_path)
    if audit_path.exists():
        audit_text = audit_path.read_text(encoding="utf-8")
        if any(secret in audit_text for secret in secrets):
            raise typer.BadParameter(
                "KIS read-only smoke audit log contains configured secret values"
            )


def _portfolio_state_from_broker_account(
    account: dict,
    *,
    allowed_symbols: list[str],
    universe=None,
    unknown_symbol_policy: str = "fail_closed",
) -> PortfolioState:
    try:
        return portfolio_state_from_broker_account(
            account,
            allowed_symbols=allowed_symbols,
            universe=universe,
            unknown_symbol_policy=unknown_symbol_policy,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _configured_secret_values(maestro_config) -> list[str]:
    values = []
    for env_name in (
        maestro_config.kis.app_key_env,
        maestro_config.kis.app_secret_env,
        maestro_config.kis.access_token_env,
        maestro_config.kis.approval_key_env,
        maestro_config.approval.telegram_bot_token_env,
        maestro_config.fx.api_key_env,
    ):
        value = DEFAULT_CREDENTIAL_RESOLVER.get(env_name)
        if value:
            values.append(value)
    return values


def _uses_placeholder_telegram_ids(maestro_config) -> bool:
    placeholder = 123456789
    return placeholder in set(
        maestro_config.approval.telegram_allowed_chat_ids
    ) or placeholder in set(maestro_config.approval.whitelisted_user_ids)


def _ops_alert_message(report, alert_checks) -> str:
    lines = [
        "Maestro ops alert",
        f"status: {report.status}",
        f"generated_at: {report.generated_at}",
    ]
    for check in alert_checks[:10]:
        lines.append(f"{check.name}: {check.status} {check.message}")
    return "\n".join(lines)


def _personal_operator_config(output: Path) -> dict:
    base_dir = output.parent
    state_dir = base_dir / "var"
    return {
        "mode": "live_approval",
        "portfolio": {
            "base_currency": "USD",
            "allowed_symbols": ["CASH_USD", "AAPL", "MSFT", "VOO", "QQQ", "SGOV"],
        },
        "universe": {
            "policy": {
                "allowed_asset_types": ["stock", "etf", "us_etf"],
                "allowed_regions": ["US"],
                "allowed_currencies": ["USD"],
                "allowed_broker_products": ["kis_overseas_stock"],
                "allowed_exchange_codes": ["NASD", "NYSE", "AMEX"],
                "denied_symbols": [],
                "denied_asset_tags": [],
                "max_new_symbols_per_run": 1,
                "require_operator_approval_for_tradable": True,
                "require_broker_tradability_check": True,
                "require_data_freshness_check": True,
            },
            "instruments": [
                _personal_instrument("CASH_USD", "cash", "USD", None, 0.01, 0.01, 0.01, 0),
                _personal_instrument("AAPL", "stock", "AAPL", "NASD", 1, 0.01, 1, 1),
                _personal_instrument("MSFT", "stock", "MSFT", "NASD", 1, 0.01, 1, 1),
                _personal_instrument("VOO", "etf", "VOO", "AMEX", 1, 0.01, 1, 1),
                _personal_instrument("QQQ", "etf", "QQQ", "NASD", 1, 0.01, 1, 1),
                _personal_instrument("SGOV", "etf", "SGOV", "AMEX", 1, 0.01, 1, 1),
            ],
        },
        "strategies": [
            {
                "id": "sample_static_allocation",
                "enabled": True,
                "weight": 1.0,
                "entrypoint": "sample_static_allocation.strategy:SampleStaticAllocationStrategy",
                "config": {
                    "allocations": {
                        "CASH_USD": 0.1,
                        "VOO": 0.45,
                        "QQQ": 0.25,
                        "SGOV": 0.2,
                    }
                },
            }
        ],
        "datahub": {
            "provider": "yahoo",
            "stale_after_seconds": 604800,
            "symbol_map": {
                "AAPL": "AAPL",
                "MSFT": "MSFT",
                "VOO": "VOO",
                "QQQ": "QQQ",
                "SGOV": "SGOV",
            },
        },
        "execution": {
            "engine": "paper",
            "order_posture": "dry_run",
            "require_reconciliation_pass": True,
            "live_order_limits": {
                "max_order_notional_by_currency": {"USD": 100},
                "max_daily_notional_by_currency": {"USD": 300},
                "max_daily_order_count": 1,
                "daily_loss_limit_by_currency": {},
                "fee_buffer_pct": 0.002,
            },
            "allowed_order_type": "limit",
            "order_status_poll_interval_seconds": 30,
            "order_status_max_polls": 20,
            "order_status_terminal_timeout_seconds": 1800,
            "market_session": {
                "required": True,
                "timezone": "America/New_York",
                "open": "09:30",
                "close": "16:00",
                "weekdays": [0, 1, 2, 3, 4],
                "holidays": [],
            },
            "broker_validation": {
                "require_quote_validation": False,
                "max_quote_deviation_pct": 0.05,
                "require_risk_validation": False,
            },
        },
        "monitoring": {
            "heartbeat_max_age_seconds": 3600,
            "scheduled_run_max_age_seconds": 86400,
        },
        "state": {"sqlite_path": str(state_dir / "maestro_personal_state.db")},
        "audit": {"jsonl_path": str(state_dir / "maestro_personal_audit.jsonl")},
        "approval": {
            "enabled": True,
            "provider": "telegram",
            "require_approval": True,
            "default_decision": "expired",
            "timeout_seconds": 300,
            "telegram_bot_token_env": "TELEGRAM_BOT_TOKEN",
            "telegram_allowed_chat_ids": [123456789],
            "whitelisted_user_ids": [123456789],
            "telegram_poll_interval_seconds": 1.0,
        },
        "kis": {
            "enabled": True,
            "provider": "kis",
            "broker_products": ["kis_overseas_stock"],
            "account_id": None,
            "account_id_env": "KIS_MOCK_ACCOUNT_ID",
            "app_key_env": "KIS_MOCK_APP_KEY",
            "app_secret_env": "KIS_MOCK_APP_SECRET",
            "access_token_env": "KIS_ACCESS_TOKEN",
            "approval_key_env": "KIS_APPROVAL_KEY",
            "token_cache_path": str(state_dir / "kis_access_token.json"),
            "paper_trading": False,
            "timeout_seconds": 10,
        },
        "reconciliation": {
            "cash_tolerance": 0.0,
            "position_quantity_tolerance": 0.0,
            "value_tolerance": 0.0,
            "max_age_seconds": 86400,
        },
    }


def _personal_instrument(
    symbol: str,
    asset_type: str,
    broker_symbol: str,
    exchange_code: str | None,
    quantity_step: float,
    price_tick: float,
    min_order_quantity: float,
    min_order_notional: float,
) -> dict:
    instrument = {
        "symbol": symbol,
        "asset_type": asset_type,
        "region": "US",
        "currency": "USD",
        "broker": "kis",
        "broker_product": "kis_overseas_stock",
        "broker_symbol": broker_symbol,
        "quantity_step": quantity_step,
        "price_tick": price_tick,
        "min_order_quantity": min_order_quantity,
        "min_order_notional": min_order_notional,
    }
    if exchange_code:
        instrument["exchange_code"] = exchange_code
    return instrument


def _personal_stage(stage: str, status: str, message: str, next_command: str) -> dict[str, str]:
    return {"stage": stage, "status": status, "message": message, "next": next_command}


def _overall_personal_status(stages: list[dict[str, str]]) -> str:
    if all(stage["status"] == "ok" for stage in stages):
        return "ok"
    if any(stage["status"] == "fail" for stage in stages):
        return "blocked"
    return "warn"


def _worst_status(checks: dict, names: list[str]) -> str:
    selected = [checks[name].status for name in names if name in checks]
    if not selected or "fail" in selected:
        return "fail"
    if "warn" in selected:
        return "warn"
    return "ok"


def _all_ok(checks: dict, names: list[str]) -> str:
    if all(checks.get(name) and checks[name].status == "ok" for name in names):
        return "ok"
    return "fail"


def _telegram_personal_status(config: MaestroConfig) -> str:
    if not config.approval.enabled or not config.approval.require_approval:
        return "fail"
    if config.approval.provider != "telegram":
        return "fail"
    if not config.approval.telegram_allowed_chat_ids or not config.approval.whitelisted_user_ids:
        return "fail"
    if not DEFAULT_CREDENTIAL_RESOLVER.present(config.approval.telegram_bot_token_env):
        return "fail"
    return "ok"


def _dry_run_personal_status(config: MaestroConfig, checks: dict) -> str:
    preflight = checks.get("live_approval_preflight")
    if config.mode != RunMode.LIVE_APPROVAL or config.execution.order_posture != "dry_run":
        return "fail"
    if preflight is None or preflight.status == "fail":
        return "fail"
    if not config.strategies:
        return "fail"
    return "ok" if preflight.status == "ok" else "warn"


def _minimum_live_personal_status(config: MaestroConfig, report) -> str:
    if config.mode != RunMode.LIVE_APPROVAL:
        return "fail"
    if config.execution.order_posture != "armed":
        return "fail"
    if _telegram_personal_status(config) != "ok":
        return "fail"
    return "ok" if not private_beta_failures(config, report) else "fail"


if __name__ == "__main__":
    app()

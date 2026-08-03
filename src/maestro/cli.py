import fcntl
import json
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TypeVar

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
from maestro.monitoring.audit_logger import AuditLogger
from maestro.monitoring.health import HealthService
from maestro.monitoring.logging import configure_structured_logging
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
    SystemEventType,
    flow_class_for_cash_suspense,
    save_audited_system_event,
)
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore

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


def _send_signal_funding_request_notifications(
    maestro_config: MaestroConfig,
    signal_run_id: str,
) -> int:
    if maestro_config.approval.provider != "telegram":
        return 0
    chat_ids = maestro_config.approval.telegram_allowed_chat_ids
    if not chat_ids:
        return 0
    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        typer.echo("telegram_funding_request=warn message=missing_bot_token")
        return 0
    store = StateStore(
        maestro_config.state.sqlite_path,
        maestro_config.portfolio.initial_cash,
        maestro_config.portfolio.cash_by_currency,
    )
    signal = store.load_signal_package(signal_run_id) or {}
    funding_requests = signal.get("funding_requests") or []
    if not funding_requests:
        return 0
    try:
        client = TelegramBotAPIClient(
            token_env=maestro_config.approval.telegram_bot_token_env,
            timeout_seconds=10.0,
        )
        for request in funding_requests:
            request_id = str(request.get("request_id") or "")
            message = format_contribution_funding_request(request)
            markup = funding_request_reply_markup(request_id)
            for chat_id in chat_ids:
                client.send_message(chat_id, message, reply_markup=markup)
    except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
        typer.echo(f"telegram_funding_request=warn message={exc}")
        return 0
    sent = len(funding_requests) * len(chat_ids)
    typer.echo(f"telegram_funding_request=sent messages={sent}")
    return sent


def _send_signal_budget_request_notifications(
    maestro_config: MaestroConfig,
    signal_run_id: str,
) -> int:
    if maestro_config.approval.provider != "telegram":
        return 0
    chat_ids = maestro_config.approval.telegram_allowed_chat_ids
    if not chat_ids:
        return 0
    if not DEFAULT_CREDENTIAL_RESOLVER.present(maestro_config.approval.telegram_bot_token_env):
        typer.echo("telegram_budget_request=warn message=missing_bot_token")
        return 0
    store = StateStore(
        maestro_config.state.sqlite_path,
        maestro_config.portfolio.initial_cash,
        maestro_config.portfolio.cash_by_currency,
    )
    signal = store.load_signal_package(signal_run_id) or {}
    budget_requests = signal.get("budget_requests") or []
    if not budget_requests:
        return 0
    try:
        client = TelegramBotAPIClient(
            token_env=maestro_config.approval.telegram_bot_token_env,
            timeout_seconds=10.0,
        )
        for request in budget_requests:
            message = format_contribution_budget_request(request)
            markup = budget_request_reply_markup(request)
            for chat_id in chat_ids:
                client.send_message(chat_id, message, reply_markup=markup)
    except (RuntimeError, TimeoutError, TypeError, ValueError) as exc:
        typer.echo(f"telegram_budget_request=warn message={exc}")
        return 0
    sent = len(budget_requests) * len(chat_ids)
    typer.echo(f"telegram_budget_request=sent messages={sent}")
    return sent


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
    existing_ledger = (
        store.load_latest_account_portfolio_state(adopted_account_id)
        if adopted_account_id
        else None
    )
    adopted_classification: str | None = None
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
    run_id = new_run_id()
    if adopted_account_id:
        store.save_portfolio_snapshot(run_id, state, account_id=adopted_account_id)
    if account_id is None:
        store.save_portfolio_snapshot(run_id, state)
    payload = {
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
    save_audited_system_event(
        store,
        audit,
        run_id,
        SystemEventType.BROKER_SNAPSHOT_ADOPTED,
        payload,
    )
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

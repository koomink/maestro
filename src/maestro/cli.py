import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

import typer
import yaml

from maestro.config.app_fragment_composition import app_fragment_recommendation_failures
from maestro.config.env import load_project_dotenv
from maestro.config.identity import ConfigIdentity
from maestro.config.loader import load_config_with_identity
from maestro.config.models import MaestroConfig
from maestro.core.enums import ProfileStage, RunMode
from maestro.core.ids import new_run_id
from maestro.core.time_display import format_operator_time, operator_timezone
from maestro.execution.broker_state import portfolio_state_from_broker_account
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.funding_requests import (
    format_contribution_funding_request,
    funding_request_reply_markup,
)
from maestro.execution.live_orders import PartialFillReconciliationService
from maestro.execution.reconciliation import BrokerReconciliationService
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
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.safety.controls import SafetyControlService
from maestro.scaffold import create_virtuoso_app_scaffold
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore

app = typer.Typer()

CONFIG_ENV_VAR = "MAESTRO_CONFIG"
CONFIG_OPTION = typer.Option(
    None,
    "--config",
    envvar=CONFIG_ENV_VAR,
    help=f"Path to operator config. Defaults to ${CONFIG_ENV_VAR}.",
)


def _load_dotenv() -> None:
    load_project_dotenv()


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
    kis_accounts = _kis_readonly_accounts(maestro_config)
    if not kis_accounts:
        return None

    def refresh() -> None:
        for logical_account_id, kis_config in kis_accounts:
            KISReadOnlyService(
                kis_config,
                store,
                audit,
                instruments=maestro_config.universe.instruments,
                logical_account_id=logical_account_id,
            ).fetch_and_store_snapshot(maestro_config.portfolio.allowed_symbols)

    return refresh


def _kis_readonly_accounts(maestro_config: MaestroConfig):
    if maestro_config.kis.enabled:
        return [(None, maestro_config.kis)]
    return [
        (account.id, account.to_kis_config())
        for account in maestro_config.accounts
        if account.enabled and account.broker == "kis"
    ]


def _profile_datahub_providers(maestro_config: MaestroConfig) -> str:
    return ",".join(
        f"{provider.name}:{provider.provider}"
        for provider in maestro_config.datahub.effective_providers()
        if provider.enabled
    )


@app.command("run-once")
def run_once(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    try:
        summary = MaestroOrchestrator(maestro_config, config_identity=identity).run_once()
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
) -> None:
    maestro_config, identity = _load_operator_config(config)
    summary = MaestroOrchestrator(maestro_config, config_identity=identity).approve_signal(
        signal_run_id,
    )
    typer.echo(
        f"signal_run_id={summary.signal_run_id} run_id={summary.run_id} "
        f"orders={summary.orders_created} approval_status={summary.approval_status}"
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
    lock_path: Path = typer.Option(
        Path("/tmp/maestro-symphony-signal.lock"),
        "--lock-path",
        envvar="MAESTRO_SIGNAL_LOCK_PATH",
    ),
) -> None:
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
            )
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _run_daily_signal_approval(
    *,
    readonly_config: Path | None,
    signal_config: Path | None,
    approval_config: Path | None,
    stop_telegram_operator: bool,
    telegram_operator_service: str,
) -> None:
    readonly_maestro_config, readonly_identity = _load_operator_config(readonly_config)
    _refresh_daily_readonly(readonly_maestro_config, readonly_identity)

    signal_maestro_config, signal_identity = _load_operator_config(signal_config)
    signal_summary = MaestroOrchestrator(
        signal_maestro_config,
        config_identity=signal_identity,
    ).run_signal()
    typer.echo(
        f"symphony_daily status=signal_completed "
        f"signal_run_id={signal_summary.signal_run_id} "
        f"action_required={str(signal_summary.action_required).lower()} "
        f"orders_preview={signal_summary.orders_preview_count}"
    )
    _send_signal_summary_notification(signal_maestro_config, signal_summary)

    if not signal_summary.action_required:
        funding_sent = _send_signal_funding_request_notifications(
            signal_maestro_config,
            signal_summary.signal_run_id,
        )
        if funding_sent:
            typer.echo(
                f"symphony_daily status=funding_required "
                f"signal_run_id={signal_summary.signal_run_id}"
            )
        else:
            typer.echo(
                f"symphony_daily status=no_action "
                f"signal_run_id={signal_summary.signal_run_id}"
            )
        return

    telegram_stopped = False
    try:
        if stop_telegram_operator:
            _systemctl("stop", telegram_operator_service)
            telegram_stopped = True
        approval_maestro_config, approval_identity = _load_operator_config(approval_config)
        approval_summary = MaestroOrchestrator(
            approval_maestro_config,
            config_identity=approval_identity,
        ).approve_signal(signal_summary.signal_run_id)
    finally:
        if telegram_stopped:
            try:
                _systemctl("start", telegram_operator_service)
            except subprocess.CalledProcessError as exc:
                typer.echo(
                    "symphony_daily status=warn "
                    f"reason=telegram_operator_restart_failed service={telegram_operator_service} "
                    f"returncode={exc.returncode}"
                )
    typer.echo(
        f"symphony_daily status=approval_completed "
        f"signal_run_id={approval_summary.signal_run_id} "
        f"run_id={approval_summary.run_id} "
        f"orders={approval_summary.orders_created} "
        f"approval_status={approval_summary.approval_status}"
    )


def _refresh_daily_readonly(
    maestro_config: MaestroConfig,
    identity: ConfigIdentity,
) -> None:
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        typer.echo(
            f"symphony_daily readonly=skipped reason=mode mode={maestro_config.mode.value}"
        )
        return
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    kis_accounts = _kis_readonly_accounts(maestro_config)
    if not kis_accounts:
        typer.echo("symphony_daily readonly=skipped reason=no_kis_accounts")
        return
    for logical_account_id, kis_config in kis_accounts:
        service = KISReadOnlyService(
            kis_config,
            store,
            audit,
            instruments=maestro_config.universe.instruments,
            logical_account_id=logical_account_id,
        )
        service.fetch_and_store_snapshot(maestro_config.portfolio.allowed_symbols)
    result = BrokerReconciliationService(
        maestro_config.reconciliation,
        store,
        audit,
    ).reconcile_latest()
    status = "passed" if result.passed else "failed"
    typer.echo(
        f"symphony_daily readonly=refreshed accounts_synced={len(kis_accounts)} "
        f"reconciliation={status} issues={len(result.issues)}"
    )
    if not result.passed:
        raise typer.Exit(1)


def _send_signal_funding_request_notifications(
    maestro_config: MaestroConfig,
    signal_run_id: str,
) -> int:
    if maestro_config.approval.provider != "telegram":
        return 0
    chat_ids = maestro_config.approval.telegram_allowed_chat_ids
    if not chat_ids:
        return 0
    if not os.getenv(maestro_config.approval.telegram_bot_token_env):
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


def _send_signal_summary_notification(maestro_config: MaestroConfig, summary) -> None:
    if maestro_config.approval.provider != "telegram":
        return
    chat_ids = maestro_config.approval.telegram_allowed_chat_ids
    if not chat_ids:
        return
    if not os.getenv(maestro_config.approval.telegram_bot_token_env):
        typer.echo("telegram_signal_summary=warn message=missing_bot_token")
        return
    strategies = ", ".join(summary.loaded_strategies) if summary.loaded_strategies else "none"
    message = "\n".join(
        [
            "Maestro daily signal summary",
            f"signal_run_id: {summary.signal_run_id}",
            f"strategies: {strategies}",
            f"action_required: {str(summary.action_required).lower()}",
            f"orders_preview: {summary.orders_preview_count}",
        ]
    )
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
    if not os.getenv(maestro_config.approval.telegram_bot_token_env):
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

    if not os.getenv(maestro_config.approval.telegram_bot_token_env):
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
    if not os.getenv(maestro_config.approval.telegram_bot_token_env):
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
    )

    offset = None
    while True:
        try:
            offset = router.poll_once(offset=offset, timeout_seconds=timeout_seconds)
            typer.echo(f"telegram_operator status=ok offset={offset or 'none'}")
            if once:
                return
        except (RuntimeError, TimeoutError) as exc:
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
    if not os.getenv(maestro_config.approval.telegram_bot_token_env):
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

    if not os.getenv(maestro_config.approval.telegram_bot_token_env):
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
    current = _safety_service(config).clear_halt(new_run_id(), reason)
    typer.echo(f"state={current.state.value} reason={current.reason}")


@app.command("kis-sync")
def kis_sync(config: Path | None = CONFIG_OPTION) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("kis-sync requires mode=live_readonly or live_approval")
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    kis_accounts = _kis_readonly_accounts(maestro_config)
    if not kis_accounts:
        raise typer.BadParameter("kis-sync requires at least one enabled KIS account")
    synced = []
    try:
        for logical_account_id, kis_config in kis_accounts:
            service = KISReadOnlyService(
                kis_config,
                store,
                audit,
                instruments=maestro_config.universe.instruments,
                logical_account_id=logical_account_id,
            )
            snapshot = service.fetch_and_store_snapshot(maestro_config.portfolio.allowed_symbols)
            synced.append((logical_account_id, snapshot))
    except ValueError as exc:
        raise typer.BadParameter(f"kis-sync failed: {exc}") from exc
    if len(synced) == 1 and synced[0][0] is None:
        snapshot = synced[0][1]
        typer.echo(
            f"account_id={snapshot.account.account_id} cash={snapshot.account.cash:.2f} "
            f"buying_power={snapshot.account.buying_power:.2f} "
            f"positions={len(snapshot.account.positions)} "
            f"total_value={snapshot.account.total_value:.2f}"
        )
        return
    for logical_account_id, snapshot in synced:
        typer.echo(
            f"account_id={logical_account_id} broker_account_id={snapshot.account.account_id} "
            f"cash={snapshot.account.cash:.2f} "
            f"buying_power={snapshot.account.buying_power:.2f} "
            f"positions={len(snapshot.account.positions)} "
            f"total_value={snapshot.account.total_value:.2f}"
        )
    typer.echo(f"accounts_synced={len(synced)}")


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
        ).reconcile_latest()
    except ValueError as exc:
        raise typer.BadParameter(f"reconcile broker snapshot refresh failed: {exc}") from exc
    status = "passed" if result.passed else "failed"
    typer.echo(
        f"status={status} issues={len(result.issues)} "
        f"cash_difference={result.cash_difference or 0.0:.2f} "
        f"broker_account_id={result.broker_account_id or 'none'}"
    )
    if not result.passed:
        for issue in result.issues:
            symbol = f" symbol={issue.symbol}" if issue.symbol else ""
            typer.echo(f"issue={issue.issue_type}{symbol} message={issue.message}")
        raise typer.Exit(1)


@app.command("adopt-broker-snapshot")
def adopt_broker_snapshot(
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("adopt-broker-snapshot requires live_readonly or live_approval")
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    latest = store.load_latest_broker_account_snapshot()
    if latest is None:
        raise typer.BadParameter("adopt-broker-snapshot requires a latest broker snapshot")

    account = latest["payload"]["account"]
    state = _portfolio_state_from_broker_account(
        account,
        allowed_symbols=maestro_config.portfolio.allowed_symbols,
        universe=maestro_config.universe,
    )
    run_id = new_run_id()
    store.save_portfolio_snapshot(run_id, state)
    payload = {
        "reason": reason,
        "broker_snapshot_id": latest["id"],
        "broker_account_id": account.get("account_id"),
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
        f"account_id={account.get('account_id') or 'none'} cash={state.cash:.2f} "
        f"positions={len(state.positions)}"
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


@app.command("recover-live-order")
def recover_live_order(
    config: Path | None = CONFIG_OPTION,
    reason: str = typer.Option(..., "--reason"),
) -> None:
    maestro_config, identity = _load_operator_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("recover-live-order requires live_readonly or live_approval")
    store = _state_store(maestro_config, identity)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    latest_snapshot = store.load_latest_broker_account_snapshot()
    if latest_snapshot is None:
        raise typer.BadParameter("recover-live-order requires a latest broker snapshot")
    latest_reconciliation = store.load_latest_system_event("broker_reconciliation")
    if latest_reconciliation is None or latest_reconciliation["payload"].get("passed") is not True:
        raise typer.BadParameter("recover-live-order requires a passing broker reconciliation")

    run_id = new_run_id()
    fill_result = PartialFillReconciliationService(store, audit).reconcile_latest(run_id)
    payload = {
        "reason": reason,
        "broker_snapshot_id": latest_snapshot["id"],
        "broker_reconciliation_event_id": latest_reconciliation["id"],
        "fill_reconciliation": fill_result.model_dump(mode="json"),
    }
    store.save_system_event(run_id, "live_order_recovery_completed", payload)
    audit.log(run_id, "live_order_recovery_completed", payload)
    typer.echo(
        f"recovery_completed run_id={run_id} broker_snapshot_id={latest_snapshot['id']} "
        f"applied_fills={len(fill_result.applied_fills)} "
        f"skipped_fills={len(fill_result.skipped_fills)}"
    )


@app.command("dashboard")
def dashboard(
    config: Path | None = CONFIG_OPTION,
    host: str = typer.Option("127.0.0.1", help="Dashboard bind host."),
    port: int = typer.Option(8503, help="Dashboard bind port."),
    signal_config: Path | None = typer.Option(
        None,
        "--signal-config",
        help="Signal config for Virtuoso generate-signal actions.",
    ),
) -> None:
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
) -> PortfolioState:
    try:
        return portfolio_state_from_broker_account(
            account,
            allowed_symbols=allowed_symbols,
            universe=universe,
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
    ):
        value = os.getenv(env_name)
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
            "telegram_allowed_chat_ids": [],
            "whitelisted_user_ids": [],
            "telegram_poll_interval_seconds": 1.0,
        },
        "kis": {
            "enabled": True,
            "provider": "kis",
            "broker_product": "kis_overseas_stock",
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
    if not os.getenv(config.approval.telegram_bot_token_env):
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

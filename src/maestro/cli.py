import json
import os
import subprocess
import sys
import time
from pathlib import Path

import typer
import yaml
from dotenv import load_dotenv

from maestro.config.loader import load_config
from maestro.config.models import MaestroConfig
from maestro.core.enums import RunMode
from maestro.core.ids import new_run_id
from maestro.execution.brokers.kis.service import KISReadOnlyService
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
from maestro.state.events import SystemEventType, save_audited_system_event
from maestro.state.models import PortfolioState
from maestro.state.store import StateStore

app = typer.Typer()


def _load_dotenv() -> None:
    load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)


@app.callback()
def main() -> None:
    """Maestro command line interface."""
    _load_dotenv()
    configure_structured_logging()


@app.command("run-once")
def run_once(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    summary = MaestroOrchestrator(maestro_config).run_once()
    typer.echo(
        f"run_id={summary.run_id} strategies={summary.loaded_strategies} "
        f"orders={summary.orders_created} total_value={summary.total_value:.2f} "
        f"cash={summary.cash:.2f}"
    )


@app.command("status")
def status(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    current = store.load_latest_portfolio_state()
    store_status = store.status()
    typer.echo(
        f"cash={current.cash:.2f} positions={len(current.positions)} "
        f"strategy_runs={store_status['counts']['strategy_runs']} "
        f"orders={store_status['counts']['orders']} "
        f"approvals={store_status['counts']['approvals']} "
        f"broker_snapshots={store_status['counts']['broker_account_snapshots']}"
    )


@app.command("health")
def health(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    report = HealthService(maestro_config, store).run()
    for line in report.text_lines():
        typer.echo(line)


@app.command("heartbeat")
def heartbeat(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    run_id = new_run_id()
    payload = {"mode": maestro_config.mode.value, "source": "cli"}
    store.save_system_event(run_id, "maestro_heartbeat", payload)
    audit.log(run_id, "maestro_heartbeat", payload)
    typer.echo(f"heartbeat run_id={run_id} mode={maestro_config.mode.value}")


@app.command("ops-alerts")
def ops_alerts(
    config: Path = typer.Option(..., "--config"),
    allow_mock: bool = typer.Option(False, "--allow-mock"),
) -> None:
    maestro_config = load_config(config)
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
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
def live_preflight(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    if maestro_config.mode != RunMode.LIVE_APPROVAL:
        raise typer.BadParameter("live-preflight requires mode=live_approval")
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
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
    config: Path = typer.Option(..., "--config"),
    once: bool = typer.Option(False, "--once"),
    timeout_seconds: int = typer.Option(10, "--timeout-seconds"),
) -> None:
    maestro_config = load_config(config)
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

    store = StateStore(
        maestro_config.state.sqlite_path,
        maestro_config.portfolio.initial_cash,
        maestro_config.portfolio.cash_by_currency,
    )
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    router = TelegramOperatorCommandRouter(
        config=maestro_config,
        store=store,
        audit=audit,
        client=TelegramBotAPIClient(
            token_env=maestro_config.approval.telegram_bot_token_env,
            timeout_seconds=max(float(timeout_seconds) + 5.0, 10.0),
        ),
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
def telegram_set_commands(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    if maestro_config.approval.provider != "telegram":
        raise typer.BadParameter("telegram-set-commands requires approval.provider=telegram")
    if not os.getenv(maestro_config.approval.telegram_bot_token_env):
        typer.echo("telegram_set_commands status=fail message=missing_bot_token")
        raise typer.Exit(1)
    commands = telegram_bot_commands()
    TelegramBotAPIClient(
        token_env=maestro_config.approval.telegram_bot_token_env,
        timeout_seconds=10.0,
    ).set_my_commands(commands)
    typer.echo(f"telegram_set_commands status=ok commands={len(commands)}")


@app.command("beta-preflight")
def beta_preflight(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    if maestro_config.mode != RunMode.LIVE_APPROVAL:
        raise typer.BadParameter("beta-preflight requires mode=live_approval")
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
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


@app.command("personal-check")
def personal_check(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
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
            f"maestro health --config {config}",
        ),
        _personal_stage(
            "readonly_ready",
            _all_ok(checks, ["kis_env", "broker_snapshot", "reconciliation"]),
            "KIS env, broker snapshot, and reconciliation are ready",
            f"maestro live-smoke --config {config} --check kis-readonly",
        ),
        _personal_stage(
            "telegram_ready",
            _telegram_personal_status(maestro_config),
            "Telegram approval config and token are ready",
            f"maestro live-smoke --config {config} --check telegram-approval",
        ),
        _personal_stage(
            "dry_run_ready",
            _dry_run_personal_status(maestro_config, checks),
            "approval-gated dry-run config is ready",
            f"maestro live-smoke --config {config} --check live-dry-run",
        ),
        _personal_stage(
            "minimum_live_ready",
            _minimum_live_personal_status(maestro_config, report),
            "minimum-size approval-gated live order gate is ready",
            f"maestro beta-preflight --config {config}",
        ),
    ]
    typer.echo(f"personal_check status={_overall_personal_status(stages)} config={config}")
    for stage in stages:
        typer.echo(
            f"stage={stage['stage']} status={stage['status']} "
            f'message={stage["message"]} next="{stage["next"]}"'
        )


@app.command("operator-evidence")
def operator_evidence(
    config: Path = typer.Option(..., "--config"),
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    maestro_config = load_config(config)
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    evidence = build_operator_evidence(maestro_config, store, config_path=config)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    output_text = str(output) if output is not None else "none"
    typer.echo(
        f"operator_evidence status={evidence['overall_status']} "
        f"config={config} output={output_text}"
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
    config: Path = typer.Option(..., "--config"),
    check: str = typer.Option("kis-readonly", "--check"),
    allow_mock: bool = typer.Option(False, "--allow-mock"),
) -> None:
    maestro_config = load_config(config)
    if check == "kis-readonly":
        _run_kis_readonly_live_smoke(maestro_config, allow_mock)
        return
    if check == "telegram-approval":
        _run_telegram_approval_live_smoke(maestro_config, allow_mock)
        return
    if check == "live-dry-run":
        _run_live_dry_run_smoke(maestro_config, allow_mock)
        return
    raise typer.BadParameter("supported checks: kis-readonly, telegram-approval, live-dry-run")


def _run_kis_readonly_live_smoke(maestro_config, allow_mock: bool) -> None:
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

    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
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


def _run_live_dry_run_smoke(maestro_config, allow_mock: bool) -> None:
    if maestro_config.mode != RunMode.LIVE_APPROVAL:
        raise typer.BadParameter("live-smoke --check live-dry-run requires mode=live_approval")
    if not maestro_config.execution.live_order_dry_run:
        raise typer.BadParameter("live-smoke --check live-dry-run requires live_order_dry_run=true")
    if not allow_mock:
        if maestro_config.approval.provider != "telegram":
            raise typer.BadParameter("live-smoke --check live-dry-run requires Telegram approval")
        if maestro_config.kis.provider != "kis":
            raise typer.BadParameter("live-smoke --check live-dry-run requires kis.provider=kis")
        store = StateStore(
            maestro_config.state.sqlite_path,
            maestro_config.portfolio.initial_cash,
        )
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

    orchestrator = MaestroOrchestrator(maestro_config)
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
    config: Path = typer.Option(..., "--config"),
    limit: int = typer.Option(10, "--limit"),
) -> None:
    maestro_config = load_config(config)
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    for row in store.list_approvals(limit=limit):
        decision = row["payload"]["decision"]
        typer.echo(
            f"{row['created_at']} approval_id={row['approval_id']} "
            f"run_id={row['run_id']} status={decision['status']}"
        )


@app.command("safety-status")
def safety_status(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    current = SafetyControlService(store, audit).current_state()
    typer.echo(
        f"state={current.state.value} source={current.source} "
        f"reason={current.reason} created_at={current.created_at} "
        f"updated_at={current.updated_at}"
    )


@app.command("pause")
def pause(
    config: Path = typer.Option(..., "--config"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    current = _safety_service(config).pause(new_run_id(), reason)
    typer.echo(f"state={current.state.value} reason={current.reason}")


@app.command("resume")
def resume(
    config: Path = typer.Option(..., "--config"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    current = _safety_service(config).resume(new_run_id(), reason)
    typer.echo(f"state={current.state.value} reason={current.reason}")


@app.command("kill-switch")
def kill_switch(
    config: Path = typer.Option(..., "--config"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    current = _safety_service(config).kill_switch(new_run_id(), reason)
    typer.echo(f"state={current.state.value} reason={current.reason}")


@app.command("clear-halt")
def clear_halt(
    config: Path = typer.Option(..., "--config"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    current = _safety_service(config).clear_halt(new_run_id(), reason)
    typer.echo(f"state={current.state.value} reason={current.reason}")


@app.command("kis-sync")
def kis_sync(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("kis-sync requires mode=live_readonly or live_approval")
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    try:
        service = KISReadOnlyService(
            maestro_config.kis,
            store,
            audit,
            instruments=maestro_config.universe.instruments,
        )
        snapshot = service.fetch_and_store_snapshot(maestro_config.portfolio.allowed_symbols)
    except ValueError as exc:
        raise typer.BadParameter(f"kis-sync failed: {exc}") from exc
    typer.echo(
        f"account_id={snapshot.account.account_id} cash={snapshot.account.cash:.2f} "
        f"buying_power={snapshot.account.buying_power:.2f} "
        f"positions={len(snapshot.account.positions)} "
        f"total_value={snapshot.account.total_value:.2f}"
    )


@app.command("kis-account")
def kis_account(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    latest = store.load_latest_broker_account_snapshot()
    if latest is None:
        typer.echo("No broker account snapshot found.")
        raise typer.Exit(1)
    account = latest["payload"]["account"]
    typer.echo(
        f"created_at={latest['created_at']} account_id={account['account_id']} "
        f"cash={account['cash']:.2f} buying_power={account['buying_power']:.2f} "
        f"positions={len(account['positions'])}"
    )


@app.command("reconcile")
def reconcile(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("reconcile requires mode=live_readonly or live_approval")
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    result = BrokerReconciliationService(
        maestro_config.reconciliation,
        store,
        audit,
    ).reconcile_latest()
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
    config: Path = typer.Option(..., "--config"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    maestro_config = load_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("adopt-broker-snapshot requires live_readonly or live_approval")
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    latest = store.load_latest_broker_account_snapshot()
    if latest is None:
        raise typer.BadParameter("adopt-broker-snapshot requires a latest broker snapshot")

    account = latest["payload"]["account"]
    state = _portfolio_state_from_broker_account(
        account,
        allowed_symbols=maestro_config.portfolio.allowed_symbols,
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
def reconcile_fills(config: Path = typer.Option(..., "--config")) -> None:
    maestro_config = load_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("reconcile-fills requires mode=live_readonly or live_approval")
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
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
    config: Path = typer.Option(..., "--config"),
    reason: str = typer.Option(..., "--reason"),
) -> None:
    maestro_config = load_config(config)
    if maestro_config.mode not in {RunMode.LIVE_READONLY, RunMode.LIVE_APPROVAL}:
        raise typer.BadParameter("recover-live-order requires live_readonly or live_approval")
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
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
def dashboard(config: Path = typer.Option(Path("configs/paper.yaml"), "--config")) -> None:
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        "src/maestro/dashboard/app.py",
        "--",
        "--config",
        str(config),
    ]
    raise typer.Exit(subprocess.call(command))


def _safety_service(config: Path) -> SafetyControlService:
    maestro_config = load_config(config)
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
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
) -> PortfolioState:
    positions: dict[str, float] = {}
    unknown_symbols: list[str] = []
    allowed = set(allowed_symbols)
    for position in account.get("positions", []):
        symbol = str(position.get("symbol") or "")
        quantity = float(position.get("quantity", 0.0))
        if not symbol or quantity == 0:
            continue
        if symbol not in allowed:
            unknown_symbols.append(symbol)
            continue
        positions[symbol] = positions.get(symbol, 0.0) + quantity
    if unknown_symbols:
        raise typer.BadParameter(
            "broker snapshot contains positions outside portfolio.allowed_symbols: "
            + ",".join(sorted(set(unknown_symbols)))
        )
    return PortfolioState(
        cash=float(account.get("cash", 0.0)),
        cash_by_currency=dict(account.get("cash_by_currency") or {}),
        positions=positions,
    )


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
            "initial_cash": 10000,
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
                "mode": "live_approval",
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
            "live_order_enabled": False,
            "live_order_dry_run": True,
            "require_reconciliation_pass": True,
            "max_live_order_notional": 100,
            "max_daily_live_notional": 300,
            "max_daily_live_order_count": 1,
            "daily_loss_limit": None,
            "allowed_order_type": "limit",
            "order_status_poll_interval_seconds": 30,
            "order_status_max_polls": 20,
            "order_status_terminal_timeout_seconds": 1800,
            "require_market_session": True,
            "market_session_timezone": "America/New_York",
            "market_session_open": "09:30",
            "market_session_close": "16:00",
            "market_session_weekdays": [0, 1, 2, 3, 4],
            "market_session_holidays": [],
            "require_broker_quote_validation": False,
            "max_broker_quote_deviation_pct": 0.05,
            "require_broker_risk_validation": False,
            "live_order_fee_buffer_pct": 0.002,
            "heartbeat_max_age_seconds": 3600,
            "scheduled_run_max_age_seconds": 86400,
        },
        "risk": {"max_single_asset_weight": 0.5, "min_cash_weight": 0.05},
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
            "account_id_env": "KIS_ACCOUNT_ID",
            "app_key_env": "KIS_APP_KEY",
            "app_secret_env": "KIS_APP_SECRET",
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
    if config.mode != RunMode.LIVE_APPROVAL or not config.execution.live_order_dry_run:
        return "fail"
    if preflight is None or preflight.status == "fail":
        return "fail"
    if not config.strategies:
        return "fail"
    return "ok" if preflight.status == "ok" else "warn"


def _minimum_live_personal_status(config: MaestroConfig, report) -> str:
    if config.mode != RunMode.LIVE_APPROVAL:
        return "fail"
    if not config.execution.live_order_enabled or config.execution.live_order_dry_run:
        return "fail"
    if _telegram_personal_status(config) != "ok":
        return "fail"
    return "ok" if not private_beta_failures(config, report) else "fail"


if __name__ == "__main__":
    app()

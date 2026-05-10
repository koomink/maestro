import json
import os
import subprocess
import sys
from pathlib import Path

import typer

from maestro.config.loader import load_config
from maestro.core.enums import RunMode
from maestro.core.ids import new_run_id
from maestro.execution.brokers.kis.service import KISReadOnlyService
from maestro.execution.live_orders import PartialFillReconciliationService
from maestro.execution.reconciliation import BrokerReconciliationService
from maestro.integrations.telegram.bot import TelegramBotAPIClient
from maestro.monitoring.audit_logger import AuditLogger
from maestro.monitoring.health import HealthService
from maestro.monitoring.logging import configure_structured_logging
from maestro.ops.preflight import private_beta_failures
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.safety.controls import SafetyControlService
from maestro.state.store import StateStore

app = typer.Typer()


@app.callback()
def main() -> None:
    """Maestro command line interface."""
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
    if maestro_config.mode != RunMode.LIVE_READONLY:
        raise typer.BadParameter("live-smoke --check kis-readonly requires mode=live_readonly")
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
    if maestro_config.mode != RunMode.LIVE_READONLY:
        raise typer.BadParameter("kis-sync requires mode=live_readonly")
    store = StateStore(maestro_config.state.sqlite_path, maestro_config.portfolio.initial_cash)
    audit = AuditLogger(maestro_config.audit.jsonl_path)
    service = KISReadOnlyService(
        maestro_config.kis,
        store,
        audit,
        instruments=maestro_config.universe.instruments,
    )
    snapshot = service.fetch_and_store_snapshot(maestro_config.portfolio.allowed_symbols)
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
    if maestro_config.mode != RunMode.LIVE_READONLY:
        raise typer.BadParameter("reconcile requires mode=live_readonly")
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


def _configured_secret_values(maestro_config) -> list[str]:
    values = []
    for env_name in (
        maestro_config.kis.app_key_env,
        maestro_config.kis.app_secret_env,
        maestro_config.kis.access_token_env,
        maestro_config.approval.telegram_bot_token_env,
    ):
        value = os.getenv(env_name)
        if value:
            values.append(value)
    return values


def _ops_alert_message(report, alert_checks) -> str:
    lines = [
        "Maestro ops alert",
        f"status: {report.status}",
        f"generated_at: {report.generated_at}",
    ]
    for check in alert_checks[:10]:
        lines.append(f"{check.name}: {check.status} {check.message}")
    return "\n".join(lines)


if __name__ == "__main__":
    app()

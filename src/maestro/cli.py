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
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.safety.controls import SafetyControlService
from maestro.state.store import StateStore

app = typer.Typer()


@app.callback()
def main() -> None:
    """Maestro command line interface."""


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
    service = KISReadOnlyService(maestro_config.kis, store, audit)
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


if __name__ == "__main__":
    app()

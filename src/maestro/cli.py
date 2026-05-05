from pathlib import Path
import subprocess
import sys

import typer

from maestro.config.loader import load_config
from maestro.orchestration.orchestrator import MaestroOrchestrator
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
        f"orders={summary.orders_created} total_value={summary.total_value:.2f} cash={summary.cash:.2f}"
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
        f"approvals={store_status['counts']['approvals']}"
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


if __name__ == "__main__":
    app()

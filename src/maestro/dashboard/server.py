import argparse
import os
from pathlib import Path
from typing import Annotated

from maestro.config.loader import load_config_with_identity
from maestro.dashboard.snapshot import build_dashboard_run_detail, build_dashboard_snapshot
from maestro.state.store import StateStore

CONFIG_ENV_VAR = "MAESTRO_CONFIG"
WEB_DIR = Path(__file__).with_name("web")


def create_app(config_path: str | Path, web_dir: str | Path | None = None):
    try:
        from fastapi import FastAPI, HTTPException, Query
        from fastapi.responses import HTMLResponse
        from fastapi.staticfiles import StaticFiles
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "FastAPI is required for the dashboard. Install with `uv sync --extra dashboard`."
        ) from exc

    resolved_config = Path(config_path)
    static_root = Path(web_dir) if web_dir else WEB_DIR
    app = FastAPI(title="Symphony Dashboard", docs_url=None, redoc_url=None)

    if (static_root / "assets").exists():
        app.mount("/assets", StaticFiles(directory=static_root / "assets"), name="assets")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        config, identity = load_config_with_identity(resolved_config)
        store = StateStore(
            config.state.sqlite_path,
            config.portfolio.initial_cash,
            config.portfolio.cash_by_currency,
            config_identity=identity,
        )
        status = store.status()
        return {
            "status": "ok",
            "read_only": True,
            "config_path": str(identity.path),
            "state_path": str(Path(config.state.sqlite_path).expanduser().resolve()),
            "audit_path": str(Path(config.audit.jsonl_path).expanduser().resolve()),
            "counts": status.get("counts", {}),
        }

    @app.get("/api/dashboard/snapshot")
    def snapshot(
        display_currency: Annotated[str, Query(pattern="^(KRW|USD|krw|usd)$")] = "KRW",
    ) -> dict[str, object]:
        return build_dashboard_snapshot(resolved_config, display_currency=display_currency)

    @app.get("/api/dashboard/runs/{run_id}")
    def run_detail(run_id: str) -> dict[str, object]:
        detail = build_dashboard_run_detail(resolved_config, run_id)
        summary = detail.get("summary", {})
        if isinstance(summary, dict) and not any(summary.values()):
            raise HTTPException(status_code=404, detail=f"Run not found: {run_id}")
        return detail

    @app.head("/{path:path}", response_class=HTMLResponse)
    @app.get("/{path:path}", response_class=HTMLResponse)
    def frontend(path: str = "") -> HTMLResponse:
        index_path = static_root / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse(_fallback_html())

    return app


def run_dashboard_server(
    config_path: str | Path,
    host: str = "127.0.0.1",
    port: int = 8503,
) -> None:
    try:
        import uvicorn
    except ModuleNotFoundError as exc:
        raise ModuleNotFoundError(
            "Uvicorn is required for the dashboard. Install with `uv sync --extra dashboard`."
        ) from exc
    uvicorn.run(create_app(config_path), host=host, port=port)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8503, type=int)
    args = parser.parse_args()
    run_dashboard_server(_resolve_config(args.config), host=args.host, port=args.port)


def _resolve_config(config_path: str | Path | None) -> Path:
    if config_path:
        return Path(config_path)
    env_config = os.getenv(CONFIG_ENV_VAR)
    if env_config:
        return Path(env_config)
    raise ValueError(f"--config is required or set {CONFIG_ENV_VAR}")


def _fallback_html() -> str:
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Symphony</title>
  </head>
  <body>
    <div id="root">
      <h1>Symphony</h1>
      <p>Dashboard frontend assets have not been built yet.</p>
    </div>
  </body>
</html>
"""


if __name__ == "__main__":
    main()

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from maestro.cli import app

pytestmark = pytest.mark.skipif(
    os.getenv("MAESTRO_RUN_LIVE_DRY_RUN_SMOKE") != "1"
    or not os.getenv("MAESTRO_LIVE_DRY_RUN_CONFIG"),
    reason=(
        "set MAESTRO_RUN_LIVE_DRY_RUN_SMOKE=1 and MAESTRO_LIVE_DRY_RUN_CONFIG "
        "to run live approval dry-run smoke checks"
    ),
)


def test_live_approval_dry_run_smoke_against_operator_config():
    config_path = Path(os.environ["MAESTRO_LIVE_DRY_RUN_CONFIG"])

    result = CliRunner().invoke(
        app,
        ["live-smoke", "--config", str(config_path), "--check", "live-dry-run"],
    )

    assert result.exit_code == 0, result.output
    assert "check=live_dry_run status=ok" in result.output

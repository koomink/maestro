import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from maestro.cli import app

pytestmark = pytest.mark.skipif(
    os.getenv("MAESTRO_RUN_KIS_LIVE_SMOKE") != "1" or not os.getenv("MAESTRO_KIS_LIVE_CONFIG"),
    reason=(
        "set MAESTRO_RUN_KIS_LIVE_SMOKE=1 and MAESTRO_KIS_LIVE_CONFIG to run "
        "real KIS network smoke checks"
    ),
)


def test_kis_readonly_live_smoke_against_operator_config():
    config_path = Path(os.environ["MAESTRO_KIS_LIVE_CONFIG"])

    result = CliRunner().invoke(
        app,
        ["live-smoke", "--config", str(config_path), "--check", "kis-readonly"],
    )

    assert result.exit_code == 0, result.output
    assert "check=kis_readonly_snapshot status=ok provider=kis" in result.output
    assert "check=broker_reconciliation status=ok" in result.output

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from maestro.cli import app

pytestmark = pytest.mark.skipif(
    os.getenv("MAESTRO_RUN_TELEGRAM_LIVE_SMOKE") != "1"
    or not os.getenv("MAESTRO_TELEGRAM_LIVE_CONFIG"),
    reason=(
        "set MAESTRO_RUN_TELEGRAM_LIVE_SMOKE=1 and MAESTRO_TELEGRAM_LIVE_CONFIG "
        "to run real Telegram network smoke checks"
    ),
)


def test_telegram_approval_live_smoke_against_operator_config():
    config_path = Path(os.environ["MAESTRO_TELEGRAM_LIVE_CONFIG"])

    result = CliRunner().invoke(
        app,
        ["live-smoke", "--config", str(config_path), "--check", "telegram-approval"],
    )

    assert result.exit_code == 0, result.output
    assert "check=telegram_approval status=ok provider=telegram mock=false" in result.output

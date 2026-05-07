from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from maestro.config.loader import load_config


def test_invalid_mode_fails(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["mode"] = "live_auto"
    config_path = tmp_path / "invalid_mode.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError):
        load_config(config_path)


def test_enabled_strategy_requires_entrypoint_format(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["strategies"][0]["entrypoint"] = "not-a-module-path"
    config_path = tmp_path / "invalid_entrypoint.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="entrypoint"):
        load_config(config_path)


def test_unknown_execution_field_fails(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["execution"]["allow_market_orders"] = False
    config_path = tmp_path / "unknown_execution.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="allow_market_orders"):
        load_config(config_path)


def test_unknown_risk_field_fails(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["risk"]["allow_short"] = False
    config_path = tmp_path / "unknown_risk.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="allow_short"):
        load_config(config_path)


def test_unknown_top_level_field_fails(tmp_path):
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["unexpected"] = True
    config_path = tmp_path / "unknown_top_level.yaml"
    config_path.write_text(yaml.safe_dump(raw))

    with pytest.raises(ValidationError, match="unexpected"):
        load_config(config_path)


def test_current_sample_configs_load():
    for path in [
        "configs/paper.yaml",
        "configs/csv_paper.yaml",
        "configs/approval_paper.yaml",
        "configs/telegram_approval_paper.yaml",
        "configs/live_readonly.yaml",
    ]:
        assert load_config(path)

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

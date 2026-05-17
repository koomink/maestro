from pathlib import Path

import yaml

from maestro.config.identity import ConfigIdentity, config_identity
from maestro.config.models import MaestroConfig


def load_config(path: str | Path) -> MaestroConfig:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    return MaestroConfig.model_validate(raw)


def load_config_with_identity(path: str | Path) -> tuple[MaestroConfig, ConfigIdentity]:
    identity = config_identity(path)
    return load_config(identity.path), identity

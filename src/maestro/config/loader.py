from pathlib import Path

from maestro.config.identity import ConfigIdentity, config_identity
from maestro.config.models import MaestroConfig
from maestro.config.strategy_account_mapping import prepare_config


def load_config(path: str | Path) -> MaestroConfig:
    return MaestroConfig.model_validate(prepare_config(path).raw)


def load_config_with_identity(path: str | Path) -> tuple[MaestroConfig, ConfigIdentity]:
    identity = config_identity(path)
    return load_config(identity.path), identity

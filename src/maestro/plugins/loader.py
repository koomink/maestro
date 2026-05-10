from importlib import import_module

from maestro.config.models import StrategyPluginConfig
from maestro.core.exceptions import PluginLoadError
from maestro.sdk import BaseStrategyPlugin

SUPPORTED_SDK_CONTRACT_VERSION = "0.9"


def load_strategy(config: StrategyPluginConfig) -> BaseStrategyPlugin:
    module_name, class_name = config.entrypoint.split(":", maxsplit=1)
    try:
        module = import_module(module_name)
        strategy_class = getattr(module, class_name)
        plugin = strategy_class()
    except Exception as exc:
        raise PluginLoadError(f"Failed to load strategy {config.id}: {exc}") from exc

    if not isinstance(plugin, BaseStrategyPlugin):
        raise PluginLoadError(f"Strategy {config.id} must implement BaseStrategyPlugin")

    manifest = plugin.manifest()
    if manifest.strategy_id != config.id:
        raise PluginLoadError(
            f"Strategy config id {config.id!r} does not match manifest id {manifest.strategy_id!r}"
        )
    if manifest.result_type != "target_allocation":
        raise PluginLoadError("Maestro v0.1 supports TargetAllocationResult only")
    if _version_tuple(manifest.sdk_contract_version) > _version_tuple(
        SUPPORTED_SDK_CONTRACT_VERSION
    ):
        raise PluginLoadError(
            "Strategy requires unsupported Maestro SDK contract version "
            f"{manifest.sdk_contract_version}"
        )
    return plugin


def _version_tuple(value: str) -> tuple[int, int]:
    parts = value.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError as exc:
        raise PluginLoadError(f"Invalid SDK contract version: {value}") from exc
    return major, minor

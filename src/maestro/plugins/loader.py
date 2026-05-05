from importlib import import_module

from maestro.config.models import StrategyPluginConfig
from maestro.core.exceptions import PluginLoadError
from maestro.sdk import BaseStrategyPlugin


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
    return plugin

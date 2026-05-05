from dataclasses import dataclass

from maestro.config.models import StrategyPluginConfig
from maestro.plugins.loader import load_strategy
from maestro.sdk import BaseStrategyPlugin, StrategyManifest


@dataclass(frozen=True)
class LoadedStrategy:
    config: StrategyPluginConfig
    plugin: BaseStrategyPlugin
    manifest: StrategyManifest


class PluginRegistry:
    def __init__(self, strategies: list[LoadedStrategy]) -> None:
        self.strategies = strategies

    @classmethod
    def from_configs(cls, configs: list[StrategyPluginConfig]) -> "PluginRegistry":
        loaded = []
        for config in configs:
            if not config.enabled:
                continue
            plugin = load_strategy(config)
            loaded.append(LoadedStrategy(config=config, plugin=plugin, manifest=plugin.manifest()))
        return cls(loaded)

    @property
    def strategy_ids(self) -> set[str]:
        return {strategy.config.id for strategy in self.strategies}

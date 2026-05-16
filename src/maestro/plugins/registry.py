from dataclasses import dataclass

from maestro.config.models import StrategyPluginConfig
from maestro.core.enums import RunMode
from maestro.plugins.loader import load_strategy
from maestro.sdk import BaseStrategyPlugin, StrategyManifest


@dataclass(frozen=True)
class LoadedStrategy:
    config: StrategyPluginConfig
    plugin: BaseStrategyPlugin
    manifest: StrategyManifest
    run_mode: RunMode


class PluginRegistry:
    def __init__(self, strategies: list[LoadedStrategy]) -> None:
        self.strategies = strategies

    @classmethod
    def from_configs(
        cls,
        configs: list[StrategyPluginConfig],
        *,
        run_mode: RunMode = RunMode.PAPER,
    ) -> "PluginRegistry":
        loaded = []
        for config in configs:
            if not config.enabled:
                continue
            plugin = load_strategy(config, run_mode=run_mode)
            loaded.append(
                LoadedStrategy(
                    config=config,
                    plugin=plugin,
                    manifest=plugin.manifest(),
                    run_mode=run_mode,
                )
            )
        return cls(loaded)

    @property
    def strategy_ids(self) -> set[str]:
        return {strategy.config.id for strategy in self.strategies}

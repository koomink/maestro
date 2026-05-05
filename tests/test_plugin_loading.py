from maestro.config.loader import load_config
from maestro.plugins.registry import PluginRegistry


def test_plugin_loading_from_entrypoint():
    config = load_config("configs/paper.yaml")

    registry = PluginRegistry.from_configs(config.strategies)

    assert registry.strategy_ids == {"sample_static_allocation"}
    assert registry.strategies[0].manifest.strategy_id == "sample_static_allocation"

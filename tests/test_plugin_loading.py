import pytest

from maestro.config.loader import load_config
from maestro.config.models import StrategyPluginConfig
from maestro.core.exceptions import PluginLoadError
from maestro.plugins.loader import load_strategy
from maestro.plugins.registry import PluginRegistry


def test_plugin_loading_from_entrypoint():
    config = load_config("configs/paper.yaml")

    registry = PluginRegistry.from_configs(config.strategies)

    assert registry.strategy_ids == {"sample_static_allocation"}
    assert registry.strategies[0].manifest.strategy_id == "sample_static_allocation"


def test_invalid_entrypoint_module_fails_clearly():
    config = StrategyPluginConfig(
        id="sample_static_allocation",
        weight=1.0,
        entrypoint="missing.module:MissingStrategy",
    )

    with pytest.raises(PluginLoadError, match="Failed to load strategy"):
        load_strategy(config)


def test_manifest_config_id_mismatch_fails():
    config = StrategyPluginConfig(
        id="wrong_id",
        weight=1.0,
        entrypoint="sample_static_allocation.strategy:SampleStaticAllocationStrategy",
    )

    with pytest.raises(PluginLoadError, match="does not match manifest id"):
        load_strategy(config)


def test_unsupported_sdk_contract_version_fails(monkeypatch):
    import sample_static_allocation.strategy as strategy_module

    original_manifest = strategy_module.SampleStaticAllocationStrategy.manifest

    def unsupported_manifest(self):
        manifest = original_manifest(self)
        return manifest.model_copy(update={"sdk_contract_version": "9.0"})

    monkeypatch.setattr(
        strategy_module.SampleStaticAllocationStrategy,
        "manifest",
        unsupported_manifest,
    )
    config = StrategyPluginConfig(
        id="sample_static_allocation",
        weight=1.0,
        entrypoint="sample_static_allocation.strategy:SampleStaticAllocationStrategy",
    )

    with pytest.raises(PluginLoadError, match="unsupported Maestro SDK contract version"):
        load_strategy(config)


def test_strategy_signal_manifest_is_public_sdk_but_not_executable_yet(monkeypatch):
    import sample_static_allocation.strategy as strategy_module

    original_manifest = strategy_module.SampleStaticAllocationStrategy.manifest

    def signal_manifest(self):
        manifest = original_manifest(self)
        return manifest.model_copy(update={"result_type": "strategy_signal"})

    monkeypatch.setattr(
        strategy_module.SampleStaticAllocationStrategy,
        "manifest",
        signal_manifest,
    )
    config = StrategyPluginConfig(
        id="sample_static_allocation",
        weight=1.0,
        entrypoint="sample_static_allocation.strategy:SampleStaticAllocationStrategy",
    )

    with pytest.raises(PluginLoadError, match="target_allocation results only"):
        load_strategy(config)

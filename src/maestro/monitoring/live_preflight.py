import os
from typing import Any

from maestro.config.models import MaestroConfig
from maestro.core.enums import BrokerProduct, OrderType
from maestro.core.exceptions import PluginLoadError
from maestro.plugins.loader import load_strategy

_LLM_PROVIDER_ENV_VARS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "glm": "ZHIPU_API_KEY",
    "glm-cn": "ZHIPU_CN_API_KEY",
    "google": "GOOGLE_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
    "qwen-cn": "DASHSCOPE_CN_API_KEY",
    "xai": "XAI_API_KEY",
}

_LLM_PROVIDERS_WITHOUT_ENV = {"ollama"}


def live_approval_preflight_findings(config: MaestroConfig) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    if config.execution.order_posture == "disabled":
        warnings.append("order_posture_disabled")
    if config.execution.order_posture == "dry_run":
        warnings.append("order_posture_dry_run")
    if not config.execution.require_reconciliation_pass:
        failures.append("reconciliation_not_required")
    if config.execution.allowed_order_type != OrderType.LIMIT:
        failures.append("non_limit_order_type")
    limits = config.execution.live_order_limits
    if limits.max_order_notional <= 0:
        failures.append("missing_per_order_notional_cap")
    if limits.max_daily_notional <= 0:
        failures.append("missing_daily_notional_cap")
    if limits.max_daily_order_count <= 0:
        warnings.append("unbounded_daily_order_count")
    if not config.approval.enabled or not config.approval.require_approval:
        failures.append("approval_not_required")
    if config.approval.provider != "telegram":
        failures.append("telegram_approval_not_configured")
    if not config.approval.telegram_allowed_chat_ids:
        failures.append("telegram_chat_missing")
    if not config.approval.whitelisted_user_ids:
        failures.append("telegram_user_whitelist_missing")
    if not config.kis.enabled:
        failures.append("kis_disabled")
    if config.kis.provider != "kis":
        failures.append("kis_provider_not_real")
    enabled_products = set(config.kis.effective_broker_products())
    if not enabled_products <= {
        BrokerProduct.KIS_DOMESTIC_STOCK,
        BrokerProduct.KIS_OVERSEAS_STOCK,
    }:
        failures.append("kis_broker_product_unsupported")

    instruments = {instrument.symbol: instrument for instrument in config.universe.instruments}
    for symbol in config.portfolio.allowed_symbols:
        instrument = instruments.get(symbol)
        if instrument is None:
            failures.append(f"missing_instrument:{symbol}")
            continue
        if (
            config.portfolio.allocation_mode != "currency_sleeves"
            and instrument.currency.value != config.portfolio.base_currency
        ):
            failures.append(f"currency_mismatch:{symbol}")
        if instrument.broker_product not in enabled_products:
            failures.append(f"broker_product_mismatch:{symbol}")
        if not symbol.startswith("CASH") and instrument.exchange_code not in {
            "NASD",
            "NYSE",
            "AMEX",
            "KRX",
        }:
            failures.append(f"unsupported_exchange:{symbol}")
    _extend_llm_env_findings(config, failures)
    failures.extend(
        f"strategy_plugin_load_failed:{strategy_id}"
        for strategy_id, _message in strategy_plugin_load_failures(config)
    )
    return failures, warnings


def strategy_plugin_load_failures(config: MaestroConfig) -> list[tuple[str, str]]:
    failures: list[tuple[str, str]] = []
    for strategy in config.strategies:
        if not strategy.enabled:
            continue
        try:
            load_strategy(strategy, run_mode=config.mode)
        except PluginLoadError as exc:
            failures.append((strategy.id, str(exc)))
    return failures


def _extend_llm_env_findings(
    config: MaestroConfig,
    failures: list[str],
) -> None:
    for strategy in config.strategies:
        if not strategy.enabled:
            continue
        strategy_config = strategy.config
        providers, invalid_agent_llms = _strategy_llm_providers(
            strategy.entrypoint,
            strategy_config,
        )
        if invalid_agent_llms:
            failures.append(f"agent_llms_invalid:{strategy.id}")
        for provider in sorted(providers):
            env_var = _LLM_PROVIDER_ENV_VARS.get(provider)
            if env_var is None:
                if provider not in _LLM_PROVIDERS_WITHOUT_ENV:
                    failures.append(f"llm_provider_unknown:{strategy.id}:{provider}")
                continue
            if not os.getenv(env_var):
                failures.append(f"llm_env_missing:{strategy.id}:{provider}:{env_var}")


def _strategy_llm_providers(
    entrypoint: str,
    strategy_config: dict[str, Any],
) -> tuple[set[str], bool]:
    providers: set[str] = set()
    provider = strategy_config.get("llm_provider")
    if isinstance(provider, str) and provider.strip():
        providers.add(provider.strip().lower())
    elif entrypoint == "tradingagents_virtuoso.strategy:TradingAgentsVirtuosoStrategy":
        providers.add("openai")

    agent_llms = strategy_config.get("agent_llms")
    if agent_llms is None:
        return providers, False
    if not isinstance(agent_llms, dict):
        return providers, True
    invalid_agent_llms = False
    for llm_config in agent_llms.values():
        if not isinstance(llm_config, dict):
            invalid_agent_llms = True
            continue
        agent_provider = llm_config.get("provider")
        if isinstance(agent_provider, str) and agent_provider.strip():
            providers.add(agent_provider.strip().lower())
    return providers, invalid_agent_llms

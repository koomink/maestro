from typing import Any

from maestro.config.models import MaestroConfig
from maestro.core.enums import BrokerProduct, OrderType
from maestro.core.exceptions import PluginLoadError
from maestro.core.symbols import is_cash_symbol
from maestro.credentials import DEFAULT_CREDENTIAL_RESOLVER
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
    if not config.execution.broker_validation.require_quote_validation:
        failures.append("quote_validation_disabled")
    if not config.execution.broker_validation.require_risk_validation:
        failures.append("risk_validation_disabled")
    if not config.execution.market_session.required:
        failures.append("market_session_not_required")
    limits = config.execution.live_order_limits
    if not limits.max_order_notional_by_currency and limits.max_order_notional <= 0:
        failures.append("missing_per_order_notional_cap")
    if not limits.max_daily_notional_by_currency and limits.max_daily_notional <= 0:
        failures.append("missing_daily_notional_cap")
    if not limits.has_daily_loss_limit():
        warnings.append("missing_daily_loss_limit")
    if limits.fee_buffer_pct <= 0:
        warnings.append("missing_fee_buffer")
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
    enabled_products = _enabled_kis_broker_products(config, failures)
    kis_routed_symbols = _kis_routed_symbols(config)

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
        if (
            not is_cash_symbol(symbol)
            and symbol in kis_routed_symbols
            and enabled_products
            and instrument.broker_product not in enabled_products
        ):
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


def _enabled_kis_broker_products(
    config: MaestroConfig,
    failures: list[str],
) -> set[BrokerProduct]:
    enabled_accounts = [account for account in config.accounts if account.enabled]
    kis_accounts = [account for account in enabled_accounts if account.broker == "kis"]
    legacy_kis_enabled = not enabled_accounts and config.kis.enabled

    if legacy_kis_enabled:
        if config.kis.provider != "kis":
            failures.append("kis_provider_not_real")
        products = set(config.kis.effective_broker_products())
    else:
        products = set()
        for account in kis_accounts:
            if (account.provider or "kis") != "kis":
                failures.append(f"kis_provider_not_real:{account.id}")
            products.update(account.effective_broker_products())

    if not enabled_accounts and not legacy_kis_enabled:
        failures.append("kis_disabled")
        return set()
    if not kis_accounts and not legacy_kis_enabled:
        return set()
    if not products <= {
        BrokerProduct.KIS_DOMESTIC_STOCK,
        BrokerProduct.KIS_OVERSEAS_STOCK,
    }:
        failures.append("kis_broker_product_unsupported")
    return products


def _kis_routed_symbols(config: MaestroConfig) -> set[str]:
    enabled_accounts = {account.id: account for account in config.accounts if account.enabled}
    if not enabled_accounts and config.kis.enabled:
        return set(config.portfolio.allowed_symbols)

    symbols: set[str] = set()
    for group in config.multi_account_contributions.values():
        for target in group.account_targets:
            account = enabled_accounts.get(target.account_id)
            if account is not None and account.broker == "kis":
                symbols.update(target.allowed_symbols)

    for strategy in config.strategies:
        if not strategy.enabled or not strategy.signal_enabled or not strategy.account_id:
            continue
        account = enabled_accounts.get(strategy.account_id)
        if account is None or account.broker != "kis":
            continue
        targets = config.account_strategy_targets.get(strategy.account_id, {})
        target = targets.get(strategy.execution_sleeve or strategy.id)
        if target is not None and target.allowed_symbols:
            symbols.update(target.allowed_symbols)
            continue
        sleeve = config.execution_sleeve_for_strategy(strategy)
        if sleeve is not None and sleeve.currency_sleeve:
            currency_sleeve = config.portfolio.currency_sleeves.get(sleeve.currency_sleeve)
            if currency_sleeve is not None:
                symbols.update(currency_sleeve.symbols)
                continue
        symbols.update(config.portfolio.allowed_symbols)
    return symbols


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
            if not DEFAULT_CREDENTIAL_RESOLVER.present(env_var):
                failures.append(f"llm_env_missing:{strategy.id}:{provider}:{env_var}")


def _strategy_llm_providers(
    entrypoint: str,
    strategy_config: dict[str, Any],
) -> tuple[set[str], bool]:
    providers: set[str] = set()
    provider = strategy_config.get("llm_provider")
    if isinstance(provider, str) and provider.strip():
        providers.add(provider.strip().lower())
    elif entrypoint == "fugue.strategy:FugueStrategy":
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

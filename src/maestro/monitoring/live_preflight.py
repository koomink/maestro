from maestro.config.models import MaestroConfig
from maestro.core.enums import BrokerProduct, OrderType


def live_approval_preflight_findings(config: MaestroConfig) -> tuple[list[str], list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    if not config.execution.live_order_enabled:
        warnings.append("live_order_disabled")
    if not config.execution.require_reconciliation_pass:
        failures.append("reconciliation_not_required")
    if config.execution.allowed_order_type != OrderType.LIMIT:
        failures.append("non_limit_order_type")
    if config.execution.max_live_order_notional <= 0:
        failures.append("missing_per_order_notional_cap")
    if config.execution.max_daily_live_notional <= 0:
        failures.append("missing_daily_notional_cap")
    if config.execution.max_daily_live_order_count <= 0:
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
    if config.kis.broker_product != BrokerProduct.KIS_OVERSEAS_STOCK:
        failures.append("kis_broker_product_not_overseas_stock")

    instruments = {instrument.symbol: instrument for instrument in config.universe.instruments}
    for symbol in config.portfolio.allowed_symbols:
        instrument = instruments.get(symbol)
        if instrument is None:
            failures.append(f"missing_instrument:{symbol}")
            continue
        if instrument.currency.value != config.portfolio.base_currency:
            failures.append(f"currency_mismatch:{symbol}")
        if instrument.broker_product != config.kis.broker_product:
            failures.append(f"broker_product_mismatch:{symbol}")
        if not symbol.startswith("CASH") and instrument.exchange_code not in {
            "NASD",
            "NYSE",
            "AMEX",
        }:
            failures.append(f"unsupported_exchange:{symbol}")
    return failures, warnings

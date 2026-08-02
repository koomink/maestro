from typing import Literal

from maestro.config.universe import UniverseConfig
from maestro.core.instruments import TradableInstrument
from maestro.state.models import PortfolioState

UnknownBrokerPositionPolicy = Literal["fail_closed", "include_readonly"]


def portfolio_state_from_broker_account(
    account: dict,
    *,
    allowed_symbols: list[str],
    universe: UniverseConfig | None = None,
    unknown_symbol_policy: UnknownBrokerPositionPolicy = "fail_closed",
    ledger_state: PortfolioState | None = None,
    allow_proxy_cash: bool = True,
) -> PortfolioState:
    positions: dict[str, float] = {}
    unknown_symbols: list[str] = []
    policy_rejected_symbols: dict[str, list[str]] = {}
    allowed = set(allowed_symbols)
    instruments = {
        instrument.symbol: instrument for instrument in (universe.instruments if universe else [])
    }
    for position in account.get("positions", []):
        symbol = str(position.get("symbol") or "")
        quantity = float(position.get("quantity", 0.0))
        if not symbol or quantity == 0:
            continue
        if symbol in allowed:
            positions[symbol] = positions.get(symbol, 0.0) + quantity
            continue
        instrument = instruments.get(symbol)
        if instrument is None:
            if unknown_symbol_policy == "include_readonly":
                positions[symbol] = positions.get(symbol, 0.0) + quantity
            else:
                unknown_symbols.append(symbol)
            continue
        rejections = _universe_policy_rejections(instrument, universe)
        if rejections:
            policy_rejected_symbols[symbol] = rejections
            continue
        positions[symbol] = positions.get(symbol, 0.0) + quantity
    if unknown_symbols:
        raise ValueError(
            "broker snapshot contains positions outside portfolio.allowed_symbols and "
            "universe.instruments: " + ",".join(sorted(set(unknown_symbols)))
        )
    if policy_rejected_symbols:
        details = [
            f"{symbol}({','.join(reasons)})"
            for symbol, reasons in sorted(policy_rejected_symbols.items())
        ]
        raise ValueError(
            "broker snapshot contains positions rejected by universe.policy: " + ";".join(details)
        )
    explicit_ledger_cash = account.get("ledger_cash_by_currency")
    if explicit_ledger_cash is None and "ledger_cash_by_currency" in account:
        if ledger_state is not None:
            cash = ledger_state.cash
            cash_by_currency = dict(ledger_state.cash_by_currency)
        elif not allow_proxy_cash:
            raise ValueError(
                "broker snapshot does not provide ledger cash; establish an account "
                "ledger baseline before composing portfolio state"
            )
        else:
            # Keep legacy callers readable while making the missing ledger
            # explicit. New orchestration paths pass the account ledger state.
            cash = float(account.get("cash", 0.0))
            cash_by_currency = dict(account.get("cash_by_currency") or {})
    else:
        cash_by_currency = dict(
            explicit_ledger_cash
            if explicit_ledger_cash is not None
            else account.get("cash_by_currency") or {}
        )
        cash = float(account.get("cash", 0.0))
    return PortfolioState(
        cash=cash,
        cash_by_currency=cash_by_currency,
        positions=positions,
    )


def _universe_policy_rejections(
    instrument: TradableInstrument,
    universe: UniverseConfig | None,
) -> list[str]:
    if universe is None:
        return []
    policy = universe.policy
    reasons = []
    if instrument.symbol in policy.denied_symbols:
        reasons.append("denied_symbol")
    if set(instrument.asset_tags) & set(policy.denied_asset_tags):
        reasons.append("denied_asset_tag")
    if instrument.asset_type not in policy.allowed_asset_types:
        reasons.append("asset_type_not_allowed")
    if instrument.region not in policy.allowed_regions:
        reasons.append("region_not_allowed")
    if instrument.currency not in policy.allowed_currencies:
        reasons.append("currency_not_allowed")
    if instrument.broker_product not in policy.allowed_broker_products:
        reasons.append("broker_product_not_allowed")
    if instrument.exchange_code not in policy.allowed_exchange_codes:
        reasons.append("exchange_not_allowed")
    return reasons

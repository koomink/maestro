from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from maestro.config.loader import load_config
from maestro.core.enums import Currency, OrderSide
from maestro.execution.base import OrderIntent
from maestro.execution.broker_capacity_lookup import (
    get_order_buying_power,
    resolve_order_currency,
)
from maestro.execution.brokers.readonly import (
    BrokerBuyingPower,
    BuyingPowerCurrencyUnavailable,
)
from maestro.integrations.telegram.handlers import TelegramOperatorCommandRouter
from maestro.monitoring.audit_logger import AuditLogger
from maestro.orchestration.orchestrator import MaestroOrchestrator
from maestro.state.store import StateStore


def test_order_currency_falls_back_to_the_instrument_then_the_base_currency(tmp_path):
    config = load_config(_config_path(tmp_path))

    assert resolve_order_currency(config, _order(currency=Currency.KRW)) == Currency.KRW
    assert resolve_order_currency(config, _order(symbol="BIL")) == Currency.USD
    assert resolve_order_currency(config, _order(symbol="NOT_IN_UNIVERSE")) == Currency(
        config.portfolio.base_currency
    )


def test_a_figure_in_another_currency_is_refused(tmp_path):
    """A dollar buy routed to a won account must not be judged against won."""
    config = load_config(_config_path(tmp_path))
    client = _FixedCapacityClient(cash_buying_power=5_000_000.0, currency="KRW")

    with pytest.raises(BuyingPowerCurrencyUnavailable) as exc_info:
        get_order_buying_power(client, config, "kis", _order(currency=Currency.USD))

    assert exc_info.value.currency == "USD"
    assert exc_info.value.available_currencies == ("KRW",)


def test_an_adapter_that_names_no_currency_is_refused(tmp_path):
    """A figure whose currency cannot be established is the original bug's input.

    Trusting it because today's adapters happen to route correctly leaves the
    same hole open for the next one.
    """
    config = load_config(_config_path(tmp_path))
    client = _FixedCapacityClient(cash_buying_power=1_000.0, currency=None)

    with pytest.raises(BuyingPowerCurrencyUnavailable) as exc_info:
        get_order_buying_power(client, config, "kis", _order(currency=Currency.USD))

    assert exc_info.value.currency == "USD"
    assert exc_info.value.available_currencies == ()


def test_approval_and_retry_paths_read_the_same_dollar_figure(monkeypatch, tmp_path):
    """The 2026-08-05 block and its failed retry were the same misreading twice.

    Both paths have to resolve the order's currency identically, or an operator
    is offered a retry the first check would have refused.
    """
    config = load_config(_config_path(tmp_path))
    client = _MultiCurrencyClient({"KRW": 2.0, "USD": 26_072.0})
    service = SimpleNamespace(client=client)
    for module in (
        "maestro.orchestration.orchestrator",
        "maestro.integrations.telegram.handlers",
    ):
        monkeypatch.setattr(
            f"{module}.build_broker_readonly_service",
            lambda *args, **kwargs: service,
        )
    order = _order(symbol="BIL", price=91.43, quantity=102, currency=Currency.USD)
    orchestrator = MaestroOrchestrator(config)
    router = TelegramOperatorCommandRouter(
        config=config,
        store=StateStore(config.state.sqlite_path, config.portfolio.initial_cash),
        audit=AuditLogger(config.audit.jsonl_path),
        client=SimpleNamespace(),
    )

    approval = orchestrator._lookup_order_capacity(order)
    retry = router._lookup_retry_capacity(config, order)

    assert approval.cash_buying_power == 26_072.0
    assert retry.cash_buying_power == approval.cash_buying_power
    assert retry.currency == approval.currency == "USD"


def _order(
    *,
    symbol: str = "BIL",
    quantity: float = 102,
    price: float = 91.43,
    currency: Currency | None = None,
) -> OrderIntent:
    return OrderIntent(
        order_id="ord_capacity",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=quantity,
        price=price,
        notional=quantity * price,
        account_id="toss_brokerage",
        currency=currency,
    )


def _config_path(tmp_path) -> Path:
    raw = yaml.safe_load(Path("configs/paper.yaml").read_text())
    raw["state"]["sqlite_path"] = str(tmp_path / "state.db")
    raw["audit"]["jsonl_path"] = str(tmp_path / "audit.jsonl")
    raw["portfolio"]["allowed_symbols"] = ["CASH", "BIL"]
    raw["strategies"] = []
    raw["universe"] = {
        "instruments": [
            {
                "symbol": "BIL",
                "asset_type": "us_etf",
                "region": "US",
                "currency": "USD",
                "broker": "toss",
                "broker_product": "kis_overseas_stock",
                "broker_symbol": "BIL",
                "exchange_code": "NYSE",
                "quantity_step": 1,
                "price_tick": 0.01,
            }
        ]
    }
    config_path = tmp_path / "capacity_lookup.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    return config_path


class _FixedCapacityClient:
    def __init__(self, *, cash_buying_power: float, currency: str | None) -> None:
        self.cash_buying_power = cash_buying_power
        self.currency = currency

    def get_buying_power(self, symbol=None, order_price=None, currency=None):
        del currency
        return BrokerBuyingPower(
            symbol=symbol,
            order_price=order_price,
            cash_buying_power=self.cash_buying_power,
            currency=self.currency,
            source="test",
        )


class _MultiCurrencyClient:
    """A Toss-shaped account: one holder, one pot per currency."""

    def __init__(self, buying_power_by_currency: dict[str, float]) -> None:
        self.buying_power_by_currency = buying_power_by_currency

    def get_buying_power(self, symbol=None, order_price=None, currency=None):
        if currency not in self.buying_power_by_currency:
            raise BuyingPowerCurrencyUnavailable(currency, self.buying_power_by_currency)
        return BrokerBuyingPower(
            symbol=symbol,
            order_price=order_price,
            cash_buying_power=self.buying_power_by_currency[currency],
            currency=currency,
            source="test",
        )

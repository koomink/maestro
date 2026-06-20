from maestro.config.models import KISConfig
from maestro.core.enums import BrokerProduct
from maestro.core.instruments import TradableInstrument
from maestro.execution.brokers.kis.client import KISReadOnlyClient
from maestro.execution.brokers.kis.domestic_live_order import KISRestDomesticStockLiveOrderClient
from maestro.execution.brokers.kis.domestic_readonly import KISRestDomesticStockReadOnlyClient
from maestro.execution.brokers.kis.overseas_live_order import KISRestOverseasStockLiveOrderClient
from maestro.execution.brokers.kis.overseas_readonly import KISRestOverseasStockReadOnlyClient
from maestro.execution.live_order_ports import LiveOrderClient


def build_kis_rest_readonly_client(
    config: KISConfig,
    instruments: list[TradableInstrument] | None = None,
    broker_product: BrokerProduct | None = None,
) -> KISReadOnlyClient:
    product = broker_product or _single_broker_product(config)
    if product == BrokerProduct.KIS_DOMESTIC_STOCK:
        return KISRestDomesticStockReadOnlyClient(config, instruments=instruments)
    if product == BrokerProduct.KIS_OVERSEAS_STOCK:
        return KISRestOverseasStockReadOnlyClient(config, instruments=instruments)
    raise ValueError(f"Unsupported KIS broker product: {product}")


def build_kis_rest_live_order_client(
    config: KISConfig,
    instruments: list[TradableInstrument] | None = None,
    broker_product: BrokerProduct | None = None,
) -> LiveOrderClient:
    product = broker_product or _single_broker_product(config)
    if product == BrokerProduct.KIS_DOMESTIC_STOCK:
        return KISRestDomesticStockLiveOrderClient(config, instruments=instruments)
    if product == BrokerProduct.KIS_OVERSEAS_STOCK:
        return KISRestOverseasStockLiveOrderClient(config, instruments=instruments)
    raise ValueError(f"Unsupported KIS broker product: {product}")


def _single_broker_product(config: KISConfig) -> BrokerProduct:
    products = config.effective_broker_products()
    if len(products) != 1:
        raise ValueError("KIS REST client requires exactly one broker product")
    return products[0]


KISRestReadOnlyClient = KISRestDomesticStockReadOnlyClient
KISRestLiveOrderClient = KISRestDomesticStockLiveOrderClient

__all__ = [
    "KISRestDomesticStockLiveOrderClient",
    "KISRestDomesticStockReadOnlyClient",
    "KISRestLiveOrderClient",
    "KISRestOverseasStockLiveOrderClient",
    "KISRestOverseasStockReadOnlyClient",
    "KISRestReadOnlyClient",
    "build_kis_rest_live_order_client",
    "build_kis_rest_readonly_client",
]

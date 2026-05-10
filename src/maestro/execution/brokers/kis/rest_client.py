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
) -> KISReadOnlyClient:
    if config.broker_product == BrokerProduct.KIS_DOMESTIC_STOCK:
        return KISRestDomesticStockReadOnlyClient(config, instruments=instruments)
    if config.broker_product == BrokerProduct.KIS_OVERSEAS_STOCK:
        return KISRestOverseasStockReadOnlyClient(config, instruments=instruments)
    raise ValueError(f"Unsupported KIS broker product: {config.broker_product}")


def build_kis_rest_live_order_client(
    config: KISConfig,
    instruments: list[TradableInstrument] | None = None,
) -> LiveOrderClient:
    if config.broker_product == BrokerProduct.KIS_DOMESTIC_STOCK:
        return KISRestDomesticStockLiveOrderClient(config, instruments=instruments)
    if config.broker_product == BrokerProduct.KIS_OVERSEAS_STOCK:
        return KISRestOverseasStockLiveOrderClient(config, instruments=instruments)
    raise ValueError(f"Unsupported KIS broker product: {config.broker_product}")


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

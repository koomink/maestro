class BrokerOrderRejectedError(RuntimeError):
    """The broker returned a definitive rejection before accepting an order."""

    def __init__(self, broker: str, code: str, message: str) -> None:
        super().__init__(f"{broker} order rejected: {code} {message}")
        self.broker = broker
        self.code = code
        self.message = message


__all__ = ["BrokerOrderRejectedError"]

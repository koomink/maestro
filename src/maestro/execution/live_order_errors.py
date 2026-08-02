from typing import Any


class BrokerOrderRejectedError(RuntimeError):
    """The broker returned a definitive rejection before accepting an order."""

    def __init__(
        self,
        broker: str,
        code: str,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{broker} order rejected: {code} {message}")
        self.broker = broker
        self.code = code
        self.message = message
        self.status_code = status_code
        self.request_id = request_id
        self.data = data or {}


__all__ = ["BrokerOrderRejectedError"]

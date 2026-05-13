from collections.abc import Callable, Sequence
from typing import Any

from maestro.sdk.schemas import DataBundle, DataRequest, StrategyContext


class StrategyRuntime:
    """Runtime services exposed to SDK 1.1 strategy plugins."""

    def __init__(
        self,
        fetch_data: Callable[[list[DataRequest]], DataBundle],
        *,
        context: StrategyContext,
    ) -> None:
        self._fetch_data = fetch_data
        self.context = context
        self.requests: list[DataRequest] = []
        self.bundles: list[DataBundle] = []
        self.errors: list[dict[str, Any]] = []

    def get_data(self, requests: DataRequest | Sequence[DataRequest]) -> DataBundle:
        normalized = [requests] if isinstance(requests, DataRequest) else list(requests)
        self.requests.extend(normalized)
        try:
            bundle = self._fetch_data(normalized)
        except Exception as exc:
            self.errors.append(
                {
                    "requests": [request.model_dump(mode="json") for request in normalized],
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                }
            )
            raise
        self.bundles.append(bundle)
        return bundle

    def audit_payload(self) -> dict[str, Any]:
        return {
            "requests": [request.model_dump(mode="json") for request in self.requests],
            "bundles": [
                {
                    "source": bundle.source,
                    "generated_at": bundle.generated_at.isoformat()
                    if hasattr(bundle.generated_at, "isoformat")
                    else str(bundle.generated_at),
                    "symbols": sorted(str(symbol) for symbol in bundle.data.keys()),
                    "request_count": len(bundle.requests),
                }
                for bundle in self.bundles
            ],
            "errors": list(self.errors),
        }

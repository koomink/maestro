from collections import defaultdict
from typing import Any

from pydantic import BaseModel

from maestro.core.clock import utc_now
from maestro.core.enums import RunMode
from maestro.datahub.base import BaseDataProvider
from maestro.datahub.errors import (
    NoProviderError,
    ProviderUnavailableError,
    StaleDataError,
    UnsupportedDataTypeError,
)
from maestro.datahub.registry import DataHubRegistry, ProviderRegistration
from maestro.datahub.schemas import SUPPORTED_DATA_TYPES
from maestro.sdk import DataBundle, DataRequest


class DataHubRouter(BaseDataProvider):
    def __init__(
        self,
        registry: DataHubRegistry,
        *,
        run_mode: RunMode | None = None,
        allow_stale: bool = True,
    ) -> None:
        self.registry = registry
        self.run_mode = run_mode
        self.allow_stale = allow_stale

    def get_data(self, requests: list[DataRequest]) -> DataBundle:
        routed: dict[ProviderRegistration, list[DataRequest]] = defaultdict(list)
        for request in requests:
            routed[self._select_provider(request)].append(request)

        data: dict[str, Any] = {}
        sources = []
        for registration, provider_requests in routed.items():
            bundle = registration.provider.get_data(provider_requests)
            sources.append(bundle.source)
            for symbol, payload in bundle.data.items():
                normalized = self._normalize_payload(payload)
                if not self.allow_stale and self._is_stale(normalized):
                    raise StaleDataError(f"Stale data returned for symbol: {symbol}")
                data[symbol] = self._merge_payloads(data.get(symbol), normalized)

        source = sources[0] if sources and len(set(sources)) == 1 else "router"
        return DataBundle(requests=requests, data=data, generated_at=utc_now(), source=source)

    def _select_provider(self, request: DataRequest) -> ProviderRegistration:
        if request.data_type not in SUPPORTED_DATA_TYPES:
            raise UnsupportedDataTypeError(f"Unsupported DataHub data_type: {request.data_type}")

        registrations = self.registry.registrations_for(request, self.run_mode)
        if not registrations:
            raise NoProviderError(
                f"No DataHub provider for symbol={request.symbol} "
                f"asset_type={request.asset_type} data_type={request.data_type}"
            )

        for registration in sorted(registrations, key=lambda item: item.priority):
            if registration.available:
                return registration
        raise ProviderUnavailableError(
            f"DataHub providers for data_type={request.data_type} are unavailable"
        )

    def _normalize_payload(self, payload: Any) -> Any:
        if isinstance(payload, BaseModel):
            return payload.model_dump(mode="json")
        return payload

    def _merge_payloads(self, existing: Any, incoming: Any) -> Any:
        if isinstance(existing, dict) and isinstance(incoming, dict):
            return {**existing, **incoming}
        return incoming if existing is None else existing

    def _is_stale(self, payload: Any) -> bool:
        return isinstance(payload, dict) and bool(payload.get("is_stale"))

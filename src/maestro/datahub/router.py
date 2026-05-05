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
        data: dict[str, Any] = {}
        sources = []
        for request in requests:
            bundle = self._get_data_from_provider(request)
            sources.append(bundle.source)
            for symbol, payload in bundle.data.items():
                normalized = self._normalize_payload(payload)
                if not self.allow_stale and self._is_stale(normalized):
                    raise StaleDataError(f"Stale data returned for symbol: {symbol}")
                data[symbol] = self._merge_payloads(data.get(symbol), normalized)

        source = sources[0] if sources and len(set(sources)) == 1 else "router"
        return DataBundle(requests=requests, data=data, generated_at=utc_now(), source=source)

    def _get_data_from_provider(self, request: DataRequest) -> DataBundle:
        failures = []
        for registration in self._matching_providers(request):
            try:
                return registration.provider.get_data([request])
            except ProviderUnavailableError as exc:
                failures.append(f"{registration.name}: {exc}")
        detail = "; ".join(failures) if failures else "all matching providers are unavailable"
        raise ProviderUnavailableError(
            f"DataHub providers for data_type={request.data_type} are unavailable: {detail}"
        )

    def _matching_providers(self, request: DataRequest) -> list[ProviderRegistration]:
        if request.data_type not in SUPPORTED_DATA_TYPES:
            raise UnsupportedDataTypeError(f"Unsupported DataHub data_type: {request.data_type}")

        registrations = self.registry.registrations_for(request, self.run_mode)
        if not registrations:
            raise NoProviderError(
                f"No DataHub provider for symbol={request.symbol} "
                f"asset_type={request.asset_type} data_type={request.data_type}"
            )

        available = [registration for registration in registrations if registration.available]
        if not available:
            raise ProviderUnavailableError(
                f"DataHub providers for data_type={request.data_type} are unavailable"
            )
        return sorted(available, key=lambda item: item.priority)

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

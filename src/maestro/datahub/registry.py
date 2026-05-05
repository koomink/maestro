from dataclasses import dataclass

from maestro.core.enums import AssetType, RunMode
from maestro.datahub.base import BaseDataProvider
from maestro.sdk import DataRequest


@dataclass(frozen=True)
class ProviderRegistration:
    name: str
    provider: BaseDataProvider
    data_types: frozenset[str]
    symbols: frozenset[str] | None = None
    asset_types: frozenset[AssetType] | None = None
    run_modes: frozenset[RunMode] | None = None
    available: bool = True

    def matches(self, request: DataRequest, run_mode: RunMode | None = None) -> bool:
        return (
            request.data_type in self.data_types
            and (self.symbols is None or request.symbol in self.symbols)
            and (self.asset_types is None or request.asset_type in self.asset_types)
            and (run_mode is None or self.run_modes is None or run_mode in self.run_modes)
        )


class DataHubRegistry:
    def __init__(self) -> None:
        self._registrations: list[ProviderRegistration] = []

    def register(
        self,
        name: str,
        provider: BaseDataProvider,
        data_types: set[str],
        *,
        symbols: set[str] | None = None,
        asset_types: set[AssetType] | None = None,
        run_modes: set[RunMode] | None = None,
        available: bool = True,
    ) -> None:
        self._registrations.append(
            ProviderRegistration(
                name=name,
                provider=provider,
                data_types=frozenset(data_types),
                symbols=frozenset(symbols) if symbols is not None else None,
                asset_types=frozenset(asset_types) if asset_types is not None else None,
                run_modes=frozenset(run_modes) if run_modes is not None else None,
                available=available,
            )
        )

    def registrations_for(
        self, request: DataRequest, run_mode: RunMode | None = None
    ) -> list[ProviderRegistration]:
        return [
            registration
            for registration in self._registrations
            if registration.matches(request, run_mode)
        ]

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from maestro.monitoring.health_models import HealthCheck


class HealthCheckProvider(Protocol):
    name: str

    def run(self) -> HealthCheck:
        raise NotImplementedError


@dataclass(frozen=True)
class FunctionHealthCheckProvider:
    name: str
    check_fn: Callable[[], HealthCheck]

    def run(self) -> HealthCheck:
        return self.check_fn()

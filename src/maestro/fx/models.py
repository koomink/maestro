from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class FXRateSnapshot(BaseModel):
    source: str
    as_of: datetime
    fetched_at: datetime
    max_age_seconds: int = Field(gt=0)
    rates: dict[str, float]
    metadata: dict[str, Any] = Field(default_factory=dict)

    def as_event_payload(self) -> dict[str, Any]:
        payload = {
            "source": self.source,
            "as_of": self.as_of.isoformat(),
            "fetched_at": self.fetched_at.isoformat(),
            "max_age_seconds": self.max_age_seconds,
            "rates": dict(self.rates),
        }
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


class FXRefreshResult(BaseModel):
    status: str
    source: str | None = None
    as_of: str | None = None
    rates: dict[str, float] = Field(default_factory=dict)


__all__ = ["FXRateSnapshot", "FXRefreshResult"]

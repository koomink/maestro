from typing import Literal

from pydantic import Field, model_validator

from maestro.config.base import StrictConfigModel


class FXConfig(StrictConfigModel):
    enabled: bool = True
    provider: Literal["exchangerate_api"] = "exchangerate_api"
    api_key_env: str = "EXCHANGERATE_API_KEY"
    pairs: list[str] = Field(default_factory=lambda: ["USD/KRW"])
    stale_after_seconds: int = Field(default=14400, gt=0)
    refresh_interval_seconds: int = Field(default=3600, gt=0)
    timeout_seconds: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def validate_v1_pairs(self) -> "FXConfig":
        if self.pairs != ["USD/KRW"]:
            raise ValueError("FX v1 supports only pairs: USD/KRW")
        return self


__all__ = ["FXConfig"]

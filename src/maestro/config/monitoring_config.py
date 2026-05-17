from pydantic import Field

from maestro.config.base import StrictConfigModel


class MonitoringConfig(StrictConfigModel):
    heartbeat_max_age_seconds: int = Field(default=0, ge=0)
    scheduled_run_max_age_seconds: int = Field(default=0, ge=0)


__all__ = ["MonitoringConfig"]

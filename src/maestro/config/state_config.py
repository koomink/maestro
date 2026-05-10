from maestro.config.base import StrictConfigModel


class StateConfig(StrictConfigModel):
    sqlite_path: str


class AuditConfig(StrictConfigModel):
    jsonl_path: str


__all__ = ["AuditConfig", "StateConfig"]

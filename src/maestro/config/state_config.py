from maestro.config.base import StrictConfigModel


class StateConfig(StrictConfigModel):
    sqlite_path: str
    identity_group: str | None = None


class AuditConfig(StrictConfigModel):
    jsonl_path: str


__all__ = ["AuditConfig", "StateConfig"]

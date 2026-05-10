from pydantic import Field, field_validator

from maestro.config.base import StrictConfigModel


class ApprovalConfig(StrictConfigModel):
    enabled: bool = False
    provider: str = "console"
    require_approval: bool = False
    default_decision: str = "approved"
    timeout_seconds: int = Field(default=300, gt=0)
    whitelisted_user_ids: list[int] = Field(default_factory=list)
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_allowed_chat_ids: list[int] = Field(default_factory=list)
    telegram_poll_interval_seconds: float = Field(default=1.0, ge=0)

    @field_validator("default_decision")
    @classmethod
    def validate_default_decision(cls, value: str) -> str:
        if value not in {"approved", "rejected", "expired"}:
            raise ValueError("default_decision must be approved, rejected, or expired")
        return value


__all__ = ["ApprovalConfig"]

import os

from pydantic import Field, field_validator, model_validator

from maestro.config.base import StrictConfigModel


class ApprovalConfig(StrictConfigModel):
    enabled: bool = False
    provider: str = "console"
    require_approval: bool = False
    default_decision: str = "approved"
    timeout_seconds: int = Field(default=300, gt=0)
    signal_max_age_seconds: int = Field(default=3600, gt=0)
    whitelisted_user_ids: list[int] = Field(default_factory=list)
    telegram_bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_allowed_chat_ids: list[int] = Field(default_factory=list)
    telegram_poll_interval_seconds: float = Field(default=1.0, ge=0)
    telegram_reminder_seconds: list[int] = Field(default_factory=list)

    @field_validator("default_decision")
    @classmethod
    def validate_default_decision(cls, value: str) -> str:
        if value not in {"approved", "rejected", "expired"}:
            raise ValueError("default_decision must be approved, rejected, or expired")
        return value

    @field_validator("telegram_reminder_seconds")
    @classmethod
    def validate_telegram_reminder_seconds(cls, value: list[int]) -> list[int]:
        if any(item <= 0 for item in value):
            raise ValueError("telegram_reminder_seconds values must be positive")
        if value != sorted(set(value)):
            raise ValueError("telegram_reminder_seconds must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_reminders_before_timeout(self) -> "ApprovalConfig":
        if any(item >= self.timeout_seconds for item in self.telegram_reminder_seconds):
            raise ValueError("telegram reminders must occur before timeout_seconds")
        return self

    @model_validator(mode="after")
    def apply_maestro_telegram_env_defaults(self) -> "ApprovalConfig":
        if self.provider != "telegram":
            return self
        if not self.telegram_allowed_chat_ids:
            self.telegram_allowed_chat_ids = _parse_env_int_list(
                "MAESTRO_TELEGRAM_ALLOWED_CHAT_IDS"
            )
        if not self.whitelisted_user_ids:
            self.whitelisted_user_ids = _parse_env_int_list(
                "MAESTRO_TELEGRAM_WHITELISTED_USER_IDS"
            )
        return self


def _parse_env_int_list(name: str) -> list[int]:
    raw = os.getenv(name, "")
    values = [item.strip() for item in raw.split(",")]
    return [int(item) for item in values if item]


__all__ = ["ApprovalConfig"]

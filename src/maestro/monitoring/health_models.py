from typing import Any

from pydantic import BaseModel, Field

from maestro.core.time_display import add_operator_time_details, format_operator_time


class HealthCheck(BaseModel):
    name: str
    status: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class HealthReport(BaseModel):
    status: str
    generated_at: str
    checks: list[HealthCheck]

    def text_lines(self, operator_timezone: str | None = None) -> list[str]:
        generated_at = self.generated_at
        if operator_timezone:
            generated_at = format_operator_time(self.generated_at, operator_timezone)
        lines = [f"status={self.status} generated_at={generated_at}"]
        for check in self.checks:
            details = check.details
            if operator_timezone:
                details = add_operator_time_details(details, operator_timezone)
            detail_text = " ".join(f"{key}={value}" for key, value in details.items())
            suffix = f" {detail_text}" if detail_text else ""
            lines.append(
                f"check={check.name} status={check.status} message={check.message}{suffix}"
            )
        return lines


def overall_health_status(checks: list[HealthCheck]) -> str:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "ok"

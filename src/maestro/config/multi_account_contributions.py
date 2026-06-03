from typing import Literal

from pydantic import Field, model_validator

from maestro.config.base import StrictConfigModel
from maestro.config.execution import OrderGenerationMode


class MultiAccountContributionTargetConfig(StrictConfigModel):
    account_id: str
    execution_sleeve: str
    allowed_symbols: list[str] = Field(min_length=1)
    monthly_budget: float = Field(default=0.0, ge=0.0)
    min_monthly_budget: float = Field(default=0.0, ge=0.0)
    max_monthly_budget: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def validate_budget(self) -> "MultiAccountContributionTargetConfig":
        if self.monthly_budget <= 0 and self.min_monthly_budget <= 0:
            raise ValueError(
                "multi_account_contributions account target requires monthly_budget "
                "or min_monthly_budget"
            )
        if self.monthly_budget > 0:
            if self.min_monthly_budget == 0:
                self.min_monthly_budget = self.monthly_budget
            if self.max_monthly_budget == 0:
                self.max_monthly_budget = self.monthly_budget
        if self.max_monthly_budget <= 0:
            self.max_monthly_budget = self.min_monthly_budget
        if self.min_monthly_budget > self.max_monthly_budget:
            raise ValueError(
                "multi_account_contributions min_monthly_budget must be less than "
                "or equal to max_monthly_budget"
            )
        if self.monthly_budget and self.monthly_budget != self.max_monthly_budget:
            raise ValueError(
                "multi_account_contributions fixed monthly_budget must match max_monthly_budget"
            )
        return self


class MultiAccountContributionGroupConfig(StrictConfigModel):
    strategy_id: str
    allocation_basis: Literal["aggregate_current_holdings"] = "aggregate_current_holdings"
    order_generation_mode: OrderGenerationMode = "buy_only_contribution"
    account_targets: list[MultiAccountContributionTargetConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_mode(self) -> "MultiAccountContributionGroupConfig":
        if self.order_generation_mode != "buy_only_contribution":
            raise ValueError(
                "multi_account_contributions order_generation_mode must be buy_only_contribution"
            )
        seen = set()
        duplicates = set()
        for target in self.account_targets:
            key = (target.account_id, target.execution_sleeve)
            if key in seen:
                duplicates.add(key)
            seen.add(key)
        if duplicates:
            formatted = ", ".join(
                f"{account_id}/{execution_sleeve}"
                for account_id, execution_sleeve in sorted(duplicates)
            )
            raise ValueError(
                "multi_account_contributions account targets must be unique: " + formatted
            )
        return self


__all__ = [
    "MultiAccountContributionGroupConfig",
    "MultiAccountContributionTargetConfig",
]

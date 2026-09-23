from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from trailforge.domain.enums import (
    EligibilityOutcome,
    HealthRuleAction,
    ReviewDecision,
    TrainingMetric,
    TrainingType,
)
from trailforge.schemas.common import ORMModel, TimestampedResponse, clean_text


class TrainingRequirementRule(BaseModel):
    training_type: Literal[TrainingType.ENDURANCE, TrainingType.LOADED_WALK]
    window_days: int = Field(ge=1, le=365)
    metric: TrainingMetric
    minimum: float = Field(gt=0, le=1_000_000)


class HealthRestrictionRule(BaseModel):
    restriction_name: str = Field(min_length=1, max_length=120)
    action: HealthRuleAction

    @field_validator("restriction_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return clean_text(value)


class EligibilityRules(BaseModel):
    training_requirements: list[TrainingRequirementRule] = Field(
        default_factory=list, max_length=10
    )
    minimum_outdoor_level: int | None = Field(default=None, ge=1, le=5)
    experience_recency_days: int | None = Field(default=None, ge=1, le=3650)
    health_restriction_rules: list[HealthRestrictionRule] = Field(
        default_factory=list, max_length=50
    )
    require_emergency_contact: bool = False

    @model_validator(mode="after")
    def validate_rule_combinations(self) -> EligibilityRules:
        training_keys = [
            (item.training_type, item.window_days, item.metric)
            for item in self.training_requirements
        ]
        if len(training_keys) != len(set(training_keys)):
            raise ValueError("duplicate training requirements for the same type, window and metric")
        names = [item.restriction_name.lower() for item in self.health_restriction_rules]
        if len(names) != len(set(names)):
            raise ValueError("health restriction rules must not repeat the same restriction name")
        return self

    def is_empty(self) -> bool:
        return not (
            self.training_requirements
            or self.minimum_outdoor_level is not None
            or self.health_restriction_rules
            or self.require_emergency_contact
        )


class EligibilityPolicyUpsert(BaseModel):
    rules: EligibilityRules
    change_note: str = Field(default="", max_length=2000)
    expected_version: int | None = Field(default=None, ge=1)


class EligibilityPolicyResponse(TimestampedResponse):
    expedition_id: int
    version: int
    is_active: bool
    rules: EligibilityRules
    change_note: str
    created_by: int


class EligibilityReason(BaseModel):
    code: str
    passed: bool
    effect: Literal["deny", "review", "info"]
    message: str
    expected: object = None
    actual: object = None


class EligibilityPrecheckRequest(BaseModel):
    user_id: int = Field(gt=0)


class EligibilityEvaluation(BaseModel):
    outcome: EligibilityOutcome
    policy_id: int | None = None
    policy_version: int | None = None
    rules: EligibilityRules | None = None
    evaluated_at: datetime
    facts: dict
    reasons: list[EligibilityReason]


class EligibilityDecisionResponse(ORMModel):
    id: int
    expedition_id: int
    user_id: int
    registration_id: int | None
    policy_id: int | None
    policy_version: int
    attempt: int
    outcome: EligibilityOutcome
    rules: EligibilityRules | None = Field(default=None, validation_alias="rules_json")
    input_facts: dict
    reasons: list[EligibilityReason] = Field(
        default_factory=list, validation_alias="reasons_json"
    )
    decided_at: datetime
    reviewed_by: int | None
    reviewed_at: datetime | None
    review_decision: ReviewDecision | None
    review_reason: str | None
    review_facts: dict
    version: int


class EligibilityReviewRequest(BaseModel):
    decision: ReviewDecision
    reason: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        return clean_text(value)

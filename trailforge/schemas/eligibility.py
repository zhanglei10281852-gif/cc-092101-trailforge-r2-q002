from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from trailforge.domain.enums import (
    EligibilityDecision,
    EligibilityRuleOutcome,
    ReviewDecision,
)
from trailforge.schemas.activities import RegistrationResponse
from trailforge.schemas.common import ORMModel, TimestampedResponse, clean_text


def normalize_restriction_name(value: str) -> str:
    """Canonical form used to compare health restriction names in policies."""
    return " ".join(value.strip().lower().split())


class EligibilityPolicyUpsert(BaseModel):
    """Creates a new active policy version for an expedition."""

    actor_id: int = Field(gt=0)
    note: str = Field(default="", max_length=2000)
    training_window_days: int | None = Field(default=None, ge=1, le=365)
    min_endurance_minutes: int | None = Field(default=None, ge=0, le=100000)
    min_loaded_sessions: int | None = Field(default=None, ge=0, le=1000)
    min_completed_expeditions: int | None = Field(default=None, ge=0, le=1000)
    experience_window_days: int | None = Field(default=None, ge=1, le=3650)
    blocked_restrictions: list[str] = Field(default_factory=list, max_length=50)
    review_restrictions: list[str] = Field(default_factory=list, max_length=50)
    min_emergency_contacts: int | None = Field(default=None, ge=0, le=10)

    @field_validator("blocked_restrictions", "review_restrictions")
    @classmethod
    def normalize_names(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            name = normalize_restriction_name(item)
            if not name:
                raise ValueError("restriction names must not be blank")
            if name not in normalized:
                normalized.append(name)
        return normalized

    @model_validator(mode="after")
    def validate_rule_combination(self) -> EligibilityPolicyUpsert:
        uses_training_window = (
            self.min_endurance_minutes is not None or self.min_loaded_sessions is not None
        )
        if uses_training_window and self.training_window_days is None:
            raise ValueError("training_window_days is required when training thresholds are set")
        if self.experience_window_days is not None and self.min_completed_expeditions is None:
            raise ValueError("experience_window_days requires min_completed_expeditions")
        overlap = set(self.blocked_restrictions) & set(self.review_restrictions)
        if overlap:
            raise ValueError(
                "a restriction cannot be both blocked and review-only",
            )
        has_rule = any(
            [
                uses_training_window,
                self.min_completed_expeditions is not None,
                self.blocked_restrictions,
                self.review_restrictions,
                self.min_emergency_contacts is not None,
            ]
        )
        if not has_rule:
            raise ValueError("policy requires at least one eligibility rule")
        return self


class EligibilityPolicyResponse(TimestampedResponse):
    expedition_id: int
    version: int
    is_active: bool
    training_window_days: int | None
    min_endurance_minutes: int | None
    min_loaded_sessions: int | None
    min_completed_expeditions: int | None
    experience_window_days: int | None
    blocked_restrictions: list[str]
    review_restrictions: list[str]
    min_emergency_contacts: int | None
    note: str
    created_by: int | None


class EligibilityReason(BaseModel):
    rule: str
    outcome: EligibilityRuleOutcome
    message: str
    expected: Any = None
    actual: Any = None


class EligibilityEvaluationResponse(ORMModel):
    id: int
    expedition_id: int
    registration_id: int | None
    user_id: int
    policy_id: int | None
    policy_version: int | None
    decision: EligibilityDecision
    facts: dict[str, Any]
    reasons: list[EligibilityReason]
    created_at: datetime


class EligibilityPrecheckRequest(BaseModel):
    user_id: int = Field(gt=0)


class EligibilityPrecheckResponse(BaseModel):
    expedition_id: int
    user_id: int
    policy_id: int | None
    policy_version: int | None
    decision: EligibilityDecision
    reasons: list[EligibilityReason]
    facts: dict[str, Any]
    message: str


class EligibilityReviewCreate(BaseModel):
    actor_id: int = Field(gt=0)
    decision: ReviewDecision
    reason: str = Field(min_length=1, max_length=2000)
    expected_version: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def reason_not_blank(cls, value: str) -> str:
        return clean_text(value)


class PendingReviewItem(BaseModel):
    registration: RegistrationResponse
    evaluation: EligibilityEvaluationResponse | None


class PendingReviewList(BaseModel):
    expedition_id: int
    items: list[PendingReviewItem]

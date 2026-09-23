from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from trailforge.api.dependencies import get_session
from trailforge.schemas.activities import RegistrationResponse
from trailforge.schemas.eligibility import (
    EligibilityEvaluationResponse,
    EligibilityPolicyResponse,
    EligibilityPolicyUpsert,
    EligibilityPrecheckRequest,
    EligibilityPrecheckResponse,
    EligibilityReviewCreate,
    PendingReviewList,
)
from trailforge.services.eligibility import EligibilityService

router = APIRouter(prefix="/expeditions", tags=["eligibility"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.put(
    "/{expedition_id}/eligibility-policy",
    response_model=EligibilityPolicyResponse,
    status_code=status.HTTP_201_CREATED,
)
def upsert_eligibility_policy(
    expedition_id: int,
    data: EligibilityPolicyUpsert,
    session: SessionDep,
) -> EligibilityPolicyResponse:
    return EligibilityService(session).upsert_policy(expedition_id, data)


@router.get("/{expedition_id}/eligibility-policy", response_model=EligibilityPolicyResponse)
def get_active_eligibility_policy(
    expedition_id: int,
    session: SessionDep,
) -> EligibilityPolicyResponse:
    return EligibilityService(session).get_active_policy(expedition_id)


@router.get(
    "/{expedition_id}/eligibility-policies",
    response_model=list[EligibilityPolicyResponse],
)
def list_eligibility_policies(
    expedition_id: int,
    session: SessionDep,
) -> list[EligibilityPolicyResponse]:
    return EligibilityService(session).list_policies(expedition_id)


@router.post(
    "/{expedition_id}/eligibility-precheck",
    response_model=EligibilityPrecheckResponse,
)
def precheck_eligibility(
    expedition_id: int,
    data: EligibilityPrecheckRequest,
    session: SessionDep,
) -> EligibilityPrecheckResponse:
    return EligibilityService(session).precheck(expedition_id, data.user_id)


@router.get("/{expedition_id}/pending-reviews", response_model=PendingReviewList)
def list_pending_reviews(
    expedition_id: int,
    session: SessionDep,
) -> PendingReviewList:
    return EligibilityService(session).pending_reviews(expedition_id)


@router.get(
    "/{expedition_id}/registrations/{registration_id}/eligibility",
    response_model=EligibilityEvaluationResponse,
)
def get_registration_eligibility(
    expedition_id: int,
    registration_id: int,
    session: SessionDep,
) -> EligibilityEvaluationResponse:
    return EligibilityService(session).get_registration_evaluation(expedition_id, registration_id)


@router.post(
    "/{expedition_id}/registrations/{registration_id}/review",
    response_model=RegistrationResponse,
)
def review_registration(
    expedition_id: int,
    registration_id: int,
    data: EligibilityReviewCreate,
    session: SessionDep,
) -> RegistrationResponse:
    return EligibilityService(session).review(expedition_id, registration_id, data)

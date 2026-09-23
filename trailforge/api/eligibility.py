from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from trailforge.api.dependencies import get_session
from trailforge.errors import NotFoundError
from trailforge.schemas.eligibility import (
    EligibilityDecisionResponse,
    EligibilityEvaluation,
    EligibilityPolicyResponse,
    EligibilityPolicyUpsert,
    EligibilityPrecheckRequest,
    EligibilityReviewRequest,
)
from trailforge.services.eligibility import EligibilityService

router = APIRouter(tags=["eligibility"])
SessionDep = Annotated[Session, Depends(get_session)]


@router.put(
    "/expeditions/{expedition_id}/eligibility-policy",
    response_model=EligibilityPolicyResponse,
)
def put_eligibility_policy(
    expedition_id: int,
    data: EligibilityPolicyUpsert,
    session: SessionDep,
    actor_id: int = Query(gt=0),
) -> EligibilityPolicyResponse:
    return EligibilityService(session).publish_policy(
        expedition_id, data, actor_id=actor_id
    )


@router.get(
    "/expeditions/{expedition_id}/eligibility-policy",
    response_model=EligibilityPolicyResponse,
)
def get_eligibility_policy(
    expedition_id: int,
    session: SessionDep,
) -> EligibilityPolicyResponse:
    policy = EligibilityService(session).active_policy(expedition_id)
    if policy is None:
        raise NotFoundError("no eligibility policy configured")
    return policy


@router.get(
    "/expeditions/{expedition_id}/eligibility-policy/versions",
    response_model=list[EligibilityPolicyResponse],
)
def list_eligibility_policy_versions(
    expedition_id: int,
    session: SessionDep,
) -> list[EligibilityPolicyResponse]:
    return EligibilityService(session).list_policy_versions(expedition_id)


@router.get(
    "/expeditions/{expedition_id}/eligibility-policy/versions/{version}",
    response_model=EligibilityPolicyResponse,
)
def get_eligibility_policy_version(
    expedition_id: int,
    version: int,
    session: SessionDep,
) -> EligibilityPolicyResponse:
    return EligibilityService(session).get_policy_version(expedition_id, version)


@router.post(
    "/expeditions/{expedition_id}/eligibility-precheck",
    response_model=EligibilityEvaluation,
)
def eligibility_precheck(
    expedition_id: int,
    data: EligibilityPrecheckRequest,
    session: SessionDep,
) -> EligibilityEvaluation:
    return EligibilityService(session).precheck(expedition_id, data.user_id)


@router.get(
    "/expeditions/{expedition_id}/eligibility-decisions",
    response_model=list[EligibilityDecisionResponse],
)
def list_eligibility_decisions(
    expedition_id: int,
    session: SessionDep,
    actor_id: int = Query(gt=0),
    outcome: str | None = None,
) -> list[EligibilityDecisionResponse]:
    return EligibilityService(session).list_decisions(
        expedition_id, actor_id=actor_id, outcome=outcome
    )


@router.get(
    "/eligibility-decisions/{decision_id}",
    response_model=EligibilityDecisionResponse,
)
def get_eligibility_decision(
    decision_id: int,
    session: SessionDep,
    actor_id: int = Query(gt=0),
) -> EligibilityDecisionResponse:
    return EligibilityService(session).get_decision(decision_id, actor_id=actor_id)


@router.post(
    "/eligibility-decisions/{decision_id}/reviews",
    response_model=EligibilityDecisionResponse,
    status_code=status.HTTP_200_OK,
)
def review_eligibility_decision(
    decision_id: int,
    data: EligibilityReviewRequest,
    session: SessionDep,
    actor_id: int = Query(gt=0),
) -> EligibilityDecisionResponse:
    return EligibilityService(session).review(decision_id, data, actor_id=actor_id)

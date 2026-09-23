from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import func, select

from tests.conftest import create_expedition, create_route, create_user
from trailforge.database.session import Database
from trailforge.domain.enums import (
    ActivityStatus,
    EligibilityDecision,
    PlanStatus,
    RegistrationStatus,
)
from trailforge.errors import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    TrailForgeError,
    UnauthorizedOperationError,
)
from trailforge.models.activities import Expedition, ExpeditionRegistration
from trailforge.models.audit import AuditLog, IdempotencyRecord
from trailforge.models.eligibility import (
    EligibilityEvaluation,
    EligibilityReview,
)
from trailforge.models.users import HealthRestriction
from trailforge.schemas.activities import (
    ActivityStateChange,
    RegistrationCreate,
    WithdrawalRequest,
)
from trailforge.schemas.eligibility import (
    EligibilityPolicyUpsert,
    EligibilityReviewCreate,
)
from trailforge.schemas.training import (
    SessionCompleteRequest,
    TrainingExerciseCreate,
    TrainingPlanCreate,
    TrainingRecordCreate,
    TrainingSessionCreate,
)
from trailforge.schemas.users import (
    EmergencyContactCreate,
    HealthRestrictionCreate,
    HealthRestrictionUpdate,
)
from trailforge.services.activities import ExpeditionService
from trailforge.services.eligibility import EligibilityService
from trailforge.services.training import TrainingService
from trailforge.services.users import UserService

SHANGHAI = timezone(timedelta(hours=8))


def _new_user(session, label: str) -> int:
    token = uuid4().hex[:8]
    return create_user(session, email=f"{label}-{token}@example.com", name=label)


def _open_expedition(session, capacity: int = 3, offset_days: int = 10) -> tuple[int, int]:
    organizer = _new_user(session, "organizer")
    route = create_route(session, actor_id=organizer)
    expedition = create_expedition(
        session,
        organizer_id=organizer,
        route_id=route,
        capacity=capacity,
        offset_days=offset_days,
    )
    ExpeditionService(session).change_status(
        expedition,
        ActivityStateChange(target_status="open", actor_id=organizer),
    )
    return organizer, expedition


def _active_training_plan(session, user_id: int):
    now = datetime.now(UTC)
    service = TrainingService(session)
    plan = service.create_plan(
        TrainingPlanCreate(
            user_id=user_id,
            name="Eligibility prep",
            goal="Qualify",
            start_at=now - timedelta(days=90),
            end_at=now + timedelta(days=90),
            target_sessions_per_week=7,
            exercises=[
                TrainingExerciseCreate(
                    sequence=1,
                    name="Endurance ride",
                    training_type="endurance",
                    target_duration_minutes=60,
                    planned_rpe=5,
                ),
                TrainingExerciseCreate(
                    sequence=2,
                    name="Loaded walk",
                    training_type="loaded_walk",
                    target_duration_minutes=60,
                    target_load_kg=10,
                    planned_rpe=6,
                ),
            ],
        ),
        actor_id=user_id,
    )
    return service.change_plan_status(plan.id, PlanStatus.ACTIVE, actor_id=user_id)


def _complete_training(
    session,
    plan,
    user_id: int,
    *,
    exercise_index: int,
    start: datetime,
    duration_minutes: int,
) -> None:
    service = TrainingService(session)
    exercise = plan.exercises[exercise_index]
    scheduled = service.schedule_session(
        TrainingSessionCreate(
            plan_id=plan.id,
            title="Eligibility training",
            planned_start_at=start,
            planned_end_at=start + timedelta(minutes=duration_minutes),
        ),
        actor_id=user_id,
    )
    started = service.start_session(scheduled.id, actor_id=user_id, started_at=start)
    service.complete_session(
        started.id,
        SessionCompleteRequest(
            completed_at=start + timedelta(minutes=duration_minutes),
            records=[
                TrainingRecordCreate(
                    exercise_id=exercise.id,
                    duration_minutes=duration_minutes,
                    perceived_exertion=5,
                    completion_percent=100,
                )
            ],
        ),
        actor_id=user_id,
    )


def _completed_expedition(
    session,
    *,
    organizer_id: int,
    route_id: int,
    user_id: int,
    ended_days_ago: int,
    offset_days: int,
) -> int:
    """Register the user on an expedition, then move it to a completed past state."""
    expedition_id = create_expedition(
        session,
        organizer_id=organizer_id,
        route_id=route_id,
        capacity=5,
        offset_days=offset_days,
    )
    service = ExpeditionService(session)
    service.change_status(
        expedition_id,
        ActivityStateChange(target_status="open", actor_id=organizer_id),
    )
    service.register(
        expedition_id,
        RegistrationCreate(user_id=user_id, idempotency_key=f"past-{expedition_id}-key"),
    )
    expedition = session.get(Expedition, expedition_id)
    end = datetime.now(UTC) - timedelta(days=ended_days_ago)
    start = end - timedelta(hours=8)
    expedition.status = ActivityStatus.COMPLETED
    expedition.start_at = start
    expedition.end_at = end
    expedition.meeting_at = start - timedelta(hours=1)
    expedition.registration_deadline = start - timedelta(days=1)
    session.flush()
    return expedition_id


def _upsert_policy(session, expedition_id: int, organizer_id: int, **rules):
    return EligibilityService(session).upsert_policy(
        expedition_id,
        EligibilityPolicyUpsert(actor_id=organizer_id, **rules),
    )


def _evaluation_for(session, registration_id: int) -> EligibilityEvaluation:
    evaluation = session.scalar(
        select(EligibilityEvaluation)
        .where(EligibilityEvaluation.registration_id == registration_id)
        .order_by(EligibilityEvaluation.id.desc())
        .limit(1)
    )
    assert evaluation is not None
    return evaluation


def _reasons_by_rule(evaluation: EligibilityEvaluation) -> dict[str, dict]:
    return {item["rule"]: item for item in evaluation.reasons}


# ----------------------------------------------------------------------
# Policy validation
# ----------------------------------------------------------------------
def test_policy_requires_at_least_one_rule() -> None:
    with pytest.raises(PydanticValidationError, match="at least one eligibility rule"):
        EligibilityPolicyUpsert(actor_id=1)


def test_policy_training_threshold_requires_window() -> None:
    with pytest.raises(PydanticValidationError, match="training_window_days"):
        EligibilityPolicyUpsert(actor_id=1, min_endurance_minutes=60)


def test_policy_experience_window_requires_expedition_threshold() -> None:
    with pytest.raises(PydanticValidationError, match="experience_window_days"):
        EligibilityPolicyUpsert(actor_id=1, experience_window_days=365)


def test_policy_restriction_overlap_is_rejected() -> None:
    with pytest.raises(PydanticValidationError, match="both blocked and review"):
        EligibilityPolicyUpsert(
            actor_id=1,
            blocked_restrictions=["Asthma"],
            review_restrictions=[" asthma "],
        )


def test_policy_normalizes_and_deduplicates_restriction_names() -> None:
    policy = EligibilityPolicyUpsert(
        actor_id=1,
        blocked_restrictions=["  Severe   Asthma ", "severe asthma", "Knee"],
    )
    assert policy.blocked_restrictions == ["severe asthma", "knee"]


def test_policy_maintenance_requires_organizer(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    intruder = _new_user(session, "intruder")
    with pytest.raises(UnauthorizedOperationError):
        _upsert_policy(
            session,
            expedition_id,
            intruder,
            min_emergency_contacts=1,
        )
    policy = _upsert_policy(session, expedition_id, organizer, min_emergency_contacts=1)
    assert policy.version == 1
    assert policy.is_active is True


def test_policy_update_creates_new_version_and_deactivates_previous(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    service = EligibilityService(session)
    _upsert_policy(session, expedition_id, organizer, min_emergency_contacts=1)
    second = _upsert_policy(
        session,
        expedition_id,
        organizer,
        training_window_days=30,
        min_endurance_minutes=120,
    )
    assert second.version == 2
    versions = service.list_policies(expedition_id)
    assert [item.version for item in versions] == [2, 1]
    assert versions[0].is_active is True
    assert versions[1].is_active is False
    assert service.get_active_policy(expedition_id).version == 2


# ----------------------------------------------------------------------
# Rule combinations evaluated inside the registration transaction
# ----------------------------------------------------------------------
def test_full_rule_combination_passes_and_persists_evaluation(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    route_id = session.get(Expedition, expedition_id).route_id
    applicant = _new_user(session, "qualified")
    plan = _active_training_plan(session, applicant)
    now = datetime.now(UTC)
    _complete_training(
        session, plan, applicant, exercise_index=0, start=now - timedelta(days=2),
        duration_minutes=120,
    )
    _complete_training(
        session, plan, applicant, exercise_index=1, start=now - timedelta(days=1),
        duration_minutes=45,
    )
    _completed_expedition(
        session,
        organizer_id=organizer,
        route_id=route_id,
        user_id=applicant,
        ended_days_ago=20,
        offset_days=40,
    )
    UserService(session).add_contact(
        applicant,
        EmergencyContactCreate(
            name="Base Camp", relationship_label="Friend", phone="13800000000", priority=1
        ),
        actor_id=applicant,
    )
    _upsert_policy(
        session,
        expedition_id,
        organizer,
        training_window_days=30,
        min_endurance_minutes=60,
        min_loaded_sessions=1,
        min_completed_expeditions=1,
        experience_window_days=365,
        blocked_restrictions=["heart condition"],
        review_restrictions=["asthma"],
        min_emergency_contacts=1,
    )
    registration = ExpeditionService(session).register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="combo-pass-key"),
    )
    assert registration.status == RegistrationStatus.CONFIRMED
    evaluation = _evaluation_for(session, registration.id)
    assert evaluation.decision == EligibilityDecision.APPROVED
    assert evaluation.policy_version == 1
    facts = evaluation.facts
    assert facts["endurance_minutes"] == 120
    assert facts["loaded_sessions"] == 1
    assert facts["completed_expeditions"] == 1
    assert facts["emergency_contacts"] == 1
    assert facts["active_restrictions"] == []
    assert facts["fitness_rank"] == 2
    reasons = _reasons_by_rule(evaluation)
    assert set(reasons) == {
        "blocked_restrictions",
        "review_restrictions",
        "min_endurance_minutes",
        "min_loaded_sessions",
        "min_completed_expeditions",
        "min_emergency_contacts",
    }
    assert all(item["outcome"] == "passed" for item in reasons.values())


def test_blocked_restriction_rejects_registration_without_occupying_capacity(session) -> None:
    organizer, expedition_id = _open_expedition(session, capacity=2)
    applicant = _new_user(session, "blocked")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Heart Condition", severity=4),
        actor_id=applicant,
    )
    _upsert_policy(
        session,
        expedition_id,
        organizer,
        blocked_restrictions=["heart condition"],
    )
    registration = ExpeditionService(session).register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="blocked-key-1"),
    )
    assert registration.status == RegistrationStatus.REJECTED
    evaluation = _evaluation_for(session, registration.id)
    assert evaluation.decision == EligibilityDecision.REJECTED
    reason = _reasons_by_rule(evaluation)["blocked_restrictions"]
    assert reason["outcome"] == "failed"
    assert reason["actual"] == ["heart condition"]
    roster = ExpeditionService(session).roster(expedition_id)
    assert roster.confirmed_count == 1  # only the organizer
    assert roster.available_places == 1


def test_review_restriction_sends_registration_to_pending(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    applicant = _new_user(session, "review-me")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    _upsert_policy(session, expedition_id, organizer, review_restrictions=["asthma"])
    registration = ExpeditionService(session).register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="pending-key-1"),
    )
    assert registration.status == RegistrationStatus.PENDING
    evaluation = _evaluation_for(session, registration.id)
    assert evaluation.decision == EligibilityDecision.PENDING_REVIEW
    assert _reasons_by_rule(evaluation)["review_restrictions"]["outcome"] == "review"


def test_multiple_failing_rules_are_all_reported(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    applicant = _new_user(session, "unprepared")
    _upsert_policy(
        session,
        expedition_id,
        organizer,
        training_window_days=7,
        min_endurance_minutes=60,
        min_emergency_contacts=1,
    )
    registration = ExpeditionService(session).register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="multi-fail-key"),
    )
    assert registration.status == RegistrationStatus.REJECTED
    evaluation = _evaluation_for(session, registration.id)
    reasons = _reasons_by_rule(evaluation)
    failed = {rule for rule, item in reasons.items() if item["outcome"] == "failed"}
    assert failed == {"min_endurance_minutes", "min_emergency_contacts"}
    assert reasons["min_endurance_minutes"]["expected"] == 60
    assert reasons["min_endurance_minutes"]["actual"] == 0


def test_inactive_restriction_does_not_trigger_rules(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    applicant = _new_user(session, "recovered")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2, is_active=False),
        actor_id=applicant,
    )
    _upsert_policy(
        session,
        expedition_id,
        organizer,
        blocked_restrictions=["asthma"],
        review_restrictions=["knee strain"],
    )
    registration = ExpeditionService(session).register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="inactive-rule-key"),
    )
    assert registration.status == RegistrationStatus.CONFIRMED


# ----------------------------------------------------------------------
# Timezone-aware windows
# ----------------------------------------------------------------------
def test_training_window_counts_only_sessions_inside_utc_window(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    applicant = _new_user(session, "timezone")
    plan = _active_training_plan(session, applicant)
    now = datetime.now(UTC)
    # 30 hours ago (inside a 2-day window) and 50 hours ago (outside),
    # both expressed in UTC+08:00 to prove instants are compared in UTC.
    _complete_training(
        session,
        plan,
        applicant,
        exercise_index=0,
        start=(now - timedelta(hours=30)).astimezone(SHANGHAI),
        duration_minutes=45,
    )
    _complete_training(
        session,
        plan,
        applicant,
        exercise_index=0,
        start=(now - timedelta(hours=50)).astimezone(SHANGHAI),
        duration_minutes=200,
    )
    _upsert_policy(
        session,
        expedition_id,
        organizer,
        training_window_days=2,
        min_endurance_minutes=60,
    )
    precheck = EligibilityService(session).precheck(expedition_id, applicant)
    assert precheck.decision == EligibilityDecision.REJECTED
    assert precheck.facts["endurance_minutes"] == 45
    window_start = datetime.fromisoformat(precheck.facts["training_window_start"])
    assert window_start.tzinfo is not None
    age = datetime.now(UTC) - window_start
    assert abs(age - timedelta(days=2)) < timedelta(minutes=1)


def test_experience_window_expires_old_expeditions(session) -> None:
    organizer = _new_user(session, "veteran-organizer")
    route_id = create_route(session, actor_id=organizer)
    applicant = _new_user(session, "rusty")
    _completed_expedition(
        session,
        organizer_id=organizer,
        route_id=route_id,
        user_id=applicant,
        ended_days_ago=10,
        offset_days=30,
    )
    _completed_expedition(
        session,
        organizer_id=organizer,
        route_id=route_id,
        user_id=applicant,
        ended_days_ago=300,
        offset_days=40,
    )
    expedition_id = create_expedition(
        session, organizer_id=organizer, route_id=route_id, capacity=3, offset_days=10
    )
    ExpeditionService(session).change_status(
        expedition_id,
        ActivityStateChange(target_status="open", actor_id=organizer),
    )
    service = EligibilityService(session)
    _upsert_policy(
        session,
        expedition_id,
        organizer,
        min_completed_expeditions=2,
        experience_window_days=30,
    )
    stale = service.precheck(expedition_id, applicant)
    assert stale.decision == EligibilityDecision.REJECTED
    assert stale.facts["completed_expeditions"] == 1
    _upsert_policy(
        session,
        expedition_id,
        organizer,
        min_completed_expeditions=2,
        experience_window_days=365,
    )
    fresh = service.precheck(expedition_id, applicant)
    assert fresh.decision == EligibilityDecision.APPROVED
    assert fresh.facts["completed_expeditions"] == 2


# ----------------------------------------------------------------------
# History cannot be rewritten; policy updates only affect new applications
# ----------------------------------------------------------------------
def test_profile_change_does_not_rewrite_stored_evaluation(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    applicant = _new_user(session, "changing")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    _upsert_policy(session, expedition_id, organizer, review_restrictions=["asthma"])
    registration = ExpeditionService(session).register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="history-key-1"),
    )
    assert registration.status == RegistrationStatus.PENDING
    evaluation = _evaluation_for(session, registration.id)
    restriction = session.scalar(
        select(HealthRestriction).where(HealthRestriction.user_id == applicant)
    )
    UserService(session).update_restriction(
        applicant,
        restriction.id,
        HealthRestrictionUpdate(is_active=False),
        actor_id=applicant,
    )
    session.expire(evaluation)
    stored = session.get(EligibilityEvaluation, evaluation.id)
    assert stored.decision == EligibilityDecision.PENDING_REVIEW
    assert stored.facts["active_restrictions"] == ["asthma"]
    precheck = EligibilityService(session).precheck(expedition_id, applicant)
    assert precheck.decision == EligibilityDecision.APPROVED


def test_policy_update_applies_only_to_new_applications(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    applicant = _new_user(session, "reapply")
    plan = _active_training_plan(session, applicant)
    _complete_training(
        session,
        plan,
        applicant,
        exercise_index=0,
        start=datetime.now(UTC) - timedelta(days=1),
        duration_minutes=45,
    )
    _upsert_policy(
        session,
        expedition_id,
        organizer,
        training_window_days=7,
        min_endurance_minutes=300,
    )
    expedition_service = ExpeditionService(session)
    first = expedition_service.register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="strict-apply-key"),
    )
    assert first.status == RegistrationStatus.REJECTED
    _upsert_policy(
        session,
        expedition_id,
        organizer,
        training_window_days=7,
        min_endurance_minutes=30,
    )
    second = expedition_service.register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="lenient-apply-key"),
    )
    assert second.id == first.id
    assert second.status == RegistrationStatus.CONFIRMED
    evaluations = list(
        session.scalars(
            select(EligibilityEvaluation)
            .where(EligibilityEvaluation.registration_id == first.id)
            .order_by(EligibilityEvaluation.id)
        )
    )
    assert len(evaluations) == 2
    assert evaluations[0].policy_version == 1
    assert evaluations[0].decision == EligibilityDecision.REJECTED
    assert evaluations[1].policy_version == 2
    assert evaluations[1].decision == EligibilityDecision.APPROVED


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------
def test_pending_registration_replay_does_not_duplicate_records(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    applicant = _new_user(session, "idempotent")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    _upsert_policy(session, expedition_id, organizer, review_restrictions=["asthma"])
    service = ExpeditionService(session)
    request = RegistrationCreate(user_id=applicant, idempotency_key="pending-replay-key")
    first = service.register(expedition_id, request)
    replay = service.register(expedition_id, request)
    assert first.status == RegistrationStatus.PENDING
    assert replay.id == first.id
    assert replay.status == RegistrationStatus.PENDING
    assert (
        session.scalar(
            select(func.count())
            .select_from(ExpeditionRegistration)
            .where(ExpeditionRegistration.user_id == applicant)
        )
        == 1
    )
    assert session.scalar(select(func.count()).select_from(EligibilityEvaluation)) == 1
    assert session.scalar(select(func.count()).select_from(IdempotencyRecord)) == 1


def test_rejected_user_can_reapply_with_new_key(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    applicant = _new_user(session, "rejected")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    _upsert_policy(session, expedition_id, organizer, blocked_restrictions=["asthma"])
    service = ExpeditionService(session)
    rejected = service.register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="rejected-first-key"),
    )
    assert rejected.status == RegistrationStatus.REJECTED
    replayed = service.register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="rejected-first-key"),
    )
    assert replayed.id == rejected.id
    assert replayed.status == RegistrationStatus.REJECTED
    restriction = session.scalar(
        select(HealthRestriction).where(HealthRestriction.user_id == applicant)
    )
    UserService(session).update_restriction(
        applicant,
        restriction.id,
        HealthRestrictionUpdate(is_active=False),
        actor_id=applicant,
    )
    reapplied = service.register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="rejected-second-key"),
    )
    assert reapplied.id == rejected.id
    assert reapplied.status == RegistrationStatus.CONFIRMED


# ----------------------------------------------------------------------
# Review flow
# ----------------------------------------------------------------------
def _pending_registration(session, capacity: int = 3):
    organizer, expedition_id = _open_expedition(session, capacity=capacity)
    applicant = _new_user(session, "pending")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    _upsert_policy(session, expedition_id, organizer, review_restrictions=["asthma"])
    registration = ExpeditionService(session).register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key=f"pending-{uuid4().hex[:8]}"),
    )
    assert registration.status == RegistrationStatus.PENDING
    return organizer, applicant, expedition_id, registration


def test_review_requires_organizer(session) -> None:
    _, applicant, expedition_id, registration = _pending_registration(session)
    with pytest.raises(UnauthorizedOperationError):
        EligibilityService(session).review(
            expedition_id,
            registration.id,
            EligibilityReviewCreate(
                actor_id=applicant,
                decision="approve",
                reason="self approval",
                expected_version=registration.version,
            ),
        )


def test_review_requires_a_reason() -> None:
    with pytest.raises(PydanticValidationError):
        EligibilityReviewCreate(
            actor_id=1,
            decision="approve",
            reason="   ",
            expected_version=1,
        )


def test_review_requires_pending_registration(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    leader = ExpeditionService(session).expeditions.get_registration(expedition_id, organizer)
    with pytest.raises(InvalidStateError):
        EligibilityService(session).review(
            expedition_id,
            leader.id,
            EligibilityReviewCreate(
                actor_id=organizer,
                decision="approve",
                reason="not pending",
                expected_version=leader.version,
            ),
        )


def test_review_detects_version_conflict(session) -> None:
    organizer, _, expedition_id, registration = _pending_registration(session)
    with pytest.raises(ConflictError, match="modified by another operation"):
        EligibilityService(session).review(
            expedition_id,
            registration.id,
            EligibilityReviewCreate(
                actor_id=organizer,
                decision="approve",
                reason="stale version",
                expected_version=registration.version + 5,
            ),
        )


def test_review_approve_confirms_when_capacity_available(session) -> None:
    organizer, _, expedition_id, registration = _pending_registration(session, capacity=3)
    reviewed = EligibilityService(session).review(
        expedition_id,
        registration.id,
        EligibilityReviewCreate(
            actor_id=organizer,
            decision="approve",
            reason="documents verified",
            expected_version=registration.version,
        ),
    )
    assert reviewed.status == RegistrationStatus.CONFIRMED
    assert reviewed.version == registration.version + 1
    review = session.scalar(select(EligibilityReview))
    assert review.reviewer_id == organizer
    assert review.reason == "documents verified"
    assert review.resulting_status == RegistrationStatus.CONFIRMED
    evaluation = _evaluation_for(session, registration.id)
    assert review.evaluation_id == evaluation.id
    # the historical evaluation keeps its original conclusion
    assert evaluation.decision == EligibilityDecision.PENDING_REVIEW


def test_review_approve_waitlists_when_capacity_is_full(session) -> None:
    organizer, _, expedition_id, registration = _pending_registration(session, capacity=2)
    other = _new_user(session, "other")
    ExpeditionService(session).register(
        expedition_id,
        RegistrationCreate(user_id=other, idempotency_key="other-full-key"),
    )
    reviewed = EligibilityService(session).review(
        expedition_id,
        registration.id,
        EligibilityReviewCreate(
            actor_id=organizer,
            decision="approve",
            reason="approved but full",
            expected_version=registration.version,
        ),
    )
    assert reviewed.status == RegistrationStatus.WAITLISTED


def test_review_approve_rechecks_time_conflicts(session) -> None:
    # a parallel expedition at the same time that is already full
    other_organizer = _new_user(session, "other-organizer")
    other_route = create_route(session, actor_id=other_organizer, name="Parallel Route")
    parallel_id = create_expedition(
        session,
        organizer_id=other_organizer,
        route_id=other_route,
        capacity=2,
        offset_days=10,
    )
    expedition_service = ExpeditionService(session)
    expedition_service.change_status(
        parallel_id,
        ActivityStateChange(target_status="open", actor_id=other_organizer),
    )
    filler = _new_user(session, "filler")
    applicant = _new_user(session, "conflicted")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    expedition_service.register(
        parallel_id,
        RegistrationCreate(user_id=filler, idempotency_key="parallel-filler-key"),
    )
    waitlisted = expedition_service.register(
        parallel_id,
        RegistrationCreate(user_id=applicant, idempotency_key="parallel-applicant-key"),
    )
    assert waitlisted.status == RegistrationStatus.WAITLISTED
    # the applicant then applies to the policy-protected expedition and pends
    organizer = _new_user(session, "main-organizer")
    main_route = create_route(session, actor_id=organizer, name="Main Route")
    expedition_id = create_expedition(
        session,
        organizer_id=organizer,
        route_id=main_route,
        capacity=3,
        offset_days=10,
    )
    expedition_service.change_status(
        expedition_id,
        ActivityStateChange(target_status="open", actor_id=organizer),
    )
    _upsert_policy(session, expedition_id, organizer, review_restrictions=["asthma"])
    pending = expedition_service.register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="main-applicant-key"),
    )
    assert pending.status == RegistrationStatus.PENDING
    # a withdrawal on the parallel expedition promotes the applicant to confirmed
    expedition_service.withdraw(
        parallel_id,
        WithdrawalRequest(
            user_id=filler,
            reason="schedule changed",
            idempotency_key="parallel-withdraw-key",
        ),
    )
    promoted = expedition_service.expeditions.get_registration(parallel_id, applicant)
    assert promoted.status == RegistrationStatus.CONFIRMED
    # approving the pending registration must now fail the time-conflict re-check
    with pytest.raises(ConflictError, match="another expedition"):
        EligibilityService(session).review(
            expedition_id,
            pending.id,
            EligibilityReviewCreate(
                actor_id=organizer,
                decision="approve",
                reason="should fail on conflict",
                expected_version=pending.version,
            ),
        )
    still_pending = expedition_service.expeditions.get_registration(expedition_id, applicant)
    assert still_pending.status == RegistrationStatus.PENDING


def test_review_reject_marks_registration_rejected(session) -> None:
    organizer, _, expedition_id, registration = _pending_registration(session)
    reviewed = EligibilityService(session).review(
        expedition_id,
        registration.id,
        EligibilityReviewCreate(
            actor_id=organizer,
            decision="reject",
            reason="medical certificate missing",
            expected_version=registration.version,
        ),
    )
    assert reviewed.status == RegistrationStatus.REJECTED
    review = session.scalar(select(EligibilityReview))
    assert review.decision == "reject"
    assert review.reason == "medical certificate missing"
    audit = session.scalar(
        select(AuditLog).where(AuditLog.action == "eligibility_reviewed")
    )
    assert audit is not None
    assert audit.context["reason"] == "medical certificate missing"


def test_second_review_is_rejected_after_resolution(session) -> None:
    organizer, _, expedition_id, registration = _pending_registration(session)
    service = EligibilityService(session)
    service.review(
        expedition_id,
        registration.id,
        EligibilityReviewCreate(
            actor_id=organizer,
            decision="approve",
            reason="first review",
            expected_version=registration.version,
        ),
    )
    with pytest.raises(InvalidStateError):
        service.review(
            expedition_id,
            registration.id,
            EligibilityReviewCreate(
                actor_id=organizer,
                decision="reject",
                reason="second review",
                expected_version=registration.version + 1,
            ),
        )


def test_concurrent_reviews_allow_only_one_success(database: Database) -> None:
    with database.session() as session:
        organizer, _, expedition_id, registration = _pending_registration(session)
        organizer_id = organizer
        registration_id = registration.id
        version = registration.version

    def attempt() -> str:
        def operation(session) -> str:
            EligibilityService(session).review(
                expedition_id,
                registration_id,
                EligibilityReviewCreate(
                    actor_id=organizer_id,
                    decision="approve",
                    reason="concurrent review",
                    expected_version=version,
                ),
            )
            return "ok"

        database.run_write(operation)
        return "ok"

    outcomes: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        for future in futures:
            try:
                outcomes.append(future.result())
            except TrailForgeError:
                outcomes.append("conflict")
    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 1
    with database.session() as session:
        stored = session.get(ExpeditionRegistration, registration_id)
        assert stored.status == RegistrationStatus.CONFIRMED
        reviews = list(
            session.scalars(
                select(EligibilityReview).where(
                    EligibilityReview.registration_id == registration_id
                )
            )
        )
        assert len(reviews) == 1


# ----------------------------------------------------------------------
# Legacy expeditions without a policy
# ----------------------------------------------------------------------
def test_expedition_without_policy_keeps_legacy_behavior(session) -> None:
    _, expedition_id = _open_expedition(session)
    applicant = _new_user(session, "legacy")
    registration = ExpeditionService(session).register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="legacy-apply-key"),
    )
    assert registration.status == RegistrationStatus.CONFIRMED
    assert session.scalar(select(func.count()).select_from(EligibilityEvaluation)) == 0
    service = EligibilityService(session)
    with pytest.raises(NotFoundError):
        service.get_active_policy(expedition_id)
    precheck = service.precheck(expedition_id, applicant)
    assert precheck.decision == EligibilityDecision.APPROVED
    assert precheck.policy_version is None
    assert "no eligibility policy" in precheck.message


# ----------------------------------------------------------------------
# Audit masking
# ----------------------------------------------------------------------
def test_evaluation_audit_redacts_health_details(session) -> None:
    organizer, expedition_id = _open_expedition(session)
    applicant = _new_user(session, "sensitive")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(
            name="Severe Asthma",
            severity=3,
            activity_guidance="Carry two inhalers at all times",
        ),
        actor_id=applicant,
    )
    _upsert_policy(session, expedition_id, organizer, review_restrictions=["severe asthma"])
    registration = ExpeditionService(session).register(
        expedition_id,
        RegistrationCreate(user_id=applicant, idempotency_key="sensitive-key-1"),
    )
    assert registration.status == RegistrationStatus.PENDING
    audit = session.scalar(
        select(AuditLog).where(AuditLog.entity_type == "eligibility_evaluation")
    )
    assert audit is not None
    serialized = json.dumps(
        {"after": audit.after_state, "context": audit.context}, ensure_ascii=False
    )
    assert "Severe Asthma" not in serialized
    assert "severe asthma" not in serialized
    assert "inhaler" not in serialized
    assert audit.context["health"] == "[REDACTED]"
    # the evaluation record itself keeps the facts that justify the decision
    evaluation = _evaluation_for(session, registration.id)
    assert _reasons_by_rule(evaluation)["review_restrictions"]["actual"] == ["severe asthma"]


def test_emergency_contact_phone_is_redacted_in_audit(session) -> None:
    user_id = _new_user(session, "phone-owner")
    UserService(session).add_contact(
        user_id,
        EmergencyContactCreate(
            name="Mountain Rescue",
            relationship_label="Friend",
            phone="13800001111",
            priority=1,
        ),
        actor_id=user_id,
    )
    audit = session.scalar(
        select(AuditLog).where(AuditLog.entity_type == "emergency_contact")
    )
    assert audit is not None
    # sensitive fields are stripped from audit snapshots entirely
    assert "phone" not in audit.after_state
    assert "13800001111" not in json.dumps(audit.after_state)


# ----------------------------------------------------------------------
# API integration
# ----------------------------------------------------------------------
def _api_user(client, label: str) -> int:
    response = client.post(
        "/api/v1/users",
        json={
            "email": f"{label}-{uuid4().hex[:8]}@example.com",
            "display_name": label,
            "timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 201, response.text
    user_id = response.json()["id"]
    profile = client.put(
        f"/api/v1/users/{user_id}/sport-profile",
        params={"actor_id": user_id},
        json={
            "height_cm": 170,
            "weight_kg": 65,
            "fitness_level": "intermediate",
            "outdoor_experience": "Local hiking",
            "weekly_training_minutes": 180,
        },
    )
    assert profile.status_code == 200, profile.text
    return user_id


def _api_expedition(client, organizer_id: int, capacity: int = 3) -> int:
    route = client.post(
        "/api/v1/routes",
        params={"actor_id": organizer_id},
        json={
            "name": "Eligibility Ridge",
            "region": "API Mountains",
            "description": "Stored locally",
            "distance_km": 8,
            "elevation_gain_m": 400,
            "elevation_loss_m": 400,
            "min_altitude_m": 100,
            "max_altitude_m": 500,
            "estimated_duration_minutes": 180,
            "difficulty": "moderate",
            "is_loop": True,
            "is_published": True,
            "segments": [
                {
                    "sequence": 1,
                    "name": "Loop",
                    "distance_km": 8,
                    "elevation_gain_m": 400,
                    "estimated_duration_minutes": 180,
                    "difficulty": "moderate",
                    "start_latitude": 30,
                    "start_longitude": 120,
                    "end_latitude": 30,
                    "end_longitude": 120,
                }
            ],
            "points": [],
            "risk_tag_ids": [],
        },
    )
    assert route.status_code == 201, route.text
    start = datetime.now(UTC) + timedelta(days=10)
    expedition = client.post(
        "/api/v1/expeditions",
        json={
            "organizer_id": organizer_id,
            "route_id": route.json()["id"],
            "name": "Eligibility Expedition",
            "meeting_location": "Trailhead",
            "meeting_at": (start - timedelta(hours=1)).isoformat(),
            "start_at": start.isoformat(),
            "end_at": (start + timedelta(hours=6)).isoformat(),
            "registration_deadline": (start - timedelta(days=1)).isoformat(),
            "capacity": capacity,
            "minimum_fitness_level": 1,
            "risk_level": "moderate",
        },
    )
    assert expedition.status_code == 201, expedition.text
    expedition_id = expedition.json()["id"]
    opened = client.post(
        f"/api/v1/expeditions/{expedition_id}/status",
        json={"target_status": "open", "actor_id": organizer_id, "reason": "Ready"},
    )
    assert opened.status_code == 200, opened.text
    return expedition_id


def test_api_eligibility_end_to_end(client) -> None:
    organizer = _api_user(client, "api-organizer")
    expedition_id = _api_expedition(client, organizer)
    applicant = _api_user(client, "api-applicant")
    restriction = client.post(
        f"/api/v1/users/{applicant}/health-restrictions",
        params={"actor_id": applicant},
        json={"name": "Asthma", "severity": 2},
    )
    assert restriction.status_code == 201, restriction.text
    contact = client.post(
        f"/api/v1/users/{applicant}/emergency-contacts",
        params={"actor_id": applicant},
        json={
            "name": "Base Camp",
            "relationship_label": "Friend",
            "phone": "13800000000",
            "priority": 1,
        },
    )
    assert contact.status_code == 201, contact.text
    policy = client.put(
        f"/api/v1/expeditions/{expedition_id}/eligibility-policy",
        json={
            "actor_id": organizer,
            "review_restrictions": ["asthma"],
            "min_emergency_contacts": 1,
            "note": "High altitude caution",
        },
    )
    assert policy.status_code == 201, policy.text
    assert policy.json()["version"] == 1
    fetched = client.get(f"/api/v1/expeditions/{expedition_id}/eligibility-policy")
    assert fetched.status_code == 200
    assert fetched.json()["review_restrictions"] == ["asthma"]
    precheck = client.post(
        f"/api/v1/expeditions/{expedition_id}/eligibility-precheck",
        json={"user_id": applicant},
    )
    assert precheck.status_code == 200
    assert precheck.json()["decision"] == "pending_review"
    registration = client.post(
        f"/api/v1/expeditions/{expedition_id}/registrations",
        json={"user_id": applicant, "idempotency_key": "api-pending-key"},
    )
    assert registration.status_code == 201, registration.text
    assert registration.json()["status"] == "pending"
    registration_id = registration.json()["id"]
    evaluation = client.get(
        f"/api/v1/expeditions/{expedition_id}/registrations/{registration_id}/eligibility"
    )
    assert evaluation.status_code == 200
    assert evaluation.json()["decision"] == "pending_review"
    assert evaluation.json()["policy_version"] == 1
    assert evaluation.json()["facts"]["emergency_contacts"] == 1
    pending = client.get(f"/api/v1/expeditions/{expedition_id}/pending-reviews")
    assert pending.status_code == 200
    assert len(pending.json()["items"]) == 1
    assert pending.json()["items"][0]["registration"]["id"] == registration_id
    review = client.post(
        f"/api/v1/expeditions/{expedition_id}/registrations/{registration_id}/review",
        json={
            "actor_id": organizer,
            "decision": "approve",
            "reason": "inhaler confirmed",
            "expected_version": registration.json()["version"],
        },
    )
    assert review.status_code == 200, review.text
    assert review.json()["status"] == "confirmed"
    roster = client.get(f"/api/v1/expeditions/{expedition_id}/roster")
    assert roster.json()["confirmed_count"] == 2
    versions = client.get(f"/api/v1/expeditions/{expedition_id}/eligibility-policies")
    assert versions.status_code == 200
    assert [item["version"] for item in versions.json()] == [1]


def test_api_policy_maintenance_forbidden_for_non_organizer(client) -> None:
    organizer = _api_user(client, "api-owner")
    expedition_id = _api_expedition(client, organizer)
    intruder = _api_user(client, "api-intruder")
    response = client.put(
        f"/api/v1/expeditions/{expedition_id}/eligibility-policy",
        json={"actor_id": intruder, "min_emergency_contacts": 1},
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "operation_not_allowed"


def test_api_review_forbidden_for_non_organizer(client) -> None:
    organizer = _api_user(client, "api-review-owner")
    expedition_id = _api_expedition(client, organizer)
    applicant = _api_user(client, "api-review-applicant")
    client.post(
        f"/api/v1/users/{applicant}/health-restrictions",
        params={"actor_id": applicant},
        json={"name": "Asthma", "severity": 2},
    )
    client.put(
        f"/api/v1/expeditions/{expedition_id}/eligibility-policy",
        json={"actor_id": organizer, "review_restrictions": ["asthma"]},
    )
    registration = client.post(
        f"/api/v1/expeditions/{expedition_id}/registrations",
        json={"user_id": applicant, "idempotency_key": "api-review-key"},
    )
    assert registration.json()["status"] == "pending"
    response = client.post(
        f"/api/v1/expeditions/{expedition_id}/registrations/{registration.json()['id']}/review",
        json={
            "actor_id": applicant,
            "decision": "approve",
            "reason": "self review",
            "expected_version": registration.json()["version"],
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "operation_not_allowed"


def test_api_active_policy_returns_404_for_legacy_expedition(client) -> None:
    organizer = _api_user(client, "api-legacy")
    expedition_id = _api_expedition(client, organizer)
    response = client.get(f"/api/v1/expeditions/{expedition_id}/eligibility-policy")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
    precheck = client.post(
        f"/api/v1/expeditions/{expedition_id}/eligibility-precheck",
        json={"user_id": organizer},
    )
    assert precheck.status_code == 200
    assert precheck.json()["decision"] == "approved"
    assert precheck.json()["policy_version"] is None

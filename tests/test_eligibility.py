from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import select

from tests.conftest import create_expedition, create_route, create_user
from trailforge.database.base import utc_now
from trailforge.domain.enums import (
    EligibilityOutcome,
    PlanStatus,
    RegistrationStatus,
    TrainingType,
)
from trailforge.errors import (
    ConflictError,
    InvalidStateError,
    UnauthorizedOperationError,
)
from trailforge.models.activities import ExpeditionRegistration
from trailforge.models.audit import AuditLog
from trailforge.models.eligibility import EligibilityDecision
from trailforge.schemas.activities import ActivityStateChange, RegistrationCreate
from trailforge.schemas.eligibility import (
    EligibilityPolicyUpsert,
    EligibilityReason,
    EligibilityReviewRequest,
    EligibilityRules,
    HealthRestrictionRule,
    TrainingRequirementRule,
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
    OutdoorExperienceCreate,
)
from trailforge.services.activities import ExpeditionService
from trailforge.services.eligibility import EligibilityService
from trailforge.services.training import TrainingService
from trailforge.services.users import UserService

# --- fixtures / helpers ------------------------------------------------------


def _open_expedition_with_policy(
    session,
    rules: EligibilityRules,
    *,
    capacity: int = 4,
    offset_days: int = 10,
):
    organizer = create_user(session, email="organizer@example.com", name="Organizer")
    route = create_route(session, actor_id=organizer)
    expedition_id = create_expedition(
        session,
        organizer_id=organizer,
        route_id=route,
        capacity=capacity,
        offset_days=offset_days,
    )
    policy = EligibilityService(session).publish_policy(
        expedition_id,
        EligibilityPolicyUpsert(rules=rules, change_note="initial"),
        actor_id=organizer,
    )
    ExpeditionService(session).change_status(
        expedition_id,
        ActivityStateChange(target_status="open", actor_id=organizer),
    )
    return organizer, expedition_id, policy


def _add_completed_session(
    session,
    *,
    user_id: int,
    training_type: TrainingType,
    started_at: datetime,
    duration_minutes: int = 90,
    distance_km: float = 0,
    load_kg: float = 0,
) -> None:
    """Create+activate a plan and complete a session at a fixed instant."""
    service = TrainingService(session)
    exercise = TrainingExerciseCreate(
        sequence=1,
        name=training_type.value,
        training_type=training_type,
        target_duration_minutes=max(duration_minutes, 30),
        target_distance_km=distance_km,
        target_load_kg=max(load_kg, 5) if training_type == TrainingType.LOADED_WALK else load_kg,
        planned_rpe=6,
    )
    plan = service.create_plan(
        TrainingPlanCreate(
            user_id=user_id,
            name=f"{training_type.value} plan {started_at.isoformat()}",
            start_at=started_at - timedelta(days=1),
            end_at=started_at + timedelta(days=30),
            target_sessions_per_week=3,
            exercises=[exercise],
        ),
        actor_id=user_id,
    )
    service.change_plan_status(plan.id, PlanStatus.ACTIVE, actor_id=user_id)
    planned = service.schedule_session(
        TrainingSessionCreate(
            plan_id=plan.id,
            title=f"{training_type.value} session",
            planned_start_at=started_at,
            planned_end_at=started_at + timedelta(hours=2),
        ),
        actor_id=user_id,
    )
    service.start_session(planned.id, actor_id=user_id, started_at=started_at)
    service.complete_session(
        planned.id,
        SessionCompleteRequest(
            completed_at=started_at + timedelta(hours=1),
            records=[
                TrainingRecordCreate(
                    exercise_id=plan.exercises[0].id,
                    duration_minutes=duration_minutes,
                    distance_km=distance_km,
                    load_kg=load_kg,
                    perceived_exertion=7,
                    completion_percent=100,
                )
            ],
        ),
        actor_id=user_id,
    )


def _reasons(decision: EligibilityDecision) -> list[EligibilityReason]:
    return [EligibilityReason.model_validate(item) for item in decision.reasons_json]


def _register(session, expedition_id, user_id, key):
    return ExpeditionService(session).register(
        expedition_id, RegistrationCreate(user_id=user_id, idempotency_key=key)
    )


# --- rule combinations -------------------------------------------------------


def test_all_rules_pass_confirms_registration(session) -> None:
    rules = EligibilityRules(
        training_requirements=[
            TrainingRequirementRule(
                training_type=TrainingType.ENDURANCE,
                window_days=14,
                metric="duration_minutes",
                minimum=120,
            )
        ],
        minimum_outdoor_level=2,
        require_emergency_contact=True,
    )
    organizer, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="hiker@example.com", name="Hiker")
    now = utc_now()
    _add_completed_session(
        session,
        user_id=applicant,
        training_type=TrainingType.ENDURANCE,
        started_at=now - timedelta(days=3),
        duration_minutes=150,
        distance_km=12,
    )
    UserService(session).add_experience(
        applicant,
        OutdoorExperienceCreate(
            title="Alpine course",
            level=3,
            valid_from=now - timedelta(days=30),
            valid_until=now + timedelta(days=300),
        ),
        actor_id=applicant,
    )
    UserService(session).add_contact(
        applicant,
        EmergencyContactCreate(
            name="Contact", relationship_label="Family", phone="13800000000", priority=1
        ),
        actor_id=applicant,
    )

    result = _register(session, expedition_id, applicant, "register-hiker-1")
    assert result.status == RegistrationStatus.CONFIRMED
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    assert decision.outcome == EligibilityOutcome.APPROVED.value
    assert all(reason.passed for reason in _reasons(decision))


def test_training_threshold_denies_and_older_training_outside_window_ignored(session) -> None:
    rules = EligibilityRules(
        training_requirements=[
            TrainingRequirementRule(
                training_type=TrainingType.ENDURANCE,
                window_days=7,
                metric="duration_minutes",
                minimum=200,
            )
        ]
    )
    _, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="lazy@example.com", name="Lazy")
    _add_completed_session(
        session,
        user_id=applicant,
        training_type=TrainingType.ENDURANCE,
        started_at=utc_now() - timedelta(days=20),
        duration_minutes=300,
    )
    result = _register(session, expedition_id, applicant, "register-lazy-1")
    assert result.status == RegistrationStatus.REJECTED
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    reason = next(item for item in _reasons(decision) if item.code.startswith("endurance"))
    assert reason.passed is False
    assert reason.actual == 0
    assert reason.expected == 200


def test_loaded_walk_requirement_counts_only_loaded_walk_type(session) -> None:
    rules = EligibilityRules(
        training_requirements=[
            TrainingRequirementRule(
                training_type=TrainingType.LOADED_WALK,
                window_days=10,
                metric="session_count",
                minimum=2,
            )
        ]
    )
    _, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="loaded@example.com", name="Loaded")
    _add_completed_session(
        session,
        user_id=applicant,
        training_type=TrainingType.ENDURANCE,
        started_at=utc_now() - timedelta(days=1),
    )
    result = _register(session, expedition_id, applicant, "register-loaded-1")
    assert result.status == RegistrationStatus.REJECTED


def test_session_count_metric_aggregates_multiple_sessions(session) -> None:
    rules = EligibilityRules(
        training_requirements=[
            TrainingRequirementRule(
                training_type=TrainingType.LOADED_WALK,
                window_days=14,
                metric="session_count",
                minimum=2,
            )
        ]
    )
    _, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="multi@example.com", name="Multi")
    for days_ago in (2, 4):
        _add_completed_session(
            session,
            user_id=applicant,
            training_type=TrainingType.LOADED_WALK,
            started_at=utc_now() - timedelta(days=days_ago),
            load_kg=15,
        )
    result = _register(session, expedition_id, applicant, "register-multi-1")
    assert result.status == RegistrationStatus.CONFIRMED


def test_experience_recency_rule_requires_recently_issued_certificate(session) -> None:
    rules = EligibilityRules(minimum_outdoor_level=2, experience_recency_days=180)
    _, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="recency@example.com", name="Recency")
    now = utc_now()
    users = UserService(session)
    # A high-level, still-valid certificate issued long ago fails recency.
    users.add_experience(
        applicant,
        OutdoorExperienceCreate(
            title="Old but valid",
            level=4,
            valid_from=now - timedelta(days=400),
            valid_until=now + timedelta(days=400),
        ),
        actor_id=applicant,
    )
    rejected = _register(session, expedition_id, applicant, "register-recency-1")
    assert rejected.status == RegistrationStatus.REJECTED
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    recency_reason = next(item for item in _reasons(decision) if item.code == "outdoor_recency")
    assert recency_reason.passed is False

    users.add_experience(
        applicant,
        OutdoorExperienceCreate(
            title="Recent level 2",
            level=2,
            valid_from=now - timedelta(days=10),
            valid_until=now + timedelta(days=300),
        ),
        actor_id=applicant,
    )
    approved = _register(session, expedition_id, applicant, "register-recency-2")
    assert approved.status == RegistrationStatus.CONFIRMED


def test_expired_outdoor_experience_is_denied_then_fresh_one_passes(session) -> None:
    rules = EligibilityRules(minimum_outdoor_level=3)
    _, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="expired@example.com", name="Expired")
    now = utc_now()
    users = UserService(session)
    users.add_experience(
        applicant,
        OutdoorExperienceCreate(
            title="Expired cert",
            level=5,
            valid_from=now - timedelta(days=400),
            valid_until=now - timedelta(days=1),
        ),
        actor_id=applicant,
    )
    rejected = _register(session, expedition_id, applicant, "register-expired-1")
    assert rejected.status == RegistrationStatus.REJECTED
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    facts = decision.input_facts["outdoor_experience"]
    assert facts["expired_count"] == 1
    assert facts["valid_count"] == 0

    users.add_experience(
        applicant,
        OutdoorExperienceCreate(
            title="Fresh cert",
            level=3,
            valid_from=now - timedelta(days=2),
            valid_until=now + timedelta(days=100),
        ),
        actor_id=applicant,
    )
    approved = _register(session, expedition_id, applicant, "register-expired-2")
    assert approved.status == RegistrationStatus.CONFIRMED


def test_health_deny_rule_rejects_while_review_rule_pends(session) -> None:
    organizer, expedition_id, _ = _open_expedition_with_policy(
        session,
        EligibilityRules(
            health_restriction_rules=[
                HealthRestrictionRule(restriction_name="heart condition", action="deny"),
            ]
        ),
    )
    denied_user = create_user(session, email="heart@example.com", name="Heart")
    UserService(session).add_restriction(
        denied_user,
        HealthRestrictionCreate(name="Heart condition", severity=4),
        actor_id=denied_user,
    )
    denied = _register(session, expedition_id, denied_user, "register-heart-1")
    assert denied.status == RegistrationStatus.REJECTED

    EligibilityService(session).publish_policy(
        expedition_id,
        EligibilityPolicyUpsert(
            rules=EligibilityRules(
                health_restriction_rules=[
                    HealthRestrictionRule(restriction_name="asthma", action="review"),
                ]
            )
        ),
        actor_id=organizer,
    )
    review_user = create_user(session, email="asthma@example.com", name="Asthma")
    UserService(session).add_restriction(
        review_user,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=review_user,
    )
    pending = _register(session, expedition_id, review_user, "register-asthma-1")
    assert pending.status == RegistrationStatus.PENDING
    decision = EligibilityService(session).decisions.latest_for(expedition_id, review_user)
    assert decision.outcome == EligibilityOutcome.MANUAL_REVIEW.value
    roster = ExpeditionService(session).roster(expedition_id)
    assert roster.pending_count == 1
    # Only the organizer leader occupies a confirmed slot; the pending review
    # reserves time consideration but never a capacity place.
    assert roster.confirmed_count == 1
    assert roster.available_places == roster.capacity - 1


def test_wildcard_health_rule_matches_any_active_restriction(session) -> None:
    _, expedition_id, _ = _open_expedition_with_policy(
        session,
        EligibilityRules(
            health_restriction_rules=[
                HealthRestrictionRule(restriction_name="*", action="review")
            ]
        ),
    )
    applicant = create_user(session, email="any@example.com", name="Any")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Allergy", severity=1),
        actor_id=applicant,
    )
    result = _register(session, expedition_id, applicant, "register-any-1")
    assert result.status == RegistrationStatus.PENDING


def test_missing_emergency_contact_is_rejected(session) -> None:
    _, expedition_id, _ = _open_expedition_with_policy(
        session, EligibilityRules(require_emergency_contact=True)
    )
    applicant = create_user(session, email="nocontact@example.com", name="No Contact")
    result = _register(session, expedition_id, applicant, "register-nocontact-1")
    assert result.status == RegistrationStatus.REJECTED


def test_inactive_restriction_does_not_trigger_rule(session) -> None:
    _, expedition_id, _ = _open_expedition_with_policy(
        session,
        EligibilityRules(
            health_restriction_rules=[
                HealthRestrictionRule(restriction_name="knee strain", action="deny"),
            ]
        ),
    )
    applicant = create_user(session, email="knee@example.com", name="Knee")
    users = UserService(session)
    restriction = users.add_restriction(
        applicant,
        HealthRestrictionCreate(name="Knee strain", severity=2),
        actor_id=applicant,
    )
    users.update_restriction(
        applicant,
        restriction.id,
        HealthRestrictionUpdate(is_active=False),
        actor_id=applicant,
    )
    result = _register(session, expedition_id, applicant, "register-knee-1")
    assert result.status == RegistrationStatus.CONFIRMED


# --- timezone time windows ---------------------------------------------------


def test_training_window_uses_utc_instant_when_supplied_with_offset(session) -> None:
    rules = EligibilityRules(
        training_requirements=[
            TrainingRequirementRule(
                training_type=TrainingType.ENDURANCE,
                window_days=7,
                metric="duration_minutes",
                minimum=100,
            )
        ]
    )
    _, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="tz@example.com", name="TZ")
    shanghai = ZoneInfo("Asia/Shanghai")
    # Expressed in Shanghai wall-clock but comfortably inside the 7-day window.
    started = datetime.now(shanghai) - timedelta(days=6, hours=20)
    _add_completed_session(
        session,
        user_id=applicant,
        training_type=TrainingType.ENDURANCE,
        started_at=started,
        duration_minutes=100,
    )
    result = _register(session, expedition_id, applicant, "register-tz-1")
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    reason = next(item for item in _reasons(decision) if item.code.startswith("endurance"))
    window_start = datetime.fromisoformat(
        decision.input_facts["training"][reason.code]["window_start"]
    )
    assert window_start.tzinfo is not None
    assert window_start.utcoffset() == timedelta(0)
    assert result.status == RegistrationStatus.CONFIRMED


def test_experience_expiry_boundary_uses_instant_not_local_date(session) -> None:
    rules = EligibilityRules(minimum_outdoor_level=2)
    _, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="boundary@example.com", name="Boundary")
    valid_until = (utc_now() + timedelta(hours=3)).astimezone(ZoneInfo("Asia/Shanghai"))
    UserService(session).add_experience(
        applicant,
        OutdoorExperienceCreate(
            title="Almost expired",
            level=2,
            valid_from=utc_now() - timedelta(days=10),
            valid_until=valid_until,
        ),
        actor_id=applicant,
    )
    result = _register(session, expedition_id, applicant, "register-boundary-1")
    assert result.status == RegistrationStatus.CONFIRMED


# --- review workflow ---------------------------------------------------------


def test_only_organizer_may_review_decision(session) -> None:
    rules = EligibilityRules(
        health_restriction_rules=[
            HealthRestrictionRule(restriction_name="asthma", action="review")
        ]
    )
    organizer, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="review1@example.com", name="Review1")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    _register(session, expedition_id, applicant, "register-review1")
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    stranger = create_user(session, email="stranger@example.com", name="Stranger")
    with pytest.raises(UnauthorizedOperationError):
        EligibilityService(session).review(
            decision.id,
            EligibilityReviewRequest(
                decision="approve", reason="looks fine", expected_version=decision.version
            ),
            actor_id=stranger,
        )
    # The applicant also cannot self-approve.
    with pytest.raises(UnauthorizedOperationError):
        EligibilityService(session).review(
            decision.id,
            EligibilityReviewRequest(
                decision="approve", reason="self", expected_version=decision.version
            ),
            actor_id=applicant,
        )


def test_review_approve_confirms_and_freezes_decision(session) -> None:
    rules = EligibilityRules(
        health_restriction_rules=[
            HealthRestrictionRule(restriction_name="asthma", action="review")
        ]
    )
    organizer, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="review2@example.com", name="Review2")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    pending = _register(session, expedition_id, applicant, "register-review2")
    assert pending.status == RegistrationStatus.PENDING
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)

    reviewed = EligibilityService(session).review(
        decision.id,
        EligibilityReviewRequest(
            decision="approve",
            reason="Recent medical clearance provided",
            expected_version=decision.version,
        ),
        actor_id=organizer,
    )
    assert reviewed.review_decision == "approve"
    assert reviewed.review_facts["resulting_status"] == "confirmed"
    assert reviewed.reviewed_by == organizer
    assert reviewed.review_reason == "Recent medical clearance provided"
    stored = session.get(ExpeditionRegistration, pending.id)
    assert stored.status == RegistrationStatus.CONFIRMED

    with pytest.raises((InvalidStateError, ConflictError)):
        EligibilityService(session).review(
            decision.id,
            EligibilityReviewRequest(
                decision="reject", reason="changed mind", expected_version=reviewed.version
            ),
            actor_id=organizer,
        )


def test_review_reject_marks_registration_rejected(session) -> None:
    rules = EligibilityRules(
        health_restriction_rules=[
            HealthRestrictionRule(restriction_name="asthma", action="review")
        ]
    )
    organizer, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="review3@example.com", name="Review3")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    pending = _register(session, expedition_id, applicant, "register-review3")
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    reviewed = EligibilityService(session).review(
        decision.id,
        EligibilityReviewRequest(
            decision="reject",
            reason="Documentation missing",
            expected_version=decision.version,
        ),
        actor_id=organizer,
    )
    assert reviewed.review_decision == "reject"
    assert session.get(ExpeditionRegistration, pending.id).status == RegistrationStatus.REJECTED


def test_review_reason_is_required() -> None:
    with pytest.raises(PydanticValidationError):
        EligibilityReviewRequest(decision="approve", reason="   ", expected_version=1)


def test_concurrent_review_optimistic_lock_conflict(session) -> None:
    import threading

    from sqlalchemy.orm import Session

    rules = EligibilityRules(
        health_restriction_rules=[
            HealthRestrictionRule(restriction_name="asthma", action="review")
        ]
    )
    organizer, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="concurrent@example.com", name="Concurrent")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    _register(session, expedition_id, applicant, "register-concurrent")
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    stale_version = decision.version
    decision_id = decision.id
    EligibilityService(session).review(
        decision_id,
        EligibilityReviewRequest(
            decision="approve", reason="first approval", expected_version=stale_version
        ),
        actor_id=organizer,
    )
    session.commit()
    # A different client/session acting after the first review must be refused
    # (either by the already-reviewed guard or the atomic version claim).
    with Session(session.bind) as other_session:
        with pytest.raises(ConflictError):
            EligibilityService(other_session).review(
                decision_id,
                EligibilityReviewRequest(
                    decision="reject",
                    reason="late rejection",
                    expected_version=stale_version,
                ),
                actor_id=organizer,
            )
        other_session.rollback()

    # True cross-thread concurrency on one fresh pending decision: exactly one
    # reviewer must win even though both start at version 1.
    raced_user = create_user(session, email="raceshared@example.com", name="RaceShared")
    UserService(session).add_restriction(
        raced_user,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=raced_user,
    )
    _register(session, expedition_id, raced_user, "register-race-shared")
    raced_decision_id = EligibilityService(session).decisions.latest_for(
        expedition_id, raced_user
    ).id
    session.commit()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def race() -> None:
        barrier.wait()
        worker_session = Session(session.bind)
        try:
            EligibilityService(worker_session).review(
                raced_decision_id,
                EligibilityReviewRequest(
                    decision="approve", reason="race", expected_version=1
                ),
                actor_id=organizer,
            )
            worker_session.commit()
            outcomes.append("ok")
        except (ConflictError, InvalidStateError):
            worker_session.rollback()
            outcomes.append("lost")
        finally:
            worker_session.close()

    threads = [threading.Thread(target=race), threading.Thread(target=race)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["lost", "ok"]

    # The winning review is committed exactly once with a single version bump.
    session.expire_all()
    final = session.get(EligibilityDecision, raced_decision_id)
    assert final.version == 2
    assert final.review_decision == "approve"


def test_concurrent_approvals_never_oversell_last_seat(session) -> None:
    # Capacity is 2 and the organizer already holds one confirmed seat, so only
    # ONE of two simultaneously approved pending applicants may be confirmed.
    import threading

    from sqlalchemy.orm import Session

    rules = EligibilityRules(
        health_restriction_rules=[
            HealthRestrictionRule(restriction_name="asthma", action="review")
        ]
    )
    organizer, expedition_id, _ = _open_expedition_with_policy(session, rules, capacity=2)
    decision_ids = []
    registration_ids = []
    for tag in ("one", "two"):
        applicant = create_user(session, email=f"seat-{tag}@example.com", name=f"Seat {tag}")
        UserService(session).add_restriction(
            applicant,
            HealthRestrictionCreate(name="Asthma", severity=2),
            actor_id=applicant,
        )
        registration = _register(session, expedition_id, applicant, f"key-seat-{tag}-xx")
        decision_ids.append(
            EligibilityService(session).decisions.latest_for(
                expedition_id, applicant
            ).id
        )
        registration_ids.append(registration.id)
    session.commit()

    barrier = threading.Barrier(2)

    def approve(decision_id: int) -> None:
        barrier.wait()
        worker = Session(session.bind)
        try:
            EligibilityService(worker).review(
                decision_id,
                EligibilityReviewRequest(
                    decision="approve", reason="ok", expected_version=1
                ),
                actor_id=organizer,
            )
            worker.commit()
        except Exception:
            worker.rollback()
        finally:
            worker.close()

    threads = [
        threading.Thread(target=approve, args=(decision_id,))
        for decision_id in decision_ids
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    session.expire_all()
    statuses = [
        session.get(ExpeditionRegistration, registration_id).status
        for registration_id in registration_ids
    ]
    assert sorted(statuses) == ["confirmed", "waitlisted"]
    assert ExpeditionService(session).expeditions.confirmed_count(expedition_id) == 2
    roster = ExpeditionService(session).roster(expedition_id)
    assert roster.available_places == 0


def test_review_approve_rechecks_capacity_and_uses_waitlist(session) -> None:
    rules = EligibilityRules(
        health_restriction_rules=[
            HealthRestrictionRule(restriction_name="asthma", action="review")
        ]
    )
    organizer, expedition_id, _ = _open_expedition_with_policy(session, rules, capacity=2)
    # Organizer already holds one confirmed slot; fill the last one.
    filler = create_user(session, email="filler@example.com", name="Filler")
    _register(session, expedition_id, filler, "register-filler")
    applicant = create_user(session, email="waitreview@example.com", name="WaitReview")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    _register(session, expedition_id, applicant, "register-waitreview")
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    reviewed = EligibilityService(session).review(
        decision.id,
        EligibilityReviewRequest(
            decision="approve", reason="ok", expected_version=decision.version
        ),
        actor_id=organizer,
    )
    assert reviewed.review_facts["resulting_status"] == "waitlisted"
    assert reviewed.review_facts["confirmed_count"] == 2


def test_review_approve_rechecks_time_conflict(session) -> None:
    rules = EligibilityRules(
        health_restriction_rules=[
            HealthRestrictionRule(restriction_name="asthma", action="review")
        ]
    )
    organizer, first_expedition, _ = _open_expedition_with_policy(
        session, rules, offset_days=10
    )
    second_route = create_route(session, actor_id=organizer, name="Other route")
    second_expedition = create_expedition(
        session, organizer_id=organizer, route_id=second_route, offset_days=10
    )
    EligibilityService(session).publish_policy(
        second_expedition,
        EligibilityPolicyUpsert(rules=rules),
        actor_id=organizer,
    )
    ExpeditionService(session).change_status(
        second_expedition,
        ActivityStateChange(target_status="open", actor_id=organizer),
    )
    applicant = create_user(session, email="busyreview@example.com", name="BusyReview")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    # Both applications are pending; pending does not hard-block the schedule.
    first_reg = _register(session, first_expedition, applicant, "register-busy-first")
    _register(session, second_expedition, applicant, "register-busy-second")
    first_decision = EligibilityService(session).decisions.latest_for(
        first_expedition, applicant
    )
    EligibilityService(session).review(
        first_decision.id,
        EligibilityReviewRequest(
            decision="approve", reason="first ok", expected_version=first_decision.version
        ),
        actor_id=organizer,
    )
    # Now the first registration is confirmed and occupies the time slot.
    assert session.get(ExpeditionRegistration, first_reg.id).status == (
        RegistrationStatus.CONFIRMED
    )
    second_decision = EligibilityService(session).decisions.latest_for(
        second_expedition, applicant
    )
    with pytest.raises(ConflictError, match="another confirmed expedition"):
        EligibilityService(session).review(
            second_decision.id,
            EligibilityReviewRequest(
                decision="approve", reason="second ok", expected_version=second_decision.version
            ),
            actor_id=organizer,
        )
    # The failed review leaves the second registration pending and unlocked.
    session.expire_all()
    second_reg = ExpeditionService(session).expeditions.get_registration(
        second_expedition, applicant
    )
    assert second_reg.status == RegistrationStatus.PENDING


# --- policy versioning and frozen history ------------------------------------


def test_policy_update_only_affects_future_applications(session) -> None:
    organizer, expedition_id, policy_v1 = _open_expedition_with_policy(
        session, EligibilityRules(require_emergency_contact=True)
    )
    applicant = create_user(session, email="versioned@example.com", name="Versioned")
    UserService(session).add_contact(
        applicant,
        EmergencyContactCreate(
            name="Contact", relationship_label="Family", phone="13800000000", priority=1
        ),
        actor_id=applicant,
    )
    _register(session, expedition_id, applicant, "register-versioned-1")
    first_decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    assert first_decision.policy_version == 1
    assert first_decision.rules_json["require_emergency_contact"] is True

    EligibilityService(session).publish_policy(
        expedition_id,
        EligibilityPolicyUpsert(
            rules=EligibilityRules(minimum_outdoor_level=5),
            change_note="stricter",
            expected_version=policy_v1.version,
        ),
        actor_id=organizer,
    )
    session.expire_all()
    frozen = session.get(EligibilityDecision, first_decision.id)
    assert frozen.policy_version == 1
    assert frozen.rules_json["require_emergency_contact"] is True
    assert "minimum_outdoor_level" not in frozen.rules_json

    versions = EligibilityService(session).list_policy_versions(expedition_id)
    assert [item.version for item in versions] == [1, 2]
    assert versions[0].is_active is False
    assert versions[1].is_active is True


def test_profile_change_after_decision_does_not_rewrite_history(session) -> None:
    _, expedition_id, _ = _open_expedition_with_policy(
        session, EligibilityRules(minimum_outdoor_level=3)
    )
    applicant = create_user(session, email="history@example.com", name="History")
    now = utc_now()
    users = UserService(session)
    users.add_experience(
        applicant,
        OutdoorExperienceCreate(
            title="Cert",
            level=3,
            valid_from=now - timedelta(days=1),
            valid_until=now + timedelta(days=2),
        ),
        actor_id=applicant,
    )
    _register(session, expedition_id, applicant, "register-history-1")
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    assert decision.outcome == EligibilityOutcome.APPROVED.value

    experience = users.users.list_outdoor_experiences(applicant)[0]
    experience.valid_until = now - timedelta(days=1)
    session.flush()
    session.expire_all()
    frozen = session.get(EligibilityDecision, decision.id)
    assert frozen.outcome == EligibilityOutcome.APPROVED.value
    assert frozen.input_facts["outdoor_experience"]["valid_count"] == 1
    assert frozen.reasons_json


def test_concurrent_policy_publish_requires_expected_version(session) -> None:
    organizer, expedition_id, policy_v1 = _open_expedition_with_policy(
        session, EligibilityRules(require_emergency_contact=True)
    )
    service = EligibilityService(session)
    service.publish_policy(
        expedition_id,
        EligibilityPolicyUpsert(
            rules=EligibilityRules(minimum_outdoor_level=2),
            expected_version=policy_v1.version,
        ),
        actor_id=organizer,
    )
    with pytest.raises(ConflictError):
        service.publish_policy(
            expedition_id,
            EligibilityPolicyUpsert(
                rules=EligibilityRules(minimum_outdoor_level=3),
                expected_version=policy_v1.version,
            ),
            actor_id=organizer,
        )


def test_non_organizer_cannot_publish_policy(session) -> None:
    _, expedition_id, _ = _open_expedition_with_policy(
        session, EligibilityRules(require_emergency_contact=True)
    )
    stranger = create_user(session, email="intruder@example.com", name="Intruder")
    with pytest.raises(UnauthorizedOperationError):
        EligibilityService(session).publish_policy(
            expedition_id,
            EligibilityPolicyUpsert(rules=EligibilityRules(minimum_outdoor_level=1)),
            actor_id=stranger,
        )


# --- audit sanitization ------------------------------------------------------


def test_audit_trail_never_exposes_health_names_or_contact_phones(session) -> None:
    rules = EligibilityRules(
        require_emergency_contact=True,
        health_restriction_rules=[
            HealthRestrictionRule(restriction_name="secret condition", action="review")
        ],
    )
    _, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="private@example.com", name="Private")
    users = UserService(session)
    users.add_restriction(
        applicant,
        HealthRestrictionCreate(
            name="Secret condition", severity=5, description="private diagnosis notes"
        ),
        actor_id=applicant,
    )
    users.add_contact(
        applicant,
        EmergencyContactCreate(
            name="Closest Person",
            relationship_label="Spouse",
            phone="13912345678",
            priority=1,
        ),
        actor_id=applicant,
    )
    _register(session, expedition_id, applicant, "register-private-1")
    decision_logs = list(
        session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "eligibility_decision")
        )
    )
    assert decision_logs
    serialized = str(decision_logs)
    assert "secret condition" not in serialized.lower()
    assert "13912345678" not in serialized
    assert "private diagnosis" not in serialized
    # The decision record itself keeps the structured facts (applicant evidence),
    # while the audit log only stores counters.
    assert decision_logs[0].context["review_rule_count"] == 1
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    assert decision.input_facts["emergency_contact"]["count"] == 1


def test_policy_audit_contains_counts_not_contents(session) -> None:
    organizer = create_user(session, email="pa@example.com", name="PA")
    route = create_route(session, actor_id=organizer)
    expedition_id = create_expedition(session, organizer_id=organizer, route_id=route)
    EligibilityService(session).publish_policy(
        expedition_id,
        EligibilityPolicyUpsert(
            rules=EligibilityRules(
                training_requirements=[
                    TrainingRequirementRule(
                        training_type=TrainingType.ENDURANCE,
                        window_days=14,
                        metric="duration_minutes",
                        minimum=120,
                    )
                ],
                health_restriction_rules=[
                    HealthRestrictionRule(restriction_name="a", action="deny"),
                    HealthRestrictionRule(restriction_name="b", action="review"),
                ],
                require_emergency_contact=True,
            )
        ),
        actor_id=organizer,
    )
    log = session.scalar(select(AuditLog).where(AuditLog.entity_type == "eligibility_policy"))
    assert log.after_state["training_rule_count"] == 1
    assert log.after_state["health_rule_count"] == 2
    assert "restriction" not in str(log.after_state).lower()


# --- legacy compatibility ----------------------------------------------------


def test_expedition_without_policy_keeps_legacy_fitness_gate(session) -> None:
    organizer = create_user(session, email="legacyorg@example.com", name="LegacyOrg")
    route = create_route(session, actor_id=organizer)
    expedition_id = create_expedition(session, organizer_id=organizer, route_id=route)
    ExpeditionService(session).change_status(
        expedition_id,
        ActivityStateChange(target_status="open", actor_id=organizer),
    )
    applicant = create_user(
        session, email="legacy@example.com", name="Legacy", fitness="beginner"
    )
    result = _register(session, expedition_id, applicant, "register-legacy-1")
    assert result.status == RegistrationStatus.CONFIRMED
    assert result.latest_decision_id is None
    assert EligibilityService(session).active_policy(expedition_id) is None
    assert (
        session.scalar(
            select(EligibilityDecision).where(
                EligibilityDecision.expedition_id == expedition_id
            )
        )
        is None
    )


def test_precheck_without_policy_returns_approved(session) -> None:
    organizer = create_user(session, email="preorg@example.com", name="PreOrg")
    route = create_route(session, actor_id=organizer)
    expedition_id = create_expedition(session, organizer_id=organizer, route_id=route)
    applicant = create_user(session, email="precheck@example.com", name="Precheck")
    evaluation = EligibilityService(session).precheck(expedition_id, applicant)
    assert evaluation.outcome == EligibilityOutcome.APPROVED
    assert evaluation.policy_version is None
    assert evaluation.reasons == []


def test_pending_withdrawal_blocks_later_review(session) -> None:
    rules = EligibilityRules(
        health_restriction_rules=[
            HealthRestrictionRule(restriction_name="asthma", action="review")
        ]
    )
    organizer, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="withdrawp@example.com", name="WithdrawP")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    pending = _register(session, expedition_id, applicant, "register-withdrawp-1")
    decision = EligibilityService(session).decisions.latest_for(expedition_id, applicant)
    assert pending.status == RegistrationStatus.PENDING

    from trailforge.schemas.activities import WithdrawalRequest

    ExpeditionService(session).withdraw(
        expedition_id,
        WithdrawalRequest(
            user_id=applicant,
            reason="changed plans",
            idempotency_key="withdraw-withdrawp-1",
        ),
    )
    # The frozen decision still says manual_review, but reviewing it now fails
    # because the registration it points at is no longer pending.
    with pytest.raises(InvalidStateError, match="no longer pending"):
        EligibilityService(session).review(
            decision.id,
            EligibilityReviewRequest(
                decision="approve", reason="too late", expected_version=decision.version
            ),
            actor_id=organizer,
        )


def test_legacy_fitness_gate_still_rejects_unqualified_applicant(session) -> None:
    organizer = create_user(session, email="strictorg@example.com", name="StrictOrg")
    route = create_route(session, actor_id=organizer)
    expedition_id = create_expedition(session, organizer_id=organizer, route_id=route)
    expedition = ExpeditionService(session).expeditions.require(expedition_id)
    expedition.minimum_fitness_level = 4
    session.flush()
    ExpeditionService(session).change_status(
        expedition_id,
        ActivityStateChange(target_status="open", actor_id=organizer),
    )
    applicant = create_user(
        session, email="notfit@example.com", name="NotFit", fitness="beginner"
    )
    from trailforge.errors import ValidationError

    with pytest.raises(ValidationError, match="fitness level"):
        _register(session, expedition_id, applicant, "register-notfit-1")


# --- idempotency -------------------------------------------------------------


def test_repeated_registration_idempotency_key_never_creates_second_placeholder(
    session,
) -> None:
    rules = EligibilityRules(
        health_restriction_rules=[
            HealthRestrictionRule(restriction_name="asthma", action="review")
        ]
    )
    _, expedition_id, _ = _open_expedition_with_policy(session, rules)
    applicant = create_user(session, email="idem2@example.com", name="Idem2")
    UserService(session).add_restriction(
        applicant,
        HealthRestrictionCreate(name="Asthma", severity=2),
        actor_id=applicant,
    )
    service = ExpeditionService(session)
    request = RegistrationCreate(user_id=applicant, idempotency_key="same-pending-key")
    first = service.register(expedition_id, request)
    second = service.register(expedition_id, request)
    assert first.id == second.id
    assert first.status == RegistrationStatus.PENDING
    decisions = session.scalars(
        select(EligibilityDecision).where(
            EligibilityDecision.expedition_id == expedition_id,
            EligibilityDecision.user_id == applicant,
        )
    ).all()
    assert len(decisions) == 1

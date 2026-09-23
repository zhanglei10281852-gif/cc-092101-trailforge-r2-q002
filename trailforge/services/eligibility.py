from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from trailforge.database.base import utc_now
from trailforge.domain.enums import (
    ActivityStatus,
    AuditAction,
    EligibilityDecision,
    EligibilityRuleOutcome,
    RegistrationStatus,
    ReviewDecision,
)
from trailforge.errors import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    UnauthorizedOperationError,
)
from trailforge.models.activities import Expedition
from trailforge.models.eligibility import (
    EligibilityEvaluation,
    EligibilityPolicy,
    EligibilityReview,
)
from trailforge.repositories.activities import ExpeditionRepository
from trailforge.repositories.eligibility import EligibilityRepository
from trailforge.repositories.training import TrainingRepository
from trailforge.repositories.users import UserRepository
from trailforge.schemas.activities import RegistrationResponse
from trailforge.schemas.eligibility import (
    EligibilityEvaluationResponse,
    EligibilityPolicyResponse,
    EligibilityPolicyUpsert,
    EligibilityPrecheckResponse,
    EligibilityReason,
    EligibilityReviewCreate,
    PendingReviewItem,
    PendingReviewList,
)
from trailforge.services.base import ServiceBase

RULE_BLOCKED_RESTRICTIONS = "blocked_restrictions"
RULE_REVIEW_RESTRICTIONS = "review_restrictions"
RULE_MIN_ENDURANCE_MINUTES = "min_endurance_minutes"
RULE_MIN_LOADED_SESSIONS = "min_loaded_sessions"
RULE_MIN_COMPLETED_EXPEDITIONS = "min_completed_expeditions"
RULE_MIN_EMERGENCY_CONTACTS = "min_emergency_contacts"

_REVIEWABLE_EXPEDITION_STATUSES = {
    ActivityStatus.DRAFT,
    ActivityStatus.OPEN,
    ActivityStatus.ASSEMBLING,
}


class EligibilityService(ServiceBase):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.policies = EligibilityRepository(session)
        self.expeditions = ExpeditionRepository(session)
        self.users = UserRepository(session)
        self.training = TrainingRepository(session)

    # ------------------------------------------------------------------
    # Policy maintenance
    # ------------------------------------------------------------------
    def upsert_policy(
        self, expedition_id: int, data: EligibilityPolicyUpsert
    ) -> EligibilityPolicyResponse:
        expedition = self.expeditions.get_detail(expedition_id, for_update=True)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        self.users.require(data.actor_id)
        if data.actor_id != expedition.organizer_id:
            raise UnauthorizedOperationError(
                "only the expedition organizer can maintain eligibility policies"
            )
        self.policies.deactivate_all(expedition.id)
        policy = EligibilityPolicy(
            expedition_id=expedition.id,
            version=self.policies.max_version(expedition.id) + 1,
            is_active=True,
            training_window_days=data.training_window_days,
            min_endurance_minutes=data.min_endurance_minutes,
            min_loaded_sessions=data.min_loaded_sessions,
            min_completed_expeditions=data.min_completed_expeditions,
            experience_window_days=data.experience_window_days,
            blocked_restrictions=list(data.blocked_restrictions),
            review_restrictions=list(data.review_restrictions),
            min_emergency_contacts=data.min_emergency_contacts,
            note=data.note.strip(),
            created_by=data.actor_id,
        )
        self.session.add(policy)
        self.session.flush()
        self.audit(
            actor_id=data.actor_id,
            entity_type="eligibility_policy",
            entity_id=policy.id,
            action=AuditAction.CREATED,
            after=self.snapshot(policy),
            context={"expedition_id": expedition.id, "version": policy.version},
        )
        return EligibilityPolicyResponse.model_validate(policy)

    def get_active_policy(self, expedition_id: int) -> EligibilityPolicyResponse:
        self._require_expedition(expedition_id)
        policy = self.policies.get_active(expedition_id)
        if policy is None:
            raise NotFoundError(f"Expedition {expedition_id} has no active eligibility policy")
        return EligibilityPolicyResponse.model_validate(policy)

    def list_policies(self, expedition_id: int) -> list[EligibilityPolicyResponse]:
        self._require_expedition(expedition_id)
        return [
            EligibilityPolicyResponse.model_validate(item)
            for item in self.policies.list_versions(expedition_id)
        ]

    def find_active_policy(self, expedition_id: int) -> EligibilityPolicy | None:
        return self.policies.get_active(expedition_id)

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    def evaluate(
        self, *, expedition_id: int, user_id: int, policy: EligibilityPolicy
    ) -> EligibilityEvaluation:
        """Build a transient evaluation; nothing is persisted here."""
        facts = self._gather_facts(user_id=user_id, policy=policy, now=utc_now())
        decision, reasons = self._evaluate_rules(policy, facts)
        return EligibilityEvaluation(
            expedition_id=expedition_id,
            user_id=user_id,
            policy_id=policy.id,
            policy_version=policy.version,
            decision=decision,
            facts=facts,
            reasons=[reason.model_dump(mode="json") for reason in reasons],
        )

    def record_evaluation(self, evaluation: EligibilityEvaluation) -> EligibilityEvaluation:
        self.session.add(evaluation)
        self.session.flush()
        matched_health = sorted(
            {
                name
                for reason in evaluation.reasons
                if reason.get("rule") in {RULE_BLOCKED_RESTRICTIONS, RULE_REVIEW_RESTRICTIONS}
                and reason.get("outcome")
                in {EligibilityRuleOutcome.FAILED, EligibilityRuleOutcome.REVIEW}
                for name in (reason.get("actual") or [])
            }
        )
        self.audit(
            actor_id=evaluation.user_id,
            entity_type="eligibility_evaluation",
            entity_id=evaluation.id,
            action=AuditAction.ELIGIBILITY_EVALUATED,
            after={
                "decision": str(evaluation.decision),
                "policy_version": evaluation.policy_version,
                "registration_id": evaluation.registration_id,
            },
            context={
                "expedition_id": evaluation.expedition_id,
                "failed_rules": [
                    reason["rule"]
                    for reason in evaluation.reasons
                    if reason.get("outcome") == EligibilityRuleOutcome.FAILED
                ],
                "health": {"matched_restrictions": matched_health},
            },
        )
        return evaluation

    def precheck(self, expedition_id: int, user_id: int) -> EligibilityPrecheckResponse:
        self._require_expedition(expedition_id)
        self.users.require(user_id)
        policy = self.policies.get_active(expedition_id)
        if policy is None:
            return EligibilityPrecheckResponse(
                expedition_id=expedition_id,
                user_id=user_id,
                policy_id=None,
                policy_version=None,
                decision=EligibilityDecision.APPROVED,
                reasons=[],
                facts={"evaluated_at": utc_now().isoformat()},
                message=(
                    "no eligibility policy is configured; "
                    "the standard fitness level check applies"
                ),
            )
        evaluation = self.evaluate(expedition_id=expedition_id, user_id=user_id, policy=policy)
        return EligibilityPrecheckResponse(
            expedition_id=expedition_id,
            user_id=user_id,
            policy_id=policy.id,
            policy_version=policy.version,
            decision=EligibilityDecision(evaluation.decision),
            reasons=[EligibilityReason(**item) for item in evaluation.reasons],
            facts=evaluation.facts,
            message="",
        )

    def get_registration_evaluation(
        self, expedition_id: int, registration_id: int
    ) -> EligibilityEvaluationResponse:
        registration = self.expeditions.get_registration_by_id(registration_id)
        if registration is None or registration.expedition_id != expedition_id:
            raise NotFoundError(f"Registration {registration_id} was not found")
        evaluation = self.policies.latest_evaluation(registration.id)
        if evaluation is None:
            raise NotFoundError(
                f"Registration {registration_id} has no eligibility evaluation"
            )
        return EligibilityEvaluationResponse.model_validate(evaluation)

    def pending_reviews(self, expedition_id: int) -> PendingReviewList:
        self._require_expedition(expedition_id)
        items: list[PendingReviewItem] = []
        for registration in self.policies.pending_registrations(expedition_id):
            evaluation = self.policies.latest_evaluation(registration.id)
            items.append(
                PendingReviewItem(
                    registration=RegistrationResponse.model_validate(registration),
                    evaluation=(
                        EligibilityEvaluationResponse.model_validate(evaluation)
                        if evaluation is not None
                        else None
                    ),
                )
            )
        return PendingReviewList(expedition_id=expedition_id, items=items)

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------
    def review(
        self, expedition_id: int, registration_id: int, data: EligibilityReviewCreate
    ) -> RegistrationResponse:
        expedition = self.expeditions.get_detail(expedition_id, for_update=True)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        registration = self.expeditions.get_registration_by_id(registration_id, for_update=True)
        if registration is None or registration.expedition_id != expedition.id:
            raise NotFoundError(f"Registration {registration_id} was not found")
        self.users.require(data.actor_id)
        if data.actor_id != expedition.organizer_id:
            raise UnauthorizedOperationError(
                "only the expedition organizer can review pending registrations"
            )
        if ActivityStatus(expedition.status) not in _REVIEWABLE_EXPEDITION_STATUSES:
            raise InvalidStateError(
                "registrations can no longer be reviewed for this expedition"
            )
        if RegistrationStatus(registration.status) != RegistrationStatus.PENDING:
            raise InvalidStateError("only pending registrations can be reviewed")
        evaluation = self.policies.latest_evaluation(registration.id)
        if data.decision == ReviewDecision.REJECT:
            resulting = RegistrationStatus.REJECTED
        else:
            conflict = self.expeditions.conflicting_registration(
                registration.user_id,
                expedition.start_at,
                expedition.end_at,
                exclude_expedition_id=expedition.id,
            )
            if conflict is not None:
                raise ConflictError(
                    "user has another expedition during this time",
                    context={"conflicting_expedition_id": conflict.expedition_id},
                )
            confirmed = self.expeditions.confirmed_count(expedition.id)
            resulting = (
                RegistrationStatus.CONFIRMED
                if confirmed < expedition.capacity
                else RegistrationStatus.WAITLISTED
            )
        transitioned = self.expeditions.transition_registration_status(
            registration.id,
            expected_version=data.expected_version,
            expected_status=RegistrationStatus.PENDING,
            new_status=resulting,
        )
        if not transitioned:
            raise ConflictError(
                "resource was modified by another operation",
                context={"expected_version": data.expected_version},
            )
        self.session.refresh(registration)
        review = EligibilityReview(
            evaluation_id=evaluation.id if evaluation is not None else None,
            registration_id=registration.id,
            expedition_id=expedition.id,
            reviewer_id=data.actor_id,
            decision=data.decision,
            reason=data.reason.strip(),
            resulting_status=resulting,
        )
        self.session.add(review)
        self.session.flush()
        self.audit(
            actor_id=data.actor_id,
            entity_type="expedition_registration",
            entity_id=registration.id,
            action=AuditAction.ELIGIBILITY_REVIEWED,
            before={"status": RegistrationStatus.PENDING.value},
            after={"status": resulting.value},
            context={
                "expedition_id": expedition.id,
                "decision": data.decision.value,
                "reason": data.reason.strip(),
                "review_id": review.id,
                "evaluation_id": evaluation.id if evaluation is not None else None,
            },
        )
        return RegistrationResponse.model_validate(registration)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _require_expedition(self, expedition_id: int) -> Expedition:
        expedition = self.expeditions.get_detail(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        return expedition

    def _gather_facts(
        self, *, user_id: int, policy: EligibilityPolicy, now: datetime
    ) -> dict[str, Any]:
        facts: dict[str, Any] = {
            "evaluated_at": now.isoformat(),
            "fitness_rank": self.users.fitness_rank(user_id),
        }
        uses_training = (
            policy.min_endurance_minutes is not None or policy.min_loaded_sessions is not None
        )
        if uses_training:
            window_start = now - timedelta(days=policy.training_window_days or 0)
            facts["training_window_days"] = policy.training_window_days
            facts["training_window_start"] = window_start.isoformat()
            if policy.min_endurance_minutes is not None:
                facts["endurance_minutes"] = self.training.endurance_minutes(
                    user_id, since=window_start
                )
            if policy.min_loaded_sessions is not None:
                facts["loaded_sessions"] = self.training.loaded_session_count(
                    user_id, since=window_start
                )
        if policy.min_completed_expeditions is not None:
            experience_start = None
            if policy.experience_window_days is not None:
                experience_start = now - timedelta(days=policy.experience_window_days)
                facts["experience_window_days"] = policy.experience_window_days
                facts["experience_window_start"] = experience_start.isoformat()
            facts["completed_expeditions"] = self.expeditions.completed_expedition_count(
                user_id, since=experience_start
            )
        if policy.blocked_restrictions or policy.review_restrictions:
            facts["active_restrictions"] = self.users.active_restriction_names(user_id)
        if policy.min_emergency_contacts is not None:
            facts["emergency_contacts"] = self.users.contact_count(user_id)
        return facts

    def _evaluate_rules(
        self, policy: EligibilityPolicy, facts: dict[str, Any]
    ) -> tuple[EligibilityDecision, list[EligibilityReason]]:
        reasons: list[EligibilityReason] = []
        decision = EligibilityDecision.APPROVED

        def add(
            rule: str,
            outcome: EligibilityRuleOutcome,
            message: str,
            *,
            expected: Any = None,
            actual: Any = None,
        ) -> None:
            reasons.append(
                EligibilityReason(
                    rule=rule, outcome=outcome, message=message, expected=expected, actual=actual
                )
            )

        active_restrictions = set(facts.get("active_restrictions", []))
        if policy.blocked_restrictions:
            matched = sorted(set(policy.blocked_restrictions) & active_restrictions)
            if matched:
                add(
                    RULE_BLOCKED_RESTRICTIONS,
                    EligibilityRuleOutcome.FAILED,
                    "an active health restriction is prohibited by this expedition",
                    expected=list(policy.blocked_restrictions),
                    actual=matched,
                )
                decision = EligibilityDecision.REJECTED
            else:
                add(
                    RULE_BLOCKED_RESTRICTIONS,
                    EligibilityRuleOutcome.PASSED,
                    "no prohibited health restriction is active",
                    expected=list(policy.blocked_restrictions),
                    actual=[],
                )
        if policy.review_restrictions:
            matched = sorted(set(policy.review_restrictions) & active_restrictions)
            if matched:
                add(
                    RULE_REVIEW_RESTRICTIONS,
                    EligibilityRuleOutcome.REVIEW,
                    "an active health restriction requires organizer confirmation",
                    expected=list(policy.review_restrictions),
                    actual=matched,
                )
                if decision == EligibilityDecision.APPROVED:
                    decision = EligibilityDecision.PENDING_REVIEW
            else:
                add(
                    RULE_REVIEW_RESTRICTIONS,
                    EligibilityRuleOutcome.PASSED,
                    "no health restriction requires organizer confirmation",
                    expected=list(policy.review_restrictions),
                    actual=[],
                )
        if policy.min_endurance_minutes is not None:
            actual = facts["endurance_minutes"]
            if actual >= policy.min_endurance_minutes:
                outcome, message = (
                    EligibilityRuleOutcome.PASSED,
                    "endurance training volume meets the requirement",
                )
            else:
                outcome, message = (
                    EligibilityRuleOutcome.FAILED,
                    "endurance training volume is below the requirement",
                )
                decision = EligibilityDecision.REJECTED
            add(
                RULE_MIN_ENDURANCE_MINUTES,
                outcome,
                message,
                expected=policy.min_endurance_minutes,
                actual=actual,
            )
        if policy.min_loaded_sessions is not None:
            actual = facts["loaded_sessions"]
            if actual >= policy.min_loaded_sessions:
                outcome, message = (
                    EligibilityRuleOutcome.PASSED,
                    "loaded training sessions meet the requirement",
                )
            else:
                outcome, message = (
                    EligibilityRuleOutcome.FAILED,
                    "loaded training sessions are below the requirement",
                )
                decision = EligibilityDecision.REJECTED
            add(
                RULE_MIN_LOADED_SESSIONS,
                outcome,
                message,
                expected=policy.min_loaded_sessions,
                actual=actual,
            )
        if policy.min_completed_expeditions is not None:
            actual = facts["completed_expeditions"]
            if actual >= policy.min_completed_expeditions:
                outcome, message = (
                    EligibilityRuleOutcome.PASSED,
                    "outdoor experience meets the requirement",
                )
            else:
                outcome, message = (
                    EligibilityRuleOutcome.FAILED,
                    "outdoor experience is below the requirement",
                )
                decision = EligibilityDecision.REJECTED
            add(
                RULE_MIN_COMPLETED_EXPEDITIONS,
                outcome,
                message,
                expected=policy.min_completed_expeditions,
                actual=actual,
            )
        if policy.min_emergency_contacts is not None:
            actual = facts["emergency_contacts"]
            if actual >= policy.min_emergency_contacts:
                outcome, message = (
                    EligibilityRuleOutcome.PASSED,
                    "emergency contact requirement is met",
                )
            else:
                outcome, message = (
                    EligibilityRuleOutcome.FAILED,
                    "emergency contact requirement is not met",
                )
                decision = EligibilityDecision.REJECTED
            add(
                RULE_MIN_EMERGENCY_CONTACTS,
                outcome,
                message,
                expected=policy.min_emergency_contacts,
                actual=actual,
            )
        return decision, reasons

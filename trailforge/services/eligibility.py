from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from trailforge.database.base import utc_now
from trailforge.domain.enums import (
    ActivityStatus,
    AuditAction,
    EligibilityOutcome,
    HealthRuleAction,
    RegistrationStatus,
    ReviewDecision,
)
from trailforge.errors import (
    ConflictError,
    InvalidStateError,
    NotFoundError,
    UnauthorizedOperationError,
)
from trailforge.models.eligibility import EligibilityDecision, EligibilityPolicy
from trailforge.repositories.activities import ExpeditionRepository
from trailforge.repositories.eligibility import (
    EligibilityDecisionRepository,
    EligibilityPolicyRepository,
)
from trailforge.repositories.training import TrainingRepository
from trailforge.repositories.users import UserRepository
from trailforge.schemas.eligibility import (
    EligibilityDecisionResponse,
    EligibilityEvaluation,
    EligibilityPolicyResponse,
    EligibilityPolicyUpsert,
    EligibilityReason,
    EligibilityReviewRequest,
    EligibilityRules,
)
from trailforge.services.base import ServiceBase

WILDCARD_RESTRICTION = "*"


class EligibilityService(ServiceBase):
    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.policies = EligibilityPolicyRepository(session)
        self.decisions = EligibilityDecisionRepository(session)
        self.expeditions = ExpeditionRepository(session)
        self.users = UserRepository(session)
        self.training = TrainingRepository(session)

    # -- policy maintenance -------------------------------------------------

    def publish_policy(
        self,
        expedition_id: int,
        data: EligibilityPolicyUpsert,
        *,
        actor_id: int,
    ) -> EligibilityPolicyResponse:
        expedition = self.expeditions.get_detail(expedition_id, for_update=True)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        if expedition.organizer_id != actor_id:
            raise UnauthorizedOperationError("only the organizer can maintain the policy")
        if ActivityStatus(expedition.status) not in {ActivityStatus.DRAFT, ActivityStatus.OPEN}:
            raise InvalidStateError("policies can only be edited for draft or open expeditions")
        current = self.policies.active_policy(expedition_id)
        if current is not None and data.expected_version is not None:
            check_policy_version(current, data.expected_version)
        self.policies.deactivate_all(expedition_id)
        policy = EligibilityPolicy(
            expedition_id=expedition_id,
            version=self.policies.next_version(expedition_id),
            is_active=True,
            rules_json=compact_rules(data.rules),
            change_note=data.change_note,
            created_by=actor_id,
        )
        self.session.add(policy)
        self.session.flush()
        # Audit intentionally carries rule counts only; restriction names,
        # contact details and free-text notes stay out of the audit trail.
        self.audit(
            actor_id=actor_id,
            entity_type="eligibility_policy",
            entity_id=policy.id,
            action=AuditAction.POLICY_PUBLISHED,
            after={
                "expedition_id": expedition_id,
                "version": policy.version,
                "training_rule_count": len(data.rules.training_requirements),
                "health_rule_count": len(data.rules.health_restriction_rules),
                "requires_outdoor_level": data.rules.minimum_outdoor_level is not None,
                "requires_emergency_contact": data.rules.require_emergency_contact,
            },
            context={"change_note": data.change_note},
        )
        return policy_response(policy)

    def active_policy(self, expedition_id: int) -> EligibilityPolicyResponse | None:
        expedition = self.expeditions.get_detail(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        policy = self.policies.active_policy(expedition_id)
        return policy_response(policy) if policy is not None else None

    def list_policy_versions(self, expedition_id: int) -> list[EligibilityPolicyResponse]:
        expedition = self.expeditions.get_detail(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        return [policy_response(item) for item in self.policies.list_versions(expedition_id)]

    def get_policy_version(self, expedition_id: int, version: int) -> EligibilityPolicyResponse:
        policy = self.policies.get_version(expedition_id, version)
        if policy is None:
            raise NotFoundError(f"policy version {version} was not found")
        return policy_response(policy)

    # -- evaluation ---------------------------------------------------------

    def precheck(self, expedition_id: int, user_id: int) -> EligibilityEvaluation:
        expedition = self.expeditions.get_detail(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        self.users.require(user_id)
        policy = self.policies.active_policy(expedition_id)
        rules = EligibilityRules.model_validate(policy.rules_json) if policy is not None else None
        return self.evaluate(user_id=user_id, rules=rules, policy=policy, now=utc_now())

    def evaluate(
        self,
        *,
        user_id: int,
        rules: EligibilityRules | None,
        policy: EligibilityPolicy | None,
        now: datetime,
    ) -> EligibilityEvaluation:
        facts = self._gather_facts(user_id, rules, now)
        reasons = self._apply_rules(rules, facts, now) if rules is not None else []
        if any(item.effect == "deny" and not item.passed for item in reasons):
            outcome = EligibilityOutcome.REJECTED
        elif any(item.effect == "review" and not item.passed for item in reasons):
            outcome = EligibilityOutcome.MANUAL_REVIEW
        else:
            outcome = EligibilityOutcome.APPROVED
        return EligibilityEvaluation(
            outcome=outcome,
            policy_id=policy.id if policy is not None else None,
            policy_version=policy.version if policy is not None else None,
            rules=rules,
            evaluated_at=now,
            facts=facts,
            reasons=reasons,
        )

    def persist_decision(
        self,
        *,
        expedition_id: int,
        user_id: int,
        evaluation: EligibilityEvaluation,
        registration_id: int | None,
        attempt: int,
    ) -> EligibilityDecision:
        decision = EligibilityDecision(
            expedition_id=expedition_id,
            user_id=user_id,
            registration_id=registration_id,
            policy_id=evaluation.policy_id,
            policy_version=evaluation.policy_version or 0,
            attempt=attempt,
            outcome=evaluation.outcome.value,
            rules_json=compact_rules(evaluation.rules) if evaluation.rules else {},
            input_facts=evaluation.facts,
            reasons_json=[item.model_dump(mode="json") for item in evaluation.reasons],
            decided_at=evaluation.evaluated_at,
        )
        self.session.add(decision)
        self.session.flush()
        self.audit(
            actor_id=user_id,
            entity_type="eligibility_decision",
            entity_id=decision.id,
            action=AuditAction.ELIGIBILITY_EVALUATED,
            after={"outcome": evaluation.outcome.value, "policy_version": decision.policy_version},
            context={
                "expedition_id": expedition_id,
                "denied_rule_count": sum(
                    item.effect == "deny" and not item.passed for item in evaluation.reasons
                ),
                "review_rule_count": sum(
                    item.effect == "review" and not item.passed for item in evaluation.reasons
                ),
            },
        )
        return decision

    # -- manual review ------------------------------------------------------

    def review(
        self,
        decision_id: int,
        data: EligibilityReviewRequest,
        *,
        actor_id: int,
    ) -> EligibilityDecisionResponse:
        # The request already owns this (write) transaction on SQLite, so all
        # work stays in it. Mutual exclusion comes from the single conditional
        # UPDATE below: SQLite serialises the two writers and the version +
        # not-yet-reviewed predicates make the loser's claim affect zero rows.
        decision = self.decisions.get(decision_id)
        if decision is None:
            raise NotFoundError(f"EligibilityDecision {decision_id} was not found")
        expedition = self.expeditions.get_detail(decision.expedition_id, for_update=True)
        if expedition is None:
            raise NotFoundError(f"Expedition {decision.expedition_id} was not found")
        if expedition.organizer_id != actor_id:
            raise UnauthorizedOperationError("only the organizer may review eligibility")
        if decision.outcome != EligibilityOutcome.MANUAL_REVIEW.value:
            raise InvalidStateError("only pending manual-review decisions can be reviewed")
        if decision.review_decision is not None:
            raise ConflictError("decision has already been reviewed")
        registration = self.expeditions.get_registration(
            decision.expedition_id, decision.user_id, for_update=True
        )
        if registration is None or registration.status != RegistrationStatus.PENDING:
            raise InvalidStateError(
                "the registration linked to the decision is no longer pending"
            )

        reviewed_at = utc_now()
        claimed = self.decisions.claim_for_review(
            decision_id,
            expected_version=data.expected_version,
            reviewer_id=actor_id,
            review_decision=data.decision.value,
            review_reason=data.reason,
            reviewed_at=reviewed_at,
        )
        if claimed == 0:
            raise ConflictError(
                "decision was modified or reviewed by another operation",
                context={"expected_version": data.expected_version},
            )

        review_facts: dict[str, Any] = {
            "capacity": expedition.capacity,
            "reviewed_at": reviewed_at.isoformat(),
        }
        registration_id = registration.id
        if data.decision == ReviewDecision.APPROVE:
            # The capacity and schedule checks are evaluated inside the UPDATE
            # write cursor (which reads the latest committed data), not against
            # this transaction's earlier read snapshot, so concurrent approvals
            # cannot both consume the last seat.
            confirmed = self.expeditions.confirm_pending_if_room_and_no_conflict(
                registration_id,
                expedition_id=expedition.id,
                user_id=decision.user_id,
                start_at=expedition.start_at,
                end_at=expedition.end_at,
            )
            if confirmed == 1:
                target_status = RegistrationStatus.CONFIRMED
                review_facts["confirmed_count"] = self.expeditions.confirmed_count(
                    expedition.id
                )
            else:
                # Either the schedule now conflicts or the last seat is gone.
                # Distinguish with a second atomic transition: waitlisting does
                # not require capacity, so it succeeds unless there is a
                # conflict or the registration ceased to be pending.
                waitlisted = self.expeditions.waitlist_pending_if_no_conflict(
                    registration_id,
                    expedition_id=expedition.id,
                    user_id=decision.user_id,
                    start_at=expedition.start_at,
                    end_at=expedition.end_at,
                )
                if waitlisted == 1:
                    target_status = RegistrationStatus.WAITLISTED
                    review_facts["confirmed_count"] = self.expeditions.confirmed_count(
                        expedition.id
                    )
                else:
                    raise ConflictError(
                        "user has another confirmed expedition during this time",
                        context={"expedition_id": expedition.id},
                    )
        else:
            rejected = self.expeditions.reject_pending(registration_id)
            if rejected == 0:
                raise InvalidStateError(
                    "the registration linked to the decision is no longer pending"
                )
            target_status = RegistrationStatus.REJECTED
        review_facts["resulting_status"] = target_status.value
        self.session.flush()
        self.session.refresh(decision)
        decision.review_facts = review_facts
        self.session.flush()
        self.audit(
            actor_id=actor_id,
            entity_type="eligibility_decision",
            entity_id=decision_id,
            action=AuditAction.ELIGIBILITY_REVIEWED,
            before={"outcome": EligibilityOutcome.MANUAL_REVIEW.value},
            after={
                "review_decision": data.decision.value,
                "resulting_status": target_status.value,
            },
            context={"reason": data.reason, "expedition_id": expedition.id},
        )
        return EligibilityDecisionResponse.model_validate(decision)

    def get_decision(self, decision_id: int, *, actor_id: int) -> EligibilityDecisionResponse:
        decision = self.decisions.get(decision_id)
        if decision is None:
            raise NotFoundError(f"EligibilityDecision {decision_id} was not found")
        expedition = self.expeditions.get_detail(decision.expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {decision.expedition_id} was not found")
        if expedition.organizer_id != actor_id and decision.user_id != actor_id:
            raise UnauthorizedOperationError("decision is only visible to organizer or applicant")
        return EligibilityDecisionResponse.model_validate(decision)

    def list_decisions(
        self,
        expedition_id: int,
        *,
        actor_id: int,
        outcome: str | None = None,
    ) -> list[EligibilityDecisionResponse]:
        expedition = self.expeditions.get_detail(expedition_id)
        if expedition is None:
            raise NotFoundError(f"Expedition {expedition_id} was not found")
        if expedition.organizer_id != actor_id:
            raise UnauthorizedOperationError("only the organizer can list decisions")
        rows = self.decisions.list_for_expedition(expedition_id, outcome=outcome)
        return [EligibilityDecisionResponse.model_validate(row) for row in rows]

    # -- fact gathering and rule application --------------------------------

    def _gather_facts(
        self, user_id: int, rules: EligibilityRules | None, now: datetime
    ) -> dict[str, Any]:
        profile = self.users.get_sport_profile(user_id)
        fitness_rank = self.users.fitness_rank(user_id)
        experiences = self.users.list_outdoor_experiences(user_id)
        valid_experiences = [
            item
            for item in experiences
            if item.valid_from <= now and (item.valid_until is None or item.valid_until >= now)
        ]
        active_restrictions = self.users.active_restrictions(user_id)

        training_facts: dict[str, Any] = {}
        if rules is not None:
            for requirement in rules.training_requirements:
                window_start = now - timedelta(days=requirement.window_days)
                summary = self.training.training_window_summary(
                    user_id, window_start, now
                ).get(requirement.training_type.value, {})
                training_facts[requirement_code(requirement)] = {
                    "training_type": requirement.training_type.value,
                    "window_days": requirement.window_days,
                    "window_start": window_start.isoformat(),
                    "window_end": now.isoformat(),
                    "duration_minutes": summary.get("duration_minutes", 0),
                    "session_count": summary.get("session_count", 0),
                    "distance_km": summary.get("distance_km", 0),
                    "training_load": summary.get("training_load", 0),
                }

        return {
            "evaluated_at": now.isoformat(),
            "sport_profile": {"exists": profile is not None, "fitness_rank": fitness_rank},
            "outdoor_experience": {
                "valid_count": len(valid_experiences),
                "max_valid_level": max((item.level for item in valid_experiences), default=None),
                "expired_count": sum(
                    1
                    for item in experiences
                    if item.valid_until is not None and item.valid_until < now
                ),
                "valid": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "level": item.level,
                        "valid_from": item.valid_from.isoformat(),
                        "valid_until": item.valid_until.isoformat()
                        if item.valid_until
                        else None,
                    }
                    for item in valid_experiences
                ],
            },
            "training": training_facts,
            "health_restrictions": {
                "active_count": len(active_restrictions),
                "active": [
                    {"id": item.id, "name": item.name, "severity": item.severity}
                    for item in active_restrictions
                ],
            },
            "emergency_contact": {"count": self.users.contact_count(user_id)},
        }

    def _apply_rules(
        self, rules: EligibilityRules, facts: dict[str, Any], now: datetime
    ) -> list[EligibilityReason]:
        reasons: list[EligibilityReason] = []

        if not facts["sport_profile"]["exists"]:
            reasons.append(
                EligibilityReason(
                    code="sport_profile",
                    passed=False,
                    effect="deny",
                    message="a sport profile is required before registration",
                )
            )

        for requirement in rules.training_requirements:
            code = requirement_code(requirement)
            actual = facts["training"].get(code, {}).get(requirement.metric.value, 0)
            reasons.append(
                EligibilityReason(
                    code=code,
                    passed=actual >= requirement.minimum,
                    effect="deny",
                    message=(
                        f"{requirement.training_type.value} {requirement.metric.value} in the "
                        f"last {requirement.window_days} days must be >= {requirement.minimum}"
                    ),
                    expected=requirement.minimum,
                    actual=actual,
                )
            )

        outdoor = facts["outdoor_experience"]
        if rules.minimum_outdoor_level is not None:
            max_level = outdoor["max_valid_level"]
            reasons.append(
                EligibilityReason(
                    code="outdoor_level",
                    passed=max_level is not None and max_level >= rules.minimum_outdoor_level,
                    effect="deny",
                    message=(
                        "a currently valid outdoor experience at or above level "
                        f"{rules.minimum_outdoor_level} is required"
                    ),
                    expected=rules.minimum_outdoor_level,
                    actual=max_level,
                )
            )
        if rules.experience_recency_days is not None:
            cutoff = now - timedelta(days=rules.experience_recency_days)
            qualifying = [
                item
                for item in outdoor["valid"]
                if datetime.fromisoformat(item["valid_from"]) >= cutoff
                and (
                    rules.minimum_outdoor_level is None
                    or item["level"] >= rules.minimum_outdoor_level
                )
            ]
            reasons.append(
                EligibilityReason(
                    code="outdoor_recency",
                    passed=bool(qualifying),
                    effect="deny",
                    message=(
                        "qualifying outdoor experience must have been issued within the last "
                        f"{rules.experience_recency_days} days"
                    ),
                    expected=rules.experience_recency_days,
                    actual=(
                        (now - datetime.fromisoformat(qualifying[0]["valid_from"])).days
                        if qualifying
                        else None
                    ),
                )
            )

        active_names = {
            str(item["name"]).strip().lower(): item
            for item in facts["health_restrictions"]["active"]
        }
        for rule in rules.health_restriction_rules:
            target = rule.restriction_name.strip().lower()
            matched = (
                list(active_names.values())
                if target == WILDCARD_RESTRICTION
                else [active_names[target]] if target in active_names else []
            )
            effect = "deny" if rule.action == HealthRuleAction.DENY else "review"
            reasons.append(
                EligibilityReason(
                    code=f"health:{target}",
                    passed=not matched,
                    effect=effect,
                    message=(
                        f"active health restriction '{rule.restriction_name}' "
                        f"requires {'rejection' if effect == 'deny' else 'manual review'}"
                    ),
                    expected=0,
                    actual=len(matched),
                )
            )

        if rules.require_emergency_contact:
            count = facts["emergency_contact"]["count"]
            reasons.append(
                EligibilityReason(
                    code="emergency_contact",
                    passed=count >= 1,
                    effect="deny",
                    message="at least one emergency contact is required",
                    expected=1,
                    actual=count,
                )
            )
        return reasons


def requirement_code(requirement: Any) -> str:
    return (
        f"{requirement.training_type.value}:{requirement.metric.value}:"
        f"{requirement.window_days}d"
    )


def compact_rules(rules: EligibilityRules) -> dict[str, Any]:
    """Serialize only configured rule fields, so frozen snapshots stay minimal."""
    return rules.model_dump(mode="json", exclude_none=True)


def check_policy_version(policy: EligibilityPolicy, expected_version: int) -> None:
    if policy.version != expected_version:
        raise ConflictError(
            "policy was published by another operation",
            context={"expected_version": expected_version, "current_version": policy.version},
        )


def policy_response(policy: EligibilityPolicy) -> EligibilityPolicyResponse:
    return EligibilityPolicyResponse(
        id=policy.id,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
        expedition_id=policy.expedition_id,
        version=policy.version,
        is_active=policy.is_active,
        rules=EligibilityRules.model_validate(policy.rules_json),
        change_note=policy.change_note,
        created_by=policy.created_by,
    )

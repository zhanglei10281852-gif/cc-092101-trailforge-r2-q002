from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update

from trailforge.models.eligibility import EligibilityDecision, EligibilityPolicy
from trailforge.repositories.base import BaseRepository


class EligibilityPolicyRepository(BaseRepository[EligibilityPolicy]):
    model = EligibilityPolicy
    sortable = {"created_at": EligibilityPolicy.created_at, "version": EligibilityPolicy.version}

    def active_policy(self, expedition_id: int) -> EligibilityPolicy | None:
        return self.session.scalar(
            select(EligibilityPolicy)
            .where(
                EligibilityPolicy.expedition_id == expedition_id,
                EligibilityPolicy.is_active.is_(True),
            )
            .limit(1)
        )

    def get_version(self, expedition_id: int, version: int) -> EligibilityPolicy | None:
        return self.session.scalar(
            select(EligibilityPolicy).where(
                EligibilityPolicy.expedition_id == expedition_id,
                EligibilityPolicy.version == version,
            )
        )

    def list_versions(self, expedition_id: int) -> list[EligibilityPolicy]:
        return list(
            self.session.scalars(
                select(EligibilityPolicy)
                .where(EligibilityPolicy.expedition_id == expedition_id)
                .order_by(EligibilityPolicy.version)
            )
        )

    def next_version(self, expedition_id: int) -> int:
        return max((row.version for row in self.list_versions(expedition_id)), default=0) + 1

    def deactivate_all(self, expedition_id: int) -> None:
        for policy in self.list_versions(expedition_id):
            policy.is_active = False


class EligibilityDecisionRepository(BaseRepository[EligibilityDecision]):
    model = EligibilityDecision
    sortable = {"decided_at": EligibilityDecision.decided_at, "id": EligibilityDecision.id}

    def get_for_update(self, decision_id: int) -> EligibilityDecision | None:
        # SQLite serialises writers rather than supporting row locks, so this
        # is a plain read; atomicity for reviews comes from claim_for_review.
        return self.session.get(EligibilityDecision, decision_id)

    def claim_for_review(
        self,
        decision_id: int,
        *,
        expected_version: int,
        reviewer_id: int,
        review_decision: str,
        review_reason: str,
        reviewed_at: datetime,
    ) -> int:
        """Atomically claim an unreviewed decision at its expected version.

        Returns the number of rows updated. A result of 0 means another
        reviewer already claimed it or the version is stale. The conditional
        UPDATE is the real optimistic lock because SQLite ignores
        SELECT ... FOR UPDATE.
        """
        statement = (
            update(EligibilityDecision)
            .where(
                EligibilityDecision.id == decision_id,
                EligibilityDecision.version == expected_version,
                EligibilityDecision.review_decision.is_(None),
            )
            .values(
                version=expected_version + 1,
                reviewed_by=reviewer_id,
                reviewed_at=reviewed_at,
                review_decision=review_decision,
                review_reason=review_reason,
            )
        )
        return int(self.session.execute(statement).rowcount or 0)

    def latest_for(self, expedition_id: int, user_id: int) -> EligibilityDecision | None:
        return self.session.scalar(
            select(EligibilityDecision)
            .where(
                EligibilityDecision.expedition_id == expedition_id,
                EligibilityDecision.user_id == user_id,
            )
            .order_by(EligibilityDecision.attempt.desc(), EligibilityDecision.id.desc())
            .limit(1)
        )

    def next_attempt(self, expedition_id: int, user_id: int) -> int:
        latest = self.latest_for(expedition_id, user_id)
        return (latest.attempt + 1) if latest is not None else 1

    def list_for_expedition(
        self,
        expedition_id: int,
        *,
        user_id: int | None = None,
        outcome: str | None = None,
    ) -> list[EligibilityDecision]:
        statement = select(EligibilityDecision).where(
            EligibilityDecision.expedition_id == expedition_id
        )
        if user_id is not None:
            statement = statement.where(EligibilityDecision.user_id == user_id)
        if outcome is not None:
            statement = statement.where(EligibilityDecision.outcome == outcome)
        return list(
            self.session.scalars(
                statement.order_by(
                    EligibilityDecision.decided_at.desc(), EligibilityDecision.id.desc()
                )
            )
        )

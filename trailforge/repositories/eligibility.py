from __future__ import annotations

from sqlalchemy import func, select, update

from trailforge.domain.enums import RegistrationStatus
from trailforge.models.activities import ExpeditionRegistration
from trailforge.models.eligibility import (
    EligibilityEvaluation,
    EligibilityPolicy,
)
from trailforge.repositories.base import BaseRepository


class EligibilityRepository(BaseRepository[EligibilityPolicy]):
    model = EligibilityPolicy
    sortable = {
        "version": EligibilityPolicy.version,
        "created_at": EligibilityPolicy.created_at,
    }

    def get_active(self, expedition_id: int) -> EligibilityPolicy | None:
        statement = select(EligibilityPolicy).where(
            EligibilityPolicy.expedition_id == expedition_id,
            EligibilityPolicy.is_active.is_(True),
        )
        return self.session.scalar(statement)

    def max_version(self, expedition_id: int) -> int:
        statement = select(func.coalesce(func.max(EligibilityPolicy.version), 0)).where(
            EligibilityPolicy.expedition_id == expedition_id
        )
        return int(self.session.scalar(statement) or 0)

    def deactivate_all(self, expedition_id: int) -> None:
        self.session.execute(
            update(EligibilityPolicy)
            .where(
                EligibilityPolicy.expedition_id == expedition_id,
                EligibilityPolicy.is_active.is_(True),
            )
            .values(is_active=False)
        )

    def list_versions(self, expedition_id: int) -> list[EligibilityPolicy]:
        statement = (
            select(EligibilityPolicy)
            .where(EligibilityPolicy.expedition_id == expedition_id)
            .order_by(EligibilityPolicy.version.desc())
        )
        return list(self.session.scalars(statement))

    def latest_evaluation(self, registration_id: int) -> EligibilityEvaluation | None:
        statement = (
            select(EligibilityEvaluation)
            .where(EligibilityEvaluation.registration_id == registration_id)
            .order_by(EligibilityEvaluation.id.desc())
            .limit(1)
        )
        return self.session.scalar(statement)

    def pending_registrations(self, expedition_id: int) -> list[ExpeditionRegistration]:
        statement = (
            select(ExpeditionRegistration)
            .where(
                ExpeditionRegistration.expedition_id == expedition_id,
                ExpeditionRegistration.status == RegistrationStatus.PENDING,
            )
            .order_by(ExpeditionRegistration.registered_at, ExpeditionRegistration.id)
        )
        return list(self.session.scalars(statement))

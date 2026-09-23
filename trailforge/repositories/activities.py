from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, or_, select, update
from sqlalchemy.orm import selectinload

from trailforge.domain.enums import ActivityStatus, RegistrationStatus
from trailforge.models.activities import Expedition, ExpeditionRegistration
from trailforge.repositories.base import BaseRepository, PageResult
from trailforge.schemas.activities import ExpeditionFilter

ACTIVE_REGISTRATION_STATUSES = {
    RegistrationStatus.CONFIRMED,
    RegistrationStatus.PENDING,
}


class ExpeditionRepository(BaseRepository[Expedition]):
    model = Expedition
    sortable = {
        "created_at": Expedition.created_at,
        "start_at": Expedition.start_at,
        "end_at": Expedition.end_at,
        "name": Expedition.name,
        "status": Expedition.status,
        "capacity": Expedition.capacity,
    }

    def get_detail(self, expedition_id: int, *, for_update: bool = False) -> Expedition | None:
        statement = (
            select(Expedition)
            .options(selectinload(Expedition.registrations))
            .where(Expedition.id == expedition_id)
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list_expeditions(self, filters: ExpeditionFilter) -> PageResult[Expedition]:
        statement: Select = select(Expedition).options(selectinload(Expedition.registrations))
        if filters.organizer_id is not None:
            statement = statement.where(Expedition.organizer_id == filters.organizer_id)
        if filters.route_id is not None:
            statement = statement.where(Expedition.route_id == filters.route_id)
        if filters.participant_id is not None:
            statement = statement.join(ExpeditionRegistration).where(
                ExpeditionRegistration.user_id == filters.participant_id,
                ExpeditionRegistration.status.in_(ACTIVE_REGISTRATION_STATUSES),
            )
        if filters.status is not None:
            statement = statement.where(Expedition.status == filters.status)
        if filters.risk_level is not None:
            statement = statement.where(Expedition.risk_level == filters.risk_level)
        if filters.starts_after is not None:
            statement = statement.where(Expedition.start_at >= filters.starts_after)
        if filters.starts_before is not None:
            statement = statement.where(Expedition.start_at <= filters.starts_before)
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            statement = statement.where(
                or_(Expedition.name.ilike(pattern), Expedition.description.ilike(pattern))
            )
        if filters.has_capacity is True:
            confirmed_count = (
                select(func.count(ExpeditionRegistration.id))
                .where(
                    ExpeditionRegistration.expedition_id == Expedition.id,
                    ExpeditionRegistration.status == RegistrationStatus.CONFIRMED,
                )
                .correlate(Expedition)
                .scalar_subquery()
            )
            statement = statement.where(confirmed_count < Expedition.capacity)
        return self.paginate(
            statement.distinct(),
            page=filters.page,
            page_size=filters.page_size,
            sort=filters.sort,
            direction=filters.direction,
        )

    def get_registration(
        self,
        expedition_id: int,
        user_id: int,
        *,
        for_update: bool = False,
    ) -> ExpeditionRegistration | None:
        statement = select(ExpeditionRegistration).where(
            ExpeditionRegistration.expedition_id == expedition_id,
            ExpeditionRegistration.user_id == user_id,
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def get_registration_by_id(
        self,
        registration_id: int,
        *,
        for_update: bool = False,
    ) -> ExpeditionRegistration | None:
        statement = select(ExpeditionRegistration).where(
            ExpeditionRegistration.id == registration_id
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def transition_registration_status(
        self,
        registration_id: int,
        *,
        expected_version: int,
        expected_status: RegistrationStatus,
        new_status: RegistrationStatus,
    ) -> bool:
        """Atomically move a registration to ``new_status``.

        The version and current status are part of the WHERE clause, so a
        concurrent transaction that already changed the row makes this
        statement match nothing instead of silently overwriting it.
        """
        result = self.session.execute(
            update(ExpeditionRegistration)
            .where(
                ExpeditionRegistration.id == registration_id,
                ExpeditionRegistration.version == expected_version,
                ExpeditionRegistration.status == expected_status,
            )
            .values(
                status=new_status,
                version=ExpeditionRegistration.version + 1,
            )
        )
        return result.rowcount == 1

    def completed_expedition_count(self, user_id: int, *, since: datetime | None = None) -> int:
        """Expeditions the user completed as a confirmed participant.

        ``since`` optionally limits the count to expeditions that ended at or
        after the given moment, which models expiring outdoor experience.
        """
        statement = (
            select(func.count(Expedition.id))
            .join(ExpeditionRegistration, ExpeditionRegistration.expedition_id == Expedition.id)
            .where(
                ExpeditionRegistration.user_id == user_id,
                ExpeditionRegistration.status == RegistrationStatus.CONFIRMED,
                Expedition.status == ActivityStatus.COMPLETED,
            )
        )
        if since is not None:
            statement = statement.where(Expedition.end_at >= since)
        return int(self.session.scalar(statement) or 0)

    def confirmed_count(self, expedition_id: int) -> int:
        statement = select(func.count()).where(
            ExpeditionRegistration.expedition_id == expedition_id,
            ExpeditionRegistration.status == RegistrationStatus.CONFIRMED,
        )
        return int(self.session.scalar(statement) or 0)

    def conflicting_registration(
        self,
        user_id: int,
        start_at: datetime,
        end_at: datetime,
        *,
        exclude_expedition_id: int | None = None,
    ) -> ExpeditionRegistration | None:
        statement = (
            select(ExpeditionRegistration)
            .join(Expedition)
            .where(
                ExpeditionRegistration.user_id == user_id,
                ExpeditionRegistration.status.in_(ACTIVE_REGISTRATION_STATUSES),
                Expedition.start_at < end_at,
                Expedition.end_at > start_at,
            )
        )
        if exclude_expedition_id is not None:
            statement = statement.where(Expedition.id != exclude_expedition_id)
        return self.session.scalar(statement.limit(1))

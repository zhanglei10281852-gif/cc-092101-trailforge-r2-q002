from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, exists, func, or_, select, update
from sqlalchemy.orm import selectinload

from trailforge.domain.enums import RegistrationStatus
from trailforge.models.activities import Expedition, ExpeditionRegistration
from trailforge.repositories.base import BaseRepository, PageResult
from trailforge.schemas.activities import ExpeditionFilter

ACTIVE_REGISTRATION_STATUSES = {
    RegistrationStatus.CONFIRMED,
    RegistrationStatus.PENDING,
}

# Only confirmed members occupy the schedule. Pending reviews are not yet
# admitted and waitlisted members never blocked other sign-ups, so neither
# participates in time-overlap checks (approval re-runs the same check).
TIME_BLOCKING_STATUSES = {RegistrationStatus.CONFIRMED}


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

    def confirmed_count(self, expedition_id: int) -> int:
        statement = select(func.count()).where(
            ExpeditionRegistration.expedition_id == expedition_id,
            ExpeditionRegistration.status == RegistrationStatus.CONFIRMED,
        )
        return int(self.session.scalar(statement) or 0)

    def confirm_pending_if_room_and_no_conflict(
        self,
        registration_id: int,
        *,
        expedition_id: int,
        user_id: int,
        start_at: datetime,
        end_at: datetime,
    ) -> int:
        """Atomically move PENDING -> CONFIRMED if capacity and schedule allow.

        All predicates run in the UPDATE's write cursor against the latest
        committed data, so two concurrent approvals of different applicants
        can never both observe a free last seat.
        """
        return int(
            self.session.execute(
                self._pending_transition_statement(
                    registration_id,
                    RegistrationStatus.CONFIRMED,
                    expedition_id=expedition_id,
                    user_id=user_id,
                    start_at=start_at,
                    end_at=end_at,
                    require_room=True,
                )
            ).rowcount
            or 0
        )

    def waitlist_pending_if_no_conflict(
        self,
        registration_id: int,
        *,
        expedition_id: int,
        user_id: int,
        start_at: datetime,
        end_at: datetime,
    ) -> int:
        """Atomically move PENDING -> WAITLISTED regardless of capacity."""
        return int(
            self.session.execute(
                self._pending_transition_statement(
                    registration_id,
                    RegistrationStatus.WAITLISTED,
                    expedition_id=expedition_id,
                    user_id=user_id,
                    start_at=start_at,
                    end_at=end_at,
                    require_room=False,
                )
            ).rowcount
            or 0
        )

    def reject_pending(self, registration_id: int) -> int:
        statement = (
            update(ExpeditionRegistration)
            .where(
                ExpeditionRegistration.id == registration_id,
                ExpeditionRegistration.status == RegistrationStatus.PENDING,
            )
            .values(
                status=RegistrationStatus.REJECTED,
                version=ExpeditionRegistration.version + 1,
            )
        )
        return int(self.session.execute(statement).rowcount or 0)

    def _pending_transition_statement(
        self,
        registration_id: int,
        target: RegistrationStatus,
        *,
        expedition_id: int,
        user_id: int,
        start_at: datetime,
        end_at: datetime,
        require_room: bool,
    ):
        capacity = (
            select(Expedition.capacity)
            .where(Expedition.id == expedition_id)
            .scalar_subquery()
        )
        confirmed = (
            select(func.count())
            .select_from(ExpeditionRegistration)
            .where(
                ExpeditionRegistration.expedition_id == expedition_id,
                ExpeditionRegistration.status == RegistrationStatus.CONFIRMED,
            )
            .scalar_subquery()
        )
        schedule_conflict = (
            select(ExpeditionRegistration.id)
            .join(Expedition)
            .where(
                ExpeditionRegistration.user_id == user_id,
                ExpeditionRegistration.status == RegistrationStatus.CONFIRMED,
                Expedition.id != expedition_id,
                Expedition.start_at < end_at,
                Expedition.end_at > start_at,
            )
            .limit(1)
        )
        conditions = [
            ExpeditionRegistration.id == registration_id,
            ExpeditionRegistration.status == RegistrationStatus.PENDING,
            ~exists(schedule_conflict),
        ]
        if require_room:
            conditions.append(confirmed < capacity)
        return (
            update(ExpeditionRegistration)
            .where(*conditions)
            .values(status=target, version=ExpeditionRegistration.version + 1)
        )

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
                ExpeditionRegistration.status.in_(TIME_BLOCKING_STATUSES),
                Expedition.start_at < end_at,
                Expedition.end_at > start_at,
            )
        )
        if exclude_expedition_id is not None:
            statement = statement.where(Expedition.id != exclude_expedition_id)
        return self.session.scalar(statement.limit(1))

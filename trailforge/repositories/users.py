from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import joinedload, selectinload

from trailforge.domain.enums import FitnessLevel
from trailforge.models.users import (
    EmergencyContact,
    HealthRestriction,
    OutdoorExperience,
    SportProfile,
    User,
)
from trailforge.repositories.base import BaseRepository, PageResult
from trailforge.schemas.users import UserFilter


class UserRepository(BaseRepository[User]):
    model = User
    sortable = {
        "created_at": User.created_at,
        "display_name": User.display_name,
        "email": User.email,
        "birth_date": User.birth_date,
    }

    def get_by_email(self, email: str) -> User | None:
        return self.session.scalar(select(User).where(User.email == email.lower()))

    def get_profile_bundle(self, user_id: int) -> User | None:
        statement = (
            select(User)
            .options(
                joinedload(User.profile),
                selectinload(User.emergency_contacts),
                selectinload(User.health_restrictions),
                selectinload(User.outdoor_experiences),
            )
            .where(User.id == user_id)
        )
        return self.session.scalar(statement)

    def list(self, filters: UserFilter) -> PageResult[User]:
        statement = select(User).options(joinedload(User.profile))
        if filters.search:
            pattern = f"%{filters.search.strip()}%"
            statement = statement.where(
                or_(User.display_name.ilike(pattern), User.email.ilike(pattern))
            )
        if filters.is_active is not None:
            statement = statement.where(User.is_active == filters.is_active)
        if filters.fitness_level is not None:
            statement = statement.join(SportProfile).where(
                SportProfile.fitness_level == filters.fitness_level
            )
        return self.paginate(
            statement,
            page=filters.page,
            page_size=filters.page_size,
            sort=filters.sort,
            direction=filters.direction,
        )

    def get_sport_profile(self, user_id: int) -> SportProfile | None:
        return self.session.scalar(select(SportProfile).where(SportProfile.user_id == user_id))

    def get_contact(self, user_id: int, contact_id: int) -> EmergencyContact | None:
        return self.session.scalar(
            select(EmergencyContact).where(
                EmergencyContact.id == contact_id,
                EmergencyContact.user_id == user_id,
            )
        )

    def list_contacts(self, user_id: int) -> list[EmergencyContact]:
        statement = (
            select(EmergencyContact)
            .where(EmergencyContact.user_id == user_id)
            .order_by(EmergencyContact.priority, EmergencyContact.id)
        )
        return list(self.session.scalars(statement))

    def get_restriction(self, user_id: int, restriction_id: int) -> HealthRestriction | None:
        return self.session.scalar(
            select(HealthRestriction).where(
                HealthRestriction.id == restriction_id,
                HealthRestriction.user_id == user_id,
            )
        )

    def active_restriction_count(self, user_id: int) -> int:
        statement = select(func.count()).where(
            HealthRestriction.user_id == user_id,
            HealthRestriction.is_active.is_(True),
        )
        return int(self.session.scalar(statement) or 0)

    def contact_count(self, user_id: int) -> int:
        return int(
            self.session.scalar(select(func.count()).where(EmergencyContact.user_id == user_id))
            or 0
        )

    def fitness_rank(self, user_id: int) -> int | None:
        profile = self.get_sport_profile(user_id)
        if profile is None:
            return None
        ranks = {
            FitnessLevel.BEGINNER: 1,
            FitnessLevel.INTERMEDIATE: 2,
            FitnessLevel.ADVANCED: 3,
            FitnessLevel.EXPERT: 4,
        }
        return ranks[FitnessLevel(profile.fitness_level)]

    def active_restrictions(self, user_id: int) -> list[HealthRestriction]:
        return list(
            self.session.scalars(
                select(HealthRestriction).where(
                    HealthRestriction.user_id == user_id,
                    HealthRestriction.is_active.is_(True),
                )
            )
        )

    def emergency_contact_count(self, user_id: int) -> int:
        return self.contact_count(user_id)

    def valid_experience(self, user_id: int, at: datetime) -> list[OutdoorExperience]:
        """Experiences that have started and not expired at the given instant."""
        return [
            item
            for item in self.list_outdoor_experiences(user_id)
            if item.valid_from <= at and (item.valid_until is None or item.valid_until >= at)
        ]

    def list_outdoor_experiences(self, user_id: int) -> list[OutdoorExperience]:
        return list(
            self.session.scalars(
                select(OutdoorExperience)
                .where(OutdoorExperience.user_id == user_id)
                .order_by(OutdoorExperience.valid_from.desc(), OutdoorExperience.id.desc())
            )
        )

    def get_experience(self, user_id: int, experience_id: int) -> OutdoorExperience | None:
        return self.session.scalar(
            select(OutdoorExperience).where(
                OutdoorExperience.id == experience_id,
                OutdoorExperience.user_id == user_id,
            )
        )

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from trailforge.database.base import Base, UTCDateTime, utc_now
from trailforge.domain.enums import EligibilityDecision, RegistrationStatus, ReviewDecision
from trailforge.models.mixins import IntegerPrimaryKeyMixin, TimestampMixin


class EligibilityPolicy(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    """Versioned eligibility rules for an expedition.

    Policies are append-only: maintaining a policy creates a new version and
    deactivates the previous one, so historical evaluations keep pointing at
    the exact rule set that produced them.
    """

    __tablename__ = "eligibility_policies"
    __table_args__ = (
        UniqueConstraint("expedition_id", "version", name="uq_policy_expedition_version"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "training_window_days IS NULL OR training_window_days BETWEEN 1 AND 365",
            name="training_window_range",
        ),
        CheckConstraint(
            "min_endurance_minutes IS NULL OR min_endurance_minutes >= 0",
            name="endurance_nonnegative",
        ),
        CheckConstraint(
            "min_loaded_sessions IS NULL OR min_loaded_sessions >= 0",
            name="loaded_nonnegative",
        ),
        CheckConstraint(
            "min_completed_expeditions IS NULL OR min_completed_expeditions >= 0",
            name="experience_nonnegative",
        ),
        CheckConstraint(
            "experience_window_days IS NULL OR experience_window_days BETWEEN 1 AND 3650",
            name="experience_window_range",
        ),
        CheckConstraint(
            "min_emergency_contacts IS NULL OR min_emergency_contacts BETWEEN 0 AND 10",
            name="contacts_range",
        ),
        Index("ix_policy_expedition_active", "expedition_id", "is_active"),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    training_window_days: Mapped[int | None] = mapped_column(Integer)
    min_endurance_minutes: Mapped[int | None] = mapped_column(Integer)
    min_loaded_sessions: Mapped[int | None] = mapped_column(Integer)
    min_completed_expeditions: Mapped[int | None] = mapped_column(Integer)
    experience_window_days: Mapped[int | None] = mapped_column(Integer)
    blocked_restrictions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    review_restrictions: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    min_emergency_contacts: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class EligibilityEvaluation(IntegerPrimaryKeyMixin, Base):
    """Immutable record of one eligibility decision.

    Stores the policy version, the input facts and the per-rule reasons as
    they were at evaluation time. Rows are never updated, so later profile
    changes cannot rewrite historical conclusions.
    """

    __tablename__ = "eligibility_evaluations"
    __table_args__ = (
        Index("ix_evaluation_registration", "registration_id"),
        Index("ix_evaluation_expedition_user", "expedition_id", "user_id"),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), nullable=False
    )
    registration_id: Mapped[int | None] = mapped_column(
        ForeignKey("expedition_registrations.id", ondelete="CASCADE")
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("eligibility_policies.id", ondelete="SET NULL")
    )
    policy_version: Mapped[int | None] = mapped_column(Integer)
    decision: Mapped[EligibilityDecision] = mapped_column(String(24), nullable=False)
    facts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)


class EligibilityReview(IntegerPrimaryKeyMixin, Base):
    """Organizer decision on a pending registration."""

    __tablename__ = "eligibility_reviews"
    __table_args__ = (
        Index("ix_review_expedition", "expedition_id"),
        Index("ix_review_registration", "registration_id"),
    )

    evaluation_id: Mapped[int | None] = mapped_column(
        ForeignKey("eligibility_evaluations.id", ondelete="SET NULL")
    )
    registration_id: Mapped[int] = mapped_column(
        ForeignKey("expedition_registrations.id", ondelete="CASCADE"), nullable=False
    )
    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decision: Mapped[ReviewDecision] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    resulting_status: Mapped[RegistrationStatus] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utc_now, nullable=False)

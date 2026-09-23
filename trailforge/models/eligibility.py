from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trailforge.database.base import Base, UTCDateTime, utc_now
from trailforge.models.mixins import IntegerPrimaryKeyMixin, TimestampMixin


class EligibilityPolicy(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    """An immutable, append-only policy version attached to an expedition."""

    __tablename__ = "eligibility_policies"
    __table_args__ = (
        UniqueConstraint("expedition_id", "version", name="uq_policy_expedition_version"),
        CheckConstraint("version >= 1", name="policy_version_positive"),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    rules_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    change_note: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))

    decisions: Mapped[list[EligibilityDecision]] = relationship(
        back_populates="policy",
    )


class EligibilityDecision(IntegerPrimaryKeyMixin, TimestampMixin, Base):
    """Frozen evaluation record. Review columns are the only fields ever written."""

    __tablename__ = "eligibility_decisions"
    __table_args__ = (
        UniqueConstraint(
            "expedition_id", "user_id", "attempt", name="uq_decision_expedition_user_attempt"
        ),
        CheckConstraint("attempt >= 1", name="decision_attempt_positive"),
        CheckConstraint(
            "review_decision IS NULL OR review_decision IN ('approve', 'reject')",
            name="review_decision_value",
        ),
    )

    expedition_id: Mapped[int] = mapped_column(
        ForeignKey("expeditions.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    registration_id: Mapped[int | None] = mapped_column(
        ForeignKey("expedition_registrations.id", ondelete="CASCADE"), index=True
    )
    policy_id: Mapped[int | None] = mapped_column(
        ForeignKey("eligibility_policies.id", ondelete="SET NULL")
    )
    policy_version: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    rules_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    input_facts: Mapped[dict] = mapped_column(JSON, nullable=False)
    reasons_json: Mapped[list] = mapped_column(JSON, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utc_now, nullable=False, index=True
    )

    reviewed_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    review_decision: Mapped[str | None] = mapped_column(String(24))
    review_reason: Mapped[str | None] = mapped_column(Text)
    review_facts: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    policy: Mapped[EligibilityPolicy | None] = relationship(back_populates="decisions")

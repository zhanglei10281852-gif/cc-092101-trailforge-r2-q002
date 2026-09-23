from __future__ import annotations

from datetime import datetime

from sqlalchemy import Select, func, select
from sqlalchemy.orm import selectinload

from trailforge.domain.enums import SessionStatus
from trailforge.models.training import (
    TrainingExercise,
    TrainingPlan,
    TrainingRecord,
    TrainingSession,
)
from trailforge.repositories.base import BaseRepository, PageResult
from trailforge.schemas.training import TrainingPlanFilter


class TrainingRepository(BaseRepository[TrainingPlan]):
    model = TrainingPlan
    sortable = {
        "created_at": TrainingPlan.created_at,
        "start_at": TrainingPlan.start_at,
        "end_at": TrainingPlan.end_at,
        "name": TrainingPlan.name,
        "status": TrainingPlan.status,
    }

    def get_plan_detail(self, plan_id: int, *, for_update: bool = False) -> TrainingPlan | None:
        statement = (
            select(TrainingPlan)
            .options(selectinload(TrainingPlan.exercises), selectinload(TrainingPlan.sessions))
            .where(TrainingPlan.id == plan_id)
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list_plans(self, filters: TrainingPlanFilter) -> PageResult[TrainingPlan]:
        statement: Select = select(TrainingPlan).options(selectinload(TrainingPlan.exercises))
        if filters.user_id is not None:
            statement = statement.where(TrainingPlan.user_id == filters.user_id)
        if filters.status is not None:
            statement = statement.where(TrainingPlan.status == filters.status)
        if filters.training_type is not None:
            statement = statement.join(TrainingExercise).where(
                TrainingExercise.training_type == filters.training_type
            )
        if filters.starts_after is not None:
            statement = statement.where(TrainingPlan.start_at >= filters.starts_after)
        if filters.ends_before is not None:
            statement = statement.where(TrainingPlan.end_at <= filters.ends_before)
        return self.paginate(
            statement.distinct(),
            page=filters.page,
            page_size=filters.page_size,
            sort=filters.sort,
            direction=filters.direction,
        )

    def get_exercise(self, exercise_id: int) -> TrainingExercise | None:
        return self.session.get(TrainingExercise, exercise_id)

    def get_session(self, session_id: int, *, for_update: bool = False) -> TrainingSession | None:
        statement = (
            select(TrainingSession)
            .options(selectinload(TrainingSession.records))
            .where(TrainingSession.id == session_id)
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalar(statement)

    def list_sessions(
        self,
        *,
        user_id: int,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> list[TrainingSession]:
        statement = select(TrainingSession).where(TrainingSession.user_id == user_id)
        if start_at is not None:
            statement = statement.where(TrainingSession.planned_start_at >= start_at)
        if end_at is not None:
            statement = statement.where(TrainingSession.planned_start_at <= end_at)
        return list(self.session.scalars(statement.order_by(TrainingSession.planned_start_at)))

    def records_for_user(
        self,
        user_id: int,
        start_at: datetime | None,
        end_at: datetime | None,
    ) -> list[tuple[TrainingRecord, TrainingExercise]]:
        statement = (
            select(TrainingRecord, TrainingExercise)
            .join(TrainingSession, TrainingSession.id == TrainingRecord.session_id)
            .join(TrainingExercise, TrainingExercise.id == TrainingRecord.exercise_id)
            .where(TrainingSession.user_id == user_id)
        )
        if start_at is not None:
            statement = statement.where(TrainingSession.planned_start_at >= start_at)
        if end_at is not None:
            statement = statement.where(TrainingSession.planned_start_at <= end_at)
        return list(self.session.execute(statement).tuples())

    def training_window_summary(
        self,
        user_id: int,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, dict[str, float]]:
        """Aggregate completed-session records per exercise training type.

        Time basis is the actual session start, so timezone-aware window edges
        compare against when the training really happened.
        """
        duration = func.coalesce(func.sum(TrainingRecord.duration_minutes), 0)
        distance = func.coalesce(func.sum(TrainingRecord.distance_km), 0.0)
        load = func.coalesce(func.sum(TrainingRecord.training_load), 0.0)
        sessions = func.count(func.distinct(TrainingSession.id))
        statement = (
            select(
                TrainingExercise.training_type,
                duration.label("duration_minutes"),
                sessions.label("session_count"),
                distance.label("distance_km"),
                load.label("training_load"),
            )
            .join(TrainingSession, TrainingSession.id == TrainingRecord.session_id)
            .join(TrainingExercise, TrainingExercise.id == TrainingRecord.exercise_id)
            .where(
                TrainingSession.user_id == user_id,
                TrainingSession.status == SessionStatus.COMPLETED,
                TrainingSession.actual_start_at >= window_start,
                TrainingSession.actual_start_at <= window_end,
            )
            .group_by(TrainingExercise.training_type)
        )
        result: dict[str, dict[str, float]] = {}
        for row in self.session.execute(statement).mappings():
            result[str(row["training_type"])] = {
                "duration_minutes": float(row["duration_minutes"]),
                "session_count": float(row["session_count"]),
                "distance_km": round(float(row["distance_km"]), 3),
                "training_load": round(float(row["training_load"]), 3),
            }
        return result

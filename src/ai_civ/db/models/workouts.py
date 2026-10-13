"""Workout tracker (migration 0018). DDL source of truth: migrations/sql/0018."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Numeric, SmallInteger, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class Exercise(Base):
    """The canonical dictionary of one tenant: a clean name, a muscle group (a closed list, CHECK in the SQL) and the synonyms a name is resolved against."""

    __tablename__ = "exercises"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("exercises")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    muscle_group: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'other'"))
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_exercises_user_id_id"),
        UniqueConstraint("user_id", "name", name="uq_exercises_user_name"),
    )


class WorkoutSession(Base):
    """One logged workout. ``source_message_id`` (the user's message that reported it) is unique per tenant: a replayed update cannot log it twice."""

    __tablename__ = "workout_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("workout_sessions")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    notes: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_workout_sessions_user_id_id"),
        ForeignKeyConstraint(
            ["user_id", "source_message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_workout_sessions_message",
            ondelete="SET NULL (source_message_id)",  # PostgreSQL 15+
        ),
        Index("uq_workout_sessions_message", "user_id", "source_message_id", unique=True, postgresql_where=text("source_message_id IS NOT NULL")),
        Index("ix_workout_sessions_user_started", "user_id", "started_at"),
    )


class WorkoutSet(Base):
    """One set. ``set_order`` counts within (session, exercise); ``target_reps`` (optional) is what the "all target reps done" rule compares ``reps`` with."""

    __tablename__ = "workout_sets"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("workout_sets")
    session_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    exercise_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    set_order: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    weight: Mapped[Decimal] = mapped_column(Numeric(7, 2), nullable=False, server_default=text("0"))
    reps: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    rpe: Mapped[Decimal | None] = mapped_column(Numeric(3, 1))
    target_reps: Mapped[int | None] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "session_id", "exercise_id", "set_order", name="uq_workout_sets_order"),
        ForeignKeyConstraint(["user_id", "session_id"], ["workout_sessions.user_id", "workout_sessions.id"], name="fk_workout_sets_session", ondelete="CASCADE"),
        ForeignKeyConstraint(["user_id", "exercise_id"], ["exercises.user_id", "exercises.id"], name="fk_workout_sets_exercise"),
        Index("ix_workout_sets_user_exercise", "user_id", "exercise_id", "session_id"),
        Index("ix_workout_sets_user_session", "user_id", "session_id"),
    )

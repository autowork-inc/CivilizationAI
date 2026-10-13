"""Tasks and their decomposition (migration 0013). DDL source of truth: migrations/sql/0013."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, SmallInteger, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class Task(Base):
    """A root task (``parent_task_id`` NULL: the goal the user named) or one of its steps (one level only; a trigger enforces it)."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("tasks")
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
    due: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    persona_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_tasks_user_id_id"),
        ForeignKeyConstraint(["user_id", "parent_task_id"], ["tasks.user_id", "tasks.id"], name="fk_tasks_parent", ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["user_id", "persona_id"],
            ["personas.user_id", "personas.id"],
            name="fk_tasks_persona",
            ondelete="SET NULL (persona_id)",  # PostgreSQL 15+
        ),
        Index("ix_tasks_user_parent", "user_id", "parent_task_id", "position"),
        Index("ix_tasks_user_status_due", "user_id", "status", "due"),
        Index("uq_tasks_open_root_title", "user_id", "title", unique=True, postgresql_where=text("parent_task_id IS NULL AND status = 'open'")),
    )


class TaskEvent(Base):
    """The calendar event that is the slot of a task. Made only when the user confirms an event draft that carries ``task_id``."""

    __tablename__ = "task_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("task_events")
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "task_id", "event_id", name="uq_task_events_link"),
        ForeignKeyConstraint(["user_id", "task_id"], ["tasks.user_id", "tasks.id"], name="fk_task_events_task", ondelete="CASCADE"),
        ForeignKeyConstraint(["user_id", "event_id"], ["events.user_id", "events.id"], name="fk_task_events_event", ondelete="CASCADE"),
        Index("ix_task_events_user_event", "user_id", "event_id"),
    )

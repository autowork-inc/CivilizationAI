"""Recipe history (migration 0019). DDL source of truth: migrations/sql/0019."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, SmallInteger, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class RecipeHistory(Base):
    """A recipe the bot suggested. ``status`` goes ``suggested`` -> ``cooked`` once (a trigger makes ``cooked`` final); the words are never edited."""

    __tablename__ = "recipe_history"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("recipe_history")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    prep_time_minutes: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    ingredients_used: Mapped[Any] = mapped_column(JSONB, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    nutrition_focus: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'suggested'"))
    cooked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_recipe_history_user_id_id"),
        ForeignKeyConstraint(
            ["user_id", "source_message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_recipe_history_message",
            ondelete="SET NULL (source_message_id)",  # PostgreSQL 15+
        ),
        Index("uq_recipe_history_message", "user_id", "source_message_id", unique=True, postgresql_where=text("source_message_id IS NOT NULL")),
        Index("ix_recipe_history_user_created", "user_id", "created_at"),
    )

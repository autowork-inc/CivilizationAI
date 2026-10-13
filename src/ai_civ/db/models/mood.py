"""Daily mood estimates (migration 0024). DDL source of truth: migrations/sql/0024."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, Integer, SmallInteger, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.enums import SharedMood, pg_enum
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class DailyMood(Base):
    """One row per LOCAL calendar day: a model's reading (-2 heavy .. +2 bright) of the user's own words of that day. An estimate, never shared with
    anybody (the Diplomat and the Tavern read only what the user confirmed); ``message_count`` = how many messages were read, so the job re-scores a day
    only when it changes."""

    __tablename__ = "daily_mood"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("daily_mood")
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    score: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    mood: Mapped[SharedMood | None] = mapped_column(pg_enum(SharedMood, "shared_mood"))
    message_count: Mapped[int] = mapped_column(Integer, nullable=False)
    scored_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_daily_mood_user_id_id"),
        UniqueConstraint("user_id", "local_date", name="uq_daily_mood_user_date"),
    )

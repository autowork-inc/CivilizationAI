"""Time capsules (migration 0022). DDL source of truth: migrations/sql/0022."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class TimeCapsule(Base):
    """A letter to the future self. ``status`` goes ``pending`` -> ``delivered`` | ``canceled`` once (a trigger makes the end final); the words and the date are never
    edited. ``claimed_at`` is the lease of the delivery job; ``source_message_id`` is the Telegram id of the sealing message (unique per tenant)."""

    __tablename__ = "time_capsules"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("time_capsules")
    title: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    deliver_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_time_capsules_user_id_id"),
        Index("uq_time_capsules_source", "user_id", "source_message_id", unique=True, postgresql_where=text("source_message_id IS NOT NULL")),
        Index("ix_time_capsules_user_status_due", "user_id", "status", "deliver_at"),
    )

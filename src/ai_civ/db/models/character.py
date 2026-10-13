"""Character profile and XP ledger (migration 0016). DDL source of truth: migrations/sql/0016."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class CharacterProfile(Base):
    """One row per tenant. ``character_name`` NULL = the API falls back to the Telegram first name; the avatar is already re-encoded WebP."""

    __tablename__ = "character_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_character_profiles_user"), primary_key=True
    )
    character_name: Mapped[str | None] = mapped_column(Text)
    avatar_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    show_level: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    show_mood: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    show_interests: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    open_for_intros: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)


class XpLedger(Base):
    """Append-only experience awards. ``(user_id, source_ref)`` is unique: one event is paid once. The 100 XP/day cap is a trigger."""

    __tablename__ = "xp_ledger"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("xp_ledger")
    amount: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("10"))
    category: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_xp_ledger_user_id_id"),
        UniqueConstraint("user_id", "source_ref", name="uq_xp_ledger_source"),
        Index("ix_xp_ledger_user_created", "user_id", "created_at"),
    )


class TavernCheer(Base):
    """A friendly impulse (migration 0017). Written only by ``civ.send_cheer``; one per (sender, friend, sender-local day)."""

    __tablename__ = "tavern_cheers"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("tavern_cheers")
    target_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE", name="fk_tavern_cheers_target"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "target_user_id", "local_date", name="uq_tavern_cheers_day"),
        Index("ix_tavern_cheers_target_created", "target_user_id", "created_at"),
    )

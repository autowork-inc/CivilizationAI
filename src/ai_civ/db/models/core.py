"""Tenants, personas and Tier 1 (Core Profile + Emotional Overrides). DDL source of truth: migrations/sql/0001."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.enums import (
    EmotionSource,
    EmotionStatus,
    FactCategory,
    FactSource,
    OverrideMode,
    SharedMood,
    pg_enum,
)
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class User(Base):
    """A tenant. ``id`` IS the tenant id (the only table without a separate user_id column)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger)  # NULL = a person of the mobile app (0026); CHECK ck_users_identity in SQL
    forum_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'UTC'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    # Onboarding interview (migration 0003): fixed script, state in the database. CHECKs live in SQL only.
    onboarding_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    onboarding_step: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    onboarding_draft: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Local-time window in which the bot never writes first (may wrap around midnight).
    quiet_hours_start: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'22:00'"))
    quiet_hours_end: Mapped[time] = mapped_column(Time, nullable=False, server_default=text("'08:00'"))
    # How many check-ins a day the user wants (migration 0024; CHECK 0..4 in SQL): NULL = the system default (3-4 random slots), 0 = none.
    checkin_per_day: Mapped[int | None] = mapped_column(SmallInteger)
    # State of the group (migration 0004): active | frozen | dormant. CHECK lives in SQL only.
    group_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    group_state_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    # Identity outside Telegram (migration 0026). CHECKs (at least one identity, e-mail shape, provider+id together) live in SQL only.
    email: Mapped[str | None] = mapped_column(Text)
    pass_hash: Mapped[str | None] = mapped_column(Text)
    external_auth_provider: Mapped[str | None] = mapped_column(Text)
    external_auth_id: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("telegram_user_id", name="uq_users_telegram_user_id"),
        UniqueConstraint("forum_chat_id", name="uq_users_forum_chat_id"),
        Index("uq_users_email_lower", func.lower(text("email")), unique=True, postgresql_where=text("email IS NOT NULL")),
        Index(
            "uq_users_external_auth",
            "external_auth_provider",
            "external_auth_id",
            unique=True,
            postgresql_where=text("external_auth_id IS NOT NULL"),
        ),
    )


class Persona(Base):
    __tablename__ = "personas"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("personas")
    code: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_thread_id: Mapped[int | None] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # Who set the CURRENT topic name ('bot' | 'user', CHECK in SQL) and when. NULL = never renamed since migration 0009. The bot's own
    # renames are rate-limited by these two columns (bot/personas.py: claim_rename), a manual rename is respected for longer.
    title_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title_source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'bot'"))
    # The language mode of the topic ('normal' | 'immersion_da', CHECK in SQL; migration 0015): changed only by the user's /dansk and /normal.
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'normal'"))

    __table_args__ = (
        UniqueConstraint("user_id", "code", name="uq_personas_user_code"),
        UniqueConstraint("user_id", "telegram_thread_id", name="uq_personas_user_thread"),
        UniqueConstraint("user_id", "id", name="uq_personas_user_id_id"),
    )


class CoreFact(Base):
    """A versioned hard fact. Rows are immutable except ``valid_to`` (trigger); one current row per (user, key)."""

    __tablename__ = "core_facts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("core_facts")
    persona_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    category: Mapped[FactCategory] = mapped_column(pg_enum(FactCategory, "fact_category"), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_num: Mapped[Decimal | None] = mapped_column(Numeric)
    value_unit: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[Any | None] = mapped_column(JSONB)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("50"))
    source: Mapped[FactSource] = mapped_column(pg_enum(FactSource, "fact_source"), nullable=False)
    source_pending_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # The user message a source='chat_inline' fact came from (migration 0010; the CHECK and the trigger live in SQL only).
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "persona_id"],
            ["personas.user_id", "personas.id"],
            name="fk_core_facts_persona",
            ondelete="SET NULL (persona_id)",  # PostgreSQL 15+: keep user_id, null only persona_id
        ),
        ForeignKeyConstraint(
            ["user_id", "source_pending_id"],
            ["pending_facts.user_id", "pending_facts.id"],
            name="fk_core_facts_source_pending",
        ),
        ForeignKeyConstraint(
            ["user_id", "source_message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_core_facts_source_message",  # NO ACTION: a version is immutable, the FK must never null the column
        ),
        UniqueConstraint("user_id", "id", name="uq_core_facts_user_id_id"),
        Index("uq_core_facts_current_key", "user_id", "key", unique=True, postgresql_where=text("valid_to IS NULL")),
        Index("ix_core_facts_user_key_history", "user_id", "key", "valid_from"),
    )


class EmotionalState(Base):
    """Temporary emotional state ("chemistry"). AI-inferred states are capped at priority 60 (CHECK)."""

    __tablename__ = "emotional_states"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("emotional_states")
    label: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    intensity: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source: Mapped[EmotionSource] = mapped_column(pg_enum(EmotionSource, "emotion_source"), nullable=False)
    source_pending_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    mood: Mapped[SharedMood | None] = mapped_column(pg_enum(SharedMood, "shared_mood"))  # only on user_confirmed states (CHECK)
    status: Mapped[EmotionStatus] = mapped_column(
        pg_enum(EmotionStatus, "emotion_status"), nullable=False, server_default=text("'active'")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "source_pending_id"],
            ["pending_facts.user_id", "pending_facts.id"],
            name="fk_emotional_states_source_pending",
        ),
        UniqueConstraint("user_id", "id", name="uq_emotional_states_user_id_id"),
        Index(
            "ix_emotional_states_user_active",
            "user_id",
            "expires_at",
            postgresql_where=text("status = 'active'"),
        ),
    )


class EmotionalOverride(Base):
    """Links an emotional state to the principle VERSION it overrides (resolved by view effective_principles)."""

    __tablename__ = "emotional_overrides"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("emotional_overrides")
    emotional_state_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    core_fact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    mode: Mapped[OverrideMode] = mapped_column(
        pg_enum(OverrideMode, "override_mode"), nullable=False, server_default=text("'suspend'")
    )
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "emotional_state_id"],
            ["emotional_states.user_id", "emotional_states.id"],
            name="fk_emotional_overrides_state",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["user_id", "core_fact_id"],
            ["core_facts.user_id", "core_facts.id"],
            name="fk_emotional_overrides_fact",
            ondelete="CASCADE",
        ),
        UniqueConstraint("emotional_state_id", "core_fact_id", name="uq_emotional_overrides_state_fact"),
        Index("ix_emotional_overrides_user_fact", "user_id", "core_fact_id"),
    )

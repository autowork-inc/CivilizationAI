"""Hard schedule (events). Created from the Telegram Mini App; the LLM never invents dates.
DDL source of truth: migrations/sql/0001."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Text, Time, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.enums import EventStatus, EventVisibility, pg_enum
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class ProactiveSchedule(Base):
    """At what LOCAL time (users.timezone) a proactive trigger fires for this user; at most one nudge per day."""

    __tablename__ = "proactive_schedule"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("proactive_schedule")
    trigger_key: Mapped[str] = mapped_column(Text, nullable=False)
    local_time: Mapped[time] = mapped_column(Time, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_fired_on: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (UniqueConstraint("user_id", "trigger_key", name="uq_proactive_schedule_user_trigger"),)


class ProactiveCheckinLog(Base):
    """One proactive check-in per (tenant-local day, slot): the row is the claim and the record that it was sent (migration 0012)."""

    __tablename__ = "proactive_checkin_log"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("proactive_checkin_log")
    slot_date: Mapped[date] = mapped_column(Date, nullable=False)
    slot_time: Mapped[time] = mapped_column(Time, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "slot_date", "slot_time", name="uq_proactive_checkin_log_slot"),
        Index("ix_proactive_checkin_log_user_sent", "user_id", "sent_at"),
    )


class EventProposal(Base):
    """``user_id`` (the proposer) asks ``recipient_user_id`` to a meeting (migration 0008). Written ONLY by the SECURITY DEFINER functions
    propose_event / respond_event_proposal: the application role may just read the rows of which it is a party."""

    __tablename__ = "event_proposals"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("event_proposals")
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_event_proposals_recipient"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    proposer_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL", name="fk_event_proposals_proposer_event")
    )
    recipient_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id", ondelete="SET NULL", name="fk_event_proposals_recipient_event")
    )

    __table_args__ = (
        Index(
            "uq_event_proposals_pending",
            "user_id",
            "recipient_user_id",
            "title",
            "starts_at",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_event_proposals_recipient", "recipient_user_id", "created_at"),
        Index("ix_event_proposals_pair_created", "user_id", "recipient_user_id", "created_at"),
        Index("ix_event_proposals_open_expiry", "expires_at", postgresql_where=text("status = 'pending'")),
    )


class EventDraft(Base):
    """"Put it into your calendar?": the user's OWN plan heard in conversation, waiting for the button (migration 0008).
    Plain tenant table: the bot writes it, the confirmation turns it into an ``events`` row (``created_via='bot_confirmed'``)."""

    __tablename__ = "event_drafts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("event_drafts")
    persona_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # the step this slot is for (migration 0013)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "persona_id"],
            ["personas.user_id", "personas.id"],
            name="fk_event_drafts_persona",
            ondelete="SET NULL (persona_id)",  # PostgreSQL 15+
        ),
        ForeignKeyConstraint(
            ["user_id", "event_id"],
            ["events.user_id", "events.id"],
            name="fk_event_drafts_event",
            ondelete="SET NULL (event_id)",
        ),
        ForeignKeyConstraint(
            ["user_id", "task_id"],
            ["tasks.user_id", "tasks.id"],
            name="fk_event_drafts_task",
            ondelete="SET NULL (task_id)",
        ),
        Index("uq_event_drafts_pending", "user_id", "title", "starts_at", unique=True, postgresql_where=text("status = 'pending'")),
        Index("ix_event_drafts_user_created", "user_id", "created_at"),
        Index("ix_event_drafts_open_expiry", "expires_at", postgresql_where=text("status = 'pending'")),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("events")
    persona_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    all_day: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    timezone: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[EventStatus] = mapped_column(
        pg_enum(EventStatus, "event_status"), nullable=False, server_default=text("'planned'")
    )
    recurrence_rule: Mapped[str | None] = mapped_column(Text)
    parent_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    original_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    visibility: Mapped[EventVisibility] = mapped_column(
        pg_enum(EventVisibility, "event_visibility"), nullable=False, server_default=text("'busy_only'")
    )
    created_via: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "persona_id"],
            ["personas.user_id", "personas.id"],
            name="fk_events_persona",
            ondelete="SET NULL (persona_id)",  # PostgreSQL 15+
        ),
        ForeignKeyConstraint(
            ["user_id", "parent_event_id"],
            ["events.user_id", "events.id"],
            name="fk_events_parent",
            ondelete="CASCADE",
        ),
        UniqueConstraint("user_id", "id", name="uq_events_user_id_id"),
        Index("ix_events_user_starts", "user_id", "starts_at"),
        Index("ix_events_user_persona_starts", "user_id", "persona_id", "starts_at"),
        Index(
            "uq_events_exception",
            "user_id",
            "parent_event_id",
            "original_start",
            unique=True,
            postgresql_where=text("parent_event_id IS NOT NULL"),
        ),
    )

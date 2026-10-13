"""Messages (Tier 3 source of truth), Outbox and Tier 2 episodic memory. DDL source of truth: migrations/sql/0001."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.enums import (
    EpisodeKind,
    MessageRole,
    OutboxOperation,
    OutboxStatus,
    Sensitivity,
    pg_enum,
)
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk

EMBEDDING_DIM = 1536  # gemini-embedding-001 with output_dimensionality=1536 (native 3072 exceeds the 2000-dim HNSW limit)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("messages")
    persona_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    role: Mapped[MessageRole] = mapped_column(pg_enum(MessageRole, "message_role"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    # a Diplomat answer carries ANOTHER person's data: the nightly GC skips it so that it cannot outlive the grant (0007)
    third_party_data: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    # The mobile app (0026): the id the app made for a message it sent (a retried request is a no-op), and for an assistant message the
    # user message it answers (a repeated request is answered from the stored reply). CHECK ck_messages_reply_not_self lives in SQL only.
    client_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    cards: Mapped[Any] = mapped_column(JSONB, nullable=True)  # the cards of an assistant reply as the mobile client got them (None for a Telegram message)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "persona_id"],
            ["personas.user_id", "personas.id"],
            name="fk_messages_persona",
            ondelete="SET NULL (persona_id)",  # PostgreSQL 15+
        ),
        ForeignKeyConstraint(
            ["user_id", "reply_to_id"],
            ["messages.user_id", "messages.id"],
            name="fk_messages_reply_to",
            ondelete="SET NULL (reply_to_id)",
        ),
        Index(
            "uq_messages_user_client_id", "user_id", "client_message_id", unique=True, postgresql_where=text("client_message_id IS NOT NULL")
        ),
        Index("uq_messages_user_reply_to", "user_id", "reply_to_id", unique=True, postgresql_where=text("reply_to_id IS NOT NULL")),
        UniqueConstraint("user_id", "telegram_message_id", name="uq_messages_user_telegram"),
        UniqueConstraint("user_id", "id", name="uq_messages_user_id_id"),  # target of the tenant-safe FK from core_facts.source_message_id (0010)
        Index("ix_messages_user_created", "user_id", "created_at"),
        Index("ix_messages_user_persona_created", "user_id", "persona_id", "created_at"),
    )


class MessageAttachment(Base):
    """What the user attached to a message (migration 0011): a record, never the bytes. CHECKs and RLS live in SQL only."""

    __tablename__ = "message_attachments"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("message_attachments")
    message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # photo | document
    content_kind: Mapped[str] = mapped_column(Text, nullable=False)  # image | pdf | txt | docx
    mime_type: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(Text, nullable=False)
    telegram_file_id: Mapped[str] = mapped_column(Text, nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    text_truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_message_attachments_message",
            ondelete="CASCADE",
        ),
        UniqueConstraint("user_id", "message_id", name="uq_message_attachments_message"),
    )


class MemoryOutbox(Base):
    """Memory work that must survive crashes. Done rows are kept (status='done') and purged by TTL (7 days)."""

    __tablename__ = "memory_outbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = tenant_fk("memory_outbox")
    operation: Mapped[OutboxOperation] = mapped_column(pg_enum(OutboxOperation, "outbox_operation"), nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        pg_enum(OutboxStatus, "outbox_status"), nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("5"))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_memory_outbox_user_idem"),
        Index(
            "ix_memory_outbox_open",
            "created_at",
            postgresql_where=text("status IN ('pending', 'processing', 'failed')"),
        ),
        Index("ix_memory_outbox_done_ttl", "processed_at", postgresql_where=text("status = 'done'")),
    )


class EpisodicMemory(Base):
    __tablename__ = "episodic_memories"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("episodic_memories")
    persona_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[EpisodeKind] = mapped_column(pg_enum(EpisodeKind, "episode_kind"), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[Any] = mapped_column(Vector(EMBEDDING_DIM), nullable=False)
    embedding_model: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_to: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_message_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=text("'{}'")
    )
    importance: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("5"))
    sensitivity: Mapped[Sensitivity] = mapped_column(
        pg_enum(Sensitivity, "sensitivity"), nullable=False, server_default=text("'normal'")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "persona_id"],
            ["personas.user_id", "personas.id"],
            name="fk_episodic_memories_persona",
            ondelete="SET NULL (persona_id)",  # PostgreSQL 15+
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_episodic_memories_user_idem"),
        Index("ix_episodic_memories_user_time", "user_id", "occurred_from"),
        Index("ix_episodic_memories_user_persona", "user_id", "persona_id"),
        Index(
            "ix_episodic_memories_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

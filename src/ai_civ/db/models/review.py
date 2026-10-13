"""Quarantine ("pending_facts") and weekly review batches. DDL source of truth: migrations/sql/0001."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.enums import ChangeType, FactCategory, PendingStatus, PendingSubject, SharedMood, pg_enum
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class ReviewBatch(Base):
    """One weekly "keep or delete?" question sent to the user."""

    __tablename__ = "review_batches"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("review_batches")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (UniqueConstraint("user_id", "id", name="uq_review_batches_user_id_id"),)


class PendingFact(Base):
    """A fact or emotional state proposed by the Garbage Collector. Invisible to prompts until approved by the user.

    ``subject`` selects which group of columns is meaningful (enforced by CHECK constraints).
    """

    __tablename__ = "pending_facts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("pending_facts")
    subject: Mapped[PendingSubject] = mapped_column(
        pg_enum(PendingSubject, "pending_subject"), nullable=False, server_default=text("'core_fact'")
    )
    change: Mapped[ChangeType] = mapped_column(pg_enum(ChangeType, "change_type"), nullable=False)
    target_fact_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    # core fact proposal
    category: Mapped[FactCategory | None] = mapped_column(pg_enum(FactCategory, "fact_category"))
    key: Mapped[str | None] = mapped_column(Text)
    value_text: Mapped[str | None] = mapped_column(Text)
    value_num: Mapped[Decimal | None] = mapped_column(Numeric)
    value_unit: Mapped[str | None] = mapped_column(Text)
    value_json: Mapped[Any | None] = mapped_column(JSONB)
    priority: Mapped[int | None] = mapped_column(SmallInteger)
    # emotional state proposal (priority up to 100 allowed here: needs the user's confirmation)
    emotion_label: Mapped[str | None] = mapped_column(Text)
    emotion_description: Mapped[str | None] = mapped_column(Text)
    emotion_intensity: Mapped[int | None] = mapped_column(SmallInteger)
    emotion_priority: Mapped[int | None] = mapped_column(SmallInteger)
    emotion_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    emotion_override_keys: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    emotion_mood: Mapped[SharedMood | None] = mapped_column(pg_enum(SharedMood, "shared_mood"))  # what contacts may be told
    # provenance
    evidence: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    extractor_confidence: Mapped[Decimal | None] = mapped_column(Numeric)
    # moderation
    status: Mapped[PendingStatus] = mapped_column(
        pg_enum(PendingStatus, "pending_status"), nullable=False, server_default=text("'pending'")
    )
    review_batch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "review_batch_id"],
            ["review_batches.user_id", "review_batches.id"],
            name="fk_pending_facts_batch",
        ),
        ForeignKeyConstraint(
            ["user_id", "target_fact_id"],
            ["core_facts.user_id", "core_facts.id"],
            name="fk_pending_facts_target",
            ondelete="CASCADE",
            use_alter=True,  # breaks the pending_facts <-> core_facts cycle (the migration also adds it via ALTER)
        ),
        UniqueConstraint("user_id", "id", name="uq_pending_facts_user_id_id"),
        UniqueConstraint("user_id", "idempotency_key", name="uq_pending_facts_user_idem"),
        Index("ix_pending_facts_user_status", "user_id", "status", "created_at"),
    )

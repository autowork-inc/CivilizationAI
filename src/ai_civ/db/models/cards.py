"""Spaced repetition (migration 0014). DDL source of truth: migrations/sql/0014."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Integer, Numeric, SmallInteger, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class Card(Base):
    """A multiple-choice card: a question, EXACTLY four distinct options, the index of the right one (the CHECKs live in the SQL)."""

    __tablename__ = "cards"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("cards")
    deck: Mapped[str] = mapped_column(Text, nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    options: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    correct_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_cards_user_id_id"),
        Index("uq_cards_user_deck_question", "user_id", "deck", "question", unique=True),
        Index("ix_cards_user_deck", "user_id", "deck"),
    )


class CardReview(Base):
    """The SM-2 state of ONE card. Awaiting an answer = ``last_sent_at`` is set and newer than ``reviewed_at`` (or the card was never answered)."""

    __tablename__ = "card_reviews"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("card_reviews")
    card_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'new'"))
    interval_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ease: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False, server_default=text("2.50"))
    reps: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lapses: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_review_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_chosen: Mapped[int | None] = mapped_column(SmallInteger)

    __table_args__ = (
        UniqueConstraint("user_id", "card_id", name="uq_card_reviews_card"),
        ForeignKeyConstraint(["user_id", "card_id"], ["cards.user_id", "cards.id"], name="fk_card_reviews_card", ondelete="CASCADE"),
        Index("ix_card_reviews_user_due", "user_id", "next_review_at"),
        Index("ix_card_reviews_user_sent", "user_id", "last_sent_at"),
    )

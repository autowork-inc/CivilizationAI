"""Places and geo notes (migration 0021). DDL source of truth: migrations/sql/0021."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Integer, Numeric, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class Place(Base):
    """A place the tenant named (home, gym, office ...): the centre and the radius (metres) inside which a sent location counts as "being there"."""

    __tablename__ = "places"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("places")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'"))
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    radius_meters: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("150"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_places_user_id_id"),
        UniqueConstraint("user_id", "name", name="uq_places_user_name"),
    )


class GeoNote(Base):
    """A note tied to a place (``place_id``; before the place exists only ``place_name``) and / or to a point of its own (latitude / longitude)."""

    __tablename__ = "geo_notes"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("geo_notes")
    place_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    place_name: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(9, 6))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_geo_notes_user_id_id"),
        ForeignKeyConstraint(
            ["user_id", "place_id"],
            ["places.user_id", "places.id"],
            name="fk_geo_notes_place",
            ondelete="SET NULL (place_id)",  # PostgreSQL 15+
        ),
        ForeignKeyConstraint(
            ["user_id", "source_message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_geo_notes_message",
            ondelete="SET NULL (source_message_id)",
        ),
        Index("ix_geo_notes_user_status", "user_id", "status"),
        Index("ix_geo_notes_user_place", "user_id", "place_id"),
        Index("ix_geo_notes_user_message", "user_id", "source_message_id"),
    )

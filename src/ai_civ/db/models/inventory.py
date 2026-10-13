"""Smart inventory of consumables (migration 0020). DDL source of truth: migrations/sql/0020."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKeyConstraint, Index, Integer, Numeric, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class InventoryItem(Base):
    """A consumable the tenant keeps stock of. ``current_quantity`` is in ``unit`` (one of a closed list, CHECK in the SQL); ``daily_burn_rate`` 0 = no
    automatic burn and no reminders. ``last_burn_on`` is the last tenant-local day whose burn was applied; ``reminder_token`` marks the open reminder card."""

    __tablename__ = "inventory_items"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("inventory_items")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    current_quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, server_default=text("0"))
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    daily_burn_rate: Mapped[Decimal] = mapped_column(Numeric(12, 3), nullable=False, server_default=text("0"))
    restock_threshold_days: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    last_restocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_burn_on: Mapped[date] = mapped_column(Date, nullable=False, server_default=text("(CURRENT_DATE - 1)"))
    last_reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reminder_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        UniqueConstraint("user_id", "id", name="uq_inventory_items_user_id_id"),
        UniqueConstraint("user_id", "name", name="uq_inventory_items_user_name"),
    )


class InventoryMovement(Base):
    """One change of an item's quantity (append-only audit trail): + restock, - consumption; ``quantity_after`` is the stock right after it."""

    __tablename__ = "inventory_movements"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("inventory_movements")
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    change_amount: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    quantity_after: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        ForeignKeyConstraint(["user_id", "item_id"], ["inventory_items.user_id", "inventory_items.id"], name="fk_inventory_movements_item", ondelete="CASCADE"),
        ForeignKeyConstraint(
            ["user_id", "source_message_id"],
            ["messages.user_id", "messages.id"],
            name="fk_inventory_movements_message",
            ondelete="SET NULL (source_message_id)",  # PostgreSQL 15+
        ),
        Index("uq_inventory_movements_message", "user_id", "item_id", "source_message_id", unique=True, postgresql_where=text("source_message_id IS NOT NULL")),
        Index("ix_inventory_movements_user_item", "user_id", "item_id", "created_at"),
    )

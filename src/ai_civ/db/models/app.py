"""Sessions of the mobile app (migration 0026). DDL source of truth: migrations/sql/0026."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Text
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class UserSession(Base):
    """One signed-in device of an app user: only the HASH of its refresh token is stored. The app holds ``<user_id>.<session_id>.<secret>``;
    a refresh opens the tenant session of ``user_id`` and finds the row by ``session_id`` (RLS keeps other tenants' rows out)."""

    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("user_sessions")
    refresh_token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (Index("ix_user_sessions_user_expires", "user_id", "expires_at"),)

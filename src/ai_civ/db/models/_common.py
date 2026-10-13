from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import mapped_column

UUID_PK_DEFAULT = text("gen_random_uuid()")
NOW = text("now()")


def uuid_pk() -> Any:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=UUID_PK_DEFAULT)


def tenant_fk(table: str) -> Any:
    """user_id -> users.id (the tenant). Every table except users has one (project rule: multi-tenant by design)."""
    return mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name=f"fk_{table}_user"),
        nullable=False,
    )

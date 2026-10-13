"""Social graph and ACL foundation ("Diplomat" pattern). DDL source of truth: migrations/sql/0001."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from ai_civ.db.base import Base
from ai_civ.db.enums import AclScope, ContactStatus, OutboxStatus, pg_enum
from ai_civ.db.models._common import NOW, tenant_fk, uuid_pk


class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("contacts")
    contact_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE", name="fk_contacts_contact_user"),
        nullable=False,
    )
    alias: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ContactStatus] = mapped_column(
        pg_enum(ContactStatus, "contact_status"), nullable=False, server_default=text("'pending'")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (UniqueConstraint("user_id", "contact_user_id", name="uq_contacts_user_contact"),)


class AccessGrant(Base):
    """ACL row: ``user_id`` allows ``grantee_user_id`` to request ``scope``. Default is deny."""

    __tablename__ = "access_grants"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("access_grants")
    grantee_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    scope: Mapped[AclScope] = mapped_column(pg_enum(AclScope, "acl_scope"), nullable=False)
    constraints: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["user_id", "grantee_user_id"],
            ["contacts.user_id", "contacts.contact_user_id"],
            name="fk_access_grants_contact",
            ondelete="CASCADE",
        ),
        Index(
            "uq_access_grants_active",
            "user_id",
            "grantee_user_id",
            "scope",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )


class ContactInvite(Base):
    """A single-use invite link of ``user_id`` (migration 0005). Only the SHA-256 of the token is stored. Written ONLY by the
    SECURITY DEFINER functions create_invite / revoke_invites / accept_invite: the application role may just read its own rows."""

    __tablename__ = "contact_invites"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = tenant_fk("contact_invites")
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL", name="fk_contact_invites_used_by")
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_contact_invites_token"),
        Index(
            "ix_contact_invites_user_open",
            "user_id",
            "expires_at",
            postgresql_where=text("used_at IS NULL AND revoked_at IS NULL"),
        ),
    )


class NotificationOutbox(Base):
    """Something to tell a tenant (migration 0005), delivered by a worker: claim / lease / fencing / backoff / dead, like
    memory_outbox. Rows are created by SECURITY DEFINER functions; the application only claims and finishes its own."""

    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = tenant_fk("notification_outbox")
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False, server_default=text("'{}'"))
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
        UniqueConstraint("user_id", "idempotency_key", name="uq_notification_outbox_user_idem"),
        Index(
            "ix_notification_outbox_open",
            "created_at",
            postgresql_where=text("status IN ('pending', 'processing', 'failed')"),
        ),
        Index("ix_notification_outbox_done_ttl", "processed_at", postgresql_where=text("status = 'done'")),
    )


class DiplomatAuditLog(Base):
    """Who asked what about whom. Read-only for the application role; written by SECURITY DEFINER functions."""

    __tablename__ = "diplomat_audit_log"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = tenant_fk("diplomat_audit_log")
    requester_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_diplomat_audit_log_requester"),
    )
    scope: Mapped[AclScope] = mapped_column(pg_enum(AclScope, "acl_scope"), nullable=False)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)

    __table_args__ = (
        Index("ix_diplomat_audit_log_user_created", "user_id", "created_at"),
        Index("ix_diplomat_audit_log_pair", "user_id", "requester_user_id", "scope", "created_at"),  # rate limit (0007)
    )

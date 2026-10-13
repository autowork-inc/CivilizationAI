"""Engine, tenant-scoped sessions and the fail-fast RLS check.

Every database access in the application goes through ``Database.tenant_session(user_id)``: it opens ONE transaction
and binds it to a tenant with ``set_config('app.user_id', <uuid>, true)`` (is_local = true, so the value dies with the
transaction and is safe with transaction poolers). PostgreSQL Row Level Security then makes every table invisible
outside that tenant. There is intentionally no way to open a session without a tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from ai_civ.config import Settings, get_database_url, get_settings
from ai_civ.db.enums import RegistrationOutcome


class RlsNotEnforcedError(RuntimeError):
    """The connected role is not subject to Row Level Security (owner, superuser or BYPASSRLS)."""


@dataclass(frozen=True)
class Tenant:
    id: uuid.UUID
    timezone: str


@dataclass(frozen=True)
class AppRegistration:
    tenant_id: uuid.UUID | None  # None for ``email_taken``
    outcome: str  # created | email_taken


@dataclass(frozen=True)
class AppCredentials:
    tenant_id: uuid.UUID
    pass_hash: str
    timezone: str


@dataclass(frozen=True)
class Registration:
    tenant_id: uuid.UUID | None  # None only for ``chat_taken`` of a person who is not registered
    outcome: RegistrationOutcome


@dataclass(frozen=True)
class Relocation:
    tenant_id: uuid.UUID | None
    outcome: str  # relocated | already_here | no_tenant | chat_taken
    old_chat_id: int | None


@dataclass(frozen=True)
class PurgeStats:
    reaped: int  # notifications out of attempts -> dead
    expired: int  # notifications older than the maximum age -> dead
    purged: int  # delivered notifications deleted
    invites: int  # ended invites deleted


@dataclass(frozen=True)
class EventPurgeStats:
    drafts_expired: int  # open quick-event drafts that ran out of time
    drafts_purged: int  # decided / expired drafts deleted
    proposals_expired: int  # open invitations that ran out of time
    proposals_purged: int  # decided / expired invitations deleted


def engine_options(settings: Settings) -> dict[str, Any]:
    """Keyword arguments for create_async_engine, depending on the Supabase connection type."""
    options: dict[str, Any] = {"echo": settings.db_echo, "pool_pre_ping": True}
    if settings.db_pooler_mode == "transaction":
        # Supabase transaction pooler (port 6543) does not support prepared statements. Disable both the asyncpg
        # and the SQLAlchemy-dialect statement caches and give prepared statements unique names.
        options["poolclass"] = NullPool
        options["connect_args"] = {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid.uuid4()}__",
        }
    else:
        options["pool_size"] = settings.db_pool_size
        options["max_overflow"] = settings.db_max_overflow
    return options


class Database:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Database:
        settings = settings or get_settings()
        return cls(create_async_engine(get_database_url(settings), **engine_options(settings)))

    @asynccontextmanager
    async def tenant_session(self, user_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
        """One transaction bound to one tenant. Commits on success, rolls back on any exception.

        Do not call ``session.commit()`` inside the block: the context manager owns the transaction.
        """
        if not isinstance(user_id, uuid.UUID):
            raise TypeError(f"user_id must be uuid.UUID, got {type(user_id).__name__}")
        async with self._sessionmaker() as session:
            async with session.begin():
                await session.execute(text("SELECT set_config('app.user_id', :uid, true)"), {"uid": str(user_id)})
                yield session

    async def resolve_tenant_id(
        self, *, telegram_user_id: int | None = None, forum_chat_id: int | None = None
    ) -> uuid.UUID | None:
        """Map a Telegram identity to a tenant id (the only lookup that needs no tenant context).

        Backed by the SECURITY DEFINER function civ.resolve_tenant_id, which returns nothing but the uuid.
        """
        async with self._sessionmaker() as session:
            async with session.begin():
                value = (
                    await session.execute(
                        text("SELECT civ.resolve_tenant_id(:tg, :chat)"),
                        {"tg": telegram_user_id, "chat": forum_chat_id},
                    )
                ).scalar_one()
        return None if value is None else uuid.UUID(str(value))

    async def register_tenant(self, *, telegram_user_id: int, forum_chat_id: int) -> Registration:
        """Create a tenant for a person who owns a forum group, or say why not (see the outcomes in migration 0004).

        Backed by the SECURITY DEFINER function civ.register_tenant: atomic and safe to call concurrently / repeatedly.
        The function trusts ``telegram_user_id``: the caller must have verified it against Telegram first.
        """
        async with self._sessionmaker() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        text("SELECT tenant_id, outcome FROM civ.register_tenant(:tg, :chat)"),
                        {"tg": telegram_user_id, "chat": forum_chat_id},
                    )
                ).one()
        return Registration(
            tenant_id=None if row[0] is None else uuid.UUID(str(row[0])), outcome=RegistrationOutcome(row[1])
        )

    async def register_app_user(self, *, email: str, pass_hash: str, timezone: str = "UTC") -> AppRegistration:
        """Create the tenant of a mobile-app person (no Telegram): onboarding done, the system personas and the character profile are made by the
        SECURITY DEFINER function ``civ.register_app_user``. ``pass_hash`` is an ALREADY HASHED password. ``email_taken`` is a value, not an exception."""
        async with self._sessionmaker() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        text("SELECT tenant_id, outcome FROM civ.register_app_user(:email, :hash, :tz)"),
                        {"email": email, "hash": pass_hash, "tz": timezone},
                    )
                ).one()
        return AppRegistration(None if row[0] is None else uuid.UUID(str(row[0])), row[1])

    async def resolve_tenant_by_email(self, email: str) -> AppCredentials | None:
        """What a sign-in needs before it knows the tenant: id, stored password hash, time zone (None = no such person with a password)."""
        async with self._sessionmaker() as session:
            async with session.begin():
                row = (
                    await session.execute(text("SELECT id, pass_hash, timezone FROM civ.resolve_tenant_by_email(:email)"), {"email": email})
                ).one_or_none()
        return None if row is None else AppCredentials(uuid.UUID(str(row[0])), row[1], row[2])

    async def relocate_group(self, *, telegram_user_id: int, forum_chat_id: int) -> Relocation:
        """Point an existing tenant at a NEW forum group (migration 0025). Topics and the Telegram message numbers of the old group are reset, nothing is deleted.

        Like ``register_tenant`` it trusts the caller: the bot has already checked with Telegram that the new group was made for this person and that the old one is broken."""
        async with self._sessionmaker() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        text("SELECT tenant_id, outcome, old_chat_id FROM civ.relocate_group(:tg, :chat)"),
                        {"tg": telegram_user_id, "chat": forum_chat_id},
                    )
                ).one()
        return Relocation(
            tenant_id=None if row[0] is None else uuid.UUID(str(row[0])), outcome=row[1], old_chat_id=None if row[2] is None else int(row[2])
        )

    async def list_tenants(self) -> list[Tenant]:
        """Every tenant id with its time zone, for background dispatchers (Celery Beat).

        Backed by the SECURITY DEFINER function civ.list_tenants (migration 0002). Use it ONLY to decide which tenants to
        visit; all work on a tenant's data must go through ``tenant_session``.
        """
        async with self._sessionmaker() as session:
            async with session.begin():
                rows = (await session.execute(text("SELECT tenant_id, tenant_timezone FROM civ.list_tenants()"))).all()
        return [Tenant(id=uuid.UUID(str(r[0])), timezone=r[1]) for r in rows]

    async def tenants_with_pending_notifications(self, *, limit: int) -> list[uuid.UUID]:
        """Tenants that have a notification deliverable right now (active group, onboarding done), at most ``limit``.

        Backed by the SECURITY DEFINER function civ.pending_notification_tenants (migration 0005): one indexed query, not a scan of
        every tenant. Use it ONLY to decide whom to visit; the delivery itself goes through ``tenant_session``.
        """
        async with self._sessionmaker() as session:
            async with session.begin():
                rows = (
                    await session.execute(text("SELECT tenant_id FROM civ.pending_notification_tenants(:n)"), {"n": limit})
                ).all()
        return [uuid.UUID(str(r[0])) for r in rows]

    async def tenants_with_due_cards(self, *, limit: int, resend_hours: float) -> list[uuid.UUID]:
        """Tenants that have a flashcard to send right now (civ.cards_due_tenants, migration 0014): one indexed query. Use it ONLY to decide
        whom to visit; quiet hours, the daily limit and the claim itself are the worker's (``Cards.claim_due``)."""
        async with self._sessionmaker() as session:
            async with session.begin():
                rows = (
                    await session.execute(
                        text("SELECT tenant_id FROM civ.cards_due_tenants(:n, make_interval(secs => :s))"), {"n": limit, "s": resend_hours * 3600}
                    )
                ).all()
        return [uuid.UUID(str(r[0])) for r in rows]

    async def tenants_with_due_inventory(self, *, limit: int, local_hour: int, cooldown_days: float) -> list[uuid.UUID]:
        """Tenants whose local clock is past ``local_hour`` and that have a consumable to burn or to remind about (civ.inventory_due_tenants, migration 0020):
        one indexed query. Use it ONLY to decide whom to visit; quiet hours and the claims are the worker's (``Inventory.claim_due``)."""
        async with self._sessionmaker() as session:
            async with session.begin():
                rows = (
                    await session.execute(
                        text("SELECT tenant_id FROM civ.inventory_due_tenants(:n, :h, make_interval(secs => :c))"),
                        {"n": limit, "h": local_hour, "c": cooldown_days * 86400},
                    )
                ).all()
        return [uuid.UUID(str(r[0])) for r in rows]

    async def tenants_with_due_capsules(self, *, limit: int, lease_minutes: float) -> list[uuid.UUID]:
        """Tenants with a time capsule that is due and not under a live lease (civ.time_capsules_due_tenants, migration 0022): one indexed query. Use it ONLY to
        decide whom to visit; quiet hours and the claim itself are the worker's (``TimeCapsules.claim_due``)."""
        async with self._sessionmaker() as session:
            async with session.begin():
                rows = (
                    await session.execute(
                        text("SELECT tenant_id FROM civ.time_capsules_due_tenants(:n, make_interval(secs => :s))"), {"n": limit, "s": lease_minutes * 60}
                    )
                ).all()
        return [uuid.UUID(str(r[0])) for r in rows]

    async def purge_social(self, *, done_ttl_days: int, notification_max_age_days: int, invite_ttl_days: int) -> PurgeStats:
        """Housekeeping of notifications and ended invites for ALL tenants in one call (civ.purge_social, migration 0005)."""
        async with self._sessionmaker() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        text("SELECT out_reaped, out_expired, out_purged, out_invites FROM civ.purge_social(:d, :m, :i)"),
                        {"d": done_ttl_days, "m": notification_max_age_days, "i": invite_ttl_days},
                    )
                ).one()
        return PurgeStats(reaped=row[0], expired=row[1], purged=row[2], invites=row[3])

    async def purge_events(self, *, draft_ttl_days: int, proposal_ttl_days: int) -> EventPurgeStats:
        """Housekeeping of quick-event drafts and invitations for ALL tenants in one call (civ.purge_events, migration 0008)."""
        async with self._sessionmaker() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        text(
                            "SELECT out_drafts_expired, out_drafts_purged, out_proposals_expired, out_proposals_purged "
                            "FROM civ.purge_events(:d, :p)"
                        ),
                        {"d": draft_ttl_days, "p": proposal_ttl_days},
                    )
                ).one()
        return EventPurgeStats(drafts_expired=row[0], drafts_purged=row[1], proposals_expired=row[2], proposals_purged=row[3])

    async def verify_rls_enforced(self) -> None:
        """Fail-fast guard: call once at application / worker startup.

        Raises RlsNotEnforcedError if row level security does not apply to the connected role
        (superuser, BYPASSRLS, or the owner of the tables) - in that case tenant isolation would be silently off.
        """
        async with self._sessionmaker() as session:
            async with session.begin():
                active = (
                    await session.execute(text("SELECT pg_catalog.row_security_active('civ.users'::regclass)"))
                ).scalar_one()
                who = (await session.execute(text("SELECT current_user"))).scalar_one()
        if not active:
            raise RlsNotEnforcedError(
                f"connected as {who!r}: Row Level Security is NOT enforced for this role. "
                "Use the runtime role ai_civ_app in DATABASE_URL (the owner role is for Alembic only)."
            )

    async def dispose(self) -> None:
        await self.engine.dispose()

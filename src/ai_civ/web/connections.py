"""Screen "Связи": the address book, invite links, what each contact is allowed to ask for, and the journal of who asked.

Reading is plain SELECTs under RLS (``tenant_session``). Every WRITE goes through a ``SECURITY DEFINER`` function of migrations 0005 /
0006, because the application role cannot write ``civ.contacts`` at all: it could otherwise declare itself "accepted". The functions
learn who is calling from the tenant session, take the pair lock first, and return an outcome the route turns into an answer; this
module never trusts a client for "who": a contact is addressed by the id of the CALLER's own row, and the other person's ids
(tenant uuid, Telegram id) never leave the server.

Each mutation answers with the whole fresh snapshot, so the page just replaces what it shows. The journal (``diplomat_audit_log``)
is read-only for the application: it is written only by the future Diplomat functions, so it is empty until they exist.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Path, Query, Request
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_civ.bot import invites
from ai_civ.bot.invites import CreateOutcome
from ai_civ.config import Settings
from ai_civ.db.enums import ContactStatus
from ai_civ.db.models import AccessGrant, Contact, ContactInvite, DiplomatAuditLog
from ai_civ.web.deps import DB, ApiError, Auth

router = APIRouter(prefix="/connections", tags=["connections"])

MAX_OPEN_INVITES = 5  # mirrors civ.create_invite (the database enforces it; this is only shown to the user)
SHARE_TEXT = "Давай свяжемся через бота. Ссылка одноразовая."
NO_SUCH_CONTACT = "Этого контакта уже нет в списке."

# The scopes the screen offers (the database knows more: calendar_details, profile_public, persona_status). Nothing outside this list
# can be switched from the page; the order is the order on screen.
SHARED_SCOPES: dict[str, tuple[str, str]] = {
    "calendar_free_busy": (
        "Занят или свободен",
        "Человек сможет узнать, занят ли я в нужное ему время. Названий и подробностей событий он не увидит.",
    ),
    "emotional_state_label": (
        "Моё состояние",
        "Человек сможет узнать короткую метку моего состояния, например «устал». Без причин и без переписки.",
    ),
}
# Names for the journal, which may hold any scope.
SCOPE_TITLES: dict[str, str] = {
    **{key: title for key, (title, _) in SHARED_SCOPES.items()},
    "calendar_details": "Подробности календаря",
    "profile_public": "Публичный профиль",
    "persona_status": "Статус темы",
}

ContactId = Annotated[uuid.UUID, Path()]
InviteId = Annotated[uuid.UUID, Path()]


class AliasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alias: str

    @field_validator("alias")
    @classmethod
    def _name(cls, value: str) -> str:
        return invites.check_name(value)


class GrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ScopeOut(BaseModel):
    key: str
    title: str
    description: str


class ContactOut(BaseModel):
    id: uuid.UUID  # the id of the caller's own row: the handle for every action below
    alias: str | None
    status: str  # accepted | blocked | pending
    connected_since: datetime
    grants: dict[str, bool]  # offered scope -> in force now (a blocked contact has none)


class OpenInviteOut(BaseModel):
    id: uuid.UUID
    created_at: datetime
    expires_at: datetime  # the link itself cannot be recovered: only its hash is stored


class LimitsOut(BaseModel):
    max_open_invites: int
    invite_ttl_hours: int


class ConnectionsOut(BaseModel):
    scopes: list[ScopeOut]
    contacts: list[ContactOut]
    invites: list[OpenInviteOut]
    limits: LimitsOut


class InviteCreatedOut(BaseModel):
    link: str  # shown ONCE: the database keeps only a hash
    share_url: str  # t.me/share/url?... : opens Telegram's "send to" picker with the link as text
    expires_at: datetime
    connections: ConnectionsOut


class AuditEntryOut(BaseModel):
    id: int
    created_at: datetime
    requester: str | None  # the caller's alias for the asker; None when that person is no longer in the address book
    scope: str
    scope_title: str
    decision: str  # allowed | denied


class AuditOut(BaseModel):
    entries: list[AuditEntryOut]
    next_before: int | None  # pass as ?before= for the next (older) page; None at the end


# ------------------------------------------------------------------ reading


async def _snapshot(session: AsyncSession, settings: Settings) -> ConnectionsOut:
    contacts = (await session.execute(select(Contact))).scalars().all()
    grant_rows = (
        await session.execute(
            select(AccessGrant.grantee_user_id, AccessGrant.scope).where(
                AccessGrant.revoked_at.is_(None), or_(AccessGrant.expires_at.is_(None), AccessGrant.expires_at > func.now())
            )
        )
    ).all()
    granted: dict[uuid.UUID, set[str]] = {}
    for grantee, scope in grant_rows:
        granted.setdefault(grantee, set()).add(str(scope))
    open_invites = (
        await session.execute(
            select(ContactInvite.id, ContactInvite.created_at, ContactInvite.expires_at)
            .where(
                ContactInvite.used_at.is_(None), ContactInvite.revoked_at.is_(None), ContactInvite.expires_at > func.now()
            )
            .order_by(ContactInvite.created_at.desc())
        )
    ).all()

    def entry(contact: Contact) -> ContactOut:
        status = ContactStatus(contact.status)
        mine = granted.get(contact.contact_user_id, set()) if status is ContactStatus.accepted else set()
        return ContactOut(
            id=contact.id,
            alias=contact.alias,
            status=status.value,
            connected_since=contact.created_at,
            grants={scope: scope in mine for scope in SHARED_SCOPES},
        )

    ordered = sorted(
        contacts,
        key=lambda c: (ContactStatus(c.status) is ContactStatus.blocked, (c.alias or "").casefold(), str(c.id)),
    )
    return ConnectionsOut(
        scopes=[ScopeOut(key=key, title=title, description=description) for key, (title, description) in SHARED_SCOPES.items()],
        contacts=[entry(contact) for contact in ordered],
        invites=[OpenInviteOut(id=i, created_at=created, expires_at=expires) for i, created, expires in open_invites],
        limits=LimitsOut(max_open_invites=MAX_OPEN_INVITES, invite_ttl_hours=settings.invite_ttl_hours),
    )


@router.get("", response_model=ConnectionsOut)
async def get_connections(request: Request, ctx: Auth, db: DB) -> ConnectionsOut:
    async with db.tenant_session(ctx.tenant_id) as session:
        return await _snapshot(session, request.app.state.settings)


@router.get("/audit", response_model=AuditOut)
async def get_audit(
    ctx: Auth,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    before: Annotated[int | None, Query(ge=1)] = None,
) -> AuditOut:
    """Who asked for what about ME and what the answer was, newest first. The asker is named by the caller's own alias."""
    query = (
        select(
            DiplomatAuditLog.id,
            DiplomatAuditLog.created_at,
            DiplomatAuditLog.scope,
            DiplomatAuditLog.decision,
            Contact.alias,
        )
        .select_from(DiplomatAuditLog)
        .outerjoin(
            Contact,
            (Contact.user_id == DiplomatAuditLog.user_id) & (Contact.contact_user_id == DiplomatAuditLog.requester_user_id),
        )
        .where(DiplomatAuditLog.user_id == ctx.tenant_id)
        .order_by(DiplomatAuditLog.id.desc())
        .limit(limit + 1)  # one more than asked: tells whether there is an older page
    )
    if before is not None:
        query = query.where(DiplomatAuditLog.id < before)
    async with db.tenant_session(ctx.tenant_id) as session:
        rows = (await session.execute(query)).all()
    page = rows[:limit]
    return AuditOut(
        entries=[
            AuditEntryOut(
                id=row.id,
                created_at=row.created_at,
                requester=row.alias,
                scope=str(row.scope),
                scope_title=SCOPE_TITLES.get(str(row.scope), str(row.scope)),
                decision=row.decision,
            )
            for row in page
        ],
        next_before=page[-1].id if len(rows) > limit else None,
    )


# ------------------------------------------------------------------ invites


@router.post("/invites", response_model=InviteCreatedOut)
async def create_invite(request: Request, ctx: Auth, db: DB) -> InviteCreatedOut:
    """A new one-time link. The name the other person will be shown is taken from the Core Profile (like ``/connect`` in the group):
    the page cannot choose it. The link is in this answer and nowhere else."""
    settings: Settings = request.app.state.settings
    token = invites.new_token()
    async with db.tenant_session(ctx.tenant_id) as session:
        name = await invites.my_display_name(session)
        created = await invites.create_invite(
            session,
            token_hash=invites.hash_token(token),
            display_name=name,
            ttl=timedelta(hours=settings.invite_ttl_hours),
        )
        if created.outcome is CreateOutcome.too_many:
            raise ApiError(
                409, "too_many_invites", f"Открытых ссылок уже {MAX_OPEN_INVITES}. Отзовите ненужные и создайте новую."
            )
        if created.outcome is not CreateOutcome.created or created.expires_at is None:
            raise ApiError(
                409, "not_ready", "Сейчас ссылку создать нельзя: проверьте, что группа с ботом активна (в ней только вы и бот)."
            )
        snapshot = await _snapshot(session, settings)
    link = invites.deep_link(settings.telegram_bot_username, token)
    return InviteCreatedOut(
        link=link,
        share_url=f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(SHARE_TEXT, safe='')}",
        expires_at=created.expires_at,
        connections=snapshot,
    )


@router.delete("/invites", response_model=ConnectionsOut)
async def revoke_all_invites(request: Request, ctx: Auth, db: DB) -> ConnectionsOut:
    """Every open link stops working (idempotent: no open links is fine)."""
    async with db.tenant_session(ctx.tenant_id) as session:
        await invites.revoke_invites(session)
        return await _snapshot(session, request.app.state.settings)


@router.delete("/invites/{invite_id}", response_model=ConnectionsOut)
async def revoke_invite(invite_id: InviteId, request: Request, ctx: Auth, db: DB) -> ConnectionsOut:
    async with db.tenant_session(ctx.tenant_id) as session:
        if not await invites.revoke_invite(session, invite_id):
            raise ApiError(404, "not_found", "Эта ссылка уже недействительна.")
        return await _snapshot(session, request.app.state.settings)


# ------------------------------------------------------------------ contacts


async def _outcome(session: AsyncSession, sql: str, **params: object) -> str:
    return (await session.execute(text(sql), params)).scalar_one()


@router.put("/contacts/{contact_id}/alias", response_model=ConnectionsOut)
async def rename_contact(contact_id: ContactId, body: AliasIn, request: Request, ctx: Auth, db: DB) -> ConnectionsOut:
    """How YOU call this person. It is only ever shown to you."""
    async with db.tenant_session(ctx.tenant_id) as session:
        if await _outcome(session, "SELECT civ.rename_contact(:c, :a)", c=contact_id, a=body.alias) != "ok":
            raise ApiError(404, "not_found", NO_SUCH_CONTACT)
        return await _snapshot(session, request.app.state.settings)


@router.post("/contacts/{contact_id}/block", response_model=ConnectionsOut)
async def block_contact(contact_id: ContactId, request: Request, ctx: Auth, db: DB) -> ConnectionsOut:
    """Silent for the other person; every permission you gave them is withdrawn at once."""
    async with db.tenant_session(ctx.tenant_id) as session:
        if await _outcome(session, "SELECT civ.block_contact(:c)", c=contact_id) != "ok":
            raise ApiError(404, "not_found", NO_SUCH_CONTACT)
        return await _snapshot(session, request.app.state.settings)


@router.post("/contacts/{contact_id}/unblock", response_model=ConnectionsOut)
async def unblock_contact(contact_id: ContactId, request: Request, ctx: Auth, db: DB) -> ConnectionsOut:
    """Back among the contacts, with NO permissions: they have to be given again."""
    async with db.tenant_session(ctx.tenant_id) as session:
        if await _outcome(session, "SELECT civ.unblock_contact(:c)", c=contact_id) != "ok":
            raise ApiError(404, "not_found", NO_SUCH_CONTACT)
        return await _snapshot(session, request.app.state.settings)


@router.delete("/contacts/{contact_id}", response_model=ConnectionsOut)
async def remove_contact(contact_id: ContactId, request: Request, ctx: Auth, db: DB) -> ConnectionsOut:
    """Ends the connection on both sides (permissions both ways go with it). A new invite can restore it. Not for blocked contacts."""
    async with db.tenant_session(ctx.tenant_id) as session:
        outcome = await _outcome(session, "SELECT civ.remove_contact(:c)", c=contact_id)
        if outcome == "not_found":
            raise ApiError(404, "not_found", NO_SUCH_CONTACT)
        if outcome == "blocked":
            raise ApiError(
                409, "contact_blocked", "Заблокированного контакта удалить нельзя: сначала разблокируйте его, если связь больше не нужна."
            )
        return await _snapshot(session, request.app.state.settings)


@router.put("/contacts/{contact_id}/grants/{scope}", response_model=ConnectionsOut)
async def set_grant(
    contact_id: ContactId, scope: Annotated[str, Path(max_length=40)], body: GrantIn, request: Request, ctx: Auth, db: DB
) -> ConnectionsOut:
    """Allow or stop allowing this contact to ask for one kind of information. Idempotent; only the scopes the screen offers."""
    if scope not in SHARED_SCOPES:
        raise ApiError(404, "not_found", "Такого разрешения нет.")
    async with db.tenant_session(ctx.tenant_id) as session:
        outcome = await _outcome(
            session, "SELECT civ.set_grant(:c, :s, :e)", c=contact_id, s=scope, e=body.enabled
        )
        if outcome == "not_found":
            raise ApiError(404, "not_found", NO_SUCH_CONTACT)
        if outcome == "not_accepted":
            raise ApiError(409, "not_accepted", "Разрешения можно давать только подтверждённым контактам.")
        return await _snapshot(session, request.app.state.settings)

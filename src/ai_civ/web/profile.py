"""Screen "Мозг": what the avatar knows about the user (Core facts that are not principles) and the time zone.

Reading: current versions only (``valid_to IS NULL``). Writing: a new version of an EXISTING fact (see ``facts``) or a deliberate
removal. Creating facts is not a Mini App feature: they come from the interview, the quarantine review and the bot.
The time zone is a column of ``users`` and the one column of it this API may touch.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, update

from ai_civ.db.enums import FactCategory
from ai_civ.db.models import CoreFact, EpisodicMemory, User
from ai_civ.web import facts
from ai_civ.web.deps import DB, Auth, ApiError

router = APIRouter(prefix="/profile", tags=["profile"])

KEY_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$"  # the same shape as the database CHECK
MAX_TEXT = 1000
MAX_NUM = Decimal("1e12")

FactKey = Annotated[str, Path(max_length=100, pattern=KEY_PATTERN)]


class ProfileOut(BaseModel):
    timezone: str
    facts: list[facts.FactOut]  # flat, with ``group`` and ``title`` filled in
    groups: list[facts.GroupOut]  # the same facts by human section: what the screen "Мозг" renders


class FactsOut(BaseModel):
    groups: list[facts.GroupOut]


class MemoriesOut(BaseModel):
    items: list[facts.MemoryOut]
    limit: int
    offset: int
    has_more: bool  # another page follows (the page asks for ``offset + limit``)


class FactEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_fact_id: uuid.UUID  # the version the page showed; anything else -> 409
    value_text: str | None = Field(default=None, max_length=MAX_TEXT)
    value_num: float | None = None
    value_unit: str | None = Field(default=None, max_length=20)

    @field_validator("value_text", "value_unit")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _something_changes(self) -> FactEdit:
        if self.value_text is None and self.value_num is None:
            raise ValueError("send value_text or value_num")
        return self


class TimezoneIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(max_length=64)


def valid_iana_zone(value: str) -> str:
    """A real region zone ("Europe/Copenhagen") or UTC; offsets and abbreviations ("EST", "+03:00") are not places."""
    zone = value.strip()
    if zone != "UTC" and "/" not in zone:
        raise ValueError("expected an IANA zone like Europe/Copenhagen")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"unknown time zone {zone!r}") from None
    return zone


@router.get("", response_model=ProfileOut)
async def get_profile(ctx: Auth, db: DB) -> ProfileOut:
    async with db.tenant_session(ctx.tenant_id) as session:
        rows = await _current_facts(session)
        timezone = (await session.execute(select(User.timezone).where(User.id == ctx.tenant_id))).scalar_one()
    shown = [f for f in rows if facts.has_content(f)]
    return ProfileOut(timezone=timezone, facts=[facts.fact_out(f) for f in shown], groups=facts.grouped(rows))


async def _current_facts(session) -> list[CoreFact]:
    return list(
        (
            await session.execute(
                select(CoreFact)
                .where(CoreFact.valid_to.is_(None), CoreFact.category != FactCategory.principle)
                .order_by(CoreFact.category, CoreFact.priority.desc(), CoreFact.key)
            )
        ).scalars().all()
    )


@router.get("/facts", response_model=FactsOut)
async def get_facts(ctx: Auth, db: DB) -> FactsOut:
    """What the avatar remembers, by human section ("Проекты", "Цели", "Привычки", "Здоровье"...). The page renders ``title`` (only the few system facts have one) and the
    text of the fact; the machine key (``work.trading_bot``) is only the handle for editing and is not meant to be shown. Blank facts are left out."""
    async with db.tenant_session(ctx.tenant_id) as session:
        rows = await _current_facts(session)
    return FactsOut(groups=facts.grouped(rows))


MEMORIES_PAGE = 20
MEMORIES_MAX_PAGE = 100


@router.get("/memories", response_model=MemoriesOut)
async def get_memories(
    ctx: Auth,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=MEMORIES_MAX_PAGE)] = MEMORIES_PAGE,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> MemoriesOut:
    """The indexed history (``episodic_memories``), newest first, so the user can see what the avatar has stored. Read-only; RLS limits the rows to this tenant.
    Only the columns the page shows are read: the 1536-number embedding never leaves the database."""
    async with db.tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.execute(
                select(EpisodicMemory.id, EpisodicMemory.created_at, EpisodicMemory.content, EpisodicMemory.importance)
                .order_by(EpisodicMemory.created_at.desc(), EpisodicMemory.id.desc())
                .limit(limit + 1)  # one extra row tells whether there is a next page
                .offset(offset)
            )
        ).all()
    page = rows[:limit]
    return MemoriesOut(
        items=[
            facts.MemoryOut(id=r.id, created_at=r.created_at.isoformat(), snippet=facts.snippet(r.content), importance=r.importance) for r in page
        ],
        limit=limit,
        offset=offset,
        has_more=len(rows) > limit,
    )


def _not_a_principle(key: str) -> None:
    if key.startswith("principle."):
        raise ApiError(400, "wrong_screen", "Принципы редактируются на вкладке «Конституция».")


@router.put("/facts/{key}", response_model=facts.FactOut)
async def edit_fact(key: FactKey, body: FactEdit, ctx: Auth, db: DB) -> facts.FactOut:
    _not_a_principle(key)
    try:
        num = None if body.value_num is None else Decimal(str(body.value_num))
        if num is not None and abs(num) >= MAX_NUM:
            raise InvalidOperation
    except InvalidOperation:
        raise ApiError(422, "invalid_request", "Число вне допустимого диапазона.") from None
    async with db.tenant_session(ctx.tenant_id) as session:
        _, new = await facts.replace_version(
            session, key, body.expected_fact_id, facts.Changes(value_text=body.value_text, value_num=num, value_unit=body.value_unit)
        )
        return facts.fact_out(new)


@router.delete("/facts/{key}", status_code=204)
async def delete_fact(
    key: FactKey, ctx: Auth, db: DB, expected_fact_id: Annotated[uuid.UUID, Query()]
) -> None:
    """Forgetting a fact on purpose: the version is closed, nothing is erased from the history."""
    _not_a_principle(key)
    async with db.tenant_session(ctx.tenant_id) as session:
        await facts.close_version(session, key, expected_fact_id)


@router.put("/timezone", response_model=TimezoneIn)
async def set_timezone(body: TimezoneIn, ctx: Auth, db: DB) -> TimezoneIn:
    """Only ``users.timezone``, and only for the caller's own row. Reminders and quiet hours are read in this zone."""
    try:
        zone = valid_iana_zone(body.timezone)
    except ValueError as exc:
        raise ApiError(422, "invalid_timezone", f"Не похоже на часовой пояс: {exc}") from None
    async with db.tenant_session(ctx.tenant_id) as session:
        await session.execute(update(User).where(User.id == ctx.tenant_id).values(timezone=zone))
    return TimezoneIn(timezone=zone)

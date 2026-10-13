"""Screen "Таверна": the characters of the user's friends and friendly cheers.

Everything about OTHER tenants comes from three SECURITY DEFINER functions (migration 0017) that check the pair under ``civ.lock_pair``,
require both rows of the pair to be ``accepted``, hand out a field only if its owner switched the matching flag on and audit every read.
This module never queries another tenant's tables. A friend that does not qualify is simply not in the feed; an unknown handle, a stale
handle and a refused one answer identically (404 ``not_found``), so the page cannot be used to probe who is connected to whom.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Path
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from ai_civ.web.deps import DB, Auth, ApiError

router = APIRouter(prefix="/tavern", tags=["tavern"])

Kind = Literal["focus", "intellect", "body", "connection"]
UNKNOWN_FRIEND = "Знакомый"


class FriendCard(BaseModel):
    contact_id: uuid.UUID  # the user's OWN address-book row: a handle, never another tenant's id
    name: str  # the friend's character name, else the alias the user gave them
    avatar_url: str | None
    level: int | None  # only when the friend shows it
    total_xp: int | None
    mood: str | None  # a word of the closed vocabulary, only from a state the friend confirmed, only when they share it
    interests: list[str] | None  # only when the friend shares them
    open_for_intros: bool
    connected_since: datetime
    you_cheered_today: bool
    cheered_you_today: bool


class FeedOut(BaseModel):
    friends: list[FriendCard]


class CheerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact_id: uuid.UUID
    kind: Kind = "focus"


class CheerOut(BaseModel):
    outcome: Literal["sent"]
    kind: Kind
    xp: Literal["awarded", "daily_cap", "duplicate"]  # the sender's own result only


def _card(raw: dict) -> FriendCard:
    contact_id = raw["contact_id"]
    avatar = f"/api/tavern/avatar/{contact_id}?v={raw['avatar_hash']}" if raw.get("avatar_hash") else None
    return FriendCard(
        contact_id=contact_id,
        name=raw.get("character_name") or raw.get("alias") or UNKNOWN_FRIEND,
        avatar_url=avatar,
        level=raw.get("level"),
        total_xp=raw.get("total_xp"),
        mood=raw.get("mood"),
        interests=raw.get("interests"),
        open_for_intros=bool(raw.get("open_for_intros")),
        connected_since=raw["connected_since"],
        you_cheered_today=bool(raw.get("you_cheered_today")),
        cheered_you_today=bool(raw.get("cheered_you_today")),
    )


@router.get("/feed", response_model=FeedOut)
async def feed(ctx: Auth, db: DB) -> FeedOut:
    async with db.tenant_session(ctx.tenant_id) as session:
        raw = (await session.execute(text("SELECT civ.tavern_feed()"))).scalar_one()
    cards = [_card(item) for item in raw]
    cards.sort(key=lambda c: (c.connected_since, str(c.contact_id)))
    return FeedOut(friends=cards)


@router.get("/avatar/{contact_id}")
async def avatar(contact_id: uuid.UUID, ctx: Auth, db: DB) -> Response:
    async with db.tenant_session(ctx.tenant_id) as session:
        data = (await session.execute(text("SELECT civ.tavern_avatar(:c)"), {"c": contact_id})).scalar_one()
    if data is None:  # no such friend / not mutual / not allowed / no avatar: one answer
        raise ApiError(404, "not_found", "Не найдено.")
    return Response(content=bytes(data), media_type="image/webp")


@router.post("/cheer", response_model=CheerOut)
async def cheer(body: CheerIn, ctx: Auth, db: DB) -> CheerOut:
    async with db.tenant_session(ctx.tenant_id) as session:
        result = (
            await session.execute(text("SELECT civ.send_cheer(:c, :k)"), {"c": body.contact_id, "k": body.kind})
        ).scalar_one()
    outcome = result["outcome"]
    if outcome == "unavailable":
        raise ApiError(404, "not_found", "Не найдено.")
    if outcome == "already_today":
        raise ApiError(409, "already_cheered_today", "Сегодня вы уже поддержали этого человека.")
    return CheerOut(outcome="sent", kind=body.kind, xp=result["xp"])

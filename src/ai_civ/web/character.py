"""Screen "Персонаж": the RPG profile of the user (name, avatar, level, XP per branch) and what the future Tavern may show of it.

Reading: ``civ.character_progress`` is the verdict (the level is computed by the database, 100 XP = one level). Writing: only the profile row
(name, four visibility flags, avatar). XP is never written here: it is earned by the bot's services (``bot/xp.py``) and nothing in this API can award it.

The avatar arrives as the raw request body (``Content-Type: image/*``, at most 256 KiB), is decoded by Pillow, stripped of every piece of
metadata, scaled down to fit 256x256 and stored re-encoded as WebP: whatever the client sent, only pixels we produced are kept.
"""

from __future__ import annotations

import asyncio
import io
import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ.bot import invites
from ai_civ.db.models import CharacterProfile
from ai_civ.web.deps import DB, Auth, ApiError

log = logging.getLogger(__name__)

router = APIRouter(prefix="/character", tags=["character"])

LEVEL_XP = 100  # mirrors civ.character_progress (the database computes the level; this is only for "XP to the next level")
DAILY_CAP = 100  # mirrors civ.xp_ledger_daily_cap (a test keeps the two equal)
AVATAR_MAX_BYTES = 256 * 1024
AVATAR_SIDE = 256
AVATAR_MAX_PIXELS = 40_000_000  # checked from the header, BEFORE the image is decoded (decompression bombs)
AVATAR_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}
AVATAR_QUALITY = 85
CATEGORIES = ("focus", "intellect", "body", "connection")


class VisibilityOut(BaseModel):
    show_level: bool
    show_mood: bool
    show_interests: bool
    open_for_intros: bool


class PenaltyItem(BaseModel):
    amount: int  # negative: what the night took away
    category: str
    kind: str  # "body" (no workouts for too long) | "task" (a task overdue by too long) | "other"
    created_at: datetime


class PenaltiesOut(BaseModel):
    """The decay system as the screen shows it (migration 0023, ``bot/decay.py``): the streak and what neglect cost lately."""

    streak_days: int  # consecutive local days with at least one award, ending today (or yesterday while today has none yet)
    penalty_today: int  # XP taken away today / in the last 7 days / ever (positive numbers)
    penalty_week: int
    penalty_total: int
    recent: list[PenaltyItem]  # the latest penalties that really took XP (zero markers are not shown), newest first
    body_idle_days: int | None  # whole days since the last logged workout; None = no workout was ever logged
    body_threshold_days: int  # the decay starts when the break is LONGER than this
    body_decay_active: bool  # tonight's run will take body XP (idle longer than the threshold and the branch has something to lose)


class CharacterOut(BaseModel):
    character_name: str  # the chosen name, else the Telegram first name
    name_is_default: bool
    has_avatar: bool
    level: int
    total_xp: int
    current_level_xp: int  # progress inside the level, 0..99 (also the percentage)
    xp_to_next_level: int
    xp_by_category: dict[str, int]
    xp_today: int
    daily_cap: int
    visibility: VisibilityOut
    penalties: PenaltiesOut


class SettingsEdit(BaseModel):
    """Only the fields that were sent change. ``character_name: null`` returns to the Telegram first name."""

    model_config = ConfigDict(extra="forbid")

    character_name: str | None = None
    show_level: bool | None = None
    show_mood: bool | None = None
    show_interests: bool | None = None
    open_for_intros: bool | None = None

    @field_validator("character_name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else invites.check_name(value)

    @model_validator(mode="after")
    def _something_changes(self) -> SettingsEdit:
        if not self.model_fields_set:
            raise ValueError("send at least one field")
        for flag in ("show_level", "show_mood", "show_interests", "open_for_intros"):
            if flag in self.model_fields_set and getattr(self, flag) is None:
                raise ValueError(f"{flag} must be true or false")
        return self


SQL_STREAK = "SELECT civ.xp_streak_days(:u)"
SQL_PENALTY_SUMS = """
    SELECT COALESCE(-sum(amount) FILTER (WHERE (created_at AT TIME ZONE :tz)::date = (now() AT TIME ZONE :tz)::date), 0)::int,
           COALESCE(-sum(amount) FILTER (WHERE created_at > now() - interval '7 days'), 0)::int,
           COALESCE(-sum(amount), 0)::int
    FROM civ.xp_ledger WHERE amount < 0
"""
SQL_PENALTY_RECENT = "SELECT amount, category, source_ref, created_at FROM civ.xp_ledger WHERE amount < 0 ORDER BY created_at DESC, source_ref LIMIT 5"
SQL_IDLE = "SELECT floor(extract(epoch FROM now() - max(started_at)) / 86400)::int, (now() - max(started_at)) > make_interval(days => :d) FROM civ.workout_sessions"


def _penalty_kind(source_ref: str) -> str:
    return "body" if source_ref.startswith("decay:body:") else "task" if source_ref.startswith("penalty:task:") else "other"


async def _snapshot(db: Any, ctx: Any, settings: Any = None) -> CharacterOut:
    threshold = settings.xp_decay_body_idle_days if settings is not None else 5
    async with db.tenant_session(ctx.tenant_id) as session:
        streak = (await session.execute(text(SQL_STREAK), {"u": ctx.tenant_id})).scalar_one()
        today_lost, week_lost, total_lost = (await session.execute(text(SQL_PENALTY_SUMS), {"tz": ctx.timezone})).one()
        recent = (await session.execute(text(SQL_PENALTY_RECENT))).all()
        idle_days, idle_long = (await session.execute(text(SQL_IDLE), {"d": threshold})).one()
        progress = (
            await session.execute(
                text("SELECT total_xp, level, current_level_xp, xp_by_category, xp_today FROM civ.character_progress WHERE user_id = :u"),
                {"u": ctx.tenant_id},
            )
        ).one()
        profile = (
            await session.execute(
                select(
                    CharacterProfile.character_name,
                    CharacterProfile.show_level,
                    CharacterProfile.show_mood,
                    CharacterProfile.show_interests,
                    CharacterProfile.open_for_intros,
                    (CharacterProfile.avatar_bytes.is_not(None)).label("has_avatar"),
                ).where(CharacterProfile.user_id == ctx.tenant_id)
            )
        ).one_or_none()
    chosen = profile.character_name if profile is not None else None
    by_category = {key: int(progress.xp_by_category.get(key, 0)) for key in CATEGORIES}
    penalties = PenaltiesOut(
        streak_days=streak,
        penalty_today=today_lost,
        penalty_week=week_lost,
        penalty_total=total_lost,
        recent=[PenaltyItem(amount=r.amount, category=r.category, kind=_penalty_kind(r.source_ref), created_at=r.created_at) for r in recent if r.amount < 0],
        body_idle_days=idle_days,
        body_threshold_days=threshold,
        body_decay_active=bool(idle_long) and by_category["body"] > 0,
    )
    return CharacterOut(
        character_name=chosen or ctx.init.first_name,
        name_is_default=chosen is None,
        has_avatar=bool(profile.has_avatar) if profile is not None else False,
        level=progress.level,
        total_xp=progress.total_xp,
        current_level_xp=progress.current_level_xp,
        xp_to_next_level=LEVEL_XP - progress.current_level_xp,
        xp_by_category=by_category,
        xp_today=progress.xp_today,
        daily_cap=DAILY_CAP,
        visibility=VisibilityOut(
            show_level=profile.show_level if profile is not None else True,
            show_mood=profile.show_mood if profile is not None else False,
            show_interests=profile.show_interests if profile is not None else False,
            open_for_intros=profile.open_for_intros if profile is not None else False,
        ),
        penalties=penalties,
    )


@router.get("/me", response_model=CharacterOut)
async def character_me(request: Request, ctx: Auth, db: DB) -> CharacterOut:
    return await _snapshot(db, ctx, request.app.state.settings)


@router.patch("/settings", response_model=CharacterOut)
async def update_settings(request: Request, body: SettingsEdit, ctx: Auth, db: DB) -> CharacterOut:
    values = {name: getattr(body, name) for name in body.model_fields_set}
    async with db.tenant_session(ctx.tenant_id) as session:
        stmt = pg_insert(CharacterProfile).values(user_id=ctx.tenant_id, **values)
        await session.execute(stmt.on_conflict_do_update(index_elements=[CharacterProfile.user_id], set_={**values, "updated_at": text("now()")}))
    return await _snapshot(db, ctx, request.app.state.settings)


# ---------------------------------------------------------------- avatar


def reencode_avatar(raw: bytes) -> bytes:
    """Any accepted picture -> a small WebP made from its pixels only. Raises ``ValueError`` (a message for the user) for anything else."""
    try:
        image = Image.open(io.BytesIO(raw))
        if image.format not in AVATAR_FORMATS:
            raise ValueError("Поддерживаются JPEG, PNG, WebP и GIF.")
        width, height = image.size
        if width < 1 or height < 1 or width * height > AVATAR_MAX_PIXELS:
            raise ValueError("Слишком большое изображение.")
        image.seek(0)  # an animation: the first frame only
        image = ImageOps.exif_transpose(image)  # honour the camera's rotation, then the metadata is dropped with the old object
        image = image.convert("RGBA" if "A" in image.getbands() or image.mode == "P" else "RGB")
        image.thumbnail((AVATAR_SIDE, AVATAR_SIDE), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="WEBP", quality=AVATAR_QUALITY, method=4)  # no exif / icc / xmp is passed on
        return out.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, SyntaxError, EOFError):
        raise ValueError("Не удалось прочитать картинку.") from None


async def _read_body(request: Request, limit: int) -> bytes:
    """The body, refused as soon as it is longer than ``limit`` (never buffers more than that)."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise ApiError(413, "avatar_too_large", "Аватар больше 256 КБ.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise ApiError(413, "avatar_too_large", "Аватар больше 256 КБ.")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/avatar", response_model=CharacterOut)
async def upload_avatar(request: Request, ctx: Auth, db: DB) -> CharacterOut:
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if not content_type.startswith("image/"):
        raise ApiError(415, "avatar_not_image", "Пришлите картинку (JPEG, PNG, WebP или GIF).")
    raw = await _read_body(request, AVATAR_MAX_BYTES)
    if not raw:
        raise ApiError(422, "avatar_empty", "Файл пустой.")
    try:
        webp = await asyncio.to_thread(reencode_avatar, raw)
    except ValueError as exc:
        raise ApiError(422, "avatar_invalid", str(exc)) from None
    async with db.tenant_session(ctx.tenant_id) as session:
        stmt = pg_insert(CharacterProfile).values(user_id=ctx.tenant_id, avatar_bytes=webp)
        await session.execute(stmt.on_conflict_do_update(index_elements=[CharacterProfile.user_id], set_={"avatar_bytes": webp, "updated_at": text("now()")}))
    return await _snapshot(db, ctx, request.app.state.settings)


@router.get("/avatar")
async def get_avatar(ctx: Auth, db: DB) -> Response:
    async with db.tenant_session(ctx.tenant_id) as session:
        data = (await session.execute(select(CharacterProfile.avatar_bytes).where(CharacterProfile.user_id == ctx.tenant_id))).scalar_one_or_none()
    if data is None:
        raise ApiError(404, "no_avatar", "Аватара нет.")
    return Response(content=bytes(data), media_type="image/webp")

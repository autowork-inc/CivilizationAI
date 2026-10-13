"""Screen "Напоминания": when the bot may write first (quiet hours) and at what local time each proactive trigger fires.

Data: ``civ.proactive_schedule`` (one row per trigger: local time + on/off), ``users.quiet_hours_start/end`` (the evening "отбой" and the START OF THE DAY: the
first moment the bot may write) and ``users.checkin_per_day`` (how many check-ins a day: null = default, 0 = none). The Beat dispatchers (``bot.schedule.claim_due``,
``claim_checkin``) read exactly these columns, so a change saved here applies at their next tick (every 5 minutes).

The invariant the interview already enforces is kept here in both directions: an ENABLED reminder never falls inside the quiet hours.
  * Moving a reminder INTO the quiet hours is not an error: in the same transaction the quiet window is shrunk just enough to let the reminder
    out (``shrink_quiet_hours``: the end moves to 15 minutes before the reminder, or - when that would eat most of the night - the start moves to
    15 minutes after it), and the answer says so (``quiet_hours_adjusted``). Shrinking can never put another enabled reminder into the window.
  * Widening the quiet hours over an enabled reminder is still refused with an explanation (``reminder_in_quiet_hours``): there the user asked
    for the window, not for the reminder to move, and nothing may be changed silently.
Both writes lock the user's row first, so two parallel requests cannot jointly break the rule.

Times are wall-clock times in the profile's time zone (``users.timezone``). One nudge per trigger per day: changing the time of a
reminder that has already come today does not make it come twice (``last_fired_on`` is never touched here).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo
from typing import Annotated

from fastapi import APIRouter, Path, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_civ.bot.proactive import TRIGGERS, in_quiet_hours
from ai_civ.bot.schedule import todays_checkins
from ai_civ.db.models import ProactiveSchedule, User
from ai_civ.web.deps import DB, ApiError, Auth

router = APIRouter(prefix="/reminders", tags=["reminders"])

SCHEDULED = {key: trigger for key, trigger in TRIGGERS.items() if trigger.scheduled}  # triggers with a time of their own (not ``check_in``)

QUIET_GAP_MINUTES = 15  # how far from the reminder the quiet window is pulled back
DAY = 24 * 60

HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?::00)?$")

TriggerKey = Annotated[str, Path(max_length=40, pattern=r"^[a-z][a-z0-9_]{1,39}$")]


def parse_time(value: str) -> time:
    match = HHMM.match(value.strip())
    if match is None:
        raise ValueError("время нужно в формате ЧЧ:ММ, например 13:30")
    return time(int(match.group(1)), int(match.group(2)))


def fmt(value: time) -> str:
    return value.strftime("%H:%M")


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def _from_minutes(value: int) -> time:
    value %= DAY
    return time(value // 60, value % 60)


def shrink_quiet_hours(moment: time, start: time, end: time, gap: int = QUIET_GAP_MINUTES) -> tuple[time, time] | None:
    """The smallest change to the quiet window [start, end) that lets ``moment`` (inside it) out, keeping ``gap`` minutes of distance, or None when
    the window is too small for that. Two candidates: pull the END back to ``moment - gap`` (the case of a reminder in the morning part of the
    night) or push the START forward to ``moment + gap`` (a reminder in the evening part); the one that keeps more quiet time wins, a tie goes to
    the end. The window never grows and never becomes empty, so no other reminder can fall into it."""
    s, e, t = _minutes(start), _minutes(end), _minutes(moment)
    length = (e - s) % DAY
    before = (t - s) % DAY  # quiet time before the moment
    after = length - before  # quiet time from the moment on
    keep_end = before - gap  # length of the window if the end is pulled back
    keep_start = after - gap  # length if the start is pushed forward
    if keep_end <= 0 and keep_start <= 0:
        return None
    if keep_end >= keep_start:
        return start, _from_minutes(t - gap)
    return _from_minutes(t + gap), end


class TimeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @staticmethod
    def check(value: str) -> str:
        return fmt(parse_time(value))


class QuietHoursIn(TimeIn):
    start: str  # no max_length: the pattern in ``parse_time`` bounds it, and its message is the Russian one the user sees
    end: str

    @field_validator("start", "end")
    @classmethod
    def _time(cls, value: str) -> str:
        return cls.check(value)


class ReminderIn(TimeIn):
    local_time: str
    enabled: bool

    @field_validator("local_time")
    @classmethod
    def _time(cls, value: str) -> str:
        return cls.check(value)


class CheckinsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per_day: int | None = Field(default=None, ge=0, le=4)  # None = the system default, 0 = none, 1-4 = that many a day


class QuietHoursOut(BaseModel):
    start: str  # the evening: from here the bot does not write first
    end: str  # the morning: the START OF THE DAY (the first moment the bot may write)


class CheckinsOut(BaseModel):
    per_day: int | None  # the user's choice (None = the default of the system)
    today: list[str]  # the check-in times of TODAY (local, HH:MM), computed exactly as the dispatcher does; empty = none today


class ReminderOut(BaseModel):
    trigger_key: str
    title: str
    description: str
    local_time: str  # the saved time, or the trigger's default when it was never configured
    enabled: bool
    configured: bool  # a row exists (False: shown with the default time, switched off)
    last_fired_on: date | None  # tenant-local date of the last nudge: it came today if this is today


class QuietHoursAdjustedOut(BaseModel):
    """Present when saving a reminder moved the quiet hours: what they were and what they are now."""

    previous_start: str
    previous_end: str
    start: str
    end: str


class RemindersOut(BaseModel):
    quiet_hours_adjusted: QuietHoursAdjustedOut | None = None
    timezone: str
    grace_minutes: int  # a nudge is still sent if its time passed less than this long ago
    quiet_hours: QuietHoursOut
    checkins: CheckinsOut
    reminders: list[ReminderOut]


def _out(key: str, row: ProactiveSchedule | None) -> ReminderOut:
    trigger = TRIGGERS[key]
    return ReminderOut(
        trigger_key=key,
        title=trigger.title or key,
        description=trigger.description,
        local_time=fmt(row.local_time if row is not None else trigger.default_time),
        enabled=bool(row is not None and row.enabled),
        configured=row is not None,
        last_fired_on=None if row is None else row.last_fired_on,
    )


async def _lock_user(session: AsyncSession, tenant_id) -> User:
    """The user's row, locked until the end of the transaction: serialises every write of this screen for one tenant."""
    return (await session.execute(select(User).where(User.id == tenant_id).with_for_update())).scalar_one()


async def _rows(session: AsyncSession) -> dict[str, ProactiveSchedule]:
    rows = (await session.execute(select(ProactiveSchedule))).scalars().all()
    return {row.trigger_key: row for row in rows if row.trigger_key in SCHEDULED}  # rows of retired triggers stay invisible


def _snapshot(user: User, rows: dict[str, ProactiveSchedule], settings, adjusted: QuietHoursAdjustedOut | None = None) -> RemindersOut:
    local_date = datetime.now(UTC).astimezone(ZoneInfo(user.timezone)).date()
    today = todays_checkins(settings, user.id, local_date, user.quiet_hours_start, user.quiet_hours_end, user.checkin_per_day)
    return RemindersOut(
        quiet_hours_adjusted=adjusted,
        timezone=user.timezone,
        grace_minutes=settings.proactive_grace_minutes,
        quiet_hours=QuietHoursOut(start=fmt(user.quiet_hours_start), end=fmt(user.quiet_hours_end)),
        checkins=CheckinsOut(per_day=user.checkin_per_day, today=[fmt(t) for t in today]),
        reminders=[_out(key, rows.get(key)) for key in SCHEDULED],
    )


@router.get("", response_model=RemindersOut)
async def get_reminders(request: Request, ctx: Auth, db: DB) -> RemindersOut:
    async with db.tenant_session(ctx.tenant_id) as session:
        user = (await session.execute(select(User).where(User.id == ctx.tenant_id))).scalar_one()
        rows = await _rows(session)
    return _snapshot(user, rows, request.app.state.settings)


@router.put("/quiet-hours", response_model=RemindersOut)
async def set_quiet_hours(body: QuietHoursIn, request: Request, ctx: Auth, db: DB) -> RemindersOut:
    """The window in which the bot never writes first; it may wrap around midnight (22:00-08:00)."""
    start, end = parse_time(body.start), parse_time(body.end)
    if start == end:
        raise ApiError(422, "invalid_request", "Начало и конец тихих часов не могут совпадать.")
    async with db.tenant_session(ctx.tenant_id) as session:
        user = await _lock_user(session, ctx.tenant_id)
        rows = await _rows(session)
        clash = [
            f"«{TRIGGERS[key].title or key}» в {fmt(row.local_time)}"
            for key, row in rows.items()
            if row.enabled and in_quiet_hours(row.local_time, start, end)
        ]
        if clash:
            raise ApiError(
                422,
                "reminder_in_quiet_hours",
                f"В эти тихие часы попадает: {', '.join(clash)}. Сначала поменяйте время напоминания или выключите его.",
                details={"conflicts": [key for key, row in rows.items() if row.enabled and in_quiet_hours(row.local_time, start, end)]},
            )
        await session.execute(update(User).where(User.id == ctx.tenant_id).values(quiet_hours_start=start, quiet_hours_end=end))
        await session.refresh(user)
        return _snapshot(user, rows, request.app.state.settings)


@router.put("/checkins", response_model=RemindersOut)
async def set_checkins(body: CheckinsIn, request: Request, ctx: Auth, db: DB) -> RemindersOut:
    """How many times a day the bot may start a conversation (a check-in): ``null`` = the default of the system, 0 = never, 1-4 = that many. The times themselves stay the
    system's (random, inside the active hours, fixed per day); the answer lists today's. Applies at the next Beat tick (the dispatcher reads this column)."""
    async with db.tenant_session(ctx.tenant_id) as session:
        user = await _lock_user(session, ctx.tenant_id)
        await session.execute(update(User).where(User.id == ctx.tenant_id).values(checkin_per_day=body.per_day))
        await session.refresh(user)
        rows = await _rows(session)
        return _snapshot(user, rows, request.app.state.settings)


@router.put("/{trigger_key}", response_model=RemindersOut)
async def set_reminder(trigger_key: TriggerKey, body: ReminderIn, request: Request, ctx: Auth, db: DB) -> RemindersOut:
    """Time and on/off of one trigger. Only triggers the code knows can be scheduled (``bot.proactive.TRIGGERS``)."""
    if trigger_key not in SCHEDULED:
        raise ApiError(404, "not_found", "Такого напоминания нет.")
    local_time = parse_time(body.local_time)
    async with db.tenant_session(ctx.tenant_id) as session:
        user = await _lock_user(session, ctx.tenant_id)
        adjusted: QuietHoursAdjustedOut | None = None
        if body.enabled and in_quiet_hours(local_time, user.quiet_hours_start, user.quiet_hours_end):
            shrunk = shrink_quiet_hours(local_time, user.quiet_hours_start, user.quiet_hours_end)
            if shrunk is None:
                raise ApiError(
                    422,
                    "in_quiet_hours",
                    f"{fmt(local_time)} попадает в тихие часы ({fmt(user.quiet_hours_start)}–{fmt(user.quiet_hours_end)}), а они слишком короткие, "
                    "чтобы подвинуть их автоматически. Выберите другое время или измените тихие часы.",
                )
            new_start, new_end = shrunk
            adjusted = QuietHoursAdjustedOut(
                previous_start=fmt(user.quiet_hours_start),
                previous_end=fmt(user.quiet_hours_end),
                start=fmt(new_start),
                end=fmt(new_end),
            )
            await session.execute(update(User).where(User.id == ctx.tenant_id).values(quiet_hours_start=new_start, quiet_hours_end=new_end))
            await session.refresh(user)
        await session.execute(
            pg_insert(ProactiveSchedule)
            .values(user_id=ctx.tenant_id, trigger_key=trigger_key, local_time=local_time, enabled=body.enabled)
            .on_conflict_do_update(
                constraint="uq_proactive_schedule_user_trigger",
                set_={"local_time": local_time, "enabled": body.enabled},  # last_fired_on stays: one nudge per day
            )
        )
        rows = await _rows(session)
        return _snapshot(user, rows, request.app.state.settings, adjusted)

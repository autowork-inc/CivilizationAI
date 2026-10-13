"""Which proactive nudges are due for a tenant right now.

Per-user times live in ``civ.proactive_schedule`` (filled by the onboarding interview): trigger key + LOCAL time. Beat calls
the dispatcher every few minutes; for each tenant ``claim_due`` decides, in ONE transaction, which rows fire:

  * only tenants who finished onboarding, and never inside the tenant's quiet hours (``users.quiet_hours_*``, may wrap
    around midnight) - checked at dispatch time, so changed quiet hours take effect immediately;
  * a row is due while the tenant-local clock is in ``[local_time, local_time + grace)`` and it has not fired today;
  * the claim is the write ``last_fired_on = <local today>`` in the same transaction that selected the row
    (``FOR UPDATE``): two overlapping dispatcher runs cannot both fire the same nudge, and a re-run after a crash is
    harmless (idempotent). A nudge that missed its grace window (downtime) is skipped for that day, not sent hours late;
  * if handing the claimed nudge to the queue then fails, ``release`` takes the claim back so the next tick retries it.
"""

from __future__ import annotations

import logging
import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ.bot.proactive import TRIGGERS, in_quiet_hours
from ai_civ.db import Database
from ai_civ.db.enums import GroupState, MessageRole, OnboardingState
from ai_civ.db.models import Message, ProactiveCheckinLog, ProactiveSchedule, User

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Due:
    trigger_key: str
    schedule_id: uuid.UUID
    local_date: date  # the tenant-local day that was claimed
    previous_fired_on: date | None  # what ``release`` restores


def is_due(local_now: datetime, local_time: time, last_fired_on: date | None, grace: timedelta) -> bool:
    """Pure. ``local_now`` is tz-aware in the tenant's zone; ``local_time`` is a wall-clock time in that zone."""
    today = local_now.date()
    if last_fired_on == today:
        return False
    scheduled = datetime.combine(today, local_time, tzinfo=local_now.tzinfo)
    return scheduled <= local_now < scheduled + grace


async def claim_due(db: Database, tenant_id: uuid.UUID, *, now: datetime, grace: timedelta) -> list[Due]:
    async with db.tenant_session(tenant_id) as session:
        user = (
            await session.execute(
                select(
                    User.timezone, User.onboarding_state, User.group_state, User.quiet_hours_start, User.quiet_hours_end
                ).where(User.id == tenant_id, User.telegram_user_id.is_not(None))  # an app person has no Telegram: not ours
            )
        ).one_or_none()
        if user is None or OnboardingState(user.onboarding_state) is not OnboardingState.done:
            return []
        if GroupState(user.group_state) is not GroupState.active:  # frozen / dormant: nothing is sent to that group
            return []
        local_now = now.astimezone(ZoneInfo(user.timezone))
        if in_quiet_hours(local_now.time(), user.quiet_hours_start, user.quiet_hours_end):
            return []
        rows = (
            await session.execute(
                select(ProactiveSchedule).where(ProactiveSchedule.enabled.is_(True)).with_for_update()
            )
        ).scalars().all()
        claimed: list[Due] = []
        for row in rows:
            if not is_due(local_now, row.local_time, row.last_fired_on, grace):
                continue
            if row.trigger_key not in TRIGGERS:
                log.error("tenant %s: schedule row %s names unknown trigger %r; skipped", tenant_id, row.id, row.trigger_key)
                continue
            claimed.append(Due(row.trigger_key, row.id, local_now.date(), row.last_fired_on))
            row.last_fired_on = local_now.date()
        return claimed


@dataclass(frozen=True)
class CheckinDue:
    log_id: uuid.UUID  # the claim; ``release_checkin`` deletes it when the hand-off to the queue fails


def parse_slots(value: str) -> list[time]:
    """``"10:30,14:00"`` -> the slot times, sorted, without duplicates. A malformed value raises (a typo in the settings is not silently ignored)."""
    slots = sorted({time.fromisoformat(part.strip()) for part in value.split(",") if part.strip()})
    if not slots:
        raise ValueError("CHECKIN_SLOTS has no slot")
    return slots


SLOT_STEP_MINUTES = 5  # the dispatcher ticks every 5 minutes: a slot on a finer grid would only be reached by the next tick anyway
SlotPlanner = Callable[[uuid.UUID, date, time, time], list[time]]  # (tenant, local date, quiet start, quiet end) -> that day's slots


def daily_slots(
    tenant_id: uuid.UUID,
    local_date: date,
    quiet_start: time,
    quiet_end: time,
    *,
    min_slots: int = 3,
    max_slots: int = 4,
    min_gap: timedelta = timedelta(minutes=150),
    edge: timedelta = timedelta(minutes=90),
) -> list[time]:
    """The check-in slots of ONE tenant-local day, chosen at random but DETERMINISTICALLY from (tenant, date): every Beat tick of the day computes the same ones, so a slot is
    never lost or doubled between ticks. They lie inside the user's ACTIVE hours (from the end of the quiet hours to their start, which may wrap around midnight), at least
    ``edge`` away from both ends (nobody is greeted the minute they wake up or right before bed) and at least ``min_gap`` apart; 3-4 of them when the day is long enough, fewer
    (down to one) when the active window is short. Times are on a 5-minute grid, sorted."""
    rng = random.Random(f"checkin:{tenant_id}:{local_date.isoformat()}")  # a str seed is hashed with sha512: stable across processes and runs
    start_min = quiet_end.hour * 60 + quiet_end.minute
    length = (quiet_start.hour * 60 + quiet_start.minute - start_min) % (24 * 60) or 24 * 60  # the active window, in minutes after the end of quiet hours
    edge_min, gap = int(edge.total_seconds() // 60), int(min_gap.total_seconds() // 60)
    span = length - 2 * edge_min
    count = rng.randint(min_slots, max_slots)
    if span <= 0:
        offsets = [length // 2]  # a very short active window: one slot in the middle
    else:
        while count > 1 and (count - 1) * gap > span:
            count -= 1
        free = span - (count - 1) * gap  # the slack the slots may move in
        base = sorted(rng.randint(0, free) for _ in range(count))
        offsets = [edge_min + point + index * gap for index, point in enumerate(base)]  # shifting the i-th point by i gaps keeps every gap >= min_gap
    slots = sorted({(start_min + offset) // SLOT_STEP_MINUTES * SLOT_STEP_MINUTES % (24 * 60) for offset in offsets})
    return [time(minute // 60, minute % 60) for minute in slots]


def thin_slots(slots: list[time], per_day: int | None) -> list[time]:
    """The user's own say over the day's check-ins: ``None`` = the slots as planned, ``0`` = none, ``n`` = n of them spread evenly over the day (one = the middle slot, two =
    the first and the last, ...). Never more than there are. Deterministic, so every Beat tick of the day keeps the same ones."""
    if per_day is None or per_day >= len(slots):
        return list(slots)
    if per_day <= 0 or not slots:
        return []
    if per_day == 1:
        return [slots[len(slots) // 2]]
    last = len(slots) - 1
    return [slots[round(i * last / (per_day - 1))] for i in range(per_day)]


def todays_checkins(
    settings, tenant_id: uuid.UUID, local_date: date, quiet_start: time, quiet_end: time, per_day: int | None
) -> list[time]:
    """The check-in times of one local day exactly as the dispatcher computes them (fixed ``CHECKIN_SLOTS`` or the random plan, then the user's own count). The screen
    "Напоминания" shows this list, so what it shows is what will happen."""
    if not settings.checkin_enabled:
        return []
    fixed = parse_slots(settings.checkin_slots) if settings.checkin_slots.strip() else []
    planned = fixed or daily_slots(
        tenant_id, local_date, quiet_start, quiet_end,
        min_slots=settings.checkin_min_slots, max_slots=settings.checkin_max_slots,
        min_gap=timedelta(minutes=settings.checkin_min_gap_minutes), edge=timedelta(minutes=settings.checkin_edge_minutes),
    )  # fmt: skip
    return thin_slots(planned, per_day)


def current_slot(local_now: datetime, slots: list[time], grace: timedelta) -> time | None:
    """The slot whose window ``[slot, slot + grace)`` contains the tenant-local moment (the latest one if windows overlap), or None."""
    hit = [slot for slot in slots if datetime.combine(local_now.date(), slot, tzinfo=local_now.tzinfo) <= local_now < datetime.combine(local_now.date(), slot, tzinfo=local_now.tzinfo) + grace]
    return hit[-1] if hit else None


async def claim_checkin(
    db: Database,
    tenant_id: uuid.UUID,
    *,
    now: datetime,
    grace: timedelta,
    slots: list[time],
    min_silence: timedelta,
    max_unanswered: int,
    plan: SlotPlanner | None = None,
) -> CheckinDue | None:
    """Should the bot write first to this tenant RIGHT NOW? ONE transaction under ``FOR UPDATE`` of the tenant's row.

    ``slots`` are fixed times (CHECKIN_SLOTS); with ``plan`` the slots of the tenant's local day come from the planner instead (``daily_slots``: random, inside the user's active hours).

    Yes only if: onboarding is done and the group is active; the local clock is inside a slot window and not in quiet hours; the user HAS written
    before (nobody is greeted who never spoke) and has been silent for at least ``min_silence``; fewer than ``max_unanswered`` check-ins were sent
    since their last message (then the bot waits until they write). The claim is the row in ``proactive_checkin_log`` (one per local day and slot):
    overlapping ticks and retries cannot fire a slot twice, and a slot that missed its window is skipped, not sent late."""
    async with db.tenant_session(tenant_id) as session:
        user = (
            await session.execute(
                select(User.timezone, User.onboarding_state, User.group_state, User.quiet_hours_start, User.quiet_hours_end, User.checkin_per_day)
                .where(User.id == tenant_id, User.telegram_user_id.is_not(None))  # an app person has no Telegram: not ours
                .with_for_update()
            )
        ).one_or_none()
        if user is None or OnboardingState(user.onboarding_state) is not OnboardingState.done:
            return None
        if GroupState(user.group_state) is not GroupState.active:
            return None
        local_now = now.astimezone(ZoneInfo(user.timezone))
        if in_quiet_hours(local_now.time(), user.quiet_hours_start, user.quiet_hours_end):
            return None
        if user.checkin_per_day == 0:  # the user switched the check-ins off on the screen "Напоминания"
            return None
        today_slots = slots if plan is None else plan(tenant_id, local_now.date(), user.quiet_hours_start, user.quiet_hours_end)
        today_slots = thin_slots(today_slots, user.checkin_per_day)
        slot = current_slot(local_now, today_slots, grace)
        if slot is None:
            return None
        last_user = (await session.execute(select(func.max(Message.created_at)).where(Message.role == MessageRole.user))).scalar_one()
        if last_user is None or now - last_user < min_silence:
            return None
        unanswered = (
            await session.execute(select(func.count()).select_from(ProactiveCheckinLog).where(ProactiveCheckinLog.sent_at > last_user))
        ).scalar_one()
        if unanswered >= max_unanswered:
            return None
        log_id = (
            await session.execute(
                pg_insert(ProactiveCheckinLog)
                .values(user_id=tenant_id, slot_date=local_now.date(), slot_time=slot)
                .on_conflict_do_nothing(constraint="uq_proactive_checkin_log_slot")
                .returning(ProactiveCheckinLog.id)
            )
        ).scalar_one_or_none()
        return None if log_id is None else CheckinDue(log_id)


async def release_checkin(db: Database, tenant_id: uuid.UUID, due: CheckinDue) -> None:
    """Give a claim back (the hand-off to the queue failed): the slot may be claimed again by the next tick inside its window."""
    async with db.tenant_session(tenant_id) as session:
        await session.execute(delete(ProactiveCheckinLog).where(ProactiveCheckinLog.id == due.log_id))


async def review_due(db: Database, tenant_id: uuid.UUID, *, now: datetime, weekday: int, hour: int) -> bool:
    """Is it time for the tenant's weekly quarantine review? Tenant-local weekday == ``weekday`` (Monday = 0) and the local
    clock has reached ``hour`` (``>=``: a missed tick is made up the same day); the tenant finished onboarding and is not in
    quiet hours."""
    async with db.tenant_session(tenant_id) as session:
        user = (
            await session.execute(
                select(
                    User.timezone, User.onboarding_state, User.group_state, User.quiet_hours_start, User.quiet_hours_end
                ).where(User.id == tenant_id, User.telegram_user_id.is_not(None))  # an app person has no Telegram: not ours
            )
        ).one_or_none()
    if user is None or OnboardingState(user.onboarding_state) is not OnboardingState.done:
        return False
    if GroupState(user.group_state) is not GroupState.active:
        return False
    local_now = now.astimezone(ZoneInfo(user.timezone))
    if local_now.weekday() != weekday or local_now.hour < hour:
        return False
    return not in_quiet_hours(local_now.time(), user.quiet_hours_start, user.quiet_hours_end)


async def release(db: Database, tenant_id: uuid.UUID, due: Due) -> None:
    """Give a claim back (the hand-off to the queue failed): only if it still is the claim made for ``due.local_date``."""
    async with db.tenant_session(tenant_id) as session:
        await session.execute(
            update(ProactiveSchedule)
            .where(ProactiveSchedule.id == due.schedule_id, ProactiveSchedule.last_fired_on == due.local_date)
            .values(last_fired_on=due.previous_fired_on)
        )

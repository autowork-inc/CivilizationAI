"""What an autonomous check-in may talk about (PR 5.4). The bot writes first 3-4 times a day; to be a friend and not a form letter, the message is built from facts that CODE
reads from the database right before it is written - the model gets them as a short section and may use at most one of them:

  * the status of the user's open tasks (the nearest deadlines first, an overdue one is marked);
  * how they feel (always allowed: one light question);
  * the meal window of the local clock (lunch 12:00-14:30, dinner 18:00-20:30): a gentle "have you eaten?", never outside the window;
  * a short warm-up: only when the user has been writing for long without a break in training (the last logged workout is 3+ days ago) - one sentence, no lecture.

Nothing here is a number the model invents: every line is rendered from rows (``tasks``, ``workout_sessions``, ``messages``) or from the clock. Any failure only costs the extras
(the check-in then falls back to the plain instruction): a courtesy must not fail the nudge.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from ai_civ.db import Database
from ai_civ.db.enums import MessageRole
from ai_civ.db.models import Message, ProactiveCheckinLog, Task, WorkoutSession

log = logging.getLogger(__name__)

MAX_TASKS = 3
MORNING = (time(5, 0), time(11, 0))  # the morning slots of the local clock: the first check-in here gets the "wake-up" rule and its follow-up
LUNCH = (time(12, 0), time(14, 30))
DINNER = (time(18, 0), time(20, 30))
WARMUP_AFTER_DAYS = 3
MAX_TITLE_CHARS = 80


def meal_window(local: time) -> str | None:
    if LUNCH[0] <= local < LUNCH[1]:
        return "lunch"
    if DINNER[0] <= local < DINNER[1]:
        return "dinner"
    return None


def is_morning(local: time) -> bool:
    return MORNING[0] <= local < MORNING[1]


def _one_line(value: str) -> str:
    line = " ".join(value.split())
    return line if len(line) <= MAX_TITLE_CHARS else line[: MAX_TITLE_CHARS - 1].rstrip() + "…"


def render(
    *,
    local_now: datetime,
    tasks: list[tuple[str, datetime | None]],
    days_since_workout: int | None,
    silence_hours: float | None,
    followup_minutes: int = 0,
) -> str:
    """The "Check-in context" section. Pure: the rows are read by ``load``."""
    lines = [f"Check-in context (read from the database and the clock by the system; use AT MOST ONE thread, do not list them all): it is now {local_now:%H:%M} for the user."]
    if tasks:
        lines.append("Open tasks, nearest deadline first:")
        for title, due in tasks:
            if due is None:
                lines.append(f'- "{_one_line(title)}"')
            else:
                mark = ", OVERDUE" if due < local_now else ""
                lines.append(f'- "{_one_line(title)}" (due {due.astimezone(local_now.tzinfo):%d.%m %H:%M}{mark})')
        lines.append("You may ask how ONE of them is going (a friendly status question, not a reprimand).")
    else:
        lines.append("There are no open tasks recorded: do not invent any; ask how the day or the goals are going instead.")
    lines.append("You may also simply ask how they feel.")
    window = meal_window(local_now.time())
    if window:
        lines.append(f"It is the {window} window: you may gently ask whether they have eaten.")
    if days_since_workout is not None and days_since_workout >= WARMUP_AFTER_DAYS:
        lines.append(f"The last logged workout was {days_since_workout} days ago: you may suggest a short warm-up or stretch break, in one sentence, without a lecture.")
    if silence_hours is not None and silence_hours >= 6:
        lines.append(f"They have not written for about {int(silence_hours)} hours: keep it light, no pressure.")
    if followup_minutes > 0 and is_morning(local_now.time()):
        lines.append(
            "MORNING SLOT, the first ping of the user's day: write one short, warm good-morning message that invites an answer. Rule of the system: if there is NO answer within "
            f"{followup_minutes} minutes, the SYSTEM itself sends ONE more short, insistent message with an \"I am up\" button. Do not send or promise that second message yourself "
            "and do not mention it."
        )
    return "\n".join(lines)


async def load(db: Database, tenant_id: uuid.UUID, timezone: str, now: datetime, *, followup_minutes: int = 0) -> str | None:
    """The section for this tenant right now, or None when it could not be built (logged; the check-in goes out without it)."""
    try:
        tz = ZoneInfo(timezone)
        async with db.tenant_session(tenant_id) as session:
            rows = (
                await session.execute(
                    select(Task.title, Task.due)
                    .where(Task.status == "open")
                    .order_by(Task.due.asc().nulls_last(), Task.created_at)
                    .limit(MAX_TASKS)
                )
            ).all()
            last_workout = (await session.execute(select(func.max(WorkoutSession.started_at)))).scalar_one()
            last_user = (await session.execute(select(func.max(Message.created_at)).where(Message.role == MessageRole.user))).scalar_one()
        local_now = now.astimezone(tz)
        return render(
            local_now=local_now,
            tasks=[(row.title, row.due) for row in rows],
            days_since_workout=None if last_workout is None else (now - last_workout).days,
            silence_hours=None if last_user is None else (now - last_user).total_seconds() / 3600,
            followup_minutes=followup_minutes,
        )
    except Exception:  # noqa: BLE001 - extras only: the check-in itself must not fail because of them
        log.warning("could not build the check-in context for tenant %s", tenant_id, exc_info=True)
        return None


async def wants_morning_followup(db: Database, tenant_id: uuid.UUID, timezone: str, now: datetime) -> bool:
    """True when the check-in being sent right now is the FIRST of the local day and falls in the morning window. The claim row of this very check-in is already in
    ``proactive_checkin_log`` (``claim_checkin``), so "first" = at most one row for today. Never raises: a failure only means no follow-up."""
    try:
        zone = ZoneInfo(timezone)
        local = now.astimezone(zone)
        if not is_morning(local.time()):
            return False
        async with db.tenant_session(tenant_id) as session:
            sent_today = (
                await session.execute(select(func.count()).select_from(ProactiveCheckinLog).where(ProactiveCheckinLog.slot_date == local.date()))
            ).scalar_one()
        return sent_today <= 1
    except Exception:  # noqa: BLE001
        log.warning("could not decide on the morning follow-up for tenant %s", tenant_id, exc_info=True)
        return False

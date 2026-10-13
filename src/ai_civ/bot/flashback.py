"""The "a year ago" line (v2.0, PR 4.5): one nostalgic sentence at the end of the FIRST check-in of the local day, written by CODE from the database.

``find`` looks at the tenant-local date exactly ``FLASHBACK_LONG_DAYS`` (365) days ago and, if nothing is there, ``FLASHBACK_SHORT_DAYS`` (180) days ago, and takes the
first milestone of that day in this order: a finished goal (a root task done that day), a key fact written that day (a goal or a principle, or priority 70+), a finished
step of a plan, a closed place note. The sentence is a fixed template filled with the user's own stored words ("Год назад в этот день ты завершил задачу «…»"): the
model writes none of it, so it cannot invent a memory. No milestone, a text too long to quote, a check-in that is not the first of the day, a database failure: no
line (it never raises, the check-in itself is never lost because of it).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from ai_civ.bot import texts
from ai_civ.bot.prompt import format_value
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import FactCategory
from ai_civ.db.models import CoreFact, GeoNote, ProactiveCheckinLog, Task

log = logging.getLogger(__name__)

MAX_QUOTE_CHARS = 160
KEY_PRIORITY = 70


def _window(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
    return start, datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)


def _quotable(value: str | None) -> str | None:
    line = " ".join((value or "").split())
    return line if line and len(line) <= MAX_QUOTE_CHARS else None  # a text that does not fit a line is skipped, never cut


async def _milestone(session, start: datetime, end: datetime) -> str | None:
    """The action of the first milestone inside [start, end), or None."""
    root = (
        await session.execute(
            select(Task.title).where(Task.parent_task_id.is_(None), Task.status == "done", Task.updated_at >= start, Task.updated_at < end).order_by(Task.updated_at).limit(1)
        )
    ).scalar_one_or_none()
    if (title := _quotable(root)) is not None:
        return f"завершил задачу «{title}»"
    facts = (
        await session.execute(
            select(CoreFact)
            .where(
                CoreFact.valid_from >= start,
                CoreFact.valid_from < end,
                or_(CoreFact.category.in_([FactCategory.goal, FactCategory.principle]), CoreFact.priority >= KEY_PRIORITY),
            )
            .order_by(CoreFact.priority.desc(), CoreFact.valid_from)
            .limit(5)
        )
    ).scalars().all()
    for fact in facts:
        value = _quotable(format_value(fact.value_text, fact.value_num, fact.value_unit, fact.value_json))
        if value is not None:
            return f"поставил цель: «{value}»" if fact.category is FactCategory.goal else f"записал в профиль: «{value}»"
    step = (
        await session.execute(
            select(Task.title).where(Task.parent_task_id.is_not(None), Task.status == "done", Task.updated_at >= start, Task.updated_at < end).order_by(Task.updated_at).limit(1)
        )
    ).scalar_one_or_none()
    if (title := _quotable(step)) is not None:
        return f"выполнил шаг «{title}»"
    note = (
        await session.execute(
            select(GeoNote.content).where(GeoNote.status == "archived", GeoNote.archived_at >= start, GeoNote.archived_at < end).order_by(GeoNote.archived_at).limit(1)
        )
    ).scalar_one_or_none()
    if (content := _quotable(note)) is not None:
        return f"закрыл заметку «{content}»"
    return None


async def find(db: Database, tenant_id: uuid.UUID, timezone: str, settings: Settings, *, now: datetime | None = None) -> str | None:
    """The line for this check-in, or None. Never raises (except cancellation)."""
    if not settings.flashback_enabled:
        return None
    try:
        zone = ZoneInfo(timezone)
        today = (now or datetime.now(UTC)).astimezone(zone).date()
        async with db.tenant_session(tenant_id) as session:
            sent_today = (
                await session.execute(select(func.count()).select_from(ProactiveCheckinLog).where(ProactiveCheckinLog.slot_date == today))
            ).scalar_one()
            if sent_today > 1:  # the claim of this very check-in is one of the rows; more means an earlier check-in of the day already had its chance
                return None
            for days, ago in ((settings.flashback_long_days, texts.FLASHBACK_LONG), (settings.flashback_short_days, texts.FLASHBACK_SHORT)):
                start, end = _window(today - timedelta(days=days), zone)
                action = await _milestone(session, start, end)
                if action is not None:
                    return texts.FLASHBACK_LINE.format(ago=ago, action=action)
        return None
    except Exception:  # noqa: BLE001 - a nostalgic line must never cost the user their check-in
        log.warning("could not build the flashback line for tenant %s", tenant_id, exc_info=True)
        return None

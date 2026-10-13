"""The decay system (v2.0, PR 5.3): the nightly job that takes XP away for neglect. Never a model: two plain rules, evaluated by code on rows of the database.

  * body  - no logged workout for MORE than ``XP_DECAY_BODY_IDLE_DAYS`` (5) days -> -10 XP in the ``body`` branch, once per tenant-local day
            (``decay:body:<tenant>:<YYYY-MM-DD>``). Only for a user who HAS trained before (a newcomer is not punished for not having started)
            and whose body branch still has something to lose.
  * focus - a task (step) still ``open`` ``XP_DECAY_TASK_OVERDUE_HOURS`` (48) hours after its deadline -> -10 XP in ``focus``, once per task
            (``penalty:task:<task id>``), at most ``XP_DECAY_MAX_TASK_PENALTIES`` tasks per tenant per run.

Idempotency and the floor are the database's job (migration 0023, ``civ.apply_xp_penalty``): a ``source_ref`` is paid once, so a second run for the same date
(a retry, two workers) finds ``duplicate``; a branch never goes below 0 (a penalty that has nothing to take leaves a zero marker, so it is not applied later,
when the user has earned something). All penalties of one tenant are written in ONE transaction: a failure rolls the whole night back and the retry redoes it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text

from ai_civ.bot.xp import BODY_CATEGORY, TASK_CATEGORY
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import OnboardingState

log = logging.getLogger(__name__)

PENALIZED = "penalized"
FLOOR = "floor"
DUPLICATE = "duplicate"


def body_ref(tenant_id: uuid.UUID, local_date: object) -> str:
    return f"decay:body:{tenant_id}:{local_date}"


def task_ref(task_id: uuid.UUID) -> str:
    return f"penalty:task:{task_id}"


@dataclass(frozen=True)
class DecayResult:
    """What one tenant's run did. ``body`` / each ``tasks`` value is the database's code: ``penalized | floor | duplicate``; None = the rule did not apply."""

    local_date: str | None = None
    body: str | None = None
    tasks: dict[str, str] = field(default_factory=dict)

    @property
    def penalties(self) -> int:
        """Entries that really took XP away (a floored marker or a duplicate did not)."""
        return int(self.body == PENALIZED) + sum(1 for code in self.tasks.values() if code == PENALIZED)


SQL_USER = "SELECT timezone, onboarding_state FROM civ.users WHERE id = :u"
SQL_LAST_WORKOUT = "SELECT max(started_at) FROM civ.workout_sessions"
SQL_BRANCH_XP = "SELECT COALESCE(sum(amount), 0)::int FROM civ.xp_ledger WHERE category = :c"
SQL_OVERDUE = """
    SELECT t.id FROM civ.tasks t
    WHERE t.status = 'open' AND t.due IS NOT NULL AND t.due < :cutoff
      AND NOT EXISTS (SELECT 1 FROM civ.xp_ledger l WHERE l.user_id = t.user_id AND l.source_ref = 'penalty:task:' || t.id::text)
    ORDER BY t.due, t.id
    LIMIT :n
"""
SQL_PENALTY = "SELECT civ.apply_xp_penalty(CAST(:c AS text), CAST(:r AS text), CAST(:a AS integer))"


async def run_tenant(db: Database, tenant_id: uuid.UUID, *, now: datetime, settings: Settings) -> DecayResult:
    """One tenant's night. Safe to repeat: the same date / the same task is never punished twice."""
    if not settings.xp_decay_enabled:
        return DecayResult()
    amount = settings.xp_decay_amount
    async with db.tenant_session(tenant_id) as session:
        user = (await session.execute(text(SQL_USER), {"u": tenant_id})).one_or_none()
        if user is None or OnboardingState(user.onboarding_state) is not OnboardingState.done:
            return DecayResult()
        local_date = now.astimezone(ZoneInfo(user.timezone)).date()

        async def penalize(category: str, ref: str) -> str:
            return str((await session.execute(text(SQL_PENALTY), {"c": category, "r": ref, "a": amount})).scalar_one())

        body: str | None = None
        last_workout = (await session.execute(text(SQL_LAST_WORKOUT))).scalar_one()
        if last_workout is not None and now - last_workout > timedelta(days=settings.xp_decay_body_idle_days):
            if (await session.execute(text(SQL_BRANCH_XP), {"c": BODY_CATEGORY})).scalar_one() > 0:  # nothing to lose = no entry (no row a day forever)
                body = await penalize(BODY_CATEGORY, body_ref(tenant_id, local_date))

        tasks: dict[str, str] = {}
        cutoff = now - timedelta(hours=settings.xp_decay_task_overdue_hours)
        overdue = (await session.execute(text(SQL_OVERDUE), {"cutoff": cutoff, "n": settings.xp_decay_max_task_penalties})).scalars().all()
        for task_id in overdue:
            tasks[str(task_id)] = await penalize(TASK_CATEGORY, task_ref(task_id))
    result = DecayResult(str(local_date), body, tasks)
    if result.penalties or body == FLOOR or FLOOR in tasks.values():
        log.info("xp decay tenant=%s date=%s body=%s tasks=%s", tenant_id, local_date, body, tasks)
    return result

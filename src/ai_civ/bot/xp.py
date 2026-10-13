"""Experience awards (RPG, migration 0016). Called by the services AFTER the transaction of the action has committed, never by a model:
a button press (or any other code path) earns XP, words of an LLM never do.

``civ.award_xp`` is idempotent per ``source_ref`` and enforces the 100 XP per tenant-local day cap; this module only adds the tenant
transaction and a safety net: a failed award is logged and reported as ``"error"`` - the action the user just took has already happened
and must not fail (or be repeated) because of its bonus.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import text

from ai_civ.db import Database

log = logging.getLogger(__name__)

AWARD = 10  # XP for one task step / one right card answer / one logged workout
CATEGORIES = ("focus", "intellect", "body", "connection")

TASK_CATEGORY = "focus"
CARD_CATEGORY = "intellect"
BODY_CATEGORY = "body"


def task_ref(step_id: uuid.UUID) -> str:
    return f"task:{step_id}"


def workout_ref(session_id: uuid.UUID) -> str:
    """A logged workout pays once: ``workout:<session id>`` (the per-day cap of 100 XP is enforced by the database)."""
    return f"workout:{session_id}"


def recipe_ref(recipe_id: uuid.UUID) -> str:
    """A cooked recipe pays once: ``recipe:<recipe id>`` (the per-day cap of 100 XP is enforced by the database)."""
    return f"recipe:{recipe_id}"


def card_ref(card_id: uuid.UUID) -> str:
    """The tenant-local date is appended by the database (``award(..., daily=True)``): the same card pays once per local day."""
    return f"card:{card_id}"


async def award(
    db: Database,
    tenant_id: uuid.UUID,
    category: str,
    source_ref: str,
    *,
    amount: int = AWARD,
    daily: bool = False,
) -> str:
    """``awarded | duplicate | daily_cap | error``. ``daily=True`` appends ``:<tenant-local date>`` to ``source_ref`` (computed in SQL, in the user's zone)."""
    ref = (
        "CAST(:ref AS text) || ':' || ((now() AT TIME ZONE (SELECT u.timezone FROM civ.users u WHERE u.id = civ.current_tenant_id()))::date)::text"
        if daily
        else "CAST(:ref AS text)"
    )
    try:
        async with db.tenant_session(tenant_id) as session:
            code = (
                await session.execute(
                    text(f"SELECT civ.award_xp(CAST(:category AS text), {ref}, CAST(:amount AS integer))"),
                    {"category": category, "ref": source_ref, "amount": amount},
                )
            ).scalar_one()
        return str(code)
    except Exception:
        log.warning("could not award XP to tenant %s for %s", tenant_id, source_ref, exc_info=True)
        return "error"

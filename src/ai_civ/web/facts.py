"""Versioned writes to ``core_facts`` for the Mini App (profile and constitution share them).

A fact version is immutable (trigger); an edit is ONE transaction: lock the current row, check that it is the version the page
saw, close it (``valid_to = now()``), insert the next one (``valid_from = now()``, ``source = 'user_manual'``). Two parallel
edits of one fact are ordered by ``SELECT ... FOR UPDATE``; the loser does not silently overwrite the winner: it sees that the
version it edited is gone and gets ``409 conflict`` with the id of the current version. The caller owns the transaction
(``db.tenant_session``) and must let an exception leave the ``async with`` so that everything rolls back together.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ai_civ.db.enums import FactSource
from ai_civ.db.models import CoreFact
from ai_civ.web.deps import ApiError, conflict


class FactOut(BaseModel):
    id: uuid.UUID  # the VERSION id: the page sends it back as ``expected_fact_id``
    key: str
    category: str
    value_text: str | None
    value_num: float | None
    value_unit: str | None
    priority: int
    source: str
    persona_id: uuid.UUID | None
    valid_from: str
    group: str  # a human section of the "Мозг" screen (see ``GROUPS``), never derived from the key by the page
    title: str | None  # a human name for the few system facts ("Город"); None = the fact is just its text, the key is not shown


# ---------------------------------------------------------------- how the screen "Мозг" names things (the page shows no machine key)

# (code, label): the order of the sections on the screen.
GROUPS: tuple[tuple[str, str], ...] = (
    ("identity", "О вас"),
    ("communication", "Общение"),
    ("goals", "Цели"),
    ("projects", "Проекты"),
    ("plans", "Планы"),
    ("habits", "Привычки"),
    ("food", "Питание"),
    ("health", "Здоровье"),
    ("body", "Тело"),
    ("relationships", "Отношения"),
    ("other", "Другое"),
)
GROUP_LABELS = dict(GROUPS)

# Facts the interview / the onboarding write under fixed keys: they get a human name.
SYSTEM_TITLES: dict[str, str] = {
    "name": "Имя",
    "address_as": "Как обращаться",
    "city": "Город",
    "occupation": "Занятие",
    "communication.formality": "Обращение (ты / вы)",
    "communication.length": "Длина ответов",
    "communication.tone": "Тон",
    "communication.notes": "Пожелания к общению",
}

# ``save_fact`` keys are ``<what the model called it>.<slug>`` (``work.trading_bot``); older ones may use an underscore (``work_trading_bot``).
_KEY_PREFIX = re.compile(r"^(preference|habit|plan|work|food|health|goal)[._]")
_PREFIX_GROUP = {
    "preference": "habits",
    "habit": "habits",
    "plan": "plans",
    "work": "projects",
    "food": "food",
    "health": "health",
    "goal": "goals",
}
_CATEGORY_GROUP = {
    "identity": "identity",
    "preference": "habits",
    "goal": "goals",
    "body": "body",
    "health": "health",
    "relationship": "relationships",
    "principle": "other",
    "other": "other",
}


def fact_group(key: str, category: str) -> str:
    """The section of the screen for a fact: fixed keys first, then the kind the key's prefix names, then the category column."""
    if key.startswith("communication."):
        return "communication"
    if key in SYSTEM_TITLES:
        return "identity"
    prefix = _KEY_PREFIX.match(key)
    if prefix is not None:
        return _PREFIX_GROUP[prefix.group(1)]
    return _CATEGORY_GROUP.get(category, "other")


def fact_title(key: str) -> str | None:
    if key in SYSTEM_TITLES:
        return SYSTEM_TITLES[key]
    goal = re.fullmatch(r"goal\.(\d+)", key)
    return f"Цель {goal.group(1)}" if goal else None


def has_content(fact: CoreFact) -> bool:
    """A fact worth showing: it says something (a non-blank text or a number). Empty rows are not rendered."""
    return bool((fact.value_text or "").strip()) or fact.value_num is not None


class GroupOut(BaseModel):
    group: str
    label: str
    facts: list[FactOut]


def grouped(rows: list[CoreFact]) -> list[GroupOut]:
    """The facts of the screen: only those with content, in the order of ``GROUPS``, inside a section newest priority first (the order of ``rows``)."""
    buckets: dict[str, list[FactOut]] = {}
    for row in rows:
        if has_content(row):
            out = fact_out(row)
            buckets.setdefault(out.group, []).append(out)
    return [GroupOut(group=code, label=label, facts=buckets[code]) for code, label in GROUPS if code in buckets]


def fact_out(fact: CoreFact) -> FactOut:
    return FactOut(
        id=fact.id,
        key=fact.key,
        category=fact.category.value,
        value_text=fact.value_text,
        value_num=None if fact.value_num is None else float(fact.value_num),
        value_unit=fact.value_unit,
        priority=fact.priority,
        source=fact.source.value,
        persona_id=fact.persona_id,
        valid_from=fact.valid_from.isoformat(),
        group=fact_group(fact.key, fact.category.value),
        title=fact_title(fact.key),
    )


SNIPPET_CHARS = 200


class MemoryOut(BaseModel):
    """One indexed record of the episodic memory (Tier 2) as the page shows it: no embedding, no source ids, only a short readable piece."""

    id: uuid.UUID
    created_at: str
    snippet: str
    importance: int


def snippet(content: str, limit: int = SNIPPET_CHARS) -> str:
    """The first ``limit`` characters on ONE line (whitespace collapsed), with an ellipsis when the text was cut."""
    line = " ".join(content.split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class Changes:
    """Fields the user changed; ``None`` = keep the old value. ``clear_*`` lets the page remove a value part explicitly."""

    value_text: str | None = None
    value_num: Decimal | None = None
    value_unit: str | None = None
    priority: int | None = None


async def current(session: AsyncSession, key: str, *, lock: bool = False) -> CoreFact | None:
    query = select(CoreFact).where(CoreFact.key == key, CoreFact.valid_to.is_(None))
    return (await session.execute(query.with_for_update() if lock else query)).scalar_one_or_none()


async def lock_expected(session: AsyncSession, key: str, expected_id: uuid.UUID) -> CoreFact:
    """The current version of ``key`` WITH a row lock, if (and only if) it is the one the page edited. Otherwise 404 / 409."""
    row = await current(session, key, lock=True)
    if row is None:
        # no row, or a parallel edit committed while we waited for its lock: the re-check of a locked row does not see the
        # version that edit inserted, so look again without a lock to tell "gone" from "replaced"
        row = await current(session, key)
        if row is None:
            raise ApiError(404, "not_found", "Такой записи уже нет.")
    if row.id != expected_id:
        raise conflict("Запись только что изменилась. Обновил данные — проверьте и повторите.", current_fact_id=str(row.id))
    return row


async def replace_version(session: AsyncSession, key: str, expected_id: uuid.UUID, changes: Changes) -> tuple[CoreFact, CoreFact]:
    """Close the version the page saw and insert the next one. Returns (closed, new)."""
    old = await lock_expected(session, key, expected_id)
    await session.execute(update(CoreFact).where(CoreFact.id == old.id).values(valid_to=func.now()))
    values: dict[str, Any] = {
        "user_id": old.user_id,
        "persona_id": old.persona_id,
        "category": old.category,
        "key": old.key,
        "value_text": old.value_text if changes.value_text is None else changes.value_text,
        "value_num": old.value_num if changes.value_num is None else changes.value_num,
        "value_unit": old.value_unit if changes.value_unit is None else changes.value_unit,
        "value_json": old.value_json,
        "priority": old.priority if changes.priority is None else changes.priority,
        "source": FactSource.user_manual,
    }
    new = CoreFact(**values)
    session.add(new)
    await session.flush()
    await session.refresh(new)
    return old, new


async def close_version(session: AsyncSession, key: str, expected_id: uuid.UUID) -> CoreFact:
    """A deliberate removal: the version is closed, its history stays."""
    old = await lock_expected(session, key, expected_id)
    await session.execute(update(CoreFact).where(CoreFact.id == old.id).values(valid_to=func.now()))
    return old

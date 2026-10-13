"""Screen "Конституция": the user's principles (Core facts of category ``principle``, key ``principle.<slug>``).

Who wins between a principle and an emotional state is decided by the SQL view ``civ.effective_principles`` (the state must be
active, not expired and have a STRICTLY higher priority); this module only reads its verdict, it never recomputes it.

A manual edit of a principle by the user RESETS the temporary overrides of that principle: the link rows
(``emotional_overrides``) point at the version being replaced, and they are deleted in the same transaction, so the rule comes
back at full priority. The emotional state itself stays (it still colours the tone of voice); only its power over this rule ends.
``/restore`` does the same without changing the text ("bring the rule back into force").
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from fastapi import APIRouter, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, select, text
from sqlalchemy.exc import IntegrityError

from ai_civ.bot.personas import clean_line
from ai_civ.db.enums import FactCategory, FactSource
from ai_civ.db.models import CoreFact, EmotionalOverride
from ai_civ.web import facts
from ai_civ.web.deps import DB, Auth, ApiError, conflict

router = APIRouter(prefix="/principles", tags=["constitution"])

MAX_TEXT = 300  # the same bound as the onboarding interview
DEFAULT_PRIORITY = 70  # what the interview gives a principle
PREFIX = "principle."
MAX_SLUG = 36

PrincipleKey = Annotated[str, Path(max_length=100, pattern=r"^principle\.[a-z][a-z0-9_]*$")]

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k",
    "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", "і": "i", "ї": "yi", "є": "ye",
}  # fmt: skip


def slugify(value: str) -> str:
    """An id for a new principle from its own words: ``principle.<this>``. Cyrillic is transliterated; the result always matches
    the database key CHECK (starts with a letter, a-z 0-9 _ only, at most MAX_SLUG characters)."""
    latin = "".join(_TRANSLIT.get(ch, ch) for ch in value.lower())
    slug = re.sub(r"[^a-z0-9]+", "_", latin).strip("_")[:MAX_SLUG].strip("_")
    if not slug:
        return "rule"
    return slug if slug[0].isalpha() else f"p_{slug}"[:MAX_SLUG]


class PrincipleOut(BaseModel):
    fact_id: uuid.UUID  # the VERSION id: sent back as ``expected_fact_id``
    key: str
    text: str
    priority: int
    persona_id: uuid.UUID | None = None
    overridden: bool = False
    overridden_by: str | None = None  # label of the emotional state that currently outranks this principle
    override_priority: int | None = None
    override_mode: str | None = None  # "suspend" | "soften"
    override_note: str | None = None


class PrincipleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=MAX_TEXT * 3)
    priority: int = Field(default=DEFAULT_PRIORITY, ge=0, le=100)

    @field_validator("text")
    @classmethod
    def _one_line(cls, value: str) -> str:
        value = clean_line(value, MAX_TEXT)
        if not value:
            raise ValueError("must not be blank")
        return value


class PrincipleEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_fact_id: uuid.UUID
    text: str | None = Field(default=None, max_length=MAX_TEXT * 3)
    priority: int | None = Field(default=None, ge=0, le=100)

    @field_validator("text")
    @classmethod
    def _one_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = clean_line(value, MAX_TEXT)
        if not value:
            raise ValueError("must not be blank")
        return value

    @model_validator(mode="after")
    def _something_changes(self) -> PrincipleEdit:
        if self.text is None and self.priority is None:
            raise ValueError("send text or priority")
        return self


class EditResult(BaseModel):
    principle: PrincipleOut
    overrides_cleared: int  # temporary overrides that were lifted by this edit


class RestoreIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_fact_id: uuid.UUID


class RestoreResult(BaseModel):
    overrides_cleared: int


def from_fact(fact: CoreFact) -> PrincipleOut:
    return PrincipleOut(
        fact_id=fact.id, key=fact.key, text=fact.value_text or "", priority=fact.priority, persona_id=fact.persona_id
    )


@router.get("", response_model=list[PrincipleOut])
async def list_principles(ctx: Auth, db: DB) -> list[PrincipleOut]:
    async with db.tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.execute(
                text(
                    "SELECT fact_id, key, value_text, priority, persona_id, is_overridden, overridden_by, override_priority, "
                    "override_mode, override_note FROM civ.effective_principles ORDER BY priority DESC, key"
                )
            )
        ).all()
    return [
        PrincipleOut(
            fact_id=r.fact_id,
            key=r.key,
            text=r.value_text or "",
            priority=r.priority,
            persona_id=r.persona_id,
            overridden=bool(r.is_overridden),
            overridden_by=r.overridden_by if r.is_overridden else None,
            override_priority=r.override_priority if r.is_overridden else None,
            override_mode=str(r.override_mode) if r.is_overridden else None,
            override_note=r.override_note if r.is_overridden else None,
        )
        for r in rows
    ]


@router.post("", response_model=PrincipleOut, status_code=201)
async def create_principle(body: PrincipleIn, ctx: Auth, db: DB) -> PrincipleOut:
    try:
        async with db.tenant_session(ctx.tenant_id) as session:
            taken = set((await session.execute(select(CoreFact.key).where(CoreFact.valid_to.is_(None), CoreFact.key.like("principle.%")))).scalars())
            base = slugify(body.text)
            slug, n = base, 2
            while f"{PREFIX}{slug}" in taken:
                slug, n = f"{base[: MAX_SLUG - 4]}_{n}", n + 1
            fact = CoreFact(
                user_id=ctx.tenant_id,
                category=FactCategory.principle,
                key=f"{PREFIX}{slug}",
                value_text=body.text,
                priority=body.priority,
                source=FactSource.user_manual,
            )
            session.add(fact)
            await session.flush()  # the partial unique index decides a race for the same key here
            await session.refresh(fact)
            return from_fact(fact)
    except IntegrityError:
        raise conflict("Такой принцип только что появился. Повторите.") from None


async def _principle(session, key: str, expected_id: uuid.UUID) -> CoreFact:
    fact = await facts.lock_expected(session, key, expected_id)
    if fact.category is not FactCategory.principle:
        raise ApiError(400, "wrong_screen", "Это не принцип.")
    return fact


@router.put("/{key}", response_model=EditResult)
async def edit_principle(key: PrincipleKey, body: PrincipleEdit, ctx: Auth, db: DB) -> EditResult:
    async with db.tenant_session(ctx.tenant_id) as session:
        old = await _principle(session, key, body.expected_fact_id)
        same_text = body.text is None or body.text == (old.value_text or "")
        same_priority = body.priority is None or body.priority == old.priority
        if same_text and same_priority:  # nothing to save: no new version, and no reset (that is what /restore is for)
            return EditResult(principle=from_fact(old), overrides_cleared=0)
        old, new = await facts.replace_version(
            session, key, body.expected_fact_id, facts.Changes(value_text=body.text, priority=body.priority)
        )
        cleared = (await session.execute(delete(EmotionalOverride).where(EmotionalOverride.core_fact_id == old.id))).rowcount
        return EditResult(principle=from_fact(new), overrides_cleared=cleared or 0)


@router.post("/{key}/restore", response_model=RestoreResult)
async def restore_principle(key: PrincipleKey, body: RestoreIn, ctx: Auth, db: DB) -> RestoreResult:
    """Lift every temporary override of this principle without touching its text. Idempotent (0 if nothing overrode it)."""
    async with db.tenant_session(ctx.tenant_id) as session:
        fact = await _principle(session, key, body.expected_fact_id)
        cleared = (await session.execute(delete(EmotionalOverride).where(EmotionalOverride.core_fact_id == fact.id))).rowcount
        return RestoreResult(overrides_cleared=cleared or 0)


@router.delete("/{key}", status_code=204)
async def delete_principle(key: PrincipleKey, ctx: Auth, db: DB, expected_fact_id: Annotated[uuid.UUID, Query()]) -> None:
    async with db.tenant_session(ctx.tenant_id) as session:
        await _principle(session, key, expected_fact_id)
        await facts.close_version(session, key, expected_fact_id)

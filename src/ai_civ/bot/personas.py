"""Personas = Telegram forum topics = roles of the bot. A persona owns only a System Prompt; it has no memory of its own
(CLAUDE.md "Единое состояние"): every persona reads the same brain and only the prompt differs.

DB operations take an open ``tenant_session`` (the caller owns the transaction).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_civ.db.enums import FactCategory, PersonaMode
from ai_civ.db.models import CoreFact, Message, Persona

CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
RESERVED_CODES = frozenset({"general"})  # General is not a persona: it is the concierge, with persona_id NULL
FEEDBACK_CODE = "feedback"
SYSTEM_CODES = frozenset({"settings", FEEDBACK_CODE})  # created by the code itself, never proposed, renamed or deleted by a model
MAX_TITLE_CHARS = 64  # Telegram allows 128 for a topic name; shorter names keep the topic list readable
MAX_FOCUS_CHARS = 300
FALLBACK_EMOJI = "📌"  # for a title the model wrote without any emoji
TitleSource = Literal["bot", "user"]  # who set the current topic name (personas.title_source)

# Tone of Voice and the user's state live in ONE place (prompt.VOICE), shared by every persona: a persona only adds its role.
CONCIERGE_PROMPT = """You are the user's personal AI avatar in the main chat of their Telegram forum. The user may have
renamed this topic, so never call it by a name. You handle everything that has no topic of its own: questions, planning,
small talk. When the user starts caring about a distinct activity or goal, the system opens a dedicated topic for it;
never claim to have created one yourself."""

PERSONA_TEMPLATE = """You are the user's personal AI avatar in their topic "{title}".
Topic focus: {focus}
Stay inside this focus and keep it concrete."""


@dataclass(frozen=True)
class PersonaSpec:
    code: str
    title: str
    focus: str


# The only topic every user gets regardless of goals: the weekly review of the quarantine and, later, the Mini App entry live here.
SETTINGS_PERSONA = PersonaSpec(
    "settings", "⚙️ Настройки", "settings: what the avatar remembers about the user, how it behaves, how to change it"
)


# The feedback channel: whatever the user writes here is NOT answered by the model but forwarded to the project's author (``Orchestrator._feedback``).
FEEDBACK_PERSONA = PersonaSpec(
    FEEDBACK_CODE, "💡 Идеи и отзывы", "feedback: ideas, wishes and complaints about the product; the messages go to the author of the project"
)


def compose_system_prompt(title: str, focus: str) -> str:
    return PERSONA_TEMPLATE.format(title=title, focus=focus)


def clean_line(value: str, limit: int) -> str:
    """One line, trimmed, at most ``limit`` characters (model-written values are never stored verbatim and unbounded)."""
    return " ".join(value.split())[:limit].strip()


_HIDDEN = re.compile(r"[\x00-\x1f\x7f‪-‮⁦-⁩​-‏]")  # control, bidi and zero-width characters


def has_hidden_chars(value: str) -> bool:
    """True if the value contains characters that change how a name is displayed or hide it (never allowed in a model-written name)."""
    return _HIDDEN.search(value) is not None


def has_emoji(value: str) -> bool:
    """Any pictographic symbol (category So covers emoji, flags and dingbats)."""
    return any(unicodedata.category(ch) == "So" for ch in value)


def normalize_title(raw: str, fallback: str) -> str:
    """A topic name: one line, at most MAX_TITLE_CHARS, always with an emoji. A model that forgot the emoji gets a neutral
    one from us; an empty title is replaced by ``fallback`` (derived by the code from the persona code)."""
    title = clean_line(raw, MAX_TITLE_CHARS) or clean_line(fallback, MAX_TITLE_CHARS)
    if not has_emoji(title):
        title = clean_line(f"{FALLBACK_EMOJI} {title}", MAX_TITLE_CHARS)
    return title


# ---------------------------------------------------------------- SQL (caller owns the tenant transaction)


async def get_by_thread(session: AsyncSession, thread_id: int) -> Persona | None:
    return (
        await session.execute(
            select(Persona).where(Persona.telegram_thread_id == thread_id, Persona.is_active.is_(True))
        )
    ).scalar_one_or_none()


async def get_by_code(session: AsyncSession, code: str) -> Persona | None:
    return (await session.execute(select(Persona).where(Persona.code == code))).scalar_one_or_none()


async def get_by_id(session: AsyncSession, persona_id: uuid.UUID) -> Persona | None:
    return (await session.execute(select(Persona).where(Persona.id == persona_id))).scalar_one_or_none()


async def set_mode(session: AsyncSession, persona_id: uuid.UUID, mode: PersonaMode) -> bool:
    """Store the language mode of a topic. False = no such persona. Idempotent: the same mode again is not an error."""
    persona = await get_by_id(session, persona_id)
    if persona is None:
        return False
    persona.mode = mode.value
    return True


async def list_active(session: AsyncSession) -> list[Persona]:
    return list(
        (await session.execute(select(Persona).where(Persona.is_active.is_(True)).order_by(Persona.code))).scalars()
    )


async def ensure_persona(session: AsyncSession, user_id: uuid.UUID, spec: PersonaSpec) -> Persona:
    """The persona with this code: created if missing, re-activated if it was switched off. Idempotent."""
    await session.execute(
        pg_insert(Persona)
        .values(
            user_id=user_id,
            code=spec.code,
            title=spec.title,
            system_prompt=compose_system_prompt(spec.title, spec.focus),
        )
        .on_conflict_do_nothing(constraint="uq_personas_user_code")
    )
    persona = await get_by_code(session, spec.code)
    assert persona is not None  # inserted above or already there (RLS shows only this tenant's rows)
    if not persona.is_active:
        persona.is_active = True
    return persona


async def load_goals(session: AsyncSession) -> list[str]:
    """The user's current global goals (written by the onboarding interview as ``goal.N``), in the order they were named."""
    rows = (
        await session.execute(
            select(CoreFact.value_text)
            .where(
                CoreFact.category == FactCategory.goal,
                CoreFact.valid_to.is_(None),
                CoreFact.persona_id.is_(None),
                CoreFact.value_text.is_not(None),
            )
            .order_by(CoreFact.key)
        )
    ).scalars()
    return [clean_line(value, 200) for value in rows if value and value.strip()]


async def set_title(session: AsyncSession, persona_id: uuid.UUID, title: str, *, source: TitleSource | None = None) -> bool:
    """The topic now has this name: store it, and refresh the one place the persona's prompt says it (``topic "<title>"``).
    False = no such persona. A prompt that does not contain the old marker (hand-edited) is left alone.

    ``source`` ('bot' | 'user') also stamps WHO set the name and WHEN (the cooldown of autonomous renames runs from this stamp). None =
    the name is only being brought in line with what the topic already shows: nobody renamed anything, so the stamp is left alone."""
    persona = await get_by_id(session, persona_id)
    if persona is None:
        return False
    old, new = f'topic "{persona.title}"', f'topic "{title.replace(chr(34), chr(39))}"'  # a quote in the name must not break the prompt
    if old in persona.system_prompt:
        persona.system_prompt = persona.system_prompt.replace(old, new, 1)
    persona.title = title
    if source is not None:
        persona.title_source = source
        persona.title_changed_at = (await session.execute(select(func.now()))).scalar_one()
    return True


@dataclass(frozen=True)
class RenameClaim:
    """The right to rename one topic, taken in the database. ``previous_*`` is what the row said before, so a failed attempt can give it back."""

    claimed_at: datetime
    previous_changed_at: datetime | None
    previous_source: TitleSource


async def claim_rename(
    session: AsyncSession, persona_id: uuid.UUID, *, bot_cooldown: timedelta, user_cooldown: timedelta
) -> RenameClaim | None:
    """Take the right to rename a topic by itself: ONE transaction, the row locked (``FOR UPDATE``), so two quick messages (or two bot
    processes) cannot both pass. None = not allowed now: no such topic, a topic without a forum topic, a system topic (Settings), or the
    cooldown has not passed (``bot_cooldown`` after the bot's own rename, ``user_cooldown`` after a manual one; NULL stamp = never renamed).
    The caller renames in Telegram AFTER this commits and gives the claim back (``release_rename``) if Telegram refused."""
    row = (await session.execute(select(Persona).where(Persona.id == persona_id).with_for_update())).scalar_one_or_none()
    if row is None or not row.is_active or row.telegram_thread_id is None or row.code in SYSTEM_CODES:
        return None
    now = (await session.execute(select(func.now()))).scalar_one()
    cooldown = user_cooldown if row.title_source == "user" else bot_cooldown
    if row.title_changed_at is not None and now - row.title_changed_at < cooldown:
        return None
    claim = RenameClaim(now, row.title_changed_at, row.title_source)  # type: ignore[arg-type] - the column is CHECKed to the two values
    row.title_changed_at, row.title_source = now, "bot"
    return claim


async def release_rename(session: AsyncSession, persona_id: uuid.UUID, claim: RenameClaim) -> None:
    """Give a claim back because nothing was renamed. Only if the row still carries OUR claim: a manual rename that happened in between
    (it re-stamped the row) is never undone."""
    await session.execute(
        update(Persona)
        .where(Persona.id == persona_id, Persona.title_source == "bot", Persona.title_changed_at == claim.claimed_at)
        .values(title_changed_at=claim.previous_changed_at, title_source=claim.previous_source)
    )


def rename_allowed(persona: Persona, *, bot_cooldown: timedelta, user_cooldown: timedelta, now: datetime) -> bool:
    """Whether the model is OFFERED the rename function at all (a pure pre-check on the row already loaded; the database still decides
    in ``claim_rename``). Not in a system topic, not without a forum topic, not inside the cooldown."""
    if not persona.is_active or persona.telegram_thread_id is None or persona.code in SYSTEM_CODES:
        return False
    if persona.title_changed_at is None:
        return True
    return now - persona.title_changed_at >= (user_cooldown if persona.title_source == "user" else bot_cooldown)


async def attach_topic(session: AsyncSession, persona_id: uuid.UUID, thread_id: int) -> bool:
    """Bind a freshly created forum topic to the persona, only if it has none yet. False = it already had one.
    The name was chosen a moment ago, so the topic counts as just named (the cooldown of autonomous renames starts here)."""
    result = await session.execute(
        update(Persona)
        .where(Persona.id == persona_id, Persona.telegram_thread_id.is_(None))
        .values(telegram_thread_id=thread_id, title_changed_at=func.now(), title_source="bot")
    )
    return result.rowcount == 1


async def clear_thread(session: AsyncSession, thread_id: int) -> int:
    """Telegram says the forum topic ``thread_id`` does not exist (it was deleted by hand): forget the binding. The persona stays as it is (active,
    with its prompt and history) but has no topic, so a later ``ensure_persona`` + ``_ensure_topic`` makes a NEW one. Idempotent. Returns how many
    personas were unbound (0 = nothing was bound to that thread, e.g. a second send into the same dead topic)."""
    result = await session.execute(update(Persona).where(Persona.telegram_thread_id == thread_id).values(telegram_thread_id=None))
    return result.rowcount


async def adopt_message(session: AsyncSession, message_id: uuid.UUID, persona_id: uuid.UUID) -> None:
    """Re-file a message written in General into the persona it turned out to belong to (it is stored first, routed later)."""
    await session.execute(update(Message).where(Message.id == message_id).values(persona_id=persona_id))


async def retire(session: AsyncSession, persona_id: uuid.UUID) -> bool:
    """The forum topic of this persona was deleted: the persona is switched off and loses its thread id (so a later ``ensure_persona`` of the same code
    re-activates it and a NEW topic is made for it). Its messages stay: the conversation is part of the user's history. False = no such persona."""
    persona = await get_by_id(session, persona_id)
    if persona is None:
        return False
    persona.is_active = False
    persona.telegram_thread_id = None
    return True

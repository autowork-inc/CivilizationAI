"""Instant memory: the model may call ``save_fact`` the moment the user states something lasting about themselves ("бегаю по утрам по 5 км",
"в пятницу иду к стоматологу"), without waiting for the nightly Garbage Collector.

The GC stays the careful path (quarantine, weekly review). This is the fast path, and it is narrow on purpose, because it writes to Core
without a human review - so what lets it write is enforced by CODE and, last, by the database:

  * the model supplies words only: ``category`` (a closed list; ``habit`` is stored as ``preference``), ``topic`` (a slug), ``content`` and ``evidence``. ``evidence`` must be a
    VERBATIM quote of the user's CURRENT message (case, punctuation and whitespace aside: speech-to-text and typing differ in those) and every number in ``content`` must stand in that quote,
    so the model cannot invent a fact or a figure the user did not write. Text from anywhere else (a contact's data, later an attached
    file) is never "the user said so": the quote is matched against ``UserTurn.text`` only;
  * secrets are refused (passwords, tokens, card / phone / ID-like digit runs): fail closed;
  * priority is fixed at 50 and the DB refuses ``source='chat_inline'`` above 60, for a principle, or without the message it came from
    (migration 0010); a fact whose priority is above 50 is protected here too: the Mini App or the quarantine changes it, not a chat line;
  * a changed fact is a NEW VERSION (the old one is closed, history stays), at most ``SAVE_FACT_MAX_PER_DAY`` versions per day;
  * what the user reads about it is a card written by CODE after the commit, with a button that undoes it. The words the model wrote next
    to the call are never sent: the reply is phrased again with a note that says what the system did.

Undo is one transaction under ``FOR UPDATE``: the version is closed and, if it had replaced an older one, that older one comes back as a
new version (a clone: versions are immutable). A second press, or a press after the fact moved on, changes nothing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, text, update

from ai_civ.bot import personas, texts
from ai_civ.bot.janitor import Janitor
from ai_civ.bot.tools.base import Card, Offered, ToolContext, ToolResult
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import FactCategory, FactSource
from ai_civ.db.models import CoreFact
from ai_civ.memory.llm import ToolCall, ToolSpec

log = logging.getLogger(__name__)

TOOL_NAME = "save_fact"
INLINE_PRIORITY = 50  # below every principle (>= 60): a line in a chat never outranks what the user wrote into their constitution
MAX_CONTENT_CHARS = 300
MAX_EVIDENCE_CHARS = 400
MIN_EVIDENCE_CHARS = 3
SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

# A word the model may use that is stored as another one: a habit IS a preference (``preference.morning_run``), so the key does not fork
# into two spellings of the same subject. The alias is applied to the key too, not only to the column.
CATEGORY_ALIASES: dict[str, str] = {"habit": "preference"}

# What the model calls a fact -> the category column (a PostgreSQL enum that is not widened for this). The model's own word stays in the key
# (``habit.morning_run``), so ``plan`` and ``work`` do not pass for ``goal``: the goals of the interview feed ``/setup`` and must stay theirs.
CATEGORIES: dict[str, FactCategory] = {
    "habit": FactCategory.preference,
    "plan": FactCategory.other,
    "preference": FactCategory.preference,
    "health": FactCategory.health,
    "food": FactCategory.preference,
    "work": FactCategory.other,
    "other": FactCategory.other,
}

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Remember a lasting fact the USER has just told you about THEMSELVES: a habit, a plan, a preference, a health restriction, a food "
        "rule, a work routine. Call it only when the user's CURRENT message states it. Do NOT call it for moods or feelings, questions, "
        "hypotheticals, opinions, facts about OTHER people, anything a contact or a document says, or secrets (passwords, card or ID "
        "numbers). One call per fact. The system shows the user a card 'remembered' with a cancel button: you never confirm it yourself."
    ),
    parameters={
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(CATEGORIES), "description": "What kind of fact it is."},
            "topic": {
                "type": "string",
                "description": "A short latin snake_case name of WHAT the fact is about, e.g. morning_run, dentist_visit, lactose. The SAME name "
                "for the same subject, so a changed fact replaces the old one instead of piling up.",
            },
            "content": {
                "type": "string",
                "description": f"The fact as one short sentence in the user's language, at most {MAX_CONTENT_CHARS} characters. Only what the user "
                "said: add nothing, infer nothing. Numbers exactly as the user wrote them.",
            },
            "evidence": {
                "type": "string",
                "description": "A VERBATIM quote of the user's own words from their current message that states the fact: copy the words "
                "exactly (case and punctuation do not matter). Every number in `content` must appear in this quote.",
            },
        },
        "required": ["category", "topic", "content", "evidence"],
    },
)

INSTRUCTIONS = (
    "## Memory\n"
    "When the user tells you something lasting about themselves (a habit, a plan, a preference, a health or food restriction, a work\n"
    "routine) call save_fact: the SYSTEM then shows them a card 'remembered' with a cancel button. Do it quietly, once per fact, and\n"
    "only for what their CURRENT message says; quote their words in `evidence`. Never save moods, questions, guesses, other people's\n"
    "data or secrets. Reply to the message as usual: never say you remembered, saved or noted something (the card says it), and\n"
    "do not mention the card."
)


SaveOutcome = Literal["saved", "updated", "unchanged", "protected", "limit"]


@dataclass(frozen=True)
class Saved:
    outcome: SaveOutcome
    content: str
    fact_id: uuid.UUID | None = None  # the version that is current now (saved / updated)


def render_note(saved: Sequence[Saved]) -> str:
    """The section of the prompt for the turn that phrases the reply once the system has acted on a call."""
    lines = ["## Memory (made by the system)"]
    done = [s for s in saved if s.outcome in ("saved", "updated")]
    refused = [s for s in saved if s.outcome in ("protected", "limit")]
    if done:
        lines += [
            "The system saved the facts below to the user's profile; the user sees a card with a cancel button right under your reply.",
            "Do not say you remembered, saved or noted them and do not mention the card: just answer the user's message as usual.",
        ]
        lines += [f"- {s.content}" for s in done]
    if refused:
        lines.append("The system did NOT save the facts below (the user is told why right after your reply). Do not say they were remembered.")
        lines += [f"- {s.content}" for s in refused]
    return "\n".join(lines)


# ---------------------------------------------------------------- validating a call (the model proposes, the code decides)

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_SECRET_WORDS = re.compile(
    r"парол|passw|passcode|\bcvv\b|\bcvc\b|seed phrase|сид[- ]?фраз|мнемоник|api[ _-]?key|токен|\btoken\b|secret|секретн|пин[- ]?код|\bpin\b|private key|приватн\w* ключ",
    re.IGNORECASE,
)
_LONG_DIGITS = re.compile(r"(?:\d[ \-]?){9,}")  # a card, a phone, an ID, an IBAN: nine digits in a row, spaces and dashes aside


def _norm(value: str) -> str:
    return " ".join(value.split()).casefold()


_NOT_A_WORD = re.compile(r"[\W_]+", re.UNICODE)


def _squash(value: str) -> str:
    """The form in which a quote is compared with the message: lower case, ``ё`` = ``е``, every run of punctuation / symbols / whitespace is one
    space. Speech-to-text and typing differ in exactly these things (a comma, a dash, a capital letter) and in nothing that changes a
    meaning; the WORDS and the NUMBERS still have to be the user's."""
    return " ".join(_NOT_A_WORD.sub(" ", value.casefold().replace("ё", "е")).split())


def _numbers(value: str) -> set[str]:
    return {token.replace(",", ".") for token in _NUMBER.findall(value)}


def looks_secret(value: str) -> bool:
    """Fail closed: anything that smells like a credential or a long identifier is not stored in a profile that is read into every prompt."""
    return _SECRET_WORDS.search(value) is not None or _LONG_DIGITS.search(value) is not None


@dataclass(frozen=True)
class ParsedFact:
    category: str  # the model's word (a key of CATEGORIES)
    key: str  # <category>.<slug>
    content: str
    evidence: str


def _slug(raw: Any, content: str) -> str:
    """The slug the model chose, cleaned; when it is unusable, a stable one derived from the content (the same sentence -> the same key)."""
    if isinstance(raw, str):
        slug = re.sub(r"_+", "_", re.sub(r"[^a-z0-9_]", "_", raw.strip().lower())).strip("_")
        if SLUG_RE.match(slug):
            return slug
    return "f_" + hashlib.sha1(_norm(content).encode("utf-8")).hexdigest()[:10]


def parse_fact(args: dict[str, Any], user_text: str) -> ParsedFact | None:
    """None = the call is dropped silently (the user did not ask for a memory, so there is nothing to apologise for): an unknown category,
    an unreadable or hidden-character content, evidence that is NOT a verbatim piece of the user's own message, a number in the content
    that the quote does not contain, or anything that looks like a secret."""
    category = str(args.get("category", "")).strip().lower()
    category = CATEGORY_ALIASES.get(category, category)
    if category not in CATEGORIES:
        return None
    content, evidence = args.get("content"), args.get("evidence")
    if not isinstance(content, str) or not isinstance(evidence, str):
        return None
    content, evidence = " ".join(content.split()), " ".join(evidence.split())
    if not content or len(content) > MAX_CONTENT_CHARS or personas.has_hidden_chars(content) or not any(ch.isalnum() for ch in content):
        return None
    if not MIN_EVIDENCE_CHARS <= len(evidence) <= MAX_EVIDENCE_CHARS or not any(ch.isalnum() for ch in evidence):
        return None
    squashed = _squash(evidence)
    if not squashed or squashed not in _squash(user_text):
        return None  # not the user's own words: the model may not put an invented or paraphrased line into Core
    if not _numbers(content) <= _numbers(evidence):
        return None  # a figure the quote does not contain (Zero-Hallucination: numbers are the user's, not the model's)
    if looks_secret(content) or looks_secret(evidence):
        return None
    return ParsedFact(category, f"{category}.{_slug(args.get('topic'), content)}", content, evidence)


# ---------------------------------------------------------------- the card and its button

CALLBACK_PREFIX = "sf"
ACK_CALLBACK = "fact_ack"  # "✅ ОК": no payload needed, it only removes the very message it sits under
CALLBACK_RE = re.compile(r"^sf:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):u$")


def callback_data(fact_id: uuid.UUID) -> str:
    return f"{CALLBACK_PREFIX}:{fact_id}:u"  # 40 bytes of Telegram's 64


def parse_callback(data: str) -> uuid.UUID | None:
    match = CALLBACK_RE.match(data)
    return None if match is None else uuid.UUID(match.group(1))


def card(saved: Saved, ttl: float | None = None) -> Card:
    assert saved.fact_id is not None
    template = texts.FACT_CARD_UPDATED if saved.outcome == "updated" else texts.FACT_CARD
    undo = InlineKeyboardButton(text=texts.FACT_UNDO, callback_data=callback_data(saved.fact_id))
    ack = InlineKeyboardButton(text=texts.FACT_ACK, callback_data=ACK_CALLBACK)
    return Card(template.format(content=saved.content), InlineKeyboardMarkup(inline_keyboard=[[undo, ack]]), auto_delete_seconds=ttl)


@dataclass(frozen=True)
class Undone:
    outcome: Literal["undone", "restored", "stale", "gone"]
    content: str = ""
    previous: str = ""  # restored: what the fact says again


def final_text(result: Undone) -> str:
    if result.outcome == "undone":
        return texts.FACT_UNDONE.format(content=result.content)
    if result.outcome == "restored":
        return texts.FACT_RESTORED.format(content=result.content, previous=result.previous)
    return texts.FACT_STALE


def _after(previous: CoreFact | None) -> Any:
    """SQL for the moment a version changes hands: the database clock, but never earlier than the start of the version it closes
    (``valid_to > valid_from`` is a CHECK, and a transaction that started before its predecessor was written would otherwise break it)."""
    if previous is None:
        return func.now()
    return func.greatest(func.now(), previous.valid_from + timedelta(microseconds=1))


class SaveFacts:
    def __init__(self, db: Database, bot: Bot, settings: Settings, janitor: Janitor | None = None) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings
        self.janitor = janitor or Janitor(bot)  # shared with the orchestrator, which schedules the card's self-deletion

    # ------------------------------------------------------------ saving

    async def apply(self, tenant_id: uuid.UUID, message_id: uuid.UUID, user_text: str, calls: Sequence[ToolCall]) -> list[Saved]:
        """One outcome per honoured call, in order. Never raises (except cancellation): a malformed call, a duplicate, a database failure
        and a timeout all produce no outcome (logged), so the reply still goes out and nothing is claimed about a fact that is not stored."""
        outcomes: list[Saved] = []
        seen: set[str] = set()
        considered = 0
        for call in calls:
            if call.name != TOOL_NAME:
                continue
            considered += 1
            if considered > self.settings.save_fact_max_calls:
                log.info("save_fact: more than %d in one message; the rest are ignored", self.settings.save_fact_max_calls)
                break
            try:
                parsed = parse_fact(call.args, user_text)
            except Exception:  # noqa: BLE001 - a malformed call must not break the reply
                log.warning("save_fact: unreadable arguments %r", call.args, exc_info=True)
                parsed = None
            if parsed is None:
                log.info("save_fact: dropped a call with unusable arguments %r", call.args)
                continue
            if parsed.key in seen:
                continue
            seen.add(parsed.key)
            try:
                saved = await asyncio.wait_for(self._save(tenant_id, message_id, parsed), timeout=self.settings.save_fact_timeout_seconds)
            except Exception:  # noqa: BLE001 - no fact, no card: the reply still goes out
                log.warning("save_fact: could not save %r for tenant %s", parsed.key, tenant_id, exc_info=True)
                continue
            outcomes.append(saved)
        return outcomes

    async def _save(self, tenant_id: uuid.UUID, message_id: uuid.UUID, fact: ParsedFact) -> Saved:
        """ONE transaction. Two parallel saves of one fact are ordered by an advisory lock on (tenant, key) - there may be no row to lock yet -
        and the loser sees what the winner wrote; the closing and the new version share ONE timestamp (a version never overlaps its successor)."""
        async with self.db.tenant_session(tenant_id) as session:
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"save_fact:{tenant_id}:{fact.key}"})
            old = (
                await session.execute(select(CoreFact).where(CoreFact.key == fact.key, CoreFact.valid_to.is_(None)).with_for_update())
            ).scalar_one_or_none()
            if old is not None and old.value_text is not None and _norm(old.value_text) == _norm(fact.content):
                return Saved("unchanged", fact.content, old.id)
            if old is not None and old.priority > INLINE_PRIORITY:
                return Saved("protected", fact.content)  # only the Mini App / the quarantine changes a fact the user ranked higher
            recent = (
                await session.execute(
                    select(func.count())
                    .select_from(CoreFact)
                    .where(CoreFact.source == FactSource.chat_inline, CoreFact.valid_from > func.now() - timedelta(hours=24))
                )
            ).scalar_one()
            if recent >= self.settings.save_fact_max_per_day:
                log.info("save_fact: tenant %s reached %d saved facts in 24 h", tenant_id, recent)
                return Saved("limit", fact.content)
            moment = (await session.execute(select(_after(old)))).scalar_one()
            if old is not None:
                await session.execute(update(CoreFact).where(CoreFact.id == old.id).values(valid_to=moment))
            new = CoreFact(
                user_id=tenant_id,
                persona_id=None,
                category=CATEGORIES[fact.category],
                key=fact.key,
                value_text=fact.content,
                priority=INLINE_PRIORITY,
                source=FactSource.chat_inline,
                source_message_id=message_id,
                valid_from=moment,
            )
            session.add(new)
            await session.flush()
            return Saved("saved" if old is None else "updated", fact.content, new.id)

    # ------------------------------------------------------------ undo

    async def undo(self, tenant_id: uuid.UUID, fact_id: uuid.UUID) -> Undone:
        """ONE transaction: the same advisory lock on (tenant, key) as a save, then ``FOR UPDATE`` of the row. Raises on a database failure
        (nothing was changed then)."""
        async with self.db.tenant_session(tenant_id) as session:
            key = (
                await session.execute(select(CoreFact.key).where(CoreFact.id == fact_id, CoreFact.source == FactSource.chat_inline))
            ).scalar_one_or_none()
            if key is None:  # not there, or not a fact the chat wrote: a forged button can reach nothing else
                return Undone("gone")
            await session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"save_fact:{tenant_id}:{key}"})
            row = (
                await session.execute(
                    select(CoreFact).where(CoreFact.id == fact_id, CoreFact.source == FactSource.chat_inline).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:  # not there, or not a fact the chat wrote: a forged button can reach nothing else
                return Undone("gone")
            content = row.value_text or ""
            if row.valid_to is not None:  # already undone, or the fact has moved on since
                return Undone("stale", content)
            moment = (await session.execute(select(_after(row)))).scalar_one()
            await session.execute(update(CoreFact).where(CoreFact.id == row.id).values(valid_to=moment))
            previous = (
                await session.execute(
                    select(CoreFact)
                    .where(CoreFact.key == row.key, CoreFact.valid_to == row.valid_from, CoreFact.id != row.id)
                    .order_by(CoreFact.valid_from.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if previous is None:
                return Undone("undone", content)
            session.add(  # versions are immutable: "the old value again" is a new version that copies the old one
                CoreFact(
                    user_id=previous.user_id,
                    persona_id=previous.persona_id,
                    category=previous.category,
                    key=previous.key,
                    value_text=previous.value_text,
                    value_num=previous.value_num,
                    value_unit=previous.value_unit,
                    value_json=previous.value_json,
                    priority=previous.priority,
                    source=previous.source,
                    source_pending_id=previous.source_pending_id,
                    source_message_id=previous.source_message_id,
                    valid_from=moment,
                )
            )
            await session.flush()
            return Undone("restored", content, previous.value_text or "")

    async def on_callback(self, tenant: Any, query: CallbackQuery) -> None:
        fact_id = parse_callback(query.data or "")
        if fact_id is None:
            await self._answer(query, texts.PROPOSAL_UNKNOWN_BUTTON)
            return
        message = query.message
        if message is not None:
            self.janitor.cancel(message.chat.id, message.message_id)  # the user answered the card: it must not vanish under the confirmation
        result = await self.undo(tenant.id, fact_id)
        await self._answer(query, texts.FACT_TOAST[result.outcome])
        if message is None:
            return
        try:
            await self.bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id, text=final_text(result))
        except TelegramAPIError:  # includes "message is not modified" (a double click); the decision is already made
            log.info("could not edit the memory card of tenant %s", tenant.id, exc_info=True)

    async def on_ack(self, tenant: Any, query: CallbackQuery) -> None:
        """"✅ ОК": the card is read, remove it at once. Only the message the button sits under is touched (no id travels in the payload). If Telegram
        will not delete it (too old), the buttons are taken off instead, so the press is never a dead end."""
        await self._answer(query, None)
        message = query.message
        if message is None:
            return
        if await self.janitor.delete_now(message.chat.id, message.message_id):
            return
        try:
            await self.bot.edit_message_reply_markup(chat_id=message.chat.id, message_id=message.message_id, reply_markup=None)
        except TelegramAPIError:
            log.info("could not clear the buttons of the memory card of tenant %s", tenant.id, exc_info=True)

    @staticmethod
    async def _answer(query: CallbackQuery, text_: str | None) -> None:
        try:
            await query.answer(text_)
        except TelegramAPIError:
            log.warning("could not answer a callback query", exc_info=True)


# ---------------------------------------------------------------- the tool

_NOT_SAVED_TEXT = {"protected": texts.FACT_PROTECTED, "limit": texts.FACT_LIMIT}


class SaveFactTool:
    """Offered in every reply to a message of the user (not in the interview, not in proactive messages: they do not go through ``_answer``)."""

    name = "save_fact"
    functions = frozenset({TOOL_NAME})
    offer_rank = 40

    def __init__(self, engine: SaveFacts, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        turn = ctx.turn
        if not self.settings.save_fact_enabled or turn is None or len(turn.text.strip()) < self.settings.save_fact_min_chars:
            return None  # "ok", "да": nothing lasting can be said in so few characters

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            outcomes = await self.engine.apply(ctx.tenant.id, turn.message_id, turn.text, calls)
            acted = [s for s in outcomes if s.outcome != "unchanged"]  # the same fact said again: nothing happened, nothing to announce
            if not acted:
                return None
            refusals: list[str] = []
            for outcome in acted:
                text = _NOT_SAVED_TEXT.get(outcome.outcome)
                if text is not None and text not in refusals:
                    refusals.append(text)
            return ToolResult(
                notes=(render_note(acted),),
                phrase=True,
                text="\n\n".join(refusals) or None,
                cards=tuple(card(s, self.settings.save_fact_card_ttl_seconds) for s in acted if s.outcome in ("saved", "updated")),
            )

        return Offered((TOOL,), INSTRUCTIONS, run, run_rank=40)

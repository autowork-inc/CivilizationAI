"""Deleting a forum topic (v2.0, PR 5.1): ``delete_topic(code)``.

Deleting a topic wipes every message in it from Telegram for good, so the division of labour here is stricter than for the other functions:
  * the function is OFFERED only when the user's own words ask to remove a topic (``wants_delete``: "удали тему", "убери топик", "delete the topic") and the
    user has topics the bot made (Settings and the feedback channel are system topics: never listed, never deletable);
  * the model only NAMES the topic (a ``code`` from the list it is shown; anything else is dropped by code) - it never deletes anything;
  * what the user gets is a card written by code with two buttons (``dt:<persona id>:y|n``); ONLY the user's press calls ``bot.delete_forum_topic``.
    Whatever the model wrote next to the call is not sent, and it is told that nothing is deleted yet;
  * after Telegram agreed, the persona is retired (``personas.retire``: inactive, no thread id) in one transaction; its messages stay in the history.
    A refusal of Telegram is a RESULT (no rights / already gone / failed), never an exception: nothing is changed in our records on a refusal, except that a topic
    that Telegram says does not exist any more is retired too.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ai_civ.bot import personas, texts
from ai_civ.bot.tools.base import Card, Offered, ToolContext, ToolResult
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.memory.llm import ToolCall, ToolSpec

log = logging.getLogger(__name__)

TOOL_NAME = "delete_topic"
CALLBACK_PREFIX = "dt"
CALLBACK_RE = re.compile(rf"^{CALLBACK_PREFIX}:([0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}):([yn])$")
MAX_LISTED = 20

# "удали тему", "убери этот топик", "закрой тему про спорт", "delete the topic": a verb of removal, up to three words, a noun for the topic.
_WANTS = re.compile(
    r"\b(?:удал\w*|убер\w*|убр\w*|закро\w*|закрыв\w*|удалить|delete|remove|close|drop)\s+(?:[\w«»\"']+\s+){0,3}(?:топик\w*|тем[уаыеой]\b|тред\w*|topic|thread)",
    re.IGNORECASE,
)

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Ask to delete one of the topics (threads of the forum) that the system made earlier, because the user has just asked for it. Pass the code of the topic "
        "from the list. This does NOT delete anything: the system shows the user a confirmation, and only their button deletes the topic. Call it only when "
        "it is clear which topic they mean; otherwise ask them."
    ),
    parameters={
        "type": "object",
        "properties": {"code": {"type": "string", "description": "The code of the topic to delete, exactly as in the list."}},
        "required": ["code"],
    },
)


def wants_delete(text: str) -> bool:
    return _WANTS.search(text) is not None


def _line(value: str) -> str:
    return " ".join(value.split()).replace('"', "'")


def render_section(candidates: Sequence[personas.PersonaSpec]) -> str:
    lines = "\n".join(f'- {spec.code}: "{_line(spec.title)}"' for spec in candidates[:MAX_LISTED])
    return (
        "## Topic deletion\n"
        "The user asked to remove a topic. Their topics, as code: title:\n"
        f"{lines}\n"
        "If it is clear which one they mean, call delete_topic with its code. The SYSTEM then asks them to confirm; nothing is deleted until they press the\n"
        "button, so never say it was deleted. If it is not clear, do not call the function: ask which topic they mean."
    )


def render_note(title: str) -> str:
    return (
        "## Topic deletion (made by the system)\n"
        f'The system shows the user a confirmation card for deleting the topic "{_line(title)}". Nothing is deleted yet. Do not say that it was deleted\n'
        "and do not repeat the question the card asks; just answer the user's message as usual in a short phrase."
    )


def parse_code(calls: Sequence[ToolCall], known: dict[str, personas.PersonaSpec]) -> str | None:
    """The code of the first valid call that names one of the offered topics, else None (unusable calls are dropped silently)."""
    for call in calls:
        if call.name != TOOL_NAME:
            continue
        code = call.args.get("code")
        if isinstance(code, str) and code.strip().lower() in known:
            return code.strip().lower()
    return None


def callback_data(persona_id: uuid.UUID, yes: bool) -> str:
    return f"{CALLBACK_PREFIX}:{persona_id}:{'y' if yes else 'n'}"  # 3 + 36 + 2 = 41 bytes of Telegram's 64


def parse_callback(data: str) -> tuple[uuid.UUID, bool] | None:
    match = CALLBACK_RE.match(data)
    return None if match is None else (uuid.UUID(match.group(1)), match.group(2) == "y")


def ask_markup(persona_id: uuid.UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.TOPIC_DELETE_YES, callback_data=callback_data(persona_id, True)),
                InlineKeyboardButton(text=texts.TOPIC_DELETE_NO, callback_data=callback_data(persona_id, False)),
            ]
        ]
    )


@dataclass(frozen=True)
class Candidate:
    persona_id: uuid.UUID
    code: str
    title: str


@dataclass(frozen=True)
class Decision:
    outcome: str  # deleted | kept | no_rights | failed | gone | unavailable
    title: str = ""


class DeleteTopics:
    def __init__(self, db: Database, bot: Bot, settings: Settings) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings

    async def candidates(self, tenant_id: uuid.UUID) -> list[Candidate]:
        """The topics the model may name: the bot's own (a forum thread is bound), never a system topic."""
        async with self.db.tenant_session(tenant_id) as session:
            rows = await personas.list_active(session)
        return [Candidate(p.id, p.code, p.title) for p in rows if p.code not in personas.SYSTEM_CODES and p.telegram_thread_id is not None]

    async def decide(self, tenant: Any, persona_id: uuid.UUID, yes: bool) -> Decision:
        """The user's answer. ``yes`` calls Telegram (no database transaction is open during the call), then retires the persona in its own short transaction."""
        async with self.db.tenant_session(tenant.id) as session:
            persona = await personas.get_by_id(session, persona_id)
            if persona is None or not persona.is_active or persona.telegram_thread_id is None or persona.code in personas.SYSTEM_CODES:
                return Decision("unavailable")
            title, thread_id = persona.title, persona.telegram_thread_id
        if not yes:
            return Decision("kept", title)
        if tenant.forum_chat_id is None:
            return Decision("failed", title)
        try:
            await self.bot.delete_forum_topic(chat_id=tenant.forum_chat_id, message_thread_id=thread_id)
        except TelegramForbiddenError as exc:
            log.warning("deleting topic %s of tenant %s refused: %s", thread_id, tenant.id, exc)
            return Decision("no_rights", title)
        except TelegramBadRequest as exc:
            low = str(exc).lower().replace("_", " ")
            if "topic id invalid" in low or "thread not found" in low or "topic not found" in low or "topic deleted" in low:
                outcome = "gone"  # somebody deleted it by hand: our record is simply behind
            elif "rights" in low or "administrator" in low or "can manage topics" in low:
                log.warning("deleting topic %s of tenant %s refused for lack of rights: %s", thread_id, tenant.id, exc)
                return Decision("no_rights", title)
            else:
                log.warning("deleting topic %s of tenant %s failed: %s", thread_id, tenant.id, exc)
                return Decision("failed", title)
        except TelegramAPIError:  # a network error, a flood limit: nothing changed, the user can press again
            log.warning("deleting topic %s of tenant %s failed", thread_id, tenant.id, exc_info=True)
            return Decision("failed", title)
        else:
            outcome = "deleted"
        async with self.db.tenant_session(tenant.id) as session:
            await personas.retire(session, persona_id)
        return Decision(outcome, title)

    async def on_callback(self, tenant: Any, query: CallbackQuery) -> None:
        parsed = parse_callback(query.data or "")
        if parsed is None:
            await self._answer(query, texts.PROPOSAL_UNKNOWN_BUTTON)
            return
        persona_id, yes = parsed
        result = await self.decide(tenant, persona_id, yes)
        await self._answer(query, texts.TOPIC_DELETE_TOASTS[result.outcome])
        message = query.message
        final = {
            "deleted": texts.TOPIC_DELETED,
            "kept": texts.TOPIC_DELETE_KEPT,
            "gone": texts.TOPIC_DELETE_GONE,
            "no_rights": texts.TOPIC_DELETE_NO_RIGHTS,
            "failed": texts.TOPIC_DELETE_FAILED,
        }.get(result.outcome)
        if message is None or final is None or result.outcome in ("failed", "no_rights"):
            return  # the buttons stay when nothing happened: the user may press again
        try:
            await self.bot.edit_message_text(
                chat_id=message.chat.id, message_id=message.message_id, text=final.format(title=result.title), reply_markup=None
            )
        except TelegramAPIError:  # the card lived in the deleted topic, or "message is not modified": the decision is already made
            log.info("could not edit the topic-deletion card of tenant %s", tenant.id, exc_info=True)

    @staticmethod
    async def _answer(query: CallbackQuery, text_: str) -> None:
        try:
            await query.answer(text_)
        except TelegramAPIError:
            log.warning("could not answer a callback query", exc_info=True)


class DeleteTopicTool:
    """Offered only when the user's own words ask to remove a topic and there is a topic of the bot's own to remove."""

    name = "delete_topic"
    functions = frozenset({TOOL_NAME})
    offer_rank = 36

    def __init__(self, engine: DeleteTopics, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        turn = ctx.turn
        if not self.settings.delete_topic_enabled or turn is None or not wants_delete(turn.text):
            return None
        found = await self.engine.candidates(ctx.tenant.id)
        if not found:
            return None
        known = {c.code: c for c in found}
        specs = {c.code: personas.PersonaSpec(c.code, c.title, "") for c in found}

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            code = parse_code(calls, specs)
            if code is None:
                return None
            chosen = known[code]
            card = Card(texts.TOPIC_DELETE_ASK.format(title=chosen.title), ask_markup(chosen.persona_id))
            return ToolResult(notes=(render_note(chosen.title),), phrase=True, cards=(card,))

        return Offered((TOOL,), render_section(list(specs.values())), run, run_rank=36)

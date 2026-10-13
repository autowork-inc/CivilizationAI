"""Quick events: the user says what they are doing or about to do ("я сейчас иду 2 часа смотреть стенку") and the bot OFFERS to put it into
the calendar. Nothing is written to ``civ.events`` until the user presses the button.

Same division of labour as the Diplomat. The model proposes one function, ``create_event(title, start, duration_minutes)``; the code
parses and validates every argument; a valid call becomes a row in ``civ.event_drafts`` (migration 0008) and a card with two buttons,
``qe:<draft uuid>:y|n``, that CODE sends under the model's conversational reply. The model is told that the event is NOT saved; the card is
the only thing that says "put into the calendar?", and the button the only thing that does it.

``start`` is the user's wall clock (``YYYY-MM-DDTHH:MM``) or the word ``now``; the model never produces an instant, and "now" is OUR clock.
A press = ONE transaction under ``FOR UPDATE`` of the draft: the draft becomes an ``events`` row (``created_via='bot_confirmed'``, visibility
``busy_only``: it is exactly what a friend's avatar reads as busy time) and is marked confirmed. A second click, or a click after the card
went stale, shows what was decided and never creates a second event.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ.bot import diplomat, texts
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import DraftStatus, EventSource
from ai_civ.db.models import Event, EventDraft, TaskEvent
from ai_civ.memory.llm import ToolCall, ToolSpec

if TYPE_CHECKING:  # the orchestrator imports this module, so the reverse import is for type checkers only
    from ai_civ.bot.orchestrator import TenantInfo

log = logging.getLogger(__name__)

TOOL_NAME = "create_event"
NOW_WORD = "now"
MAX_PAST = timedelta(hours=12)  # a plan that began a while ago and is still going on ("I have been at it since 19:00") is fine

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Offer to put the USER'S OWN plan into their calendar. Call it when the user says they ARE DOING or WILL DO something at a "
        "specific time or for a duration ('иду на 2 часа смотреть стенку', 'завтра в 10 у меня встреча'). The system shows the user a "
        "card with Yes / No: NOTHING is saved until they press it. Do NOT call it for questions, hypotheticals, things already over, "
        "habits in general, or when there is no time and no duration at all. Never use it for invitations to other people."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": f"What the user is doing, a short phrase in the user's language, at most {diplomat.MAX_TITLE_CHARS} characters."},
            "start": {
                "type": "string",
                "description": "When it starts: the word 'now' if it is going on or starts right now, otherwise the USER's local date and time, "
                "24-hour, format YYYY-MM-DDTHH:MM, no time zone suffix.",
            },
            "duration_minutes": {
                "type": "integer",
                "description": f"How long it lasts, {diplomat.MIN_MINUTES}-{diplomat.MAX_MINUTES} minutes. Default {diplomat.DEFAULT_MINUTES} when the user did not say.",
            },
        },
        "required": ["title", "start"],
    },
)

INSTRUCTIONS = (
    "## Calendar\n"
    "When the user tells you what they are doing or about to do, at a time or for a duration, call create_event: the SYSTEM then shows\n"
    "them a card 'put it into the calendar?' under your reply, and only their button saves it. Reply to the user's message as usual;\n"
    "never say the event was saved, added or recorded (it is not, until they press the button) and do not describe the card."
)


def render_note(drafts: Sequence[Draft], timezone: str) -> str:
    """The section of the prompt for the turn that phrases the reply, once cards have been made."""
    lines = [
        "## Calendar suggestion (made by the system)",
        "The system has offered the user to put this into their calendar, with Yes / No buttons right under your reply. It is NOT saved:",
        "do not say it was saved, added or recorded, do not mention the buttons, just answer the user's message as usual.",
    ]
    lines += [f"- {d.title}, {texts.when_text(d.start, d.end, timezone)}" for d in drafts]
    return "\n".join(lines)


@dataclass(frozen=True)
class QuickEvent:
    title: str
    start: datetime  # aware, UTC
    end: datetime


@dataclass(frozen=True)
class Draft:
    id: uuid.UUID
    title: str
    start: datetime
    end: datetime
    timezone: str


def parse_quick(args: dict[str, Any], timezone: str, now: datetime) -> QuickEvent | None:
    """None = the call is malformed (an unreadable title / time / duration, a plan that is over or beyond a year): it is dropped silently -
    the user did not ask for a calendar entry, so there is nothing to apologise for."""
    title = diplomat.clean_title(args.get("title"))
    minutes = diplomat.parse_minutes(args.get("duration_minutes"))
    raw = args.get("start")
    if isinstance(raw, str) and raw.strip().lower() == NOW_WORD:
        start: datetime | None = now.astimezone(UTC).replace(second=0, microsecond=0)
    else:
        start = diplomat.parse_start(raw, ZoneInfo(timezone))
    if title is None or start is None or minutes is None:
        return None
    end = start + timedelta(minutes=minutes)
    if end < now + diplomat.MIN_LEAD or start < now - MAX_PAST or start > now + diplomat.MAX_LEAD:
        return None
    return QuickEvent(title, start, end)


# ---------------------------------------------------------------- the buttons

CALLBACK_PREFIX = "qe"
CALLBACK_RE = re.compile(r"^qe:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(y|n)$")
_FINAL_TEXT = {
    "confirmed": texts.QUICK_CONFIRMED,
    "already_confirmed": texts.QUICK_CONFIRMED,
    "dismissed": texts.QUICK_DISMISSED,
    "already_dismissed": texts.QUICK_DISMISSED,
    "expired": texts.QUICK_EXPIRED,
}


def callback_data(draft_id: uuid.UUID, confirm: bool) -> str:
    return f"{CALLBACK_PREFIX}:{draft_id}:{'y' if confirm else 'n'}"  # 41 bytes of Telegram's 64


def parse_callback(data: str) -> tuple[uuid.UUID, bool] | None:
    match = CALLBACK_RE.match(data)
    return None if match is None else (uuid.UUID(match.group(1)), match.group(2) == "y")


def card_text(draft: Draft, timezone: str) -> str:
    return texts.QUICK_CARD.format(title=draft.title, when=texts.when_text(draft.start, draft.end, timezone))


def card_markup(draft_id: uuid.UUID) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=texts.QUICK_YES, callback_data=callback_data(draft_id, True)),
        InlineKeyboardButton(text=texts.QUICK_NO, callback_data=callback_data(draft_id, False)),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


@dataclass(frozen=True)
class Decision:
    outcome: str  # confirmed | dismissed | already_confirmed | already_dismissed | expired | gone
    title: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str | None = None


def final_text(decision: Decision) -> str:
    """The card after the press: the state the DATABASE reports, in the zone the draft was made in (the event keeps it too)."""
    template = _FINAL_TEXT.get(decision.outcome)
    if template is None or decision.title is None or decision.starts_at is None or decision.ends_at is None or decision.timezone is None:
        return texts.QUICK_GONE
    return template.format(title=decision.title, when=texts.when_text(decision.starts_at, decision.ends_at, decision.timezone))


class QuickEvents:
    def __init__(self, db: Database, bot: Bot, settings: Settings) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings

    # ------------------------------------------------------------ drafts

    async def draft(
        self,
        tenant_id: uuid.UUID,
        timezone: str,
        persona_id: uuid.UUID | None,
        calls: Sequence[ToolCall],
        *,
        now: datetime | None = None,
    ) -> list[Draft]:
        """The drafts that were actually created for these calls, in order. Never raises (except cancellation): a malformed call, a
        duplicate of an open draft, too many open drafts and a database failure all just produce no card (logged)."""
        now = now or datetime.now(UTC)
        made: list[Draft] = []
        seen: set[tuple[str, datetime]] = set()
        considered = 0
        for call in calls:
            if call.name != TOOL_NAME:
                continue
            considered += 1
            if considered > self.settings.quick_events_max_calls:
                log.info("quick events: more than %d in one message; the rest are ignored", self.settings.quick_events_max_calls)
                break
            try:
                event = parse_quick(call.args, timezone, now)
            except Exception:  # noqa: BLE001 - a malformed call must not break the reply
                log.warning("quick events: unreadable arguments %r", call.args, exc_info=True)
                event = None
            if event is None:
                log.info("quick events: dropped a call with unusable arguments %r", call.args)
                continue
            if (event.title, event.start) in seen:
                continue
            seen.add((event.title, event.start))
            try:
                draft = await asyncio.wait_for(
                    self._insert(tenant_id, timezone, persona_id, event), timeout=self.settings.diplomat_timeout_seconds
                )
            except Exception:  # noqa: BLE001 - no draft, no card: the reply still goes out
                log.warning("quick events: could not save a draft for tenant %s", tenant_id, exc_info=True)
                continue
            if draft is not None:
                made.append(draft)
        return made

    async def _insert(self, tenant_id: uuid.UUID, timezone: str, persona_id: uuid.UUID | None, event: QuickEvent) -> Draft | None:
        s = self.settings
        async with self.db.tenant_session(tenant_id) as session:
            await session.execute(  # an open draft whose time ran out must not block the same plan again
                update(EventDraft)
                .where(EventDraft.status == DraftStatus.pending, EventDraft.expires_at <= func.now())
                .values(status=DraftStatus.expired, decided_at=func.now())
            )
            open_drafts = (
                await session.execute(select(func.count()).select_from(EventDraft).where(EventDraft.status == DraftStatus.pending))
            ).scalar_one()
            if open_drafts >= s.quick_events_max_pending:
                log.info("quick events: tenant %s has %d open drafts; no new one", tenant_id, open_drafts)
                return None
            draft_id = (
                await session.execute(
                    pg_insert(EventDraft)
                    .values(
                        user_id=tenant_id,
                        persona_id=persona_id,
                        title=event.title,
                        starts_at=event.start,
                        ends_at=event.end,
                        timezone=timezone,
                        expires_at=func.now() + timedelta(hours=s.quick_events_ttl_hours),
                    )
                    .on_conflict_do_nothing(
                        index_elements=[EventDraft.user_id, EventDraft.title, EventDraft.starts_at],
                        index_where=text("status = 'pending'"),
                    )
                    .returning(EventDraft.id)
                )
            ).scalar_one_or_none()
        if draft_id is None:  # the same plan is already waiting for its answer: its card is in the chat
            log.info("quick events: %r at %s is already waiting for an answer", event.title, event.start)
            return None
        return Draft(draft_id, event.title, event.start, event.end, timezone)

    # ------------------------------------------------------------ the button

    async def decide(self, tenant_id: uuid.UUID, draft_id: uuid.UUID, confirm: bool) -> Decision:
        """ONE transaction under ``FOR UPDATE``: pending -> confirmed (+ the event) / dismissed / expired. Idempotent. Raises on a database
        failure: nothing was changed then."""
        async with self.db.tenant_session(tenant_id) as session:
            draft = (await session.execute(select(EventDraft).where(EventDraft.id == draft_id).with_for_update())).scalar_one_or_none()
            if draft is None:
                return Decision("gone")
            shown = (draft.title, draft.starts_at, draft.ends_at, draft.timezone)
            if draft.status != DraftStatus.pending:
                return Decision(f"already_{draft.status}" if draft.status in ("confirmed", "dismissed") else "expired", *shown)
            now = (await session.execute(select(func.now()))).scalar_one()  # the database clock, like every expiry here
            if draft.expires_at <= now:
                draft.status, draft.decided_at = DraftStatus.expired, now
                return Decision("expired", *shown)
            if not confirm:
                draft.status, draft.decided_at = DraftStatus.dismissed, now
                return Decision("dismissed", *shown)
            event = Event(
                user_id=tenant_id,
                persona_id=draft.persona_id,
                title=draft.title,
                starts_at=draft.starts_at,
                ends_at=draft.ends_at,
                timezone=draft.timezone,
                created_via=EventSource.bot_confirmed.value,
            )
            session.add(event)
            await session.flush()  # the id of the new event
            draft.status, draft.decided_at, draft.event_id = DraftStatus.confirmed, now, event.id
            if draft.task_id is not None:  # a slot of a task step (bot/tools/decompose.py): the event now belongs to that step
                session.add(TaskEvent(user_id=tenant_id, task_id=draft.task_id, event_id=event.id))
            return Decision("confirmed", *shown)

    async def on_callback(self, tenant: TenantInfo, query: CallbackQuery) -> None:
        parsed = parse_callback(query.data or "")
        if parsed is None:
            await self._answer(query, texts.PROPOSAL_UNKNOWN_BUTTON)
            return
        decision = await self.decide(tenant.id, *parsed)
        await self._answer(query, texts.QUICK_TOAST[decision.outcome])
        message = query.message
        if message is None:
            return
        try:
            await self.bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id, text=final_text(decision))
        except TelegramAPIError:  # includes "message is not modified" (a double click); the decision is already made
            log.info("could not edit the quick-event card of tenant %s", tenant.id, exc_info=True)

    @staticmethod
    async def _answer(query: CallbackQuery, text_: str) -> None:
        try:
            await query.answer(text_)
        except TelegramAPIError:
            log.warning("could not answer a callback query", exc_info=True)

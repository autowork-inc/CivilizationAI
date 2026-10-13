"""Time capsules (v2.0, PR 4.5, migration 0022): ``seal_time_capsule`` and the hourly delivery of the letters that are due.

The division of labour is the one of every tool here (the model proposes, the code decides):
  * the function is OFFERED only when CODE finds an explicit marker in the user's own words ("капсула времени", "письмо в будущее", "напомни через полгода",
    "отправь мне через год", "напомни мне в 2027"): ``wants_capsule``;
  * the DATE is the code's wherever the user's words fix it: a relative offset ("через полгода", "через 2 года") is computed by code from today's local date, an explicit
    date ("1 января 2027", "01.01.2027", "2027-01-01") is read by code; only when the words name neither, the model's date is used - and then a year the user
    wrote ("в 2027") must be the year of that date. The date is turned into an instant in the USER's time zone (a date without a time is delivered at
    ``CAPSULES_DEFAULT_HOUR``). It is NOT a timer: at least ``CAPSULES_MIN_DAYS`` (7) local days ahead, at most 5 years - otherwise the user is told so by a fixed text;
  * the letter must be the user's own words (``geo.grounded`` with a share of 0.7: every number stands in the message, most longer words start like a word of it - a
    greeting to the future self is allowed, an invented plan is not); a title is the model's only if it is grounded too, otherwise "Письмо на <date>";
  * what the user reads is a fixed text written by code with a button «✖️ Отменить капсулу»; the model's words next to the call are never sent.

Delivery (Beat ``ai_civ.dispatch_time_capsules`` hourly at :15 -> ``ai_civ.check_time_capsules``): ``TimeCapsules.claim_due`` takes the tenant's pending capsules with
``deliver_at <= now`` under a lease (``claimed_at``) - never in quiet hours (the delivery waits for the morning), never while onboarding is not done or the group is not
active; the card «⏳ Письмо из прошлого (запечатано <дата>)…» is sent; only then the capsule becomes ``delivered`` (``delivered_at``) and the card is remembered as the
bot's own message (the user's answer sees the question). A failed send gives the lease back and raises (Celery retries); a crash between the send and the mark can
repeat a card after the lease runs out (at-least-once), never lose one.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from dateutil.relativedelta import relativedelta
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ.bot import diplomat, texts
from ai_civ.bot.proactive import in_quiet_hours
from ai_civ.bot.tools.base import Card, Offered, ToolContext, ToolResult, UserTurn
from ai_civ.bot.tools.geo import grounded
from ai_civ.bot.tools.workout import norm_key
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import GroupState, MessageRole, OnboardingState
from ai_civ.db.models import Message, TimeCapsule, User
from ai_civ.memory.llm import ToolCall, ToolSpec

log = logging.getLogger(__name__)

TOOL_NAME = "seal_time_capsule"
MAX_MESSAGE_CHARS = 900
MAX_TITLE_CHARS = 60
GROUNDED_SHARE = 0.7

# ---------------------------------------------------------------- the request check (code, not prompt)

_MARKERS = re.compile(
    r"\bкапсул\w*\W+(?:\w+\W+){0,1}?времени"
    r"|\bписьм\w*\W+(?:\w+\W+){0,3}?(?:в\W+будущее|самому\W+себе|себе\W+в\W+будущее|будущему\W+себе)"
    r"|\b(?:напомни|напиши|отправь|пришли|верни)\w*\W+(?:\w+\W+){0,4}?через\W+(?:\w+\W+){0,1}?(?:полгода|полугода|год\w*|лет|месяц\w*|недел\w*)"
    r"|\b(?:напомни|отправь|пришли)\w*\W+(?:мне\W+)?(?:\w+\W+){0,3}?в\W+20\d\d\b"
    r"|\btime\W+capsule\b|\bletter\W+to\W+(?:my\W+)?(?:future\W+)?(?:self|me)\b",
    re.IGNORECASE,
)


def wants_capsule(message: str) -> bool:
    """True when the USER's own words ask for a letter to the future / a far reminder. ``ё`` is folded into ``е``."""
    return _MARKERS.search(message.replace("ё", "е").replace("Ё", "Е")) is not None


# ---------------------------------------------------------------- the date: the code's wherever the words fix it

_NUMBER_WORDS = {"один": 1, "одну": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10}
_RELATIVE = re.compile(
    r"\bчерез\W+(?:(\d{1,3}|один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять)\W+)?"
    r"(полгода|полугода|годов|года|году|год|лет|месяцев|месяца|месяц|недели|недель|неделю|дней|дня|день)\b",
    re.IGNORECASE,
)
_MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
    "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}  # fmt: skip
_ISO_DATE = re.compile(r"\b(20\d\d)-(\d{2})-(\d{2})\b")
_DOT_DATE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(20\d\d)\b")
_WORD_DATE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(_MONTHS) + r")\s+(20\d\d)\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(20\d\d)\b")


def _calendar(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def relative_date(user_text: str, today: date) -> date | None:
    """"через полгода" / "через 2 года" / "через 3 месяца" / "через 2 недели" / "через 10 дней" -> the date, from today's LOCAL date. The first offset in the words wins."""
    match = _RELATIVE.search(user_text.replace("ё", "е").replace("Ё", "Е"))
    if match is None:
        return None
    raw, unit = match.group(1), match.group(2).lower()
    count = int(raw) if raw and raw.isdigit() else _NUMBER_WORDS.get((raw or "").lower(), 1)
    if count < 1:
        return None
    if unit in ("полгода", "полугода"):
        return today + relativedelta(months=6)
    if unit.startswith("год") or unit == "лет":
        return today + relativedelta(years=count)
    if unit.startswith("месяц"):
        return today + relativedelta(months=count)
    if unit.startswith("недел"):
        return today + timedelta(weeks=count)
    return today + timedelta(days=count)


def explicit_date(user_text: str) -> date | None:
    """A full calendar date the user wrote: ``2027-01-01``, ``01.01.2027`` or ``1 января 2027``."""
    text = user_text.replace("ё", "е").replace("Ё", "Е")
    if (m := _ISO_DATE.search(text)) is not None:
        return _calendar(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if (m := _DOT_DATE.search(text)) is not None:
        return _calendar(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    if (m := _WORD_DATE.search(text)) is not None:
        return _calendar(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    return None


def model_date(value: Any) -> tuple[date, time | None] | None:
    """The model's ``deliver_at``: ``YYYY-MM-DD`` or ``YYYY-MM-DDTHH:MM`` in the USER's local time; a zone suffix is not accepted."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw), None
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return (moment.date(), moment.time().replace(second=0, microsecond=0)) if moment.tzinfo is None else None


# ---------------------------------------------------------------- the function the model sees

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Seal a letter to the user's FUTURE self (a time capsule) because the user asked for one ('капсула времени', 'письмо в будущее', 'напомни через полгода ...', "
        "'отправь мне через год ...', 'напомни мне в 2027 ...'). `message` is what the user wants to hear then, in THEIR words (copy them; a short greeting to the future "
        "self is fine, but add no plans, facts or numbers of your own). `deliver_at` is the local date it must arrive, YYYY-MM-DD (or YYYY-MM-DDTHH:MM), computed from "
        "the current date in the system prompt; it must be at least 7 days and at most 5 years ahead. The SYSTEM seals it and tells the user: nothing else is needed from you."
    ),
    parameters={
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": f"The letter, one short paragraph in the user's language, at most {MAX_MESSAGE_CHARS} characters."},
            "deliver_at": {"type": "string", "description": "The user's LOCAL date of delivery, YYYY-MM-DD (or YYYY-MM-DDTHH:MM), no time zone suffix."},
            "title": {"type": "string", "description": f"OPTIONAL. A short title in the user's words, at most {MAX_TITLE_CHARS} characters."},
        },
        "required": ["message", "deliver_at"],
    },
)

INSTRUCTIONS = (
    "## Time capsule\n"
    "The user wants a letter to their future self or a far-away reminder: call seal_time_capsule ONCE with their own words as the message and the local delivery date.\n"
    "The SYSTEM seals the capsule and shows the user a card with the date and a cancel button. Never say it was sealed, saved or scheduled yourself and do not repeat the\n"
    "letter: reply briefly (one or two sentences), for example something warm about writing to their future self."
)


def render_note(sealed: Sealed) -> str:
    return "\n".join(
        [
            "## Time capsule (made by the system)",
            "The system has sealed the letter and shows the user the date and a cancel button in a card right under your reply.",
            "Do not repeat the letter, do not say it was sealed or scheduled: reply briefly and warmly.",
            f"Opens: {sealed.when}",
        ]
    )


# ---------------------------------------------------------------- validating a call


@dataclass(frozen=True)
class CapsuleSpec:
    message: str
    title: str
    deliver_at: datetime  # aware, UTC
    when: str  # the local delivery date as the user reads it (dd.mm.yyyy)


@dataclass(frozen=True)
class Refused:
    reason: str  # too_soon | too_far | limit


def _line(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    line = " ".join(value.split())
    if not line or len(line) > limit or re.search(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩﻿]", line):
        return None
    return line


def date_text(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def parse_capsule(args: dict[str, Any], user_text: str, tz: ZoneInfo, now: datetime, settings: Settings) -> CapsuleSpec | Refused | None:
    """None = the call is unusable and dropped silently (an unreadable or unfounded letter, no usable date); ``Refused`` = the user asked for it but the date is outside
    the window (they are told so by a fixed text)."""
    message = _line(args.get("message"), MAX_MESSAGE_CHARS)
    if message is None or not any(ch.isalpha() for ch in message) or len(message.split()) < 2 or not grounded(message, user_text, GROUNDED_SHARE):
        return None
    today = now.astimezone(tz).date()
    local_time: time | None = None
    day = relative_date(user_text, today) or explicit_date(user_text)
    if day is None:  # the words fix no date: the model's date, and a year the user wrote must be its year
        proposed = model_date(args.get("deliver_at"))
        if proposed is None:
            return None
        day, local_time = proposed
        years = {int(y) for y in _YEAR.findall(user_text)}
        if years and day.year not in years:
            return None
    if (day - today).days < settings.capsules_min_days:
        return Refused("too_soon")
    if day > today + relativedelta(years=settings.capsules_max_years):
        return Refused("too_far")
    deliver_at = datetime.combine(day, local_time or time(settings.capsules_default_hour), tzinfo=tz).astimezone(UTC)
    title = _line(args.get("title"), MAX_TITLE_CHARS)
    if title is None or diplomat.clean_title(title) is None or not grounded(title, user_text, GROUNDED_SHARE):
        title = f"Письмо на {date_text(day)}"
    return CapsuleSpec(message, title, deliver_at, date_text(day))


# ---------------------------------------------------------------- the cards and the button

CALLBACK_PREFIX = "tc"
CALLBACK_RE = re.compile(r"^tc:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):x$")


def callback_data(capsule_id: uuid.UUID) -> str:
    return f"{CALLBACK_PREFIX}:{capsule_id}:x"  # 41 bytes of Telegram's 64


def parse_callback(data: str) -> uuid.UUID | None:
    match = CALLBACK_RE.match(data)
    return None if match is None else uuid.UUID(match.group(1))


def sealed_text(sealed: Sealed) -> str:
    return texts.CAPSULE_SEALED.format(when=sealed.when, message=sealed.message)


def sealed_markup(capsule_id: uuid.UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=texts.CAPSULE_CANCEL_BUTTON, callback_data=callback_data(capsule_id))]])


def delivered_text(message: str, sealed_at: datetime, tz: ZoneInfo) -> str:
    return texts.CAPSULE_DELIVERED.format(sealed=date_text(sealed_at.astimezone(tz).date()), message=message)


# ---------------------------------------------------------------- the engine


@dataclass(frozen=True)
class Sealed:
    capsule_id: uuid.UUID
    message: str
    when: str


@dataclass(frozen=True)
class Cancelled:
    outcome: str  # canceled | settled | delivered | gone


@dataclass(frozen=True)
class DueCapsule:
    capsule_id: uuid.UUID
    message: str
    created_at: datetime
    previous_claimed_at: datetime | None


@dataclass(frozen=True)
class Claim:
    chat_id: int
    timezone: str
    capsules: tuple[DueCapsule, ...]
    claimed_at: datetime


class TimeCapsules:
    def __init__(self, db: Database, bot: Bot, settings: Settings) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings

    # ------------------------------------------------------------ sealing

    async def seal(
        self, tenant_id: uuid.UUID, timezone: str, turn: UserTurn, calls: Sequence[ToolCall], *, now: datetime | None = None
    ) -> Sealed | Refused | None:
        """The result for the FIRST usable call (one capsule per message), or None (no usable call, a delivered/canceled replay, a database failure). Never raises
        (except cancellation). A replayed message gets its own pending capsule back (the card of a first attempt that died before it was sent), never a second one."""
        now = now or datetime.now(UTC)
        tz = ZoneInfo(timezone)
        for call in calls:
            if call.name != TOOL_NAME:
                continue
            try:
                parsed = parse_capsule(call.args, turn.text, tz, now, self.settings)
            except Exception:  # noqa: BLE001 - a malformed call must not break the reply
                log.warning("seal_time_capsule: unreadable arguments %r", call.args, exc_info=True)
                parsed = None
            if parsed is None:
                log.info("seal_time_capsule: dropped a call with unusable arguments")
                continue
            if isinstance(parsed, Refused):
                return parsed
            try:
                return await asyncio.wait_for(self._insert(tenant_id, tz, turn, parsed), timeout=self.settings.capsules_timeout_seconds)
            except Exception:  # noqa: BLE001 - no capsule, no card: the reply still goes out
                log.warning("seal_time_capsule: could not seal the capsule of tenant %s", tenant_id, exc_info=True)
                return None
        return None

    async def _insert(self, tenant_id: uuid.UUID, tz: ZoneInfo, turn: UserTurn, spec: CapsuleSpec) -> Sealed | Refused | None:
        async with self.db.tenant_session(tenant_id) as session:
            telegram_id = (await session.execute(select(Message.telegram_message_id).where(Message.id == turn.message_id))).scalar_one_or_none()
            if telegram_id is not None:
                earlier = (await session.execute(select(TimeCapsule).where(TimeCapsule.source_message_id == telegram_id))).scalar_one_or_none()
                if earlier is not None:
                    return Sealed(earlier.id, earlier.message, date_text(earlier.deliver_at.astimezone(tz).date())) if earlier.status == "pending" else None
            pending = (await session.execute(select(func.count()).select_from(TimeCapsule).where(TimeCapsule.status == "pending"))).scalar_one()
            today = (
                await session.execute(select(func.count()).select_from(TimeCapsule).where(TimeCapsule.created_at > func.now() - timedelta(hours=24)))
            ).scalar_one()
            if pending >= self.settings.capsules_max_pending or today >= self.settings.capsules_max_per_day:
                return Refused("limit")
            capsule_id = (
                await session.execute(
                    pg_insert(TimeCapsule)
                    .values(user_id=tenant_id, title=spec.title, message=spec.message, deliver_at=spec.deliver_at, source_message_id=telegram_id)
                    .on_conflict_do_nothing(index_elements=[TimeCapsule.user_id, TimeCapsule.source_message_id], index_where=TimeCapsule.source_message_id.is_not(None))
                    .returning(TimeCapsule.id)
                )
            ).scalar_one_or_none()
            return None if capsule_id is None else Sealed(capsule_id, spec.message, spec.when)

    # ------------------------------------------------------------ cancelling

    async def cancel(self, tenant_id: uuid.UUID, capsule_id: uuid.UUID) -> Cancelled:
        """ONE transaction under ``FOR UPDATE``: ``pending -> canceled`` (idempotent). Raises on a database failure: nothing was changed then."""
        async with self.db.tenant_session(tenant_id) as session:
            row = (await session.execute(select(TimeCapsule).where(TimeCapsule.id == capsule_id).with_for_update())).scalar_one_or_none()
            if row is None:
                return Cancelled("gone")
            if row.status == "delivered":
                return Cancelled("delivered")
            if row.status == "canceled":
                return Cancelled("settled")
            row.status = "canceled"
            return Cancelled("canceled")

    async def on_callback(self, tenant: Any, query: CallbackQuery) -> None:
        capsule_id = parse_callback(query.data or "")
        if capsule_id is None:
            await self._answer(query, texts.PROPOSAL_UNKNOWN_BUTTON)
            return
        result = await self.cancel(tenant.id, capsule_id)
        await self._answer(query, texts.CAPSULE_TOAST[result.outcome])
        message = query.message
        if message is None or result.outcome not in ("canceled", "settled"):
            return
        try:
            await self.bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id, text=texts.CAPSULE_CANCELED, reply_markup=None)
        except TelegramAPIError:  # includes "message is not modified" (a double click); the decision is already made
            log.info("could not edit the capsule card of tenant %s", tenant.id, exc_info=True)

    @staticmethod
    async def _answer(query: CallbackQuery, text_: str) -> None:
        try:
            await query.answer(text_)
        except TelegramAPIError:
            log.warning("could not answer a callback query", exc_info=True)

    # ------------------------------------------------------------ delivery

    async def claim_due(self, tenant_id: uuid.UUID, *, now: datetime | None = None) -> Claim | None:
        """The capsules to deliver RIGHT NOW, claimed under a lease in ONE transaction under ``FOR UPDATE`` of the tenant's row. Nothing while onboarding is not done,
        the group is not active or the local clock is inside quiet hours (the delivery waits for the morning); at most ``CAPSULES_PER_TICK`` per run, the oldest first."""
        s = self.settings
        now = now or datetime.now(UTC)
        async with self.db.tenant_session(tenant_id) as session:
            user = (
                await session.execute(
                    select(User.timezone, User.onboarding_state, User.group_state, User.quiet_hours_start, User.quiet_hours_end, User.forum_chat_id)
                    .where(User.id == tenant_id)
                    .with_for_update()
                )
            ).one_or_none()
            if user is None or user.forum_chat_id is None:
                return None
            if OnboardingState(user.onboarding_state) is not OnboardingState.done or GroupState(user.group_state) is not GroupState.active:
                return None
            if in_quiet_hours(now.astimezone(ZoneInfo(user.timezone)).time(), user.quiet_hours_start, user.quiet_hours_end):
                return None
            rows = (
                await session.execute(
                    select(TimeCapsule)
                    .where(
                        TimeCapsule.status == "pending",
                        TimeCapsule.deliver_at <= now,
                        (TimeCapsule.claimed_at.is_(None)) | (TimeCapsule.claimed_at < now - timedelta(minutes=s.capsules_lease_minutes)),
                    )
                    .order_by(TimeCapsule.deliver_at, TimeCapsule.id)
                    .limit(s.capsules_per_tick)
                    .with_for_update()
                )
            ).scalars().all()
            if not rows:
                return None
            due = tuple(DueCapsule(r.id, r.message, r.created_at, r.claimed_at) for r in rows)
            for row in rows:
                row.claimed_at = now
            return Claim(user.forum_chat_id, user.timezone, due, now)

    async def release(self, tenant_id: uuid.UUID, claim: Claim, capsules: Sequence[DueCapsule]) -> None:
        """Give leases back (the send failed): the capsule is due again on the next tick. Only a capsule that is still pending and still under OUR lease is touched."""
        async with self.db.tenant_session(tenant_id) as session:
            for capsule in capsules:
                row = (await session.execute(select(TimeCapsule).where(TimeCapsule.id == capsule.capsule_id).with_for_update())).scalar_one_or_none()
                if row is not None and row.status == "pending" and row.claimed_at == claim.claimed_at:
                    row.claimed_at = capsule.previous_claimed_at

    async def _delivered(self, tenant_id: uuid.UUID, capsule: DueCapsule, text_: str, telegram_id: int, now: datetime) -> None:
        async with self.db.tenant_session(tenant_id) as session:
            row = (await session.execute(select(TimeCapsule).where(TimeCapsule.id == capsule.capsule_id).with_for_update())).scalar_one_or_none()
            if row is not None and row.status == "pending":
                row.status, row.delivered_at = "delivered", now
            await session.execute(  # the card is the bot's own message: the answer of the user finds its question in the context
                pg_insert(Message)
                .values(user_id=tenant_id, persona_id=None, role=MessageRole.assistant, content=text_, telegram_message_id=telegram_id)
                .on_conflict_do_nothing(constraint="uq_messages_user_telegram")
            )

    async def send_due(self, tenant_id: uuid.UUID, *, now: datetime | None = None) -> int:
        """Claim, send, mark. Fail-fast: the first failed send gives back its lease and the ones after it, then raises (the Celery task retries); the letters already
        delivered stay delivered."""
        now = now or datetime.now(UTC)
        claim = await self.claim_due(tenant_id, now=now)
        if claim is None:
            return 0
        tz = ZoneInfo(claim.timezone)
        for index, capsule in enumerate(claim.capsules):
            text_ = delivered_text(capsule.message, capsule.created_at, tz)
            try:
                sent = await self.bot.send_message(chat_id=claim.chat_id, text=text_)
            except BaseException:
                await self.release(tenant_id, claim, claim.capsules[index:])
                raise
            try:
                await self._delivered(tenant_id, capsule, text_, sent.message_id, now)
            except BaseException:  # sent but not marked: this one repeats after its lease; the rest are given back now
                await self.release(tenant_id, claim, claim.capsules[index + 1 :])
                raise
        return len(claim.capsules)


# ---------------------------------------------------------------- the tool


class CapsuleTool:
    """Offered only when the user's own words ask for a letter to the future or a far reminder (``wants_capsule``). Never in the interview or in proactive messages,
    never on a message with an attachment (those replies have no tools at all)."""

    name = "time_capsule"
    functions = frozenset({TOOL_NAME})
    offer_rank = 51

    def __init__(self, engine: TimeCapsules, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        turn = ctx.turn
        if not self.settings.capsules_enabled or turn is None or not wants_capsule(turn.text):
            return None
        tenant = ctx.tenant

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            done = await self.engine.seal(tenant.id, tenant.timezone, turn, calls)
            if done is None:
                return None
            if isinstance(done, Refused):
                fixed = {
                    "too_soon": texts.CAPSULE_TOO_SOON.format(n=self.settings.capsules_min_days),
                    "too_far": texts.CAPSULE_TOO_FAR.format(n=self.settings.capsules_max_years),
                }.get(done.reason, texts.CAPSULE_LIMIT)
                return ToolResult(text=fixed)
            return ToolResult(notes=(render_note(done),), phrase=True, cards=(Card(sealed_text(done), sealed_markup(done.capsule_id)),))

        return Offered((TOOL,), INSTRUCTIONS, run, run_rank=51)

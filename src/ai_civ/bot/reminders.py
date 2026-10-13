"""Scheduled reminders, wake-up alarms and persistent pings (v2.0, PR 5.4): the bot writes FIRST, on a schedule the user asked for.

Flow
  * The model's function ``schedule_reminder(delay_seconds, text, reminder_type, repeat_until_ack)`` is OFFERED only when the user's own words ask for something of the
    kind (``wants_reminder``: "напомни", "разбуди", "будильник", "пингани", ...). CODE validates every argument (a delay inside ``REMINDERS_MIN/MAX_DELAY_SECONDS``, a clean one-line
    text, a known type), writes the state to Redis, and puts a Celery task with a countdown (``send_scheduled_message.apply_async``). What the user reads is a CARD written by code
    that says the exact LOCAL time the message will come ("завтра в 07:30") with a cancel button: the model's own words next to the call are never sent, so a wrongly counted
    delay is visible to the user at once, and "it is set" is never a claim of the model.
  * ``deliver`` (the Celery task body) sends the message. An alarm (``reminder_type == "alarm"``) or any reminder with ``repeat_until_ack`` carries the button
    "✅ Я проснулся / На месте" and repeats until it is acknowledged: an ALARM every 180 seconds (``REMINDERS_ALARM_REPEAT_SECONDS``) at most 10 times (``REMINDERS_ALARM_MAX_REPEATS``), a persistent ping every 2-3 minutes (``REMINDERS_REPEAT_*``) at most 5 times: by the button, or by the
    user writing "я встал" / "я тут" (``ack_by_text``). The state lives in Redis (``civ:rem:*``), the same Redis as the broker.
  * Duplicates are harmless: a delayed task whose countdown is longer than the broker's visibility timeout may be delivered twice, so every send first claims
    ``sent:<token>:<attempt>`` with ``SET NX``; the second delivery finds the claim and does nothing. A failed send gives the claim back and raises (Celery retries, bounded).

Fail-fast: no loops around network calls. Redis down while scheduling = the user is told (a fixed text) and nothing is scheduled; down while acknowledging by text = ignored (fail-open,
the next message or the button works); down inside the task = the task raises and Celery retries it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message as TgMessage
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ.bot import personas, texts
from ai_civ.bot.tools.base import Card, Offered, ToolContext, ToolResult
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import GroupState, MessageRole, OnboardingState
from ai_civ.db.models import Message, User
from ai_civ.memory.llm import ToolCall, ToolSpec

log = logging.getLogger(__name__)

TOOL_NAME = "schedule_reminder"
CALLBACK_PREFIX = "rm"
CALLBACK_RE = re.compile(rf"^{CALLBACK_PREFIX}:([0-9a-f]{{32}}):([ax])$")
KINDS = ("alarm", "checkin", "ping")

# ---------------------------------------------------------------- the request check (code, not prompt)

_WANTS = re.compile(
    r"\b(?:напомни\w*|напомин\w*|разбуд\w*|разбуж\w*|будильник\w*|буди\b|пингани\w*|пинг\b|подними\s+меня|"
    r"remind\w*|wake\s+me|alarm|ping\s+me|set\s+a\s+(?:timer|reminder))",
    re.IGNORECASE,
)
_ACK = re.compile(
    r"\b(?:я\s+(?:уже\s+|вот\s+)?(?:встал[аи]?|проснул\w+|тут|здесь|на\s+месте|на\s+связи|не\s+сплю)|"
    r"проснул\w+|встал[аи]?|(?:i'?m|i\s+am)\s+(?:up|here|awake)|awake)\b",
    re.IGNORECASE,
)


# An explicit request to be WOKEN UP. It is an alarm that repeats until the user answers: whatever type the model chose (a passive "checkin" is not what was asked), CODE makes it one.
_WAKE = re.compile(r"\b(?:разбуд\w*|разбуж\w*|будильник\w*|буди\b|подними\s+меня|wake\s+me|wake-?up\s+call)", re.IGNORECASE)


def wants_reminder(text: str) -> bool:
    return _WANTS.search(text) is not None


def wants_wakeup(text: str) -> bool:
    return _WAKE.search(text) is not None


def is_ack_text(text: str) -> bool:
    """"я встал" / "я тут": the user says they are there. Only consulted while the user has an alarm that is waiting for an answer."""
    return len(text) <= 120 and _ACK.search(text) is not None


TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Schedule a message that the SYSTEM will send to the user later: a reminder, a wake-up alarm, a ping or a check-in. Use it only when the user has just asked for "
        "something like that. delay_seconds is counted from the current time (stated in the system prompt, in the user's time zone): work it out carefully, e.g. "
        "'at 7:30' = the seconds until the next 7:30. The system shows the user the exact time it will send, so never claim in your own words that it is set."
    ),
    parameters={
        "type": "object",
        "properties": {
            "delay_seconds": {"type": "integer", "description": "Seconds from now until the message is sent (at most 24 hours)."},
            "text": {"type": "string", "description": "What the message should say, short, one line, in the user's language, addressed to the user."},
            "reminder_type": {
                "type": "string",
                "enum": list(KINDS),
                "description": "alarm = wake the user up (repeats until they answer); ping = a nudge; checkin = the bot starts a conversation (asks how it is going).",
            },
            "repeat_until_ack": {
                "type": "boolean",
                "description": "True = repeat every few minutes until the user answers (use it only if they asked to be pestered until they respond). An alarm always repeats.",
            },
        },
        "required": ["delay_seconds", "text", "reminder_type"],
    },
)

INSTRUCTIONS = (
    "## Reminders, alarms and pings\n"
    "You DO have the technical ability to write to the user first. The user has asked for a reminder, an alarm or a ping: call schedule_reminder (type alarm = wake up,\n"
    "ping = nudge, checkin = you start a conversation). The SYSTEM then confirms to the user with the exact time; never say yourself that it is set, and if you are\n"
    "unsure WHEN they want it, do not call the function: ask them. An alarm is repeated by the system every few minutes until the user answers."
)

WAKEUP_INSTRUCTIONS = (
    "The user asked to be WOKEN UP: you MUST call schedule_reminder with reminder_type='alarm' and repeat_until_ack=true (the system then pings them every 2-3 minutes\n"
    "until they press the button or say they are up). Do not replace it with a check-in, a plain ping or a promise in words."
)


def render_note(text: str, kind: str, persistent: bool) -> str:
    shown = " ".join(text.split()).replace('"', "'")
    again = " and repeats it every few minutes until the user answers" if persistent else ""
    return (
        "## Reminder (made by the system)\n"
        f'The system scheduled a {kind} message "{shown}"{again} and tells the user the exact time itself. Do not state the time and do not say that it is set;\n'
        "answer the rest of the user's message as usual, in a short phrase."
    )


@dataclass(frozen=True)
class ReminderSpec:
    delay_seconds: int
    text: str
    kind: str
    persistent: bool
    extra: dict[str, Any] = field(default_factory=dict)  # State.extra: ack_button, skip_if_user_wrote_since (see ``deliver``)


@dataclass(frozen=True)
class Rejected:
    reason: str  # bad_time | too_far


@dataclass(frozen=True)
class Scheduled:
    token: str
    when: str  # the exact local time, as the user reads it


def parse_reminder(calls: Sequence[ToolCall], settings: Settings, *, user_text: str | None = None) -> ReminderSpec | Rejected | None:
    """The first call of ``schedule_reminder``: a spec, ``Rejected`` (the user is told why), or None (unusable: dropped silently, the user can ask again).
    ``user_text`` is the user's own message: when it asks to be woken up, the spec is an ALARM that repeats until acknowledged, whatever type the model wrote."""
    for call in calls:
        if call.name != TOOL_NAME:
            continue
        args = call.args
        delay, text, kind, repeat = args.get("delay_seconds"), args.get("text"), args.get("reminder_type"), args.get("repeat_until_ack", False)
        if isinstance(delay, float) and delay.is_integer():
            delay = int(delay)
        if isinstance(delay, bool) or not isinstance(delay, int):
            return None
        if not isinstance(text, str) or kind not in KINDS or not isinstance(repeat, bool):
            return None
        line = " ".join(text.split())
        if not line or len(line) > settings.reminders_text_max_chars or personas.has_hidden_chars(line) or not any(ch.isalnum() for ch in line):
            return None  # too long is refused, not cut: a cut message is a message nobody wrote
        if delay < settings.reminders_min_delay_seconds:
            return Rejected("bad_time")
        if delay > settings.reminders_max_delay_seconds:
            return Rejected("too_far")
        if user_text is not None and wants_wakeup(user_text):
            return ReminderSpec(delay, line, "alarm", True)
        return ReminderSpec(delay, line, kind, kind == "alarm" or repeat)
    return None


# ---------------------------------------------------------------- what the user reads (fixed texts)


def when_phrase(fire_at: datetime, now: datetime, tz: ZoneInfo) -> str:
    """"через 20 мин (в 14:35)" / "сегодня в 18:00" / "завтра в 07:30": the exact local time the message will come."""
    local, here = fire_at.astimezone(tz), now.astimezone(tz)
    clock = local.strftime("%H:%M")
    minutes = round((fire_at - now).total_seconds() / 60)
    if minutes < 60:
        return f"через {max(minutes, 1)} мин (в {clock})"
    days = (local.date() - here.date()).days
    if days == 0:
        return f"сегодня в {clock}"
    if days == 1:
        return f"завтра в {clock}"
    return f"{local.strftime('%d.%m')} в {clock}"


def scheduled_text(spec: ReminderSpec, when: str) -> str:
    template = texts.REMINDER_ALARM_SCHEDULED if spec.kind == "alarm" else texts.REMINDER_PING_SCHEDULED if spec.persistent else texts.REMINDER_SCHEDULED
    return template.format(when=when, text=spec.text)


def callback_data(token: str, action: str) -> str:
    return f"{CALLBACK_PREFIX}:{token}:{action}"  # 3 + 32 + 2 = 37 bytes of Telegram's 64


def parse_callback(data: str) -> tuple[str, str] | None:
    match = CALLBACK_RE.match(data)
    return None if match is None else (match.group(1), match.group(2))


def cancel_markup(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=texts.REMINDER_CANCEL_BUTTON, callback_data=callback_data(token, "x"))]])


def ack_markup(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=texts.REMINDER_ACK_BUTTON, callback_data=callback_data(token, "a"))]])


FIRE = {"alarm": "⏰ {text}", "ping": "🔔 {text}", "checkin": "💬 {text}"}


def fire_text(kind: str, text: str, attempt: int, total: int) -> str:
    body = FIRE[kind].format(text=text)
    return body if attempt == 0 else body + texts.REMINDER_REPEAT_NOTE.format(n=attempt, total=total)


# ---------------------------------------------------------------- state in Redis

ACTIVE = "active"  # sent, waiting for the user's answer (a persistent reminder)
SCHEDULED = "scheduled"  # not sent yet
ACKED = "acked"
CANCELED = "canceled"
DONE = "done"  # delivered and nothing more is owed (a plain reminder, or a persistent one that ran out of repeats)


@dataclass
class State:
    tenant: str
    text: str
    kind: str
    persistent: bool
    thread_id: int | None
    persona_id: str | None
    fire_at: str
    status: str = SCHEDULED
    attempt: int = 0  # the number of the next / current send: 0 = the first message, 1.. = repeats
    last_message_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def dumps(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def loads(cls, raw: str) -> State:
        return cls(**json.loads(raw))


class ReminderStore:
    """Reminders in Redis: ``<prefix>:r:<token>`` (the state), ``<prefix>:t:<tenant>`` (a set of the tenant's tokens: the cap and the "who is waiting" lookup),
    ``<prefix>:sent:<token>:<attempt>`` (the delivery claim). Everything has a TTL: nothing lives for ever."""

    def __init__(self, redis: Any, *, prefix: str = "civ:rem", ttl_seconds: int = 172_800) -> None:
        self._redis = redis
        self._prefix = prefix
        self._ttl = ttl_seconds

    @classmethod
    def from_settings(cls, settings: Settings) -> ReminderStore:
        from redis.asyncio import Redis

        timeout = settings.reminders_redis_timeout_seconds
        client = Redis.from_url(
            settings.reminders_redis_url or settings.celery_broker_url, socket_timeout=timeout, socket_connect_timeout=timeout, decode_responses=True
        )
        return cls(client, prefix=settings.reminders_key_prefix, ttl_seconds=settings.reminders_state_ttl_seconds)

    def _key(self, *parts: str) -> str:
        return ":".join((self._prefix, *parts))

    async def aclose(self) -> None:
        close = getattr(self._redis, "aclose", None)
        if close is not None:
            await close()

    async def put(self, token: str, state: State) -> None:
        await self._redis.set(self._key("r", token), state.dumps(), ex=self._ttl)
        await self._redis.sadd(self._key("t", state.tenant), token)
        await self._redis.expire(self._key("t", state.tenant), self._ttl)

    async def get(self, token: str) -> State | None:
        raw = await self._redis.get(self._key("r", token))
        return None if raw is None else State.loads(raw)

    async def drop(self, token: str, tenant: str) -> None:
        await self._redis.delete(self._key("r", token))
        await self._redis.srem(self._key("t", tenant), token)

    async def tokens(self, tenant: str) -> list[str]:
        """The tenant's live tokens (a token whose state expired is pruned from the set on the way)."""
        live: list[str] = []
        for token in sorted(await self._redis.smembers(self._key("t", tenant))):
            if await self._redis.exists(self._key("r", token)):
                live.append(token)
            else:
                await self._redis.srem(self._key("t", tenant), token)
        return live

    async def pending_count(self, tenant: str) -> int:
        """Reminders that still owe the user something: not yet sent, or waiting for an answer."""
        count = 0
        for token in await self.tokens(tenant):
            state = await self.get(token)
            if state is not None and state.status in (SCHEDULED, ACTIVE):
                count += 1
        return count

    async def waiting(self, tenant: str) -> list[tuple[str, State]]:
        """Persistent reminders that were sent and wait for the user's answer."""
        waiting: list[tuple[str, State]] = []
        for token in await self.tokens(tenant):
            state = await self.get(token)
            if state is not None and state.status == ACTIVE:
                waiting.append((token, state))
        return waiting

    async def claim_send(self, token: str, attempt: int) -> bool:
        """True for the first delivery of this attempt only (``SET NX``): a duplicate delivery of the same Celery task does nothing."""
        return bool(await self._redis.set(self._key("sent", token, str(attempt)), "1", nx=True, ex=self._ttl))

    async def release_send(self, token: str, attempt: int) -> None:
        await self._redis.delete(self._key("sent", token, str(attempt)))


def repeat_limit(kind: str, settings: Settings) -> int:
    """How many REPEATS follow the first message: an alarm gets ``reminders_alarm_max_repeats`` (60 = three hours at 180 s), everything else ``reminders_max_repeats`` (5)."""
    return settings.reminders_alarm_max_repeats if kind == "alarm" else settings.reminders_max_repeats


def repeat_gap(kind: str, settings: Settings, rng: random.Random | None = None) -> int:
    """Seconds until the next repeat: an alarm repeats every ``reminders_alarm_repeat_seconds`` (180) exactly, other persistent messages every 2-3 minutes."""
    if kind == "alarm":
        return settings.reminders_alarm_repeat_seconds
    return (rng or random).randint(settings.reminders_repeat_min_seconds, settings.reminders_repeat_max_seconds)


async def send_ping(
    bot: Bot, settings: Settings, *, chat_id: int, thread_id: int | None, text: str, markup: InlineKeyboardMarkup | None, kind: str, attempt: int
) -> TgMessage:
    """One message of a reminder. ALWAYS with sound (``disable_notification=False`` is passed explicitly: a wake-up call that arrives silently is no wake-up call).
    A REPEAT of an alarm goes out as a voice note when the voice mode is on and the sound file exists: a voice note makes the phone sound its priority tone. Any problem with the
    voice (no file, a file Telegram refuses) falls back to the ordinary text message: the alarm is never lost to its own sound."""
    voice_path = settings.reminders_voice_file
    if kind == "alarm" and attempt > 0 and settings.reminders_voice_ping and voice_path and Path(voice_path).is_file():
        try:
            return await bot.send_voice(
                chat_id=chat_id, voice=FSInputFile(voice_path), caption=text[:1024], message_thread_id=thread_id,
                reply_markup=markup, disable_notification=False,
            )  # fmt: skip
        except (TelegramAPIError, OSError):
            log.warning("could not send the voice ping %s; sending text instead", voice_path, exc_info=True)
    return await bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id, reply_markup=markup, disable_notification=False)


# ---------------------------------------------------------------- the engine

Enqueue = Callable[[Sequence[Any], int], None]  # (celery args, countdown seconds): the hand-off to the queue


def default_enqueue(args: Sequence[Any], countdown: int) -> None:
    from ai_civ.tasks import send_scheduled_message  # lazily: ai_civ.tasks imports the bot package

    send_scheduled_message.apply_async(args=list(args), countdown=countdown)


class Reminders:
    def __init__(
        self, db: Database, bot: Bot, settings: Settings, *, store: ReminderStore | None = None, enqueue: Enqueue = default_enqueue
    ) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings
        self._store = store
        self._enqueue = enqueue

    @property
    def store(self) -> ReminderStore:
        if self._store is None:  # connects lazily: building the bot never needs Redis to be up
            self._store = ReminderStore.from_settings(self.settings)
        return self._store

    async def aclose(self) -> None:
        if self._store is not None:
            await self._store.aclose()

    # ------------------------------------------------------------ scheduling (the model's function)

    async def schedule(
        self, tenant_id: uuid.UUID, tz_name: str, spec: ReminderSpec, *, thread_id: int | None, persona_id: uuid.UUID | None, now: datetime | None = None
    ) -> Scheduled | str:
        """Write the state, hand the task to the queue. Returns ``Scheduled`` (the token and the user-facing "when" phrase), or ``"too_many"`` (the cap of open
        reminders), or ``"unavailable"`` (Redis or the broker is down; nothing is left behind)."""
        s = self.settings
        now = now or datetime.now(UTC)
        token = uuid.uuid4().hex
        fire_at = now + timedelta(seconds=spec.delay_seconds)
        state = State(
            tenant=str(tenant_id), text=spec.text, kind=spec.kind, persistent=spec.persistent, thread_id=thread_id,
            persona_id=None if persona_id is None else str(persona_id), fire_at=fire_at.isoformat(), extra=dict(spec.extra),
        )  # fmt: skip
        try:
            if await self.store.pending_count(state.tenant) >= s.reminders_max_pending:
                return "too_many"
            await self.store.put(token, state)
            try:
                await asyncio.wait_for(asyncio.to_thread(self._enqueue, [str(tenant_id), token, 0], spec.delay_seconds), timeout=s.reminders_redis_timeout_seconds * 5)
            except Exception:
                await self.store.drop(token, state.tenant)  # nothing may stay behind a reminder that was never queued
                raise
        except Exception:  # noqa: BLE001 - a courtesy feature must not break the reply: the user is told by a fixed text
            log.warning("could not schedule a reminder for tenant %s", tenant_id, exc_info=True)
            return "unavailable"
        return Scheduled(token, when_phrase(fire_at, now, ZoneInfo(tz_name)))

    # ------------------------------------------------------------ buttons and the user's words

    async def on_callback(self, tenant: Any, query: CallbackQuery) -> None:
        parsed = parse_callback(query.data or "")
        if parsed is None:
            await self._answer(query, texts.PROPOSAL_UNKNOWN_BUTTON)
            return
        token, action = parsed
        state = await self.store.get(token)
        if state is None or state.tenant != str(tenant.id):  # expired, or somebody else's: indistinguishable
            await self._answer(query, texts.REMINDER_TOAST["gone"])
            return
        if action == "a":
            outcome = await self._ack(token, state)
            final = texts.REMINDER_ACKED
        else:
            outcome = await self._cancel(token, state)
            final = texts.REMINDER_CANCELED.format(text=state.text)
        await self._answer(query, texts.REMINDER_TOAST[outcome])
        message = query.message
        if message is None or outcome in ("gone",):
            return
        try:
            await self.bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id, text=final, reply_markup=None)
        except TelegramAPIError:  # includes "message is not modified" (a double press): the decision is already made
            log.info("could not edit the reminder card of tenant %s", tenant.id, exc_info=True)

    async def _ack(self, token: str, state: State) -> str:
        if state.status in (ACKED, DONE, CANCELED):
            return "settled"
        state.status = ACKED
        await self.store.put(token, state)
        return "acked"

    async def _cancel(self, token: str, state: State) -> str:
        if state.status != SCHEDULED:
            return "settled"  # already delivered / answered / canceled: nothing to take back
        state.status = CANCELED
        await self.store.put(token, state)
        return "canceled"

    async def ack_by_text(self, tenant_id: uuid.UUID, text: str) -> int:
        """The user wrote "я встал" / "я тут": every alarm that waits for an answer stops. Fail-open: a Redis problem is only logged (the button still works)."""
        if not is_ack_text(text):
            return 0
        try:
            waiting = await self.store.waiting(str(tenant_id))
            for token, state in waiting:
                await self._ack(token, state)
        except Exception:  # noqa: BLE001
            log.warning("could not acknowledge the reminders of tenant %s by text", tenant_id, exc_info=True)
            return 0
        return len(waiting)

    @staticmethod
    async def _answer(query: CallbackQuery, text_: str) -> None:
        try:
            await query.answer(text_)
        except TelegramAPIError:
            log.warning("could not answer a callback query", exc_info=True)


# ---------------------------------------------------------------- delivery (the Celery task body)


async def _keep_the_alarm_going(
    store: ReminderStore, state: State, tenant_id: uuid.UUID, token: str, attempt: int, settings: Settings, enqueue_next: Enqueue,
    rng: random.Random | None, queued: bool,
) -> None:
    """A wake-up alarm must not die because ONE message failed (Telegram hiccup, a full Celery retry budget): when a send of an alarm fails, the next repeat is queued BEFORE the
    error propagates (Celery still retries the failed attempt on its own). Never raises: it is a last-resort safety net, and it is skipped when the alarm was answered or canceled,
    when the chain is already queued, and for everything that is not a persistent alarm."""
    if queued or state.kind != "alarm" or not state.persistent or attempt >= repeat_limit(state.kind, settings):
        return
    try:
        fresh = await store.get(token)
        if fresh is not None and fresh.status in (ACKED, CANCELED, DONE):
            return
        state.status, state.attempt = ACTIVE, attempt + 1
        await store.put(token, state)
        enqueue_next([str(tenant_id), token, attempt + 1], repeat_gap(state.kind, settings, rng))
    except Exception:  # noqa: BLE001
        log.warning("could not keep the alarm %s going after a failed send", token, exc_info=True)


async def deliver(
    db: Database,
    bot: Bot,
    settings: Settings,
    store: ReminderStore,
    tenant_id: uuid.UUID,
    token: str,
    attempt: int,
    *,
    enqueue_next: Enqueue,
    rng: random.Random | None = None,
) -> str:
    """Send one scheduled message. Returns ``sent | duplicate | gone | skipped | inactive``. Raises on a failed send AFTER giving the claim back: Celery retries (bounded).

    ``skipped`` = the reminder was canceled or acknowledged meanwhile (or a follow-up whose user wrote in the meantime); ``inactive`` = the tenant's group is not active (nothing is sent to a frozen group). After a persistent
    message the next repeat is queued (2-3 minutes later, at most ``REMINDERS_MAX_REPEATS`` times); a plain one is done."""
    state = await store.get(token)
    if state is None or state.tenant != str(tenant_id):
        return "gone"
    if state.status in (ACKED, CANCELED, DONE) or (attempt > 0 and state.status != ACTIVE):
        return "skipped"
    if not await store.claim_send(token, attempt):
        return "duplicate"
    queued = False
    try:
        async with db.tenant_session(tenant_id) as session:
            user = (
                await session.execute(
                    select(User.forum_chat_id, User.onboarding_state, User.group_state).where(User.id == tenant_id, User.telegram_user_id.is_not(None))  # an app person has no Telegram
                )
            ).one_or_none()
        if (
            user is None or user.forum_chat_id is None
            or OnboardingState(user.onboarding_state) is not OnboardingState.done or GroupState(user.group_state) is not GroupState.active
        ):
            state.status = DONE
            await store.put(token, state)
            return "inactive"
        since = state.extra.get("skip_if_user_wrote_since")
        if since is not None:  # a follow-up to a message: if the user answered meanwhile (wrote anything), there is nothing to follow up
            async with db.tenant_session(tenant_id) as session:
                wrote = (
                    await session.execute(
                        select(Message.id)
                        .where(Message.role == MessageRole.user, Message.created_at > datetime.fromisoformat(since))
                        .limit(1)
                    )
                ).first()
            if wrote is not None:
                state.status = DONE
                await store.put(token, state)
                return "skipped"
        markup = ack_markup(token) if state.persistent or state.extra.get("ack_button") else None
        sent = await send_ping(
            bot, settings, chat_id=user.forum_chat_id, thread_id=state.thread_id, kind=state.kind, attempt=attempt, markup=markup,
            text=fire_text(state.kind, state.text, attempt, repeat_limit(state.kind, settings)),
        )  # fmt: skip
        async with db.tenant_session(tenant_id) as session:  # the bot wrote first: the user's answer sees this message in its context
            await session.execute(
                pg_insert(Message)
                .values(
                    user_id=tenant_id,
                    persona_id=None if state.persona_id is None else uuid.UUID(state.persona_id),
                    role=MessageRole.assistant,
                    content=fire_text(state.kind, state.text, 0, repeat_limit(state.kind, settings)),
                    telegram_message_id=sent.message_id,
                )
                .on_conflict_do_nothing(constraint="uq_messages_user_telegram")
            )
        state.last_message_id = sent.message_id
        fresh = await store.get(token)
        if fresh is not None and fresh.status in (ACKED, CANCELED):  # the user answered while the message was on its way: that decision stands
            return "sent"
        if state.persistent and attempt < repeat_limit(state.kind, settings):
            state.status, state.attempt = ACTIVE, attempt + 1
            await store.put(token, state)
            gap = repeat_gap(state.kind, settings, rng)
            queued = True  # from here on the chain is in the queue: a failure of the line below must not queue it a second time
            enqueue_next([str(tenant_id), token, attempt + 1], gap)
        else:
            state.status = DONE
            await store.put(token, state)
    except Exception:
        try:
            await store.release_send(token, attempt)  # the next try of this very task must be allowed to send
        except Exception:  # noqa: BLE001
            log.warning("could not release the delivery claim of reminder %s", token, exc_info=True)
        await _keep_the_alarm_going(store, state, tenant_id, token, attempt, settings, enqueue_next, rng, queued)
        raise
    return "sent"


# ---------------------------------------------------------------- the model's tool


class ReminderTool:
    """Offered only when the user's own words ask for a reminder / alarm / ping (``wants_reminder``). Never in the interview, never on a message with an attachment."""

    name = "schedule_reminder"
    functions = frozenset({TOOL_NAME})
    offer_rank = 52

    def __init__(self, engine: Reminders, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        turn = ctx.turn
        if not self.settings.reminders_enabled or turn is None or not wants_reminder(turn.text):
            return None
        tenant = ctx.tenant

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            parsed = parse_reminder(calls, self.settings, user_text=turn.text)
            if parsed is None:
                return None
            if isinstance(parsed, Rejected):
                return ToolResult(text=texts.REMINDER_BAD_TIME if parsed.reason == "bad_time" else texts.REMINDER_TOO_FAR)
            thread_id = None if ctx.persona is None else ctx.persona.telegram_thread_id
            when = await self.engine.schedule(tenant.id, tenant.timezone, parsed, thread_id=thread_id, persona_id=ctx.persona_id)
            if when == "unavailable":
                return ToolResult(text=texts.REMINDER_UNAVAILABLE)
            if when == "too_many":
                return ToolResult(text=texts.REMINDER_TOO_MANY)
            assert isinstance(when, Scheduled)
            card = Card(scheduled_text(parsed, when.when), cancel_markup(when.token))
            return ToolResult(notes=(render_note(parsed.text, parsed.kind, parsed.persistent),), phrase=True, cards=(card,))

        instructions = f"{INSTRUCTIONS}\n{WAKEUP_INSTRUCTIONS}" if wants_wakeup(turn.text) else INSTRUCTIONS
        return Offered((TOOL,), instructions, run, run_rank=52)

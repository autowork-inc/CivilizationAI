"""The Orchestrator: one use case per entry point, no aiogram types in the signatures (only the ``Bot`` it sends with).

Reply transaction (``handle_message``), in the order that matters:

  1. the incoming message is written to ``messages`` FIRST (PostgreSQL is the source of truth: no byte is lost even if
     everything after this line fails; a replayed Telegram update hits ``uq_messages_user_telegram`` and is not stored twice);
  2. persona: a message in a topic -> the persona bound to that ``message_thread_id``; a message in "General" -> the topic
     router (LLM) may open / reuse a topic, the stored message is re-filed into that persona;
  3. Tier 2: the user's message -> embedding -> nearest episodes -> LLM-Judge (no transaction open; optional - a failure
     degrades to "memory unavailable" instead of failing the reply);
  3b. prompt = persona prompt + Core Profile + Effective Principles + Episodic memory + Context Buffer; Tier 1 and 3 are
     read in ONE short transaction;
  4. Gemini is called with NO database transaction open and a hard ``asyncio.wait_for`` on top of the HTTP timeout. A user who has
     contacts also offers the model two functions (the Diplomat, ``bot/diplomat.py``): ``ask_contact`` - the code validates the ask, the
     database checks the grants and writes the audit row, and the model is then asked again with the result rendered into the prompt -
     and ``propose_event``, whose confirmation the user reads is a fixed text written by code after the commit. EVERY user is also
     offered ``create_event`` (``bot/quick_events.py``): the user's own plan becomes a draft and a card with buttons that code sends
     under the reply - the model is told that nothing is saved until the button is pressed;
  5. the answer is sent to the topic, THEN saved to ``messages`` (what is stored is what the user actually saw; a failed
     send leaves no phantom assistant message in the context).

Failure model (CLAUDE.md "Fail-Fast"): no retry loops here. Whatever raises inside one message is logged with its traceback,
the user gets the fixed system notice in the place where they wrote, and the polling loop keeps running. Retrying is the
user's (and, for proactive messages, Celery's) job.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as TgMessage
from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ.bot import attachments, checkin, diplomat, error_handler, fetch, flashback, summary, onboarding, personas, quick_events, routing, texts, topics, voice
from ai_civ.bot.janitor import Janitor
from ai_civ.bot.personas import CONCIERGE_PROMPT, FEEDBACK_CODE, FEEDBACK_PERSONA, SETTINGS_PERSONA, SYSTEM_CODES, PersonaSpec
from ai_civ.bot.prompt import Memories, Prompt, build_prompt, select_memories
from ai_civ.bot.proactive import CHECK_IN, ProactiveTrigger
from ai_civ.bot.reminders import ReminderSpec, ReminderTool, Reminders, Scheduled
from ai_civ.bot.texts import KeyedLocks
from ai_civ.bot.tools import AfterReply, Card, Delivery, ToolContext, ToolRegistry, UserTurn
from ai_civ.bot.tools.base import Markup as CardMarkup
from ai_civ.bot.tools.builtin import DiplomatTool, QuickEventTool, RenameTopicTool
from ai_civ.bot.tools.capsule import CapsuleTool, TimeCapsules
from ai_civ.bot.tools.cards import Cards, CardsTool
from ai_civ.bot.tools.create_topic import CreateTopicTool
from ai_civ.bot.tools.delete_topic import DeleteTopics, DeleteTopicTool
from ai_civ.bot.tools.debate import DebateTool
from ai_civ.bot.tools.decompose import DecomposeTool, Tasks
from ai_civ.bot.tools.geo import Geo, GeoNoteTool
from ai_civ.bot.tools.inventory import Inventory, InventoryTool
from ai_civ.bot.tools.recipes import RecipeTool, Recipes
from ai_civ.bot.tools.save_fact import SaveFactTool, SaveFacts
from ai_civ.bot.tools.workout import WorkoutTool, Workouts
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import GroupState, MessageRole, OnboardingState, PersonaMode
from ai_civ.db.models import CharacterProfile, Message, MessageAttachment, Persona, User
from ai_civ.memory.judge import recall_with_evidence
from ai_civ.memory.llm import AudioLLM, Embedder, JsonLLM, LLMError, Media, TextLLM, ToolLLM

log = logging.getLogger(__name__)

T = TypeVar("T")

TYPING_HEARTBEAT_SECONDS = 4.5  # Telegram shows "typing..." for ~5 s after one sendChatAction; a long voice note takes tens of seconds


MAX_TOPIC_NAME_CHARS = 128  # Telegram's own limit for a topic name


class RenameResult(enum.StrEnum):
    renamed = "renamed"
    unchanged = "unchanged"  # the topic already has this name
    invalid_title = "invalid_title"  # nothing usable was asked for
    unknown_topic = "unknown_topic"  # not one of this tenant's topics that the bot created
    no_rights = "no_rights"  # Telegram says the bot may not edit it
    gone = "gone"  # the topic was deleted
    failed = "failed"  # anything else (network, flood limit): nothing changed


def telegram_markup(markup: InlineKeyboardMarkup | CardMarkup | None) -> InlineKeyboardMarkup | None:
    """The Telegram adapter of the neutral card buttons (``tools.base.CardButton`` rows) -> ``InlineKeyboardMarkup``. An aiogram markup passes through."""
    if markup is None or isinstance(markup, InlineKeyboardMarkup):
        return markup
    rows = [
        [
            InlineKeyboardButton(text=b.label, url=(b.payload or {}).get("url") or b.id)
            if b.action == "url"
            else InlineKeyboardButton(text=b.label, callback_data=(b.payload or {}).get("callback_data") or b.id)
            for b in row
        ]
        for row in markup
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def topic_is_gone(reason: str) -> bool:
    """Is this a Telegram 400 about a forum topic that does not exist (any more)? The Bot API has no error codes for it, only the text
    ("Bad Request: message thread not found"; the underscored spelling is accepted too)."""
    return "message thread not found" in reason.lower().replace("_", " ")


class TopicCreationError(RuntimeError):
    """Telegram refused createForumTopic (not a forum, or the bot lacks the can_manage_topics right)."""


@dataclass(frozen=True)
class TenantInfo:
    id: uuid.UUID
    timezone: str
    forum_chat_id: int | None
    onboarding_state: OnboardingState  # anything but ``done``: the interview runs instead of the ecosystem
    group_state: GroupState = GroupState.active  # frozen / dormant: the bot says nothing in the group (see registration)


@dataclass(frozen=True)
class Incoming:
    """A message from the tenant, already attributed to a tenant by the middleware. A voice note arrives with ``text=""`` and the
    ``voice`` clip; ``Orchestrator._hear`` turns it into text before anything else happens."""

    tenant: TenantInfo
    thread_id: int | None  # forum topic (message_thread_id); None = the "General" topic
    telegram_message_id: int
    text: str
    voice: voice.VoiceClip | None = None
    audio: Media | None = None  # the recording itself (set by ``_hear`` when ``voice_hear_tone`` is on): it travels to the chat model for THIS reply only
    attachment: attachments.Attachment | None = None  # a photo / document: metadata from Telegram (``text`` is then the caption)
    prepared: attachments.Prepared | None = None  # the same attachment after ``Orchestrator._look`` (downloaded and read)
    username: str | None = None  # the sender's Telegram @username (for the feedback card only)

    @property
    def content(self) -> str:
        """What the stored message row says: the caption, then a marker for the attachment (the bytes are never stored)."""
        return attachments.stored_text(self.text, self.attachment)


@dataclass(frozen=True)
class Answer:
    text: str
    third_party: bool = False  # built from ANOTHER person's data: stored flagged, never turned into memory
    cards: tuple[Card, ...] = ()  # messages with buttons that CODE sends under the text (a calendar draft, a "remembered" card)
    after_reply: tuple[AfterReply, ...] = ()  # actions of the tools, run once the text was delivered (a validated topic rename)


@dataclass(frozen=True)
class Stored:
    id: uuid.UUID
    created_at: datetime
    persona_id: uuid.UUID | None
    replay: bool  # the row already existed: this Telegram update was delivered before


async def load_tenant(db: Database, user_id: uuid.UUID) -> TenantInfo | None:
    async with db.tenant_session(user_id) as session:
        row = (
            await session.execute(
                select(User.timezone, User.forum_chat_id, User.onboarding_state, User.group_state).where(
                    User.id == user_id
                )
            )
        ).one_or_none()
    if row is None:
        return None
    return TenantInfo(
        id=user_id,
        timezone=row.timezone,
        forum_chat_id=row.forum_chat_id,
        onboarding_state=OnboardingState(row.onboarding_state),
        group_state=GroupState(row.group_state),
    )


class Orchestrator:
    def __init__(
        self,
        *,
        db: Database,
        router_llm: JsonLLM,
        chat_llm: TextLLM,
        settings: Settings,
        bot: Bot,
        embedder: Embedder | None = None,
    ) -> None:
        self.db = db
        self.router_llm = router_llm  # also the Judge's transport (JSON mode); the model is GEMINI_JUDGE_MODEL
        self.chat_llm = chat_llm
        self.settings = settings
        self.bot = bot
        self.embedder = embedder  # None = Tier 2 is off: replies are built from Tier 1 + Tier 3 only
        self.diplomat = diplomat.Diplomat(db, settings)
        self.quick = quick_events.QuickEvents(db, bot, settings)
        self.janitor = Janitor(bot)  # deletes service cards by itself (the memory card) or on request
        self.facts = SaveFacts(db, bot, settings, self.janitor)
        self.tasks = Tasks(db, bot, settings)
        self.cards = Cards(db, bot, settings)
        self.workouts = Workouts(db, settings)
        self.recipes = Recipes(db, bot, settings)
        self.inventory = Inventory(db, bot, settings)
        self.geo = Geo(db, bot, settings)
        self.capsules = TimeCapsules(db, bot, settings)
        self.delete_topics = DeleteTopics(db, bot, settings)
        self.reminders = Reminders(db, bot, settings)
        # What the model may DO in a reply (see bot/tools): each tool decides by itself whether it is offered and runs its own calls.
        self.tools = ToolRegistry(
            [
                QuickEventTool(self.quick, settings),
                DiplomatTool(self.diplomat, settings),
                RenameTopicTool(settings, allowed=self._rename_offered, apply=self._apply_rename),
                CreateTopicTool(settings, allowed=self._create_topic_allowed, apply=self._apply_create_topic, last_reply=self._last_main_reply),
                DeleteTopicTool(self.delete_topics, settings),
                SaveFactTool(self.facts, settings),
                DecomposeTool(self.tasks, settings),
                CardsTool(self.cards, settings),
                WorkoutTool(self.workouts, settings),
                RecipeTool(self.recipes, settings),
                InventoryTool(self.inventory, settings),
                GeoNoteTool(self.geo, settings),
                CapsuleTool(self.capsules, settings),
                ReminderTool(self.reminders, settings),
                DebateTool(settings),
            ]
        )
        self._locks = KeyedLocks()
        self.typing_heartbeat_seconds = TYPING_HEARTBEAT_SECONDS

    # ------------------------------------------------------------ entry points (never raise, except cancellation)

    async def handle_message(self, inc: Incoming) -> None:
        """A tenant who has not finished onboarding is interviewed, whatever they write and wherever they write it."""
        if inc.attachment is not None and inc.tenant.onboarding_state is not OnboardingState.done:
            await self._guarded(inc.tenant, inc.thread_id, lambda: self._send(self._chat_id(inc.tenant), inc.thread_id, texts.FILE_LATER))
            return
        work = self._onboard if inc.tenant.onboarding_state is not OnboardingState.done else self._reply
        await self._guarded(inc.tenant, inc.thread_id, lambda: self._with_ears(inc, work))

    async def _with_ears(self, inc: Incoming, work: Callable[[Incoming], Awaitable[None]]) -> None:
        """A voice note is listened to first (inside the same boundary and the same topic lock as any message); then ``work`` gets an
        ordinary text message and cannot tell the difference."""
        if inc.voice is not None:
            # a long recording is downloaded, transcribed and answered in tens of seconds, and Telegram's "typing" fades after ~5: keep it alive until `work` is done
            async with self._keep_typing(self._chat_id(inc.tenant), inc.thread_id):
                heard = await self._hear(inc, inc.voice)
                if heard is None:  # refused, unintelligible or already answered: the user was told (or nothing is owed)
                    return
                await self._with_attachment_and_work(heard, work)
            return
        await self._with_attachment_and_work(inc, work)

    async def _with_attachment_and_work(self, inc: Incoming, work: Callable[[Incoming], Awaitable[None]]) -> None:
        if inc.attachment is not None:
            seen = await self._look(inc)
            if seen is None:  # refused or unreadable (the user was told why), or already answered
                return
            inc = seen
        await work(inc)

    async def _look(self, inc: Incoming) -> Incoming | None:
        """A photo / document -> the same message with ``prepared`` set. None = nothing to process (the user is told why).

        Order: limits on Telegram's metadata (nothing is downloaded for a refused file) -> a redelivered update that was already answered is
        skipped -> download into memory -> signature, size and extraction (DOCX in a thread) -> ``Prepared``. A failure of the download or a
        timeout raises: the boundary says ``FAILURE`` and nothing is stored. The bytes are kept nowhere."""
        att, cfg, tenant = inc.attachment, self.settings, inc.tenant
        assert att is not None
        chat_id = self._chat_id(tenant)
        why = attachments.refusal(att, cfg)
        if why is not None:
            await self._send(chat_id, inc.thread_id, why)
            return None
        if await self._answered_before(inc):
            log.info("attachment update %s of tenant %s was already answered; skipped", inc.telegram_message_id, tenant.id)
            return None
        await self._typing(chat_id, inc.thread_id)
        data = await self._bounded(attachments.download(self.bot, att, cfg), "file download")
        try:
            prepared = await self._bounded(asyncio.to_thread(attachments.prepare, att, data, cfg), "file reading")
        except attachments.Rejected as exc:
            log.info("attachment of tenant %s refused after the download: %s", tenant.id, exc.reason)
            reply = {
                "too_big": texts.FILE_TOO_BIG.format(limit=cfg.attachment_max_bytes // 1_000_000),
                "text_too_big": texts.FILE_TEXT_TOO_BIG,
                "unsupported": texts.FILE_UNSUPPORTED,
                "empty": texts.FILE_EMPTY,
            }.get(exc.reason, texts.FILE_UNREADABLE)
            await self._send(chat_id, inc.thread_id, reply)
            return None
        return replace(inc, prepared=prepared)

    async def _store_attachment(self, tenant: TenantInfo, message_id: uuid.UUID, inc: Incoming) -> None:
        """The record of what was attached (no bytes) hangs on the stored message; a redelivered update does not write it twice."""
        att, prepared = inc.attachment, inc.prepared
        if att is None or prepared is None:
            return
        async with self.db.tenant_session(tenant.id) as session:
            await session.execute(
                pg_insert(MessageAttachment)
                .values(
                    user_id=tenant.id,
                    message_id=message_id,
                    kind=att.kind,
                    content_kind=prepared.content_kind,
                    mime_type=prepared.mime,
                    file_name=att.clean_name,
                    size_bytes=prepared.size,
                    sha256=prepared.sha256,
                    telegram_file_id=att.file_id,
                    extracted_text=prepared.text,
                    text_truncated=prepared.truncated,
                )
                .on_conflict_do_nothing(constraint="uq_message_attachments_message")
            )

    async def _summarize(self, tenant: TenantInfo, persona: Persona | None, request: summary.SummaryRequest) -> Answer:
        """The reply to "link + summarise it". Never returns None. The page is fetched by code (``fetch.fetch_content``: SSRF-safe, bounded); a
        refusal of the fetcher is a fixed polite sentence written by code. The text goes to the model as untrusted data and the reply is generated
        WITHOUT any function (this method never reaches the tool registry), so nothing the page says can trigger an action; the reply is stored
        flagged ``third_party_data`` so the GC never remembers it."""
        try:
            page = await fetch.fetch_content(request.url, limits=fetch.limits_from(self.settings))
        except fetch.FetchError as exc:
            log.info("summary of %s for tenant %s refused: %s", request.url, tenant.id, exc)
            return Answer(texts.SUMMARY_FAIL.get(exc.kind, texts.SUMMARY_FAIL["failed"]))
        prompt = await build_prompt(
            self.db,
            user_id=tenant.id,
            timezone=tenant.timezone,
            persona_id=None if persona is None else persona.id,
            persona_prompt=CONCIERGE_PROMPT if persona is None else persona.system_prompt,
            settings=self.settings,
            memories=None,  # the page is the subject of this reply, not the user's past
            mode=PersonaMode.normal.value if persona is None else persona.mode,
        )
        return Answer(await self._generate(summary.with_page(prompt, page)), third_party=True)

    @staticmethod
    def _with_attachment(prompt: Prompt, inc: Incoming) -> Prompt:
        """The last user turn gets the attachment: media for the model (an image, a PDF) or the fenced text of a TXT / DOCX. The rules say it is data."""
        prepared, att = inc.prepared, inc.attachment
        if prepared is None or att is None or not prompt.turns or prompt.turns[-1].role != "user":
            return prompt
        last = prompt.turns[-1]
        if prepared.media is not None:
            turn = replace(last, media=(prepared.media,))
        else:
            turn = replace(last, text=f"{last.text}\n\n{attachments.render_file_block(att, prepared)}")
        return replace(prompt, system=f"{prompt.system}\n\n{attachments.FILE_RULES}", turns=[*prompt.turns[:-1], turn])

    async def _hear(self, inc: Incoming, clip: voice.VoiceClip) -> Incoming | None:
        """Voice note -> the same message with ``text`` = what was said. None = nothing to process (the user is told why).

        Order: limits on Telegram's metadata (nothing is downloaded for a refused note) -> a redelivered update that was already answered
        is skipped (no second download, no second echo) -> download into memory -> Gemini transcribes -> cleaned and bounded. A failure of
        the download or of the model raises: the boundary says ``FAILURE`` and nothing is stored (there is no text to store; the user
        records it again). The audio is not kept anywhere."""
        cfg, tenant = self.settings, inc.tenant
        chat_id = self._chat_id(tenant)
        if not cfg.voice_enabled or not isinstance(self.chat_llm, AudioLLM):
            await self._send(chat_id, inc.thread_id, texts.VOICE_OFF)
            return None
        too_long = texts.VOICE_TOO_LONG.format(limit=cfg.voice_max_seconds)
        if voice.too_long(clip, cfg):
            await self._send(chat_id, inc.thread_id, too_long)
            return None
        if await self._answered_before(inc):
            log.info("voice update %s of tenant %s was already answered; skipped", inc.telegram_message_id, tenant.id)
            return None
        await self._typing(chat_id, inc.thread_id)
        data = await self._bounded(voice.download_clip(self.bot, clip, cfg), "voice download", timeout=cfg.voice_llm_timeout_seconds)
        if not data:
            raise LLMError("the voice file Telegram returned is empty")
        if len(data) > cfg.voice_max_bytes:
            await self._send(chat_id, inc.thread_id, too_long)
            return None
        raw = await self._bounded(
            self.chat_llm.transcribe(model=cfg.gemini_transcribe_model, system=voice.SYSTEM, data=data, mime_type=clip.mime),
            "transcription",
            timeout=cfg.voice_llm_timeout_seconds,  # minutes of audio: not the short limit of a text reply
        )
        text = voice.clean_transcript(raw)
        if text is None:
            log.info("voice update %s of tenant %s: no intelligible speech", inc.telegram_message_id, tenant.id)
            await self._send(chat_id, inc.thread_id, texts.VOICE_UNCLEAR)
            return None
        if len(text) > cfg.voice_transcript_max_chars:  # refused, not cut: a cut transcript would change what the user said
            await self._send(chat_id, inc.thread_id, too_long)
            return None
        if cfg.voice_echo:  # not stored: it is a courtesy of the system, not a word of the assistant
            await self._send_quietly(tenant, inc.thread_id, texts.VOICE_ECHO.format(text=texts.shorten(text, 600)))
        audio = Media(data, clip.mime) if cfg.voice_hear_tone else None
        return replace(inc, text=text, audio=audio)

    def _with_voice(self, prompt: Prompt, inc: Incoming) -> Prompt:
        """The last user turn carries the recording (the chat model hears the tone), and the system prompt says how to weigh it against the words.
        The transcript stays the text of the turn: it is what is stored, searched, quoted and saved. No audio, no change."""
        if inc.audio is None or not prompt.turns or prompt.turns[-1].role != "user":
            return prompt
        last = prompt.turns[-1]
        turn = replace(last, media=(*last.media, inc.audio))
        return replace(prompt, system=f"{prompt.system}\n\n{voice.TONE_RULES}", turns=[*prompt.turns[:-1], turn])

    async def _answered_before(self, inc: Incoming) -> bool:
        """This Telegram message is already in ``messages`` AND its topic holds a later assistant message (a redelivered update)."""
        async with self.db.tenant_session(inc.tenant.id) as session:
            row = (
                await session.execute(
                    select(Message.id, Message.created_at, Message.persona_id).where(
                        Message.telegram_message_id == inc.telegram_message_id
                    )
                )
            ).one_or_none()
        return row is not None and await self._already_answered(
            inc.tenant.id, Stored(row.id, row.created_at, row.persona_id, replay=True)
        )

    async def start_onboarding(self, tenant: TenantInfo) -> None:
        """A tenant was just registered (the bot was added to their group): open the interview in the main chat.
        Safe to call twice (overlapping Telegram events): the second call finds the interview already running."""
        await self._guarded(tenant, None, lambda: self._start_interview(tenant))

    async def setup_defaults(self, tenant: TenantInfo, thread_id: int | None) -> None:
        """/setup: the Settings topic plus topics built from the user's goals (idempotent: see ``_setup``).
        Before onboarding is done it only (re)starts the interview: the ecosystem is not open yet."""
        if tenant.onboarding_state is not OnboardingState.done:
            await self._guarded(tenant, thread_id, lambda: self._onboarding_prompt(tenant, thread_id))
        else:
            await self._guarded(tenant, thread_id, lambda: self._setup(tenant, thread_id))

    async def run_proactive(self, tenant: TenantInfo, trigger: ProactiveTrigger) -> bool:
        """The bot writes first (``is_proactive=True``). Raises on failure: the Celery task retries; the user is not
        bothered with a system notice for something they did not ask for. False = nothing to do (the tenant has not
        finished onboarding: nobody writes first to someone who has not been let in).

        Topics are built from each user's goals, so the trigger's persona may not exist for this user. Then the nudge goes
        to the main chat (concierge, ``persona_id NULL``) instead of being lost."""
        if tenant.onboarding_state is not OnboardingState.done:
            log.info("proactive trigger %s skipped for tenant %s: onboarding is not done", trigger.key, tenant.id)
            return False
        if tenant.group_state is not GroupState.active:  # frozen (a stranger is in the group) or dormant (the bot left)
            log.info("proactive trigger %s skipped for tenant %s: the group is %s", trigger.key, tenant.id, tenant.group_state)
            return False
        chat_id = self._chat_id(tenant)
        async with self.db.tenant_session(tenant.id) as session:
            persona = await personas.get_by_code(session, trigger.persona_code)
        if persona is not None and persona.is_active and persona.telegram_thread_id is not None:
            persona_id: uuid.UUID | None = persona.id
            thread_id: int | None = persona.telegram_thread_id
            persona_prompt = persona.system_prompt
            mode = persona.mode
        else:
            log.info(
                "proactive trigger %s for tenant %s: persona %r has no active topic; writing in the main chat",
                trigger.key, tenant.id, trigger.persona_code,
            )  # fmt: skip
            persona_id, thread_id, persona_prompt = None, None, CONCIERGE_PROMPT
            mode = PersonaMode.normal.value
        instruction = trigger.instruction
        if trigger.key == CHECK_IN:  # facts for the check-in (open tasks, the meal window, the last workout), rendered by code from the database; never raises
            followup = self.settings.checkin_morning_followup_seconds if self.settings.reminders_enabled else 0
            extra = await checkin.load(self.db, tenant.id, tenant.timezone, datetime.now(UTC), followup_minutes=followup // 60)
            if extra is not None:
                instruction = f"{instruction}\n\n{extra}"
        async with self._locks.hold((tenant.id, thread_id)):
            prompt = await build_prompt(
                self.db,
                user_id=tenant.id,
                timezone=tenant.timezone,
                persona_id=persona_id,
                persona_prompt=persona_prompt,
                settings=self.settings,
                proactive_instruction=instruction,
                mode=mode,
            )
            answer = await self._generate(prompt)
            if trigger.key == CHECK_IN:  # one line of code about this day last year, only on the first check-in of the local day (never raises)
                line = await flashback.find(self.db, tenant.id, tenant.timezone, self.settings)
                if line is not None:
                    answer = f"{answer}\n\n{line}"
            first_id = await self._send(chat_id, thread_id, answer)
            await self._store_assistant(tenant.id, persona_id, answer, first_id)
        if trigger.key == CHECK_IN:
            await self._morning_followup(tenant, thread_id, persona_id)
        return True

    async def _morning_followup(self, tenant: TenantInfo, thread_id: int | None, persona_id: uuid.UUID | None) -> None:
        """After the first morning check-in: queue ONE short insistent message with the \"I am up\" button, dropped at delivery if the user writes anything before it. Never raises:
        the check-in itself has already gone out."""
        delay = self.settings.checkin_morning_followup_seconds
        if delay <= 0 or not self.settings.reminders_enabled:
            return
        now = datetime.now(UTC)
        if not await checkin.wants_morning_followup(self.db, tenant.id, tenant.timezone, now):
            return
        spec = ReminderSpec(delay, texts.MORNING_FOLLOWUP, "alarm", False, extra={"ack_button": True, "skip_if_user_wrote_since": now.isoformat()})
        result = await self.reminders.schedule(tenant.id, tenant.timezone, spec, thread_id=thread_id, persona_id=persona_id, now=now)
        if not isinstance(result, Scheduled):
            log.info("no morning follow-up for tenant %s: %s", tenant.id, result)

    # ------------------------------------------------------------ the boundary

    async def _guarded(self, tenant: TenantInfo, thread_id: int | None, work: Callable[[], Awaitable[None]]) -> None:
        async with self._locks.hold((tenant.id, thread_id)):
            try:
                await work()
            except TopicCreationError as exc:
                log.error("topic creation refused for tenant %s: %s", tenant.id, exc)
                await self._notify(tenant, thread_id, texts.TOPIC_FAILED)
            except Exception:  # noqa: BLE001 - the boundary: one bad message must never stop the polling loop
                log.exception("failed to process a message of tenant %s", tenant.id)
                await self._notify(tenant, thread_id, texts.FAILURE)
                error_handler.report_crash_soon(sys.exc_info()[1], where="bot:message", settings=self.settings, bot=self.bot)  # the admin gets the traceback as a file

    async def _notify(self, tenant: TenantInfo, thread_id: int | None, text: str) -> None:
        """Best effort: if Telegram is the thing that is down, this fails too - then it is only logged."""
        try:
            await self._post(self._chat_id(tenant), thread_id, text)
        except Exception:  # noqa: BLE001
            log.exception("could not deliver the notice to tenant %s", tenant.id)

    @staticmethod
    def _chat_id(tenant: TenantInfo) -> int:
        if tenant.forum_chat_id is None:
            raise RuntimeError(f"tenant {tenant.id} has no forum_chat_id")
        return tenant.forum_chat_id

    # ------------------------------------------------------------ use cases

    async def _reply(self, inc: Incoming) -> None:
        tenant, chat_id = inc.tenant, self._chat_id(inc.tenant)
        await self._typing(chat_id, inc.thread_id)

        persona: Persona | None = None
        if inc.thread_id is not None:
            async with self.db.tenant_session(tenant.id) as session:
                persona = await personas.get_by_thread(session, inc.thread_id)
            if persona is None:
                await self._send(chat_id, inc.thread_id, texts.UNKNOWN_TOPIC)
                return

        stored = await self._store_incoming(inc, None if persona is None else persona.id)
        if stored.replay and await self._already_answered(tenant.id, stored):
            log.info("update %s of tenant %s was already answered; skipped", inc.telegram_message_id, tenant.id)
            return
        if persona is not None and persona.code == FEEDBACK_CODE:  # the feedback channel: forwarded to the author, the model is not asked
            await self._feedback(inc, persona)
            return
        if self.settings.reminders_enabled:  # "я встал" / "я тут" stops an alarm that is waiting for an answer (fail-open: the button works anyway)
            await self.reminders.ack_by_text(tenant.id, inc.text)

        moved = False
        if inc.thread_id is None:  # "General": maybe a new activity
            if stored.persona_id is not None:  # replay after routing: the decision was made, do not ask the model again
                async with self.db.tenant_session(tenant.id) as session:
                    persona = await personas.get_by_id(session, stored.persona_id)
                moved = persona is not None
            else:
                route = await self._route(tenant, inc.content)
                if route.spec is not None:
                    persona, _ = await self._ensure_topic(tenant, route.spec, adopt=stored.id)
                    moved = True

        request = summary.detect(inc.text) if self.settings.summary_enabled and inc.attachment is None else None
        result: Answer | None = None
        if request is not None:  # a link AND a request for a summary: a reply with NO functions at all (see ``_summarize``)
            result = await self._summarize(tenant, persona, request)
        if result is None:
            memories = await self._recall(tenant, inc.text)  # Tier 2; never raises: a failure degrades to "unavailable"
            prompt = await build_prompt(
                self.db,
                user_id=tenant.id,
                timezone=tenant.timezone,
                persona_id=None if persona is None else persona.id,
                persona_prompt=CONCIERGE_PROMPT if persona is None else persona.system_prompt,
                settings=self.settings,
                memories=memories,
                mode=PersonaMode.normal.value if persona is None else persona.mode,
            )
            await self._store_attachment(tenant, stored.id, inc)
            prompt = self._with_attachment(prompt, inc)
            prompt = self._with_voice(prompt, inc)
            result = await self._answer(  # no DB transaction is open here
                tenant, prompt, persona=persona, turn=UserTurn(stored.id, inc.text, image=inc.prepared is not None and inc.prepared.content_kind == "image"),
                untrusted=inc.prepared is not None,
                slow=inc.voice is not None,  # the recording travels with the turn: the model needs the long deadline
            )
        reply_thread = None if persona is None else persona.telegram_thread_id
        first_id = await self._send(chat_id, reply_thread, result.text)
        await self._store_assistant(
            tenant.id, None if persona is None else persona.id, result.text, first_id, third_party_data=result.third_party
        )
        for card in result.cards:  # the cards come from code, after the reply: "put it into the calendar?" is never the model's words
            card_id = await self._send_quietly(tenant, reply_thread, card.text, markup=card.markup)
            if card_id is not None and card.auto_delete_seconds:  # a card that removes itself (the memory card): a background timer, see ``Janitor``
                self.janitor.delete_later(chat_id, card_id, card.auto_delete_seconds)
        delivery = Delivery(tenant, reply_thread, persona)
        for action in result.after_reply:  # after the reply: a failed action (a topic rename) can never spoil what the user reads
            await action(delivery)
        if moved and persona is not None and persona.telegram_thread_id is not None:
            await self._send_quietly(
                tenant,
                inc.thread_id,
                texts.MOVED.format(title=persona.title),
                markup=self._topic_button(chat_id, persona.telegram_thread_id, persona.title),
            )

    async def set_mode(self, tenant: TenantInfo, thread_id: int | None, mode: PersonaMode) -> None:
        """/dansk and /normal: the language mode of the topic the command was written in. The confirmation is a fixed text written by code (Danish
        when the immersion is switched on, the standard language when it is switched off); the model never says it. The main chat and the Settings
        topic have no language mode: they get a hint. Not before onboarding is done."""
        if tenant.onboarding_state is not OnboardingState.done:
            await self._guarded(tenant, thread_id, lambda: self._onboarding_prompt(tenant, thread_id))
            return

        async def switch() -> None:
            persona: Persona | None = None
            if thread_id is not None:
                async with self.db.tenant_session(tenant.id) as session:
                    persona = await personas.get_by_thread(session, thread_id)
                    if persona is not None and persona.code not in personas.SYSTEM_CODES:
                        await personas.set_mode(session, persona.id, mode)
                    else:
                        persona = None
            if persona is None:
                await self._send(self._chat_id(tenant), thread_id, texts.MODE_NO_TOPIC)
                return
            confirmation = texts.MODE_DANSK_ON if mode is PersonaMode.immersion_da else texts.MODE_NORMAL_ON
            await self._send(self._chat_id(tenant), thread_id, confirmation)

        await self._guarded(tenant, thread_id, switch)

    async def open_app(self, tenant: TenantInfo, thread_id: int | None) -> None:
        """/app: the Mini App button, in whatever topic the user asked. Not before onboarding (the app needs a profile)."""
        if tenant.onboarding_state is not OnboardingState.done:
            await self._guarded(tenant, thread_id, lambda: self._onboarding_prompt(tenant, thread_id))
        else:
            await self._guarded(
                tenant,
                thread_id,
                lambda: self._send(self._chat_id(tenant), thread_id, texts.APP_LINK, markup=self._app_button()),
            )

    async def open_feedback(self, tenant: TenantInfo, thread_id: int | None) -> None:
        """/feedback: the way to the feedback channel (made at registration; made now for a user who registered before it existed). Not before onboarding is done."""
        if tenant.onboarding_state is not OnboardingState.done:
            await self._guarded(tenant, thread_id, lambda: self._onboarding_prompt(tenant, thread_id))
            return

        async def show() -> None:
            persona, created = await self._ensure_topic(tenant, FEEDBACK_PERSONA, adopt=None)
            if created:
                await self._send_quietly(tenant, persona.telegram_thread_id, texts.FEEDBACK_GREETING)
            assert persona.telegram_thread_id is not None
            await self._send(
                self._chat_id(tenant), thread_id, texts.FEEDBACK_LINK, markup=self._topic_button(self._chat_id(tenant), persona.telegram_thread_id, persona.title)
            )

        await self._guarded(tenant, thread_id, show)

    async def rename_topic(self, tenant: TenantInfo, persona_id: uuid.UUID, new_title: str) -> RenameResult:
        """Rename the forum topic of one of the tenant's personas. Every refusal of Telegram is a RESULT, never an exception (the only thing
        that can still raise is the database failing AFTER Telegram accepted the name: that is not swallowed, because "renamed" would be
        a lie about our record; asking again heals it, see ``_rename_refused`` / TOPIC_NOT_MODIFIED).

        Only topics this bot created can be reached: a persona's ``telegram_thread_id`` is set solely by ``_ensure_topic`` after our own
        ``createForumTopic``, and a topic the user made by hand has no persona. The Bot API says "bot must be an administrator with the
        can_manage_topics right, unless it is the creator of the topic" - and it exposes nothing that tells who created a topic, so the
        guard is "we made it" (our own bookkeeping) plus catching what Telegram says anyway. The new name is stored ONLY after Telegram
        accepted it, so the database never claims a name the topic does not have."""
        title = personas.clean_line(new_title, personas.MAX_TITLE_CHARS)
        if not title:
            return RenameResult.invalid_title
        title = personas.normalize_title(title, title)
        async with self.db.tenant_session(tenant.id) as session:
            persona = await personas.get_by_id(session, persona_id)
        if persona is None or not persona.is_active or persona.telegram_thread_id is None:
            return RenameResult.unknown_topic
        if persona.title == title:
            return RenameResult.unchanged
        try:
            await self.bot.edit_forum_topic(
                chat_id=self._chat_id(tenant), message_thread_id=persona.telegram_thread_id, name=title
            )
        except TelegramForbiddenError as exc:
            log.warning("renaming topic %s of tenant %s refused: %s", persona.telegram_thread_id, tenant.id, exc)
            return RenameResult.no_rights
        except TelegramBadRequest as exc:
            return await self._rename_refused(tenant, persona, title, str(exc))
        except TelegramAPIError:  # a network error, a flood limit: nothing changed, the user can ask again
            log.warning("renaming topic %s of tenant %s failed", persona.telegram_thread_id, tenant.id, exc_info=True)
            return RenameResult.failed
        async with self.db.tenant_session(tenant.id) as session:
            await personas.set_title(session, persona.id, title, source="bot")
        return RenameResult.renamed

    def _rename_offered(self, persona: Persona | None) -> bool:
        """Is the model offered ``rename_current_topic`` for this topic? Only in a topic of the bot's own with the cooldown passed (a pre-check
        on the loaded row; ``claim_rename`` is the authority). Inside the cooldown the function is simply not there."""
        cfg = self.settings
        return (
            cfg.topic_rename_enabled
            and persona is not None
            and isinstance(self.chat_llm, ToolLLM)
            and personas.rename_allowed(
                persona,
                bot_cooldown=timedelta(hours=cfg.topic_rename_cooldown_hours),
                user_cooldown=timedelta(days=cfg.topic_rename_user_cooldown_days),
                now=datetime.now(UTC),
            )
        )

    async def _apply_rename(self, tenant: TenantInfo, persona: Persona, title: str, thread_id: int | None) -> None:
        """Rename the topic the model asked for. Never raises (except cancellation): the reply has already been delivered.

        Order: (1) the right is CLAIMED in the database (the cooldown; a race loses here); (2) Telegram is asked through ``rename_topic``;
        (3) only a rename that really happened is announced, by a fixed text. A refusal of Telegram hands the claim back (nothing changed, the
        next message may try again); a failure whose outcome is unknown (the database failed after Telegram accepted the name) keeps the claim,
        so the bot does not hammer Telegram - the cooldown then ends and the next attempt heals the record."""
        cfg = self.settings
        try:
            async with self.db.tenant_session(tenant.id) as session:
                claim = await personas.claim_rename(
                    session,
                    persona.id,
                    bot_cooldown=timedelta(hours=cfg.topic_rename_cooldown_hours),
                    user_cooldown=timedelta(days=cfg.topic_rename_user_cooldown_days),
                )
            if claim is None:
                log.info("autonomous rename of topic %r of tenant %s not allowed now (cooldown or topic state)", persona.code, tenant.id)
                return
            try:
                result = await self.rename_topic(tenant, persona.id, title)
            except Exception:  # noqa: BLE001 - unknown outcome: keep the claim (see above)
                log.warning("autonomous rename of topic %r of tenant %s failed after Telegram; the claim is kept", persona.code, tenant.id, exc_info=True)
                return
            if result is not RenameResult.renamed:
                async with self.db.tenant_session(tenant.id) as session:
                    await personas.release_rename(session, persona.id, claim)
                log.info("autonomous rename of topic %r of tenant %s was not applied: %s", persona.code, tenant.id, result)
                return
        except Exception:  # noqa: BLE001 - a courtesy feature must never break the update (CancelledError is not an Exception)
            log.warning("autonomous rename of topic %r of tenant %s failed", persona.code, tenant.id, exc_info=True)
            return
        await self._send_quietly(tenant, thread_id, texts.TOPIC_RENAMED.format(title=title))

    async def _rename_refused(self, tenant: TenantInfo, persona: Persona, title: str, reason: str) -> RenameResult:
        """Telegram answered 400 to editForumTopic: tell the cases apart by what it says (the Bot API has no error codes for this)."""
        low = reason.lower().replace("_", " ")  # Telegram writes both "TOPIC_NOT_MODIFIED" and "message thread not found"
        if "not modified" in low:  # the topic already has this name (someone renamed it by hand): only our record was behind
            async with self.db.tenant_session(tenant.id) as session:
                await personas.set_title(session, persona.id, title)
            return RenameResult.unchanged
        if "rights" in low or "administrator" in low or "can manage topics" in low:
            log.warning("renaming topic %s of tenant %s refused for lack of rights: %s", persona.telegram_thread_id, tenant.id, reason)
            return RenameResult.no_rights
        if "topic id invalid" in low or "thread not found" in low or "topic not found" in low or "topic deleted" in low:
            log.warning("topic %s of tenant %s does not exist any more: %s", persona.telegram_thread_id, tenant.id, reason)
            return RenameResult.gone
        log.warning("renaming topic %s of tenant %s failed: %s", persona.telegram_thread_id, tenant.id, reason)
        return RenameResult.failed

    async def sync_topic_title(self, tenant: TenantInfo, thread_id: int | None, name: str | None) -> bool:
        """The user renamed a topic by hand (the service message ``forum_topic_edited``): keep ``personas.title`` equal to what they see,
        because the router shows the model these titles and ``MOVED`` quotes them. The user's own wording wins (no emoji is forced on
        it). Not a user-facing action: never raises, never writes to the chat. True = the stored title changed."""
        if thread_id is None or name is None:  # the main chat (it has no persona), or an edit of the icon only
            return False
        title = personas.clean_line(name, MAX_TOPIC_NAME_CHARS)
        if not title:
            return False
        try:
            async with self.db.tenant_session(tenant.id) as session:
                persona = await personas.get_by_thread(session, thread_id)
                if persona is None or persona.title == title:
                    return False
                await personas.set_title(session, persona.id, title, source="user")  # a name typed by hand is respected for longer
            return True
        except Exception:  # noqa: BLE001 - housekeeping: a failure only leaves the old title until the next edit
            log.warning("could not sync the title of topic %s of tenant %s", thread_id, tenant.id, exc_info=True)
            return False

    def _app_button(self) -> InlineKeyboardMarkup:
        """A plain ``url`` button with the Direct Link: ``web_app`` buttons are private-chat-only in the Bot API, and this
        bot lives in a group. Telegram opens the Mini App with signed launch data for whoever pressed the button."""
        button = InlineKeyboardButton(text=texts.OPEN_APP, url=self.settings.mini_app_direct_link)
        return InlineKeyboardMarkup(inline_keyboard=[[button]])

    @staticmethod
    def _topic_button(chat_id: int, thread_id: int, title: str) -> InlineKeyboardMarkup | None:
        url = texts.topic_url(chat_id, thread_id)
        if url is None:
            return None
        button = InlineKeyboardButton(text=texts.OPEN_TOPIC.format(title=texts.shorten(title, 40)), url=url)
        return InlineKeyboardMarkup(inline_keyboard=[[button]])

    async def _onboard(self, inc: Incoming) -> None:
        """One message of the interview. The message is stored first (as always); the state machine in ``onboarding``
        decides the reply; the reply is SENT first and the new state saved after it, so a failed send leaves the
        interview where the user last saw it (the question is simply asked again)."""
        tenant = inc.tenant
        await self._typing(self._chat_id(tenant), inc.thread_id)
        if inc.thread_id is not None:  # a message in the feedback channel is feedback even while the interview is still running
            async with self.db.tenant_session(tenant.id) as session:
                topic = await personas.get_by_thread(session, inc.thread_id)
            if topic is not None and topic.code == FEEDBACK_CODE:
                stored = await self._store_incoming(inc, topic.id)
                if not (stored.replay and await self._already_answered(tenant.id, stored)):
                    await self._feedback(inc, topic)
                return
        stored = await self._store_incoming(inc, None)
        if stored.replay and await self._already_answered(tenant.id, stored):
            log.info("update %s of tenant %s was already answered; skipped", inc.telegram_message_id, tenant.id)
            return
        async with self.db.tenant_session(tenant.id) as session:
            before = await onboarding.load_progress(session, tenant.id)
        if before.state is OnboardingState.done:  # finished between the middleware and now
            log.info("tenant %s finished onboarding concurrently; message not interviewed", tenant.id)
            return
        if self.settings.onboarding_light:
            await self._apply_light(tenant, inc.thread_id, before, await onboarding.next_light_turn(self._ask_onboarding, before, inc.text))
            return
        turn = await onboarding.next_turn(self._ask_onboarding, before, inc.text)
        await self._apply_turn(tenant, inc.thread_id, before, turn)

    async def _start_interview(self, tenant: TenantInfo) -> None:
        async with self.db.tenant_session(tenant.id) as session:
            before = await onboarding.load_progress(session, tenant.id)
        if before.state is not OnboardingState.pending:  # already asked (or finished): never ask the first question twice
            return
        if self.settings.onboarding_light:
            await self._apply_light(tenant, None, before, await onboarding.next_light_turn(self._ask_onboarding, before, ""))
            return
        await self._apply_turn(tenant, None, before, await onboarding.next_turn(self._ask_onboarding, before, ""))

    async def _apply_light(
        self, tenant: TenantInfo, thread_id: int | None, before: onboarding.Progress, turn: onboarding.LightTurn
    ) -> None:
        """The light onboarding: ask the city once; when it is settled (a city, "skip" or two failures -> UTC) the tenant is let in and gets only the
        Settings topic: every other topic appears later, as the conversation develops."""
        chat_id = self._chat_id(tenant)
        if turn.finished:
            async with self.db.tenant_session(tenant.id) as session:
                written = await onboarding.finalize_light(session, tenant.id, before, turn.place)
            if not written:
                log.warning("onboarding of tenant %s moved concurrently; nothing was finalized", tenant.id)
                return
            await self._store_assistant(tenant.id, None, turn.reply, await self._send(chat_id, thread_id, turn.reply))
            admitted = replace(
                tenant, timezone="UTC" if turn.place is None else turn.place["timezone"], onboarding_state=OnboardingState.done
            )
            await self._ensure_settings_topic(admitted)
            return
        first_id = await self._send(chat_id, thread_id, turn.reply)
        async with self.db.tenant_session(tenant.id) as session:
            if not await onboarding.save_progress(session, tenant.id, before, turn.progress):
                log.warning("onboarding of tenant %s moved concurrently; the new state was not saved", tenant.id)
        await self._store_assistant(tenant.id, None, turn.reply, first_id)

    async def _ensure_settings_topic(self, tenant: TenantInfo) -> None:
        """The Settings topic (the entry to the Mini App), with its welcome. The only topic every user gets."""
        persona, created = await self._ensure_topic(tenant, SETTINGS_PERSONA, adopt=None)
        if created:
            await self._send_quietly(tenant, persona.telegram_thread_id, texts.SETTINGS_GREETING, markup=self._app_button())

    async def _last_main_reply(self, tenant: TenantInfo) -> str | None:
        """The text of the bot's latest message in the main chat (``create_topic`` is allowed only after the bot offered a topic there)."""
        async with self.db.tenant_session(tenant.id) as session:
            return (
                await session.execute(
                    select(Message.content)
                    .where(Message.persona_id.is_(None), Message.role == MessageRole.assistant)
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()

    async def ensure_system_topics(self, tenant: TenantInfo) -> None:
        """The feedback channel ("Ideas and feedback") with its welcome. Idempotent: an existing topic is re-used and gets no second welcome. Never raises
        (except cancellation): registration and onboarding must not fail because a topic could not be made - ``/setup`` completes it later."""
        try:
            async with self._locks.hold((tenant.id, "system-topics")):  # overlapping registration events must not make the topic twice (Telegram would keep an orphan)
                persona, created = await self._ensure_topic(tenant, FEEDBACK_PERSONA, adopt=None)
        except TopicCreationError as exc:
            log.warning("the feedback topic of tenant %s could not be made: %s", tenant.id, exc)
            return
        except Exception:  # noqa: BLE001 - a courtesy: the user can still use the bot
            log.warning("the feedback topic of tenant %s failed", tenant.id, exc_info=True)
            return
        if created:
            await self._send_quietly(tenant, persona.telegram_thread_id, texts.FEEDBACK_GREETING)

    async def adopt_system_topics(self, tenant: TenantInfo, *, settings_thread: int | None, feedback_thread: int | None) -> None:
        """A group the userbot made already has the topics Settings and Ideas-and-feedback: bind them as the tenant's personas and send their welcomes.
        A topic the userbot could not make (``None``) is made by the bot: the feedback one always, Settings only for a tenant whose interview is over
        (a new one gets it when the interview ends, as before). Idempotent; never raises (except cancellation): registration must not fail over a topic."""
        for spec, thread, greeting, markup in (
            (SETTINGS_PERSONA, settings_thread, texts.SETTINGS_GREETING, self._app_button()),
            (FEEDBACK_PERSONA, feedback_thread, texts.FEEDBACK_GREETING, None),
        ):
            if thread is None and spec is SETTINGS_PERSONA and tenant.onboarding_state is not OnboardingState.done:
                continue
            try:
                async with self._locks.hold((tenant.id, "system-topics")):
                    persona, created = await self._ensure_topic(tenant, spec, adopt=None, existing_thread=thread)
            except Exception:  # noqa: BLE001 - a courtesy: /setup completes it later
                log.warning("the topic %r of tenant %s could not be set up", spec.code, tenant.id, exc_info=True)
                continue
            if created:
                await self._send_quietly(tenant, persona.telegram_thread_id, greeting, markup=markup)

    async def _feedback(self, inc: Incoming, persona: Persona) -> None:
        """A message in the feedback topic: NOT answered by the model but handed to the project's author in a private message. The user is thanked
        only AFTER the author's message was sent (a failure raises: the boundary says ``FAILURE`` and nothing is acknowledged). Neither the message nor the
        thanks becomes a memory of the user (the GC skips this topic)."""
        tenant, cfg = inc.tenant, self.settings
        chat_id = self._chat_id(tenant)
        text = inc.text.strip()
        if not text:
            await self._send(chat_id, inc.thread_id, texts.FEEDBACK_EMPTY)
            return
        async with self.db.tenant_session(tenant.id) as session:
            telegram_id = (await session.execute(select(User.telegram_user_id).where(User.id == tenant.id))).scalar_one()
            character = (
                await session.execute(select(CharacterProfile.character_name).where(CharacterProfile.user_id == tenant.id))
            ).scalar_one_or_none()
        card = texts.FEEDBACK_CARD.format(
            username=f"@{inc.username}" if inc.username else texts.FEEDBACK_NO_USERNAME,
            tg_id=telegram_id,
            character=character or texts.FEEDBACK_NO_NAME,
            text=text if len(text) <= cfg.feedback_max_chars else text[: cfg.feedback_max_chars - 1].rstrip() + "…",  # the line breaks of the user are kept
        )
        await self._bounded(self.bot.send_message(chat_id=cfg.feedback_admin_telegram_id, text=card), "feedback forwarding")
        first_id = await self._send(chat_id, inc.thread_id, texts.FEEDBACK_ACCEPTED)
        await self._store_assistant(tenant.id, persona.id, texts.FEEDBACK_ACCEPTED, first_id, third_party_data=True)

    async def _create_topic_allowed(self, tenant: TenantInfo) -> bool:
        """May the model be offered ``create_topic`` now? Not past the cap of the bot's own topics, not inside the cooldown since the newest of them
        (``title_changed_at`` is stamped when a topic is bound). Settings does not count."""
        cfg = self.settings
        async with self.db.tenant_session(tenant.id) as session:
            mine = [p for p in await personas.list_active(session) if p.code not in SYSTEM_CODES and p.telegram_thread_id is not None]
        if len(mine) >= cfg.create_topic_max_active:
            return False
        stamps = [p.title_changed_at for p in mine if p.title_changed_at is not None]
        return not stamps or datetime.now(UTC) - max(stamps) >= timedelta(hours=cfg.create_topic_cooldown_hours)

    async def _apply_create_topic(self, tenant: TenantInfo, spec: PersonaSpec, thread_id: int | None) -> None:
        """Make the topic the model asked for, AFTER the reply was delivered. Never raises (except cancellation): a refusal of Telegram costs the topic,
        not the reply; the announcement is a fixed text with a link button."""
        try:
            persona, created = await self._ensure_topic(tenant, spec, adopt=None)
        except TopicCreationError as exc:
            log.warning("create_topic of tenant %s refused: %s", tenant.id, exc)
            await self._send_quietly(tenant, thread_id, texts.TOPIC_FAILED)
            return
        except Exception:  # noqa: BLE001 - a courtesy feature must never break the update
            log.warning("create_topic %r of tenant %s failed", spec.code, tenant.id, exc_info=True)
            return
        if created and persona.telegram_thread_id is not None:
            await self._send_quietly(
                tenant,
                thread_id,
                texts.TOPIC_CREATED.format(title=persona.title),
                markup=self._topic_button(self._chat_id(tenant), persona.telegram_thread_id, persona.title),
            )

    async def _onboarding_prompt(self, tenant: TenantInfo, thread_id: int | None) -> None:
        """/setup before onboarding: start the interview, or repeat the question the user is on."""
        async with self.db.tenant_session(tenant.id) as session:
            before = await onboarding.load_progress(session, tenant.id)
        if before.state is OnboardingState.done:
            return
        if self.settings.onboarding_light:  # the light path has one question: repeat it (or open the conversation)
            turn = await onboarding.next_light_turn(self._ask_onboarding, before, "") if before.state is OnboardingState.pending else None
            await self._send(
                self._chat_id(tenant), thread_id, onboarding.LIGHT_INTRO if turn is None else turn.reply
            )
            if turn is not None:
                async with self.db.tenant_session(tenant.id) as session:
                    await onboarding.save_progress(session, tenant.id, before, turn.progress)
            return
        if before.state is OnboardingState.pending:
            turn = await onboarding.next_turn(self._ask_onboarding, before, "")
        elif before.step == onboarding.CONFIRM_STEP:
            turn = onboarding.Turn(onboarding.render_summary(before.answers), before)
        else:
            turn = onboarding.Turn(onboarding.question_text(before.step), before)
        await self._apply_turn(tenant, thread_id, before, turn)

    async def _apply_turn(
        self, tenant: TenantInfo, thread_id: int | None, before: onboarding.Progress, turn: onboarding.Turn
    ) -> None:
        chat_id = self._chat_id(tenant)
        if turn.finished:
            async with self.db.tenant_session(tenant.id) as session:
                written = await onboarding.finalize(session, tenant.id, before)
            if not written:
                log.warning("onboarding of tenant %s moved concurrently; nothing was finalized", tenant.id)
                return
            text = onboarding.DONE.format(name=before.answers["name"]["address_as"])
            await self._store_assistant(tenant.id, None, text, await self._send(chat_id, thread_id, text))
            admitted = replace(
                tenant, timezone=before.answers["place"]["timezone"], onboarding_state=OnboardingState.done
            )
            await self._setup(admitted, thread_id)  # profile is saved; a topic failure only needs a later /setup
            return
        first_id = await self._send(chat_id, thread_id, turn.reply)
        async with self.db.tenant_session(tenant.id) as session:
            if not await onboarding.save_progress(session, tenant.id, before, turn.progress):
                log.warning("onboarding of tenant %s moved concurrently; the new state was not saved", tenant.id)
        await self._store_assistant(tenant.id, None, turn.reply, first_id)

    async def _ask_onboarding(self, step: onboarding.Step, text: str) -> str:
        return await self._bounded(
            self.router_llm.generate_json(
                model=self.settings.gemini_onboarding_model,
                system=step.system,
                prompt=onboarding.render_prompt(step, text),
                schema=step.schema,
            ),
            "onboarding parser",
        )

    async def _setup(self, tenant: TenantInfo, thread_id: int | None) -> None:
        """Settings topic (always) + topics from the user's goals (once).

        There is no hardcoded list: while the user has no topics besides Settings, the goals (Core facts ``goal.N`` from the
        interview) go through the model, which proposes 3-4 titled topics; the code validates them. Idempotent: once
        any such topic exists the model is not asked again (a second run would only produce different names) - /setup then
        merely completes topics whose creation failed earlier. Persona rows of the whole plan are written BEFORE the first
        Telegram call, so a refused ``createForumTopic`` loses nothing: the rerun finds the rows and finishes the job."""
        created: list[str] = []
        settings_persona, settings_created = await self._ensure_topic(tenant, SETTINGS_PERSONA, adopt=None)
        if settings_created:
            created.append(SETTINGS_PERSONA.title)
            await self._send_quietly(  # the welcome of the new topic: the entry to the Mini App
                tenant,
                settings_persona.telegram_thread_id,
                texts.SETTINGS_GREETING,
                markup=self._app_button(),
            )

        async with self.db.tenant_session(tenant.id) as session:
            existing = [p for p in await personas.list_active(session) if p.code not in SYSTEM_CODES]
            goals = [] if existing else await personas.load_goals(session)
        if existing:
            specs = [PersonaSpec(p.code, p.title, "") for p in existing if p.telegram_thread_id is None]
        elif not goals:
            await self._send(self._chat_id(tenant), thread_id, texts.SETUP_NO_GOALS)
            return
        else:
            specs = await self._bounded(
                topics.plan_topics(self.router_llm, model=self.settings.gemini_onboarding_model, goals=goals),
                "topic planner",
            )
            if not specs:
                await self._send(self._chat_id(tenant), thread_id, texts.SETUP_NO_TOPICS)
                return
            async with self.db.tenant_session(tenant.id) as session:
                for spec in specs:
                    await personas.ensure_persona(session, tenant.id, spec)
        for spec in specs:
            persona, was_created = await self._ensure_topic(tenant, spec, adopt=None)
            if was_created:
                created.append(persona.title)
        text = texts.SETUP_DONE.format(summary=", ".join(created)) if created else texts.SETUP_NOTHING_NEW
        await self._send(self._chat_id(tenant), thread_id, text)

    # ------------------------------------------------------------ steps

    async def _store_incoming(self, inc: Incoming, persona_id: uuid.UUID | None) -> Stored:
        async with self.db.tenant_session(inc.tenant.id) as session:
            fresh = (
                await session.execute(
                    pg_insert(Message)
                    .values(
                        user_id=inc.tenant.id,
                        persona_id=persona_id,
                        role=MessageRole.user,
                        content=inc.content,
                        telegram_message_id=inc.telegram_message_id,
                    )
                    .on_conflict_do_nothing(constraint="uq_messages_user_telegram")
                    .returning(Message.id, Message.created_at, Message.persona_id)
                )
            ).one_or_none()
            if fresh is not None:
                return Stored(fresh.id, fresh.created_at, fresh.persona_id, replay=False)
            old = (
                await session.execute(
                    select(Message.id, Message.created_at, Message.persona_id).where(
                        Message.telegram_message_id == inc.telegram_message_id
                    )
                )
            ).one()
            return Stored(old.id, old.created_at, old.persona_id, replay=True)

    async def _already_answered(self, user_id: uuid.UUID, stored: Stored) -> bool:
        """A replayed update is skipped only if the topic already holds an assistant message written after it;
        if the process died before answering, the replay gets its answer (at-least-once)."""
        topic = Message.persona_id.is_(None) if stored.persona_id is None else Message.persona_id == stored.persona_id
        async with self.db.tenant_session(user_id) as session:
            return bool(
                (
                    await session.execute(
                        select(
                            exists().where(
                                Message.role == MessageRole.assistant, topic, Message.created_at >= stored.created_at
                            )
                        )
                    )
                ).scalar()
            )

    async def _route(self, tenant: TenantInfo, text: str) -> routing.Route:
        """Ask the router, then apply the limits CODE keeps on topics: the model's own confidence bar (``routing.validate_decision``), and - for a route
        that would MAKE a topic (a new activity, or an existing persona whose topic is gone) - the same cap and daily cooldown as ``create_topic``.
        A refused route is just General: the user is answered there, nothing is created."""
        cfg = self.settings
        async with self.db.tenant_session(tenant.id) as session:
            active = [p for p in await personas.list_active(session) if p.code not in SYSTEM_CODES]  # a system topic is never a destination
        route = await self._bounded(
            routing.classify(
                self.router_llm,
                model=cfg.gemini_router_model,
                text=text,
                existing={p.code: p.title for p in active},
                min_new=cfg.router_min_confidence,
                min_existing=cfg.router_min_confidence_existing,
            ),
            "topic router",
        )
        if route.spec is None:
            return route
        if any(p.code == route.spec.code and p.telegram_thread_id is not None for p in active):  # a topic the user already has: no creation
            return route
        if not await self._create_topic_allowed(tenant):
            log.info("topic %r for tenant %s not created: the cap (%d) or the cooldown (%d h) holds; answering in General",
                     route.spec.code, tenant.id, cfg.create_topic_max_active, cfg.create_topic_cooldown_hours)  # fmt: skip
            return routing.Route(None, "topic_limit")
        return route

    async def _ensure_topic(
        self, tenant: TenantInfo, spec: PersonaSpec, *, adopt: uuid.UUID | None, existing_thread: int | None = None
    ) -> tuple[Persona, bool]:
        """The persona for ``spec`` WITH a forum topic: created / re-used as needed. Returns (persona, topic_was_created).
        ``existing_thread``: a topic somebody else (the userbot) already made for this persona: it is bound instead of calling ``createForumTopic``
        (and counts as "created": the caller sends its welcome).

        Order: persona row first (tx), then the Telegram call (no tx), then bind + re-file the message (tx). A failed
        createForumTopic leaves a persona without a topic, which the next request simply completes.
        """
        async with self.db.tenant_session(tenant.id) as session:
            persona = await personas.ensure_persona(session, tenant.id, spec)
        thread_id = persona.telegram_thread_id
        created = thread_id is None
        if created:
            thread_id = existing_thread if existing_thread is not None else await self._create_topic(self._chat_id(tenant), persona.title)
        async with self.db.tenant_session(tenant.id) as session:
            if created and not await personas.attach_topic(session, persona.id, thread_id):
                log.error("persona %s got a topic concurrently; the new topic %s is orphaned", persona.id, thread_id)
                created = False
            if adopt is not None:
                await personas.adopt_message(session, adopt, persona.id)
            fresh = await personas.get_by_id(session, persona.id)
        assert fresh is not None and fresh.telegram_thread_id is not None
        return fresh, created

    async def _create_topic(self, chat_id: int, title: str) -> int:
        try:
            topic = await self.bot.create_forum_topic(chat_id=chat_id, name=title[:128])
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            raise TopicCreationError(str(exc)) from exc
        return topic.message_thread_id

    async def _recall(self, tenant: TenantInfo, text: str) -> Memories | None:
        """Tier 2 for one reply: the user's last message -> embedding -> nearest episodes -> LLM-Judge.

        Returns None when no lookup is warranted (off, too short, a command, the tenant has no episodes): the prompt then
        has no memory section. Optional enrichment, so ANY failure (timeout, Gemini, embedding, database) is logged as a
        warning and becomes ``unavailable``; the reply goes on without memory and is told not to claim there is none.
        Runs with no transaction open: the only DB work is the short reads inside ``recall_with_evidence``."""
        cfg = self.settings
        if self.embedder is None or not cfg.recall_enabled:
            return None
        query = text.strip()
        if len(query) < cfg.recall_min_query_chars or query.startswith("/"):
            return None
        started = time.monotonic()
        try:
            recalled = await asyncio.wait_for(
                recall_with_evidence(
                    self.db,
                    self.router_llm,
                    self.embedder,
                    tenant.id,
                    query[: cfg.recall_query_max_chars],
                    judge_model=cfg.gemini_judge_model,
                    top_k=cfg.judge_top_k,
                    max_distance=cfg.recall_max_distance,
                ),
                timeout=cfg.recall_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - degrade, never fail the reply (CancelledError is not an Exception)
            log.warning(
                "episodic recall failed for tenant %s after %.1fs; answering without Tier 2",
                tenant.id, time.monotonic() - started, exc_info=True,
            )  # fmt: skip
            return Memories("unavailable")
        verdict = recalled.result
        log.info(
            "recall tenant=%s verdict=%s reason=%s considered=%d best_distance=%s evidence=%s in %.2fs",
            tenant.id, verdict.verdict, verdict.reason, recalled.considered,
            None if recalled.best_distance is None else round(recalled.best_distance, 4),
            [str(i) for i in verdict.evidence_ids], time.monotonic() - started,
        )  # fmt: skip
        if verdict.reason == "no_episodes":
            return None  # nothing stored yet: there is nothing to claim either way
        return select_memories(recalled.evidence, tenant.timezone, max_chars=cfg.recall_max_chars)

    async def _generate(self, prompt: Prompt, *, max_output_tokens: int | None = None, slow: bool = False) -> str:
        cap = {} if max_output_tokens is None else {"max_output_tokens": max_output_tokens}  # passed only when set: plain replies are not capped
        return await self._bounded(
            self.chat_llm.generate_text(model=self.settings.gemini_chat_model, system=prompt.system, turns=prompt.turns, **cap),
            "chat model",
            timeout=self.settings.voice_llm_timeout_seconds if slow else None,
        )

    async def _answer(
        self,
        tenant: TenantInfo,
        prompt: Prompt,
        *,
        persona: Persona | None = None,
        turn: UserTurn | None = None,
        untrusted: bool = False,
        slow: bool = False,
    ) -> Answer:
        """The reply, whether it was built from ANOTHER person's data (flagged: the GC never remembers such a reply), and what code sends or
        does after it (cards with buttons, a topic rename).

        What the model may DO is the tool registry's business (``bot/tools``): every tool decides by itself whether it is offered to this user
        in this reply (the Diplomat only to a user who has contacts, a rename only in a topic of the bot's own whose cooldown has passed, ...)
        and runs its own calls. With no tool on offer this is exactly ``_generate``.

        When the model calls something, the tools validate and run the calls, then the text is built from parts: a reply the model phrases
        (needed when a question was answered with a contact's data, or a card was made; the model is asked AGAIN, with every tool's note about
        what the system did written into the prompt, and with no function), then fixed texts written by code (a refused question, the
        confirmation of an invitation). What the user reads about an action is never the model's own claim."""
        if not isinstance(self.chat_llm, ToolLLM):
            return Answer(await self._generate(prompt, slow=slow))
        # an attachment's content is data: nothing it says may trigger an action, so only tools that opted in (``untrusted_ok``) are offered
        offers = await self.tools.offer(ToolContext(tenant, persona, turn), untrusted=untrusted)
        if not offers:
            return Answer(await self._generate(prompt, slow=slow))
        offered = replace(prompt, system="\n\n".join([prompt.system, *(offer.section for offer in offers)]))
        reply = await self._bounded(
            self.chat_llm.generate_with_tools(
                model=self.settings.gemini_chat_model,
                system=offered.system,
                turns=offered.turns,
                tools=[spec for offer in offers for spec in offer.specs],
            ),
            "chat model",
            timeout=self.settings.voice_llm_timeout_seconds if slow else None,
        )
        if not reply.calls:
            return Answer(reply.text)
        results = []
        for offer in sorted(offers, key=lambda item: item.run_rank):  # the order of the side effects: the Diplomat, the calendar, the rename, memory
            result = await offer.run(reply.calls)
            if result is not None:
                results.append(result)
        if not results:  # nothing ran (a function we did not offer, or an unusable call)
            # Words written NEXT TO a call of one of OUR functions that did nothing (not offered to this user, or unusable arguments)
            # are not trusted: they may claim an action ("написал Саше", "занёс в календарь") that did not happen, so the reply is
            # phrased again, without functions. A name that is none of ours is just noise: the plain text beside it stands.
            if any(call.name in self.tools.function_names for call in reply.calls):
                return Answer(await self._generate(prompt, slow=slow))
            return Answer(reply.text or await self._generate(prompt, slow=slow))
        parts: list[str] = []
        if any(result.phrase for result in results):
            notes = [note for result in results for note in result.notes]
            caps = [result.output_cap for result in results if result.output_cap]
            parts.append(
                await self._generate(
                    replace(prompt, system="\n\n".join([prompt.system, *notes])), max_output_tokens=min(caps) if caps else None, slow=slow
                )
            )
        parts += [result.text for result in results if result.text]
        if not parts:  # defensive: a tool that ran but asked for neither the model's words nor a text of its own
            parts.append(await self._generate(prompt, slow=slow))
        return Answer(
            "\n\n".join(parts),
            third_party=any(result.third_party for result in results),
            cards=tuple(card for result in results for card in result.cards),
            after_reply=tuple(action for result in results for action in result.after_reply),
        )

    async def _bounded(self, call: Awaitable[T], what: str, *, timeout: float | None = None) -> T:
        """Hard deadline on top of the SDK's own HTTP timeout: a hung socket cannot hold a message hostage. ``timeout`` replaces the ordinary
        ``llm_timeout_seconds`` for work that is slow by nature (a long voice note)."""
        limit = self.settings.llm_timeout_seconds if timeout is None else timeout
        try:
            return await asyncio.wait_for(call, timeout=limit)
        except TimeoutError as exc:
            raise LLMError(f"{what} did not answer within {limit}s") from exc

    async def _store_assistant(
        self,
        user_id: uuid.UUID,
        persona_id: uuid.UUID | None,
        text: str,
        telegram_message_id: int,
        *,
        third_party_data: bool = False,
    ) -> None:
        async with self.db.tenant_session(user_id) as session:
            await session.execute(
                pg_insert(Message)
                .values(
                    user_id=user_id,
                    persona_id=persona_id,
                    role=MessageRole.assistant,
                    content=text,
                    telegram_message_id=telegram_message_id,
                    third_party_data=third_party_data,
                )
                .on_conflict_do_nothing(constraint="uq_messages_user_telegram")
            )

    # ------------------------------------------------------------ Telegram

    async def _send(
        self, chat_id: int, thread_id: int | None, text: str, *, markup: InlineKeyboardMarkup | CardMarkup | None = None
    ) -> int:
        """Send ``text`` (split to Telegram's limit) into the topic; returns the id of the first message sent.
        ``markup`` (inline buttons, or the neutral rows of a ``Card``) goes under the LAST piece."""
        markup = telegram_markup(markup)
        first_id: int | None = None
        pieces = texts.split_message(text)
        for index, piece in enumerate(pieces):
            sent, thread_id = await self._post(chat_id, thread_id, piece, markup=markup if index == len(pieces) - 1 else None)  # a dead topic: the rest follows into General
            if first_id is None:
                first_id = sent.message_id
        if first_id is None:
            raise LLMError("nothing to send: the reply is empty")
        return first_id

    async def _post(
        self, chat_id: int, thread_id: int | None, text: str, *, markup: InlineKeyboardMarkup | None = None
    ) -> tuple[TgMessage, int | None]:
        """ONE ``sendMessage`` into a topic. If Telegram answers ``message thread not found`` (the topic was deleted by hand) the dead binding is
        dropped (``personas.telegram_thread_id = NULL``) and the SAME text goes once into the main chat instead of failing: returns the sent message
        and the thread it really went to (None = the main chat), so a split reply continues there. Exactly one fallback, no retry loop; any other
        error, and an error of the fallback itself, propagates as before (the boundary decides)."""
        try:
            return await self.bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id, reply_markup=markup), thread_id
        except TelegramBadRequest as exc:
            if thread_id is None or not topic_is_gone(str(exc)):
                raise
        log.warning("topic %s of chat %s no longer exists (message thread not found); forgetting it and answering in the main chat", thread_id, chat_id)
        await self._forget_topic(chat_id, thread_id)
        return await self.bot.send_message(chat_id=chat_id, text=text, message_thread_id=None, reply_markup=markup), None

    async def _forget_topic(self, chat_id: int, thread_id: int) -> None:
        """Unbind a deleted forum topic from its persona. Housekeeping on the way to answering: it never raises (a database failure only means the next
        send into the dead topic repeats the cleanup), cancellation excepted."""
        try:
            tenant_id = await self.db.resolve_tenant_id(forum_chat_id=chat_id)
            if tenant_id is None:
                return
            async with self.db.tenant_session(tenant_id) as session:
                if await personas.clear_thread(session, thread_id):
                    log.info("persona of tenant %s lost its deleted topic %s; a new topic is made when it is needed again", tenant_id, thread_id)
        except Exception:  # noqa: BLE001
            log.warning("could not forget the deleted topic %s of chat %s", thread_id, chat_id, exc_info=True)

    async def _send_quietly(
        self, tenant: TenantInfo, thread_id: int | None, text: str, *, markup: InlineKeyboardMarkup | CardMarkup | None = None
    ) -> int | None:
        """For messages whose loss does not matter (the answer was already delivered). Returns the id of the first message sent, None if it failed."""
        try:
            return await self._send(self._chat_id(tenant), thread_id, text, markup=markup)
        except Exception:  # noqa: BLE001
            log.exception("could not deliver a courtesy message to tenant %s", tenant.id)
            return None

    @asynccontextmanager
    async def _keep_typing(self, chat_id: int, thread_id: int | None) -> AsyncIterator[None]:
        """Heartbeat of the "typing..." status for the time the body runs: Telegram drops it after ~5 seconds, so it is renewed every
        ``TYPING_HEARTBEAT_SECONDS`` (4.5) until the block ends - however it ends (the task is cancelled in ``finally``). Cosmetic: a failed
        renewal is only logged at debug level and never reaches the body."""

        async def beat() -> None:
            while True:
                await asyncio.sleep(self.typing_heartbeat_seconds)
                await self._typing(chat_id, thread_id)

        task = asyncio.create_task(beat())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _typing(self, chat_id: int, thread_id: int | None) -> None:
        try:
            await self.bot.send_chat_action(chat_id=chat_id, action="typing", message_thread_id=thread_id)
        except Exception:  # noqa: BLE001 - cosmetic
            log.debug("typing indicator failed", exc_info=True)

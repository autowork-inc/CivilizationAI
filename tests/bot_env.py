"""One tenant wired to the real Orchestrator + real aiogram Dispatcher, with fake Telegram wire and fake Gemini."""

from __future__ import annotations

import itertools
import random
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.types import Update
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_civ.bot.handlers import build_dispatcher
from ai_civ.bot.orchestrator import Incoming, Orchestrator, TenantInfo
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import OnboardingState
from tests.fakes import FakeLLM
from tests.helpers import new_user, owner_exec
from tests.tg_fakes import FakeChatLLM, FakeTelegramSession, make_bot, make_file_update, make_update, make_voice_update

GENERAL_REPLY = {"intent": "general", "code": "", "title": "", "focus": "", "confidence": 0.9}


def route_new(code: str, title: str, focus: str = "help the user with this activity", confidence: float = 0.95) -> dict[str, Any]:
    return {"intent": "new_activity", "code": code, "title": title, "focus": focus, "confidence": confidence}


@dataclass
class BotEnv:
    owner_engine: AsyncEngine
    db: Database
    session: FakeTelegramSession
    bot: Bot
    chat: FakeChatLLM
    router: FakeLLM
    orchestrator: Orchestrator
    dispatcher: Dispatcher
    settings: Settings
    user_id: uuid.UUID
    telegram_user_id: int
    chat_id: int
    tenant: TenantInfo
    _message_ids: Any = field(default_factory=lambda: itertools.count(1))

    def next_message_id(self) -> int:
        return next(self._message_ids)

    def incoming(self, text: str, *, thread_id: int | None = None, message_id: int | None = None) -> Incoming:
        return Incoming(
            tenant=self.tenant,
            thread_id=thread_id,
            telegram_message_id=message_id or self.next_message_id(),
            text=text,
        )

    def update(self, text: str, *, thread_id: int | None = None, **kw: Any) -> Update:
        kw.setdefault("user_id", self.telegram_user_id)
        kw.setdefault("chat_id", self.chat_id)
        kw.setdefault("message_id", self.next_message_id())
        return make_update(text=text, thread_id=thread_id, **kw)

    async def feed(self, text: str, *, thread_id: int | None = None, **kw: Any) -> None:
        await self.dispatcher.feed_update(self.bot, self.update(text, thread_id=thread_id, **kw))

    async def feed_update(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    async def feed_voice(
        self, data: bytes = b"OggS-fake-opus", *, thread_id: int | None = None, file_id: str | None = None, **kw: Any
    ) -> str:
        """The user sends a voice note whose bytes are ``data``; returns its file id."""
        file_id = file_id or f"voice-{uuid.uuid4().hex[:12]}"
        self.session.files[file_id] = data
        kw.setdefault("user_id", self.telegram_user_id)
        kw.setdefault("chat_id", self.chat_id)
        kw.setdefault("message_id", self.next_message_id())
        await self.dispatcher.feed_update(self.bot, make_voice_update(file_id=file_id, thread_id=thread_id, **kw))
        return file_id

    async def feed_file(
        self, data: bytes, *, caption: str | None = None, thread_id: int | None = None, photo: bool = False, **kw: Any
    ) -> str:
        """The user sends a document (or a photo) whose bytes are ``data``; returns its file id. ``kw``: file_name, mime_type, file_size."""
        file_id = f"file-{uuid.uuid4().hex[:12]}"
        self.session.files[file_id] = data
        kw.setdefault("file_size", len(data))
        await self.dispatcher.feed_update(
            self.bot,
            make_file_update(
                file_id=file_id, user_id=self.telegram_user_id, chat_id=self.chat_id, message_id=self.next_message_id(),
                photo=photo, caption=caption, thread_id=thread_id, **kw,
            ),
        )  # fmt: skip
        return file_id

    async def messages(self) -> list[dict[str, Any]]:
        rows = await owner_exec(
            self.owner_engine,
            "SELECT role::text, content, persona_id, telegram_message_id FROM civ.messages WHERE user_id = :u "
            "ORDER BY created_at, id",
            u=self.user_id,
        )
        return [{"role": r[0], "content": r[1], "persona_id": r[2], "telegram_message_id": r[3]} for r in rows]

    async def personas(self) -> list[dict[str, Any]]:
        rows = await owner_exec(
            self.owner_engine,
            "SELECT id, code, title, system_prompt, telegram_thread_id, is_active FROM civ.personas "
            "WHERE user_id = :u ORDER BY code",
            u=self.user_id,
        )
        return [
            {"id": r[0], "code": r[1], "title": r[2], "system_prompt": r[3], "thread_id": r[4], "is_active": r[5]}
            for r in rows
        ]


@dataclass
class BareEnv:
    """The bot stack with NO tenant at all: registration starts from nothing (fake Telegram wire, fake Gemini)."""

    owner_engine: AsyncEngine
    db: Database
    session: FakeTelegramSession
    bot: Bot
    chat: FakeChatLLM
    router: FakeLLM
    orchestrator: Orchestrator
    dispatcher: Dispatcher
    settings: Settings
    users_at_start: int = 0
    _message_ids: Any = field(default_factory=lambda: itertools.count(1))

    async def feed_update(self, update: Update) -> None:
        await self.dispatcher.feed_update(self.bot, update)

    async def say(self, text: str, *, user_id: int, chat_id: int, **kw: Any) -> None:
        kw.setdefault("message_id", next(self._message_ids))
        await self.feed_update(make_update(text=text, user_id=user_id, chat_id=chat_id, **kw))

    async def user_row(self, telegram_user_id: int) -> dict[str, Any] | None:
        rows = await owner_exec(
            self.owner_engine,
            "SELECT id, forum_chat_id, onboarding_state, group_state FROM civ.users WHERE telegram_user_id = :t",
            t=telegram_user_id,
        )
        if not rows:
            return None
        return {"id": rows[0][0], "forum_chat_id": rows[0][1], "onboarding_state": rows[0][2], "group_state": rows[0][3]}

    async def count_users(self) -> int:
        """Tenants created since this environment was built (the test database keeps the rows of earlier tests)."""
        return (await owner_exec(self.owner_engine, "SELECT count(*) FROM civ.users"))[0][0] - self.users_at_start


async def build_bare_env(owner_engine: AsyncEngine, db: Database, **settings_kw: Any) -> BareEnv:
    settings_kw.setdefault("llm_timeout_seconds", 5.0)
    settings_kw.setdefault("rate_limit_enabled", False)  # see build_bot_env
    settings_kw.setdefault("error_reports_enabled", False)
    settings_kw.setdefault("onboarding_light", False)  # the registration tests are about the first question of the interview
    settings = Settings(_env_file=None, **settings_kw)
    bot, session = make_bot()
    chat = FakeChatLLM()
    router = FakeLLM(lambda prompt: GENERAL_REPLY)
    orchestrator = Orchestrator(db=db, router_llm=router, chat_llm=chat, settings=settings, bot=bot)
    return BareEnv(
        owner_engine=owner_engine, db=db, session=session, bot=bot, chat=chat, router=router, orchestrator=orchestrator,
        dispatcher=build_dispatcher(db, orchestrator), settings=settings,
        users_at_start=(await owner_exec(owner_engine, "SELECT count(*) FROM civ.users"))[0][0],
    )  # fmt: skip


async def build_bot_env(
    owner_engine: AsyncEngine, db: Database, *, onboarding_state: str = "done", **settings_kw: Any
) -> BotEnv:
    telegram_user_id = random.randrange(10**9, 10**12)
    chat_id = -int(f"100{random.randrange(10**9, 10**10)}")  # a real supergroup id looks like -100<internal id>
    user_id = await new_user(
        owner_engine,
        tz="Europe/Copenhagen",
        telegram_user_id=telegram_user_id,
        forum_chat_id=chat_id,
        onboarding_state=onboarding_state,
    )
    settings_kw.setdefault("llm_timeout_seconds", 5.0)
    # Quick events are ON by default in production (every user is offered ``create_event``), which routes every reply through
    # ``generate_with_tools``. The many tests that are about something else assert on the plain ``generate_text`` path, so this stack
    # starts with the feature off; ``tests/test_quick_events_bot.py`` (fixture ``quick_env``) turns it on.
    settings_kw.setdefault("quick_events_enabled", False)
    # Likewise for autonomous topic renaming (ON in production): inside a topic it adds a function to every reply, which would move the
    # many older topic tests from ``generate_text`` to ``generate_with_tools``. ``tests/test_topic_rename.py`` (fixture ``rename_env``) turns it on.
    settings_kw.setdefault("topic_rename_enabled", False)
    # Instant memory (ON in production) also puts ``save_fact`` into every reply to a message of 8+ characters; the same reason, the same switch:
    # ``tests/test_save_fact_bot.py`` (fixture ``fact_env``) turns it on.
    settings_kw.setdefault("save_fact_enabled", False)
    # The organic onboarding (ON in production: one question about the city) replaces the 7-question interview that many older tests are about, and
    # ``create_topic`` adds a function in the main chat: both off here; ``tests/test_organic.py`` turns them on.
    settings_kw.setdefault("onboarding_light", False)
    settings_kw.setdefault("create_topic_enabled", False)
    # ``decompose_task`` (ON in production) is offered only on an explicit marker ("разбей на шаги"); the older tests stay on the plain path
    # regardless: ``tests/test_tasks_decomposition.py`` turns it on.
    settings_kw.setdefault("tasks_enabled", False)
    # Flashcards (ON in production): offered only on an explicit request ("сделай карточки"); the older tests stay on the plain path regardless.
    # ``tests/test_spaced_repetition.py`` turns it on.
    settings_kw.setdefault("sr_enabled", False)
    # Link summaries (ON in production): a link + "перескажи" would start a real download; ``tests/test_summarizer.py`` turns it on and fakes the fetch.
    settings_kw.setdefault("summary_enabled", False)
    # Workout tracker (ON in production): offered only on a workout cue + a number; ``tests/test_workout_tracker.py`` turns it on.
    settings_kw.setdefault("workout_enabled", False)
    # Recipes (ON in production): offered on an explicit request or a photo; ``tests/test_recipe_generator.py`` turns it on.
    settings_kw.setdefault("recipes_enabled", False)
    # Smart inventory (ON in production): offered on a purchase / stock message; ``tests/test_smart_inventory.py`` turns it on.
    settings_kw.setdefault("inventory_enabled", False)
    # Geo notes (ON in production): offered on "напомни в магазине ..."; the location handler answers only when on; ``tests/test_geo_notes.py`` turns it on.
    settings_kw.setdefault("geo_enabled", False)
    # Time capsules and the "a year ago" line (ON in production): ``tests/test_time_capsule.py`` turns them on.
    settings_kw.setdefault("capsules_enabled", False)
    settings_kw.setdefault("flashback_enabled", False)
    # Debate (ON in production): offered only on an explicit request; ``tests/test_cognitive_debate.py`` turns it on.
    settings_kw.setdefault("debate_enabled", False)
    # Scheduled reminders / alarms (ON in production): offered on "напомни" / "разбуди" and need Redis + Celery; ``tests/test_reminders.py`` turns them on with fakes.
    settings_kw.setdefault("reminders_enabled", False)
    # The memory card deletes itself after 15 s in production (a background timer); tests start without it, ``tests/test_save_fact_bot.py`` turns it on.
    settings_kw.setdefault("save_fact_card_ttl_seconds", 0.0)
    # Deleting a topic (ON in production): offered on "удали тему"; ``tests/test_topic_policy.py`` turns it on.
    settings_kw.setdefault("delete_topic_enabled", False)
    # ``create_topic`` (when a test turns it on) works as before here: the production default demands the bot's offer + the user's "yes" first
    # (``tests/test_topic_policy.py`` covers that).
    settings_kw.setdefault("create_topic_require_offer", False)
    # The rate limiter needs Redis: off in the stack the other tests use (they would otherwise wait for a connection per message when Redis is
    # not running). ``tests/test_ratelimit.py`` builds its own dispatcher with a limiter on an in-memory fake Redis.
    settings_kw.setdefault("rate_limit_enabled", False)
    settings_kw.setdefault("error_reports_enabled", False)  # a crash file to the admin is one more Bot API call; tests/test_error_handler.py switches it on
    settings = Settings(_env_file=None, **settings_kw)
    bot, session = make_bot()
    chat = FakeChatLLM()
    router = FakeLLM(lambda prompt: GENERAL_REPLY)
    orchestrator = Orchestrator(db=db, router_llm=router, chat_llm=chat, settings=settings, bot=bot)
    return BotEnv(
        owner_engine=owner_engine,
        db=db,
        session=session,
        bot=bot,
        chat=chat,
        router=router,
        orchestrator=orchestrator,
        dispatcher=build_dispatcher(db, orchestrator),
        settings=settings,
        user_id=user_id,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        tenant=TenantInfo(
            id=user_id,
            timezone="Europe/Copenhagen",
            forum_chat_id=chat_id,
            onboarding_state=OnboardingState(onboarding_state),
        ),
    )

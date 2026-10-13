"""Proactivity hooks (Celery), bot start-up wiring and the new settings. No network, no Telegram, no Gemini."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest
from aiogram import Bot, Dispatcher

from ai_civ import tasks
from ai_civ.bot import app as bot_app
from ai_civ.bot.proactive import UnknownTrigger
from ai_civ.celery_app import app
from ai_civ.config import ConfigError, Settings
from ai_civ.runtime import Runtime
from tests.helpers import new_persona, new_user, owner_exec

# ---------------------------------------------------------------- settings and wiring (no database)


def test_module_2_settings_defaults_match_the_requirements():
    s = Settings(_env_file=None)
    assert s.gemini_chat_model == "gemini-3.8-flash"  # the model the requirements name for answers
    assert s.gemini_router_model == "gemini-3.5-flash-lite"
    assert (s.context_hours, s.telegram_bot_token) == (72, None)  # Context Buffer = 72 h; secrets are never defaulted
    assert s.telegram_request_timeout > 0 and s.telegram_polling_timeout > 0 and s.llm_timeout_seconds > 0  # all calls bounded


def test_the_bot_token_never_appears_in_repr():
    s = Settings(_env_file=None, telegram_bot_token="123456789:SECRET-TOKEN-VALUE")  # type: ignore[arg-type]
    assert "SECRET-TOKEN-VALUE" not in repr(s) and "SECRET-TOKEN-VALUE" not in str(s)


def test_start_up_fails_fast_without_a_token():
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        bot_app.build_bot(Settings(_env_file=None))


async def test_the_bot_is_built_with_a_bounded_request_timeout_and_its_session_is_closed():
    settings = Settings(_env_file=None, telegram_bot_token="123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", telegram_request_timeout=12.0)  # type: ignore[arg-type]
    async with bot_app.open_bot(settings) as bot:
        assert isinstance(bot, Bot) and bot.session.timeout == 12.0
    assert bot_app.ALLOWED_UPDATES == ["message", "callback_query", "my_chat_member", "chat_member"]  # only what the handlers use


async def test_a_missing_token_stops_start_up_before_any_connection_is_opened(monkeypatch):
    monkeypatch.setattr(bot_app, "get_settings", lambda: Settings(_env_file=None))

    def must_not_open(with_llm):
        raise AssertionError("the runtime (database / Gemini) must not be opened before the config is valid")

    monkeypatch.setattr(bot_app.runtime, "open_runtime", must_not_open)
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        await bot_app.run_polling()


async def test_run_polling_checks_the_token_then_starts_long_polling_and_closes_the_session(monkeypatch, bot_env):
    env = bot_env
    started: dict = {}

    @asynccontextmanager
    async def fake_runtime(with_llm):
        yield Runtime(db=env.db, settings=env.settings, llm=env.router, embedder=None, chat=env.chat)

    async def fake_start_polling(self, bot, **kwargs):
        started.update(kwargs, dispatcher=self, bot=bot)

    monkeypatch.setattr(bot_app, "get_settings", lambda: env.settings)
    monkeypatch.setattr(bot_app, "build_bot", lambda settings: env.bot)
    monkeypatch.setattr(bot_app.runtime, "open_runtime", fake_runtime)
    monkeypatch.setattr(Dispatcher, "start_polling", fake_start_polling)

    await bot_app.run_polling()

    assert any(call.__api_method__ == "getMe" for call in env.session.calls)  # fail-fast token check before polling
    assert started["bot"] is env.bot and started["allowed_updates"] == ["message", "callback_query", "my_chat_member", "chat_member"]
    assert started["polling_timeout"] == env.settings.telegram_polling_timeout
    assert started["close_bot_session"] is False and env.session.closed  # open_bot() owns the session

def test_proactive_tasks_are_registered_with_the_right_retry_policy():
    assert {"ai_civ.send_proactive", "ai_civ.dispatch_proactive"} <= set(app.tasks)
    send, dispatch = tasks.send_proactive, tasks.dispatch_proactive
    assert send.max_retries == 3 and Exception in send.autoretry_for  # bounded Celery retry, never a loop of ours
    assert UnknownTrigger in send.dont_autoretry_for  # a typo in a trigger key is not retried
    assert not getattr(dispatch, "autoretry_for", ())  # a re-run after a partial fan-out would nudge twice
    # the hook-style dispatcher (explicit trigger + hour) is still not scheduled by default; the per-user schedule is
    assert "ai_civ.dispatch_proactive" not in {e["task"] for e in app.conf.beat_schedule.values()}
    assert "ai_civ.dispatch_scheduled_proactive" in {e["task"] for e in app.conf.beat_schedule.values()}


# ---------------------------------------------------------------- send_proactive_async


def runtime_of(env) -> Runtime:
    return Runtime(db=env.db, settings=env.settings, llm=env.router, embedder=None, chat=env.chat)


async def test_send_proactive_pushes_a_message_for_a_tenant(bot_env):
    env = bot_env
    await new_persona(env.owner_engine, env.user_id, "discipline", thread_id=612, system_prompt="DISCIPLINE")

    done = await tasks.send_proactive_async(runtime_of(env), env.bot, env.user_id, "work_start")

    assert done is True
    assert env.chat.calls[0]["system"].startswith("DISCIPLINE") and "## Proactive mode" in env.chat.calls[0]["system"]
    [pushed] = env.session.sent_to(612)
    assert pushed.chat_id == env.chat_id


async def test_send_proactive_rejects_unknown_triggers_and_skips_unknown_or_unbound_tenants(bot_env, owner_engine):
    env = bot_env
    with pytest.raises(UnknownTrigger):
        await tasks.send_proactive_async(runtime_of(env), env.bot, env.user_id, "no_such_trigger")

    assert await tasks.send_proactive_async(runtime_of(env), env.bot, uuid.uuid4(), "meal_check") is False  # no such tenant
    await owner_exec(owner_engine, "UPDATE civ.users SET forum_chat_id = NULL WHERE id = :u", u=env.user_id)
    assert await tasks.send_proactive_async(runtime_of(env), env.bot, env.user_id, "meal_check") is False  # no forum group
    assert env.session.sent() == [] and env.chat.calls == []


# ---------------------------------------------------------------- dispatch_proactive_async


async def test_dispatch_fans_out_to_tenants_whose_local_clock_shows_the_hour(bot_env, owner_engine, db):
    env = bot_env  # Europe/Copenhagen (UTC+2 in early October)
    new_york = await new_user(owner_engine, tz="America/New_York")  # UTC-4
    now = datetime(2026, 10, 3, 11, 30, tzinfo=UTC)  # Copenhagen 13:30, New York 07:30
    submitted: list[tuple[uuid.UUID, str]] = []

    count = await tasks.dispatch_proactive_async(
        runtime_of(env), lambda user_id, trigger: submitted.append((user_id, trigger)), "meal_check", local_hour=13, now=now
    )

    ids = {user_id for user_id, _ in submitted}
    assert env.user_id in ids and new_york not in ids and count == len(submitted)
    assert {trigger for _, trigger in submitted} == {"meal_check"}

    submitted.clear()
    await tasks.dispatch_proactive_async(runtime_of(env), lambda u, t: submitted.append((u, t)), "meal_check", now=now)
    assert {env.user_id, new_york} <= {u for u, _ in submitted}  # no local_hour: every tenant


async def test_dispatch_validates_the_trigger_before_submitting_anything(bot_env):
    env = bot_env
    submitted: list = []
    with pytest.raises(UnknownTrigger):
        await tasks.dispatch_proactive_async(runtime_of(env), lambda u, t: submitted.append(u), "typo")
    assert submitted == []

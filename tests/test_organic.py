"""v2.0 PR 1.4: the organic onboarding (ONE question - the city - instead of the 7-question interview) and ``create_topic`` (topics appear as the
conversation develops, not from a fixed list). Real PostgreSQL, the real Dispatcher on a fake Telegram wire, fake Gemini."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest

from ai_civ.bot import onboarding, personas, texts
from ai_civ.bot.tools import create_topic
from ai_civ.db.enums import OnboardingState
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_persona, owner_exec

TOKYO = {"city": "Токио", "timezone": "Asia/Tokyo"}
LIGHT = onboarding.Progress(OnboardingState.in_progress, 1, {})


@asynccontextmanager
async def light_env(owner_engine, db, **kw: Any):
    env = await build_bot_env(owner_engine, db, onboarding_state="pending", onboarding_light=True, **kw)
    try:
        yield env
    finally:
        await env.bot.session.close()


@asynccontextmanager
async def topic_env(owner_engine, db, **kw: Any):
    env = await build_bot_env(owner_engine, db, create_topic_enabled=True, **kw)
    try:
        yield env
    finally:
        await env.bot.session.close()


async def user_row(env) -> tuple[Any, ...]:
    return (await owner_exec(env.owner_engine, "SELECT onboarding_state, timezone FROM civ.users WHERE id = :u", u=env.user_id))[0]


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


async def codes(env) -> list[str]:
    return [p["code"] for p in await env.personas()]


def call(code: str = "danish", title: str = "Датский язык", focus: str = "учим датский") -> ToolCall:
    return ToolCall(create_topic.TOOL_NAME, {"code": code, "title": title, "focus": focus})


def model_calls(env, *calls: ToolCall, text: str = "Создал тему!", then: str = "Здорово, давайте учить.") -> None:
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


# ================================================================== the light onboarding (pure)


async def test_the_first_message_only_opens_the_conversation_with_one_question():
    async def never(step, text):
        raise AssertionError("the model is not asked before the first answer")

    turn = await onboarding.next_light_turn(never, onboarding.Progress(OnboardingState.pending, 0, {}), "привет")
    assert turn.reply == onboarding.LIGHT_INTRO and turn.progress.step == 1 and not turn.finished
    assert "анкет" in onboarding.LIGHT_INTRO.lower() and "город" in onboarding.LIGHT_INTRO


async def test_a_city_settles_the_zone_a_skip_or_two_failures_fall_back_to_utc():
    import json

    async def good(step, text):
        return json.dumps(TOKYO)

    async def bad(step, text):
        return json.dumps({"city": "Нигде", "timezone": "UTC"})

    done = await onboarding.next_light_turn(good, LIGHT, "Я из Токио")
    assert done.finished and done.place == TOKYO
    skipped = await onboarding.next_light_turn(bad, LIGHT, " Пропустить! ")
    assert skipped.finished and skipped.place is None
    first = await onboarding.next_light_turn(bad, LIGHT, "Я из Нигде")
    assert not first.finished and first.progress.answers == {"light": {"tries": 1}} and onboarding.LIGHT_RETRY in first.reply
    second = await onboarding.next_light_turn(bad, first.progress, "Я из Нигде")
    assert second.finished and second.place is None and second.reply == onboarding.LIGHT_FALLBACK


# ================================================================== the light onboarding (flow)


async def test_the_light_onboarding_asks_one_question_then_lets_the_user_in(owner_engine, db):
    async with light_env(owner_engine, db) as env:
        env.router.responder = lambda prompt: TOKYO
        await env.feed("Привет")
        assert says(env) == [onboarding.LIGHT_INTRO] and (await user_row(env))[0] == "in_progress" and env.router.calls == []
        await env.feed("Я из Токио")
        assert await user_row(env) == ("done", "Asia/Tokyo")
        assert says(env)[-1] == onboarding.LIGHT_DONE.format(city="Токио", zone="Asia/Tokyo")
        assert await codes(env) == ["settings"]  # the Settings topic only: no topics from a questionnaire
        facts = await owner_exec(env.owner_engine, "SELECT key, value_text FROM civ.core_facts WHERE user_id = :u", u=env.user_id)
        assert [f[0] for f in facts] == ["city"] and len(env.router.calls) == 1  # one parser call in the whole onboarding


async def test_an_unknown_city_twice_means_utc_and_no_city_fact(owner_engine, db):
    async with light_env(owner_engine, db) as env:
        env.router.responder = lambda prompt: {"city": "Нигде", "timezone": "UTC"}
        await env.feed("Привет")
        await env.feed("Я из Нигде")
        assert (await user_row(env))[0] == "in_progress" and onboarding.LIGHT_RETRY in says(env)[-1]
        await env.feed("Я из Нигде")
        assert await user_row(env) == ("done", "UTC") and says(env)[-1] == onboarding.LIGHT_FALLBACK
        assert await owner_exec(env.owner_engine, "SELECT count(*) FROM civ.core_facts WHERE user_id = :u", u=env.user_id) == [(0,)]


async def test_skip_is_enough(owner_engine, db):
    async with light_env(owner_engine, db) as env:
        await env.feed("Привет")
        await env.feed("пропустить")
        assert await user_row(env) == ("done", "UTC") and env.router.calls == []


async def test_a_registered_tenant_is_greeted_once_in_the_main_chat(owner_engine, db):
    async with light_env(owner_engine, db) as env:
        await env.orchestrator.start_onboarding(env.tenant)
        await env.orchestrator.start_onboarding(env.tenant)  # overlapping Telegram events
        assert says(env) == [onboarding.LIGHT_INTRO] and (await user_row(env))[0] == "in_progress"


async def test_the_old_interview_is_still_there_behind_the_switch(owner_engine, db):
    env = await build_bot_env(owner_engine, db, onboarding_state="pending")  # onboarding_light=false in the test stack
    try:
        await env.feed("Привет")
        assert says(env) == [f"{onboarding.INTRO}\n\n{onboarding.question_text(1)}"]
    finally:
        await env.bot.session.close()


# ================================================================== create_topic (pure)


def test_a_valid_call_becomes_a_spec_with_an_emoji_title():
    spec = create_topic.parse_create([call(title="  Датский   язык ")])
    assert spec is not None and (spec.code, spec.focus) == ("danish", "учим датский") and personas.has_emoji(spec.title) and "Датский язык" in spec.title


@pytest.mark.parametrize(
    "bad",
    [
        {"code": "general"}, {"code": "settings"}, {"code": "Danish!"}, {"code": "d"}, {"code": "x" * 41}, {"code": None},
        {"title": ""}, {"title": "x" * 65}, {"title": "‮evil"}, {"title": "!!!"}, {"title": 5},
    ],
)  # fmt: skip
def test_an_unusable_call_is_dropped(bad):
    args = {"code": "danish", "title": "Датский", "focus": "f", **bad}
    assert create_topic.parse_create([ToolCall(create_topic.TOOL_NAME, args)]) is None


def test_a_missing_focus_gets_the_default_and_only_the_first_call_counts():
    spec = create_topic.parse_create([ToolCall("other", {}), ToolCall(create_topic.TOOL_NAME, {"code": "gym", "title": "Зал", "focus": 5}), call("danish")])
    assert spec is not None and spec.code == "gym" and spec.focus == create_topic.DEFAULT_FOCUS


# ================================================================== create_topic (flow)


async def test_it_is_offered_in_the_main_chat_only(owner_engine, db):
    async with topic_env(owner_engine, db) as env:
        await new_persona(env.owner_engine, env.user_id, "sport", thread_id=612)
        await env.feed("Расскажи что-нибудь", thread_id=612)
        assert env.chat.tool_calls == []  # inside a topic: no function
        await env.feed("Я решил выучить датский")
        [offered] = env.chat.tool_calls
        assert [t.name for t in offered["tools"]] == ["create_topic"] and "## Topics" in offered["system"]


async def test_the_topic_is_made_after_the_reply_and_the_announcement_comes_from_code(owner_engine, db):
    async with topic_env(owner_engine, db) as env:
        model_calls(env, call())
        await env.feed("Я решил выучить датский")
        assert await codes(env) == ["danish"]
        [persona] = await env.personas()
        assert persona["thread_id"] is not None and persona["title"].endswith("Датский язык") or "Датский язык" in persona["title"]
        sent = env.session.sent()
        assert [m.text for m in sent][0] == "Здорово, давайте учить." and "Создал тему!" not in says(env)  # the model's claim never goes out
        announcement = sent[-1]
        assert announcement.text == texts.TOPIC_CREATED.format(title=persona["title"]) and announcement.reply_markup.inline_keyboard[0][0].url.endswith(f"/{persona['thread_id']}")
        [phrased] = env.chat.calls
        assert "Topic creation (made by the system)" in phrased["system"]


async def test_a_call_with_an_unusable_code_creates_nothing_and_its_words_are_not_trusted(owner_engine, db):
    async with topic_env(owner_engine, db) as env:
        model_calls(env, call(code="settings"), text="Создал!", then="Понял.")
        await env.feed("Я решил выучить датский")
        assert says(env) == ["Понял."] and await codes(env) == []


async def test_inside_the_cooldown_the_function_is_not_even_offered(owner_engine, db):
    async with topic_env(owner_engine, db) as env:
        model_calls(env, call())
        await env.feed("Я решил выучить датский")
        env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="Окей")
        await env.feed("А ещё я хочу начать ходить в зал")
        assert len(env.chat.tool_calls) == 1  # the second message went the plain way: create_topic is not offered for 6 hours


async def test_the_cap_of_the_bots_own_topics_stops_the_offer(owner_engine, db):
    async with topic_env(owner_engine, db, create_topic_cooldown_hours=0, create_topic_max_active=1) as env:
        model_calls(env, call())
        await env.feed("Я решил выучить датский")
        await env.feed("А ещё я хочу начать ходить в зал")
        assert len(env.chat.tool_calls) == 1 and await codes(env) == ["danish"]


async def test_a_refusal_of_telegram_costs_the_topic_not_the_reply(owner_engine, db):
    async with topic_env(owner_engine, db) as env:
        env.session.fail_on("createForumTopic", TelegramBadRequest, times=1)
        model_calls(env, call())
        await env.feed("Я решил выучить датский")
        assert says(env)[0] == "Здорово, давайте учить." and texts.TOPIC_FAILED in says(env) and texts.FAILURE not in says(env)


async def test_the_feature_is_off_in_the_default_test_stack_and_by_the_switch(owner_engine, db):
    env = await build_bot_env(owner_engine, db)
    try:
        await env.feed("Я решил выучить датский")
        assert env.chat.tool_calls == []
    finally:
        await env.bot.session.close()

"""The Orchestrator end to end: REAL PostgreSQL (RLS as ai_civ_app), real aiogram Bot on a fake wire, fake Gemini.

Covers: reply pipeline and prompt contents, "Gemini outside any DB transaction", timeouts, failure containment (the polling
loop must survive), topic routing / creation from General, replay safety, ordering under concurrency, proactive messages.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from sqlalchemy import text

from ai_civ.bot import texts
from ai_civ.bot.personas import CONCIERGE_PROMPT, SETTINGS_PERSONA
from ai_civ.bot.proactive import get_trigger
from ai_civ.memory.llm import ChatTurn, LLMError
from tests.bot_env import route_new
from tests.helpers import new_fact, new_message, new_persona, owner_exec

FOOD_THREAD = 501


async def food_persona(env, **kw):
    return await new_persona(env.owner_engine, env.user_id, "foodguide", thread_id=FOOD_THREAD, system_prompt="FOODGUIDE PROMPT", **kw)


def roles(messages):
    return [(m["role"], m["content"]) for m in messages]


# ---------------------------------------------------------------- the reply transaction


async def test_a_message_in_a_known_topic_is_answered_in_that_topic(bot_env):
    env = bot_env
    persona = await food_persona(env)

    await env.orchestrator.handle_message(env.incoming("What should I eat?", thread_id=FOOD_THREAD, message_id=7))

    [call] = env.chat.calls
    assert call["model"] == "gemini-3.8-flash"  # GEMINI_CHAT_MODEL
    assert call["system"].startswith("FOODGUIDE PROMPT")
    assert call["turns"] == [ChatTurn("user", "What should I eat?")]
    [reply] = env.session.sent_to(FOOD_THREAD)
    assert reply.chat_id == env.chat_id and reply.text == "ok: What should I eat?"
    messages = await env.messages()
    assert roles(messages) == [("user", "What should I eat?"), ("assistant", "ok: What should I eat?")]
    assert {m["persona_id"] for m in messages} == {persona}  # persona_id came from the topic, via the database
    assert messages[0]["telegram_message_id"] == 7 and messages[1]["telegram_message_id"] is not None
    assert env.router.calls == []  # a message inside a topic is never routed


async def test_the_prompt_is_core_profile_plus_effective_principles_plus_the_context_buffer(bot_env):
    env = bot_env
    persona = await food_persona(env)
    await new_fact(env.owner_engine, env.user_id, "weight", category="body", value="80 kg", priority=70)
    sweets = await new_fact(env.owner_engine, env.user_id, "principle.no_sweets", category="principle", value="no sweets", priority=50)
    await owner_exec(
        env.owner_engine,
        "WITH s AS (INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, expires_at) "
        "VALUES (:u, 'celebrating', 8, 100, 'user_declared', now() + interval '5 hours') RETURNING id) "
        "INSERT INTO civ.emotional_overrides (user_id, emotional_state_id, core_fact_id) SELECT :u, id, :f FROM s",
        u=env.user_id, f=sweets,
    )  # fmt: skip
    await new_message(env.owner_engine, env.user_id, "earlier question", persona_id=persona)
    await new_message(env.owner_engine, env.user_id, "earlier answer", role="assistant", persona_id=persona)

    await env.orchestrator.handle_message(env.incoming("may I have cake?", thread_id=FOOD_THREAD))

    [call] = env.chat.calls
    system = call["system"]
    assert "- weight [body]: 80 kg" in system  # A) Core Profile
    sweets_line = next(line for line in system.splitlines() if line.startswith("- principle.no_sweets"))
    assert "TEMPORARILY OVERRIDDEN" in sweets_line and '"celebrating"' in sweets_line  # B) the view's verdict, rendered
    assert call["turns"] == [  # C) Context Buffer of this topic, ending with the message being answered
        ChatTurn("user", "earlier question"),
        ChatTurn("model", "earlier answer"),
        ChatTurn("user", "may I have cake?"),
    ]


async def test_the_prompt_carries_the_voice_and_only_the_live_emotional_states(bot_env):
    env = bot_env
    await food_persona(env)
    for label, source, started, expires, status in (
        ("exhausted", "inferred", "now() - interval '1 hour'", "now() + interval '5 hours'", "active"),
        ("was_stressed", "inferred", "now() - interval '3 hours'", "now() - interval '1 hour'", "active"),  # expired
        ("dismissed_one", "user_declared", "now() - interval '1 hour'", "now() + interval '5 hours'", "dismissed"),
    ):
        await owner_exec(
            env.owner_engine,
            "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, status, started_at, expires_at) "
            f"VALUES (:u, :l, 7, 40, CAST(:s AS civ.emotion_source), CAST(:st AS civ.emotion_status), {started}, {expires})",
            u=env.user_id, l=label, s=source, st=status,
        )  # fmt: skip

    await env.orchestrator.handle_message(env.incoming("устал", thread_id=FOOD_THREAD))

    system = env.chat.calls[0]["system"]
    assert "## Voice" in system and "Reading the user's state" in system  # tone rules are in every prompt
    state = system.split("## Current state", 1)[1]
    assert "exhausted (intensity 7/10, priority 40, the assistant's own inference" in state
    assert "was_stressed" not in state and "dismissed_one" not in state  # the same liveness rule as the principles view


async def test_gemini_is_called_outside_any_database_transaction(bot_env):
    env = bot_env
    await food_persona(env)
    probe = (
        "SELECT count(*) FROM pg_stat_activity WHERE usename = 'ai_civ_app' AND datname = current_database()"
    )  # the runtime role's connections (NullPool: a connection exists only while a tenant_session is open)
    seen: list[int] = []

    async def open_app_connections() -> None:
        seen.append((await owner_exec(env.owner_engine, probe))[0][0])

    env.chat.on_call = open_app_connections
    async with env.db.tenant_session(env.user_id) as session:  # positive control: the probe does see an open transaction
        await session.execute(text("SELECT 1"))
        await open_app_connections()
    assert seen == [1]
    seen.clear()

    await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))

    assert seen == [0]  # while the model was being called, no database connection / transaction was held
    assert len(env.chat.calls) == 1 and len(await env.messages()) == 2


async def test_a_hung_model_is_cut_off_by_the_timeout_and_the_user_is_told(bot_env):
    env = bot_env
    await food_persona(env)
    env.settings.llm_timeout_seconds = 0.05

    async def hangs(system, turns):
        await asyncio.sleep(30)

    env.chat.responder = hangs
    await asyncio.wait_for(env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD)), timeout=5)

    assert env.session.sent_to(FOOD_THREAD)[-1].text == texts.FAILURE
    assert roles(await env.messages()) == [("user", "hi")]  # the question is kept, no invented answer


# ---------------------------------------------------------------- failure containment (the polling loop must survive)


async def test_gemini_failure_sends_the_system_notice_and_keeps_the_users_message(bot_env, caplog):
    env = bot_env
    await food_persona(env)
    env.chat.responder = lambda system, turns: LLMError("503 from Gemini")

    with caplog.at_level(logging.ERROR):
        await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))  # must not raise

    [notice] = env.session.sent_to(FOOD_THREAD)
    assert notice.text == texts.FAILURE
    assert roles(await env.messages()) == [("user", "hi")]  # incoming is written first, nothing invented after it
    assert any("failed to process a message" in r.message for r in caplog.records)  # logged, with traceback


async def test_a_failed_telegram_send_is_contained_and_not_recorded_as_said(bot_env):
    env = bot_env
    await food_persona(env)
    env.session.fail_on("sendMessage", TelegramNetworkError, when=lambda m: m.text != texts.FAILURE)

    await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))

    assert [m.text for m in env.session.sent()] == ["ok: hi", texts.FAILURE]  # the answer failed, the notice got through
    assert roles(await env.messages()) == [("user", "hi")]  # a reply the user never saw is not in the context


async def test_when_telegram_is_completely_down_nothing_propagates(bot_env, caplog):
    env = bot_env
    await food_persona(env)
    env.session.fail_on("sendMessage", TelegramNetworkError)

    with caplog.at_level(logging.ERROR):
        await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))  # no exception

    assert any("could not deliver the notice" in r.message for r in caplog.records)
    assert roles(await env.messages()) == [("user", "hi")]


async def test_the_typing_indicator_is_cosmetic(bot_env):
    env = bot_env
    await food_persona(env)
    env.session.fail_on("sendChatAction", TelegramNetworkError)
    await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))
    assert env.session.sent_to(FOOD_THREAD)[-1].text == "ok: hi"


async def test_a_topic_that_is_not_a_persona_gets_a_hint_and_nothing_is_stored_or_asked(bot_env):
    env = bot_env
    await env.orchestrator.handle_message(env.incoming("hello?", thread_id=999))
    assert [m.text for m in env.session.sent_to(999)] == [texts.UNKNOWN_TOPIC]
    assert await env.messages() == [] and env.chat.calls == [] and env.router.calls == []


async def test_a_long_answer_is_split_for_telegram_and_stored_whole(bot_env):
    env = bot_env
    await food_persona(env)
    long_answer = "\n\n".join(f"Paragraph {i}. " + "tasty " * 150 for i in range(12))
    env.chat.responder = lambda system, turns: long_answer

    await env.orchestrator.handle_message(env.incoming("recipes?", thread_id=FOOD_THREAD))

    pieces = env.session.sent_to(FOOD_THREAD)
    assert len(pieces) >= 2 and all(len(p.text) <= 4096 for p in pieces)
    assert " ".join(" ".join(p.text for p in pieces).split()) == " ".join(long_answer.split())
    assistant = (await env.messages())[-1]
    assert assistant["content"] == long_answer  # the context stores what was said, in one piece


# ---------------------------------------------------------------- "General": routing and topic creation


async def test_a_new_activity_in_general_creates_a_topic_a_persona_and_answers_there(bot_env):
    env = bot_env
    env.router.responder = lambda prompt: route_new("bowling", "🎳 Боулинг", "Help the user get into bowling.")

    await env.orchestrator.handle_message(env.incoming("Хочу заняться боулингом"))

    [topic] = env.session.topics()
    assert topic.chat_id == env.chat_id and topic.name == "🎳 Боулинг"
    [persona] = await env.personas()
    assert persona["code"] == "bowling" and persona["thread_id"] is not None and persona["is_active"]
    assert "Help the user get into bowling." in persona["system_prompt"] and "Боулинг" in persona["system_prompt"]
    thread = persona["thread_id"]

    [call] = env.chat.calls
    assert call["system"].startswith(persona["system_prompt"][:40])  # the NEW persona's prompt, not the concierge's
    assert call["turns"] == [ChatTurn("user", "Хочу заняться боулингом")]  # the request was re-filed into the new persona

    [in_topic] = env.session.sent_to(thread)
    assert in_topic.text == "ok: Хочу заняться боулингом"  # the answer only: the request is not quoted back into the new topic
    [moved] = env.session.sent_to(None)
    assert moved.text == "Ушёл с этим в «🎳 Боулинг» ➡️"
    [[button]] = moved.reply_markup.inline_keyboard  # one button that opens the new topic
    assert button.url == f"https://t.me/c/{str(env.chat_id)[4:]}/{thread}" and "Боулинг" in button.text
    messages = await env.messages()
    assert roles(messages) == [("user", "Хочу заняться боулингом"), ("assistant", "ok: Хочу заняться боулингом")]
    assert {m["persona_id"] for m in messages} == {persona["id"]}
    assert env.router.calls[0]["model"] == "gemini-3.5-flash-lite" and "боулингом" in env.router.calls[0]["prompt"]


async def test_a_general_message_that_is_not_an_activity_is_answered_in_general_by_the_concierge(bot_env):
    env = bot_env  # the router fake answers "general" by default

    await env.orchestrator.handle_message(env.incoming("Привет! Как дела?"))

    assert env.session.topics() == [] and await env.personas() == []
    [call] = env.chat.calls
    assert call["system"].startswith(CONCIERGE_PROMPT[:50])
    [reply] = env.session.sent_to(None)
    assert reply.text == "ok: Привет! Как дела?"
    assert {m["persona_id"] for m in await env.messages()} == {None}


async def test_an_activity_that_matches_an_existing_persona_is_routed_there_without_a_new_topic(bot_env):
    env = bot_env
    persona = await food_persona(env)
    env.router.responder = lambda prompt: route_new("foodguide", "ignored title", "ignored focus")

    await env.orchestrator.handle_message(env.incoming("Сколько калорий в банане?"))

    assert env.session.topics() == [] and len(await env.personas()) == 1
    assert "foodguide: Foodguide" in env.router.calls[0]["prompt"]  # the model was shown the existing topics
    [reply] = env.session.sent_to(FOOD_THREAD)
    assert reply.text == "ok: Сколько калорий в банане?"
    assert env.chat.calls[0]["system"].startswith("FOODGUIDE PROMPT")  # the stored prompt wins
    assert {m["persona_id"] for m in await env.messages()} == {persona}
    [moved] = env.session.sent_to(None)
    assert moved.text == "Ушёл с этим в «Foodguide» ➡️" and moved.reply_markup.inline_keyboard[0][0].url.endswith(f"/{FOOD_THREAD}")


async def test_the_move_notice_has_no_button_when_the_chat_has_no_topic_links(owner_engine, db):
    """A group that is not a supergroup (-100…) has no t.me/c links: the notice is still sent, just without a button."""
    from tests.bot_env import build_bot_env

    env = await build_bot_env(owner_engine, db)
    try:
        await owner_exec(env.owner_engine, "UPDATE civ.users SET forum_chat_id = -4242 WHERE id = :u", u=env.user_id)
        env.router.responder = lambda prompt: route_new("bowling", "🎳 Боулинг")
        inc = replace(env.incoming("Хочу заняться боулингом"), tenant=replace(env.tenant, forum_chat_id=-4242))
        await env.orchestrator.handle_message(inc)
        [moved] = env.session.sent_to(None)
        assert moved.text == "Ушёл с этим в «🎳 Боулинг» ➡️" and moved.reply_markup is None
    finally:
        await env.bot.session.close()


async def test_an_existing_persona_without_a_topic_gets_one_instead_of_a_duplicate(bot_env):
    env = bot_env
    await new_persona(env.owner_engine, env.user_id, "bowling", title="Боулинг", system_prompt="BOWLING PROMPT")
    env.router.responder = lambda prompt: route_new("bowling", "Другое имя", "other focus")

    await env.orchestrator.handle_message(env.incoming("Погнали на боулинг"))

    [topic] = env.session.topics()
    assert topic.name == "Боулинг"  # the stored title, not the model's new idea
    [persona] = await env.personas()
    assert persona["thread_id"] is not None and persona["system_prompt"] == "BOWLING PROMPT"


async def test_the_same_activity_twice_never_creates_two_topics(bot_env):
    env = bot_env
    env.router.responder = lambda prompt: route_new("bowling", "Боулинг")

    await env.orchestrator.handle_message(env.incoming("Хочу заняться боулингом"))
    await env.orchestrator.handle_message(env.incoming("Найди клуб для боулинга"))

    assert len(env.session.topics()) == 1 and len(await env.personas()) == 1
    thread = (await env.personas())[0]["thread_id"]
    assert len(env.session.sent_to(thread)) == 2


async def test_garbage_from_the_router_degrades_to_an_answer_in_general(bot_env, caplog):
    env = bot_env
    env.router.responder = lambda prompt: "I am not JSON {"

    with caplog.at_level(logging.WARNING):
        await env.orchestrator.handle_message(env.incoming("Хочу заняться боулингом"))

    assert env.session.topics() == [] and await env.personas() == []
    assert env.session.sent_to(None)[-1].text == "ok: Хочу заняться боулингом"
    assert any("topic router output rejected" in r.message for r in caplog.records)


async def test_a_failing_router_is_a_gemini_failure_the_user_is_told_and_the_message_is_kept(bot_env):
    env = bot_env
    env.router.responder = lambda prompt: LLMError("router down")

    await env.orchestrator.handle_message(env.incoming("Хочу заняться боулингом"))

    assert [m.text for m in env.session.sent_to(None)] == [texts.FAILURE]
    assert env.chat.calls == [] and await env.personas() == []
    assert roles(await env.messages()) == [("user", "Хочу заняться боулингом")]


async def test_a_refused_topic_explains_itself_and_the_next_request_completes_the_job(bot_env):
    env = bot_env
    env.router.responder = lambda prompt: route_new("bowling", "Боулинг")
    env.session.fail_on("createForumTopic", TelegramBadRequest, times=1)  # e.g. the bot lacks "Manage topics"

    await env.orchestrator.handle_message(env.incoming("Хочу заняться боулингом"))

    assert [m.text for m in env.session.sent_to(None)] == [texts.TOPIC_FAILED]
    [persona] = await env.personas()
    assert persona["thread_id"] is None and env.chat.calls == []
    assert {m["persona_id"] for m in await env.messages()} == {None}  # not re-filed: there is no topic yet

    await env.orchestrator.handle_message(env.incoming("Так что насчёт боулинга?"))  # rights fixed

    [persona] = await env.personas()  # the same persona, now with its topic
    assert persona["thread_id"] is not None and len(env.session.sent_to(persona["thread_id"])) == 1


# ---------------------------------------------------------------- replays and ordering


async def test_a_replayed_update_that_was_already_answered_is_skipped(bot_env):
    env = bot_env
    await food_persona(env)
    await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD, message_id=42))
    await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD, message_id=42))  # same Telegram update

    assert len(env.chat.calls) == 1 and len(env.session.sent_to(FOOD_THREAD)) == 1
    assert len(await env.messages()) == 2  # one user row (UNIQUE user_id + telegram_message_id), one answer


async def test_a_replayed_update_whose_answer_was_lost_is_answered(bot_env):
    env = bot_env
    persona = await food_persona(env)
    await owner_exec(  # the process died after writing the incoming message and before answering
        env.owner_engine,
        "INSERT INTO civ.messages (user_id, persona_id, role, content, telegram_message_id) VALUES (:u, :p, 'user', 'hi', 9)",
        u=env.user_id, p=persona,
    )  # fmt: skip

    await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD, message_id=9))

    assert roles(await env.messages()) == [("user", "hi"), ("assistant", "ok: hi")]  # answered, not stored twice


async def test_a_replay_after_routing_does_not_ask_the_router_again_or_open_a_second_topic(bot_env):
    env = bot_env
    env.router.responder = lambda prompt: route_new("bowling", "Боулинг")
    env.chat.responder = lambda system, turns: LLMError("Gemini is down")  # the answer fails AFTER the topic exists

    await env.orchestrator.handle_message(env.incoming("Хочу заняться боулингом", message_id=77))
    assert len(env.session.topics()) == 1 and len(env.router.calls) == 1

    env.chat.responder = lambda system, turns: "Отлично, давайте начнём!"  # Telegram redelivers the same update
    await env.orchestrator.handle_message(env.incoming("Хочу заняться боулингом", message_id=77))

    assert len(env.session.topics()) == 1 and len(env.router.calls) == 1  # decision reused, not re-made
    thread = (await env.personas())[0]["thread_id"]
    assert env.session.sent_to(thread)[-1].text.endswith("Отлично, давайте начнём!")
    assert [m["role"] for m in await env.messages()] == ["user", "assistant"]


async def test_two_messages_in_one_topic_are_answered_in_order_and_the_second_sees_the_first_answer(bot_env):
    env = bot_env
    await food_persona(env)
    running = {"now": 0, "max": 0}

    async def slow(system, turns):
        running["now"] += 1
        running["max"] = max(running["max"], running["now"])
        await asyncio.sleep(0.05)
        running["now"] -= 1
        return f"answer to: {turns[-1].text}"

    env.chat.responder = slow
    await asyncio.gather(
        env.orchestrator.handle_message(env.incoming("first", thread_id=FOOD_THREAD)),
        env.orchestrator.handle_message(env.incoming("second", thread_id=FOOD_THREAD)),
    )

    assert running["max"] == 1  # never two model calls for one topic at once
    assert [t.text for t in env.chat.calls[1]["turns"]] == ["first", "answer to: first", "second"]
    assert [m.text for m in env.session.sent_to(FOOD_THREAD)] == ["answer to: first", "answer to: second"]


# ---------------------------------------------------------------- /setup


PLAN = {
    "topics": [
        {"code": "job_search", "title": "💼 Поиск работы", "focus": "Help the user find a job."},
        {"code": "danish", "title": "🇩🇰 Датский язык", "focus": "Help the user learn Danish."},
        {"code": "fitness", "title": "🏃 Форма", "focus": "Keep the user training."},
    ]
}


async def seed_goals(env, *goals):
    for i, goal in enumerate(goals, 1):
        await new_fact(env.owner_engine, env.user_id, f"goal.{i}", category="goal", value=goal, priority=70)


def plan_responder(plan=PLAN):
    return lambda prompt: plan if "<goals>" in prompt else {"intent": "general", "code": "", "title": "", "focus": ""}


async def test_setup_builds_the_topics_from_the_users_goals_not_from_a_fixed_list(bot_env):
    env = bot_env
    await seed_goals(env, "найти работу", "выучить датский", "бегать")
    env.router.responder = plan_responder()

    await env.orchestrator.setup_defaults(env.tenant, None)

    [call] = env.router.calls
    assert call["model"] == "gemini-3.5-flash-lite" and call["system"].startswith("You set up the Telegram forum")
    assert "<goals>" in call["prompt"] and "выучить датский" in call["prompt"]  # the goals were what the model saw
    assert [t.name for t in env.session.topics()] == [SETTINGS_PERSONA.title, "💼 Поиск работы", "🇩🇰 Датский язык", "🏃 Форма"]
    personas = await env.personas()
    assert {p["code"] for p in personas} == {"settings", "job_search", "danish", "fitness"}
    assert all(p["thread_id"] is not None for p in personas) and len({p["thread_id"] for p in personas}) == 4
    assert "foodguide" not in {p["code"] for p in personas}  # nothing hardcoded
    done = env.session.sent_to(None)[-1].text
    assert done.startswith("Готово.") and "Поиск работы" in done and "Датский язык" in done

    await env.orchestrator.setup_defaults(env.tenant, None)  # idempotent: no second model call, no new topics
    assert len(env.session.topics()) == 4 and len(env.router.calls) == 1
    assert env.session.sent_to(None)[-1].text == texts.SETUP_NOTHING_NEW


async def test_the_new_settings_topic_greets_with_the_mini_app_button(bot_env):
    env = bot_env
    await env.orchestrator.setup_defaults(env.tenant, None)

    settings_thread = next(p["thread_id"] for p in await env.personas() if p["code"] == "settings")
    [greeting] = env.session.sent_to(settings_thread)
    assert greeting.text == texts.SETTINGS_GREETING
    [[button]] = greeting.reply_markup.inline_keyboard
    assert button.text == "⚙️ Настройки и Память"
    assert button.url == "https://t.me/PerfectPerson_bot/Overview"  # the Direct Link, from the settings
    assert button.web_app is None  # web_app buttons do not work in groups

    await env.orchestrator.setup_defaults(env.tenant, None)  # the topic exists now: no second greeting
    assert len(env.session.sent_to(settings_thread)) == 1


async def test_the_app_command_answers_with_the_button_where_it_was_typed(bot_env):
    env = bot_env
    await env.feed("/app", thread_id=FOOD_THREAD)

    [reply] = env.session.sent_to(FOOD_THREAD)
    assert reply.text == texts.APP_LINK and reply.reply_markup.inline_keyboard[0][0].url == "https://t.me/PerfectPerson_bot/Overview"
    assert env.chat.calls == [] and await env.messages() == []  # a command is not a chat message


async def test_the_app_command_before_onboarding_starts_the_interview_instead(onboarding_env):
    from ai_civ.bot import onboarding

    env = onboarding_env
    await env.feed("/app")
    assert env.session.sent_to(None)[-1].text.startswith(onboarding.INTRO)


async def test_setup_without_goals_creates_only_settings_and_does_not_ask_the_model(bot_env):
    env = bot_env
    env.router.responder = plan_responder()

    await env.orchestrator.setup_defaults(env.tenant, None)

    assert env.router.calls == []
    assert [p["code"] for p in await env.personas()] == ["settings"]
    assert env.session.sent_to(None)[-1].text == texts.SETUP_NO_GOALS


async def test_setup_with_an_unusable_plan_invents_nothing_and_a_retry_asks_again(bot_env):
    env = bot_env
    await seed_goals(env, "найти работу")
    env.router.responder = lambda prompt: "I am not JSON {"

    await env.orchestrator.setup_defaults(env.tenant, None)

    assert [p["code"] for p in await env.personas()] == ["settings"]
    assert env.session.sent_to(None)[-1].text == texts.SETUP_NO_TOPICS
    env.router.responder = plan_responder()
    await env.orchestrator.setup_defaults(env.tenant, None)  # nothing but Settings exists, so the model is asked again
    assert len(env.router.calls) == 2 and len(await env.personas()) == 4


async def test_setup_survives_a_model_outage_with_the_system_notice(bot_env):
    env = bot_env
    await seed_goals(env, "найти работу")
    env.router.responder = lambda prompt: LLMError("down")

    await env.orchestrator.setup_defaults(env.tenant, None)

    assert env.session.sent_to(None)[-1].text == texts.FAILURE
    assert [p["code"] for p in await env.personas()] == ["settings"]  # the part that needs no model is already done


async def test_a_refused_topic_midway_loses_no_topic_the_rerun_finishes_the_job_without_the_model(bot_env):
    env = bot_env
    await seed_goals(env, "найти работу", "выучить датский", "бегать")
    env.router.responder = plan_responder()
    env.session.fail_on("createForumTopic", TelegramBadRequest, when=lambda m: m.name == "🇩🇰 Датский язык", times=1)

    await env.orchestrator.setup_defaults(env.tenant, None)

    assert env.session.sent_to(None)[-1].text == texts.TOPIC_FAILED
    rows = {p["code"]: p["thread_id"] for p in await env.personas()}
    assert set(rows) == {"settings", "job_search", "danish", "fitness"}  # every row of the plan was written first
    assert rows["danish"] is None and rows["job_search"] is not None  # only the refused one lacks a topic

    await env.orchestrator.setup_defaults(env.tenant, None)  # rights fixed

    assert all(p["thread_id"] is not None for p in await env.personas()) and len(env.router.calls) == 1
    assert "Датский язык" in env.session.sent_to(None)[-1].text


async def test_setup_leaves_an_existing_users_topics_alone(bot_env):
    env = bot_env
    await seed_goals(env, "бегать")
    await food_persona(env)  # a user who already has a topic (e.g. one the router created earlier)
    env.router.responder = plan_responder()

    await env.orchestrator.setup_defaults(env.tenant, None)

    assert env.router.calls == []  # no second set of topics next to the existing ones
    assert {p["code"] for p in await env.personas()} == {"foodguide", "settings"}


# ---------------------------------------------------------------- proactivity (the bot writes first)


async def test_a_proactive_message_is_generated_with_the_proactive_flag_and_pushed_into_the_topic(bot_env):
    env = bot_env
    persona = await food_persona(env)

    done = await env.orchestrator.run_proactive(env.tenant, get_trigger("meal_check"))

    assert done is True
    [call] = env.chat.calls
    assert call["system"].startswith("FOODGUIDE PROMPT") and "## Proactive mode" in call["system"]
    assert len(call["turns"]) == 1 and call["turns"][0].text.startswith("[system trigger, not written by the user]")
    [pushed] = env.session.sent_to(FOOD_THREAD)
    assert pushed.chat_id == env.chat_id
    messages = await env.messages()
    assert [(m["role"], m["persona_id"]) for m in messages] == [("assistant", persona)]  # no phantom user message


async def test_the_users_reply_to_a_proactive_question_sees_that_question_in_its_context(bot_env):
    env = bot_env
    await food_persona(env)
    env.chat.responder = lambda system, turns: "Ты поел?" if "Proactive mode" in system else "Отлично!"

    await env.orchestrator.run_proactive(env.tenant, get_trigger("meal_check"))
    await env.orchestrator.handle_message(env.incoming("Да, пообедал", thread_id=FOOD_THREAD))

    turns = env.chat.calls[1]["turns"]
    assert [t.role for t in turns] == ["user", "model", "user"]  # opener, the bot's question, the user's answer
    assert turns[1].text == "Ты поел?" and turns[2].text == "Да, пообедал"


async def test_a_proactive_trigger_without_its_topic_writes_in_the_main_chat(bot_env):
    """Topics come from each user's goals, so "foodguide" may not exist for them: the reminder must not be lost."""
    env = bot_env

    assert await env.orchestrator.run_proactive(env.tenant, get_trigger("meal_check")) is True  # no persona at all
    await new_persona(env.owner_engine, env.user_id, "foodguide", thread_id=None)
    assert await env.orchestrator.run_proactive(env.tenant, get_trigger("meal_check")) is True  # persona, no topic

    assert len(env.session.sent_to(None)) == 2 and env.session.topics() == []
    assert all(c["system"].startswith(CONCIERGE_PROMPT[:50]) and "## Proactive mode" in c["system"] for c in env.chat.calls)
    assert [(m["role"], m["persona_id"]) for m in await env.messages()] == [("assistant", None)] * 2


async def test_proactive_failures_raise_for_celery_to_retry_and_do_not_spam_the_user(bot_env):
    env = bot_env
    await food_persona(env)
    env.chat.responder = lambda system, turns: LLMError("Gemini down")
    with pytest.raises(LLMError):
        await env.orchestrator.run_proactive(env.tenant, get_trigger("meal_check"))
    env.chat.responder = lambda system, turns: "Ты поел?"
    env.session.fail_on("sendMessage", TelegramNetworkError)
    with pytest.raises(TelegramNetworkError):
        await env.orchestrator.run_proactive(env.tenant, get_trigger("meal_check"))
    assert await env.messages() == []  # nothing recorded as said
    assert all(m.text != texts.FAILURE for m in env.session.sent())  # no system notice for something the user did not ask


# ---------------------------------------------------------------- restraint: a topic is made rarely


async def test_a_router_that_is_not_sure_enough_answers_in_general_and_makes_no_topic(bot_env, caplog):
    env = bot_env
    env.router.responder = lambda prompt: route_new("climbing", "🧗 Скалодром", confidence=0.84)  # just under the bar for a NEW topic

    with caplog.at_level(logging.INFO):
        await env.orchestrator.handle_message(env.incoming("Хочу сходить на скалодром в пятницу"))

    assert env.session.topics() == [] and await env.personas() == []
    assert env.session.sent_to(None)[-1].text == "ok: Хочу сходить на скалодром в пятницу"
    assert {m["persona_id"] for m in await env.messages()} == {None}
    assert not any("topic router output rejected" in r.message for r in caplog.records)  # a verdict, not a broken reply


async def test_a_confident_router_still_opens_the_topic(bot_env):
    env = bot_env
    env.router.responder = lambda prompt: route_new("german", "🇩🇪 Немецкий", confidence=0.85)

    await env.orchestrator.handle_message(env.incoming("С сегодняшнего дня системно учу немецкий, это мой план на год"))

    [topic] = env.session.topics()
    assert topic.name == "🇩🇪 Немецкий"


async def test_a_moderately_sure_router_may_move_a_message_into_an_existing_topic(bot_env):
    env = bot_env
    await food_persona(env)
    env.router.responder = lambda prompt: route_new("foodguide", "x", confidence=0.7)  # below the bar for a new topic, above the bar for an old one

    await env.orchestrator.handle_message(env.incoming("Сколько калорий в банане?"))

    assert env.session.sent_to(FOOD_THREAD)[-1].text == "ok: Сколько калорий в банане?" and env.session.topics() == []


async def test_the_defaults_are_five_topics_one_new_topic_a_day_and_a_high_bar(bot_env):
    cfg = bot_env.orchestrator.settings
    assert cfg.create_topic_max_active == 5 and cfg.create_topic_cooldown_hours == 24 and cfg.router_min_confidence == 0.85


async def test_a_second_new_topic_within_a_day_is_not_made_the_message_is_answered_in_general(bot_env):
    env = bot_env
    env.router.responder = lambda prompt: route_new("german", "🇩🇪 Немецкий")
    await env.orchestrator.handle_message(env.incoming("Начинаю системно учить немецкий"))
    assert len(env.session.topics()) == 1

    env.router.responder = lambda prompt: route_new("running", "🏃 Бег")  # a different long-term goal, minutes later
    await env.orchestrator.handle_message(env.incoming("И ещё я буду готовиться к полумарафону"))

    assert len(env.session.topics()) == 1 and {p["code"] for p in await env.personas()} == {"german"}  # no second topic
    assert env.session.sent_to(None)[-1].text == "ok: И ещё я буду готовиться к полумарафону"  # answered in the main chat
    assert not any(m.text == texts.TOPIC_FAILED for m in env.session.sent())


async def test_the_cooldown_does_not_stop_a_message_to_a_topic_the_user_already_has(bot_env):
    env = bot_env
    env.router.responder = lambda prompt: route_new("german", "🇩🇪 Немецкий")
    await env.orchestrator.handle_message(env.incoming("Начинаю системно учить немецкий"))
    thread = (await env.personas())[0]["thread_id"]

    await env.orchestrator.handle_message(env.incoming("Подскажи, как запомнить артикли"))  # still inside the cooldown, but this topic exists

    assert len(env.session.topics()) == 1 and len(env.session.sent_to(thread)) == 2


async def test_the_cap_of_active_topics_stops_the_router_even_with_no_cooldown(owner_engine, db):
    from tests.bot_env import build_bot_env

    env = await build_bot_env(owner_engine, db, create_topic_max_active=2, create_topic_cooldown_hours=0)
    try:
        await new_persona(env.owner_engine, env.user_id, "foodguide", thread_id=501)
        await new_persona(env.owner_engine, env.user_id, "work", thread_id=502)
        env.router.responder = lambda prompt: route_new("german", "🇩🇪 Немецкий")

        await env.orchestrator.handle_message(env.incoming("Начинаю системно учить немецкий"))

        assert env.session.topics() == [] and {p["code"] for p in await env.personas()} == {"foodguide", "work"}
        assert env.session.sent_to(None)[-1].text == "ok: Начинаю системно учить немецкий"
    finally:
        await env.bot.session.close()


async def test_an_existing_persona_whose_topic_is_gone_obeys_the_same_limits(owner_engine, db):
    from tests.bot_env import build_bot_env

    env = await build_bot_env(owner_engine, db, create_topic_max_active=1, create_topic_cooldown_hours=0)
    try:
        await new_persona(env.owner_engine, env.user_id, "foodguide", thread_id=501)
        await new_persona(env.owner_engine, env.user_id, "bowling", thread_id=None)  # its topic was deleted and forgotten
        env.router.responder = lambda prompt: route_new("bowling", "Боулинг")

        await env.orchestrator.handle_message(env.incoming("Опять про боулинг: клуб на выходные"))

        assert env.session.topics() == []  # one active topic already: the cap holds for a re-created topic as well
        assert next(p for p in await env.personas() if p["code"] == "bowling")["thread_id"] is None
    finally:
        await env.bot.session.close()


# ---------------------------------------------------------------- a topic deleted by hand ("message thread not found")

GONE = "Bad Request: message thread not found"


def only_thread(thread_id):
    return lambda method: method.message_thread_id == thread_id


async def test_a_deleted_topic_is_forgotten_and_the_reply_goes_to_the_main_chat(bot_env, caplog):
    env = bot_env
    persona = await food_persona(env)
    env.session.fail_on("sendMessage", TelegramBadRequest, when=only_thread(FOOD_THREAD), message=GONE)

    with caplog.at_level(logging.WARNING):
        await env.orchestrator.handle_message(env.incoming("What should I eat?", thread_id=FOOD_THREAD))  # no exception

    assert [m.text for m in env.session.sent_to(None)] == ["ok: What should I eat?"]  # the same reply, in the main chat
    assert texts.FAILURE not in [m.text for m in env.session.sent()]
    [row] = await env.personas()
    assert row["thread_id"] is None and row["is_active"] and row["id"] == persona  # unbound, but the persona itself lives on
    assert [m["role"] for m in await env.messages()] == ["user", "assistant"]  # the reply the user saw is stored
    assert any("message thread not found" in r.message for r in caplog.records)


async def test_the_dead_topic_is_asked_once_not_in_a_loop(bot_env):
    env = bot_env
    await food_persona(env)
    env.session.fail_on("sendMessage", TelegramBadRequest, when=only_thread(FOOD_THREAD), message=GONE)

    await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))

    attempts = [m for m in env.session.sent() if m.message_thread_id == FOOD_THREAD]
    assert len(attempts) == 1 and len(env.session.sent_to(None)) == 1  # one try in the topic, one fallback, nothing more


async def test_a_long_reply_continues_in_the_main_chat_after_the_topic_died(bot_env):
    env = bot_env
    await food_persona(env)
    env.session.fail_on("sendMessage", TelegramBadRequest, when=only_thread(FOOD_THREAD), message=GONE)
    long_answer = "\n\n".join(f"Paragraph {i}. " + "tasty " * 150 for i in range(12))
    env.chat.responder = lambda system, turns: long_answer

    await env.orchestrator.handle_message(env.incoming("recipes?", thread_id=FOOD_THREAD))

    pieces = env.session.sent_to(None)
    assert len(pieces) >= 2 and " ".join(" ".join(p.text for p in pieces).split()) == " ".join(long_answer.split())
    assert len([m for m in env.session.sent() if m.message_thread_id == FOOD_THREAD]) == 1  # the rest did not knock on the dead topic again


async def test_a_deleted_topic_found_by_a_proactive_message_is_handled_the_same_way(bot_env):
    env = bot_env
    await food_persona(env)
    env.session.fail_on("sendMessage", TelegramBadRequest, when=only_thread(FOOD_THREAD), message=GONE)

    assert await env.orchestrator.run_proactive(env.tenant, get_trigger("meal_check")) is True

    assert len(env.session.sent_to(None)) == 1 and (await env.personas())[0]["thread_id"] is None
    # the next nudge: the persona has no topic any more, so it writes in the main chat directly (the existing behaviour)
    assert await env.orchestrator.run_proactive(env.tenant, get_trigger("meal_check")) is True
    assert len(env.session.sent_to(None)) == 2


async def test_the_underscored_spelling_of_the_error_is_recognised_too(bot_env):
    env = bot_env
    await food_persona(env)
    env.session.fail_on("sendMessage", TelegramBadRequest, when=only_thread(FOOD_THREAD), message="Bad Request: MESSAGE_THREAD_NOT_FOUND")

    await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))

    assert len(env.session.sent_to(None)) == 1 and (await env.personas())[0]["thread_id"] is None


async def test_other_bad_requests_are_not_mistaken_for_a_deleted_topic(bot_env):
    env = bot_env
    await food_persona(env)
    env.session.fail_on("sendMessage", TelegramBadRequest, when=only_thread(FOOD_THREAD), message="Bad Request: message is too long")

    await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))

    assert (await env.personas())[0]["thread_id"] == FOOD_THREAD  # the binding stays
    assert env.session.sent_to(None) == []  # and nothing was re-routed
    assert roles(await env.messages()) == [("user", "hi")]  # the boundary handled it as before


async def test_if_the_main_chat_fails_too_the_error_is_not_swallowed(bot_env, caplog):
    env = bot_env
    await food_persona(env)
    env.session.fail_on("sendMessage", TelegramBadRequest, when=only_thread(FOOD_THREAD), message=GONE)
    env.session.fail_on("sendMessage", TelegramNetworkError, when=only_thread(None))

    with caplog.at_level(logging.ERROR):
        await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))  # the boundary contains it

    assert roles(await env.messages()) == [("user", "hi")]  # nothing recorded as said
    assert any("failed to process a message" in r.message for r in caplog.records)


async def test_a_failing_cleanup_does_not_stop_the_redirect(bot_env, monkeypatch, caplog):
    env = bot_env
    await food_persona(env)
    env.session.fail_on("sendMessage", TelegramBadRequest, when=only_thread(FOOD_THREAD), message=GONE)

    async def broken(*args, **kwargs):
        raise RuntimeError("database down")

    monkeypatch.setattr("ai_civ.bot.personas.clear_thread", broken)
    with caplog.at_level(logging.WARNING):
        await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))

    assert [m.text for m in env.session.sent_to(None)] == ["ok: hi"]  # the user still got the answer
    assert any("could not forget the deleted topic" in r.message for r in caplog.records)


async def test_forgetting_a_topic_unbinds_only_that_thread(bot_env):
    env = bot_env
    await food_persona(env)
    await new_persona(env.owner_engine, env.user_id, "work", thread_id=777)

    await env.orchestrator._forget_topic(env.chat_id, 777)
    await env.orchestrator._forget_topic(env.chat_id, 777)  # idempotent

    assert {p["code"]: p["thread_id"] for p in await env.personas()} == {"foodguide": FOOD_THREAD, "work": None}

"""v2.0, PR 3.3: Danish immersion mode of a topic. The points under test: the mode lives in the persona row (CHECK in the database) and is changed ONLY
by the user's /dansk and /normal in that topic; the confirmation is a fixed text written by code (Danish on, the standard language off) and the model
is not called for it; the Danish rules (A2-B1, "💡 Rettelse:" block, the translation exception) reach the system prompt of exactly the topics in the
mode - in replies, in replies to attachments and in proactive messages - and nowhere else; the tools keep working in the mode. Real PostgreSQL, the real
aiogram Dispatcher on a fake Telegram wire, a fake Gemini."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from ai_civ.bot import personas, prompt, texts
from ai_civ.bot.orchestrator import load_tenant
from ai_civ.bot.prompt import IMMERSION_DA, RULES, VOICE, Profile, render_system
from ai_civ.bot.proactive import get_trigger
from ai_civ.db.enums import PersonaMode
from ai_civ.db.models import Persona
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_persona, new_user, owner_exec

DANISH = "Language immersion: Danish"
DANSK_THREAD, OTHER_THREAD = 55, 56
CYRILLIC = re.compile("[а-яА-ЯёЁ]")


# ================================================================== pure: the prompt and the texts


def system(mode: str | None = None, persona_prompt: str = "PERSONA") -> str:
    kw = {} if mode is None else {"mode": mode}
    return render_system(persona_prompt, Profile(), now=datetime(2026, 10, 6, 12, 0, tzinfo=UTC), timezone="Europe/Copenhagen", **kw)


def test_the_rules_of_the_mode_ask_for_danish_a2_b1_the_rettelse_block_and_the_translation_exception():
    low = IMMERSION_DA.lower()
    assert IMMERSION_DA.startswith("## " + DANISH) and "overrides the rule about the user's language" in low
    assert "strictly in danish" in low and "a2-b1" in low and "everyday spoken language" in low
    assert "💡 Rettelse:" in IMMERSION_DA and "short, kind explanation" in low and "never invent a mistake" in low
    assert "only exception" in low and "one short example sentence in danish" in low and "in context" in low
    assert "functions offered to you" in low  # the tools are not switched off by the mode


def test_the_rule_about_the_users_language_points_at_the_immersion_section():
    assert "Language immersion" in RULES.splitlines()[1]


def test_the_normal_prompt_has_no_immersion_section_and_the_mode_defaults_to_normal():
    assert DANISH not in system() and DANISH not in system("normal") and system() == system("normal")


def test_an_unknown_mode_adds_nothing():
    assert DANISH not in system("immersion_fr")


def test_the_immersion_section_comes_right_after_the_voice_and_before_the_data():
    text = system("immersion_da")
    assert text.startswith("PERSONA") and text.index(VOICE) < text.index(DANISH) < text.index("## Current time")
    assert text.count(DANISH) == 1


def test_build_prompt_and_render_system_agree_on_the_mode_parameter():
    assert prompt.build_prompt.__annotations__["mode"] == "str" and "mode" in render_system.__code__.co_varnames


def test_the_confirmations_are_fixed_texts_in_the_right_language():
    assert not CYRILLIC.search(texts.MODE_DANSK_ON) and "dansk" in texts.MODE_DANSK_ON and "/normal" in texts.MODE_DANSK_ON  # Danish on
    assert CYRILLIC.search(texts.MODE_NORMAL_ON)  # the standard language off
    assert CYRILLIC.search(texts.MODE_NO_TOPIC)


# ================================================================== the database


async def test_a_persona_is_normal_by_default_and_the_database_refuses_any_other_mode(owner_engine, db):
    user = await new_user(owner_engine)
    pid = await new_persona(owner_engine, user, "danish", thread_id=7)
    assert await owner_exec(owner_engine, "SELECT mode FROM civ.personas WHERE id = :p", p=pid) == [("normal",)]
    await owner_exec(owner_engine, "UPDATE civ.personas SET mode = 'immersion_da' WHERE id = :p", p=pid)
    for bad in ("immersion_fr", "", "NORMAL", "dansk"):
        with pytest.raises((IntegrityError, DBAPIError)):
            await owner_exec(owner_engine, "UPDATE civ.personas SET mode = :m WHERE id = :p", m=bad, p=pid)
    with pytest.raises((IntegrityError, DBAPIError)):
        await owner_exec(owner_engine, "UPDATE civ.personas SET mode = NULL WHERE id = :p", p=pid)


async def test_the_model_reads_and_writes_the_mode_and_the_enum_matches_the_check(owner_engine, db):
    user = await new_user(owner_engine)
    pid = await new_persona(owner_engine, user, "danish", thread_id=7)
    async with db.tenant_session(user) as session:
        assert (await personas.get_by_id(session, pid)).mode == "normal"  # type: ignore[union-attr]
        assert await personas.set_mode(session, pid, PersonaMode.immersion_da) is True
    async with db.tenant_session(user) as session:
        assert (await session.get(Persona, pid)).mode == "immersion_da"  # type: ignore[union-attr]
        assert await personas.set_mode(session, pid, PersonaMode.normal) is True
    async with db.tenant_session(user) as session:
        assert (await session.get(Persona, pid)).mode == "normal"  # type: ignore[union-attr]
    for mode in PersonaMode:  # every enum value is accepted by the CHECK
        await owner_exec(owner_engine, "UPDATE civ.personas SET mode = :m WHERE id = :p", m=mode.value, p=pid)


async def test_setting_the_mode_of_a_missing_persona_is_false_and_another_tenants_persona_is_out_of_reach(owner_engine, db):
    import uuid

    mine, theirs = await new_user(owner_engine), await new_user(owner_engine)
    foreign = await new_persona(owner_engine, theirs, "danish", thread_id=7)
    async with db.tenant_session(mine) as session:
        assert await personas.set_mode(session, uuid.uuid4(), PersonaMode.immersion_da) is False
        assert await personas.set_mode(session, foreign, PersonaMode.immersion_da) is False  # RLS: the row does not exist for them
    assert await owner_exec(owner_engine, "SELECT mode FROM civ.personas WHERE id = :p", p=foreign) == [("normal",)]


# ================================================================== the commands (through the Dispatcher)


async def mode_of(env, code: str = "danish") -> str:
    [(mode,)] = await owner_exec(env.owner_engine, "SELECT mode FROM civ.personas WHERE user_id = :u AND code = :c", u=env.user_id, c=code)
    return mode


def says(env, thread_id: int | None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


@pytest.fixture
async def env(owner_engine, db):
    env = await build_bot_env(owner_engine, db)
    await new_persona(owner_engine, env.user_id, "danish", thread_id=DANSK_THREAD, title="Dansk")
    await new_persona(owner_engine, env.user_id, "gym", thread_id=OTHER_THREAD, title="Зал")
    env.chat.responder = lambda system, turns: "Hej! Hvordan har du det?"
    yield env
    await env.bot.session.close()


async def test_dansk_switches_the_topic_and_the_confirmation_is_danish_text_of_the_code(env):
    await env.feed("/dansk", thread_id=DANSK_THREAD)
    assert says(env, DANSK_THREAD) == [texts.MODE_DANSK_ON]
    assert await mode_of(env) == "immersion_da" and await mode_of(env, "gym") == "normal"  # only THIS topic changed
    assert env.chat.calls == [] and env.chat.tool_calls == []  # the model said nothing: the code did
    assert await env.messages() == []  # a command and its confirmation are not conversation


async def test_normal_switches_back_with_a_confirmation_in_the_standard_language(env):
    await env.feed("/dansk", thread_id=DANSK_THREAD)
    await env.feed("/normal", thread_id=DANSK_THREAD)
    assert says(env, DANSK_THREAD) == [texts.MODE_DANSK_ON, texts.MODE_NORMAL_ON]
    assert await mode_of(env) == "normal" and env.chat.calls == []


async def test_the_commands_are_idempotent(env):
    for _ in range(2):
        await env.feed("/dansk", thread_id=DANSK_THREAD)
    assert await mode_of(env) == "immersion_da" and says(env, DANSK_THREAD) == [texts.MODE_DANSK_ON] * 2
    for _ in range(2):
        await env.feed("/normal", thread_id=DANSK_THREAD)
    assert await mode_of(env) == "normal"


async def test_the_main_chat_an_unknown_topic_and_the_settings_topic_have_no_language_mode(env, owner_engine):
    await new_persona(owner_engine, env.user_id, "settings", thread_id=70, title="Настройки")
    await env.feed("/dansk")  # the main chat
    await env.feed("/dansk", thread_id=999)  # a topic the bot has no persona for
    await env.feed("/dansk", thread_id=70)  # the system topic
    assert says(env, None) == [texts.MODE_NO_TOPIC] and says(env, 999) == [texts.MODE_NO_TOPIC] and says(env, 70) == [texts.MODE_NO_TOPIC]
    assert [await mode_of(env, c) for c in ("danish", "gym", "settings")] == ["normal"] * 3


async def test_the_mode_of_one_tenant_never_reaches_another_tenants_topic_with_the_same_thread_id(env, owner_engine):
    other = await new_user(owner_engine)
    await new_persona(owner_engine, other, "danish", thread_id=DANSK_THREAD)
    await env.feed("/dansk", thread_id=DANSK_THREAD)
    assert await owner_exec(owner_engine, "SELECT mode FROM civ.personas WHERE user_id = :u", u=other) == [("normal",)]


async def test_before_the_onboarding_is_done_the_command_changes_nothing(owner_engine, db):
    env = await build_bot_env(owner_engine, db, onboarding_state="pending")
    try:
        await new_persona(owner_engine, env.user_id, "danish", thread_id=DANSK_THREAD)
        await env.feed("/dansk", thread_id=DANSK_THREAD)
        assert await mode_of(env) == "normal" and texts.MODE_DANSK_ON not in says(env, DANSK_THREAD)
    finally:
        await env.bot.session.close()


# ================================================================== the prompt reaches the model only in the mode


def last_system(env) -> str:
    return env.chat.calls[-1]["system"]


async def test_only_the_topic_in_the_mode_gets_the_danish_rules(env):
    await env.feed("Hej, hvordan går det?", thread_id=DANSK_THREAD)
    assert DANISH not in last_system(env)  # not switched on yet
    await env.feed("/dansk", thread_id=DANSK_THREAD)
    await env.feed("Jeg har spise æble i dag", thread_id=DANSK_THREAD)
    assert DANISH in last_system(env) and "💡 Rettelse:" in last_system(env) and last_system(env).count(DANISH) == 1
    await env.feed("Как дела у зала?", thread_id=OTHER_THREAD)
    assert DANISH not in last_system(env)  # another topic of the same user
    await env.feed("Привет, это основной чат")
    assert DANISH not in last_system(env)  # the main chat
    await env.feed("/normal", thread_id=DANSK_THREAD)
    await env.feed("Nu på russisk igen, tak", thread_id=DANSK_THREAD)
    assert DANISH not in last_system(env)


async def test_the_mode_survives_between_messages_and_is_read_from_the_database_each_time(env, owner_engine):
    await owner_exec(owner_engine, "UPDATE civ.personas SET mode = 'immersion_da' WHERE user_id = :u AND code = 'danish'", u=env.user_id)  # e.g. set earlier
    for text in ("Hej med dig, ven", "Hvad skal vi lave i dag?"):
        await env.feed(text, thread_id=DANSK_THREAD)
        assert DANISH in last_system(env)


async def test_a_reply_to_a_document_in_the_mode_is_in_the_mode_too(env):
    await env.feed("/dansk", thread_id=DANSK_THREAD)
    await env.feed_file("Københavns historie i korte træk.".encode(), caption="Se her", thread_id=DANSK_THREAD, file_name="a.txt", mime_type="text/plain")
    assert DANISH in last_system(env)


async def test_a_proactive_message_in_the_mode_topic_is_danish_and_in_a_normal_topic_is_not(owner_engine, db):
    env = await build_bot_env(owner_engine, db)
    try:
        await new_persona(owner_engine, env.user_id, "foodguide", thread_id=DANSK_THREAD, title="Mad")
        env.chat.responder = lambda system, turns: "Hvad har du spist?"
        tenant = await load_tenant(db, env.user_id)
        assert await env.orchestrator.run_proactive(tenant, get_trigger("meal_check")) is True
        assert DANISH not in last_system(env)
        await owner_exec(owner_engine, "UPDATE civ.personas SET mode = 'immersion_da' WHERE user_id = :u AND code = 'foodguide'", u=env.user_id)
        assert await env.orchestrator.run_proactive(tenant, get_trigger("meal_check")) is True
        assert DANISH in last_system(env)
    finally:
        await env.bot.session.close()


# ================================================================== the tools keep working in the mode


async def test_the_basic_tools_are_offered_and_run_in_an_immersion_topic(owner_engine, db):
    env = await build_bot_env(owner_engine, db, save_fact_enabled=True, tasks_enabled=True, sr_enabled=True)
    try:
        await new_persona(owner_engine, env.user_id, "danish", thread_id=DANSK_THREAD, title="Dansk")
        await env.feed("/dansk", thread_id=DANSK_THREAD)
        said = "Разбей на шаги и сделай карточки: подготовка к экзамену. Я бегаю по утрам 5 км"
        calls = (
            ToolCall("decompose_task", {"goal": "Подготовка к экзамену", "steps": [{"title": "Выписать темы"}, {"title": "Решить варианты"}]}),
            ToolCall(
                "create_cards",
                {"deck": "Dansk", "cards": [{"question": "Hund?", "options": ["Кошка", "Собака", "Лошадь", "Птица"], "correct_index": 1, "explanation": "hund = собака"}]},
            ),
            ToolCall(
                "save_fact",
                {"category": "habit", "topic": "morning_run", "content": "Бегает по утрам 5 км", "evidence": "бегаю по утрам 5 км"},
            ),
        )
        env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="Gemt!", calls=calls)
        env.chat.responder = lambda system, turns: "Fedt! Lad os starte med temaerne."
        await env.feed(said, thread_id=DANSK_THREAD)
        offered = {t.name for t in env.chat.tool_calls[0]["tools"]}
        assert {"save_fact", "decompose_task", "create_cards"} <= offered  # nothing is taken away by the mode
        assert DANISH in env.chat.tool_calls[0]["system"] and DANISH in last_system(env)  # and the phrasing turn is in Danish too
        [(tasks,)] = await owner_exec(owner_engine, "SELECT count(*) FROM civ.tasks WHERE user_id = :u", u=env.user_id)
        [(cards,)] = await owner_exec(owner_engine, "SELECT count(*) FROM civ.cards WHERE user_id = :u", u=env.user_id)
        [(facts,)] = await owner_exec(owner_engine, "SELECT count(*) FROM civ.core_facts WHERE user_id = :u AND source::text = 'chat_inline'", u=env.user_id)
        assert (tasks, cards, facts) == (3, 1, 1)  # the root and two steps, one card, one fact
        sent = says(env, DANSK_THREAD)
        assert sent[1].startswith("Fedt! Lad os starte") and "Gemt!" not in " ".join(sent)  # the model's own claim is still never sent
    finally:
        await env.bot.session.close()


def test_every_marker_gated_tool_still_decides_by_the_users_words_not_the_mode():
    from ai_civ.bot.tools.cards import wants_cards
    from ai_civ.bot.tools.decompose import wants_decomposition

    assert wants_decomposition("Разбей на шаги: выучить датский") and wants_cards("Сделай карточки по датскому")
    assert not wants_decomposition("Jeg vil lære dansk") and not wants_cards("Jeg vil lære dansk")  # a Danish sentence is no marker

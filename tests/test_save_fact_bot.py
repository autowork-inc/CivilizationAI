"""v2.0, instant memory: the model may call ``save_fact(category, topic, content, evidence)`` when the user states something lasting about
themselves. The point under test is that a fact reaches Core only if CODE can show it is the user's own word - a verbatim quote of the CURRENT
message, every number in it - and that what the user reads about it (a card with a cancel button) is written by code after the commit, never by
the model. Real PostgreSQL, the real aiogram Dispatcher on a fake Telegram wire, a fake Gemini whose "model" decides in the test what to call."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from ai_civ.bot import texts
from ai_civ.bot.tools import save_fact
from ai_civ.bot.tools.save_fact import SaveFacts, parse_fact
from ai_civ.config import Settings
from ai_civ.db.enums import FactCategory
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_fact, new_persona, new_user, owner_exec
from tests.test_diplomat_bot import FREE, befriend
from tests.test_quick_events_bot import create_call, future
from tests.test_save_fact_db import message
from tests.tg_fakes import make_callback_update

SAID = "Я бегаю по утрам 5 км, это моя привычка"
QUOTE = "бегаю по утрам 5 км"
FACT = "Бегает по утрам 5 км"


def args(**kw: Any) -> dict[str, Any]:
    base = {"category": "habit", "topic": "morning_run", "content": FACT, "evidence": QUOTE}
    return {**base, **kw}


def save_call(**kw: Any) -> ToolCall:
    return ToolCall(save_fact.TOOL_NAME, args(**kw))


# ================================================================== pure: the tool and the call


def test_the_tool_has_a_closed_category_list_and_asks_for_a_quote():
    schema = save_fact.TOOL.parameters
    assert save_fact.TOOL.name == "save_fact" == save_fact.TOOL_NAME
    assert set(schema["required"]) == {"category", "topic", "content", "evidence"}
    assert schema["properties"]["category"]["enum"] == list(save_fact.CATEGORIES)
    assert "VERBATIM" in schema["properties"]["evidence"]["description"]
    assert json.dumps(schema)


def test_the_instructions_forbid_claiming_that_something_was_remembered():
    low = save_fact.INSTRUCTIONS.lower()
    assert "never say you remembered" in low and "never save moods" in low and "secrets" in low


def test_a_valid_call_becomes_a_key_that_names_the_kind_and_the_subject():
    parsed = parse_fact(args(content="  Бегает   по утрам 5 км "), SAID)
    assert parsed is not None
    assert (parsed.key, parsed.content, parsed.category) == ("preference.morning_run", "Бегает по утрам 5 км", "preference")


def test_the_categories_map_onto_the_existing_enum_and_never_onto_goal_or_principle():
    assert {c.name for c in save_fact.CATEGORIES.values()} <= {"preference", "other", "health"}
    assert FactCategory.goal not in save_fact.CATEGORIES.values() and FactCategory.principle not in save_fact.CATEGORIES.values()
    assert save_fact.CATEGORIES["plan"] is FactCategory.other and save_fact.CATEGORIES["habit"] is FactCategory.preference


@pytest.mark.parametrize("quote", [QUOTE.upper(), "  бегаю   по\nутрам 5 км ", "Я бегаю по утрам 5 км, это моя привычка"])
def test_the_quote_may_differ_in_case_and_whitespace_only(quote):
    assert parse_fact(args(evidence=quote), SAID) is not None


@pytest.mark.parametrize(
    "quote",
    [
        "Бегаю, по утрам... 5 км!",  # punctuation the speech-to-text added or dropped
        "бегаю - по утрам - 5 км",
        "«бегаю по утрам» 5 км",
        "БЕГАЮ ПО УТРАМ 5 КМ.",
    ],
)
def test_punctuation_and_case_do_not_matter_for_the_quote(quote):
    assert parse_fact(args(evidence=quote), SAID) is not None


def test_a_transcript_with_other_punctuation_than_the_quote_still_matches():
    spoken = "ну я это, бегаю по утрам - пять... нет, 5 км, да"  # how a voice message comes out
    assert parse_fact(args(evidence="бегаю по утрам 5 км"), "ну я это, бегаю по утрам 5 км, да") is not None
    assert parse_fact(args(evidence="бегаю по утрам - пять... нет, 5 км"), spoken) is not None
    assert parse_fact(args(evidence="бегаю по утрам 5 км"), spoken) is None  # different words in between: not a quote


def test_yo_and_ye_are_the_same_letter_in_a_transcript():
    assert parse_fact(args(content="Пьёт воду", evidence="пьёт воду", topic="water"), "Он пьет воду по утрам") is not None


def test_punctuation_softening_does_not_open_the_numbers_or_the_words():
    assert parse_fact(args(content="Бегает 7 км", evidence="бегаю, по утрам, 5 км"), SAID) is None  # the number is still the user's
    assert parse_fact(args(evidence="бегаю, по утрам, 10 км"), SAID) is None
    assert parse_fact(args(evidence="..."), SAID) is None  # nothing but punctuation


def test_the_word_habit_is_stored_as_a_preference_in_the_key_and_the_column():
    habit = parse_fact(args(category="habit"), SAID)
    pref = parse_fact(args(category="preference"), SAID)
    assert habit is not None and pref is not None
    assert habit.category == "preference" and habit.key == pref.key == "preference.morning_run"
    assert save_fact.CATEGORY_ALIASES == {"habit": "preference"}
    assert parse_fact(args(category=" Habit "), SAID).key == "preference.morning_run"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "evidence",
    ["бегаю по утрам 10 км", "я бегаю по утрам пять километров", "бегаю каждое утро", "", "  ", "аб", "x" * 401],
)
def test_a_quote_that_is_not_the_users_own_words_drops_the_call(evidence):
    assert parse_fact(args(evidence=evidence), SAID) is None


def test_a_quote_from_anywhere_else_is_not_the_users_word():
    assert parse_fact(args(), "Привет, как дела?") is None  # the user's CURRENT message does not say it
    assert parse_fact(args(), "") is None


def test_a_number_the_quote_does_not_contain_drops_the_call_but_a_decimal_comma_is_the_same_number():
    assert parse_fact(args(content="Бегает по утрам 7 км"), SAID) is None
    assert parse_fact(args(content="Бегает 5.5 км", evidence="бегаю 5,5 км"), "Я бегаю 5,5 км") is not None
    assert parse_fact(args(content="Бегает пять км", evidence="бегаю по утрам"), SAID) is not None  # no number to check


@pytest.mark.parametrize(
    "said",
    [
        "мой пароль от почты qwerty12",
        "my password is hunter2",
        "номер карты 4111 1111 1111 1111",
        "телефон +45 12 34 56 78 90",
        "iban DK50 0040 0440 1162 43",
        "api key лежит в заметках",
        "мой токен для бота такой",
        "сид-фраза записана на листке",
    ],
)
def test_anything_that_smells_like_a_secret_is_never_stored(said):
    assert parse_fact(args(content=said, evidence=said), said) is None


@pytest.mark.parametrize(
    "bad",
    [
        {"category": "goal"},
        {"category": "principle"},
        {"category": ""},
        {"category": None},
        {"content": ""},
        {"content": "x" * 301},
        {"content": 5},
        {"content": "‮evil"},
        {"content": "!!!"},
        {"evidence": None},
    ],
)
def test_an_unusable_argument_drops_the_call(bad):
    assert parse_fact(args(**bad), SAID + " ‮evil !!! " + "x" * 301) is None


def test_the_topic_is_cleaned_and_a_bad_one_falls_back_to_a_key_derived_from_the_content():
    assert parse_fact(args(topic=" Morning Run! "), SAID).key == "preference.morning_run"  # type: ignore[union-attr]
    fallback = parse_fact(args(topic="утренняя пробежка"), SAID)
    assert fallback is not None and fallback.key.startswith("preference.f_") and len(fallback.key) == len("preference.f_") + 10
    assert parse_fact(args(topic=None), SAID).key == fallback.key  # type: ignore[union-attr] - the same sentence, the same key
    assert parse_fact(args(topic="1abc"), SAID).key.startswith("preference.f_")  # type: ignore[union-attr] - a slug starts with a letter


def test_the_callback_data_round_trips_and_fits_telegram():
    fact = uuid.uuid4()
    data = save_fact.callback_data(fact)
    assert len(data.encode()) <= 64 and save_fact.parse_callback(data) == fact
    for junk in ("sf:zzz:u", f"sf:{fact}:x", f"qe:{fact}:u", "", f"sf:{fact}"):
        assert save_fact.parse_callback(junk) is None


def test_the_note_for_the_model_says_what_the_system_did_and_forbids_claiming_it():
    done = save_fact.Saved("saved", FACT, uuid.uuid4())
    refused = save_fact.Saved("limit", "Ещё факт")
    note = save_fact.render_note([done, refused])
    assert "saved the facts below" in note and f"- {FACT}" in note and "do not mention the card" in note
    assert "did NOT save" in note and "- Ещё факт" in note


def test_the_defaults_are_on_and_bounded():
    s = Settings(_env_file=None)
    assert s.save_fact_enabled and (s.save_fact_max_calls, s.save_fact_max_per_day, s.save_fact_min_chars) == (3, 40, 8)
    assert save_fact.INLINE_PRIORITY <= 60  # the ceiling the database enforces


# ================================================================== helpers for the database tests


async def facts_of(env, like: str = "%") -> list[dict[str, Any]]:
    rows = await owner_exec(
        env.owner_engine,
        "SELECT key, value_text, category::text, priority, source::text, source_message_id, valid_from, valid_to, persona_id, id "
        "FROM civ.core_facts WHERE user_id = :u AND key LIKE :k ORDER BY valid_from, id",
        u=env.user_id,
        k=like,
    )
    names = ("key", "value", "category", "priority", "source", "message", "valid_from", "valid_to", "persona", "id")
    return [dict(zip(names, row, strict=True)) for row in rows]


async def current(env, like: str = "%") -> list[dict[str, Any]]:
    return [f for f in await facts_of(env, like) if f["valid_to"] is None]


def engine_of(env, **settings_kw: Any) -> SaveFacts:
    """The engine of this stack, or one with other limits (same database, same fake Telegram)."""
    if not settings_kw:
        return env.orchestrator.facts
    return SaveFacts(env.db, env.bot, Settings(_env_file=None, **settings_kw))


async def said(env) -> uuid.UUID:
    """A message row of the user (the fact's provenance); the engine tests do not need the text itself in the table."""
    return await message(env.owner_engine, env.user_id)


def memory_cards(env, thread_id: int | None = None) -> list[Any]:
    return [
        m for m in env.session.sent()
        if m.reply_markup is not None and m.chat_id == env.chat_id and m.message_thread_id == thread_id
        and m.reply_markup.inline_keyboard[0][0].callback_data.startswith("sf:")
    ]  # fmt: skip


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


async def press(env, data: str, *, thread_id: int | None = None) -> None:
    await env.dispatcher.feed_update(
        env.bot,
        make_callback_update(data=data, user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=777, thread_id=thread_id),
    )


def model_saves(env, *calls: ToolCall, text: str = "Запомнил!", then: str = "Звучит бодро.") -> None:
    """The fake model calls the function(s) - and, as the real one might, ALSO claims that it remembered."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


@asynccontextmanager
async def stack(owner_engine, db, **settings_kw: Any):
    """A stack with other settings than ``fact_env`` (for example quick events on as well, or a smaller daily limit)."""
    env = await build_bot_env(owner_engine, db, **{"save_fact_enabled": True, **settings_kw})
    try:
        yield env
    finally:
        await env.bot.session.close()


# ================================================================== saving (the engine and the database)


async def test_a_valid_call_is_saved_as_a_chat_fact_with_its_message(fact_env):
    env = fact_env
    msg = await said(env)
    [saved] = await engine_of(env).apply(env.user_id, msg, SAID, [save_call()])
    assert (saved.outcome, saved.content) == ("saved", FACT) and saved.fact_id is not None
    [fact] = await facts_of(env)
    assert (fact["key"], fact["value"], fact["category"], fact["priority"], fact["source"]) == (
        "preference.morning_run", FACT, "preference", 50, "chat_inline",
    )  # fmt: skip
    assert fact["message"] == msg and fact["valid_to"] is None and fact["persona"] is None and fact["id"] == saved.fact_id


async def test_the_same_fact_said_again_changes_nothing(fact_env):
    env = fact_env
    engine = engine_of(env)
    await engine.apply(env.user_id, await said(env), SAID, [save_call()])
    [again] = await engine.apply(env.user_id, await said(env), SAID, [save_call(content=" бегает  по утрам 5 КМ ")])
    assert again.outcome == "unchanged" and len(await facts_of(env)) == 1


async def test_a_changed_fact_is_a_new_version_and_the_old_one_is_closed_at_the_same_instant(fact_env):
    env = fact_env
    engine = engine_of(env)
    await engine.apply(env.user_id, await said(env), SAID, [save_call()])
    now_said = "Теперь я бегаю по утрам 8 км"
    [updated] = await engine.apply(env.user_id, await said(env), now_said, [save_call(content="Бегает по утрам 8 км", evidence="бегаю по утрам 8 км")])
    old, new = await facts_of(env)
    assert updated.outcome == "updated" and new["id"] == updated.fact_id
    assert old["valid_to"] == new["valid_from"] and new["valid_to"] is None and (old["value"], new["value"]) == (FACT, "Бегает по утрам 8 км")
    assert len(await current(env, "preference.%")) == 1  # one current version per key (the database's unique index)


async def test_a_fact_the_user_ranked_higher_is_protected_from_a_chat_line(fact_env):
    env = fact_env
    await new_fact(env.owner_engine, env.user_id, "preference.morning_run", category="preference", priority=70, value="Не бегаю")
    [protected] = await engine_of(env).apply(env.user_id, await said(env), SAID, [save_call()])
    assert protected.outcome == "protected" and protected.fact_id is None
    [fact] = await facts_of(env)
    assert (fact["value"], fact["priority"], fact["valid_to"]) == ("Не бегаю", 70, None)


async def test_a_fact_at_the_chat_priority_may_be_replaced(fact_env):
    env = fact_env
    await new_fact(env.owner_engine, env.user_id, "preference.morning_run", category="preference", priority=50, value="Не бегаю")
    [updated] = await engine_of(env).apply(env.user_id, await said(env), SAID, [save_call()])
    assert updated.outcome == "updated" and len(await facts_of(env)) == 2


async def test_a_chatty_model_is_capped_per_message(fact_env):
    env = fact_env
    calls = [save_call(topic=f"habit_{i}") for i in range(4)]  # four different subjects
    assert len(await engine_of(env, save_fact_max_calls=2).apply(env.user_id, await said(env), SAID, calls)) == 2


async def test_the_same_fact_twice_in_one_message_is_one_fact(fact_env):
    env = fact_env
    assert len(await engine_of(env).apply(env.user_id, await said(env), SAID, [save_call(), save_call()])) == 1
    assert len(await facts_of(env)) == 1


async def test_the_daily_limit_counts_the_versions_written_in_the_last_24_hours(fact_env):
    env = fact_env
    for old in ("old_1", "old_2"):  # two chat facts from two days ago (versions are immutable, so they are written with their age)
        await owner_exec(
            env.owner_engine,
            "INSERT INTO civ.core_facts (user_id, category, key, value_text, priority, source, source_message_id, valid_from) "
            "VALUES (:u, 'preference', :k, 'x', 50, CAST('chat_inline' AS civ.fact_source), :m, now() - interval '2 days')",
            u=env.user_id,
            k=f"preference.{old}",
            m=await said(env),
        )
    engine = engine_of(env, save_fact_max_per_day=2)
    kinds: list[str] = []
    for i in range(3):
        kinds += [o.outcome for o in await engine.apply(env.user_id, await said(env), SAID, [save_call(topic=f"habit_{i}")])]
    assert kinds == ["saved", "saved", "limit"]  # the two old versions do not count, the two new ones do


async def test_a_database_failure_makes_no_outcome_and_never_raises(fact_env, monkeypatch):
    env = fact_env

    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(SaveFacts, "_save", boom)
    assert await engine_of(env).apply(env.user_id, await said(env), SAID, [save_call()]) == []
    assert await facts_of(env) == []


async def test_calls_that_are_not_ours_or_not_usable_save_nothing(fact_env):
    env = fact_env
    bad = [ToolCall("create_event", args()), save_call(evidence="выдуманная цитата"), save_call(category="goal")]
    assert await engine_of(env).apply(env.user_id, await said(env), SAID, bad) == [] and await facts_of(env) == []


async def test_parallel_saves_of_one_new_fact_leave_exactly_one_current_version(fact_env):
    env = fact_env
    engine = engine_of(env)
    messages = [await said(env) for _ in range(6)]
    results = await asyncio.gather(*(engine.apply(env.user_id, m, SAID, [save_call()]) for m in messages))
    outcomes = sorted(o.outcome for r in results for o in r)
    assert outcomes == ["saved"] + ["unchanged"] * 5  # the advisory lock orders them: the others see the winner's fact
    assert len(await facts_of(env)) == 1


# ================================================================== undo (the engine)


async def test_undo_closes_the_version_and_a_second_press_is_stale(fact_env):
    env = fact_env
    [saved] = await engine_of(env).apply(env.user_id, await said(env), SAID, [save_call()])
    assert (await engine_of(env).undo(env.user_id, saved.fact_id)).outcome == "undone"
    assert await current(env) == [] and len(await facts_of(env)) == 1  # the history stays
    again = await engine_of(env).undo(env.user_id, saved.fact_id)
    assert (again.outcome, again.content) == ("stale", FACT)


async def test_undoing_an_update_brings_the_old_value_back_as_a_new_version(fact_env):
    env = fact_env
    engine = engine_of(env)
    await engine.apply(env.user_id, await said(env), SAID, [save_call()])
    [updated] = await engine.apply(
        env.user_id, await said(env), "Теперь я бегаю по утрам 8 км", [save_call(content="Бегает по утрам 8 км", evidence="бегаю по утрам 8 км")]
    )
    result = await engine.undo(env.user_id, updated.fact_id)
    assert (result.outcome, result.content, result.previous) == ("restored", "Бегает по утрам 8 км", FACT)
    first, second, third = await facts_of(env)
    assert first["valid_to"] == second["valid_from"] and second["valid_to"] == third["valid_from"] and third["valid_to"] is None
    assert (third["value"], third["source"], third["message"]) == (FACT, "chat_inline", first["message"])  # a copy of the old version


async def test_undoing_a_fact_that_replaced_a_manual_one_restores_the_manual_one(fact_env):
    env = fact_env
    await new_fact(env.owner_engine, env.user_id, "preference.morning_run", category="preference", priority=40, value="Бегаю 3 км")
    [updated] = await engine_of(env).apply(env.user_id, await said(env), SAID, [save_call()])
    result = await engine_of(env).undo(env.user_id, updated.fact_id)
    assert (result.outcome, result.previous) == ("restored", "Бегаю 3 км")
    [back] = await current(env)
    assert (back["value"], back["source"], back["priority"]) == ("Бегаю 3 км", "user_manual", 40)


async def test_undo_after_the_fact_moved_on_changes_nothing(fact_env):
    env = fact_env
    engine = engine_of(env)
    [first] = await engine.apply(env.user_id, await said(env), SAID, [save_call()])
    await engine.apply(env.user_id, await said(env), "Теперь я бегаю по утрам 8 км", [save_call(content="Бегает 8 км", evidence="бегаю по утрам 8 км")])
    assert (await engine.undo(env.user_id, first.fact_id)).outcome == "stale"
    assert [f["value"] for f in await current(env)] == ["Бегает 8 км"]


async def test_a_forged_or_foreign_button_reaches_nothing(fact_env):
    env = fact_env
    other = await new_user(env.owner_engine)
    [saved] = await engine_of(env).apply(env.user_id, await said(env), SAID, [save_call()])
    manual = await new_fact(env.owner_engine, env.user_id, "preference.other", category="preference", priority=50)
    assert (await engine_of(env).undo(other, saved.fact_id)).outcome == "gone"  # another tenant: RLS hides the row
    assert (await engine_of(env).undo(env.user_id, manual)).outcome == "gone"  # not a fact the chat wrote
    assert (await engine_of(env).undo(env.user_id, uuid.uuid4())).outcome == "gone"
    assert len(await current(env)) == 2


async def test_parallel_presses_undo_exactly_once(fact_env):
    env = fact_env
    engine = engine_of(env)
    [saved] = await engine.apply(env.user_id, await said(env), SAID, [save_call()])
    results = await asyncio.gather(*(engine.undo(env.user_id, saved.fact_id) for _ in range(6)))
    assert sorted(r.outcome for r in results) == ["stale"] * 5 + ["undone"]


# ================================================================== the whole flow (Dispatcher, fake Telegram, fake Gemini)


async def test_a_stated_habit_is_saved_and_the_card_comes_from_code_not_from_the_models_claim(fact_env):
    env = fact_env
    model_saves(env, save_call())
    await env.feed(SAID)
    assert says(env) == ["Звучит бодро.", texts.FACT_CARD.format(content=FACT)]  # the phrased reply, then the card; "Запомнил!" never goes out
    [card] = memory_cards(env)
    [fact] = await facts_of(env)
    assert card.reply_markup.inline_keyboard[0][0].callback_data == f"sf:{fact['id']}:u" and card.reply_markup.inline_keyboard[0][0].text == texts.FACT_UNDO
    stored_user = [m for m in await env.messages() if m["role"] == "user"]
    assert fact["message"] is not None and [m["content"] for m in stored_user] == [SAID]
    [offered] = env.chat.tool_calls
    assert [t.name for t in offered["tools"]] == ["save_fact"] and "## Memory" in offered["system"]
    [phrased] = env.chat.calls
    assert "Memory (made by the system)" in phrased["system"] and FACT in phrased["system"]
    assert [m["content"] for m in await env.messages() if m["role"] == "assistant"] == ["Звучит бодро."]  # the card is not a word of the assistant


async def test_the_next_message_sees_the_fact_in_the_core_profile_at_once(fact_env):
    env = fact_env
    model_saves(env, save_call())
    await env.feed(SAID)
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="Понял.")
    await env.feed("А что мне съесть после пробежки?")
    assert f"preference.morning_run [preference]: {FACT}" in env.chat.tool_calls[-1]["system"]


async def test_the_cancel_button_undoes_the_fact_and_a_second_press_says_it_is_stale(fact_env):
    env = fact_env
    model_saves(env, save_call())
    await env.feed(SAID)
    [card] = memory_cards(env)
    data = card.reply_markup.inline_keyboard[0][0].callback_data
    await press(env, data)
    assert await current(env) == []
    assert env.session.edits()[-1].text == texts.FACT_UNDONE.format(content=FACT) and env.session.callback_answers()[-1].text == texts.FACT_TOAST["undone"]
    await press(env, data)
    assert env.session.edits()[-1].text == texts.FACT_STALE and env.session.callback_answers()[-1].text == texts.FACT_TOAST["stale"]
    assert len(await facts_of(env)) == 1


async def test_an_update_gets_its_own_card_and_the_button_restores_the_old_value(fact_env):
    env = fact_env
    model_saves(env, save_call())
    await env.feed(SAID)
    model_saves(env, save_call(content="Бегает по утрам 8 км", evidence="бегаю по утрам 8 км"))
    await env.feed("Теперь я бегаю по утрам 8 км")
    _first, second = memory_cards(env)
    assert second.text == texts.FACT_CARD_UPDATED.format(content="Бегает по утрам 8 км")
    await press(env, second.reply_markup.inline_keyboard[0][0].callback_data)
    assert [f["value"] for f in await current(env)] == [FACT]
    assert env.session.edits()[-1].text == texts.FACT_RESTORED.format(content="Бегает по утрам 8 км", previous=FACT)


async def test_a_quote_the_user_did_not_write_never_becomes_a_fact_a_claim_or_a_card(fact_env):
    env = fact_env
    model_saves(env, save_call(evidence="я бегаю по утрам 10 км"), text="Запомнил!", then="Понял, расскажите подробнее.")
    await env.feed(SAID)
    assert says(env) == ["Понял, расскажите подробнее."] and memory_cards(env) == [] and await facts_of(env) == []  # the model's words are not trusted


async def test_a_quote_from_an_earlier_message_is_not_a_quote_of_this_one(fact_env):
    env = fact_env
    env.chat.responder = lambda system, turns: "Понял."
    await env.feed(SAID)
    model_saves(env, save_call(), then="Хорошо.")  # the model digs the old sentence out of the context buffer
    await env.feed("Да, и ещё вот что скажу вам")
    assert await facts_of(env) == [] and memory_cards(env) == []


async def test_a_secret_is_never_stored_even_if_the_model_insists(fact_env):
    env = fact_env
    secret = "Мой пароль от почты qwerty12345, запомни его"
    model_saves(env, save_call(category="other", topic="mail", content="Пароль от почты qwerty12345", evidence="пароль от почты qwerty12345"), then="Такое не храню.")
    await env.feed(secret)
    assert await facts_of(env) == [] and says(env) == ["Такое не храню."]


async def test_a_very_short_message_is_not_offered_the_function_at_all(fact_env):
    env = fact_env
    await env.feed("да")
    assert env.chat.tool_calls == [] and len(env.chat.calls) == 1


async def test_with_the_feature_off_the_model_is_never_offered_the_function(bot_env):
    env = bot_env  # SAVE_FACT_ENABLED=false in this stack
    await env.feed(SAID)
    assert env.chat.tool_calls == [] and len(env.chat.calls) == 1


async def test_the_function_can_be_switched_off_in_the_settings(owner_engine, db):
    async with stack(owner_engine, db, save_fact_enabled=False) as env:
        await env.feed(SAID)
        assert env.chat.tool_calls == [] and await facts_of(env) == []


async def test_a_voice_note_is_saved_from_its_transcript(fact_env):
    env = fact_env
    env.chat.transcriber = lambda data, mime_type: SAID
    model_saves(env, save_call())
    await env.feed_voice()
    [fact] = await facts_of(env)
    assert fact["value"] == FACT and len(memory_cards(env)) == 1


async def test_in_a_topic_the_card_goes_to_that_topic_and_the_fact_stays_global(fact_env):
    env = fact_env
    await new_persona(env.owner_engine, env.user_id, "sport", thread_id=612)
    model_saves(env, save_call())
    await env.feed(SAID, thread_id=612)
    assert len(memory_cards(env, thread_id=612)) == 1 and memory_cards(env) == []
    assert (await facts_of(env))[0]["persona"] is None  # one brain: what the user said in a topic is true everywhere


async def test_a_fact_the_user_ranked_higher_is_not_touched_and_the_user_is_told_where_to_change_it(fact_env):
    env = fact_env
    await new_fact(env.owner_engine, env.user_id, "preference.morning_run", category="preference", priority=70, value="Не бегаю")
    model_saves(env, save_call())
    await env.feed(SAID)
    assert says(env) == [f"Звучит бодро.\n\n{texts.FACT_PROTECTED}"] and memory_cards(env) == []  # the fixed text follows the phrased reply
    assert [(f["value"], f["priority"]) for f in await current(env)] == [("Не бегаю", 70)]


async def test_the_daily_limit_is_explained_by_a_fixed_text(owner_engine, db):
    async with stack(owner_engine, db, save_fact_max_per_day=1) as env:
        model_saves(env, save_call())
        await env.feed(SAID)
        model_saves(env, save_call(topic="swim", content="Плавает по вечерам", evidence="плаваю по вечерам"))
        await env.feed("Ещё я плаваю по вечерам, это важно")
        assert says(env)[-1] == f"Звучит бодро.\n\n{texts.FACT_LIMIT}" and len(memory_cards(env)) == 1 and len(await facts_of(env)) == 1


async def test_the_same_fact_said_again_is_answered_as_usual_and_has_no_second_card(fact_env):
    env = fact_env
    model_saves(env, save_call())
    await env.feed(SAID)
    await env.feed(SAID + ", повторяю")
    assert len(memory_cards(env)) == 1 and len(await facts_of(env)) == 1
    assert says(env)[-1] == "Звучит бодро."  # nothing was announced, nothing was claimed: the reply is phrased without the function's words


async def test_a_database_failure_costs_the_card_not_the_reply(fact_env, monkeypatch):
    env = fact_env

    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(SaveFacts, "_save", boom)
    model_saves(env, save_call(), text="Запомнил!", then="Звучит бодро.")
    await env.feed(SAID)
    assert says(env) == ["Звучит бодро."] and texts.FAILURE not in says(env) and memory_cards(env) == []


async def test_a_plain_message_is_answered_as_usual_and_has_no_card(fact_env):
    env = fact_env
    env.chat.responder = lambda system, turns: "Привет!"
    await env.feed("Привет, как дела у тебя?")
    assert says(env) == ["Привет!"] and memory_cards(env) == [] and await facts_of(env) == []


async def test_an_unknown_or_foreign_button_is_harmless(fact_env):
    env = fact_env
    await press(env, f"sf:{uuid.uuid4()}:u")
    assert env.session.callback_answers()[-1].text == texts.FACT_TOAST["gone"]
    await press(env, "sf:not-a-uuid:u")
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_UNKNOWN_BUTTON


async def test_a_failing_button_is_visible_and_changes_nothing(fact_env, monkeypatch):
    env = fact_env
    model_saves(env, save_call())
    await env.feed(SAID)
    [card] = memory_cards(env)

    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(SaveFacts, "undo", boom)
    await press(env, card.reply_markup.inline_keyboard[0][0].callback_data)
    assert env.session.callback_answers()[-1].text == texts.FAILURE and env.session.callback_answers()[-1].show_alert
    assert len(await current(env)) == 1


async def test_a_plan_and_a_habit_in_one_message_share_one_phrasing_turn_and_both_cards_come_after(owner_engine, db):
    async with stack(owner_engine, db, quick_events_enabled=True) as env:
        said_both = "Я бегаю по утрам 5 км, а сегодня иду на 2 часа смотреть стенку"
        model_saves(env, create_call(start=future()[0], duration_minutes=120), save_call(), text="", then="Отличный план!")
        await env.feed(said_both)
        [offered] = env.chat.tool_calls
        assert [t.name for t in offered["tools"]] == ["create_event", "save_fact"]
        [phrased] = env.chat.calls
        assert "Calendar suggestion" in phrased["system"] and "Memory (made by the system)" in phrased["system"]
        sent = says(env)
        assert sent[0] == "Отличный план!" and sent[1].startswith("Занести в календарь") and sent[2] == texts.FACT_CARD.format(content=FACT)


async def test_a_user_with_contacts_is_offered_the_functions_in_a_fixed_order(owner_engine, db):
    async with stack(owner_engine, db, quick_events_enabled=True) as env:
        await befriend(env, granted=FREE)
        model_saves(env, save_call())
        await env.feed(SAID)
        [offered] = env.chat.tool_calls
        assert [t.name for t in offered["tools"]] == ["create_event", "ask_contact", "propose_event", "save_fact"]
        assert len(memory_cards(env)) == 1


async def test_another_tenants_press_cannot_undo_this_ones_fact(fact_env, owner_engine, db):
    env = fact_env
    model_saves(env, save_call())
    await env.feed(SAID)
    [card] = memory_cards(env)
    async with stack(owner_engine, db) as other:
        await other.dispatcher.feed_update(
            other.bot,
            make_callback_update(
                data=card.reply_markup.inline_keyboard[0][0].callback_data, user_id=other.telegram_user_id, chat_id=other.chat_id, message_id=778
            ),
        )
        assert other.session.callback_answers()[-1].text == texts.FACT_TOAST["gone"]  # RLS: the other tenant's press found nothing
    assert [f["value"] for f in await current(env)] == [FACT]


# ================================================================== the card cleans itself up: "✅ ОК" and the 15-second timer

ACK = save_fact.ACK_CALLBACK


async def press_on(env, data: str, message_id: int) -> None:
    await env.dispatcher.feed_update(
        env.bot, make_callback_update(data=data, user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=message_id)
    )


def card_message_id(env) -> int:
    """The Telegram id of the memory card, as the fake wire handed it out."""
    [card] = memory_cards(env)
    return env.session.sent_ids[env.session.sent().index(card)]


@asynccontextmanager
async def timed_env(owner_engine, db, ttl: float):
    env = await build_bot_env(owner_engine, db, save_fact_enabled=True, save_fact_card_ttl_seconds=ttl)
    try:
        yield env
    finally:
        await env.orchestrator.janitor.aclose()
        await env.bot.session.close()


async def test_the_card_carries_undo_and_ok_buttons_and_the_default_timer_is_15_seconds(fact_env):
    env = fact_env
    assert Settings(_env_file=None).save_fact_card_ttl_seconds == 15.0
    model_saves(env, save_call())
    await env.feed(SAID)
    [card] = memory_cards(env)
    undo, ok = card.reply_markup.inline_keyboard[0]
    assert undo.text == texts.FACT_UNDO and undo.callback_data.startswith("sf:")
    assert ok.text == "✅ ОК" and ok.callback_data == "fact_ack"


async def test_ok_deletes_the_card_at_once_and_keeps_the_fact(fact_env):
    env = fact_env
    model_saves(env, save_call())
    await env.feed(SAID)
    await press_on(env, ACK, 777)
    [deleted] = env.session.deletes()
    assert deleted.chat_id == env.chat_id and deleted.message_id == 777  # exactly the message the button sat under
    assert env.session.callback_answers()[-1].text is None  # a silent answer: no toast
    assert [f["value"] for f in await current(env)] == [FACT]  # "ОК" is not a cancel


async def test_ok_falls_back_to_removing_the_buttons_when_telegram_will_not_delete(fact_env):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import EditMessageReplyMarkup

    env = fact_env
    env.session.fail_on("deleteMessage", TelegramBadRequest, message="Bad Request: message can't be deleted")
    await press_on(env, ACK, 777)
    [edit] = [c for c in env.session.calls if isinstance(c, EditMessageReplyMarkup)]
    assert edit.message_id == 777 and edit.reply_markup is None


async def test_ok_pressed_by_a_stranger_deletes_nothing(fact_env, owner_engine, db):
    env = fact_env
    async with stack(owner_engine, db) as other:
        await other.dispatcher.feed_update(
            other.bot, make_callback_update(data=ACK, user_id=env.telegram_user_id, chat_id=other.chat_id, message_id=5)
        )  # the tenant is the presser: someone else's chat is not theirs
    assert env.session.deletes() == [] and other.session.deletes() == []


async def test_the_card_deletes_itself_after_the_timeout_and_the_fact_stays(owner_engine, db):
    async with timed_env(owner_engine, db, 0.2) as env:
        model_saves(env, save_call())
        await env.feed(SAID)
        assert env.session.deletes() == [] and env.orchestrator.janitor.pending == 1
        await asyncio.sleep(0.6)
        [deleted] = env.session.deletes()
        assert deleted.chat_id == env.chat_id and deleted.message_id == card_message_id(env)
        assert env.orchestrator.janitor.pending == 0
        assert [f["value"] for f in await current(env)] == [FACT]


async def test_undo_before_the_timeout_cancels_the_deletion(owner_engine, db):
    async with timed_env(owner_engine, db, 0.3) as env:
        model_saves(env, save_call())
        await env.feed(SAID)
        [card] = memory_cards(env)
        await press_on(env, card.reply_markup.inline_keyboard[0][0].callback_data, card_message_id(env))
        assert await current(env) == [] and env.session.edits()[-1].text == texts.FACT_UNDONE.format(content=FACT)
        await asyncio.sleep(0.7)
        assert env.session.deletes() == []  # the confirmation stays: the user answered the card


async def test_ok_before_the_timeout_deletes_once_not_twice(owner_engine, db):
    async with timed_env(owner_engine, db, 0.3) as env:
        model_saves(env, save_call())
        await env.feed(SAID)
        await press_on(env, ACK, card_message_id(env))
        await asyncio.sleep(0.7)
        assert len(env.session.deletes()) == 1


async def test_a_zero_timeout_schedules_nothing(owner_engine, db):
    async with timed_env(owner_engine, db, 0.0) as env:
        model_saves(env, save_call())
        await env.feed(SAID)
        assert env.orchestrator.janitor.pending == 0 and len(memory_cards(env)) == 1


async def test_a_failed_auto_delete_is_only_logged(owner_engine, db):
    from aiogram.exceptions import TelegramBadRequest

    async with timed_env(owner_engine, db, 0.1) as env:
        env.session.fail_on("deleteMessage", TelegramBadRequest, message="Bad Request: message to delete not found")
        model_saves(env, save_call())
        await env.feed(SAID)
        await asyncio.sleep(0.4)
        assert len(env.session.deletes()) == 1 and env.orchestrator.janitor.pending == 0  # tried once, no loop, no crash


async def test_the_janitor_keeps_one_timer_per_message_and_drops_them_on_close(owner_engine, db):
    async with timed_env(owner_engine, db, 0.0) as env:
        janitor = env.orchestrator.janitor
        janitor.delete_later(1, 2, 10)
        janitor.delete_later(1, 2, 10)  # rescheduling replaces the timer
        janitor.delete_later(1, 3, 10)
        janitor.delete_later(1, 4, 0)  # no timer for a non-positive delay
        assert janitor.pending == 2
        assert janitor.cancel(1, 2) is True and janitor.cancel(1, 2) is False
        await janitor.aclose()
        assert janitor.pending == 0 and env.session.deletes() == []

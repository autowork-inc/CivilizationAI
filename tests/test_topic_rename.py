"""Autonomous topic renaming (v1.1, migration 0009): the model may call ``rename_current_topic``; the code validates the name, takes the right in the
database (a cooldown that survives restarts and cannot be raced), renames through the safe wrapper and announces it with a fixed text.
Real PostgreSQL, real aiogram Dispatcher on a fake Telegram wire, fake model. NOT verified here (needs a live model / Telegram): whether the model
calls the function at the right moments (the prompt asks for restraint), and Telegram's real limits for ``editForumTopic``."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError

from ai_civ.bot import personas, texts, topic_rename
from ai_civ.bot.personas import RenameClaim
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.helpers import new_persona, owner_exec
from tests.test_quick_events_bot import create_call, future
from tests.tg_fakes import make_topic_edited_update

THREAD = 501
OLD = "🛡 Дипломат"
NEW = "Изучение датского"
SHOWN = "📌 Изучение датского"  # what the code makes of NEW: one line, always an emoji
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)


def rename_call(name: str = NEW, **kw: Any) -> ToolCall:
    return ToolCall(topic_rename.TOOL_NAME, {"new_name": name, **kw})


def says(env, thread_id: int | None = THREAD) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


def model_renames(env, *calls: ToolCall, text: str = "Переименовал тему!", then: str = "Давай про датский.") -> None:
    """The fake model calls the function(s) - and, as the real one might, ALSO claims the topic is renamed."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


async def topic(env, code: str = "diplomat", title: str = OLD, thread_id: int | None = THREAD, **stamp: Any) -> uuid.UUID:
    persona_id = await new_persona(env.owner_engine, env.user_id, code, thread_id=thread_id, title=title, system_prompt=f'topic "{title}" prompt')
    if stamp:
        await restamp(env, persona_id, **stamp)
    return persona_id


async def restamp(env, persona_id: uuid.UUID, *, ago: timedelta | None, source: str = "bot") -> None:
    """Put the title stamp ``ago`` into the past (None = never renamed)."""
    await owner_exec(
        env.owner_engine,
        "UPDATE civ.personas SET title_changed_at = CASE WHEN CAST(:a AS float8) IS NULL THEN NULL ELSE now() - make_interval(secs => :a) END, "
        "title_source = :s WHERE id = :p",
        a=None if ago is None else ago.total_seconds(), s=source, p=persona_id,
    )  # fmt: skip


async def row(env, persona_id: uuid.UUID) -> dict[str, Any]:
    r = (
        await owner_exec(
            env.owner_engine,
            "SELECT title, title_source, title_changed_at, system_prompt FROM civ.personas WHERE id = :p",
            p=persona_id,
        )
    )[0]
    return {"title": r[0], "source": r[1], "changed_at": r[2], "prompt": r[3]}


def offered(env) -> list[str]:
    """The function names the model was offered on the last turn with functions."""
    return [t.name for t in env.chat.tool_calls[-1]["tools"]] if env.chat.tool_calls else []


# ================================================================== pure: the tool, the instructions, the name


def test_the_tool_takes_one_required_name_and_is_plain_json():
    assert topic_rename.TOOL.name == "rename_current_topic"
    assert topic_rename.TOOL.parameters["required"] == ["new_name"] and json.dumps(topic_rename.TOOL.parameters)
    assert "when in doubt, do NOT call it" in topic_rename.TOOL.description


def test_the_instructions_name_the_topic_give_the_example_and_ask_for_restraint():
    text = topic_rename.instructions('🛡 "Дипломат"')
    assert "## Topic name" in text and "'Дипломат'" in text  # the current name, quotes defused
    assert "Изучение датского" in text and "completely different, substantial subject" in text
    assert "at most once a day" in text and "when in doubt, do not call it" in text
    assert "never say it was done" in text  # the system announces the rename itself


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Изучение датского", SHOWN),  # no emoji: the code adds one, like for every topic name of the bot
        ("🇩🇰 Датский", "🇩🇰 Датский"),
        ("  Изучение   датского \n", SHOWN),  # one line
        ("x" * 64, "📌 " + "x" * 62),  # exactly the limit is fine; the emoji we add is cut into the 64 like everywhere
    ],
)
def test_a_good_name_is_normalised_like_every_topic_name_of_the_bot(raw, expected):
    assert topic_rename.parse_rename([rename_call(raw)], OLD) == expected


@pytest.mark.parametrize(
    "bad",
    [None, 7, ["x"], "", "   ", "...", "🙂", "x" * 65, "Дат\x00ский", "Дат‮ский", "Дат​ский", "Дат\x07ский"],
)
def test_an_unusable_name_is_dropped_never_repaired_or_cut(bad):
    assert topic_rename.parse_rename([ToolCall(topic_rename.TOOL_NAME, {"new_name": bad})], OLD) is None
    assert topic_rename.parse_rename([ToolCall(topic_rename.TOOL_NAME, {})], OLD) is None


def test_the_name_the_topic_already_has_is_not_a_rename():
    assert topic_rename.parse_rename([rename_call("🛡 дипломат")], "🛡  Дипломат") is None  # case and spacing do not make a new name


def test_only_the_first_call_counts_and_other_functions_are_ignored():
    calls = [ToolCall("ask_contact", {"contact": "c1"}), rename_call("Первое"), rename_call("Второе")]
    assert topic_rename.parse_rename(calls, OLD) == "📌 Первое"
    assert topic_rename.parse_rename([ToolCall("ask_contact", {})], OLD) is None
    assert topic_rename.parse_rename([rename_call(""), rename_call("Второе")], OLD) is None  # a bad first call is not skipped in favour of the next


def test_the_note_for_the_phrasing_turn_forbids_mentioning_the_rename():
    note = topic_rename.render_note('Дат"ский')
    assert "Topic rename (made by the system)" in note and "Do not mention the rename" in note and "'" in note


# ================================================================== pure: when the function is offered


def persona_row(**kw: Any):
    base = {"is_active": True, "telegram_thread_id": THREAD, "code": "diplomat", "title_changed_at": None, "title_source": "bot"}
    from types import SimpleNamespace

    return SimpleNamespace(**{**base, **kw})


NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
LIMITS = {"bot_cooldown": DAY, "user_cooldown": 7 * DAY, "now": NOW}


@pytest.mark.parametrize(
    ("kw", "allowed"),
    [
        ({}, True),  # never renamed
        ({"title_changed_at": NOW - 23 * HOUR}, False),
        ({"title_changed_at": NOW - 24 * HOUR}, True),
        ({"title_changed_at": NOW - 3 * DAY, "title_source": "user"}, False),  # the user's own name is respected for longer
        ({"title_changed_at": NOW - 7 * DAY, "title_source": "user"}, True),
        ({"code": "settings"}, False),  # the system topic is never renamed
        ({"telegram_thread_id": None}, False),
        ({"is_active": False}, False),
    ],
)
def test_the_function_is_offered_only_where_a_rename_is_allowed(kw, allowed):
    assert personas.rename_allowed(persona_row(**kw), **LIMITS) is allowed


# ================================================================== the database: the right to rename


async def claim(db, user_id, persona_id, **kw) -> RenameClaim | None:
    async with db.tenant_session(user_id) as session:
        return await personas.claim_rename(session, persona_id, bot_cooldown=kw.get("bot", DAY), user_cooldown=kw.get("user", 7 * DAY))


async def test_a_topic_never_renamed_can_be_claimed_once_and_then_waits_out_the_cooldown(rename_env):
    env = rename_env
    pid = await topic(env)
    first = await claim(env.db, env.user_id, pid)
    assert first is not None and first.previous_changed_at is None and first.previous_source == "bot"
    assert (await row(env, pid))["changed_at"] == first.claimed_at
    assert await claim(env.db, env.user_id, pid) is None  # the cooldown is already running
    await restamp(env, pid, ago=25 * HOUR)
    assert await claim(env.db, env.user_id, pid) is not None


async def test_after_a_manual_rename_the_longer_cooldown_applies(rename_env):
    env = rename_env
    pid = await topic(env, ago=3 * DAY, source="user")
    assert await claim(env.db, env.user_id, pid) is None
    await restamp(env, pid, ago=8 * DAY, source="user")
    assert await claim(env.db, env.user_id, pid) is not None
    assert (await row(env, pid))["source"] == "bot"  # from now on it is the bot's name


async def test_system_topics_unbound_and_inactive_topics_cannot_be_claimed(rename_env):
    env = rename_env
    settings = await topic(env, "settings", "⚙️ Настройки", 502)
    unbound = await topic(env, "idle", "💤 Пусто", None)
    off = await topic(env, "off", "🔕 Выкл", 503)
    await owner_exec(env.owner_engine, "UPDATE civ.personas SET is_active = false WHERE id = :p", p=off)
    for pid in (settings, unbound, off):
        assert await claim(env.db, env.user_id, pid) is None
    assert await claim(env.db, env.user_id, uuid.uuid4()) is None  # no such topic


async def test_giving_a_claim_back_restores_what_the_row_said_before(rename_env):
    env = rename_env
    pid = await topic(env, ago=30 * HOUR, source="bot")
    before = (await row(env, pid))["changed_at"]
    got = await claim(env.db, env.user_id, pid)
    async with env.db.tenant_session(env.user_id) as session:
        await personas.release_rename(session, pid, got)
    after = await row(env, pid)
    assert after["changed_at"] == before and after["source"] == "bot"
    assert await claim(env.db, env.user_id, pid) is not None  # and the topic can be tried again


async def test_a_claim_is_not_given_back_over_a_manual_rename_that_happened_meanwhile(rename_env):
    env = rename_env
    pid = await topic(env)
    got = await claim(env.db, env.user_id, pid)
    async with env.db.tenant_session(env.user_id) as session:
        await personas.set_title(session, pid, "Моё название", source="user")  # the user renamed it by hand in between
    async with env.db.tenant_session(env.user_id) as session:
        await personas.release_rename(session, pid, got)
    after = await row(env, pid)
    assert after["source"] == "user" and after["title"] == "Моё название"  # the user's stamp survives


async def test_eight_simultaneous_claims_give_the_right_to_exactly_one(rename_env):
    env = rename_env
    pid = await topic(env)
    results = await asyncio.gather(*(claim(env.db, env.user_id, pid) for _ in range(8)))
    assert sum(r is not None for r in results) == 1  # FOR UPDATE: the losers see the winner's stamp


async def test_another_tenant_cannot_claim_my_topic(rename_env, owner_engine, db):
    from tests.helpers import new_user

    env = rename_env
    pid = await topic(env)
    stranger = await new_user(owner_engine)
    assert await claim(db, stranger, pid) is None  # RLS: the row does not exist for them
    assert (await row(env, pid))["changed_at"] is None


async def test_a_new_forum_topic_counts_as_just_named_and_set_title_stamps_only_when_told(rename_env):
    env = rename_env
    pid = await new_persona(env.owner_engine, env.user_id, "fresh", thread_id=None, title="🆕 Новое")
    async with env.db.tenant_session(env.user_id) as session:
        assert await personas.attach_topic(session, pid, 777)
    stamped = await row(env, pid)
    assert stamped["changed_at"] is not None and stamped["source"] == "bot"
    assert await claim(env.db, env.user_id, pid) is None  # the name was chosen a moment ago: no rename yet

    await restamp(env, pid, ago=None)
    async with env.db.tenant_session(env.user_id) as session:
        await personas.set_title(session, pid, "Только запись")  # bringing our record in line: nobody renamed anything
    assert (await row(env, pid))["changed_at"] is None
    async with env.db.tenant_session(env.user_id) as session:
        await personas.set_title(session, pid, "Вручную", source="user")
    assert (await row(env, pid))["source"] == "user" and (await row(env, pid))["changed_at"] is not None


async def test_the_source_column_only_takes_the_two_known_values(rename_env):
    env = rename_env
    pid = await topic(env)
    with pytest.raises(Exception, match="ck_personas_title_source"):
        await owner_exec(env.owner_engine, "UPDATE civ.personas SET title_source = 'model' WHERE id = :p", p=pid)


# ================================================================== the whole thing: a message in a topic


async def test_the_model_renames_the_topic_and_the_user_reads_code_written_words_only(rename_env):
    env = rename_env
    pid = await topic(env)
    model_renames(env, rename_call())
    await env.feed("Давай теперь учить датский всерьёз, по часу каждый вечер", thread_id=THREAD)

    [offer] = env.chat.tool_calls
    assert [t.name for t in offer["tools"]] == ["rename_current_topic"]
    assert topic_rename.instructions(OLD) in offer["system"]  # the current name is in the prompt
    [phrased] = env.chat.calls  # asked AGAIN, without functions, with the note
    assert "Topic rename (made by the system)" in phrased["system"] and SHOWN in phrased["system"]
    assert says(env) == ["Давай про датский.", texts.TOPIC_RENAMED.format(title=SHOWN)]  # the reply, then the system's announcement
    assert all("Переименовал тему!" not in t for t in says(env))  # the model's own claim is never sent
    [edit] = env.session.topic_renames()
    assert (edit.chat_id, edit.message_thread_id, edit.name) == (env.chat_id, THREAD, SHOWN)
    stored = await row(env, pid)
    assert stored["title"] == SHOWN and stored["source"] == "bot" and stored["changed_at"] is not None
    assert f'topic "{SHOWN}"' in stored["prompt"]  # the prompt of the persona moves with the name
    assert [m["content"] for m in await env.messages() if m["role"] == "assistant"] == ["Давай про датский."]  # the announcement is not "memory"


async def test_right_after_a_rename_the_function_is_not_even_offered_and_a_call_anyway_changes_nothing(rename_env):
    env = rename_env
    pid = await topic(env)
    model_renames(env, rename_call())
    await env.feed("Давай учить датский", thread_id=THREAD)
    env.chat.tool_calls.clear()
    env.chat.calls.clear()

    model_renames(env, rename_call("Ещё одно имя"), text="Снова переименовал!", then="Продолжим.")
    await env.feed("И расскажи про глаголы", thread_id=THREAD)

    assert env.chat.tool_calls == [] and offered(env) == []  # inside the cooldown: no function on offer, so a plain reply
    assert len(env.session.topic_renames()) == 1 and (await row(env, pid))["title"] == SHOWN
    assert says(env)[-1] == "Продолжим."  # a plain reply


async def test_a_call_of_a_function_that_was_not_offered_is_ignored_and_its_words_are_not_trusted(rename_env):
    env = rename_env
    await topic(env, ago=2 * HOUR)  # inside the cooldown from the start
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="Переименовал!", calls=(rename_call(),))
    env.chat.responder = lambda system, turns: "Обычный ответ."
    await env.feed("Давай про датский", thread_id=THREAD)
    assert env.chat.tool_calls == []  # never offered, so the plain path
    assert env.session.topic_renames() == []


async def test_once_the_cooldown_has_passed_the_function_is_offered_again(rename_env):
    env = rename_env
    pid = await topic(env, ago=25 * HOUR)
    model_renames(env, rename_call("Фитнес"), then="Окей.")
    await env.feed("Теперь про фитнес", thread_id=THREAD)
    assert (await row(env, pid))["title"] == "📌 Фитнес"


async def test_not_offered_in_the_main_chat_the_settings_topic_or_when_switched_off(rename_env):
    env = rename_env
    await topic(env, "settings", "⚙️ Настройки", 502)
    await env.feed("Привет, как дела")  # the main chat (no persona)
    await env.feed("Что тут настраивать?", thread_id=502)  # Settings
    assert env.chat.tool_calls == []  # no function for either: both are plain replies

    await topic(env)
    env.settings.topic_rename_enabled = False
    await env.feed("Давай про датский", thread_id=THREAD)
    assert env.chat.tool_calls == []


async def test_a_name_the_code_cannot_use_renames_nothing_and_the_models_words_are_dropped(rename_env):
    env = rename_env
    pid = await topic(env)
    model_renames(env, rename_call("x" * 70), text="Готово, переименовал!", then="Обычный ответ.")
    await env.feed("Давай про датский", thread_id=THREAD)
    assert env.session.topic_renames() == [] and says(env) == ["Обычный ответ."]  # phrased again without the function
    assert (await row(env, pid))["changed_at"] is None  # the claim was never taken


async def test_only_one_rename_per_message(rename_env):
    env = rename_env
    await topic(env)
    model_renames(env, rename_call("Первое"), rename_call("Второе"))
    await env.feed("Давай про датский", thread_id=THREAD)
    assert [e.name for e in env.session.topic_renames()] == ["📌 Первое"]


# ---------------------------------------------------------------- Telegram says no: nothing is claimed, nothing is announced


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (TelegramBadRequest, "Bad Request: not enough rights to manage topics"),
        (TelegramBadRequest, "Bad Request: TOPIC_ID_INVALID"),
        (TelegramBadRequest, "Bad Request: something new we do not know"),
        (TelegramNetworkError, "timeout"),
    ],
)
async def test_a_refusal_of_telegram_gives_the_claim_back_and_the_reply_still_goes_out(rename_env, error, message):
    env = rename_env
    pid = await topic(env)
    env.session.fail_on("editForumTopic", error, message=message)
    model_renames(env, rename_call())
    await env.feed("Давай учить датский", thread_id=THREAD)

    assert texts.TOPIC_RENAMED.format(title=SHOWN) not in says(env) and "Давай про датский." in says(env)
    stored = await row(env, pid)
    assert stored["title"] == OLD and stored["changed_at"] is None  # nothing changed, and the topic can be tried again at once
    assert texts.FAILURE not in says(env)  # the user is not bothered with something they did not ask for


async def test_telegram_saying_not_modified_syncs_our_record_without_an_announcement_or_a_cooldown(rename_env):
    env = rename_env
    pid = await topic(env)
    env.session.fail_on("editForumTopic", TelegramBadRequest, message="Bad Request: TOPIC_NOT_MODIFIED")
    model_renames(env, rename_call())
    await env.feed("Давай учить датский", thread_id=THREAD)
    stored = await row(env, pid)
    assert stored["title"] == SHOWN and stored["changed_at"] is None  # the topic already had it; nobody renamed anything
    assert texts.TOPIC_RENAMED.format(title=SHOWN) not in says(env)


async def test_a_database_failure_after_telegram_accepted_keeps_the_claim_and_announces_nothing(rename_env, monkeypatch, caplog):
    env = rename_env
    pid = await topic(env)
    original = personas.set_title

    async def broken(session, persona_id, title, *, source=None):
        if source == "bot":  # the write that records the successful rename
            raise RuntimeError("database went away")
        return await original(session, persona_id, title, source=source)

    monkeypatch.setattr(personas, "set_title", broken)
    model_renames(env, rename_call())
    await env.feed("Давай учить датский", thread_id=THREAD)

    assert len(env.session.topic_renames()) == 1  # Telegram did rename
    assert texts.TOPIC_RENAMED.format(title=SHOWN) not in says(env) and "Давай про датский." in says(env)  # no claim of success we cannot show
    stored = await row(env, pid)
    assert stored["changed_at"] is not None and stored["title"] == OLD  # the claim stays: no hammering Telegram; the cooldown ends, a later rename heals
    assert any("failed after Telegram" in r.message for r in caplog.records)


# ---------------------------------------------------------------- the user's own name wins for longer


def edited(env, name: str | None, **kw: Any):
    return make_topic_edited_update(name=name, chat_id=env.chat_id, thread_id=THREAD, user_id=env.telegram_user_id, **kw)


async def test_a_manual_rename_starts_the_longer_cooldown_so_the_bot_does_not_override_the_users_choice(rename_env):
    env = rename_env
    pid = await topic(env, ago=30 * HOUR)  # the bot's own cooldown has long passed
    await env.feed_update(edited(env, "Моё название"))
    stored = await row(env, pid)
    assert stored["title"] == "Моё название" and stored["source"] == "user" and stored["changed_at"] is not None

    model_renames(env, rename_call())
    await env.feed("Давай учить датский", thread_id=THREAD)
    assert env.chat.tool_calls == [] and env.session.topic_renames() == []  # the user's name is respected (7 days)

    await restamp(env, pid, ago=8 * DAY, source="user")
    await env.feed("Давай серьёзно про датский", thread_id=THREAD)
    assert offered(env) == ["rename_current_topic"]


async def test_the_echo_of_the_bots_own_rename_does_not_pass_for_a_manual_one(rename_env):
    env = rename_env
    pid = await topic(env)
    model_renames(env, rename_call())
    await env.feed("Давай учить датский", thread_id=THREAD)
    await env.feed_update(edited(env, SHOWN, is_bot=True))  # Telegram's service message about OUR edit
    assert (await row(env, pid))["source"] == "bot"


# ---------------------------------------------------------------- next to the other functions


async def test_a_rename_and_a_spoken_plan_in_one_message_both_happen(owner_engine, db):
    from tests.bot_env import build_bot_env

    env = await build_bot_env(owner_engine, db, topic_rename_enabled=True, quick_events_enabled=True)
    try:
        pid = await topic(env)
        start_text, _ = future()
        model_renames(env, rename_call(), create_call("Урок датского", start_text, duration_minutes=60))
        await env.feed("С сегодняшнего дня учу датский, первый урок через пару часов", thread_id=THREAD)

        assert sorted(t.name for t in env.chat.tool_calls[0]["tools"]) == ["create_event", "rename_current_topic"]
        assert (await row(env, pid))["title"] == SHOWN
        texts_sent = says(env)
        assert texts_sent[0] == "Давай про датский." and texts.TOPIC_RENAMED.format(title=SHOWN) in texts_sent
        assert any(m.reply_markup is not None for m in env.session.sent())  # the calendar card
    finally:
        await env.bot.session.close()

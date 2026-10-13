"""Module 5, step 5, part 3: forum topics. ``Orchestrator.rename_topic`` is the ONE safe way for the bot to rename a topic: only a topic the bot
created (a persona with a ``telegram_thread_id``), every refusal of Telegram is a RESULT (nothing raises into the orchestrator), and the
stored name changes only after Telegram accepted it. ``Orchestrator.sync_topic_title`` + the ``forum_topic_edited`` service message keep
``personas.title`` equal to what the user renamed the topic to by hand. Real PostgreSQL, the real Dispatcher on a fake Telegram wire."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from ai_civ.bot import personas
from ai_civ.bot.orchestrator import MAX_TOPIC_NAME_CHARS, RenameResult
from tests.helpers import new_persona, new_user, owner_exec
from tests.tg_fakes import make_topic_edited_update

THREAD = 612


async def topic_persona(env, code: str = "sport", title: str = "🏋️ Спорт", thread_id: int | None = THREAD, **kw: Any) -> uuid.UUID:
    """A persona the way ``_ensure_topic`` leaves it: a bound topic and a prompt that names the topic once (``topic "<title>"``)."""
    return await new_persona(
        env.owner_engine, env.user_id, code, thread_id=thread_id, title=title,
        system_prompt=personas.compose_system_prompt(title, "keep the user training"), **kw,
    )  # fmt: skip


async def stored(env, persona_id: uuid.UUID) -> tuple[str, str]:
    [(title, prompt)] = await owner_exec(env.owner_engine, "SELECT title, system_prompt FROM civ.personas WHERE id = :p", p=persona_id)
    return title, prompt


async def title_of(env, persona_id: uuid.UUID) -> str:
    return (await stored(env, persona_id))[0]


# ================================================================== rename_topic: the happy path and the guards


async def test_a_rename_is_sent_to_telegram_and_then_stored_with_the_prompt_in_step(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    assert await env.orchestrator.rename_topic(env.tenant, pid, "💪 Силовые") == RenameResult.renamed
    [call] = env.session.topic_renames()
    assert (call.chat_id, call.message_thread_id, call.name) == (env.chat_id, THREAD, "💪 Силовые")
    title, prompt = await stored(env, pid)
    assert title == "💪 Силовые" and 'topic "💪 Силовые"' in prompt and "🏋️ Спорт" not in prompt  # one place says the name: it follows
    assert env.session.sent() == []  # a rename is silent in the chat


async def test_the_new_name_is_normalised_like_every_topic_name_of_the_bot(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    assert await env.orchestrator.rename_topic(env.tenant, pid, "  Силовые \n  тренировки  ") == RenameResult.renamed
    assert env.session.topic_renames()[-1].name == "📌 Силовые тренировки"  # one line, and an emoji is added when there is none
    long = "💪 " + "я" * 100
    assert await env.orchestrator.rename_topic(env.tenant, pid, long) == RenameResult.renamed
    assert len(env.session.topic_renames()[-1].name) <= personas.MAX_TITLE_CHARS and (await title_of(env, pid)).startswith("💪 яя")


@pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
async def test_a_title_that_is_nothing_asks_nothing_of_telegram(bot_env, raw):
    env = bot_env
    pid = await topic_persona(env)
    assert await env.orchestrator.rename_topic(env.tenant, pid, raw) == RenameResult.invalid_title
    assert env.session.topic_renames() == [] and await title_of(env, pid) == "🏋️ Спорт"


async def test_the_same_name_is_not_sent_again(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    assert await env.orchestrator.rename_topic(env.tenant, pid, "🏋️ Спорт") == RenameResult.unchanged
    assert env.session.topic_renames() == []


async def test_a_prompt_that_was_edited_by_hand_keeps_its_text(bot_env):
    env = bot_env
    pid = await new_persona(env.owner_engine, env.user_id, "sport", thread_id=THREAD, title="🏋️ Спорт", system_prompt="my own prompt")
    assert await env.orchestrator.rename_topic(env.tenant, pid, "💪 Силовые") == RenameResult.renamed
    assert await stored(env, pid) == ("💪 Силовые", "my own prompt")


async def test_a_quote_in_the_name_cannot_break_the_prompt(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    assert await env.orchestrator.rename_topic(env.tenant, pid, '💪 Мой "зал"') == RenameResult.renamed
    title, prompt = await stored(env, pid)
    assert title == '💪 Мой "зал"' and 'topic "💪 Мой \'зал\'"' in prompt


# ================================================================== rename_topic: only topics the bot made


async def test_only_a_topic_with_a_persona_and_a_bound_thread_can_be_renamed(bot_env):
    env = bot_env
    no_topic = await topic_persona(env, "draft", "📌 Черновик", thread_id=None)  # the persona exists, its topic was never created
    off = await topic_persona(env, "old", "📌 Старое", thread_id=700)
    await owner_exec(env.owner_engine, "UPDATE civ.personas SET is_active = false WHERE id = :p", p=off)
    foreign_user = await new_user(env.owner_engine)
    foreign = await new_persona(env.owner_engine, foreign_user, "sport", thread_id=THREAD, title="Чужое")
    for persona_id in (no_topic, off, foreign, uuid.uuid4()):
        assert await env.orchestrator.rename_topic(env.tenant, persona_id, "💪 Новое") == RenameResult.unknown_topic
    assert env.session.topic_renames() == []
    assert await title_of(env, foreign) == "Чужое"  # another tenant's persona is invisible (RLS), let alone renamable


# ================================================================== rename_topic: Telegram says no - nothing raises, nothing is stored


@pytest.mark.parametrize(
    ("error", "kwargs", "expected"),
    [
        (TelegramForbiddenError, {"message": "Forbidden: bot was kicked from the supergroup chat"}, RenameResult.no_rights),
        (TelegramBadRequest, {"message": "Bad Request: not enough rights to manage topics"}, RenameResult.no_rights),
        (TelegramBadRequest, {"message": "Bad Request: CHAT_ADMIN_REQUIRED, bot must be an administrator"}, RenameResult.no_rights),
        (TelegramBadRequest, {"message": "Bad Request: TOPIC_ID_INVALID"}, RenameResult.gone),
        (TelegramBadRequest, {"message": "Bad Request: message thread not found"}, RenameResult.gone),
        (TelegramBadRequest, {"message": "Bad Request: TOPIC_DELETED"}, RenameResult.gone),
        (TelegramBadRequest, {"message": "Bad Request: something nobody has seen before"}, RenameResult.failed),
        (TelegramNetworkError, {"message": "connection reset"}, RenameResult.failed),
        (TelegramRetryAfter, {"message": "Flood control", "retry_after": 30}, RenameResult.failed),
    ],
)
async def test_a_refusal_of_telegram_is_a_result_and_the_stored_name_does_not_move(bot_env, error, kwargs, expected):
    env = bot_env
    pid = await topic_persona(env)
    env.session.fail_on("editForumTopic", error, **kwargs)
    assert await env.orchestrator.rename_topic(env.tenant, pid, "💪 Силовые") == expected  # never raises
    assert await stored(env, pid) == ("🏋️ Спорт", personas.compose_system_prompt("🏋️ Спорт", "keep the user training"))
    assert env.session.sent() == []  # and the user is not told anything: this is not a message of theirs


@pytest.mark.parametrize("message", ["Bad Request: TOPIC_NOT_MODIFIED", "Bad Request: topic not modified"])
async def test_telegram_saying_not_modified_means_the_topic_already_has_the_name_and_only_our_record_was_behind(bot_env, message):
    env = bot_env
    pid = await topic_persona(env, title="🏋️ Спорт")
    env.session.fail_on("editForumTopic", TelegramBadRequest, message=message)
    assert await env.orchestrator.rename_topic(env.tenant, pid, "💪 Силовые") == RenameResult.unchanged
    title, prompt = await stored(env, pid)
    assert title == "💪 Силовые" and 'topic "💪 Силовые"' in prompt  # the user's rename had not reached us: now it has


async def test_a_database_failure_after_telegram_accepted_is_the_callers_to_see_and_never_a_false_success(bot_env, monkeypatch):
    env = bot_env
    pid = await topic_persona(env)

    async def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(personas, "set_title", boom)
    with pytest.raises(RuntimeError):  # not swallowed: Telegram has the new name, our record does not - that must not look like "renamed"
        await env.orchestrator.rename_topic(env.tenant, pid, "💪 Силовые")
    assert await title_of(env, pid) == "🏋️ Спорт"


# ================================================================== forum_topic_edited: the user renamed a topic by hand


def edited(env, name: str | None, *, thread_id: int | None = THREAD, **kw: Any):
    kw.setdefault("user_id", env.telegram_user_id)
    return make_topic_edited_update(name=name, chat_id=env.chat_id, thread_id=thread_id, **kw)


async def test_a_manual_rename_updates_the_persona_title_and_its_prompt_in_the_users_own_words(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    await env.feed_update(edited(env, "Мои тренировки"))  # no emoji: the user's wording wins
    title, prompt = await stored(env, pid)
    assert title == "Мои тренировки" and 'topic "Мои тренировки"' in prompt
    assert env.session.sent() == [] and await env.messages() == []  # a service message is housekeeping: no reply, nothing remembered


async def test_the_router_sees_the_new_title_of_the_topic(bot_env):
    env = bot_env
    await topic_persona(env)
    await env.feed_update(edited(env, "Мои тренировки"))
    await env.feed("Что-нибудь в основной чат")
    assert "- sport: Мои тренировки" in env.router.calls[-1]["prompt"]


async def test_the_title_is_one_line_and_at_most_the_telegram_limit(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    await env.feed_update(edited(env, "  Мои \n\n тренировки  " + "я" * 200))
    title = await title_of(env, pid)
    assert "\n" not in title and title.startswith("Мои тренировки яя") and len(title) <= MAX_TOPIC_NAME_CHARS


async def test_an_icon_only_edit_the_main_chat_and_an_unknown_topic_change_nothing(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    other = await topic_persona(env, "food", "🍎 Еда", thread_id=700)
    await env.feed_update(edited(env, None))  # the icon changed, the name did not come with it
    await env.feed_update(edited(env, "Основной", thread_id=None))  # the main chat has no persona
    await env.feed_update(edited(env, "Чужая тема", thread_id=999))  # a topic the user made by hand: no persona
    await env.feed_update(edited(env, "   "))  # nothing usable
    assert (await title_of(env, pid), await title_of(env, other)) == ("🏋️ Спорт", "🍎 Еда")
    assert env.session.sent() == []


async def test_the_same_name_again_is_a_no_op(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    assert await env.orchestrator.sync_topic_title(env.tenant, THREAD, "Мои тренировки") is True
    assert await env.orchestrator.sync_topic_title(env.tenant, THREAD, "Мои тренировки") is False
    assert await env.orchestrator.sync_topic_title(env.tenant, THREAD, "🏋️ Спорт") is True  # back to the old name is a change like any other
    assert await title_of(env, pid) == "🏋️ Спорт"


async def test_the_bots_own_edit_and_strangers_are_ignored(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    await env.feed_update(edited(env, "Это сделал бот", is_bot=True))  # the service message of our own editForumTopic call
    stranger = await new_user(env.owner_engine)
    [(tg, _chat)] = [(r[0], r[1]) for r in await owner_exec(env.owner_engine, "SELECT telegram_user_id, forum_chat_id FROM civ.users WHERE id = :u", u=stranger)]
    await env.feed_update(edited(env, "Это сделал чужой", user_id=tg))  # a registered person, but this is not their group
    await env.feed_update(edited(env, "Это сделал аноним", user_id=987654321))  # nobody we know
    assert await title_of(env, pid) == "🏋️ Спорт"


async def test_a_frozen_group_does_not_get_its_titles_synced(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=env.user_id)
    await env.feed_update(edited(env, "Пока заморожено"))
    assert await title_of(env, pid) == "🏋️ Спорт"


async def test_a_database_failure_while_syncing_is_swallowed_and_logged(bot_env, monkeypatch, caplog):
    env = bot_env
    pid = await topic_persona(env)

    async def boom(*args: Any, **kwargs: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(personas, "get_by_thread", boom)
    await env.feed_update(edited(env, "Мои тренировки"))  # the polling loop must not notice
    assert await title_of(env, pid) == "🏋️ Спорт" and env.session.sent() == []
    assert "could not sync the title" in caplog.text


async def test_the_bots_own_rename_and_the_echo_that_follows_agree(bot_env):
    env = bot_env
    pid = await topic_persona(env)
    assert await env.orchestrator.rename_topic(env.tenant, pid, "💪 Силовые") == RenameResult.renamed
    await env.feed_update(edited(env, "💪 Силовые", is_bot=True))  # Telegram posts the service message for our own edit
    assert await title_of(env, pid) == "💪 Силовые"
    assert await env.orchestrator.sync_topic_title(env.tenant, THREAD, "💪 Силовые") is False

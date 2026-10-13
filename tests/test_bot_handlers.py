"""The aiogram layer through the REAL Dispatcher (``feed_update``): tenant resolution for every message, chat binding,
General vs topic detection, commands, and containment of errors (the polling loop must never see an exception)."""

from __future__ import annotations

import logging

from ai_civ.bot import texts
from ai_civ.bot.handlers import thread_of
from tests.bot_env import build_bot_env, route_new
from tests.helpers import new_fact, new_persona, owner_exec
from tests.tg_fakes import make_update


async def test_a_message_from_an_unregistered_user_is_ignored_in_a_group(bot_env, caplog):
    env = bot_env
    with caplog.at_level(logging.INFO):
        await env.feed("hello", user_id=424242)

    assert env.session.sent() == [] and env.chat.calls == [] and env.router.calls == []
    assert await env.messages() == []
    assert any("unregistered telegram user 424242" in r.message for r in caplog.records)


async def test_in_a_private_chat_an_unregistered_user_is_told_how_to_get_started(bot_env):
    env = bot_env
    await env.feed("hello", user_id=424242, chat_id=424242, chat_type="private")
    await env.feed("/start", user_id=424242, chat_id=424242, chat_type="private")

    hints = env.session.sent()  # no administrator and no Telegram id needed: the bot registers whoever adds it to a group
    assert [(h.chat_id, h.text) for h in hints] == [(424242, texts.UNREGISTERED)] * 2
    assert env.chat.calls == []


async def test_bots_and_anonymous_senders_are_ignored(bot_env):
    env = bot_env
    await env.feed("hello", is_bot=True)
    assert env.session.sent() == [] and env.chat.calls == []


async def test_a_registered_user_in_someone_elses_group_is_not_served_there(bot_env, owner_engine, db):
    env = bot_env
    await env.feed("hello", chat_id=-100555)  # right person, wrong chat
    await env.feed("hello", chat_id=env.telegram_user_id, chat_type="private")  # private chat: only a pointer to the forum
    assert [(m.chat_id, m.text) for m in env.session.sent()] == [(env.telegram_user_id, texts.PRIVATE_CHAT)]
    assert env.chat.calls == [] and await env.messages() == []


async def test_a_group_the_bot_cannot_vouch_for_is_not_served_and_is_logged_with_its_id(bot_env, owner_engine, caplog):
    env = bot_env
    await owner_exec(owner_engine, "UPDATE civ.users SET forum_chat_id = NULL WHERE id = :u", u=env.user_id)  # registered, no group yet
    with caplog.at_level(logging.WARNING):
        await env.feed("first message in the new group", chat_id=-1001234567890)  # Telegram knows nothing about this chat

    assert env.session.sent() == [] and env.chat.calls == [] and await env.messages() == []
    assert any("-1001234567890" in r.message and "not its forum group" in r.message for r in caplog.records)


async def test_a_registered_person_without_a_group_is_connected_by_their_first_message_in_a_ready_forum(bot_env, owner_engine):
    env = bot_env
    await owner_exec(owner_engine, "UPDATE civ.users SET forum_chat_id = NULL WHERE id = :u", u=env.user_id)
    env.session.add_group(-1001234567890, env.telegram_user_id)  # they are its creator, Topics are on, the bot is an admin

    await env.feed("first message in the new group", chat_id=-1001234567890)  # no SQL by an administrator any more
    assert [m.text for m in env.session.sent()] == [texts.CONNECTED] and env.chat.calls == []

    await env.feed("now it works", chat_id=-1001234567890)
    assert env.session.sent_to(None)[-1].text == "ok: now it works"  # General of the newly bound group


async def test_tenants_cannot_use_each_others_groups(bot_env, owner_engine, db):
    a = bot_env
    b = await build_bot_env(owner_engine, db)
    await a.feed("hi from B in A's group", user_id=b.telegram_user_id)  # B is registered, but A's group is not B's forum
    assert a.session.sent() == [] and a.chat.calls == [] and await a.messages() == [] and await b.messages() == []
    await b.bot.session.close()


async def test_a_topic_message_goes_through_the_whole_stack(bot_env):
    env = bot_env
    persona = await new_persona(env.owner_engine, env.user_id, "foodguide", thread_id=501, system_prompt="FOOD")

    await env.feed("what to eat?", thread_id=501)

    [reply] = env.session.sent_to(501)
    assert reply.text == "ok: what to eat?" and reply.chat_id == env.chat_id
    assert {m["persona_id"] for m in await env.messages()} == {persona}


async def test_general_is_detected_even_when_telegram_fills_message_thread_id_for_a_reply_chain(bot_env):
    env = bot_env
    # In General, a reply to another message may carry message_thread_id = that message's id, but is_topic_message is not set.
    await env.feed("hi there", thread_id=123456, is_topic_message=False)

    assert env.session.sent_to(123456) == []  # NOT treated as an unknown topic
    assert env.session.sent_to(None)[-1].text == "ok: hi there"  # answered in General
    assert len(env.router.calls) == 1  # and offered to the router, as every General message is


def test_thread_of_follows_is_topic_message():
    topic = make_update(text="x", user_id=1, chat_id=-1, message_id=1, thread_id=7).message
    reply_chain = make_update(text="x", user_id=1, chat_id=-1, message_id=1, thread_id=7, is_topic_message=False).message
    general = make_update(text="x", user_id=1, chat_id=-1, message_id=1).message
    assert (thread_of(topic), thread_of(reply_chain), thread_of(general)) == (7, None, None)


async def test_a_new_activity_written_through_the_dispatcher_opens_a_topic(bot_env):
    env = bot_env
    env.router.responder = lambda prompt: route_new("bowling", "🎳 Боулинг")
    await env.feed("Хочу заняться боулингом")
    [topic] = env.session.topics()
    assert topic.name == "🎳 Боулинг" and len(env.chat.calls) == 1


async def test_setup_command_creates_settings_and_the_topics_planned_from_the_goals(bot_env):
    env = bot_env
    await new_fact(env.owner_engine, env.user_id, "goal.1", category="goal", value="найти работу", priority=70)
    env.router.responder = lambda prompt: {
        "topics": [{"code": "job_search", "title": "💼 Поиск работы", "focus": "Help the user find a job."}]
    }
    await env.feed("/setup")
    assert [t.name for t in env.session.topics()] == ["⚙️ Настройки", "💼 Поиск работы"]
    assert env.chat.calls == []  # a command is not a chat message for the chat model
    assert await env.messages() == []


async def test_other_slash_commands_and_non_text_messages_are_ignored(bot_env):
    env = bot_env
    await env.feed("/nonsense")
    await env.dispatcher.feed_update(
        env.bot, make_update(text=None, user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=env.next_message_id())
    )  # e.g. a photo / a service message: no text
    assert env.session.sent() == [] and env.chat.calls == [] and await env.messages() == []


async def test_a_gemini_outage_through_the_dispatcher_yields_the_notice_not_an_exception(bot_env):
    from ai_civ.memory.llm import LLMError

    env = bot_env
    await new_persona(env.owner_engine, env.user_id, "foodguide", thread_id=501)
    env.chat.responder = lambda system, turns: LLMError("down")

    await env.feed("hi", thread_id=501)  # feed_update would raise if anything escaped

    assert [m.text for m in env.session.sent_to(501)] == [texts.FAILURE]


async def test_anything_that_escapes_a_handler_is_swallowed_and_logged_so_polling_survives(bot_env, caplog):
    env = bot_env

    async def explode(inc):
        raise RuntimeError("bug in a handler")

    env.orchestrator.handle_message = explode  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        await env.feed("hi")  # must not raise

    assert any("unhandled error in update" in r.message for r in caplog.records)
    await env.feed("/setup")  # and the dispatcher keeps working afterwards (no goals: only the Settings topic)
    assert [t.name for t in env.session.topics()] == ["⚙️ Настройки"]

"""The rewritten group provisioning: the userbot makes the system topics, the bot promotes and greets the owner the moment they are in, ``/start`` always makes a fresh
group (also for a registered person whose group is broken, whose tenant is then MOVED: ``civ.relocate_group``). Real PostgreSQL, the real Dispatcher, a fake Telegram wire."""

from __future__ import annotations

import asyncio
import json
import random
import time

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import LeaveChat

from ai_civ.bot import error_handler, texts
from ai_civ.bot.provision import ChatBinding, GroupProvisioner, ProvisionStore, group_health, provision
from tests.helpers import new_persona, new_user, owner_exec, pg_error
from tests.tg_fakes import make_bot, make_member_update, make_service_update
from tests.test_userbot import (  # noqa: F401  (fixtures and helpers of the userbot tests)
    GHOST,
    OWNER,
    USERBOT_ID,
    FakeApi,
    MemRedis,
    bind,
    made_group,
    prov_env,
    provisioner,
    settings,
    start_message,
    world,
)


def person() -> int:
    return random.randrange(10**9, 10**12)


def a_group() -> int:
    return -int(f"100{random.randrange(10**9, 10**10)}")


async def owner_joins(env, chat: int) -> None:
    """How a join by the invite link reaches the bot: the service message and the chat_member update."""
    await env.feed_update(make_service_update(chat_id=chat, joined=[env.owner_tg]))
    await env.feed_update(make_member_update(chat_id=chat, user_id=env.owner_tg, new_status="member", old_status="left"))


async def personas_of(env, tenant_id) -> dict[str, int | None]:
    rows = await owner_exec(env.owner_engine, "SELECT code, telegram_thread_id FROM civ.personas WHERE user_id = :u", u=tenant_id)
    return dict(rows)


# ================================================================== the userbot's part: the group, the bot, the link - and NO topics


async def test_the_userbot_makes_no_topics_and_leaves_the_binding_without_thread_ids(world):
    w = world
    await provision(w.api, w.store, w.settings, owner_id=OWNER, display_name="Аня", username="anya")

    assert w.api.names() == ["create", "add_bot", "add_user", "make_admin", "leave"]
    binding = await w.store.chat(w.api.chat_id)
    assert (binding.owner, binding.settings_thread, binding.feedback_thread) == (OWNER, None, None)


async def test_the_welcome_mark_belongs_to_the_first_caller_only(world):
    w = world
    assert await w.store.welcome_once(-1001) is True and await w.store.welcome_once(-1001) is False
    await w.store.welcome_undo(-1001)
    assert await w.store.welcome_once(-1001) is True


# ================================================================== the bot meets the owner


async def test_the_owner_who_joins_by_the_link_is_made_an_admin_and_greeted_in_the_main_chat_once(prov_env):
    env = prov_env
    chat, group = made_group(env, owner_in=False)  # the userbot is gone, the owner has not opened the link yet
    await bind(env, chat)
    group.members.add(env.owner_tg)

    await owner_joins(env, chat)  # reported twice, handled once

    [promotion] = env.session.promotions()
    assert (promotion.chat_id, promotion.user_id) == (chat, env.owner_tg)
    assert promotion.can_promote_members and promotion.can_manage_topics and promotion.can_invite_users and promotion.can_change_info and not promotion.is_anonymous
    greetings = [m for m in env.session.sent() if m.text == texts.GROUP_READY]
    assert len(greetings) == 1 and greetings[0].chat_id == chat and greetings[0].message_thread_id is None  # the main chat (General)
    assert "Ты здесь админ" in greetings[0].text and "⚙️ Настройки" in greetings[0].text and "💡 Идеи и отзывы" in greetings[0].text


async def test_an_owner_who_is_already_an_admin_is_greeted_without_a_second_promotion(prov_env):
    env = prov_env
    chat, group = made_group(env, owner_in=False)
    await bind(env, chat)
    group.members.add(env.owner_tg)
    group.admins.add(env.owner_tg)  # the userbot added them and made them an administrator

    await owner_joins(env, chat)

    assert env.session.promotions() == [] and [m.text for m in env.session.sent()].count(texts.GROUP_READY) == 1


async def test_the_bot_makes_the_topics_between_the_promotion_and_the_greeting_and_keeps_their_ids(prov_env):
    env = prov_env
    chat, group = made_group(env, owner_in=False)
    await bind(env, chat)  # the userbot made no topics
    group.members.add(env.owner_tg)

    await owner_joins(env, chat)

    order = [type(c).__name__ for c in env.session.calls if type(c).__name__ in ("PromoteChatMember", "CreateForumTopic", "SendMessage")]
    assert order[:4] == ["PromoteChatMember", "CreateForumTopic", "CreateForumTopic", "SendMessage"]  # the chain: admin, topics, greeting
    assert [t.name for t in env.session.topics()] == ["⚙️ Настройки", "💡 Идеи и отзывы"]
    binding = await env.store.chat(chat)
    assert binding.settings_thread is not None and binding.feedback_thread is not None and binding.settings_thread != binding.feedback_thread
    row = await env.user_row(env.owner_tg)  # and the registration that follows adopts them instead of making them again
    assert len(env.session.topics()) == 2
    assert await personas_of(env, row["id"]) == {"settings": binding.settings_thread, "feedback": binding.feedback_thread}
    assert [m.text for m in env.session.sent_to(binding.feedback_thread)] == [texts.FEEDBACK_GREETING]


async def test_when_telegram_refuses_the_promotion_the_warning_is_sent_the_topics_are_made_and_the_developer_gets_the_log(prov_env):
    env = prov_env
    env.settings.error_reports_enabled = True
    chat, group = made_group(env, owner_in=False, promote_error="Bad Request: not enough rights")
    await bind(env, chat)
    group.members.add(env.owner_tg)

    await owner_joins(env, chat)
    await asyncio.gather(*error_handler._background)

    sent = [m.text for m in env.session.sent() if m.chat_id == chat]
    assert texts.GROUP_NO_ADMIN in sent and texts.GROUP_READY not in sent
    assert texts.GROUP_NO_ADMIN == (
        "⚠️ Я не смог выдать тебе права админа из-за ограничений Telegram. Пожалуйста, назначь себя админом вручную, а я пока создал нужные ветки."
    )
    assert len(env.session.topics()) == 2  # "а я пока создал нужные ветки": the chain went on
    [crash] = env.session.documents()
    assert crash.chat_id == env.settings.feedback_admin_telegram_id and "registration:promote_owner" in crash.caption and "not enough rights" in crash.caption
    assert (await env.user_row(env.owner_tg))["forum_chat_id"] == chat  # the group is registered all the same


async def test_a_refused_topic_goes_to_the_developer_but_the_greeting_still_comes_and_the_registration_makes_the_feedback_topic(prov_env):
    env = prov_env
    env.settings.error_reports_enabled = True
    chat, group = made_group(env, owner_in=False)
    await bind(env, chat)
    group.members.add(env.owner_tg)
    env.session.fail_on("createForumTopic", TelegramBadRequest, times=2, message="Bad Request: not enough rights to create a topic")

    await owner_joins(env, chat)
    await asyncio.gather(*error_handler._background)

    assert texts.GROUP_READY in [m.text for m in env.session.sent() if m.chat_id == chat]
    assert {d.caption.split("\n")[0] for d in env.session.documents()} == {"🔥 registration:create_topic"}
    row = await env.user_row(env.owner_tg)
    assert set(await personas_of(env, row["id"])) == {"feedback"}  # made at registration (Settings follows the interview)


async def test_an_anonymous_admin_owner_is_a_refusal_like_any_other(prov_env):
    env = prov_env
    chat, group = made_group(env, owner_in=False, promote_error="Bad Request: user is an anonymous admin")
    await bind(env, chat)
    group.members.add(env.owner_tg)
    await owner_joins(env, chat)
    assert texts.GROUP_NO_ADMIN in [m.text for m in env.session.sent() if m.chat_id == chat]


async def test_the_topics_already_in_the_binding_are_not_made_again(prov_env):
    env = prov_env
    chat, group = made_group(env, owner_in=False)
    await env.store.bind_chat(chat, ChatBinding(env.owner_tg, USERBOT_ID, settings_thread=11, feedback_thread=12))
    group.members.add(env.owner_tg)

    await owner_joins(env, chat)

    row = await env.user_row(env.owner_tg)
    assert env.session.topics() == [] and await personas_of(env, row["id"]) == {"settings": 11, "feedback": 12}
    assert [m.text for m in env.session.sent_to(11)] == [texts.SETTINGS_GREETING] and env.session.sent_to(11)[0].reply_markup is not None  # the Mini App button


async def test_the_owners_first_message_in_the_group_also_triggers_the_meeting_if_the_join_events_never_came(prov_env):
    env = prov_env
    chat, group = made_group(env, owner_in=True)  # no join event is fed at all
    await bind(env, chat)

    await env.say("привет", user_id=env.owner_tg, chat_id=chat)

    assert texts.GROUP_READY in [m.text for m in env.session.sent() if m.chat_id == chat] and len(env.session.topics()) == 2


# ================================================================== /start always starts afresh


async def test_a_registered_person_with_a_healthy_group_is_not_moved(prov_env):
    env = prov_env
    old = a_group()
    env.session.add_group(old, env.owner_tg)  # the owner is the creator: everything works
    await new_user(env.owner_engine, telegram_user_id=env.owner_tg, forum_chat_id=old)
    queued: list[tuple] = []
    env.dispatcher["provisioner"] = GroupProvisioner(env.store, settings(), lambda *a: queued.append(a))

    await env.say("/start", user_id=env.owner_tg, chat_id=env.owner_tg, chat_type="private")

    assert queued == [] and env.session.sent()[-1].text == texts.PRIVATE_CHAT


@pytest.mark.parametrize("flaw", ["no_group", "owner_plain_member", "not_forum", "bot_cannot_manage_topics", "bot_removed"])
async def test_a_registered_person_with_a_broken_group_gets_a_new_one_on_start(prov_env, flaw):
    env = prov_env
    old = a_group()
    if flaw != "no_group":
        kw = {"is_forum": flaw != "not_forum", "bot_can_manage_topics": flaw != "bot_cannot_manage_topics", "bot_status": "left" if flaw == "bot_removed" else "administrator"}
        group = env.session.add_group(old, GHOST, **kw)
        group.members.add(env.owner_tg)  # a plain member: nobody made them an administrator
    await new_user(env.owner_engine, telegram_user_id=env.owner_tg, forum_chat_id=old)
    queued: list[tuple] = []
    env.dispatcher["provisioner"] = GroupProvisioner(env.store, settings(), lambda *a: queued.append(a))

    await env.say("/start", user_id=env.owner_tg, chat_id=env.owner_tg, chat_type="private")

    assert [q[0] for q in queued] == [env.owner_tg] and env.session.sent()[-1].text == texts.PROVISION_REBUILD


async def test_a_dormant_group_needs_no_question_to_telegram(prov_env):
    env = prov_env
    old = a_group()
    tenant = await new_user(env.owner_engine, telegram_user_id=env.owner_tg, forum_chat_id=old)
    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'dormant' WHERE id = :u", u=tenant)
    queued: list[tuple] = []
    env.dispatcher["provisioner"] = GroupProvisioner(env.store, settings(), lambda *a: queued.append(a))
    calls_before = len(env.session.calls)

    await env.say("/start", user_id=env.owner_tg, chat_id=env.owner_tg, chat_type="private")

    assert len(queued) == 1 and not [c for c in env.session.calls[calls_before:] if type(c).__name__ in ("GetChat", "GetChatMember")]


async def test_when_telegram_cannot_tell_nobody_is_moved_on_a_guess(prov_env):
    env = prov_env
    old = a_group()
    env.session.add_group(old, GHOST).members.add(env.owner_tg)  # would be "broken" ...
    env.session.fail_on("getChat", TelegramNetworkError)  # ... but Telegram does not answer
    await new_user(env.owner_engine, telegram_user_id=env.owner_tg, forum_chat_id=old)
    queued: list[tuple] = []
    env.dispatcher["provisioner"] = GroupProvisioner(env.store, settings(), lambda *a: queued.append(a))

    await env.say("/start", user_id=env.owner_tg, chat_id=env.owner_tg, chat_type="private")

    assert queued == [] and env.session.sent()[-1].text == texts.PRIVATE_CHAT


async def test_the_group_health_verdicts():
    bot, session = make_bot()
    chat = a_group()
    session.add_group(chat, 1).members.add(OWNER)
    assert await group_health(bot, chat, OWNER) == "broken"  # a plain member
    session.groups[chat].admins.add(OWNER)
    assert await group_health(bot, chat, OWNER) == "ok"
    assert await group_health(bot, chat, 999) == "broken"  # not even a member of the group
    assert await group_health(bot, a_group(), OWNER) == "broken"  # no such chat
    session.fail_on("getChat", TelegramNetworkError, times=1)
    assert await group_health(bot, chat, OWNER) == "unknown"


async def test_start_never_waits_for_an_old_link_and_a_failed_mark_lasts_seconds_not_minutes(world):
    w = world
    p = provisioner(w)
    await w.store.fail_user(OWNER)
    assert w.redis.ttl["t:prov:u:" + str(OWNER)] <= 60  # a second /start right away: the manual way; later: a fresh attempt
    w.redis.data.pop("t:prov:u:" + str(OWNER))
    assert await p.on_start(start_message()) is True and len(p.queued) == 1


async def test_the_pending_state_remembers_when_it_began(world):
    w = world
    await w.store.claim_user(OWNER)
    state = await w.store.user(OWNER)
    assert state.since is not None and abs(state.since - time.time()) < 5 and state.working(time.time())
    assert not state.working(time.time() + 3600)
    assert json.loads(w.redis.data["t:prov:u:" + str(OWNER)])["state"] == "pending"


# ================================================================== moving a registered person into the new group


async def broken_tenant(env, *, state: str = "done") -> tuple:
    old = a_group()  # not a group of the fake Telegram: "chat not found"
    tenant = await new_user(env.owner_engine, telegram_user_id=env.owner_tg, forum_chat_id=old, onboarding_state=state)
    return tenant, old


async def test_a_person_with_a_broken_group_who_joins_the_new_one_is_moved_with_everything_they_have(prov_env):
    env = prov_env
    tenant, old = await broken_tenant(env)
    await new_persona(env.owner_engine, tenant, "settings", thread_id=5)
    await new_persona(env.owner_engine, tenant, "foodguide", thread_id=6)
    await owner_exec(env.owner_engine, "INSERT INTO civ.messages (user_id, role, content, telegram_message_id) VALUES (:u, 'user', 'old', 1)", u=tenant)
    chat, group = made_group(env, owner_in=False)
    await env.store.bind_chat(chat, ChatBinding(env.owner_tg, USERBOT_ID, settings_thread=11, feedback_thread=12))
    group.members.add(env.owner_tg)

    await owner_joins(env, chat)

    row = await env.user_row(env.owner_tg)
    assert row["id"] == tenant and row["forum_chat_id"] == chat and row["group_state"] == "active"  # the same tenant, the new group
    assert await personas_of(env, tenant) == {"settings": 11, "foodguide": None, "feedback": 12}  # the old topics are gone, the userbot's are adopted
    assert (await owner_exec(env.owner_engine, "SELECT telegram_message_id FROM civ.messages WHERE user_id = :u", u=tenant)) == [(None,)]
    assert any(isinstance(c, LeaveChat) and c.chat_id == old for c in env.session.calls)  # the bot leaves the abandoned group
    texts_in_new_chat = [m.text for m in env.session.sent() if m.chat_id == chat]
    assert texts.GROUP_READY in texts_in_new_chat and texts.GROUP_MOVED in texts_in_new_chat


async def test_an_old_link_opened_by_a_person_whose_group_works_moves_nobody(prov_env):
    env = prov_env
    old = a_group()
    env.session.add_group(old, env.owner_tg)  # healthy
    tenant = await new_user(env.owner_engine, telegram_user_id=env.owner_tg, forum_chat_id=old)
    chat, group = made_group(env, owner_in=False)
    await bind(env, chat)
    group.members.add(env.owner_tg)

    await owner_joins(env, chat)

    assert (await env.user_row(env.owner_tg))["forum_chat_id"] == old and tenant is not None


async def test_a_tenant_whose_interview_never_finished_gets_the_first_question_in_the_new_group(prov_env):
    env = prov_env
    tenant, old = await broken_tenant(env, state="pending")
    chat, group = made_group(env, owner_in=False)
    await bind(env, chat)
    group.members.add(env.owner_tg)

    await owner_joins(env, chat)

    assert (await env.user_row(env.owner_tg))["forum_chat_id"] == chat
    assert (await env.user_row(env.owner_tg))["onboarding_state"] == "in_progress" and texts.GROUP_MOVED not in [m.text for m in env.session.sent()]


# ================================================================== civ.relocate_group (real PostgreSQL)


async def test_relocate_moves_the_tenant_resets_what_belonged_to_the_old_group_and_keeps_the_rest(db, owner_engine):
    tg, old, new = person(), a_group(), a_group()
    tenant = await new_user(owner_engine, telegram_user_id=tg, forum_chat_id=old)
    persona = await new_persona(owner_engine, tenant, "foodguide", thread_id=6)
    await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=tenant)
    await owner_exec(owner_engine, "INSERT INTO civ.messages (user_id, persona_id, role, content, telegram_message_id) VALUES (:u, :p, 'user', 'kept', 7)", u=tenant, p=persona)
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.time_capsules (user_id, message, deliver_at, source_message_id) VALUES (:u, 'x', now() + interval '30 days', 9)",
        u=tenant,
    )

    moved = await db.relocate_group(telegram_user_id=tg, forum_chat_id=new)

    assert (moved.outcome, moved.tenant_id, moved.old_chat_id) == ("relocated", tenant, old)
    assert (await owner_exec(owner_engine, "SELECT forum_chat_id, group_state FROM civ.users WHERE id = :u", u=tenant)) == [(new, "active")]
    assert (await owner_exec(owner_engine, "SELECT telegram_thread_id, is_active FROM civ.personas WHERE id = :p", p=persona)) == [(None, True)]
    assert (await owner_exec(owner_engine, "SELECT content, telegram_message_id FROM civ.messages WHERE user_id = :u", u=tenant)) == [("kept", None)]
    assert (await owner_exec(owner_engine, "SELECT source_message_id FROM civ.time_capsules WHERE user_id = :u", u=tenant)) == [(None,)]
    assert await db.resolve_tenant_id(forum_chat_id=new) == tenant and await db.resolve_tenant_id(forum_chat_id=old) is None


async def test_relocate_outcomes_and_idempotence(db, owner_engine):
    tg, old, new = person(), a_group(), a_group()
    assert (await db.relocate_group(telegram_user_id=tg, forum_chat_id=new)).outcome == "no_tenant"  # nobody to move

    tenant = await new_user(owner_engine, telegram_user_id=tg, forum_chat_id=old)
    assert (await db.relocate_group(telegram_user_id=tg, forum_chat_id=old)).outcome == "already_here"

    other = a_group()
    await new_user(owner_engine, telegram_user_id=person(), forum_chat_id=other)
    taken = await db.relocate_group(telegram_user_id=tg, forum_chat_id=other)
    assert (taken.outcome, taken.tenant_id) == ("chat_taken", tenant)  # somebody else's group is never taken over
    assert (await owner_exec(owner_engine, "SELECT forum_chat_id FROM civ.users WHERE id = :u", u=tenant)) == [(old,)]

    assert (await db.relocate_group(telegram_user_id=tg, forum_chat_id=new)).outcome == "relocated"
    assert (await db.relocate_group(telegram_user_id=tg, forum_chat_id=new)).outcome == "already_here"  # a repeated event changes nothing


async def test_relocate_refuses_rubbish_ids_and_is_for_the_app_role_only(db, owner_engine):
    with pg_error("22023"):
        await db.relocate_group(telegram_user_id=0, forum_chat_id=a_group())
    with pg_error("22023"):
        await db.relocate_group(telegram_user_id=person(), forum_chat_id=-5)  # not a supergroup id
    rows = await owner_exec(
        owner_engine,
        "SELECT has_function_privilege('public', 'civ.relocate_group(bigint, bigint)', 'EXECUTE'), "
        "has_function_privilege('ai_civ_app', 'civ.relocate_group(bigint, bigint)', 'EXECUTE')",
    )
    assert rows == [(False, True)]


async def test_two_moves_at_once_end_with_one_group(db, owner_engine):
    import asyncio

    tg, old = person(), a_group()
    tenant = await new_user(owner_engine, telegram_user_id=tg, forum_chat_id=old)
    chats = [a_group() for _ in range(4)]
    results = await asyncio.gather(*(db.relocate_group(telegram_user_id=tg, forum_chat_id=c) for c in chats))

    assert [r.outcome for r in results] == ["relocated"] * 4  # serialised by the advisory lock: each one moves in turn, nothing fails or deadlocks
    final = (await owner_exec(owner_engine, "SELECT forum_chat_id FROM civ.users WHERE id = :u", u=tenant))[0][0]
    assert final in chats  # one definite group at the end

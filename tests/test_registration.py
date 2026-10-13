"""Module 5, step 1: Zero-SQL registration. The DB function on a real PostgreSQL (outcomes, races), the Registrar through the REAL
aiogram Dispatcher with a fake Telegram wire (checks against Telegram, the first interview question, refusals) and the privacy
guard (a stranger in the group freezes the bot; a removed bot makes the tenant dormant)."""

from __future__ import annotations

import asyncio
import random
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Chat, Message, Update, User

from ai_civ.bot import onboarding, schedule, texts
from ai_civ.bot.orchestrator import load_tenant
from ai_civ.bot.proactive import TRIGGERS
from ai_civ.bot.registration import ANONYMOUS_ADMIN_BOT_ID, GroupCheck, Throttle
from ai_civ.bot.review import ReviewService
from ai_civ.db.enums import GroupState, RegistrationOutcome
from tests.helpers import new_user, owner_exec, pg_error, tenant_exec
from tests.tg_fakes import (
    BOT_ID,
    make_bot_membership_update,
    make_callback_update,
    make_member_update,
    make_service_update,
)


def person() -> int:
    return random.randrange(10**9, 10**12)


def group() -> int:
    return -int(f"100{random.randrange(10**9, 10**10)}")  # a supergroup id: -100 + 10 digits


def texts_sent(env, chat_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if chat_id is None or m.chat_id == chat_id]


async def add_bot(env, chat_id: int, actor_id: int, **update_kw) -> None:
    await env.feed_update(make_bot_membership_update(chat_id=chat_id, actor_id=actor_id, **update_kw))


# ================================================================== civ.register_tenant (real PostgreSQL)


async def test_a_new_person_becomes_a_pending_active_tenant(db, owner_engine):
    tg, chat = person(), group()
    registration = await db.register_tenant(telegram_user_id=tg, forum_chat_id=chat)

    assert registration.outcome is RegistrationOutcome.created and registration.tenant_id is not None
    rows = await owner_exec(
        owner_engine,
        "SELECT id, forum_chat_id, onboarding_state, group_state, timezone FROM civ.users WHERE telegram_user_id = :t",
        t=tg,
    )
    assert rows == [(registration.tenant_id, chat, "pending", "active", "UTC")]
    # the application role can now resolve and use the tenant, under RLS
    assert await db.resolve_tenant_id(telegram_user_id=tg) == registration.tenant_id
    assert await db.resolve_tenant_id(forum_chat_id=chat) == registration.tenant_id
    assert await tenant_exec(db, registration.tenant_id, "SELECT count(*) FROM civ.users") == [(1,)]


async def test_registering_the_same_group_again_changes_nothing(db, owner_engine):
    tg, chat = person(), group()
    first = await db.register_tenant(telegram_user_id=tg, forum_chat_id=chat)
    again = await db.register_tenant(telegram_user_id=tg, forum_chat_id=chat)
    assert again.outcome is RegistrationOutcome.already_registered and again.tenant_id == first.tenant_id
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.users WHERE telegram_user_id = :t", t=tg) == [(1,)]


async def test_a_person_registered_without_a_group_gets_the_group_attached(db, owner_engine):
    tg, chat = person(), group()
    user = await new_user(owner_engine, telegram_user_id=tg)
    await owner_exec(owner_engine, "UPDATE civ.users SET forum_chat_id = NULL WHERE id = :u", u=user)

    registration = await db.register_tenant(telegram_user_id=tg, forum_chat_id=chat)

    assert (registration.outcome, registration.tenant_id) == (RegistrationOutcome.attached_chat, user)
    assert await owner_exec(owner_engine, "SELECT forum_chat_id FROM civ.users WHERE id = :u", u=user) == [(chat,)]


async def test_one_group_per_person_and_one_person_per_group(db, owner_engine):
    tg, chat, other_chat = person(), group(), group()
    owner = await db.register_tenant(telegram_user_id=tg, forum_chat_id=chat)

    second_group = await db.register_tenant(telegram_user_id=tg, forum_chat_id=other_chat)
    assert (second_group.outcome, second_group.tenant_id) == (RegistrationOutcome.has_other_group, owner.tenant_id)

    stranger = await db.register_tenant(telegram_user_id=person(), forum_chat_id=chat)
    assert (stranger.outcome, stranger.tenant_id) == (RegistrationOutcome.chat_taken, None)

    # a person who exists without a group cannot take a group that belongs to somebody else either
    homeless = await new_user(owner_engine, telegram_user_id=person())
    await owner_exec(owner_engine, "UPDATE civ.users SET forum_chat_id = NULL WHERE id = :u", u=homeless)
    tg_homeless = (await owner_exec(owner_engine, "SELECT telegram_user_id FROM civ.users WHERE id = :u", u=homeless))[0][0]
    taken = await db.register_tenant(telegram_user_id=tg_homeless, forum_chat_id=chat)
    assert (taken.outcome, taken.tenant_id) == (RegistrationOutcome.chat_taken, homeless)
    assert await owner_exec(owner_engine, "SELECT forum_chat_id FROM civ.users WHERE id = :u", u=homeless) == [(None,)]
    assert await owner_exec(owner_engine, "SELECT forum_chat_id FROM civ.users WHERE id = :u", u=owner.tenant_id) == [(chat,)]


async def test_a_dormant_tenant_comes_back_to_the_same_group_only(db, owner_engine):
    tg, chat = person(), group()
    owner = await db.register_tenant(telegram_user_id=tg, forum_chat_id=chat)
    await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'dormant' WHERE id = :u", u=owner.tenant_id)

    elsewhere = await db.register_tenant(telegram_user_id=tg, forum_chat_id=group())
    assert elsewhere.outcome is RegistrationOutcome.other_group_dormant  # moving to a new group is not supported (yet)

    back = await db.register_tenant(telegram_user_id=tg, forum_chat_id=chat)
    assert (back.outcome, back.tenant_id) == (RegistrationOutcome.reactivated, owner.tenant_id)
    assert await owner_exec(owner_engine, "SELECT group_state FROM civ.users WHERE id = :u", u=owner.tenant_id) == [("active",)]


async def test_a_frozen_tenant_is_not_unfrozen_by_registering_again(db, owner_engine):
    tg, chat = person(), group()
    owner = await db.register_tenant(telegram_user_id=tg, forum_chat_id=chat)
    await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=owner.tenant_id)
    again = await db.register_tenant(telegram_user_id=tg, forum_chat_id=chat)
    assert again.outcome is RegistrationOutcome.already_registered
    assert await owner_exec(owner_engine, "SELECT group_state FROM civ.users WHERE id = :u", u=owner.tenant_id) == [("frozen",)]


@pytest.mark.parametrize(
    ("tg", "chat"),
    [(0, -1001234567890), (-5, -1001234567890), (None, -1001234567890), (5, None), (5, -123456), (5, 1001234567890), (5, -1000000000000)],
)
async def test_garbage_ids_are_rejected(db, tg, chat):
    with pg_error("22023"):
        await db.register_tenant(telegram_user_id=tg, forum_chat_id=chat)


async def test_the_function_is_not_callable_by_public_but_by_the_application_role(owner_engine):
    rows = await owner_exec(
        owner_engine,
        "SELECT has_function_privilege('public', 'civ.register_tenant(bigint, bigint)', 'EXECUTE'), "
        "has_function_privilege('ai_civ_app', 'civ.register_tenant(bigint, bigint)', 'EXECUTE'), "
        "(SELECT prosecdef FROM pg_proc WHERE proname = 'register_tenant')",
    )
    assert rows == [(False, True, True)]


async def test_group_state_accepts_only_the_three_states(owner_engine):
    user = await new_user(owner_engine)
    with pg_error("23514"):
        await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'asleep' WHERE id = :u", u=user)


async def test_overlapping_registrations_of_the_same_person_and_group_create_one_tenant(db, owner_engine):
    tg, chat = person(), group()
    results = await asyncio.gather(*[db.register_tenant(telegram_user_id=tg, forum_chat_id=chat) for _ in range(8)])
    assert Counter(r.outcome for r in results) == {RegistrationOutcome.created: 1, RegistrationOutcome.already_registered: 7}
    assert len({r.tenant_id for r in results}) == 1
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.users WHERE telegram_user_id = :t", t=tg) == [(1,)]


async def test_overlapping_registrations_of_one_group_by_different_people_give_it_to_one(db, owner_engine):
    chat = group()
    results = await asyncio.gather(*[db.register_tenant(telegram_user_id=person(), forum_chat_id=chat) for _ in range(6)])
    assert Counter(r.outcome for r in results) == {RegistrationOutcome.created: 1, RegistrationOutcome.chat_taken: 5}
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.users WHERE forum_chat_id = :c", c=chat) == [(1,)]


async def test_overlapping_registrations_of_one_person_in_different_groups_give_them_one(db, owner_engine):
    tg = person()
    results = await asyncio.gather(*[db.register_tenant(telegram_user_id=tg, forum_chat_id=group()) for _ in range(6)])
    assert Counter(r.outcome for r in results) == {RegistrationOutcome.created: 1, RegistrationOutcome.has_other_group: 5}
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.users WHERE telegram_user_id = :t", t=tg) == [(1,)]


async def test_crossing_registrations_neither_deadlock_nor_break_the_unique_keys(db, owner_engine):
    p1, p2, c1, c2 = person(), person(), group(), group()
    calls = [(p1, c1), (p2, c2), (p1, c2), (p2, c1)] * 3
    random.shuffle(calls)
    results = await asyncio.wait_for(
        asyncio.gather(*[db.register_tenant(telegram_user_id=p, forum_chat_id=c) for p, c in calls]), timeout=30
    )
    assert len(results) == len(calls)
    rows = await owner_exec(owner_engine, "SELECT telegram_user_id, forum_chat_id FROM civ.users WHERE telegram_user_id IN (:a, :b)", a=p1, b=p2)
    assert 1 <= len(rows) <= 2 and len({r[0] for r in rows}) == len(rows) and len({r[1] for r in rows}) == len(rows)


# ================================================================== pure pieces


def test_throttle_remembers_a_key_for_its_ttl_and_stays_bounded():
    now = [100.0]
    throttle = Throttle(30, max_entries=3, clock=lambda: now[0])
    assert throttle.allow("a") and not throttle.allow("a") and throttle.allow("b")
    now[0] += 29
    assert not throttle.allow("a")
    now[0] += 2  # 31 s after the first call
    assert throttle.allow("a")  # expired
    for key in ("c", "d", "e", "f"):
        assert throttle.allow(key)
        assert len(throttle) <= 3  # never grows past its bound: the oldest key is forgotten


def test_the_checklist_names_everything_that_is_missing_and_nothing_else():
    ready = GroupCheck(True, True, True, True, True, 2)
    assert ready.problems() == []
    bad = GroupCheck(is_supergroup=False, is_forum=False, is_private=False, actor_is_creator=True, bot_can_manage_topics=False, member_count=5)
    assert bad.problems() == [texts.PROBLEM_NOT_FORUM, texts.PROBLEM_NOT_ADMIN, texts.PROBLEM_NOT_PRIVATE, texts.PROBLEM_NOT_ALONE]
    assert GroupCheck(True, False, True, True, True, 2).problems() == [texts.PROBLEM_NOT_FORUM]  # a supergroup without Topics
    assert GroupCheck(True, True, True, True, False, 2).problems() == [texts.PROBLEM_NOT_ADMIN]  # an admin without the topics right
    text = texts.setup_checklist([texts.PROBLEM_NOT_FORUM, texts.PROBLEM_NOT_ADMIN])
    assert text == f"{texts.SETUP_HEADER}\n• {texts.PROBLEM_NOT_FORUM}\n• {texts.PROBLEM_NOT_ADMIN}"


def test_registration_texts_are_plain_short_and_do_not_name_the_main_topic():
    banned = ("Повторите позже", "Пожалуйста", "осуществ", "уважаемы", "данн")
    names = ("UNREGISTERED", "SETUP_HEADER", "PROBLEM_NOT_FORUM", "PROBLEM_NOT_ADMIN", "PROBLEM_NOT_PRIVATE", "PROBLEM_NOT_ALONE",
             "NOT_CREATOR", "CONNECTED", "RETURNED", "HAS_OTHER_GROUP", "OTHER_GROUP_GONE", "CHAT_TAKEN", "FROZEN", "UNFROZEN")  # fmt: skip
    for name in names:
        value = getattr(texts, name)
        assert not any(b.lower() in value.lower() for b in banned), name
        assert len(value) <= 200, name  # also the limit of a callback-query alert (FROZEN is shown as one)
        assert "General" not in value and "Чатик" not in value, name
    assert "Темы" in texts.UNREGISTERED and "администратор" in texts.UNREGISTERED and "{" not in texts.UNREGISTERED


# ================================================================== the Registrar through the real Dispatcher


async def test_start_in_a_private_chat_tells_a_stranger_how_to_begin(bare_env):
    env, tg = bare_env, person()
    await env.say("/start", user_id=tg, chat_id=tg, chat_type="private")
    assert [(m.chat_id, m.text) for m in env.session.sent()] == [(tg, texts.UNREGISTERED)]
    assert await env.count_users() == 0


async def test_adding_the_bot_to_a_ready_forum_registers_the_creator_and_opens_the_interview(bare_env):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg)

    await add_bot(env, chat, tg)

    row = await env.user_row(tg)
    assert row is not None and row["forum_chat_id"] == chat and row["group_state"] == "active"
    assert row["onboarding_state"] == "in_progress"  # the first question was asked and saved
    question, greeting = env.session.sent()
    assert (question.chat_id, question.message_thread_id) == (chat, None)  # the main chat of the new group
    assert question.text == f"{onboarding.INTRO}\n\n{onboarding.question_text(1)}"
    [feedback] = env.session.topics()  # the standard channel "Ideas and feedback" is made right away, with its welcome
    assert feedback.name == "💡 Идеи и отзывы" and greeting.text == texts.FEEDBACK_GREETING and greeting.message_thread_id is not None
    assert env.chat.calls == [] and env.router.calls == []  # no model was needed for the welcome


async def test_the_new_tenant_is_interviewed_where_it_writes_and_nobody_else_is_served(bare_env):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg)
    await add_bot(env, chat, tg)
    before = len(env.session.sent())

    await env.say("Меня зовут Анна", user_id=tg, chat_id=chat)  # the owner: this is the interview
    assert len(env.router.calls) == 1 and len(env.session.sent()) == before + 1

    await env.say("hello", user_id=person(), chat_id=chat)  # a stranger in the group: not served, nothing sent
    assert len(env.session.sent()) == before + 1 and len(env.router.calls) == 1


async def test_the_steps_may_come_in_any_order_and_each_missing_one_is_named(bare_env):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg, bot_status="member", is_forum=False)  # added first, no Topics, not an admin yet

    await add_bot(env, chat, tg, new_status="member")
    [hint] = env.session.sent()
    assert hint.text == texts.setup_checklist([texts.PROBLEM_NOT_FORUM, texts.PROBLEM_NOT_ADMIN]) and await env.count_users() == 0

    env.session.groups[chat].is_forum = True  # Topics on (no update for that)
    env.session.groups[chat].bot_status = "administrator"
    env.session.groups[chat].bot_can_manage_topics = False
    await add_bot(env, chat, tg, old_status="member")
    assert env.session.sent()[-1].text == texts.setup_checklist([texts.PROBLEM_NOT_ADMIN]) and await env.count_users() == 0

    env.session.groups[chat].bot_can_manage_topics = True
    await add_bot(env, chat, tg, old_status="administrator")
    assert (await env.user_row(tg))["onboarding_state"] == "in_progress"


async def test_a_basic_group_a_public_group_and_a_crowded_group_are_not_registered(bare_env):
    env, tg = bare_env, person()
    basic, public, crowded = group(), group(), group()
    env.session.add_group(basic, tg, type="group", is_forum=False)
    env.session.add_group(public, tg, username="my_public_group")
    env.session.add_group(crowded, tg, members={person(), person()})

    await add_bot(env, basic, tg, chat_type="group")
    await add_bot(env, public, tg)
    await add_bot(env, crowded, tg)

    assert [m.text for m in env.session.sent()] == [
        texts.setup_checklist([texts.PROBLEM_NOT_FORUM]),
        texts.setup_checklist([texts.PROBLEM_NOT_PRIVATE]),
        texts.setup_checklist([texts.PROBLEM_NOT_ALONE]),
    ]
    assert await env.count_users() == 0


async def test_only_the_creator_can_register_a_group_whoever_added_the_bot(bare_env):
    env, creator, helper, chat = bare_env, person(), person(), group()
    env.session.add_group(chat, creator, members={helper})
    await add_bot(env, chat, helper)  # an admin who is not the creator added the bot
    assert texts_sent(env) == [texts.NOT_CREATOR] and await env.count_users() == 0

    env.session.groups[chat].members.discard(helper)
    await add_bot(env, chat, creator)  # the creator: registered (the helper's attempt did not squat the group)
    assert (await env.user_row(creator)) is not None and await env.user_row(helper) is None


async def test_an_anonymous_admin_or_a_bot_cannot_register_anything(bare_env):
    env, chat = bare_env, group()
    env.session.add_group(chat, ANONYMOUS_ADMIN_BOT_ID)
    await add_bot(env, chat, ANONYMOUS_ADMIN_BOT_ID)
    assert env.session.calls == [] and await env.count_users() == 0  # not even a question to Telegram: the person is unknown

    from_the_group_itself = Message(
        message_id=1, date=datetime.now(UTC), chat=Chat(id=chat, type="supergroup", is_forum=True), text="hi",
        from_user=User(id=person(), is_bot=False, first_name="x"), sender_chat=Chat(id=chat, type="supergroup"),
    )  # fmt: skip
    await env.feed_update(Update(update_id=10**6, message=from_the_group_itself))  # no author either
    assert env.session.calls == [] and await env.count_users() == 0


async def test_repeated_and_overlapping_events_register_once_and_ask_the_first_question_once(bare_env):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg)

    await asyncio.gather(*[add_bot(env, chat, tg) for _ in range(5)])
    await add_bot(env, chat, tg)

    assert await env.count_users() == 1
    assert len(env.session.sent()) == 2  # the interview is opened once (+ the welcome of the feedback channel), whatever the number of events
    assert len(env.session.topics()) == 1  # and the feedback topic is made once


async def test_a_second_group_is_refused_with_an_explanation_and_the_first_keeps_working(bare_env):
    env, tg, first, second = bare_env, person(), group(), group()
    env.session.add_group(first, tg)
    env.session.add_group(second, tg)
    await add_bot(env, first, tg)
    sent_before = len(env.session.sent())

    await add_bot(env, second, tg)

    assert texts_sent(env, second) == [texts.HAS_OTHER_GROUP]
    assert len(env.session.sent()) == sent_before + 1  # nothing else: no second interview
    row = await env.user_row(tg)
    assert row["forum_chat_id"] == first and await env.count_users() == 1


async def test_a_registered_person_writing_in_another_ready_group_is_told_why_it_does_not_work(bare_env):
    env, tg, first, second = bare_env, person(), group(), group()
    env.session.add_group(first, tg)
    env.session.add_group(second, tg)
    await add_bot(env, first, tg)

    await env.say("hello?", user_id=tg, chat_id=second)

    assert texts_sent(env, second) == [texts.HAS_OTHER_GROUP]


async def test_a_second_group_of_a_person_whose_first_group_is_gone_gets_the_honest_answer(bare_env):
    env, tg, first, second = bare_env, person(), group(), group()
    env.session.add_group(first, tg)
    env.session.add_group(second, tg)
    await add_bot(env, first, tg)
    await add_bot(env, first, tg, new_status="left", old_status="administrator")  # removed from the first group
    assert (await env.user_row(tg))["group_state"] == "dormant"

    await add_bot(env, second, tg)

    assert texts_sent(env, second) == [texts.OTHER_GROUP_GONE]


async def test_a_group_that_belongs_to_somebody_else_cannot_be_taken(bare_env):
    env, old_owner, new_creator, chat = bare_env, person(), person(), group()
    env.session.add_group(chat, old_owner)
    await add_bot(env, chat, old_owner)
    await add_bot(env, chat, old_owner, new_status="left", old_status="administrator")  # dormant, group id stays reserved

    env.session.add_group(chat, new_creator)  # the group changed hands in Telegram
    await add_bot(env, chat, new_creator)

    assert texts_sent(env, chat)[-1] == texts.CHAT_TAKEN
    assert await env.user_row(new_creator) is None and (await env.user_row(old_owner))["forum_chat_id"] == chat


async def test_enabling_topics_later_needs_no_event_the_next_message_of_the_creator_registers(bare_env):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg, is_forum=False)
    await add_bot(env, chat, tg)  # not a forum yet: the checklist
    assert texts_sent(env) == [texts.setup_checklist([texts.PROBLEM_NOT_FORUM])]

    env.session.groups[chat].is_forum = True
    await env.say("ну вот, включил", user_id=tg, chat_id=chat)

    assert (await env.user_row(tg))["forum_chat_id"] == chat


async def test_plain_messages_do_not_hammer_telegram_nor_spam_the_group(bare_env):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg, is_forum=False)

    for _ in range(4):
        await env.say("пишу в группу", user_id=tg, chat_id=chat)

    inspections = [c for c in env.session.calls if c.__api_method__ == "getChat"]
    assert len(inspections) == 1  # re-checked at most once per retry window
    assert texts_sent(env) == [texts.setup_checklist([texts.PROBLEM_NOT_FORUM])]  # and told once


async def test_a_plain_chatter_who_is_not_the_creator_gets_no_reply_and_blocks_nobody(bare_env):
    env, creator, chatter, chat = bare_env, person(), person(), group()
    env.session.add_group(chat, creator, members={chatter}, is_forum=False)

    await env.say("привет всем", user_id=chatter, chat_id=chat)
    assert env.session.sent() == [] and await env.count_users() == 0

    env.session.groups[chat].is_forum = True
    env.session.groups[chat].members.discard(chatter)
    await env.say("я создатель", user_id=creator, chat_id=chat)  # the chatter's attempt did not use up the creator's
    assert await env.user_row(creator) is not None


async def test_telegram_trouble_registers_nobody_and_nothing_escapes(bare_env, caplog):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg)
    env.session.fail_on("getChatMemberCount", TelegramBadRequest)

    await add_bot(env, chat, tg)  # must not raise

    assert await env.count_users() == 0 and env.session.sent() == []
    assert any("could not handle a membership change" in r.message for r in caplog.records)


async def test_a_person_registered_by_sql_without_a_group_is_connected_on_the_first_message_in_a_ready_group(bare_env):
    env, tg, chat = bare_env, person(), group()
    user = await new_user(env.owner_engine, telegram_user_id=tg)
    await owner_exec(env.owner_engine, "UPDATE civ.users SET forum_chat_id = NULL WHERE id = :u", u=user)
    env.session.add_group(chat, tg)

    await env.say("первое сообщение", user_id=tg, chat_id=chat)

    assert (await env.user_row(tg))["forum_chat_id"] == chat
    assert texts_sent(env, chat) == [texts.CONNECTED]  # onboarding is already done: no interview, just the confirmation


# ================================================================== the privacy guard (frozen / dormant)


async def freeze_via_join(env, stranger: int | None = None) -> int:
    stranger = stranger or person()
    env.session.groups[env.chat_id].members.add(stranger)
    await env.feed_update(make_service_update(chat_id=env.chat_id, joined=[stranger]))
    return stranger


async def state_of(env) -> str:
    return (await owner_exec(env.owner_engine, "SELECT group_state FROM civ.users WHERE id = :u", u=env.user_id))[0][0]


async def test_a_stranger_joining_freezes_the_group_and_the_warning_goes_out_once(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)

    stranger = await freeze_via_join(env)
    await env.feed_update(  # the same join, reported a second time as chat_member
        make_member_update(chat_id=env.chat_id, user_id=stranger, new_status="member", old_status="left")
    )

    assert await state_of(env) == "frozen"
    assert texts_sent(env, env.chat_id) == [texts.FROZEN]


async def test_a_silent_join_is_caught_by_the_chat_member_update(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)
    env.session.groups[env.chat_id].members.add(person())
    await env.feed_update(make_member_update(chat_id=env.chat_id, user_id=person(), new_status="member", old_status="left"))
    assert await state_of(env) == "frozen"


async def test_the_owner_and_the_bot_joining_do_not_freeze_anything(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)
    await env.feed_update(make_service_update(chat_id=env.chat_id, joined=[env.telegram_user_id, BOT_ID]))
    await env.feed_update(make_member_update(chat_id=env.chat_id, user_id=env.telegram_user_id, new_status="member", old_status="left"))
    assert await state_of(env) == "active" and env.session.sent() == []


async def test_joins_in_a_group_nobody_registered_are_not_our_business(bot_env):
    env = bot_env
    await env.feed_update(make_service_update(chat_id=group(), joined=[person()]))
    assert await state_of(env) == "active" and env.session.sent() == []


async def test_while_frozen_the_bot_answers_nothing_and_remembers_nothing(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)
    await freeze_via_join(env)
    env.session.calls.clear()

    await env.feed("расскажи, что ты обо мне знаешь")

    assert texts_sent(env, env.chat_id) == [texts.FROZEN]  # a reason, not an answer
    assert env.chat.calls == [] and env.router.calls == [] and await env.messages() == []


async def test_a_button_of_a_frozen_group_answers_with_the_reason(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)
    await freeze_via_join(env)

    await env.dispatcher.feed_update(
        env.bot, make_callback_update(data="rv:00000000-0000-0000-0000-000000000000:k", user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=5)
    )

    [answer] = env.session.callback_answers()
    assert answer.text == texts.FROZEN and answer.show_alert is True


async def test_when_the_stranger_leaves_the_next_message_unfreezes_and_is_served(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)
    stranger = await freeze_via_join(env)

    env.session.groups[env.chat_id].members.discard(stranger)  # gone, and Telegram sent no update we could see
    await env.feed("я тут")

    assert await state_of(env) == "active"
    assert texts_sent(env, env.chat_id) == [texts.FROZEN, texts.UNFROZEN, "ok: я тут"]


async def test_the_departure_itself_unfreezes_without_waiting_for_the_owner(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)
    stranger = await freeze_via_join(env)

    env.session.groups[env.chat_id].members.discard(stranger)
    await env.feed_update(make_service_update(chat_id=env.chat_id, left=stranger))

    assert await state_of(env) == "active" and texts_sent(env, env.chat_id) == [texts.FROZEN, texts.UNFROZEN]


async def test_one_of_two_strangers_leaving_keeps_the_group_frozen(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)
    first = await freeze_via_join(env)
    await freeze_via_join(env)

    env.session.groups[env.chat_id].members.discard(first)
    await env.feed_update(make_member_update(chat_id=env.chat_id, user_id=first, new_status="left", old_status="member"))
    await env.feed("ещё тут?")

    assert await state_of(env) == "frozen" and env.chat.calls == []


async def test_if_telegram_cannot_confirm_the_group_is_private_it_stays_frozen(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)
    await freeze_via_join(env)
    env.session.groups[env.chat_id].members.clear()
    env.session.groups[env.chat_id].members.add(env.telegram_user_id)
    env.session.fail_on("getChatMemberCount", TelegramBadRequest)  # the head count cannot be obtained

    await env.feed("есть кто?")

    assert await state_of(env) == "frozen" and env.chat.calls == []
    assert texts_sent(env, env.chat_id)[-1] == texts.FROZEN


@pytest.mark.parametrize("state", ["frozen", "dormant"])
async def test_nothing_is_sent_to_a_group_that_is_not_active(bot_env, state):
    env = bot_env
    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = :s WHERE id = :u", s=state, u=env.user_id)
    tenant = await load_tenant(env.db, env.user_id)
    assert tenant is not None and tenant.group_state is GroupState(state)

    assert await env.orchestrator.run_proactive(tenant, TRIGGERS["meal_check"]) is False  # no nudge
    assert await ReviewService(env.db, env.bot, env.settings).send_weekly(tenant) == 0  # no weekly review
    assert env.session.sent() == [] and env.chat.calls == []

    # and the Beat dispatchers do not even claim anything for it
    await owner_exec(
        env.owner_engine, "INSERT INTO civ.proactive_schedule (user_id, trigger_key, local_time) VALUES (:u, 'meal_check', '13:00')", u=env.user_id
    )
    monday_13_local = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)  # 13:00 in Europe/Copenhagen (UTC+2)
    due = await schedule.claim_due(env.db, env.user_id, now=monday_13_local, grace=timedelta(minutes=30))
    assert due == [] and not await schedule.review_due(env.db, env.user_id, now=monday_13_local, weekday=0, hour=9)

    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'active' WHERE id = :u", u=env.user_id)  # control
    due = await schedule.claim_due(env.db, env.user_id, now=monday_13_local, grace=timedelta(minutes=30))
    assert [d.trigger_key for d in due] == ["meal_check"]
    assert await schedule.review_due(env.db, env.user_id, now=monday_13_local, weekday=0, hour=9)


async def test_removing_the_bot_makes_the_tenant_dormant_and_adding_it_back_reactivates(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)

    await env.feed_update(make_bot_membership_update(chat_id=env.chat_id, actor_id=env.telegram_user_id, new_status="left", old_status="administrator"))
    assert await state_of(env) == "dormant"

    await env.feed_update(make_bot_membership_update(chat_id=env.chat_id, actor_id=env.telegram_user_id))
    assert await state_of(env) == "active" and texts_sent(env, env.chat_id) == [texts.RETURNED]

    await env.feed("снова здесь")
    assert texts_sent(env, env.chat_id)[-1] == "ok: снова здесь"


async def test_a_dormant_tenant_who_writes_in_the_group_brings_the_bot_back_if_it_is_ready(bot_env):
    env = bot_env
    env.session.add_group(env.chat_id, env.telegram_user_id)
    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'dormant' WHERE id = :u", u=env.user_id)

    await env.feed("ты тут?")  # the bot got the update, so it IS in the group again: this message is not answered, it re-registers
    assert await state_of(env) == "active" and env.chat.calls == []

    await env.feed("теперь тут?")
    assert texts_sent(env, env.chat_id)[-1] == "ok: теперь тут?"


async def test_removing_the_bot_from_a_group_nobody_registered_is_a_no_op(bare_env):
    env = bare_env
    await add_bot(env, group(), person(), new_status="left", old_status="administrator")
    assert env.session.calls == [] and await env.count_users() == 0

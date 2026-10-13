"""Module 5, step 2 through the REAL aiogram Dispatcher (fake Telegram wire): ``/connect``, the deep link ``/start connect_<token>``,
the Accept / Decline buttons, double clicks, and the notification worker + Celery hooks that tell the inviter."""

from __future__ import annotations

import asyncio
import random
import re
import uuid
from datetime import timedelta

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter

from ai_civ import tasks
from ai_civ.bot import invites, notifications, social, texts
from ai_civ.bot.invites import clean_display_name, token_from_payload
from ai_civ.bot.notifications import Notifier
from ai_civ.celery_app import app
from ai_civ.runtime import Runtime
from tests.helpers import new_fact, new_persona, new_user, owner_exec
from tests.tg_fakes import make_callback_update
from tests.test_handshake_db import accept, make_invite

LINK_RE = re.compile(r"https://t\.me/PerfectPerson_bot\?start=(connect_[A-Za-z0-9_-]{22})")


def person_id() -> int:
    return random.randrange(10**9, 10**12)


async def named(env, user_id: uuid.UUID, name: str) -> None:
    """The way the person asked to be addressed, as the onboarding interview stores it."""
    await new_fact(env.owner_engine, user_id, "name", category="identity", value=name)
    await new_fact(env.owner_engine, user_id, "address_as", category="preference", value=name)


async def register_person(env, name: str, *, onboarding_state: str = "done") -> tuple[int, uuid.UUID]:
    tg = person_id()
    user = await new_user(env.owner_engine, telegram_user_id=tg, onboarding_state=onboarding_state)
    if name:
        await named(env, user, name)
    return tg, user


async def dm(env, tg: int, text: str) -> None:
    await env.feed(text, user_id=tg, chat_id=tg, chat_type="private")


async def press(env, tg: int, data: str, *, message_id: int = 9000) -> None:
    await env.dispatcher.feed_update(
        env.bot, make_callback_update(data=data, user_id=tg, chat_id=tg, message_id=message_id, chat_type="private")
    )


async def link_of(env, *, text: str = "/connect") -> str:
    """A writes ``/connect`` in their group; returns the payload (``connect_<token>``) of the link the bot answered with."""
    await env.feed(text)
    match = LINK_RE.search(env.session.sent_to(None)[-1].text)
    assert match, env.session.sent_to(None)[-1].text
    return match.group(1)


def keyboard_of(message) -> list[str]:
    return [button.callback_data for row in message.reply_markup.inline_keyboard for button in row]


async def contacts_between(env, a: uuid.UUID, b: uuid.UUID):
    return await owner_exec(
        env.owner_engine,
        "SELECT user_id, alias, status::text FROM civ.contacts WHERE (user_id = :a AND contact_user_id = :b) OR (user_id = :b AND contact_user_id = :a)",
        a=a,
        b=b,
    )


async def notification_rows(env, user_id: uuid.UUID):
    return await owner_exec(
        env.owner_engine,
        "SELECT id, status::text, attempts, locked_until > now() FROM civ.notification_outbox WHERE user_id = :u ORDER BY id",
        u=user_id,
    )


async def connect(env, b_tg: int, b_user: uuid.UUID) -> uuid.UUID:
    """The whole happy path up to the pressed button; returns the invite id."""
    payload = await link_of(env)
    await dm(env, b_tg, f"/start {payload}")
    [prompt] = [m for m in env.session.sent() if m.chat_id == b_tg][-1:]
    accept_data = keyboard_of(prompt)[0]
    await press(env, b_tg, accept_data)
    return uuid.UUID(accept_data.split(":")[1])


# ================================================================== pure pieces


def test_tokens_are_long_random_and_only_their_hash_is_kept():
    tokens = {invites.new_token() for _ in range(200)}
    assert len(tokens) == 200 and all(re.fullmatch(r"[A-Za-z0-9_-]{22}", t) for t in tokens)  # 128 random bits, url-safe
    token = next(iter(tokens))
    assert invites.hash_token(token) == __import__("hashlib").sha256(token.encode()).digest() and len(invites.hash_token(token)) == 32
    link = invites.deep_link("PerfectPerson_bot", token)
    assert link == f"https://t.me/PerfectPerson_bot?start=connect_{token}"
    assert len(f"connect_{token}") <= 64  # the Telegram limit of a start payload


@pytest.mark.parametrize(
    "payload", [None, "", "connect_", "connect_short", "connect_" + "a" * 23, "connect_" + "a" * 21 + "!", "join_" + "a" * 22, "CONNECT_" + "a" * 22]
)
def test_only_a_well_formed_connect_payload_yields_a_token(payload):
    assert token_from_payload(payload) is None


def test_a_well_formed_payload_yields_its_token():
    token = invites.new_token()
    assert token_from_payload(f"connect_{token}") == token


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Алиса   Смит ", "Алиса Смит"),
        ("Al\nice\tSmith", "Al ice Smith"),  # one line
        ("Eve‮(evil)", "Eve (evil)"),  # a bidi override cannot reverse what follows
        ("​Bob‏", "Bob"),  # zero-width / direction marks are not part of a name
        ("x" * 80, "x" * 40),
        ("", None),
        (None, None),
        ("‮ \x07", None),
    ],
)
def test_a_name_shown_to_another_person_is_one_clean_line(raw, expected):
    assert clean_display_name(raw) == expected


def test_the_button_data_fits_the_limit_and_round_trips():
    invite_id = uuid.uuid4()
    data = [b.callback_data for row in social.decision_keyboard(invite_id).inline_keyboard for b in row]
    assert all(len(d.encode()) <= 64 for d in data)
    parsed = [social.CALLBACK_RE.match(d) for d in data]
    assert [m.group(2) for m in parsed if m] == ["y", "n"] and {m.group(1) for m in parsed if m} == {str(invite_id)}


def test_the_new_texts_are_plain_and_the_celery_wiring_is_in_place():
    banned = ("Повторите позже", "Пожалуйста", "осуществ", "уважаемы", "данн")
    for name in ("CONNECT_NOT_READY", "CONNECT_TOO_MANY", "CONNECT_NOT_READY_DM", "CONNECT_ASK", "CONNECT_DONE", "CONNECT_DECLINED",
                 "CONNECT_ALREADY", "CONNECT_INVALID", "CONNECT_OWN", "NOTIFY_CONTACT_ACCEPTED", "CONNECT_LINK"):  # fmt: skip
        value = getattr(texts, name)
        assert not any(b.lower() in value.lower() for b in banned), name
        assert "General" not in value and "Чатик" not in value, name
    assert len(texts.CONNECT_DECLINE) <= 20 and len(texts.CONNECT_ACCEPT) <= 20  # button captions stay short
    assert {"ai_civ.send_notifications", "ai_civ.dispatch_notifications", "ai_civ.purge_social"} <= set(app.tasks)
    assert not getattr(tasks.send_notifications, "autoretry_for", ())  # the outbox row is the retry state, not Celery
    scheduled = {e["task"]: e["schedule"] for e in app.conf.beat_schedule.values()}
    assert scheduled["ai_civ.dispatch_notifications"] == 60.0 and "ai_civ.purge_social" in scheduled


# ================================================================== /connect in the group


async def test_connect_gives_a_one_time_link_and_stores_only_its_hash(bot_env):
    env = bot_env
    await named(env, env.user_id, "Алиса")

    payload = await link_of(env)

    message = env.session.sent_to(None)[-1]
    assert "Алиса" in message.text and str(env.settings.invite_ttl_hours) in message.text  # A sees exactly what will be shown
    token = payload.removeprefix("connect_")
    rows = await owner_exec(env.owner_engine, "SELECT display_name, token_hash, t::text FROM civ.contact_invites t WHERE user_id = :u", u=env.user_id)
    assert [(r[0], bytes(r[1])) for r in rows] == [("Алиса", invites.hash_token(token))] and token not in rows[0][2]


async def test_connect_answers_where_it_was_asked_and_refuses_when_there_are_too_many_open_links(bot_env):
    env = bot_env
    await named(env, env.user_id, "Алиса")
    await new_persona(env.owner_engine, env.user_id, "settings", thread_id=777)
    await env.feed("/connect", thread_id=777)
    assert LINK_RE.search(env.session.sent_to(777)[-1].text)

    for _ in range(4):
        await env.feed("/connect")
    await env.feed("/connect")  # the sixth
    assert env.session.sent_to(None)[-1].text == texts.CONNECT_TOO_MANY
    assert await owner_exec(env.owner_engine, "SELECT count(*) FROM civ.contact_invites WHERE user_id = :u", u=env.user_id) == [(5,)]


async def test_connect_is_not_available_before_onboarding_or_in_a_frozen_group(onboarding_env, bot_env):
    pending = onboarding_env
    await pending.feed("/connect")
    assert pending.session.sent()[-1].text == texts.CONNECT_NOT_READY  # the database decides: no link was made
    assert await owner_exec(pending.owner_engine, "SELECT count(*) FROM civ.contact_invites WHERE user_id = :u", u=pending.user_id) == [(0,)]

    env = bot_env
    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=env.user_id)
    env.session.add_group(env.chat_id, env.telegram_user_id, members={person_id()})  # a stranger is in: it stays frozen
    await env.feed("/connect")
    assert env.session.sent()[-1].text == texts.FROZEN
    assert await owner_exec(env.owner_engine, "SELECT count(*) FROM civ.contact_invites WHERE user_id = :u", u=env.user_id) == [(0,)]


async def test_a_failure_while_creating_the_link_is_visible_and_publishes_nothing(bot_env, monkeypatch):
    env = bot_env

    async def broken(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(social.invites, "create_invite", broken)
    await env.feed("/connect")
    assert env.session.sent()[-1].text == texts.FAILURE
    assert await owner_exec(env.owner_engine, "SELECT count(*) FROM civ.contact_invites WHERE user_id = :u", u=env.user_id) == [(0,)]


# ================================================================== the deep link and the buttons


async def test_opening_the_link_asks_before_anything_is_written(bot_env):
    env = bot_env
    await named(env, env.user_id, "Алиса")
    b_tg, b_user = await register_person(env, "Борис")
    payload = await link_of(env)

    await dm(env, b_tg, f"/start {payload}")

    [prompt] = [m for m in env.session.sent() if m.chat_id == b_tg]
    assert prompt.text == texts.CONNECT_ASK.format(inviter="Алиса", me="Борис")  # both names, BEFORE the decision
    yes, no = keyboard_of(prompt)
    assert yes.endswith(":y") and no.endswith(":n") and yes.split(":")[1] == no.split(":")[1]
    assert await contacts_between(env, env.user_id, b_user) == [] and await notification_rows(env, env.user_id) == []
    assert env.chat.calls == [] and env.router.calls == []  # no model is involved in a handshake


async def test_accepting_connects_the_two_and_replaces_the_question_with_the_result(bot_env):
    env = bot_env
    await named(env, env.user_id, "Алиса")
    b_tg, b_user = await register_person(env, "Борис")

    await connect(env, b_tg, b_user)

    assert sorted((r[1], r[2]) for r in await contacts_between(env, env.user_id, b_user)) == [("Алиса", "accepted"), ("Борис", "accepted")]
    [edit] = env.session.edits()
    assert edit.text == texts.CONNECT_DONE.format(inviter="Алиса") and edit.chat_id == b_tg and edit.reply_markup is None  # buttons are gone
    assert [a.text for a in env.session.callback_answers()] == [None]  # the spinner stopped
    [(_, status, attempts, _)] = await notification_rows(env, env.user_id)
    assert (status, attempts) == ("pending", 0)  # A will be told by the worker, not by this handler
    assert not [m for m in env.session.sent() if m.chat_id == env.chat_id and "теперь в ваших связях" in m.text]


async def test_a_double_click_on_accept_is_harmless(bot_env):
    env = bot_env
    await named(env, env.user_id, "Алиса")
    b_tg, b_user = await register_person(env, "Борис")
    invite_id = await connect(env, b_tg, b_user)

    await press(env, b_tg, f"cn:{invite_id}:y")  # the second click, a moment later
    await asyncio.gather(*[press(env, b_tg, f"cn:{invite_id}:y") for _ in range(4)])  # and four at once

    assert len(await contacts_between(env, env.user_id, b_user)) == 2 and len(await notification_rows(env, env.user_id)) == 1
    assert env.session.edits()[-1].text == texts.CONNECT_ALREADY.format(inviter="Алиса")


async def test_telegram_refusing_to_edit_an_unchanged_message_does_not_undo_anything(bot_env):
    env = bot_env
    b_tg, b_user = await register_person(env, "Борис")
    payload = await link_of(env)
    await dm(env, b_tg, f"/start {payload}")
    data = keyboard_of(env.session.sent()[-1])[0]
    env.session.fail_on("editMessageText", TelegramBadRequest, message="Bad Request: message is not modified")

    await press(env, b_tg, data)  # must not raise

    assert len(await contacts_between(env, env.user_id, b_user)) == 2


async def test_declining_writes_nothing_and_the_link_stays_usable(bot_env):
    env = bot_env
    b_tg, b_user = await register_person(env, "Борис")
    payload = await link_of(env)
    await dm(env, b_tg, f"/start {payload}")
    no = keyboard_of(env.session.sent()[-1])[1]

    await press(env, b_tg, no)

    assert env.session.edits()[-1].text == texts.CONNECT_DECLINED
    assert await contacts_between(env, env.user_id, b_user) == [] and await notification_rows(env, env.user_id) == []  # A is not told who said no
    await dm(env, b_tg, f"/start {payload}")  # changed their mind
    assert env.session.sent()[-1].text.startswith(texts.CONNECT_ASK.split("{")[0]) or "хочет установить" in env.session.sent()[-1].text


async def test_a_failed_decision_changes_nothing_and_says_so(bot_env, monkeypatch):
    env = bot_env
    b_tg, b_user = await register_person(env, "Борис")
    payload = await link_of(env)
    await dm(env, b_tg, f"/start {payload}")
    yes = keyboard_of(env.session.sent()[-1])[0]

    async def broken(*args, **kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(social.invites, "accept_invite", broken)
    await press(env, b_tg, yes)

    [answer] = env.session.callback_answers()
    assert answer.text == texts.FAILURE and answer.show_alert is True
    assert await contacts_between(env, env.user_id, b_user) == [] and env.session.edits() == []  # the buttons stay: press again
    monkeypatch.undo()
    await press(env, b_tg, yes)
    assert len(await contacts_between(env, env.user_id, b_user)) == 2


async def test_what_a_wrong_person_or_a_dead_link_gets(bot_env):
    env = bot_env
    payload = await link_of(env)

    stranger = person_id()  # not registered: how to get started, and to come back to the link
    await dm(env, stranger, f"/start {payload}")
    assert env.session.sent()[-1].text == texts.CONNECT_NEED_ACCOUNT and env.session.sent()[-1].chat_id == stranger
    await press(env, stranger, f"cn:{uuid.uuid4()}:y")  # a button pressed by somebody who is not registered: refused too
    assert env.session.callback_answers()[-1].text == texts.CONNECT_NEED_ACCOUNT

    newcomer_tg, _ = await register_person(env, "", onboarding_state="in_progress")
    await dm(env, newcomer_tg, f"/start {payload}")
    assert env.session.sent()[-1].text == texts.CONNECT_NOT_READY_DM

    await dm(env, env.telegram_user_id, f"/start {payload}")  # A opens their own link in a private chat
    assert env.session.sent()[-1].text == texts.CONNECT_OWN

    b_tg, b_user = await register_person(env, "Борис")
    for dead in ("connect_garbage", "connect_" + invites.new_token()):  # mangled / never existed
        await dm(env, b_tg, f"/start {dead}")
        assert env.session.sent()[-1].text == texts.CONNECT_INVALID
    assert await contacts_between(env, env.user_id, b_user) == []

    await press(env, b_tg, f"cn:{uuid.uuid4()}:y")  # a button with an id that does not exist
    assert env.session.edits()[-1].text == texts.CONNECT_INVALID


async def test_a_link_that_was_used_is_dead_for_the_next_person(bot_env):
    env = bot_env
    b_tg, b_user = await register_person(env, "Борис")
    c_tg, c_user = await register_person(env, "Вера")
    payload = await link_of(env)
    await dm(env, b_tg, f"/start {payload}")
    await press(env, b_tg, keyboard_of(env.session.sent()[-1])[0])

    await dm(env, c_tg, f"/start {payload}")

    assert env.session.sent()[-1].text == texts.CONNECT_INVALID and await contacts_between(env, env.user_id, c_user) == []


async def test_people_who_are_connected_are_told_so_by_a_second_link(bot_env):
    env = bot_env
    b_tg, b_user = await register_person(env, "Борис")
    await connect(env, b_tg, b_user)

    await dm(env, b_tg, f"/start {await link_of(env)}")

    assert env.session.sent()[-1].text == texts.CONNECT_ALREADY.format(inviter=(await named_alias(env)))


async def named_alias(env) -> str:
    rows = await owner_exec(env.owner_engine, "SELECT display_name FROM civ.contact_invites WHERE user_id = :u ORDER BY created_at DESC LIMIT 1", u=env.user_id)
    return rows[0][0]


async def test_ordinary_start_and_deep_links_outside_a_private_chat_are_not_the_handshake(bot_env):
    env = bot_env
    payload = await link_of(env)
    before = len(env.session.sent())

    await dm(env, env.telegram_user_id, "/start")  # no payload: the old behaviour (a registered person lives in the group)
    assert env.session.sent()[-1].text == texts.PRIVATE_CHAT

    await env.feed(f"/start {payload}", user_id=env.telegram_user_id)  # in a group: not a deep link, never a handshake
    assert len(env.session.sent()) == before + 1
    assert await owner_exec(env.owner_engine, "SELECT count(*) FROM civ.contacts WHERE user_id = :u", u=env.user_id) == [(0,)]


# ================================================================== telling the inviter: the notification worker


async def connected_pair(env, name: str = "Борис"):
    """A (the environment's tenant) and B are connected through the database function; A has one pending notification."""
    b = await new_user(env.owner_engine)
    await named(env, b, name)
    _, created = await make_invite(env.db, env.user_id, "Алиса")
    await accept(env.db, b, created.invite_id, name)
    return b


def notifier(env) -> Notifier:
    return Notifier(env.db, env.bot, env.settings)


async def test_the_inviter_is_told_in_the_settings_topic_with_a_button_into_the_app(bot_env):
    env = bot_env
    await new_persona(env.owner_engine, env.user_id, "settings", thread_id=777)
    await connected_pair(env, "Борис")

    assert await notifier(env).run_tenant(env.user_id) == 1

    [note] = env.session.sent_to(777)
    assert note.chat_id == env.chat_id and note.text == texts.NOTIFY_CONTACT_ACCEPTED.format(name="Борис")
    [button] = [b for row in note.reply_markup.inline_keyboard for b in row]
    assert button.url == env.settings.mini_app_direct_link
    assert [r[1] for r in await notification_rows(env, env.user_id)] == ["done"]
    assert await notifier(env).run_tenant(env.user_id) == 0 and len(env.session.sent_to(777)) == 1  # never twice


async def test_without_a_settings_topic_the_main_chat_gets_it(bot_env):
    env = bot_env
    await connected_pair(env)
    await notifier(env).run_tenant(env.user_id)
    assert env.session.sent_to(None)[-1].text == texts.NOTIFY_CONTACT_ACCEPTED.format(name="Борис")


async def test_a_deleted_settings_topic_falls_back_to_the_main_chat(bot_env):
    env = bot_env
    await new_persona(env.owner_engine, env.user_id, "settings", thread_id=777)
    await connected_pair(env)
    env.session.fail_on("sendMessage", TelegramBadRequest, when=lambda m: m.message_thread_id == 777, message="Bad Request: message thread not found")

    await notifier(env).run_tenant(env.user_id)

    assert env.session.sent_to(None)[-1].text == texts.NOTIFY_CONTACT_ACCEPTED.format(name="Борис")
    assert [r[1] for r in await notification_rows(env, env.user_id)] == ["done"]


async def test_a_failed_delivery_waits_for_its_backoff_and_then_succeeds(bot_env):
    env = bot_env
    await connected_pair(env)
    env.session.fail_on("sendMessage", TelegramNetworkError, times=1, message="network is down")

    with pytest.raises(TelegramNetworkError):  # fail-fast: the process raises, the ROW remembers
        await notifier(env).run_tenant(env.user_id)
    [(row_id, status, attempts, in_backoff)] = await notification_rows(env, env.user_id)
    assert (status, attempts, in_backoff) == ("failed", 1, True)

    assert await notifier(env).run_tenant(env.user_id) == 0  # still backing off: nothing is claimed, nothing is sent
    await owner_exec(env.owner_engine, "UPDATE civ.notification_outbox SET locked_until = now() - interval '1 second' WHERE id = :i", i=row_id)
    assert await notifier(env).run_tenant(env.user_id) == 1
    assert [(r[1], r[2]) for r in await notification_rows(env, env.user_id)] == [("done", 2)]


async def test_telegram_asking_to_slow_down_sets_the_delay_and_exhausted_attempts_end_dead(bot_env):
    env = bot_env
    await connected_pair(env)
    env.session.fail_on("sendMessage", TelegramRetryAfter, times=1, message="Too Many Requests", retry_after=600)
    with pytest.raises(TelegramRetryAfter):
        await notifier(env).run_tenant(env.user_id)
    [(row_id,)] = await owner_exec(env.owner_engine, "SELECT id FROM civ.notification_outbox WHERE user_id = :u AND locked_until > now() + interval '9 minutes'", u=env.user_id)

    env.session.fail_on("sendMessage", TelegramNetworkError, message="network is down")  # now it never works
    for _ in range(4):
        await owner_exec(env.owner_engine, "UPDATE civ.notification_outbox SET locked_until = now() - interval '1 second' WHERE id = :i", i=row_id)
        with pytest.raises(TelegramNetworkError):
            await notifier(env).run_tenant(env.user_id)
    [(status, attempts, last_error)] = await owner_exec(env.owner_engine, "SELECT status::text, attempts, last_error FROM civ.notification_outbox WHERE id = :i", i=row_id)
    assert (status, attempts) == ("dead", 5) and "network is down" in last_error  # five tries, then a human looks at it


async def test_a_notification_waits_for_a_group_it_can_be_written_to(bot_env):
    env = bot_env
    await connected_pair(env)
    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=env.user_id)

    assert await notifier(env).run_tenant(env.user_id) == 0
    assert env.session.sent() == [] and [(r[1], r[2]) for r in await notification_rows(env, env.user_id)] == [("pending", 0)]  # no attempt burned

    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'active' WHERE id = :u", u=env.user_id)
    assert await notifier(env).run_tenant(env.user_id) == 1


async def test_if_the_contact_is_gone_by_then_nothing_is_announced(bot_env):
    env = bot_env
    b = await connected_pair(env)
    await owner_exec(env.owner_engine, "DELETE FROM civ.contacts WHERE user_id = :a AND contact_user_id = :b", a=env.user_id, b=b)

    assert await notifier(env).run_tenant(env.user_id) == 1

    assert env.session.sent() == [] and [r[1] for r in await notification_rows(env, env.user_id)] == ["done"]


async def test_an_unknown_kind_goes_straight_to_dead(bot_env):
    env = bot_env
    await owner_exec(env.owner_engine, "INSERT INTO civ.notification_outbox (user_id, kind, idempotency_key) VALUES (:u, 'from_the_future', 'k')", u=env.user_id)

    with pytest.raises(notifications.UnknownKind):
        await notifier(env).run_tenant(env.user_id)

    assert [(r[1], r[2]) for r in await notification_rows(env, env.user_id)] == [("dead", 1)] and env.session.sent() == []


async def test_two_workers_never_deliver_one_notification_twice(bot_env):
    env = bot_env
    await connected_pair(env)

    results = await asyncio.gather(notifier(env).run_tenant(env.user_id), notifier(env).run_tenant(env.user_id))

    assert sorted(results) == [0, 1] and len(env.session.sent()) == 1


async def test_a_worker_that_lost_its_lease_cannot_finish_the_row(bot_env):
    env = bot_env
    await connected_pair(env)
    first = await notifications.claim_next(env.db, env.user_id, lease_seconds=300)
    assert first is not None and first.attempts == 1
    await owner_exec(env.owner_engine, "UPDATE civ.notification_outbox SET locked_until = now() - interval '1 second' WHERE id = :i", i=first.id)
    second = await notifications.claim_next(env.db, env.user_id, lease_seconds=300)  # the lease ran out: another worker takes over
    assert second is not None and second.id == first.id and second.attempts == 2

    assert await notifications.mark_done(env.db, first) is False  # the late worker is fenced off
    assert await notifications.fail(env.db, first, "late", backoff_base=1, backoff_cap=1) is None
    assert await notifications.mark_done(env.db, second) is True


# ================================================================== Celery hooks


def runtime_of(env) -> Runtime:
    return Runtime(db=env.db, settings=env.settings, llm=env.router, embedder=None, chat=env.chat)


async def test_the_dispatcher_offers_exactly_the_tenants_with_something_to_deliver(bot_env):
    env = bot_env
    idle = await new_user(env.owner_engine)
    await connected_pair(env)
    submitted: list[uuid.UUID] = []

    count = await tasks.dispatch_notifications_async(runtime_of(env), submitted.append)

    assert env.user_id in submitted and idle not in submitted and count == len(submitted)
    assert len(submitted) == len(set(submitted))


async def test_a_broker_hiccup_for_one_tenant_does_not_stop_the_others(bot_env):
    env = bot_env
    await connected_pair(env)
    other_a = await new_user(env.owner_engine)
    _, created = await make_invite(env.db, other_a)
    await accept(env.db, await new_user(env.owner_engine), created.invite_id)
    seen: list[uuid.UUID] = []

    def flaky(user_id: uuid.UUID) -> None:
        seen.append(user_id)
        if user_id == env.user_id:
            raise ConnectionError("broker is down")

    with pytest.raises(tasks.DispatchError):
        await tasks.dispatch_notifications_async(runtime_of(env), flaky)
    assert env.user_id in seen and other_a in seen  # the failure was reported AFTER everybody had been offered


async def test_send_notifications_end_to_end_and_the_housekeeping_task(bot_env):
    env = bot_env
    await new_persona(env.owner_engine, env.user_id, "settings", thread_id=777)
    await connected_pair(env, "Борис")

    assert await tasks.send_notifications_async(runtime_of(env), env.bot, env.user_id) == 1
    assert env.session.sent_to(777)[-1].text == texts.NOTIFY_CONTACT_ACCEPTED.format(name="Борис")

    await owner_exec(env.owner_engine, "UPDATE civ.notification_outbox SET processed_at = now() - interval '10 days' WHERE user_id = :u AND status = 'done'", u=env.user_id)
    stats = await tasks.purge_social_async(runtime_of(env))
    assert stats.purged >= 1
    assert await notification_rows(env, env.user_id) == []
    assert timedelta(days=env.settings.notification_done_ttl_days) == timedelta(days=7)

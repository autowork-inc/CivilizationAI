"""The userbot that creates the person's group (``bot/provision.py``, ``bot/userbot.py``, the provisioned branch of ``Registrar``).

Telethon itself is NOT exercised against Telegram (no account here): the provisioning logic runs against a fake ``UserbotApi`` and an in-memory Redis,
the Telethon adapter against a fake Telethon (the calls it makes, in order, with which arguments), and the registration of a group the userbot made
through the REAL aiogram Dispatcher on the fake Telegram wire and a real PostgreSQL."""

from __future__ import annotations

import json
import random
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from ai_civ.bot import provision, texts
from ai_civ.bot.provision import (
    ChatBinding,
    GroupProvisioner,
    GroupRef,
    ProvisionStore,
    RetryLater,
    UserbotError,
    UserNotReachable,
    UserRef,
)
from ai_civ.bot import userbot as userbot_module
from ai_civ.bot.userbot import BOT_RIGHTS, USER_RIGHTS, TelethonApi, telethon_proxy
from ai_civ.config import ConfigError, Settings
from tests.tg_fakes import BOT_ID, make_bot, make_bot_membership_update, make_member_update, make_service_update

USERBOT_ID = 777_000_111
OWNER = 555_000_222
BOT_USERNAME = "PerfectPerson_bot"


@pytest.fixture(autouse=True)
def _no_forum_pause(monkeypatch):
    monkeypatch.setattr(userbot_module, "FORUM_SETTLE_SECONDS", 0)  # the adapter waits 2 s after switching Topics on: not in every test


def settings(**kw) -> Settings:
    base = dict(telegram_api_id=1, telegram_api_hash=SecretStr("h"), telegram_userbot_session=SecretStr("s"), telegram_bot_username=BOT_USERNAME)
    return Settings(_env_file=None, **{**base, **kw})


class MemRedis:
    """The subset of ``redis.asyncio.Redis`` the store uses (no expiry: tests look at the keys and at what was asked for)."""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.error: Exception | None = None

    def _gate(self) -> None:
        if self.error is not None:
            raise self.error

    async def get(self, name):
        self._gate()
        return self.data.get(name)

    async def set(self, name, value, nx=False, ex=None):
        self._gate()
        if nx and name in self.data:
            return None
        self.data[name] = str(value)
        if ex is not None:
            self.ttl[name] = ex
        return True

    async def delete(self, name):
        self._gate()
        self.data.pop(name, None)

    async def incr(self, name):
        self._gate()
        self.data[name] = str(int(self.data.get(name, 0)) + 1)
        return int(self.data[name])

    async def expire(self, name, seconds):
        self._gate()
        self.ttl[name] = seconds


class FakeApi:
    """A ``UserbotApi`` that records what it was asked and fails where told to."""

    userbot_id = USERBOT_ID

    def __init__(self, store: ProvisionStore | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail: dict[str, Exception] = {}
        self.store = store
        self.binding_seen_by_bot_add: ChatBinding | None | str = "unset"
        self.chat_id = -int(f"100{random.randrange(10**9, 10**10)}")

    def _step(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        if name in self.fail:
            raise self.fail[name]

    async def create_forum_group(self, title):
        self._step("create", title)
        return GroupRef(self.chat_id, handle="H")

    async def add_bot_as_admin(self, group, bot_username):
        if self.store is not None:
            self.binding_seen_by_bot_add = await self.store.chat(group.chat_id)
        self._step("add_bot", bot_username)

    async def add_user(self, group, user_id, username):
        self._step("add_user", user_id, username)
        return UserRef("U")

    async def make_admin(self, group, user):
        self._step("make_admin")

    async def invite_link(self, group, *, days):
        self._step("invite_link", days)
        return "https://t.me/+one-use"

    async def leave(self, group):
        self._step("leave")

    async def delete(self, group):
        self._step("delete")

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


def factory(api: FakeApi):
    @asynccontextmanager
    async def connect(_settings):
        yield api

    return connect


@pytest.fixture
def world():
    redis = MemRedis()
    store = ProvisionStore(redis, prefix="t:prov")
    bot, session = make_bot()
    return SimpleNamespace(redis=redis, store=store, bot=bot, session=session, settings=settings(), api=FakeApi(store))


async def run(w, **kw):
    args = dict(owner_id=OWNER, display_name="Аня", username="anya")
    return await provision.provision_group_async(w.settings, w.bot, w.store, factory(w.api), **{**args, **kw})


def dms(w) -> list:
    return [m for m in w.session.sent() if m.chat_id == OWNER]


# ================================================================== the userbot's part


async def test_the_person_is_added_and_made_admin_the_group_is_bound_before_the_bot_arrives_and_the_userbot_leaves(world):
    w = world
    outcome = await run(w)

    assert w.api.names() == ["create", "add_bot", "add_user", "make_admin", "leave"]  # in this order: the userbot is the last to go
    assert w.api.calls[0] == ("create", "AI Persona | Аня") and w.api.calls[1] == ("add_bot", BOT_USERNAME)
    assert w.api.calls[2] == ("add_user", OWNER, "anya")  # and no topic anywhere: the userbot makes none
    assert w.api.binding_seen_by_bot_add == ChatBinding(OWNER, USERBOT_ID)  # the bot's first events must already find the owner
    assert (outcome.joined, outcome.link, outcome.chat_id) == (True, None, w.api.chat_id)
    assert [m.text for m in dms(w)] == [texts.PROVISION_ADDED.format(title="AI Persona | Аня")]
    assert (await w.store.user(OWNER)).state == "done" and await w.store.created_today() == 1
    assert await w.store.acquire_lock(60)  # the lock was released


async def test_a_person_who_cannot_be_added_gets_a_one_use_link_and_the_userbot_still_leaves(world):
    w = world
    w.api.fail["add_user"] = UserNotReachable("UserPrivacyRestrictedError")

    outcome = await run(w)

    assert w.api.names() == ["create", "add_bot", "add_user", "invite_link", "leave"] and w.api.calls[3] == ("invite_link", 7)
    assert (outcome.joined, outcome.link) == (False, "https://t.me/+one-use")
    [dm] = dms(w)
    assert "https://t.me/+one-use" in dm.text and "7 дн." in dm.text
    assert dm.reply_markup.inline_keyboard[0][0].url == "https://t.me/+one-use"  # a button that opens the link
    assert (await w.store.user(OWNER)).link == "https://t.me/+one-use"


async def test_a_person_without_a_username_goes_straight_to_the_link_in_the_real_adapter_case(world):
    w = world
    w.api.fail["add_user"] = UserNotReachable("no @username")
    assert (await run(w, username=None)).link is not None


async def test_failing_to_promote_the_person_does_not_fail_the_group(world):
    w = world
    w.api.fail["make_admin"] = UserbotError("CHAT_ADMIN_REQUIRED")
    outcome = await run(w)
    assert outcome.joined and w.api.names()[-1] == "leave"


@pytest.mark.parametrize("step", ["add_bot", "invite_link_after_privacy", "leave"])
async def test_a_failure_after_the_group_exists_deletes_it_and_sends_the_manual_instructions(world, step):
    w = world
    if step == "invite_link_after_privacy":
        w.api.fail["add_user"] = UserNotReachable("privacy")
        w.api.fail["invite_link"] = UserbotError("no link")
    else:
        w.api.fail[step] = UserbotError("boom")

    assert await run(w) is None

    assert w.api.names()[-1] == "delete"  # the half-made group (the userbot is its creator) is removed, a group with the userbot inside is never kept
    assert await w.store.chat(w.api.chat_id) is None  # and it no longer points at the person
    assert (await w.store.user(OWNER)).state == "failed"  # /start will not hammer the account for a few minutes
    [manual] = w.session.sent()
    assert (manual.chat_id, manual.text) == (OWNER, texts.PROVISION_FAILED)
    assert manual.reply_markup.inline_keyboard[0][0].url == texts.create_persona_url(BOT_USERNAME)
    [crash] = w.session.documents()  # the author is told: the traceback as a file
    assert crash.chat_id == w.settings.feedback_admin_telegram_id and "UserbotError" in crash.caption
    assert await w.store.acquire_lock(60)


async def test_a_failing_delete_is_logged_and_does_not_hide_the_original_failure(world):
    w = world
    w.api.fail["add_bot"] = UserbotError("boom")
    w.api.fail["delete"] = UserbotError("cannot delete")
    assert await run(w) is None  # still ends in the manual instructions
    assert dms(w)[0].text == texts.PROVISION_FAILED


async def test_a_flood_wait_before_anything_exists_is_handed_to_celery_and_nobody_is_told(world):
    w = world
    w.api.fail["create"] = RetryLater(40)
    await w.store.claim_user(OWNER)

    with pytest.raises(RetryLater) as raised:
        await run(w)

    assert raised.value.seconds == 40 and w.api.names() == ["create"]
    assert w.session.sent() == [] and (await w.store.user(OWNER)).state == "pending"  # still claimed: the retry continues the same job
    assert await w.store.acquire_lock(60)  # the connection is free for the retry


async def test_only_one_telethon_connection_at_a_time(world):
    w = world
    assert await w.store.acquire_lock(60)  # somebody else is connected
    with pytest.raises(RetryLater):
        await run(w)
    assert w.api.calls == []  # nothing was even tried


async def test_the_daily_limit_sends_the_manual_instructions_without_touching_telegram(world):
    w = world
    w.settings = settings(userbot_max_groups_per_day=2)
    await w.store.count_creation()
    await w.store.count_creation()

    assert await run(w) is None

    assert w.api.calls == [] and dms(w)[0].text == texts.PROVISION_FAILED
    assert await w.store.acquire_lock(60)


@pytest.mark.parametrize(("name", "expected"), [("Аня", "AI Persona | Аня"), (None, "AI Persona | Друг"), ("  \u202e\n ", "AI Persona | Друг"), ("x" * 500, None)])
def test_the_title_is_one_clean_line_that_fits_telegram(name, expected):
    title = provision.group_title(settings(), name)
    if expected is None:
        assert len(title) <= 120 and title.startswith("AI Persona | ")
    else:
        assert title == expected


# ================================================================== /start in the bot


def start_message(text="/start", user_id=OWNER, is_bot=False, first_name="Аня", username="anya"):
    answers: list[tuple] = []

    async def answer(text, **kw):
        answers.append((text, kw))

    message = SimpleNamespace(text=text, from_user=SimpleNamespace(id=user_id, is_bot=is_bot, first_name=first_name, username=username), answer=answer)
    message.answers = answers
    return message


def provisioner(w, enqueue=None):
    queued: list[tuple] = []
    p = GroupProvisioner(w.store, w.settings, enqueue or (lambda *a: queued.append(a)))
    p.queued = queued
    return p


@pytest.mark.parametrize(("text", "plain"), [("/start", True), ("/start@PerfectPerson_bot", True), ("/START", True), ("/start connect_abc", False), ("/start setup", False), ("hello", False), ("", False)])
def test_only_a_bare_start_creates_a_group(text, plain):
    assert GroupProvisioner.is_plain_start(start_message(text)) is plain


async def test_the_first_start_claims_the_person_queues_the_task_and_says_so(world):
    w = world
    p = provisioner(w)
    message = start_message()

    assert await p.on_start(message) is True

    assert p.queued == [(OWNER, "Аня", "anya")]
    assert [a[0] for a in message.answers] == [texts.PROVISION_START]
    assert (await w.store.user(OWNER)).state == "pending"


async def test_a_second_start_while_it_is_being_made_queues_nothing_more(world):
    w = world
    p = provisioner(w)
    await p.on_start(start_message())
    again = start_message()
    assert await p.on_start(again) is True
    assert len(p.queued) == 1 and [a[0] for a in again.answers] == [texts.PROVISION_WAIT]


async def test_a_start_after_success_makes_a_fresh_group_and_forgets_the_old_one(world):
    w = world
    p = provisioner(w)
    await w.store.finish_user(OWNER, -1001, "https://t.me/+one-use")
    await w.store.bind_chat(-1001, ChatBinding(OWNER, USERBOT_ID))
    message = start_message()
    assert await p.on_start(message) is True
    assert message.answers[0][0] == texts.PROVISION_START and [q[0] for q in p.queued] == [OWNER]  # a second group, not the old link
    assert await w.store.chat(-1001) is None  # the old group is nobody's now
    assert (await w.store.user(OWNER)).state == "pending"


async def test_a_task_that_is_still_working_is_waited_for_but_a_dead_one_is_replaced(world):
    w = world
    p = provisioner(w)
    await w.store.claim_user(OWNER)  # claimed just now: the task is running
    message = start_message()
    assert await p.on_start(message) is True and message.answers[0][0] == texts.PROVISION_WAIT and p.queued == []

    w.redis.data["t:prov:u:" + str(OWNER)] = json.dumps({"state": "pending", "since": time.time() - 600})  # claimed ten minutes ago: the worker died
    message = start_message()
    assert await p.on_start(message) is True and message.answers[0][0] == texts.PROVISION_START and len(p.queued) == 1


async def test_after_a_failure_or_at_the_daily_limit_the_manual_way_is_shown_instead(world):
    w = world
    p = provisioner(w)
    await w.store.fail_user(OWNER)
    assert await p.on_start(start_message()) is False and p.queued == []

    w2 = SimpleNamespace(store=ProvisionStore(MemRedis(), prefix="x"), settings=settings(userbot_max_groups_per_day=1))
    await w2.store.count_creation()
    assert await provisioner(w2).on_start(start_message()) is False


async def test_a_broken_queue_or_a_broken_redis_costs_nothing_the_old_way_still_works(world):
    w = world

    def broken(*_a):
        raise RuntimeError("broker down")

    p = provisioner(w, broken)
    assert await p.on_start(start_message()) is False
    assert await w.store.user(OWNER) is None  # the claim was given back: the person can try again

    w.redis.error = ConnectionError("redis down")
    assert await provisioner(w).on_start(start_message()) is False


async def test_bots_and_anonymous_senders_are_never_given_a_group(world):
    p = provisioner(world)
    assert await p.on_start(start_message(is_bot=True)) is False
    anonymous = start_message()
    anonymous.from_user = None
    assert await p.on_start(anonymous) is False and p.queued == []


# ================================================================== the store


async def test_the_store_keeps_secrets_out_and_gives_everything_a_lifetime(world):
    w = world
    await w.store.claim_user(OWNER)
    await w.store.bind_chat(-1001, ChatBinding(OWNER, USERBOT_ID))
    await w.store.finish_user(OWNER, -1001, "https://t.me/+x")
    await w.store.count_creation()
    await w.store.acquire_lock(60)
    assert w.redis.data and set(w.redis.ttl) == set(w.redis.data)  # every key expires
    assert all(key.startswith("t:prov:") for key in w.redis.data)
    assert not any("session" in v.lower() for v in w.redis.data.values())  # the StringSession never goes to Redis
    assert json.loads(w.redis.data["t:prov:c:-1001"]) == {"owner": OWNER, "userbot": USERBOT_ID, "settings": None, "feedback": None}


async def test_one_claim_at_a_time(world):
    w = world
    assert await w.store.claim_user(OWNER) is True and await w.store.claim_user(OWNER) is False
    await w.store.release_user(OWNER)
    assert await w.store.claim_user(OWNER) is True


# ================================================================== the Telethon adapter (calls only; a fake Telethon)


class FakeTelethon:
    """Records every request the adapter sends and answers like Telegram would for the happy path."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.errors_to_raise: dict[str, Exception] = {}
        self.entities: dict[str, Any] = {}
        self.invite_result: Any = SimpleNamespace(missing_invitees=[])
        self.responses: dict[str, Any] = {}  # request name -> a function answering it (instead of the default)

    async def __call__(self, request):
        name = type(request).__name__
        self.requests.append(request)
        if name in self.errors_to_raise:
            raise self.errors_to_raise[name]
        if name in self.responses:
            return self.responses[name](request)
        if name == "CreateChannelRequest":
            from datetime import UTC, datetime

            from telethon.tl.types import Channel, ChatPhotoEmpty

            return SimpleNamespace(chats=[Channel(id=1234567890, title="t", photo=ChatPhotoEmpty(), date=datetime.now(UTC), megagroup=True)])
        if name == "InviteToChannelRequest":
            return self.invite_result
        if name == "ExportChatInviteRequest":
            return SimpleNamespace(link="https://t.me/+abc")
        return True

    async def get_input_entity(self, ref):
        if ref not in self.entities:
            raise ValueError(f"Could not find the input entity for {ref!r}")
        return self.entities[ref]


def fake_tl():
    from telethon import errors, functions, types, utils

    return SimpleNamespace(functions=functions, types=types, errors=errors, utils=utils)


def adapter():
    client = FakeTelethon()
    client.entities = {"PerfectPerson_bot": "BOT-ENTITY", "anya": "ANYA-ENTITY"}
    return TelethonApi(client, USERBOT_ID, fake_tl()), client


async def test_the_adapter_creates_a_megagroup_then_switches_topics_on_and_returns_the_bot_api_chat_id():
    api, client = adapter()
    group = await api.create_forum_group("AI Persona | Аня")

    create, forum = client.requests
    assert (type(create).__name__, create.title, create.megagroup) == ("CreateChannelRequest", "AI Persona | Аня", True)
    assert (type(forum).__name__, forum.enabled) == ("ToggleForumRequest", True)
    assert group.chat_id == -1001234567890  # the Bot API id of channel 1234567890


async def test_the_bot_is_invited_and_given_every_admin_right_including_appointing_admins():
    api, client = adapter()
    await api.add_bot_as_admin(GroupRef(-1001, "CH"), "@PerfectPerson_bot")

    invite, promote = client.requests
    assert (type(invite).__name__, invite.users) == ("InviteToChannelRequest", ["BOT-ENTITY"])
    rights = promote.admin_rights
    assert type(promote).__name__ == "EditAdminRequest" and rights.manage_topics and rights.change_info and rights.delete_messages and rights.ban_users
    assert rights.add_admins and rights.invite_users and not rights.anonymous  # the bot appoints the owner when they join by the link
    assert BOT_RIGHTS["manage_topics"] and BOT_RIGHTS["add_admins"] and BOT_RIGHTS["invite_users"] and USER_RIGHTS["add_admins"]


async def test_the_adapter_has_no_topic_function_and_waits_after_switching_topics_on(monkeypatch):
    """The userbot makes no topics (the bot does), and Telegram gets two seconds to apply the forum mode before the bot is given its rights."""
    api, client = adapter()
    assert not hasattr(api, "create_topic")
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        assert [type(r).__name__ for r in client.requests] == ["CreateChannelRequest", "ToggleForumRequest"]  # the pause comes right after ToggleForum

    monkeypatch.undo()  # the autouse fixture's zero pause is replaced by the production value
    monkeypatch.setattr(userbot_module.asyncio, "sleep", fake_sleep)
    await api.create_forum_group("t")
    assert slept == [2.0] and userbot_module.FORUM_SETTLE_SECONDS == 2.0


async def test_the_person_is_added_by_username_and_promoted_with_every_right():
    api, client = adapter()
    user = await api.add_user(GroupRef(-1001, "CH"), OWNER, "@anya")
    await api.make_admin(GroupRef(-1001, "CH"), user)

    invite, promote = client.requests
    assert invite.users == ["ANYA-ENTITY"] and promote.admin_rights.add_admins and promote.admin_rights.manage_topics


async def test_every_refusal_to_add_the_person_becomes_the_same_not_reachable_error():
    from telethon import errors

    api, client = adapter()
    with pytest.raises(UserNotReachable):  # no @username: a user account cannot find a stranger by id
        await api.add_user(GroupRef(-1001, "CH"), OWNER, None)
    with pytest.raises(UserNotReachable):  # an unknown @username
        await api.add_user(GroupRef(-1001, "CH"), OWNER, "ghost")
    for error in (
        errors.UserPrivacyRestrictedError(request=None),
        errors.UserNotMutualContactError(request=None),
        errors.PeerFloodError(request=None),
        errors.UserChannelsTooMuchError(request=None),
    ):
        client.errors_to_raise["InviteToChannelRequest"] = error
        with pytest.raises(UserNotReachable):
            await api.add_user(GroupRef(-1001, "CH"), OWNER, "anya")
    client.errors_to_raise.clear()
    client.invite_result = SimpleNamespace(missing_invitees=[object()])  # newer layers: refused invitees come back in the result
    with pytest.raises(UserNotReachable):
        await api.add_user(GroupRef(-1001, "CH"), OWNER, "anya")


async def test_a_flood_wait_is_retry_later_and_a_revoked_session_is_a_clear_error():
    from telethon import errors

    api, client = adapter()
    client.errors_to_raise["CreateChannelRequest"] = errors.FloodWaitError(request=None, capture=45)
    with pytest.raises(RetryLater) as flood:
        await api.create_forum_group("t")
    assert flood.value.seconds == 45
    client.errors_to_raise["CreateChannelRequest"] = errors.AuthKeyDuplicatedError(request=None)
    with pytest.raises(UserbotError, match="revoked"):
        await api.create_forum_group("t")


async def test_the_invite_link_is_single_use_and_expires():
    api, client = adapter()
    link = await api.invite_link(GroupRef(-1001, "CH"), days=7)
    [export] = client.requests
    assert link == "https://t.me/+abc" and export.usage_limit == 1 and export.expire_date is not None and export.title == "AI Persona"


async def test_leaving_and_deleting_are_the_channel_requests():
    api, client = adapter()
    await api.leave(GroupRef(-1001, "CH"))
    await api.delete(GroupRef(-1001, "CH"))
    assert [type(r).__name__ for r in client.requests] == ["LeaveChannelRequest", "DeleteChannelRequest"]


def test_the_proxy_url_becomes_a_telethon_proxy_with_remote_dns():
    assert telethon_proxy(None) is None and telethon_proxy("") is None
    assert telethon_proxy("socks5h://proxy:1080") == ("socks5", "proxy", 1080, True)
    assert telethon_proxy("http://proxy:3128") == ("http", "proxy", 3128)
    with pytest.raises(ConfigError):
        telethon_proxy("proxy:1080")


async def test_connecting_without_credentials_is_a_config_error_not_a_crash_in_telethon():
    with pytest.raises(ConfigError):
        async with TelethonApi.connect(Settings(_env_file=None)):
            pass


def test_the_userbot_is_ready_only_with_all_three_credentials_and_the_switch():
    assert settings().userbot_ready
    assert not settings(telegram_userbot_session=None).userbot_ready and not settings(telegram_api_id=None).userbot_ready
    assert not settings(telegram_api_hash=None).userbot_ready and not settings(userbot_enabled=False).userbot_ready
    assert not Settings(_env_file=None, telegram_api_id="", telegram_api_hash="", telegram_userbot_session="").userbot_ready  # blank in .env = off


def test_the_userbot_has_its_own_queue_and_the_default_worker_does_not_read_it():
    from ai_civ.celery_app import app

    assert app.conf.task_routes == {"ai_civ.provision_group": {"queue": "userbot"}} and app.conf.task_default_queue == "celery"
    compose = open("docker-compose.yml", encoding="utf-8").read()  # no YAML parser in the dev environment: look at the commands as text
    assert '"-Q", "userbot", "--concurrency=1"' in compose  # exactly one process on the userbot queue
    assert '"worker", "--loglevel=info", "-Q", "celery"' in compose  # the normal worker reads only its own queue


# ================================================================== registering a group the userbot made (real PostgreSQL, fake Telegram)

GHOST = 31_337  # the "creator" of the fake group: not the owner, not the userbot, and (for these tests) not a member either


def made_group(env, *, owner_in=True, userbot_in=False, **kw):
    """A group as the userbot leaves it: Topics on, the bot an admin, and the humans in it as asked."""
    chat = -int(f"100{random.randrange(10**9, 10**10)}")
    group = env.session.add_group(chat, GHOST, **kw)
    group.members.clear()
    if owner_in:
        group.members.add(env.owner_tg)
    if userbot_in:
        group.members.add(USERBOT_ID)
    return chat, group


@pytest.fixture
async def prov_env(bare_env):
    env = bare_env
    env.owner_tg = random.randrange(10**9, 10**12)
    env.store = ProvisionStore(MemRedis(), prefix="t:reg")
    env.dispatcher["registrar"].provisions = env.store
    return env


async def bind(env, chat, owner=None):
    await env.store.bind_chat(chat, ChatBinding(owner or env.owner_tg, USERBOT_ID))


async def test_the_userbot_leaving_registers_the_person_it_made_the_group_for(prov_env):
    env = prov_env
    chat, group = made_group(env, userbot_in=True)
    await bind(env, chat)

    await env.feed_update(make_bot_membership_update(chat_id=chat, actor_id=USERBOT_ID))  # the userbot adds the bot: the owner is in, the userbot is not gone yet
    assert await env.count_users() == 0  # not registered while the userbot is inside
    assert [m.text for m in env.session.sent()] == [texts.GROUP_READY]  # but the owner is already promoted and greeted

    group.members.discard(USERBOT_ID)
    await env.feed_update(make_service_update(chat_id=chat, left=USERBOT_ID))  # the userbot leaves: that is what completes the registration

    row = await env.user_row(env.owner_tg)
    assert row is not None and row["forum_chat_id"] == chat and row["group_state"] == "active"
    assert row["onboarding_state"] == "in_progress"  # the first question was asked, exactly like after a manual registration
    assert any(m.chat_id == chat for m in env.session.sent())


async def test_a_person_who_joins_by_link_after_the_userbot_left_is_registered_on_arrival(prov_env):
    env = prov_env
    chat, group = made_group(env, owner_in=False)  # the userbot is gone, the owner has not opened the link yet
    await bind(env, chat)
    await env.feed_update(make_bot_membership_update(chat_id=chat, actor_id=USERBOT_ID))
    assert await env.count_users() == 0 and env.session.sent() == []

    group.members.add(env.owner_tg)
    await env.feed_update(make_member_update(chat_id=chat, user_id=env.owner_tg, new_status="member", old_status="left"))

    assert (await env.user_row(env.owner_tg))["forum_chat_id"] == chat


async def test_the_owners_first_message_also_registers_and_somebody_elses_does_not(prov_env):
    env = prov_env
    chat, group = made_group(env)
    await bind(env, chat)
    stranger = random.randrange(10**9, 10**12)
    group.members.add(stranger)
    await env.say("hello", user_id=stranger, chat_id=chat)
    assert await env.count_users() == 0 and env.session.sent() == []  # a stranger cannot take the group (and the group is crowded anyway)

    group.members.discard(stranger)
    await env.say("привет", user_id=env.owner_tg, chat_id=chat)
    assert (await env.user_row(env.owner_tg)) is not None


async def test_the_userbot_still_inside_blocks_registration_even_when_everything_else_is_fine(prov_env):
    env = prov_env
    chat, _ = made_group(env, userbot_in=True)  # three members: the owner, the userbot, the bot
    await bind(env, chat)
    await env.say("привет", user_id=env.owner_tg, chat_id=chat)
    assert await env.count_users() == 0  # a second human account in the group is not "private", whoever it is


async def test_a_group_the_userbot_did_not_make_still_needs_its_real_creator(prov_env):
    env = prov_env
    chat = -int(f"100{random.randrange(10**9, 10**10)}")
    env.session.add_group(chat, GHOST)  # nobody bound it to the owner
    await env.feed_update(make_bot_membership_update(chat_id=chat, actor_id=env.owner_tg))
    assert await env.count_users() == 0 and env.session.sent()[-1].text == texts.NOT_CREATOR  # the old rule is untouched


async def test_a_group_with_a_missing_requirement_names_it_once_the_owner_is_in(prov_env):
    env = prov_env
    chat, _ = made_group(env, is_forum=False)  # the userbot forgot Topics
    await bind(env, chat)
    await env.feed_update(make_member_update(chat_id=chat, user_id=env.owner_tg, new_status="member", old_status="left"))
    assert env.session.sent()[-1].text == texts.setup_checklist([texts.PROBLEM_NOT_FORUM]) and await env.count_users() == 0


async def test_the_userbots_own_arrival_never_freezes_a_registered_group(prov_env):
    env = prov_env
    chat, group = made_group(env)
    await bind(env, chat)
    await env.say("привет", user_id=env.owner_tg, chat_id=chat)
    row = await env.user_row(env.owner_tg)
    assert row is not None

    await env.feed_update(make_member_update(chat_id=chat, user_id=USERBOT_ID, new_status="member", old_status="left"))
    assert (await env.user_row(env.owner_tg))["group_state"] == "active"  # not an intruder: it is the userbot, about to leave
    await env.feed_update(make_member_update(chat_id=chat, user_id=random.randrange(10**9, 10**12), new_status="member", old_status="left"))
    assert (await env.user_row(env.owner_tg))["group_state"] == "frozen"  # a real stranger still freezes it


async def test_without_redis_the_group_is_never_trusted_on_a_guess(prov_env):
    env = prov_env
    chat, _ = made_group(env)
    await bind(env, chat)
    env.store._redis.error = ConnectionError("redis down")
    await env.say("привет", user_id=env.owner_tg, chat_id=chat)
    assert await env.count_users() == 0  # treated as an ordinary group: the owner is not its creator


async def test_start_in_private_goes_through_the_userbot_when_it_is_configured_and_falls_back_when_it_cannot(prov_env):
    env = prov_env
    queued: list[tuple] = []
    env.dispatcher["provisioner"] = GroupProvisioner(env.store, settings(), lambda *a: queued.append(a))
    tg = random.randrange(10**9, 10**12)

    await env.say("/start", user_id=tg, chat_id=tg, chat_type="private")
    assert [m.text for m in env.session.sent()] == [texts.PROVISION_START]
    assert len(queued) == 1 and queued[0][0] == tg

    other = random.randrange(10**9, 10**12)
    env.dispatcher["provisioner"] = GroupProvisioner(env.store, settings(userbot_max_groups_per_day=0), lambda *a: queued.append(a))
    await env.say("/start", user_id=other, chat_id=other, chat_type="private")
    assert env.session.sent()[-1].text == texts.UNREGISTERED and len(queued) == 1  # over the limit: the old instructions, nothing queued


async def test_an_invitation_start_never_creates_a_group(prov_env):
    env = prov_env
    queued: list[tuple] = []
    env.dispatcher["provisioner"] = GroupProvisioner(env.store, settings(), lambda *a: queued.append(a))
    tg = random.randrange(10**9, 10**12)
    await env.say("/start connect_" + "a" * 22, user_id=tg, chat_id=tg, chat_type="private")
    assert queued == []  # the handshake owns /start with a payload


# ================================================================== the Celery task


async def test_the_task_connects_telethon_hands_over_the_person_and_lets_a_flood_wait_reach_celery(monkeypatch):
    import asyncio

    from ai_civ import tasks

    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_bot(_settings):
        yield "BOT"

    @asynccontextmanager
    async def fake_store(_settings):
        yield "STORE"

    async def fake_run(settings_, bot, store, api_factory, **kw):
        seen.update(bot=bot, store=store, factory=api_factory, kw=kw)
        if seen.get("flood"):
            raise RetryLater(12)

    monkeypatch.setattr(tasks, "get_settings", lambda: settings())
    monkeypatch.setattr(tasks, "open_bot", fake_bot)
    monkeypatch.setattr(tasks.provision_module, "open_store", fake_store)
    monkeypatch.setattr(tasks.provision_module, "provision_group_async", fake_run)

    # the task body is synchronous (Celery) and runs its own event loop: call it from a thread, as a worker would
    await asyncio.to_thread(tasks.provision_group.run, OWNER, "Аня", "anya")
    assert (seen["bot"], seen["store"]) == ("BOT", "STORE") and seen["factory"] == TelethonApi.connect
    assert seen["kw"] == {"owner_id": OWNER, "display_name": "Аня", "username": "anya"}
    assert tasks.provision_group.name == "ai_civ.provision_group" and tasks.provision_group.max_retries == 2  # bounded

    seen["flood"] = True
    with pytest.raises(RetryLater):  # called directly (no worker), retry() re-raises the cause: with a worker, Celery re-queues it after the wait
        await asyncio.to_thread(tasks.provision_group.run, OWNER, "Аня", "anya")


@pytest.mark.parametrize("bad", ["s", "1" + "A" * 300, "1" + "A" * 400])
async def test_a_malformed_session_string_is_a_clear_config_error(bad):
    from ai_civ.bot.userbot import TelethonApi
    from ai_civ.config import ConfigError

    with pytest.raises(ConfigError, match="повреждена"):
        async with TelethonApi.connect(settings(telegram_userbot_session=SecretStr(bad))):
            pass


def test_a_quoted_session_string_is_unwrapped():
    assert Settings(_env_file=None, telegram_userbot_session="  '1abc'  ").telegram_userbot_session.get_secret_value() == "1abc"

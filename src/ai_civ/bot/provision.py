"""The userbot creates the person's private forum group, so they do not have to (Telethon; ``bot/userbot.py`` is the Telegram side).

Flow, started by a plain ``/start`` in the private chat (an unknown person, or a registered one whose group is broken - see ``group_health``; ``/start`` ALWAYS starts
a fresh group, whatever an earlier attempt left behind):

  1. the bot claims the person in Redis (one group per person at a time), answers "creating your group" and queues the Celery task
     ``ai_civ.provision_group`` on its own queue (``userbot``, one worker, one Telethon connection at a time);
  2. the task, through the userbot account: creates a supergroup ``<prefix><name>``, enables Topics (and waits two seconds for Telegram to apply it), adds the bot
     and gives it EVERY administrator right (including the right to appoint administrators, which the bot needs to promote the owner), then tries to add the person and
     make them an administrator; when that is impossible (privacy settings, no @username to find them by, ...) it exports a ONE-USE invite link instead and the bot sends
     it to the person; finally the userbot LEAVES the group, the bot stays in charge. The userbot makes NO topics: everything inside the group is the bot's work;
  3. the BOT process finishes the job: a group the userbot made is bound to its future owner in Redis (``<prefix>:c:<chat>``); the moment the owner is in the group the
     bot makes them an administrator, creates the two system topics (Settings, Ideas and feedback) and greets them in the main chat (``Registrar._welcome_owner``; the ids
     of the topics are kept in the binding), and ``Registrar`` registers the tenant (or, for a person who already had a broken group, moves their tenant:
     ``civ.relocate_group``) as soon as the userbot is out and the usual checks pass. Registration itself stays ``civ.register_tenant``; the topics are adopted as the
     Settings / feedback personas.

What this does NOT do (and why): handing the CREATOR rank to the person. Telethon 1.45 has no ``channels.editCreator`` and the call needs the
userbot's 2FA password plus a person with 2FA enabled for a week; the person becomes an administrator with every right (they can add admins,
change the group, pin, ban), and the group has no active creator after the userbot leaves.

Failure model: nothing loops. A failure after the group exists deletes it (best effort), tells the person to do it by hand (the old flow), and
leaves a short "failed" mark so ``/start`` does not hammer the account; FloodWait before the group exists is retried by Celery, bounded.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import ChatMemberAdministrator, Message

from ai_civ.bot import invites, texts
from ai_civ.config import Settings

log = logging.getLogger(__name__)

PENDING_SECONDS = 900  # a claim that nobody finished (a crashed worker) frees the person after this long
PENDING_FRESH_SECONDS = 120  # a task that started less than this long ago is still working: a second /start only waits (older = presumed dead, start again)
WELCOME_SECONDS = 30 * 86_400
DONE_SECONDS = 30 * 86_400
FAILED_SECONDS = 30  # a second /start right after a failure gets the manual way (no hammering of the account); after that, a fresh attempt
MAX_TITLE = 120  # Telegram: a group title is 1-128 characters


class UserbotError(RuntimeError):
    """The userbot could not do it (not authorised, limits, a Telegram refusal that retrying cannot fix)."""


class RetryLater(UserbotError):
    """Telegram asked to wait (FloodWait). Only raised before the group exists, where retrying is harmless."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"Telegram asks to wait {seconds} s")
        self.seconds = seconds


class UserNotReachable(UserbotError):
    """The person cannot be added by the userbot (privacy settings, not found by @username, not a mutual contact, flood)."""


@dataclass(frozen=True)
class GroupRef:
    chat_id: int  # the Bot API id (-100...)
    handle: Any = None  # whatever the Telegram side needs to address the group again (opaque here)


@dataclass(frozen=True)
class UserRef:
    handle: Any = None


class UserbotApi(Protocol):
    """What the provisioner needs from the userbot account. ``bot/userbot.py::TelethonApi`` is the real thing; tests use a fake."""

    userbot_id: int

    async def create_forum_group(self, title: str) -> GroupRef: ...
    async def add_bot_as_admin(self, group: GroupRef, bot_username: str) -> None: ...
    async def add_user(self, group: GroupRef, user_id: int, username: str | None) -> UserRef: ...  # raises UserNotReachable
    async def make_admin(self, group: GroupRef, user: UserRef) -> None: ...
    async def invite_link(self, group: GroupRef, *, days: int) -> str: ...
    async def leave(self, group: GroupRef) -> None: ...
    async def delete(self, group: GroupRef) -> None: ...


@dataclass(frozen=True)
class ChatBinding:
    owner: int  # the Telegram id of the person this group was made for
    userbot: int  # the userbot's own Telegram id (it must be gone before the person is registered)
    settings_thread: int | None = None  # the topics the BOT made when the owner arrived (the tenant may not exist yet); adopted as the Settings / feedback personas
    feedback_thread: int | None = None


@dataclass(frozen=True)
class UserState:
    state: str  # pending | done | failed
    chat_id: int | None = None
    link: str | None = None
    since: float | None = None  # pending: when the claim was made (epoch seconds)

    def working(self, now: float) -> bool:
        """A task is probably still running for this person (claimed moments ago)."""
        return self.state == "pending" and self.since is not None and now - self.since < PENDING_FRESH_SECONDS


class ProvisionStore:
    """Redis: ``<prefix>:u:<telegram id>`` (the person's state), ``<prefix>:c:<chat id>`` (who a created group belongs to),
    ``<prefix>:day:<date>`` (groups created today) and ``<prefix>:lock`` (one Telethon connection at a time). Everything has a TTL."""

    def __init__(self, redis: Any, *, prefix: str = "civ:prov") -> None:
        self._redis = redis
        self._prefix = prefix

    @classmethod
    def from_settings(cls, settings: Settings) -> ProvisionStore:
        from redis.asyncio import Redis

        timeout = settings.reminders_redis_timeout_seconds
        client = Redis.from_url(
            settings.reminders_redis_url or settings.celery_broker_url, socket_timeout=timeout, socket_connect_timeout=timeout, decode_responses=True
        )
        return cls(client, prefix=settings.provision_key_prefix)

    def _key(self, *parts: object) -> str:
        return ":".join((self._prefix, *(str(p) for p in parts)))

    async def aclose(self) -> None:
        close = getattr(self._redis, "aclose", None)
        if close is not None:
            await close()

    # ---- the person
    async def user(self, telegram_id: int) -> UserState | None:
        raw = await self._redis.get(self._key("u", telegram_id))
        if not raw:
            return None
        data = json.loads(raw)
        return UserState(data["state"], data.get("chat_id"), data.get("link"), data.get("since"))

    async def claim_user(self, telegram_id: int) -> bool:
        """True = this caller may start provisioning (nobody holds a live claim, nothing is done or recently failed)."""
        return bool(await self._redis.set(self._key("u", telegram_id), json.dumps({"state": "pending", "since": time.time()}), nx=True, ex=PENDING_SECONDS))

    async def extend_claim(self, telegram_id: int, seconds: int) -> None:
        await self._redis.set(self._key("u", telegram_id), json.dumps({"state": "pending", "since": time.time()}), ex=seconds)

    async def finish_user(self, telegram_id: int, chat_id: int, link: str | None) -> None:
        await self._redis.set(self._key("u", telegram_id), json.dumps({"state": "done", "chat_id": chat_id, "link": link}), ex=DONE_SECONDS)

    async def fail_user(self, telegram_id: int) -> None:
        await self._redis.set(self._key("u", telegram_id), json.dumps({"state": "failed"}), ex=FAILED_SECONDS)

    async def release_user(self, telegram_id: int) -> None:
        await self._redis.delete(self._key("u", telegram_id))

    # ---- the group
    async def bind_chat(self, chat_id: int, binding: ChatBinding) -> None:
        payload = {"owner": binding.owner, "userbot": binding.userbot, "settings": binding.settings_thread, "feedback": binding.feedback_thread}
        await self._redis.set(self._key("c", chat_id), json.dumps(payload), ex=DONE_SECONDS)

    async def chat(self, chat_id: int) -> ChatBinding | None:
        raw = await self._redis.get(self._key("c", chat_id))
        if not raw:
            return None
        data = json.loads(raw)
        return ChatBinding(int(data["owner"]), int(data["userbot"]), data.get("settings"), data.get("feedback"))

    async def unbind_chat(self, chat_id: int) -> None:
        await self._redis.delete(self._key("c", chat_id))

    async def welcome_once(self, chat_id: int) -> bool:
        """True for the FIRST caller per group: the owner's promotion + greeting is done once, however many join events (service message, chat_member) arrive."""
        return bool(await self._redis.set(self._key("w", chat_id), "1", nx=True, ex=WELCOME_SECONDS))

    async def welcome_undo(self, chat_id: int) -> None:
        await self._redis.delete(self._key("w", chat_id))

    # ---- limits
    def _day_key(self) -> str:
        return self._key("day", datetime.now(UTC).strftime("%Y%m%d"))

    async def created_today(self) -> int:
        return int(await self._redis.get(self._day_key()) or 0)

    async def count_creation(self) -> None:
        key = self._day_key()
        await self._redis.incr(key)
        await self._redis.expire(key, 2 * 86_400)

    # ---- the single Telethon connection
    async def acquire_lock(self, seconds: int) -> bool:
        return bool(await self._redis.set(self._key("lock"), "1", nx=True, ex=seconds))

    async def release_lock(self) -> None:
        await self._redis.delete(self._key("lock"))


def group_title(settings: Settings, display_name: str | None) -> str:
    name = invites.clean_display_name(display_name) or "Друг"
    return f"{settings.userbot_title_prefix}{name}"[:MAX_TITLE]


@dataclass(frozen=True)
class Outcome:
    chat_id: int
    title: str
    joined: bool  # the person is in the group (False: they were sent a one-use link)
    link: str | None


async def provision(
    api: UserbotApi,
    store: ProvisionStore,
    settings: Settings,
    *,
    owner_id: int,
    display_name: str | None,
    username: str | None,
) -> Outcome:
    """The userbot's part. Raises ``RetryLater`` (nothing exists yet) or ``UserbotError`` (anything else; the half-made group is deleted first)."""
    title = group_title(settings, display_name)
    group = await api.create_forum_group(title)  # a FloodWait here is retried by Celery: nothing was made yet
    try:
        await store.bind_chat(group.chat_id, ChatBinding(owner_id, api.userbot_id))  # BEFORE the bot is added: its first events must find the owner
        await store.count_creation()
        await api.add_bot_as_admin(group, settings.telegram_bot_username)  # every right, incl. appointing admins: the bot stays in charge after we leave
        link: str | None = None
        joined = False
        try:
            member = await api.add_user(group, owner_id, username)
            joined = True
            try:
                await api.make_admin(group, member)
            except UserbotError:  # the bot promotes the owner as soon as it sees them in the group
                log.warning("userbot: could not make user %s an administrator of chat %s", owner_id, group.chat_id)
        except UserNotReachable as exc:
            log.info("userbot: user %s cannot be added to chat %s (%s): a one-use link instead", owner_id, group.chat_id, exc)
            link = await api.invite_link(group, days=settings.userbot_link_ttl_days)
        await api.leave(group)
    except BaseException:
        await _discard(api, store, group)
        raise
    return Outcome(group.chat_id, title, joined, link)


async def _discard(api: UserbotApi, store: ProvisionStore, group: GroupRef) -> None:
    """Best effort: the group the userbot just made and could not finish must not be left lying around (it is the creator, so it may delete it)."""
    try:
        await store.unbind_chat(group.chat_id)
    except Exception:  # noqa: BLE001
        log.exception("userbot: could not drop the binding of chat %s", group.chat_id)
    try:
        await api.delete(group)
    except Exception:  # noqa: BLE001
        log.exception("userbot: could not delete the half-made group %s", group.chat_id)


ApiFactory = Callable[[Settings], Any]  # an async context manager yielding a ``UserbotApi``


async def provision_group_async(
    settings: Settings,
    bot: Bot,
    store: ProvisionStore,
    api_factory: ApiFactory,
    *,
    owner_id: int,
    display_name: str | None,
    username: str | None,
) -> Outcome | None:
    """The whole Celery task: lock -> connect -> ``provision`` -> tell the person. Returns None when the person was sent the manual instructions.
    ``RetryLater`` propagates (Celery retries it, bounded); every other failure ends in the manual instructions."""
    if not await store.acquire_lock(settings.userbot_lock_seconds):
        raise RetryLater(30)  # another provisioning holds the only connection
    try:
        if await store.created_today() >= settings.userbot_max_groups_per_day:
            log.warning("userbot: the daily limit of %s groups is reached; user %s gets the manual instructions", settings.userbot_max_groups_per_day, owner_id)
            raise UserbotError("daily limit of created groups reached")
        async with api_factory(settings) as api:
            outcome = await provision(api, store, settings, owner_id=owner_id, display_name=display_name, username=username)
    except RetryLater:
        await store.extend_claim(owner_id, PENDING_SECONDS)
        raise
    except Exception as exc:  # noqa: BLE001 - whatever it was, the person must not be left waiting
        log.exception("userbot: provisioning for user %s failed", owner_id)
        await store.fail_user(owner_id)
        await _tell(bot, owner_id, texts.PROVISION_FAILED, texts.create_persona_markup(settings.telegram_bot_username))
        await _alert_crash(bot, settings, exc, owner_id)
        return None
    finally:
        await store.release_lock()
    await store.finish_user(owner_id, outcome.chat_id, outcome.link)
    if outcome.joined:
        await _tell(bot, owner_id, texts.PROVISION_ADDED.format(title=outcome.title), None)
    else:
        assert outcome.link is not None
        await _tell(
            bot, owner_id, texts.PROVISION_LINK.format(title=outcome.title, days=settings.userbot_link_ttl_days, link=outcome.link), texts.link_markup(outcome.link)
        )
    return outcome


async def _tell(bot: Bot, chat_id: int, text: str, markup: Any) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, disable_web_page_preview=True)
    except Exception:  # noqa: BLE001 - the person closed the chat with the bot, or Telegram hiccuped: nothing else can be done here
        log.exception("userbot: could not write to user %s", chat_id)


async def _alert_crash(bot: Bot, settings: Settings, exc: BaseException, owner_id: int) -> None:
    """The admin gets crash_log_<time>.txt with the traceback; if that cannot be sent (or reports are off), the one-line alert as before."""
    from ai_civ.bot import error_handler  # late: error_handler -> app -> handlers -> provision

    context = [f"owner:     Telegram user {owner_id}"]
    if await error_handler.report_crash(exc, where="userbot:provision_group", settings=settings, bot=bot, context=context, force=True):
        return
    await _alert_admin(bot, settings, f"userbot: creating the group for Telegram user {owner_id} failed: {type(exc).__name__}: {exc}")


async def _alert_admin(bot: Bot, settings: Settings, text: str) -> None:
    try:
        await bot.send_message(chat_id=settings.feedback_admin_telegram_id, text=text)
    except Exception:  # noqa: BLE001
        log.exception("userbot: could not alert the admin")


Health = Literal["ok", "broken", "unknown"]


async def group_health(bot: Bot, chat_id: int, owner_id: int) -> Health:
    """Is the person's group usable? "broken" = it is gone, the bot cannot work in it (not an admin with the right to manage topics, kicked) or the owner is not an
    administrator of it (a plain member in a group made by a link, the situation this module exists to prevent). "unknown" = Telegram could not tell right now (network):
    nobody is moved to a new group on a guess."""
    try:
        chat = await bot.get_chat(chat_id)
        if chat.type != ChatType.SUPERGROUP or chat.is_forum is not True:
            return "broken"
        me = await bot.get_chat_member(chat_id, bot.id)
        if not (isinstance(me, ChatMemberAdministrator) and me.can_manage_topics):
            return "broken"
        owner = await bot.get_chat_member(chat_id, owner_id)
    except (TelegramBadRequest, TelegramForbiddenError):  # chat not found, the bot was removed, the user is not in the group at all
        return "broken"
    except Exception:  # noqa: BLE001 - a network blip, flood control...
        log.warning("group health of chat %s could not be read", chat_id, exc_info=True)
        return "unknown"
    return "ok" if owner.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR) else "broken"


class GroupProvisioner:
    """The bot side of ``/start``: claims the person and queues the task. ``enqueue(owner_id, name, username)`` is ``tasks.provision_group.delay`` in production.
    ``/start`` always means "make me a fresh group": what an earlier attempt left behind (a link nobody used, a half-working group, a failure mark) is dropped."""

    def __init__(self, store: ProvisionStore, settings: Settings, enqueue: Callable[[int, str | None, str | None], None]) -> None:
        self.store = store
        self.settings = settings
        self._enqueue = enqueue

    @staticmethod
    def is_plain_start(message: Message) -> bool:
        """``/start`` with no payload (``/start connect_...`` is an invitation: the handshake handles it)."""
        parts = (message.text or "").split()
        return bool(parts) and parts[0].split("@")[0].lower() == "/start" and len(parts) == 1

    async def on_start(self, message: Message) -> bool:
        """A person the bot does not know. True = handled (the person was answered). False = not applicable: the caller shows the manual instructions."""
        return await self._begin(message, rebuild=False)

    async def on_start_registered(self, message: Message, *, bot: Bot, chat_id: int | None, dormant: bool) -> bool:
        """A registered person writes ``/start`` in the private chat. True = their group is broken and a new one is being made. False = it works (or cannot be
        judged now): the caller gives the usual "write in the forum"."""
        if message.from_user is None:
            return False
        if chat_id is not None and not dormant and await group_health(bot, chat_id, message.from_user.id) != "broken":
            return False
        return await self._begin(message, rebuild=True)

    async def _begin(self, message: Message, *, rebuild: bool) -> bool:
        sender = message.from_user
        if sender is None or sender.is_bot:
            return False
        try:
            state = await self.store.user(sender.id)
            if state is not None:
                if state.working(time.time()):
                    await message.answer(texts.PROVISION_WAIT)
                    return True
                if state.state == "failed":
                    return False  # failed a moment ago: the manual instructions, then a fresh attempt on the next /start
                if state.chat_id is not None:
                    await self.store.unbind_chat(state.chat_id)  # the earlier group is no longer the person's: its events are nobody's business
                await self.store.release_user(sender.id)
            if await self.store.created_today() >= self.settings.userbot_max_groups_per_day:
                log.warning("userbot: the daily limit of groups is reached; %s gets the manual instructions", sender.id)
                return False
            if not await self.store.claim_user(sender.id):
                await message.answer(texts.PROVISION_WAIT)
                return True
            try:
                self._enqueue(sender.id, sender.first_name, sender.username)
            except Exception:  # noqa: BLE001 - the broker is down: free the person and fall back to the manual way
                log.exception("userbot: could not queue the group creation for %s", sender.id)
                await self.store.release_user(sender.id)
                return False
            await message.answer(texts.PROVISION_REBUILD if rebuild else texts.PROVISION_START)
            return True
        except Exception:  # noqa: BLE001 - Redis trouble must never cost the person the old way of registering
            log.exception("userbot: /start of %s could not be handled; falling back to the manual instructions", sender.id)
            return False


@asynccontextmanager
async def open_store(settings: Settings) -> AsyncIterator[ProvisionStore]:
    store = ProvisionStore.from_settings(settings)
    try:
        yield store
    finally:
        await store.aclose()


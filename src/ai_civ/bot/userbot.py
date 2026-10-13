"""The Telegram side of the userbot: ``TelethonApi`` implements ``provision.UserbotApi`` with Telethon (MTProto, a normal user account).

One connection at a time (the caller holds a Redis lock, and the Celery queue has one worker): Telegram revokes a StringSession that is used from
two places at once (``AuthKeyDuplicatedError``). Fail-fast like everything else here: no automatic retries, no silent FloodWait sleeping
(``flood_sleep_threshold=0``: a FloodWait becomes ``RetryLater`` and Celery decides), short timeouts.

NOT verified against live Telegram (no account in the build environment): every request below is the documented MTProto call as exposed by
Telethon 1.45 (checked for existence and signature, and exercised only through a fake in the tests).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_civ.bot.provision import GroupRef, RetryLater, UserbotError, UserNotReachable, UserRef
from ai_civ.config import ConfigError, Settings

log = logging.getLogger(__name__)

FORUM_SETTLE_SECONDS = 2.0  # the pause after switching Topics on (tests set it to 0)
SESSION_LENGTH = 353  # a Telethon StringSession of an IPv4 data centre: "1" + base64 of 263 bytes (352 characters, the last one is the padding "=")
SESSION_LENGTH_IPV6 = 369  # the same for an IPv6 data centre: base64 of 275 bytes
_SESSION_BYTES = {SESSION_LENGTH: 263, SESSION_LENGTH_IPV6: 275}
_BASE64 = re.compile(r"[A-Za-z0-9_-]+={0,2}")


class SessionMalformed(ConfigError):
    """TELEGRAM_USERBOT_SESSION is not a usable StringSession. Raised before Telethon is touched: it dies inside ``struct.unpack`` / ``b64decode`` on a broken one."""


def check_session(raw: str) -> str:
    """The session string, stripped (and with its lost closing "=" put back), or ``SessionMalformed`` naming only its LENGTH (the string is a secret and never goes into a message).

    The length alone is not enough: the final "=" is the base64 padding and selecting the string by double click drops it (352 characters instead of 353), after which
    Telethon fails with "Incorrect padding". The padding is fully determined by the length, so it is restored; everything else must be exactly what ``StringSession.save`` writes."""
    session = raw.strip()
    length = len(session)
    if length in (SESSION_LENGTH - 1, SESSION_LENGTH_IPV6 - 1) and not session.endswith("="):
        session += "="
    expected = _SESSION_BYTES.get(len(session))
    if expected is None or session[0] != "1" or not _BASE64.fullmatch(session[1:]):
        raise SessionMalformed(f"Сессия юзербота повреждена. Длина: {length}. Ожидается: {SESSION_LENGTH}")
    try:
        data = base64.urlsafe_b64decode(session[1:])
    except ValueError:
        data = b""
    if len(data) != expected:
        raise SessionMalformed(f"Сессия юзербота повреждена. Длина: {length}. Ожидается: {SESSION_LENGTH}")
    if len(session) != length:
        log.warning("TELEGRAM_USERBOT_SESSION lost its closing '=' (length %d); restored. Put the full string into .env", length)
    return session


_PROXY = re.compile(r"^(socks5h?|https?)://([^:/\s]+):(\d{1,5})$")


def telethon_proxy(url: str | None) -> tuple | None:
    """``socks5h://host:1080`` -> Telethon's proxy tuple (names are resolved by the proxy). None = direct. The same proxy as the Bot API's (TELEGRAM_PROXY)."""
    if not url:
        return None
    match = _PROXY.match(url)
    if match is None:
        raise ConfigError(f"unsupported proxy {url!r}")
    scheme, host, port = match.group(1), match.group(2), int(match.group(3))
    if scheme.startswith("socks5"):
        return ("socks5", host, port, True)  # True = resolve names remotely
    return ("http", host, port)


# What the person's own group needs: everything an administrator can do, NOT ``anonymous``. The BOT gets ``add_admins`` too (Bot API ``can_promote_members``): it is the one that
# stays in the group after the userbot leaves, and it must be able to make the owner an administrator the moment the owner joins by the invite link.
USER_RIGHTS = dict(
    change_info=True, delete_messages=True, ban_users=True, invite_users=True, pin_messages=True, manage_topics=True, manage_call=True, post_messages=True, edit_messages=True,
    add_admins=True,
)  # fmt: skip
BOT_RIGHTS = dict(USER_RIGHTS)


class TelethonApi:
    def __init__(self, client: Any, userbot_id: int, tl: Any) -> None:
        self._client = client
        self.userbot_id = userbot_id
        self._tl = tl  # the module bundle (functions / types / errors), injected so that the import stays lazy and tests can look at the calls

    @classmethod
    @asynccontextmanager
    async def connect(cls, settings: Settings) -> AsyncIterator[TelethonApi]:
        if not settings.userbot_ready:
            raise ConfigError("the userbot is not configured (TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_USERBOT_SESSION)")
        assert settings.telegram_api_id and settings.telegram_api_hash and settings.telegram_userbot_session
        session = check_session(settings.telegram_userbot_session.get_secret_value())  # BEFORE Telethon is even imported: a wrong length dies inside struct.unpack
        from telethon import TelegramClient, errors, functions, types, utils
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(session),
            settings.telegram_api_id,
            settings.telegram_api_hash.get_secret_value(),
            proxy=telethon_proxy(settings.telegram_proxy),
            connection_retries=1,
            request_retries=1,
            retry_delay=1,
            timeout=15,
            flood_sleep_threshold=0,  # a FloodWait must reach us as an error, not hold the worker asleep
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise UserbotError("the userbot session is not authorised (generate a new one: python -m ai_civ.cli.userbot_login)")
            me = await client.get_me()

            class Tl:  # the pieces of Telethon this adapter uses, in one place
                pass

            tl = Tl()
            tl.functions, tl.types, tl.errors, tl.utils = functions, types, errors, utils
            yield cls(client, me.id, tl)
        finally:
            await client.disconnect()

    # ---------------------------------------------------------------- calls

    async def _call(self, request: Any) -> Any:
        """One MTProto request; Telegram's refusals become our small set of errors."""
        errors = self._tl.errors
        try:
            return await self._client(request)
        except errors.FloodWaitError as exc:
            raise RetryLater(int(exc.seconds)) from exc
        except errors.AuthKeyDuplicatedError as exc:
            raise UserbotError("the userbot session is in use somewhere else and was revoked by Telegram: generate a new one") from exc
        except (errors.ChannelsTooMuchError,) as exc:
            raise UserbotError("the userbot account is in / has created too many groups") from exc

    async def create_forum_group(self, title: str) -> GroupRef:
        t = self._tl
        result = await self._call(t.functions.channels.CreateChannelRequest(title=title, about="", megagroup=True))
        channel = result.chats[0]
        await self._call(t.functions.channels.ToggleForumRequest(channel=channel, enabled=True, tabs=False))
        await asyncio.sleep(FORUM_SETTLE_SECONDS)  # Telegram needs a moment to apply the forum mode before the bot is given its rights in it
        return GroupRef(chat_id=t.utils.get_peer_id(channel), handle=channel)

    async def add_bot_as_admin(self, group: GroupRef, bot_username: str) -> None:
        t = self._tl
        bot = await self._client.get_input_entity(bot_username.lstrip("@"))
        await self._call(t.functions.channels.InviteToChannelRequest(group.handle, [bot]))
        await self._call(t.functions.channels.EditAdminRequest(group.handle, bot, t.types.ChatAdminRights(**BOT_RIGHTS), rank=""))

    async def add_user(self, group: GroupRef, user_id: int, username: str | None) -> UserRef:
        t = self._tl
        errors = t.errors
        # a user account can address a stranger only by @username (a bare numeric id is unknown to it): without one the link is the way
        if not username:
            raise UserNotReachable("the person has no @username, the userbot cannot find them by id")
        try:
            target = await self._client.get_input_entity(username.lstrip("@"))
        except (ValueError, errors.UsernameNotOccupiedError, errors.UserIdInvalidError) as exc:
            raise UserNotReachable(f"cannot resolve @{username}") from exc
        try:
            result = await self._call(t.functions.channels.InviteToChannelRequest(group.handle, [target]))
        except (
            errors.UserPrivacyRestrictedError,
            errors.UserNotMutualContactError,
            errors.UserChannelsTooMuchError,
            errors.UsersTooMuchError,
            errors.PeerFloodError,
            errors.UserIdInvalidError,
        ) as exc:
            raise UserNotReachable(type(exc).__name__) from exc
        if getattr(result, "missing_invitees", None):  # newer layers report a refused invitee here instead of raising
            raise UserNotReachable("Telegram reports the person as a missing invitee (privacy settings)")
        return UserRef(target)

    async def make_admin(self, group: GroupRef, user: UserRef) -> None:
        t = self._tl
        try:
            await self._call(t.functions.channels.EditAdminRequest(group.handle, user.handle, t.types.ChatAdminRights(**USER_RIGHTS), rank=""))
        except RetryLater:
            raise
        except Exception as exc:  # noqa: BLE001 - an administrator is a nicety, never a reason to fail the group
            raise UserbotError(f"could not promote the person: {exc!r}") from exc

    async def invite_link(self, group: GroupRef, *, days: int) -> str:
        t = self._tl
        result = await self._call(
            t.functions.messages.ExportChatInviteRequest(
                peer=group.handle, usage_limit=1, expire_date=datetime.now(UTC) + timedelta(days=days), title="AI Persona"
            )
        )
        link = getattr(result, "link", None)
        if not link:
            raise UserbotError("Telegram returned no invite link")
        return link

    async def leave(self, group: GroupRef) -> None:
        await self._call(self._tl.functions.channels.LeaveChannelRequest(group.handle))

    async def delete(self, group: GroupRef) -> None:
        await self._call(self._tl.functions.channels.DeleteChannelRequest(group.handle))

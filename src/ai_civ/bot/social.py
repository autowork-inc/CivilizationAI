"""The "digital handshake" in Telegram (Module 5, step 2).

  A: ``/connect`` in their group      -> a one-time link  ``t.me/<bot>?start=connect_<token>``  (A's consent = creating the link)
  B: opens the link -> ``/start connect_<token>`` in a PRIVATE chat with the bot
     -> "A wants to connect. Accept?"  [Accept] [Decline]          (B is shown the name A chose, and the name B will be shown as)
  B: [Accept] -> ``civ.accept_invite`` (both contact rows in one transaction, pair lock, single-use invite, a notification for A)
  A: the notification worker tells A, in the Settings topic, that B is now in their contacts. Nothing is visible to B yet:
     access is default-deny, grants are a separate decision of A.

Everything that matters is decided in PostgreSQL (``migrations/sql/0005``): who the caller is (the tenant session), whether the invite is
still valid, races between double clicks and crossing invites. This module only talks to the user. The confirmation lives in the
private chat because that is where a deep link lands; the router is registered BEFORE the orchestrator router and uses INNER
middleware, so updates it does not claim (an ordinary ``/start``, group messages) fall through untouched.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, TelegramObject

from ai_civ.bot import invites, texts
from ai_civ.bot.invites import CreateOutcome, HandshakeOutcome, InviteState
from ai_civ.bot.orchestrator import TenantInfo, load_tenant
from ai_civ.config import Settings
from ai_civ.db import Database

log = logging.getLogger(__name__)

CALLBACK_PREFIX = "cn"
CALLBACK_RE = re.compile(rf"^{CALLBACK_PREFIX}:([0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-[0-9a-f]{{12}}):([yn])$")


def decision_keyboard(invite_id: uuid.UUID) -> InlineKeyboardMarkup:
    """``cn:<invite uuid>:y|n`` = 3 + 36 + 2 characters (limit: 64 bytes). The id is not the secret: the database re-checks everything."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=texts.CONNECT_ACCEPT, callback_data=f"{CALLBACK_PREFIX}:{invite_id}:y"),
                InlineKeyboardButton(text=texts.CONNECT_DECLINE, callback_data=f"{CALLBACK_PREFIX}:{invite_id}:n"),
            ]
        ]
    )


class SocialService:
    def __init__(self, db: Database, bot: Bot, settings: Settings) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings

    # ------------------------------------------------------------ A: make a link (in the group)

    async def send_invite_link(self, tenant: TenantInfo, thread_id: int | None) -> None:
        """``/connect``: create a one-time link and send it where the command was written. The name B will see is A's own
        (``address_as`` / ``name`` from the Core Profile), shown in the message so A knows exactly what is being published."""
        token = invites.new_token()
        ttl = timedelta(hours=self.settings.invite_ttl_hours)
        async with self.db.tenant_session(tenant.id) as session:
            name = await invites.my_display_name(session)
            created = await invites.create_invite(session, token_hash=invites.hash_token(token), display_name=name, ttl=ttl)
        chat_id = tenant.forum_chat_id
        if chat_id is None:
            raise RuntimeError(f"tenant {tenant.id} has no forum group")
        if created.outcome is CreateOutcome.created:
            link = invites.deep_link(self.settings.telegram_bot_username, token)
            text = texts.CONNECT_LINK.format(link=link, hours=self.settings.invite_ttl_hours, name=name)
        elif created.outcome is CreateOutcome.too_many:
            text = texts.CONNECT_TOO_MANY
        else:
            text = texts.CONNECT_NOT_READY
        await self.bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread_id)

    # ------------------------------------------------------------ B: open the link (private chat)

    async def on_connect_link(self, message: Message, tenant: TenantInfo, payload: str | None) -> None:
        token = invites.token_from_payload(payload)
        if token is None:  # a mangled link: indistinguishable, for the user, from a dead one
            await message.answer(texts.CONNECT_INVALID)
            return
        fallback = message.from_user.first_name if message.from_user else None
        async with self.db.tenant_session(tenant.id) as session:
            peeked = await invites.peek_invite(session, invites.hash_token(token))
            my_name = await invites.my_display_name(session, fallback)
        inviter = peeked.inviter_name or texts.DEFAULT_CONTACT_NAME
        if peeked.state is InviteState.ok and peeked.invite_id is not None:
            await message.answer(
                texts.CONNECT_ASK.format(inviter=inviter, me=my_name), reply_markup=decision_keyboard(peeked.invite_id)
            )
        elif peeked.state is InviteState.already_connected:
            await message.answer(texts.CONNECT_ALREADY.format(inviter=inviter))
        elif peeked.state is InviteState.own:
            await message.answer(texts.CONNECT_OWN)
        elif peeked.state is InviteState.not_ready:
            await message.answer(texts.CONNECT_NOT_READY_DM)
        else:
            await message.answer(texts.CONNECT_INVALID)

    async def on_decision(self, query: CallbackQuery, tenant: TenantInfo, invite_id: uuid.UUID, accept: bool) -> None:
        """[Accept] / [Decline]. A double click is harmless: the second ``accept_invite`` finds the pair already connected."""
        if not accept:  # nothing is written, and A is not told who said no; the link stays usable
            await self._finish(query, texts.CONNECT_DECLINED)
            return
        async with self.db.tenant_session(tenant.id) as session:
            my_name = await invites.my_display_name(session, query.from_user.first_name)
            result = await invites.accept_invite(session, invite_id, my_name)
        inviter = result.inviter_name or texts.DEFAULT_CONTACT_NAME
        if result.outcome is HandshakeOutcome.connected:
            text = texts.CONNECT_DONE.format(inviter=inviter)
        elif result.outcome is HandshakeOutcome.already_connected:
            text = texts.CONNECT_ALREADY.format(inviter=inviter)
        elif result.outcome is HandshakeOutcome.own:
            text = texts.CONNECT_OWN
        elif result.outcome is HandshakeOutcome.not_ready:
            text = texts.CONNECT_NOT_READY_DM
        else:
            text = texts.CONNECT_INVALID
        await self._finish(query, text)

    async def _finish(self, query: CallbackQuery, text: str) -> None:
        """Replace the question with the result (buttons gone) and stop the spinner. Best effort: the decision is already made."""
        try:
            await query.answer()
            if query.message is not None:
                await self.bot.edit_message_text(chat_id=query.message.chat.id, message_id=query.message.message_id, text=text)
        except TelegramBadRequest:  # "message is not modified": the second click of a double click
            log.info("connect: the result message was already up to date")


class PrivateTenantMiddleware(BaseMiddleware):
    """INNER middleware of the social router (runs only for updates the router's filters claimed): the sender must be a registered
    tenant, and the chat must be their private chat with the bot. Puts ``tenant`` into the handler data."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        assert isinstance(event, Message | CallbackQuery)
        sender = event.from_user
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        if sender is None or sender.is_bot or chat is None or chat.type != ChatType.PRIVATE or chat.id != sender.id:
            return None
        tenant_id = await self.db.resolve_tenant_id(telegram_user_id=sender.id)
        if tenant_id is None:
            log.info("connect: unregistered telegram user %s opened an invite or pressed a button", sender.id)
            try:
                if isinstance(event, Message):
                    await event.answer(texts.CONNECT_NEED_ACCOUNT, reply_markup=texts.create_persona_markup(data["orchestrator"].settings.telegram_bot_username))
                else:
                    await event.answer(texts.CONNECT_NEED_ACCOUNT, show_alert=True)
            except Exception:  # noqa: BLE001 - a hint is a courtesy
                log.exception("could not answer unregistered user %s", sender.id)
            return None
        tenant = await load_tenant(self.db, tenant_id)
        if tenant is None:
            return None
        data["tenant"] = tenant
        return await handler(event, data)


def build_router(db: Database) -> Router:
    router = Router(name="social")
    middleware = PrivateTenantMiddleware(db)
    router.message.middleware(middleware)  # inner: after the filters below matched
    router.callback_query.middleware(middleware)

    @router.message(CommandStart(deep_link=True, magic=F.args.startswith(invites.PAYLOAD_PREFIX)), F.chat.type == ChatType.PRIVATE)
    async def on_connect_start(message: Message, command: CommandObject, tenant: TenantInfo, social: SocialService) -> None:
        try:
            await social.on_connect_link(message, tenant, command.args)
        except Exception:  # noqa: BLE001 - nothing was changed (peek is read-only): the user can open the link again
            log.exception("connect: could not show the invite to tenant %s", tenant.id)
            await _tell_failure(message)

    @router.callback_query(F.data.regexp(CALLBACK_RE.pattern).as_("match"))
    async def on_decision(query: CallbackQuery, tenant: TenantInfo, social: SocialService, match: re.Match[str]) -> None:
        try:
            await social.on_decision(query, tenant, uuid.UUID(match.group(1)), match.group(2) == "y")
        except Exception:  # noqa: BLE001 - one transaction: nothing changed, the buttons stay and can be pressed again
            log.exception("connect: the decision of tenant %s failed", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    return router


async def _tell_failure(message: Message) -> None:
    try:
        await message.answer(texts.FAILURE)
    except Exception:  # noqa: BLE001
        log.exception("could not deliver the failure notice")

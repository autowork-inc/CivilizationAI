"""aiogram layer: tenant resolution (middleware) and the handlers. All behaviour lives in the Orchestrator.

Tenant resolution, for EVERY message: ``db.resolve_tenant_id(telegram_user_id=<sender>)``. Then the chat must be that
tenant's registered forum group (``users.forum_chat_id``): one group = one user, so the same person writing from some
other group is not served there. Unknown senders are not served: in a private chat they get the instruction for getting
started (create a group, add the bot), in a group the message may be the moment their registration becomes possible
(``registration.Registrar``). A tenant whose group is frozen (a stranger joined) or dormant (the bot was removed) reaches
no handler at all.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, ErrorEvent, InlineKeyboardMarkup, Message, TelegramObject

from ai_civ.bot import error_handler, proposals, quick_events, registration, reminders as reminders_module, review, social, texts
from ai_civ.bot.attachments import Attachment
from ai_civ.bot.orchestrator import Incoming, Orchestrator, TenantInfo, load_tenant
from ai_civ.bot.proposals import ProposalService
from ai_civ.bot.ratelimit import RateLimiter, RateLimitMiddleware
from ai_civ.bot.quick_events import QuickEvents
from ai_civ.bot.registration import GROUP_TYPES, Registrar, unfrozen
from ai_civ.bot.review import ReviewService
from ai_civ.bot.social import SocialService
from ai_civ.bot.tools import cards as cards_tool
from ai_civ.bot.tools import capsule as capsule_tool, decompose, delete_topic as delete_topic_tool, geo as geo_tool, inventory as inventory_tool, recipes as recipes_tool, save_fact
from ai_civ.bot.tools.cards import Cards
from ai_civ.bot.reminders import Reminders
from ai_civ.bot.tools.decompose import Tasks
from ai_civ.bot.tools.delete_topic import DeleteTopics
from ai_civ.bot.tools.capsule import TimeCapsules
from ai_civ.bot.tools.geo import Geo
from ai_civ.bot.tools.inventory import Inventory
from ai_civ.bot.tools.recipes import Recipes
from ai_civ.bot.tools.save_fact import SaveFacts
from ai_civ.bot.provision import GroupProvisioner, ProvisionStore
from ai_civ.bot.voice import VoiceClip
from ai_civ.db import Database
from ai_civ.db.enums import GroupState, OnboardingState, PersonaMode

log = logging.getLogger(__name__)


def thread_of(message: Message) -> int | None:
    """The forum topic of a message; None = the "General" topic.

    ``message_thread_id`` is only a topic id when ``is_topic_message`` is true: in the General topic (and for plain reply
    chains) Telegram may still fill it with the id of a replied-to message. aiogram's own helpers use the same rule.
    """
    return message.message_thread_id if message.is_topic_message else None


class TenantMiddleware(BaseMiddleware):
    """Outer middleware of ``router.message`` and ``router.callback_query``: puts ``tenant`` (TenantInfo) into the handler
    data or stops the update. A button press is attributed exactly like a message: by the person who pressed it, and only
    inside that person's own forum group (the chat of the message the button belongs to)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        assert isinstance(event, Message | CallbackQuery)
        sender = event.from_user
        if sender is None or sender.is_bot:  # channel posts, anonymous admins, other bots
            return None
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        if chat is None:  # a button of a message that is no longer available: nothing to attribute it to
            log.info("ignored a callback query of telegram user %s without an accessible message", sender.id)
            return None
        registrar: Registrar = data["registrar"]
        tenant_id = await self.db.resolve_tenant_id(telegram_user_id=sender.id)
        if tenant_id is None:
            log.info("ignored an update of unregistered telegram user %s in chat %s", sender.id, chat.id)
            if isinstance(event, Message):
                provisioner = data.get("provisioner")
                if chat.type == ChatType.PRIVATE and provisioner is not None and provisioner.is_plain_start(event):
                    if await provisioner.on_start(event):  # the userbot makes the group; False = it cannot: the manual way below
                        return None
                if chat.type == ChatType.PRIVATE:  # how to get started (also the answer to /start)
                    await self._hint(data["bot"], chat.id, texts.UNREGISTERED, markup=texts.create_persona_markup(data["orchestrator"].settings.telegram_bot_username))
                elif chat.type in GROUP_TYPES:  # the bot was added to a group: maybe this person is about to register it
                    await registrar.on_group_message(event)
            return None
        tenant = await load_tenant(self.db, tenant_id)
        if tenant is None or tenant.forum_chat_id != chat.id:
            log.warning(
                "tenant %s wrote in chat %s, which is not its forum group (%s); ignored",
                tenant_id, chat.id, None if tenant is None else tenant.forum_chat_id,
            )  # fmt: skip
            if isinstance(event, Message):
                if chat.type == ChatType.PRIVATE:
                    provisioner = data.get("provisioner")
                    if tenant is not None and provisioner is not None and provisioner.is_plain_start(event):
                        # /start of a registered person: when their group is broken (gone, the bot cannot work in it, they are no administrator there) a NEW one is made
                        if await provisioner.on_start_registered(
                            event, bot=data["bot"], chat_id=tenant.forum_chat_id, dormant=tenant.group_state is GroupState.dormant
                        ):
                            return None
                    await self._hint(data["bot"], chat.id, texts.PRIVATE_CHAT)
                elif chat.type in GROUP_TYPES:  # a registered person in another group: registration explains what is possible
                    await registrar.on_group_message(event)
            return None
        if tenant.group_state is not GroupState.active:
            tenant = await self._gate(event, tenant, registrar, data["bot"])
            if tenant is None:
                return None
        data["tenant"] = tenant
        return await handler(event, data)

    @staticmethod
    async def _gate(event: Message | CallbackQuery, tenant: TenantInfo, registrar: Registrar, bot: Bot) -> TenantInfo | None:
        """The tenant's group is not active. Frozen (a stranger is in it): serve the owner only once the group is private again,
        otherwise say why the bot is silent. Dormant: the bot is evidently back (it just got this update) but was never
        re-registered - run the registration check. Either way nothing reaches the orchestrator."""
        if tenant.group_state is GroupState.frozen:
            if await registrar.try_unfreeze(tenant):
                return unfrozen(tenant)
            log.info("tenant %s: update ignored, the group is frozen", tenant.id)
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer(texts.FROZEN, show_alert=True)
                else:
                    await bot.send_message(chat_id=event.chat.id, text=texts.FROZEN, message_thread_id=thread_of(event))
            except Exception:  # noqa: BLE001 - a warning is a courtesy; never let it break the update
                log.exception("could not warn tenant %s that the group is frozen", tenant.id)
            return None
        log.info("tenant %s: update ignored, the group is %s", tenant.id, tenant.group_state)
        if isinstance(event, Message):
            await registrar.on_group_message(event)
        return None

    @staticmethod
    async def _hint(bot: Bot, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        try:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        except Exception:  # noqa: BLE001 - a hint is a courtesy; never let it break the update
            log.exception("could not send a hint to chat %s", chat_id)


def build_router(db: Database, limiter: RateLimiter | None = None) -> Router:
    router = Router(name="orchestrator")
    middleware = TenantMiddleware(db)
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)
    if limiter is not None:  # INNER: after the tenant is known and the group is active; only messages that reach a handler are counted
        router.message.middleware(RateLimitMiddleware(limiter))

    @router.message(Command("setup"))
    async def on_setup(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> None:
        await orchestrator.setup_defaults(tenant, thread_of(message))

    @router.message(Command("app"))
    async def on_app(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> None:
        await orchestrator.open_app(tenant, thread_of(message))

    @router.message(Command("feedback"))
    async def on_feedback(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> None:
        await orchestrator.open_feedback(tenant, thread_of(message))

    @router.message(Command("dansk"))
    async def on_dansk(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> None:
        await orchestrator.set_mode(tenant, thread_of(message), PersonaMode.immersion_da)

    @router.message(Command("normal"))
    async def on_normal(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> None:
        await orchestrator.set_mode(tenant, thread_of(message), PersonaMode.normal)

    @router.message(Command("connect"))
    async def on_connect(message: Message, tenant: TenantInfo, social: SocialService) -> None:
        try:
            await social.send_invite_link(tenant, thread_of(message))
        except Exception:  # noqa: BLE001 - nothing was published (one transaction); the user can ask again
            log.exception("could not create an invite link for tenant %s", tenant.id)
            try:
                await message.answer(texts.FAILURE)  # aiogram answers in the topic of the message by itself
            except Exception:  # noqa: BLE001
                log.exception("could not deliver the failure notice")

    async def geo_guard(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> bool:
        """The place features answer only when they are on and the interview is over; otherwise the user hears why (or nothing, when they are off)."""
        if not orchestrator.settings.geo_enabled:
            return False
        if tenant.onboarding_state is not OnboardingState.done:
            await message.answer(texts.GEO_LATER)
            return False
        return True

    async def geo_failed(message: Message, tenant: TenantInfo) -> None:
        log.exception("geo handler failed for tenant %s", tenant.id)
        try:
            await message.answer(texts.FAILURE)
        except Exception:  # noqa: BLE001
            log.exception("could not deliver the failure notice")

    @router.message(F.location)
    async def on_location(message: Message, tenant: TenantInfo, orchestrator: Orchestrator, geo: Geo) -> None:
        """A location (a point on the map or the current position): the places and notes of this tenant are checked, nothing is stored."""
        if not await geo_guard(message, tenant, orchestrator):
            return
        try:
            await geo.on_location(message, tenant)
        except Exception:  # noqa: BLE001 - nothing was changed except notified_at (one transaction); the user can send the location again
            await geo_failed(message, tenant)

    @router.message(Command("place"))
    async def on_place(message: Message, command: CommandObject, tenant: TenantInfo, orchestrator: Orchestrator, geo: Geo) -> None:
        if not await geo_guard(message, tenant, orchestrator):
            return
        try:
            await geo.on_place_command(message, tenant, command.args)
        except Exception:  # noqa: BLE001
            await geo_failed(message, tenant)

    @router.message(Command("geonote"))
    async def on_geonote(message: Message, command: CommandObject, tenant: TenantInfo, orchestrator: Orchestrator, geo: Geo) -> None:
        if not await geo_guard(message, tenant, orchestrator):
            return
        try:
            await geo.on_geonote_command(message, tenant, command.args)
        except Exception:  # noqa: BLE001
            await geo_failed(message, tenant)

    @router.message(F.forum_topic_edited)
    async def on_topic_edited(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> None:
        """Service message: somebody (the owner, for the bot's own edits are filtered out by the middleware) renamed a topic."""
        assert message.forum_topic_edited is not None
        await orchestrator.sync_topic_title(tenant, thread_of(message), message.forum_topic_edited.name)

    @router.message(F.text, ~F.text.startswith("/"))
    async def on_text(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> None:
        assert message.text is not None
        await orchestrator.handle_message(
            Incoming(
                tenant=tenant,
                thread_id=thread_of(message),
                telegram_message_id=message.message_id,
                text=message.text,
                username=message.from_user.username if message.from_user else None,
            )
        )

    @router.message(F.voice)
    async def on_voice(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> None:
        """A voice note: only the metadata is read here; the Orchestrator downloads, transcribes and then treats it as text."""
        assert message.voice is not None
        await orchestrator.handle_message(
            Incoming(
                tenant=tenant,
                thread_id=thread_of(message),
                telegram_message_id=message.message_id,
                text="",
                voice=VoiceClip(
                    file_id=message.voice.file_id,
                    duration=message.voice.duration,
                    size=message.voice.file_size,
                    mime_type=message.voice.mime_type,
                ),
                username=message.from_user.username if message.from_user else None,
            )
        )

    @router.message(F.photo)
    async def on_photo(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> None:
        """A photo: Telegram sends several sizes; the largest one within the limit is taken. Only metadata is read here."""
        assert message.photo is not None
        limit = orchestrator.settings.attachment_max_bytes
        fitting = [p for p in message.photo if p.file_size is None or p.file_size <= limit]
        best = max(fitting, key=lambda p: p.width * p.height) if fitting else min(message.photo, key=lambda p: p.file_size or 0)
        await orchestrator.handle_message(
            Incoming(
                tenant=tenant,
                thread_id=thread_of(message),
                telegram_message_id=message.message_id,
                text=message.caption or "",
                attachment=Attachment(best.file_id, "photo", "image/jpeg", best.file_size, None),
            )
        )

    @router.message(F.document)
    async def on_document(message: Message, tenant: TenantInfo, orchestrator: Orchestrator) -> None:
        """A document (a file sent "as a file"): the format and the size are judged on the metadata before anything is downloaded."""
        assert message.document is not None
        doc = message.document
        await orchestrator.handle_message(
            Incoming(
                tenant=tenant,
                thread_id=thread_of(message),
                telegram_message_id=message.message_id,
                text=message.caption or "",
                attachment=Attachment(doc.file_id, "document", doc.mime_type, doc.file_size, doc.file_name),
            )
        )

    @router.callback_query(F.data.startswith(f"{review.CALLBACK_PREFIX}:"))
    async def on_review_button(query: CallbackQuery, tenant: TenantInfo, review_service: ReviewService) -> None:
        try:
            await review_service.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed decision must be visible to the user (nothing was changed: one transaction)
            log.exception("review button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{proposals.CALLBACK_PREFIX}:"))
    async def on_proposal_button(query: CallbackQuery, tenant: TenantInfo, proposal_service: ProposalService) -> None:
        try:
            await proposal_service.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed answer must be visible (nothing was changed: one transaction); the buttons stay
            log.exception("invitation button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{quick_events.CALLBACK_PREFIX}:"))
    async def on_quick_event_button(query: CallbackQuery, tenant: TenantInfo, quick: QuickEvents) -> None:
        try:
            await quick.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed press must be visible (nothing was changed: one transaction); the buttons stay
            log.exception("quick-event button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{save_fact.CALLBACK_PREFIX}:"))
    async def on_memory_button(query: CallbackQuery, tenant: TenantInfo, facts: SaveFacts) -> None:
        try:
            await facts.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed press must be visible (nothing was changed: one transaction); the button stays
            log.exception("memory button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data == save_fact.ACK_CALLBACK)
    async def on_memory_ack(query: CallbackQuery, tenant: TenantInfo, facts: SaveFacts) -> None:
        try:
            await facts.on_ack(tenant, query)
        except Exception:  # noqa: BLE001 - cosmetic: a failed removal leaves the card where it was
            log.exception("memory card acknowledgement failed for tenant %s", tenant.id)
            try:
                await query.answer()
            except Exception:  # noqa: BLE001
                log.exception("could not answer the callback query")

    @router.callback_query(F.data.startswith(f"{decompose.CALLBACK_PREFIX}:"))
    async def on_task_button(query: CallbackQuery, tenant: TenantInfo, tasks: Tasks) -> None:
        try:
            await tasks.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed press must be visible (nothing was changed: one transaction); the buttons stay
            log.exception("task button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{cards_tool.CALLBACK_PREFIX}:"))
    async def on_card_button(query: CallbackQuery, tenant: TenantInfo, cards: Cards) -> None:
        try:
            await cards.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed answer must be visible (nothing was changed: one transaction); the buttons stay
            log.exception("card button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{recipes_tool.CALLBACK_PREFIX}:"))
    async def on_recipe_button(query: CallbackQuery, tenant: TenantInfo, recipes: Recipes) -> None:
        try:
            await recipes.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed press must be visible (nothing was changed: one transaction); the button stays
            log.exception("recipe button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{inventory_tool.CALLBACK_PREFIX}:"))
    async def on_inventory_button(query: CallbackQuery, tenant: TenantInfo, inventory: Inventory) -> None:
        try:
            await inventory.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed press must be visible (nothing was changed: one transaction); the button stays
            log.exception("inventory button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{geo_tool.NOTE_CALLBACK}:"))
    async def on_geo_note_button(query: CallbackQuery, tenant: TenantInfo, geo: Geo) -> None:
        try:
            await geo.on_note_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed press must be visible (nothing was changed: one transaction); the buttons stay
            log.exception("geo note button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{geo_tool.PLACE_CALLBACK}:"))
    async def on_geo_place_button(query: CallbackQuery, tenant: TenantInfo, geo: Geo) -> None:
        try:
            await geo.on_place_callback(tenant, query)
        except Exception:  # noqa: BLE001
            log.exception("geo place button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{delete_topic_tool.CALLBACK_PREFIX}:"))
    async def on_delete_topic_button(query: CallbackQuery, tenant: TenantInfo, delete_topics: DeleteTopics) -> None:
        try:
            await delete_topics.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed press must be visible (nothing was changed: Telegram is asked first, our record second); the buttons stay
            log.exception("topic-deletion button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{reminders_module.CALLBACK_PREFIX}:"))
    async def on_reminder_button(query: CallbackQuery, tenant: TenantInfo, reminders: Reminders) -> None:
        try:
            await reminders.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed press must be visible (nothing was changed); the buttons stay
            log.exception("reminder button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    @router.callback_query(F.data.startswith(f"{capsule_tool.CALLBACK_PREFIX}:"))
    async def on_capsule_button(query: CallbackQuery, tenant: TenantInfo, capsules: TimeCapsules) -> None:
        try:
            await capsules.on_callback(tenant, query)
        except Exception:  # noqa: BLE001 - a failed press must be visible (nothing was changed: one transaction); the button stays
            log.exception("capsule button failed for tenant %s", tenant.id)
            try:
                await query.answer(texts.FAILURE, show_alert=True)
            except Exception:  # noqa: BLE001
                log.exception("could not answer the failed callback query")

    return router


async def on_error(event: ErrorEvent, orchestrator: Orchestrator) -> bool:
    """Last line of defence: nothing that escapes a handler may stop the polling loop. The admin gets the traceback as a file."""
    log.error("unhandled error in update %s", event.update.update_id, exc_info=event.exception)
    error_handler.report_crash_soon(event.exception, where="bot:update", settings=orchestrator.settings, bot=orchestrator.bot)
    return True


def _queue_group_creation(owner_id: int, display_name: str | None, username: str | None) -> None:
    """Hand the group creation to the userbot worker (its own Celery queue). Imported late: ``ai_civ.tasks`` imports half of the bot."""
    from ai_civ.tasks import provision_group

    provision_group.delay(owner_id, display_name, username)


def build_dispatcher(db: Database, orchestrator: Orchestrator, *, limiter: RateLimiter | None | Literal["default"] = "default") -> Dispatcher:
    """``limiter``: "default" = built from the settings (``RATE_LIMIT_ENABLED``; None when switched off); tests pass their own, or None."""
    if limiter == "default":
        limiter = RateLimiter.from_settings(orchestrator.settings)
    review_service = ReviewService(db, orchestrator.bot, orchestrator.settings)
    provisions = ProvisionStore.from_settings(orchestrator.settings) if orchestrator.settings.userbot_ready else None
    registrar = Registrar(db, orchestrator.bot, orchestrator, orchestrator.settings, provisions)
    provisioner = None if provisions is None else GroupProvisioner(provisions, orchestrator.settings, _queue_group_creation)
    social_service = SocialService(db, orchestrator.bot, orchestrator.settings)
    proposal_service = ProposalService(db, orchestrator.bot)
    # injected into handlers (and the tenant middleware) by parameter name / key
    dp = Dispatcher(
        orchestrator=orchestrator,
        review_service=review_service,
        registrar=registrar,
        provisioner=provisioner,
        social=social_service,
        proposal_service=proposal_service,
        quick=orchestrator.quick,
        facts=orchestrator.facts,
        tasks=orchestrator.tasks,
        cards=orchestrator.cards,
        recipes=orchestrator.recipes,
        inventory=orchestrator.inventory,
        geo=orchestrator.geo,
        capsules=orchestrator.capsules,
        delete_topics=orchestrator.delete_topics,
        reminders=orchestrator.reminders,
    )
    dp.include_router(registration.build_router())  # first: member / group events never reach the tenant middleware
    dp.include_router(social.build_router(db))  # then the private-chat handshake (inner middleware: unclaimed updates fall through)
    dp.include_router(build_router(db, limiter))
    dp.errors.register(on_error)
    if provisions is not None:
        dp.shutdown.register(provisions.aclose)
    dp.shutdown.register(orchestrator.janitor.aclose)  # pending self-deletion timers are dropped (those cards stay)
    dp.shutdown.register(orchestrator.reminders.aclose)  # the reminders' Redis client (if it was ever opened) is closed with the dispatcher
    if limiter is not None:
        dp.shutdown.register(limiter.aclose)  # the Redis client is closed with the dispatcher
    return dp

"""Zero-SQL registration and the privacy guard of the tenant's group (Module 5, step 1).

A person registers by doing what ``texts.UNREGISTERED`` tells them: create a group, enable Topics, add the bot as an admin with the
"Manage topics" right. Nothing is stored while they do it: every relevant event (the bot's membership changed, or somebody wrote in a
group the bot does not know) runs the SAME checks again, so the steps may come in any order and a lost or repeated update changes
nothing. Registration happens only when ALL of these hold (each one is asked of Telegram, never taken from the update):

  * the chat is a supergroup with Topics enabled and has no public name (it is meant to be private);
  * the bot is an administrator with the right to manage topics;
  * the person who triggered the event is the CREATOR of the group - otherwise whoever added the bot to somebody else's group first
    would own it (``users.forum_chat_id`` is unique, so the real owner could not register it afterwards);
  * nobody but that person and the bot is in the group - the bot answers with what it remembers about the owner.

Then ``civ.register_tenant`` (SECURITY DEFINER, advisory-locked, idempotent) creates the tenant, and the Orchestrator opens the standard
onboarding interview in the new group. One person = one group: a second group is refused with an explanation.

The same guard keeps the group private AFTER registration: when somebody else joins, ``users.group_state`` becomes ``frozen`` - the bot
answers nothing in that group and sends nothing to it (proactive nudges, the weekly review) until the group is down to the owner and
the bot again. Removing the bot makes the tenant ``dormant``; adding it back to the same group reactivates it.

A group the USERBOT made for the person (``bot/provision.py``) has no human creator the bot could check, so it is recognised by a record the
userbot worker wrote (Redis, ``ProvisionStore``: chat -> the person it was made for). For such a group the "creator" rule is replaced by "the
person it was made for is IN the group", and the rest is as strict as ever: Topics, the bot is an admin with the right to manage topics, private, and
exactly two members - so the userbot must have LEFT first (a group with a second human account in it is not private). The checks run again on
every event, including the userbot leaving and the person joining, so no particular order is needed.

Failure model (CLAUDE.md "Fail-Fast"): every public method is a boundary - whatever raises inside is logged with a traceback and the
update is dropped. Nothing is retried in a loop; the person (or the next event) retries. Telegram problems fail CLOSED: no
registration, and a frozen group stays frozen.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Hashable
from dataclasses import dataclass, replace
from typing import Literal

from aiogram import Bot, F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import ChatMember, ChatMemberAdministrator, ChatMemberRestricted, ChatMemberUpdated, Message
from aiogram.types import User as TelegramUser
from sqlalchemy import func, select, update

from ai_civ.bot import texts
from ai_civ.bot import error_handler
from ai_civ.bot.personas import FEEDBACK_PERSONA, SETTINGS_PERSONA
from ai_civ.bot.provision import ChatBinding, ProvisionStore, group_health
from ai_civ.bot.orchestrator import Orchestrator, TenantInfo, load_tenant
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import GroupState, OnboardingState, RegistrationOutcome
from ai_civ.db.models import User
from ai_civ.db.tenancy import Registration

log = logging.getLogger(__name__)

GROUP_TYPES = frozenset({ChatType.GROUP, ChatType.SUPERGROUP})
MAX_PEOPLE = 2  # the owner and the bot: the only members a private group may have
# Anonymous admins write "as the group": the sender is this bot (id from the Bot API docs), so the message does not say who they are.
ANONYMOUS_ADMIN_BOT_ID = 1_087_968_824

Source = Literal["membership", "message"]


class Throttle:
    """Remember a key for ``ttl_seconds``: ``allow`` is True the first time and False until it expires. In-process and approximate
    (several bot processes each keep their own); bounded - when full, the oldest key is forgotten."""

    def __init__(self, ttl_seconds: float, *, max_entries: int = 20_000, clock=time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._until: dict[Hashable, float] = {}

    def allow(self, key: Hashable) -> bool:
        now = self._clock()
        until = self._until.get(key)
        if until is not None and now < until:
            return False
        self._until.pop(key, None)  # an expired entry is re-inserted at the end: the dict stays ordered by insertion time
        if len(self._until) >= self._max:
            self._until = {k: u for k, u in self._until.items() if u > now}
            while len(self._until) >= self._max:
                del self._until[next(iter(self._until))]
        self._until[key] = now + self._ttl
        return True

    def __len__(self) -> int:
        return len(self._until)


@dataclass(frozen=True)
class GroupCheck:
    """What Telegram says about a group and about the person who is trying to register it."""

    is_supergroup: bool
    is_forum: bool
    is_private: bool  # no public name
    actor_is_creator: bool
    bot_can_manage_topics: bool
    member_count: int  # everybody, the bot included

    @property
    def alone(self) -> bool:
        return self.member_count <= MAX_PEOPLE

    def problems(self) -> list[str]:
        """What is still missing, as the user-facing checklist items (empty = the group is ready)."""
        found: list[str] = []
        if not (self.is_supergroup and self.is_forum):
            found.append(texts.PROBLEM_NOT_FORUM)
        if not self.bot_can_manage_topics:
            found.append(texts.PROBLEM_NOT_ADMIN)
        if not self.is_private:
            found.append(texts.PROBLEM_NOT_PRIVATE)
        if not self.alone:
            found.append(texts.PROBLEM_NOT_ALONE)
        return found


async def inspect_group(bot: Bot, chat_id: int, actor_id: int) -> GroupCheck:
    """Four read-only Bot API calls. Any failure raises (the caller registers nobody on a guess)."""
    chat, actor, me, count = await asyncio.gather(
        bot.get_chat(chat_id),
        bot.get_chat_member(chat_id, actor_id),
        bot.get_chat_member(chat_id, bot.id),
        bot.get_chat_member_count(chat_id),
    )
    return GroupCheck(
        is_supergroup=chat.type == ChatType.SUPERGROUP,
        is_forum=chat.is_forum is True,
        is_private=not chat.username and not chat.active_usernames,
        actor_is_creator=actor.status == ChatMemberStatus.CREATOR,
        bot_can_manage_topics=isinstance(me, ChatMemberAdministrator) and bool(me.can_manage_topics),
        member_count=count,
    )


async def inspect_provisioned(bot: Bot, chat_id: int, binding: ChatBinding) -> tuple[GroupCheck, bool]:
    """Like ``inspect_group`` for a group the userbot made: ``actor_is_creator`` means "the person it was made for is a member". Also returns whether the
    userbot is still in the group. A person who never joined may make Telegram answer "not found": that is just "not a member"."""
    chat, me, count = await asyncio.gather(bot.get_chat(chat_id), bot.get_chat_member(chat_id, bot.id), bot.get_chat_member_count(chat_id))

    async def member_of(user_id: int) -> bool:
        try:
            return is_member(await bot.get_chat_member(chat_id, user_id))
        except TelegramBadRequest:
            return False

    owner_in, userbot_in = await asyncio.gather(member_of(binding.owner), member_of(binding.userbot))
    check = GroupCheck(
        is_supergroup=chat.type == ChatType.SUPERGROUP,
        is_forum=chat.is_forum is True,
        is_private=not chat.username and not chat.active_usernames,
        actor_is_creator=owner_in,
        bot_can_manage_topics=isinstance(me, ChatMemberAdministrator) and bool(me.can_manage_topics),
        member_count=count,
    )
    return check, userbot_in


def is_member(member: ChatMember) -> bool:
    if isinstance(member, ChatMemberRestricted):
        return member.is_member
    return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)


class Registrar:
    def __init__(
        self, db: Database, bot: Bot, orchestrator: Orchestrator, settings: Settings, provisions: ProvisionStore | None = None
    ) -> None:
        self.provisions = provisions  # None = no userbot: every group must be made by the person
        self.settings = settings
        self.db = db
        self.bot = bot
        self.orchestrator = orchestrator
        self._attempts = Throttle(settings.registration_retry_seconds)  # (chat, sender): re-check from plain messages
        self._hints = Throttle(settings.registration_hint_seconds)  # chat: "what is still missing" from plain messages

    # ------------------------------------------------------------ registration

    async def on_bot_membership(self, event: ChatMemberUpdated) -> None:
        """``my_chat_member``: the bot was added to / promoted in / removed from a group."""
        try:
            if event.chat.type not in GROUP_TYPES:
                return
            status = event.new_chat_member.status
            if status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                await self._go_dormant(event.chat.id)
            elif status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
                await self._attempt(event.chat.id, event.from_user, source="membership")
        except Exception:  # noqa: BLE001 - the boundary
            log.exception("registration: could not handle a membership change of the bot in chat %s", event.chat.id)

    async def on_group_message(self, message: Message) -> None:
        """A message in a group that is not the sender's registered group (called by the tenant middleware): the person may have
        just enabled Topics or promoted the bot, which does not always produce a ``my_chat_member`` update."""
        try:
            sender = message.from_user
            if sender is None or message.sender_chat is not None:  # anonymous admin / channel: the author is unknown
                return
            if not self._attempts.allow((message.chat.id, sender.id)):
                return
            await self._attempt(message.chat.id, sender, source="message")
        except Exception:  # noqa: BLE001 - the boundary
            log.exception("registration: could not handle a message in chat %s", message.chat.id)

    async def _attempt(self, chat_id: int, actor: TelegramUser | None, *, source: Source) -> None:
        if actor is None or actor.is_bot or actor.id == ANONYMOUS_ADMIN_BOT_ID:
            log.info("registration: chat %s: the actor is unknown, a bot or an anonymous admin; ignored", chat_id)
            return
        binding = await self._binding(chat_id)
        if binding is not None:  # a group the userbot made: the person it was made for is the owner, whoever triggered this event
            if source == "message" and actor.id != binding.owner:
                return  # somebody else writing in it: not the owner's business, nothing to say
            await self._attempt_provisioned(chat_id, binding, source=source)
            return
        known = await self.db.resolve_tenant_id(forum_chat_id=chat_id)
        if known is not None:  # somebody's registered group: nothing to register, except bringing a dormant tenant back
            tenant = await load_tenant(self.db, known)
            if tenant is None or tenant.group_state is not GroupState.dormant:
                return
        check = await inspect_group(self.bot, chat_id, actor.id)
        if not check.actor_is_creator:
            if source == "membership":  # somebody added the bot: tell them why nothing happens. A plain chatter is not told.
                await self._say(chat_id, texts.NOT_CREATOR)
            return
        problems = check.problems()
        if problems:
            if source == "membership" or self._hints.allow(chat_id):
                await self._say(chat_id, texts.setup_checklist(problems))
            return
        registration = await self.db.register_tenant(telegram_user_id=actor.id, forum_chat_id=chat_id)
        await self._announce(registration, chat_id)

    async def _binding(self, chat_id: int) -> ChatBinding | None:
        """Who a group made by the userbot belongs to (None for every other group, and always when there is no userbot). A Redis outage means
        "unknown": the group is then treated like any other (the person would be told only the creator may connect it), never trusted on a guess."""
        if self.provisions is None:
            return None
        try:
            return await self.provisions.chat(chat_id)
        except Exception:  # noqa: BLE001
            log.exception("registration: could not read the provisioning record of chat %s", chat_id)
            return None

    async def _attempt_provisioned(self, chat_id: int, binding: ChatBinding, *, source: Source) -> None:
        known = await self.db.resolve_tenant_id(forum_chat_id=chat_id)
        if known is not None:
            tenant = await load_tenant(self.db, known)
            if tenant is None or tenant.group_state is not GroupState.dormant:
                return
        check, userbot_in = await inspect_provisioned(self.bot, chat_id, binding)
        if not check.actor_is_creator:  # the person is not in yet (a one-use link they have not opened): nothing to do, nothing to say
            log.info("registration: chat %s was made for user %s, who has not joined yet", chat_id, binding.owner)
            return
        binding = await self._welcome_owner(chat_id, binding)  # the moment they are in: administrator rights, the topics, the greeting (once), whoever is still in the group
        if userbot_in:  # the userbot is about to leave (its departure triggers the next attempt): a group with a second account in it is not private
            log.info("registration: chat %s: waiting for the userbot to leave", chat_id)
            return
        problems = check.problems()
        if problems:
            if source == "membership" or self._hints.allow(chat_id):
                await self._say(chat_id, texts.setup_checklist(problems))
            return
        existing = await self.db.resolve_tenant_id(telegram_user_id=binding.owner)
        if existing is not None and known is None:  # a person who already has a (broken) group is moving to this one
            await self._move_group(existing, chat_id, binding)
            return
        registration = await self.db.register_tenant(telegram_user_id=binding.owner, forum_chat_id=chat_id)
        await self._announce(registration, chat_id, binding)

    async def _welcome_owner(self, chat_id: int, binding: ChatBinding) -> ChatBinding:
        """The owner of a group the userbot made is in it. The bot (the only administrator left once the userbot goes) does three things, each one guarded on its own:
        (a) makes the owner an administrator with every right, (b) creates the topics Settings and Ideas-and-feedback (the userbot makes none), (c) greets the owner in the
        main chat. A refused promotion changes the greeting into the warning and sends the traceback to the developer; a refused topic goes to the developer too.
        Once per group (a join is reported twice: service message + chat_member). Never raises. Returns the binding with the topic ids it now holds."""
        if self.provisions is None:
            return binding
        try:
            if not await self.provisions.welcome_once(chat_id):
                return binding
        except Exception:  # noqa: BLE001 - Redis down: better no greeting than a greeting at every event
            log.exception("registration: could not mark the welcome of chat %s", chat_id)
            return binding
        log.info("registration: the owner %s of chat %s is in: promoting, making topics, greeting", binding.owner, chat_id)
        promoted = True
        try:
            await self._promote_owner(chat_id, binding.owner)
        except Exception as exc:  # noqa: BLE001 - "not enough rights", an anonymous admin, Telegram hiccups: the rest of the chain still runs
            promoted = False
            log.warning("registration: could not make user %s an administrator of chat %s", binding.owner, chat_id, exc_info=True)
            error_handler.report_crash_soon(exc, where="registration:promote_owner", settings=self.settings, bot=self.bot)
        binding = await self._make_topics(chat_id, binding)
        try:
            await self.bot.send_message(chat_id=chat_id, text=texts.GROUP_READY if promoted else texts.GROUP_NO_ADMIN)  # no thread id = the main chat (General)
        except Exception:  # noqa: BLE001
            log.exception("registration: could not greet the owner in chat %s", chat_id)
            try:
                await self.provisions.welcome_undo(chat_id)  # the next event tries again (the promotion and the topics are not repeated: both are already done)
            except Exception:  # noqa: BLE001
                log.exception("registration: could not undo the welcome mark of chat %s", chat_id)
        return binding

    async def _make_topics(self, chat_id: int, binding: ChatBinding) -> ChatBinding:
        """The two system topics, made by the bot right away (before the tenant exists, so only the ids are kept: in the Redis binding, adopted at registration).
        A topic that already has an id is not made again. A refusal is logged, sent to the developer and skipped: the registration makes the missing feedback topic itself."""
        for field, spec in (("settings_thread", SETTINGS_PERSONA), ("feedback_thread", FEEDBACK_PERSONA)):
            if getattr(binding, field) is not None:
                continue
            try:
                topic = await self.bot.create_forum_topic(chat_id=chat_id, name=spec.title)
            except Exception as exc:  # noqa: BLE001
                log.warning("registration: could not create the topic %r in chat %s", spec.title, chat_id, exc_info=True)
                error_handler.report_crash_soon(exc, where="registration:create_topic", settings=self.settings, bot=self.bot)
                continue
            binding = replace(binding, **{field: topic.message_thread_id})
            try:
                await self.provisions.bind_chat(chat_id, binding)  # after EACH topic: a crash between the two must not lose the first one's id
            except Exception:  # noqa: BLE001
                log.exception("registration: could not save the id of the topic %r of chat %s", spec.title, chat_id)
        return binding

    async def _promote_owner(self, chat_id: int, owner_id: int) -> None:
        """Every administrator right the bot itself has (Telegram allows no more), except staying anonymous. Raises if Telegram refuses."""
        member = await self.bot.get_chat_member(chat_id, owner_id)
        if member.status == ChatMemberStatus.CREATOR or (isinstance(member, ChatMemberAdministrator) and member.can_promote_members and member.can_manage_topics):
            return  # already as strong as it gets
        await self.bot.promote_chat_member(
            chat_id=chat_id,
            user_id=owner_id,
            is_anonymous=False,
            can_manage_chat=True,
            can_change_info=True,
            can_delete_messages=True,
            can_restrict_members=True,
            can_invite_users=True,
            can_pin_messages=True,
            can_manage_topics=True,
            can_promote_members=True,
            can_manage_video_chats=True,
        )

    async def _move_group(self, tenant_id: uuid.UUID, chat_id: int, binding: ChatBinding) -> None:
        """The owner already has a tenant and just came into a NEW group the userbot made for them: point the tenant at it. Only when the current group is really
        broken (or the bot was removed from it): an old link opened by somebody whose group works must not move them."""
        tenant = await load_tenant(self.db, tenant_id)
        if tenant is None:
            return
        if tenant.forum_chat_id is not None and tenant.group_state is not GroupState.dormant:
            if await group_health(self.bot, tenant.forum_chat_id, binding.owner) != "broken":
                log.info("registration: user %s opened a new group %s, but the group %s works: not moved", binding.owner, chat_id, tenant.forum_chat_id)
                return
        moved = await self.db.relocate_group(telegram_user_id=binding.owner, forum_chat_id=chat_id)
        log.info("registration: chat %s -> %s for tenant %s (was chat %s)", chat_id, moved.outcome, moved.tenant_id, moved.old_chat_id)
        if moved.outcome == "chat_taken":
            await self._say(chat_id, texts.CHAT_TAKEN)
            return
        if moved.outcome != "relocated" or moved.tenant_id is None:
            return
        fresh = await load_tenant(self.db, moved.tenant_id)
        if fresh is None:
            raise RuntimeError(f"tenant {moved.tenant_id} vanished right after the move")
        if moved.old_chat_id is not None:
            try:
                await self.bot.leave_chat(moved.old_chat_id)  # the bot has no business in the abandoned group any more
            except Exception:  # noqa: BLE001 - it may be gone already
                log.info("registration: could not leave the old chat %s", moved.old_chat_id, exc_info=True)
        if fresh.onboarding_state is OnboardingState.pending:
            await self.orchestrator.start_onboarding(fresh)
        else:
            await self._say(chat_id, texts.GROUP_MOVED)
        await self.orchestrator.adopt_system_topics(fresh, settings_thread=binding.settings_thread, feedback_thread=binding.feedback_thread)

    async def _announce(self, registration: Registration, chat_id: int, binding: ChatBinding | None = None) -> None:
        outcome = registration.outcome
        log.info("registration: chat %s -> %s (tenant %s)", chat_id, outcome, registration.tenant_id)
        if outcome is RegistrationOutcome.has_other_group:
            await self._say(chat_id, texts.HAS_OTHER_GROUP)
        elif outcome is RegistrationOutcome.other_group_dormant:
            await self._say(chat_id, texts.OTHER_GROUP_GONE)
        elif outcome is RegistrationOutcome.chat_taken:
            await self._say(chat_id, texts.CHAT_TAKEN)
        else:  # created / attached_chat / already_registered / reactivated
            if registration.tenant_id is None:
                raise RuntimeError(f"register_tenant returned {outcome} without a tenant id")
            tenant = await load_tenant(self.db, registration.tenant_id)
            if tenant is None:
                raise RuntimeError(f"tenant {registration.tenant_id} vanished right after registration")
            if tenant.onboarding_state is OnboardingState.pending:
                await self.orchestrator.start_onboarding(tenant)  # the first question is the welcome; a no-op if already asked
                if binding is not None and (binding.settings_thread is not None or binding.feedback_thread is not None):
                    await self.orchestrator.adopt_system_topics(  # the topics the userbot made; one it could not make is made by the bot
                        tenant, settings_thread=binding.settings_thread, feedback_thread=binding.feedback_thread
                    )
                else:
                    await self.orchestrator.ensure_system_topics(tenant)  # the "Ideas and feedback" channel; Settings follows the interview
            elif outcome is RegistrationOutcome.reactivated:
                await self._say(chat_id, texts.RETURNED)
            elif outcome is RegistrationOutcome.attached_chat:
                await self._say(chat_id, texts.CONNECTED)

    # ------------------------------------------------------------ the privacy guard of a registered group

    async def on_member_update(self, event: ChatMemberUpdated) -> None:
        """``chat_member``: somebody joined or left. Covers joins by invite link and silent joins (no service message)."""
        try:
            if event.chat.type not in GROUP_TYPES:
                return
            was, now = is_member(event.old_chat_member), is_member(event.new_chat_member)
            if now and not was:
                await self._on_arrival(event.chat.id, [event.new_chat_member.user])
            elif was and not now:
                await self._on_departure(event.chat.id)
        except Exception:  # noqa: BLE001 - the boundary
            log.exception("registration: could not handle a member update in chat %s", event.chat.id)

    async def on_members_joined(self, message: Message) -> None:
        """The "X joined the group" service message."""
        try:
            if message.chat.type in GROUP_TYPES:
                await self._on_arrival(message.chat.id, list(message.new_chat_members or []))
        except Exception:  # noqa: BLE001 - the boundary
            log.exception("registration: could not handle a join in chat %s", message.chat.id)

    async def on_member_left(self, message: Message) -> None:
        """The "X left the group" service message."""
        try:
            if message.chat.type in GROUP_TYPES:
                await self._on_departure(message.chat.id)
        except Exception:  # noqa: BLE001 - the boundary
            log.exception("registration: could not handle a departure in chat %s", message.chat.id)

    async def _on_arrival(self, chat_id: int, people: list[TelegramUser]) -> None:
        others = [person for person in people if person.id != self.bot.id]
        if not others:
            return
        binding = await self._binding(chat_id)
        log.info("registration: arrival in chat %s of %s (made for: %s)", chat_id, [p.id for p in others], None if binding is None else binding.owner)
        if binding is not None:
            if any(person.id == binding.owner for person in others):  # the person it was made for just came in: maybe everything is ready now
                await self._attempt_provisioned(chat_id, binding, source="membership")
            others = [person for person in others if person.id != binding.userbot]  # the userbot is not an intruder
            if not others:
                return
        tenant_id = await self.db.resolve_tenant_id(forum_chat_id=chat_id)
        if tenant_id is None:
            return
        owner = await self._owner_of(tenant_id)
        if all(person.id == owner for person in others):
            return
        # a join is reported twice (service message + chat_member): the conditional update makes the warning go out once
        if await self._move(tenant_id, GroupState.frozen, only_from=(GroupState.active,)):
            log.warning("group of tenant %s (chat %s) is not private any more: frozen", tenant_id, chat_id)
            await self._say(chat_id, texts.FROZEN)

    async def _on_departure(self, chat_id: int) -> None:
        binding = await self._binding(chat_id)
        if binding is not None:  # probably the userbot leaving: the group may be ready to register now
            await self._attempt_provisioned(chat_id, binding, source="membership")
        tenant_id = await self.db.resolve_tenant_id(forum_chat_id=chat_id)
        if tenant_id is None:
            return
        tenant = await load_tenant(self.db, tenant_id)
        if tenant is not None and tenant.group_state is GroupState.frozen:
            await self.try_unfreeze(tenant)

    async def try_unfreeze(self, tenant: TenantInfo) -> bool:
        """Is the group private again? Asks Telegram for the head count; True = the group is active (it was unfrozen now, or
        by somebody a moment ago). Telegram trouble fails closed: the group stays frozen."""
        if tenant.forum_chat_id is None:
            return False
        try:
            people = await self.bot.get_chat_member_count(tenant.forum_chat_id)
        except Exception:  # noqa: BLE001 - cannot prove the group is private: stay silent
            log.exception("could not count the members of chat %s; the group stays frozen", tenant.forum_chat_id)
            return False
        if people > MAX_PEOPLE:
            return False
        if await self._move(tenant.id, GroupState.active, only_from=(GroupState.frozen,)):
            log.info("group of tenant %s is private again: unfrozen", tenant.id)
            await self._say(tenant.forum_chat_id, texts.UNFROZEN)
        return True

    async def _go_dormant(self, chat_id: int) -> None:
        tenant_id = await self.db.resolve_tenant_id(forum_chat_id=chat_id)
        if tenant_id is not None and await self._move(
            tenant_id, GroupState.dormant, only_from=(GroupState.active, GroupState.frozen)
        ):
            log.warning("the bot was removed from the group of tenant %s (chat %s): dormant", tenant_id, chat_id)

    # ------------------------------------------------------------ helpers

    async def _owner_of(self, tenant_id: uuid.UUID) -> int | None:
        async with self.db.tenant_session(tenant_id) as session:
            return (await session.execute(select(User.telegram_user_id).where(User.id == tenant_id))).scalar_one_or_none()

    async def _move(self, tenant_id: uuid.UUID, new: GroupState, *, only_from: tuple[GroupState, ...]) -> bool:
        """Compare-and-set of ``users.group_state``: True only for the caller whose update actually changed the row."""
        async with self.db.tenant_session(tenant_id) as session:
            result = await session.execute(
                update(User)
                .where(User.id == tenant_id, User.group_state.in_([state.value for state in only_from]))
                .values(group_state=new.value, group_state_changed_at=func.now())
            )
            return result.rowcount == 1

    async def _say(self, chat_id: int, text: str) -> None:
        """Best effort (a courtesy, or a warning that must not break the update): a failure is only logged."""
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except Exception:  # noqa: BLE001
            log.exception("could not write to chat %s", chat_id)


def build_router() -> Router:
    """Registered BEFORE the orchestrator router: these updates must not reach the tenant middleware (they are about groups and
    members, not about a tenant's message)."""
    router = Router(name="registration")

    @router.my_chat_member()
    async def on_bot_status(event: ChatMemberUpdated, registrar: Registrar) -> None:
        await registrar.on_bot_membership(event)

    @router.chat_member()
    async def on_member_status(event: ChatMemberUpdated, registrar: Registrar) -> None:
        await registrar.on_member_update(event)

    @router.message(F.new_chat_members)
    async def on_join(message: Message, registrar: Registrar) -> None:
        await registrar.on_members_joined(message)

    @router.message(F.left_chat_member)
    async def on_leave(message: Message, registrar: Registrar) -> None:
        await registrar.on_member_left(message)

    return router


def unfrozen(tenant: TenantInfo) -> TenantInfo:
    return replace(tenant, group_state=GroupState.active)

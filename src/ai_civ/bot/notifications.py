"""Delivery of ``civ.notification_outbox``: "tell this tenant that ..." rows that another tenant's transaction (a SECURITY DEFINER
function) wrote durably. Kinds: ``contact_accepted`` - somebody accepted your invite link; ``event_proposal`` - a contact invites you to a
meeting (a card with Accept / Decline); ``event_proposal_answer`` - the person you invited accepted or could not.

Life of a row (the same contract as ``memory.outbox``; the DB clock is the only clock):

    pending|failed|processing --claim--> processing (attempts + 1, lease)   [FOR UPDATE SKIP LOCKED: workers never double-claim]
    processing --send to Telegram (NO transaction open)--> done                [fenced: only if still ours]
    processing --error--> failed (backoff, or the Telegram retry_after)  |  dead (attempts exhausted / permanent)
    crashed worker: the lease simply expires and the row is claimed again.

Only rows whose recipient can be written to are claimed: an ``active`` group of a tenant who finished onboarding. A frozen / dormant
group keeps its notifications waiting (no attempt is burned) until the group is active again - or until they are too old
(``civ.purge_social``). Delivery is at-least-once: if the process dies between the Telegram call and ``mark_done``, the
notification is sent again after the lease expires (the text is harmless to repeat).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, or_, select, update

from ai_civ.bot import personas, proposals, texts
from ai_civ.bot.personas import SETTINGS_PERSONA
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import ContactStatus, OutboxStatus, ProposalStatus
from ai_civ.db.models import Contact, EventProposal, NotificationOutbox, User

log = logging.getLogger(__name__)

KIND_CONTACT_ACCEPTED = "contact_accepted"
KIND_EVENT_PROPOSAL = "event_proposal"  # to the recipient: the card with Accept / Decline (migration 0008)
KIND_EVENT_PROPOSAL_ANSWER = "event_proposal_answer"  # to the author: they accepted / could not
OPEN_STATUSES = (OutboxStatus.pending, OutboxStatus.failed, OutboxStatus.processing)
MAX_ERROR_CHARS = 2000


class UnknownKind(RuntimeError):
    """A notification kind without a delivery routine. Permanent: the row goes straight to 'dead'."""


@dataclass(frozen=True)
class Claimed:
    id: int
    user_id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


def backoff_seconds(attempts: int, *, base: int, cap: int) -> int:
    return min(cap, base * 2 ** max(attempts - 1, 0))


def _deliverable():
    """Conditions of a row that can be claimed, in the recipient's own session (their ``users`` row is visible to them)."""
    return (
        NotificationOutbox.status.in_(OPEN_STATUSES),
        NotificationOutbox.attempts < NotificationOutbox.max_attempts,
        or_(NotificationOutbox.locked_until.is_(None), NotificationOutbox.locked_until < func.now()),
        User.group_state == "active",
        User.onboarding_state == "done",
        User.forum_chat_id.is_not(None),
    )


async def claim_next(db: Database, user_id: uuid.UUID, *, lease_seconds: int) -> Claimed | None:
    """Claim the oldest deliverable notification of one tenant (own short transaction, committed before the slow send)."""
    async with db.tenant_session(user_id) as session:
        picked = (
            await session.execute(
                select(NotificationOutbox.id)
                .join(User, User.id == NotificationOutbox.user_id)
                .where(*_deliverable())
                .order_by(NotificationOutbox.created_at, NotificationOutbox.id)
                .with_for_update(skip_locked=True, of=NotificationOutbox)
                .limit(1)
            )
        ).first()
        if picked is None:
            return None
        row = (
            await session.execute(
                update(NotificationOutbox)
                .where(NotificationOutbox.id == picked[0])
                .values(
                    status=OutboxStatus.processing,
                    attempts=NotificationOutbox.attempts + 1,
                    locked_until=func.now() + timedelta(seconds=lease_seconds),
                )
                .returning(
                    NotificationOutbox.id,
                    NotificationOutbox.user_id,
                    NotificationOutbox.kind,
                    NotificationOutbox.payload,
                    NotificationOutbox.attempts,
                    NotificationOutbox.max_attempts,
                )
            )
        ).one()
    return Claimed(row.id, row.user_id, row.kind, dict(row.payload), row.attempts, row.max_attempts)


async def mark_done(db: Database, claimed: Claimed) -> bool:
    """Finish a delivered row. Fenced: only if it is still 'processing' with the attempt this worker claimed it with."""
    async with db.tenant_session(claimed.user_id) as session:
        result = await session.execute(
            update(NotificationOutbox)
            .where(
                NotificationOutbox.id == claimed.id,
                NotificationOutbox.status == OutboxStatus.processing,
                NotificationOutbox.attempts == claimed.attempts,
            )
            .values(status=OutboxStatus.done, processed_at=func.now(), locked_until=None, last_error=None)
        )
        return result.rowcount == 1


async def fail(
    db: Database,
    claimed: Claimed,
    error: str,
    *,
    permanent: bool = False,
    backoff_base: int,
    backoff_cap: int,
    retry_after: int | None = None,
) -> OutboxStatus | None:
    """Record a failed delivery: 'failed' + a delay (the Telegram ``retry_after`` if it gave one), or 'dead'. None = not ours any more."""
    dead = permanent or claimed.attempts >= claimed.max_attempts
    values: dict[str, Any] = {"last_error": error[:MAX_ERROR_CHARS]}
    if dead:
        values.update(status=OutboxStatus.dead, processed_at=func.now(), locked_until=None)
    else:
        delay = retry_after or backoff_seconds(claimed.attempts, base=backoff_base, cap=backoff_cap)
        values.update(status=OutboxStatus.failed, locked_until=func.now() + timedelta(seconds=delay))
    async with db.tenant_session(claimed.user_id) as session:
        row = (
            await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.id == claimed.id,
                    NotificationOutbox.status == OutboxStatus.processing,
                    NotificationOutbox.attempts == claimed.attempts,
                )
                .values(**values)
                .returning(NotificationOutbox.status)
            )
        ).first()
    return None if row is None else OutboxStatus(row[0])


class Notifier:
    def __init__(self, db: Database, bot: Bot, settings: Settings) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings

    async def run_tenant(self, user_id: uuid.UUID, *, max_tasks: int | None = None) -> int:
        """Deliver up to ``max_tasks`` notifications of ONE tenant; returns how many were delivered.

        The first failure is persisted (backoff / dead) and re-raised: the process fails fast instead of burning the attempts of
        every queued row against a Telegram that is not answering. The rest is picked up by a later run."""
        s = self.settings
        delivered = 0
        for _ in range(max_tasks or s.notification_batch_per_run):
            claimed = await claim_next(self.db, user_id, lease_seconds=s.notification_lease_seconds)
            if claimed is None:
                break
            try:
                await self._deliver(claimed)
            except Exception as exc:
                status = await fail(
                    self.db,
                    claimed,
                    f"{type(exc).__name__}: {exc}",
                    permanent=isinstance(exc, UnknownKind),
                    backoff_base=s.notification_backoff_base_seconds,
                    backoff_cap=s.notification_backoff_max_seconds,
                    retry_after=exc.retry_after if isinstance(exc, TelegramRetryAfter) else None,
                )
                log.error("notification %s (%s) failed -> %s: %r", claimed.id, claimed.kind, status, exc)
                raise
            if not await mark_done(self.db, claimed):
                log.warning("notification %s was delivered but is no longer ours (lease expired); it may be sent twice", claimed.id)
            delivered += 1
        return delivered

    async def _deliver(self, claimed: Claimed) -> None:
        if claimed.kind == KIND_CONTACT_ACCEPTED:
            await self._contact_accepted(claimed)
        elif claimed.kind == KIND_EVENT_PROPOSAL:
            await self._event_proposal(claimed)
        elif claimed.kind == KIND_EVENT_PROPOSAL_ANSWER:
            await self._event_proposal_answer(claimed)
        else:
            raise UnknownKind(f"no delivery routine for notification kind {claimed.kind!r}")

    async def _contact_accepted(self, claimed: Claimed) -> None:
        """"<name> is now in your contacts": into the Settings topic (the main chat if there is none), with the app button."""
        contact_id = uuid.UUID(str(claimed.payload["contact_id"]))
        async with self.db.tenant_session(claimed.user_id) as session:
            alias = (
                await session.execute(
                    select(Contact.alias).where(Contact.id == contact_id, Contact.status == ContactStatus.accepted)
                )
            ).first()
        if alias is None:  # the contact was removed (or blocked) before we got to it: nothing to announce
            log.info("notification %s: contact %s is gone; nothing to announce", claimed.id, contact_id)
            return
        text = texts.NOTIFY_CONTACT_ACCEPTED.format(name=alias[0] or texts.DEFAULT_CONTACT_NAME)
        button = InlineKeyboardButton(text=texts.OPEN_APP, url=self.settings.mini_app_direct_link)
        await self._post(claimed.user_id, text, InlineKeyboardMarkup(inline_keyboard=[[button]]))

    async def _event_proposal(self, claimed: Claimed) -> None:
        """The card of an invitation: "<name> invites you: <title>, <when>" with Accept / Decline, into the Settings topic.

        Only a card that can still be answered is sent: a proposal that was decided, ran out of time, or whose sender is no longer in
        the recipient's contacts (removed / blocked) is finished silently. The name is the RECIPIENT's own alias of the sender,
        never a name the sender chose; the time is in the recipient's zone."""
        proposal_id = uuid.UUID(str(claimed.payload["proposal_id"]))
        async with self.db.tenant_session(claimed.user_id) as session:
            row = (
                await session.execute(
                    select(
                        EventProposal.user_id,
                        EventProposal.title,
                        EventProposal.starts_at,
                        EventProposal.ends_at,
                        EventProposal.status,
                        (EventProposal.expires_at > func.now()).label("live"),
                    ).where(EventProposal.id == proposal_id, EventProposal.recipient_user_id == claimed.user_id)
                )
            ).first()
            contact = None
            timezone = (await session.execute(select(User.timezone).where(User.id == claimed.user_id))).scalar_one()
            if row is not None:
                contact = (
                    await session.execute(
                        select(Contact.alias, Contact.status).where(
                            Contact.user_id == claimed.user_id, Contact.contact_user_id == row.user_id
                        )
                    )
                ).first()
        if row is None or row.status != ProposalStatus.pending or not row.live or contact is None or contact.status != ContactStatus.accepted:
            log.info("notification %s: invitation %s can no longer be answered; nothing to deliver", claimed.id, proposal_id)
            return
        text = texts.PROPOSAL_CARD.format(
            name=contact.alias or texts.DEFAULT_CONTACT_NAME,
            title=row.title,
            when=texts.when_text(row.starts_at, row.ends_at, timezone),
        )
        buttons = [
            InlineKeyboardButton(text=texts.PROPOSAL_ACCEPT, callback_data=proposals.callback_data(proposal_id, True)),
            InlineKeyboardButton(text=texts.PROPOSAL_DECLINE, callback_data=proposals.callback_data(proposal_id, False)),
        ]
        await self._post(claimed.user_id, text, InlineKeyboardMarkup(inline_keyboard=[buttons]))

    async def _event_proposal_answer(self, claimed: Claimed) -> None:
        """Closes the loop for the AUTHOR: "<name> принял(а): ..." or "<name> не сможет в это время: ...". Neutral on purpose: a refusal
        gives no reason (there is none to give). The state comes from the proposal row, not from the payload."""
        proposal_id = uuid.UUID(str(claimed.payload["proposal_id"]))
        async with self.db.tenant_session(claimed.user_id) as session:
            row = (
                await session.execute(
                    select(
                        EventProposal.recipient_user_id,
                        EventProposal.title,
                        EventProposal.starts_at,
                        EventProposal.ends_at,
                        EventProposal.status,
                    ).where(EventProposal.id == proposal_id, EventProposal.user_id == claimed.user_id)
                )
            ).first()
            timezone = (await session.execute(select(User.timezone).where(User.id == claimed.user_id))).scalar_one()
            alias = None
            if row is not None:
                alias = (
                    await session.execute(
                        select(Contact.alias).where(
                            Contact.user_id == claimed.user_id, Contact.contact_user_id == row.recipient_user_id
                        )
                    )
                ).scalar_one_or_none()  # the author's own name for them; a removed contact still gets the news, under a neutral name
        template = {ProposalStatus.accepted: texts.NOTIFY_PROPOSAL_ACCEPTED, ProposalStatus.declined: texts.NOTIFY_PROPOSAL_DECLINED}.get(
            ProposalStatus(row.status) if row is not None else ProposalStatus.pending
        )
        if row is None or template is None:
            log.info("notification %s: invitation %s has no answer to report", claimed.id, proposal_id)
            return
        text = template.format(
            name=alias or texts.DEFAULT_CONTACT_NAME,
            title=row.title,
            when=texts.when_text(row.starts_at, row.ends_at, timezone),
        )
        await self._post(claimed.user_id, text, None)

    async def _post(self, user_id: uuid.UUID, text: str, markup: InlineKeyboardMarkup | None) -> None:
        """Say ``text`` to the tenant: the Settings topic, or the main chat when there is none (or it was deleted)."""
        async with self.db.tenant_session(user_id) as session:
            chat_id = (await session.execute(select(User.forum_chat_id).where(User.id == user_id))).scalar_one()
            settings_topic = await personas.get_by_code(session, SETTINGS_PERSONA.code)
        if chat_id is None:
            raise RuntimeError(f"tenant {user_id} has no forum group")
        thread = settings_topic.telegram_thread_id if settings_topic is not None and settings_topic.is_active else None
        try:
            await self.bot.send_message(chat_id=chat_id, text=text, message_thread_id=thread, reply_markup=markup)
        except TelegramBadRequest as exc:
            if thread is None or "thread" not in str(exc).lower():
                raise
            # the user deleted the Settings topic: say it in the main chat instead of failing forever (one fallback, no loop)
            await self.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)

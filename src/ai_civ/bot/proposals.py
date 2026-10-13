"""The recipient's side of an invitation (migration 0008): the Accept / Decline buttons under the card ``Notifier`` delivered.

``ep:<proposal uuid>:y|n`` (3 + 36 + 2 = 41 bytes, Telegram allows 64). The id is a handle, not a secret: the database re-checks everything
(``civ.respond_event_proposal`` answers only to the proposal's RECIPIENT, so a card pressed by anybody else gets ``unavailable``).

One press = ONE transaction: the proposal is read (to word the answer) and ``civ.respond_event_proposal`` runs in the same tenant session.
On accept the database puts the meeting into BOTH calendars and queues the neutral note for the author (``event_proposal_answer``); this
module writes nothing itself. The card is then replaced by the final state (the buttons go away). A second click, or a click after the
time ran out, shows what was decided - it never creates a second event.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery
from sqlalchemy import select, text

from ai_civ.bot import texts
from ai_civ.bot.orchestrator import TenantInfo
from ai_civ.db import Database
from ai_civ.db.models import EventProposal

log = logging.getLogger(__name__)

CALLBACK_PREFIX = "ep"
CALLBACK_RE = re.compile(r"^ep:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(y|n)$")

OUTCOMES = frozenset(texts.PROPOSAL_TOAST)  # what civ.respond_event_proposal can answer
_FINAL_TEXT = {
    "accepted": texts.PROPOSAL_ACCEPTED,
    "already_accepted": texts.PROPOSAL_ACCEPTED,
    "declined": texts.PROPOSAL_DECLINED,
    "already_declined": texts.PROPOSAL_DECLINED,
    "expired": texts.PROPOSAL_EXPIRED,
}


def callback_data(proposal_id: uuid.UUID, accept: bool) -> str:
    return f"{CALLBACK_PREFIX}:{proposal_id}:{'y' if accept else 'n'}"


def parse_callback(data: str) -> tuple[uuid.UUID, bool] | None:
    match = CALLBACK_RE.match(data)
    if match is None:
        return None
    return uuid.UUID(match.group(1)), match.group(2) == "y"


@dataclass(frozen=True)
class Decision:
    outcome: str
    title: str | None = None  # None = the proposal is not addressed to this tenant (or is gone): nothing to show about it
    starts_at: datetime | None = None
    ends_at: datetime | None = None


def final_text(decision: Decision, timezone: str) -> str:
    """The card after the press: the state the DATABASE reports, in the recipient's time zone."""
    template = _FINAL_TEXT.get(decision.outcome)
    if template is None or decision.title is None or decision.starts_at is None or decision.ends_at is None:
        return texts.PROPOSAL_GONE
    return template.format(title=decision.title, when=texts.when_text(decision.starts_at, decision.ends_at, timezone))


class ProposalService:
    def __init__(self, db: Database, bot: Bot) -> None:
        self.db = db
        self.bot = bot

    async def decide(self, tenant_id: uuid.UUID, proposal_id: uuid.UUID, accept: bool) -> Decision:
        """Read the proposal and answer it in ONE tenant transaction. Raises on a database failure: nothing was changed then."""
        async with self.db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(EventProposal.title, EventProposal.starts_at, EventProposal.ends_at).where(
                        EventProposal.id == proposal_id, EventProposal.recipient_user_id == tenant_id
                    )
                )
            ).first()
            outcome = (
                await session.execute(
                    text("SELECT out_outcome FROM civ.respond_event_proposal(:p, :a)"), {"p": proposal_id, "a": accept}
                )
            ).scalar_one()
        if outcome not in OUTCOMES:
            raise RuntimeError(f"civ.respond_event_proposal answered {outcome!r}")
        if row is None:
            return Decision(outcome)
        return Decision(outcome, row.title, row.starts_at, row.ends_at)

    async def on_callback(self, tenant: TenantInfo, query: CallbackQuery) -> None:
        parsed = parse_callback(query.data or "")
        if parsed is None:
            await self._answer(query, texts.PROPOSAL_UNKNOWN_BUTTON)
            return
        decision = await self.decide(tenant.id, *parsed)
        await self._answer(query, texts.PROPOSAL_TOAST[decision.outcome])
        message = query.message
        if message is None:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=message.chat.id, message_id=message.message_id, text=final_text(decision, tenant.timezone)
            )
        except TelegramAPIError:  # includes "message is not modified" (the second click of a double click); the decision is already made
            log.info("could not edit the invitation card of tenant %s", tenant.id, exc_info=True)

    @staticmethod
    async def _answer(query: CallbackQuery, text_: str) -> None:
        try:
            await query.answer(text_)
        except TelegramAPIError:
            log.warning("could not answer a callback query", exc_info=True)

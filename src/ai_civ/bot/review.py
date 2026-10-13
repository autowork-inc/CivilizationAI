"""Weekly review of the quarantine: the user decides what the Garbage Collector may put into Core.

The GC never writes to ``core_facts``: it only proposes (``pending_facts``). Once a week this module sends the open proposals
to the user, one message each with two buttons (keep / delete). The decision is FINAL (database trigger) and is made by the
person, not by a model:

  * keep   -> ONE transaction: ``pending -> approved``, the old version of the fact is closed (``valid_to``) and the new one
              inserted (``source='approved_review'``, linked to the approved proposal - the database refuses it otherwise).
              An emotional state is inserted as ``user_confirmed`` with its overrides, resolved to principle versions by code;
  * delete -> ``pending -> rejected``; the same proposal is not offered again for ``review_cooldown_days`` (see the GC).

Sending is fail-fast and at-least-once: the batch row is created first (items are assigned to it), then Telegram is called with
no transaction open, then the header's message id is recorded. A crash in between leaves a batch without that id, and the next
run re-sends it (duplicated cards are harmless: a decision cannot be made twice). The button handler is idempotent.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, text, update

from ai_civ.bot import texts
from ai_civ.bot.orchestrator import TenantInfo
from ai_civ.bot.prompt import format_value
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import (
    ChangeType,
    EmotionSource,
    FactCategory,
    FactSource,
    GroupState,
    OnboardingState,
    PendingStatus,
    PendingSubject,
    SharedMood,
)
from ai_civ.db.models import (
    CoreFact,
    EmotionalOverride,
    EmotionalState,
    PendingFact,
    Persona,
    ReviewBatch,
)

log = logging.getLogger(__name__)

CALLBACK_PREFIX = "rv"
BATCH_INTERVAL_DAYS = 6  # a new weekly batch is not started while one younger than this exists
DEFAULT_FACT_PRIORITY = 50

HEADER = (
    "Еженедельный обзор памяти. За неделю я заметил то, что, возможно, стоит запомнить о вас. "
    "«Оставить» — я начну это учитывать, «Удалить» — забуду. Решение окончательное."
)
ALL_DONE = "Обзор завершён, спасибо! Решённое учтено."
ANSWERS = {
    "approved": "Оставлено ✅",
    "rejected": "Удалено 🗑",
    "expired": "Состояние уже неактуально, пропускаю",
    "already": "Это предложение уже обработано",
    "missing": "Не нашёл это предложение",
}
FOOTERS = {"approved": "✅ Оставлено", "rejected": "🗑 Удалено", "expired": "⌛ Состояние уже неактуально"}


# ---------------------------------------------------------------- pure: callbacks and cards


def callback_data(pending_id: uuid.UUID, keep: bool) -> str:
    """``rv:<uuid>:k|d`` - 41 bytes, under Telegram's 64-byte limit."""
    return f"{CALLBACK_PREFIX}:{pending_id}:{'k' if keep else 'd'}"


def parse_callback(data: str) -> tuple[uuid.UUID, bool] | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[2] not in ("k", "d"):
        return None
    try:
        return uuid.UUID(parts[1]), parts[2] == "k"
    except ValueError:
        return None


def keyboard(pending_id: uuid.UUID) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Оставить", callback_data=callback_data(pending_id, True)),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=callback_data(pending_id, False)),
            ]
        ]
    )


@dataclass(frozen=True)
class Proposal:
    """What a card shows (a read-only view of one ``pending_facts`` row)."""

    id: uuid.UUID
    subject: PendingSubject
    change: ChangeType
    key: str | None
    value: str
    old_value: str | None
    emotion_label: str | None
    emotion_intensity: int | None
    emotion_expires_at: datetime | None
    evidence: str
    batch_id: uuid.UUID | None
    emotion_mood: SharedMood | None = None


def render_card(p: Proposal, timezone: str) -> str:
    if p.subject is PendingSubject.emotional_state:
        until = p.emotion_expires_at.astimezone(ZoneInfo(timezone)).strftime("%d.%m %H:%M") if p.emotion_expires_at else "?"
        body = f"Запомнить состояние (временно):\n• {p.emotion_label}, интенсивность {p.emotion_intensity}/10, до {until}"
        if p.emotion_mood is not None:  # what a contact could be told if this is kept: the user decides knowing it
            body += "\n" + texts.REVIEW_MOOD_LINE.format(word=texts.MOOD_WORDS[p.emotion_mood])
    elif p.change is ChangeType.delete:
        body = f"Забыть:\n• {p.key}: {p.old_value or '?'}"
    elif p.change is ChangeType.update and p.old_value:
        body = f"Обновить данные:\n• {p.key}: было «{p.old_value}» → станет «{p.value}»"
    else:
        body = f"Запомнить о вас:\n• {p.key}: {p.value}"
    return f"{body}\n\nОснование: «{p.evidence}»"


# ---------------------------------------------------------------- the service


@dataclass(frozen=True)
class Plan:
    batch_id: uuid.UUID
    proposals: list[Proposal]


@dataclass(frozen=True)
class Decision:
    outcome: Literal["approved", "rejected", "expired", "already", "missing"]
    proposal: Proposal | None = None
    batch_closed: bool = False


def _view(row: PendingFact, old_value: str | None) -> Proposal:
    return Proposal(
        id=row.id,
        subject=row.subject,
        change=row.change,
        key=row.key,
        value=format_value(row.value_text, row.value_num, row.value_unit, row.value_json),
        old_value=old_value,
        emotion_label=row.emotion_label,
        emotion_intensity=row.emotion_intensity,
        emotion_expires_at=row.emotion_expires_at,
        evidence=" ".join(row.evidence.split()),
        batch_id=row.review_batch_id,
        emotion_mood=row.emotion_mood,
    )


class ReviewService:
    def __init__(self, db: Database, bot: Bot, settings: Settings) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings

    # ------------------------------------------------------------ weekly send (Celery)

    async def send_weekly(self, tenant: TenantInfo) -> int:
        """Send this week's review. Returns the number of cards sent (0: nothing to ask, or one was sent this week)."""
        if tenant.onboarding_state is not OnboardingState.done or tenant.forum_chat_id is None:
            return 0
        if tenant.group_state is not GroupState.active:  # frozen / dormant: nothing is sent to that group
            return 0
        plan = await self._claim(tenant.id)
        if plan is None:
            return 0
        thread = await self._target_thread(tenant.id)
        header = await self.bot.send_message(chat_id=tenant.forum_chat_id, text=HEADER, message_thread_id=thread)
        for proposal in plan.proposals:
            await self.bot.send_message(
                chat_id=tenant.forum_chat_id,
                text=render_card(proposal, tenant.timezone),
                message_thread_id=thread,
                reply_markup=keyboard(proposal.id),
            )
        async with self.db.tenant_session(tenant.id) as session:
            await session.execute(
                update(ReviewBatch).where(ReviewBatch.id == plan.batch_id).values(telegram_message_id=header.message_id)
            )
        return len(plan.proposals)

    async def _claim(self, user_id: uuid.UUID) -> Plan | None:
        """ONE transaction: expire what nobody answered, then resume an unsent batch or open a new one with the oldest
        unassigned proposals. No empty batches are ever created."""
        async with self.db.tenant_session(user_id) as session:
            await session.execute(
                update(PendingFact)
                .where(
                    PendingFact.status == PendingStatus.pending,
                    PendingFact.created_at < func.now() - func.make_interval(0, 0, 0, self.settings.review_expire_days),
                )
                .values(status=PendingStatus.expired, decided_at=func.now())
            )
            batch = (
                await session.execute(
                    select(ReviewBatch)
                    .where(ReviewBatch.closed_at.is_(None), ReviewBatch.telegram_message_id.is_(None))
                    .order_by(ReviewBatch.created_at)
                    .limit(1)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if batch is None:
                recent = (
                    await session.execute(
                        select(ReviewBatch.id)
                        .where(ReviewBatch.created_at > func.now() - func.make_interval(0, 0, 0, BATCH_INTERVAL_DAYS))
                        .limit(1)
                    )
                ).first()
                if recent is not None:
                    return None
                fresh = (
                    await session.execute(
                        select(PendingFact)
                        .where(PendingFact.status == PendingStatus.pending, PendingFact.review_batch_id.is_(None))
                        .order_by(PendingFact.created_at, PendingFact.id)
                        .limit(self.settings.review_max_items)
                        .with_for_update()
                    )
                ).scalars().all()
                if not fresh:
                    return None
                batch = ReviewBatch(user_id=user_id, sent_at=func.now())
                session.add(batch)
                await session.flush()
                await session.execute(
                    update(PendingFact).where(PendingFact.id.in_([p.id for p in fresh])).values(review_batch_id=batch.id)
                )
            rows = (
                await session.execute(
                    select(PendingFact)
                    .where(PendingFact.review_batch_id == batch.id, PendingFact.status == PendingStatus.pending)
                    .order_by(PendingFact.created_at, PendingFact.id)
                )
            ).scalars().all()
            proposals = [_view(row, await self._current_value(session, row)) for row in rows]
            return Plan(batch.id, proposals) if proposals else None

    @staticmethod
    async def _current_value(session, row: PendingFact) -> str | None:
        if row.subject is not PendingSubject.core_fact or row.change is ChangeType.create:
            return None
        clause = CoreFact.id == row.target_fact_id if row.target_fact_id else CoreFact.key == row.key
        fact = (await session.execute(select(CoreFact).where(clause, CoreFact.valid_to.is_(None)))).scalars().first()
        return None if fact is None else format_value(fact.value_text, fact.value_num, fact.value_unit, fact.value_json)

    async def _target_thread(self, user_id: uuid.UUID) -> int | None:
        """The review goes to the Settings topic if there is one, otherwise to the main chat."""
        async with self.db.tenant_session(user_id) as session:
            return (
                await session.execute(
                    select(Persona.telegram_thread_id).where(Persona.code == "settings", Persona.is_active.is_(True))
                )
            ).scalar_one_or_none()

    # ------------------------------------------------------------ the decision (button press)

    async def decide(self, user_id: uuid.UUID, pending_id: uuid.UUID, keep: bool) -> Decision:
        """The user's decision, in ONE transaction. Idempotent: a second press on a decided proposal changes nothing."""
        async with self.db.tenant_session(user_id) as session:
            row = (
                await session.execute(select(PendingFact).where(PendingFact.id == pending_id).with_for_update())
            ).scalar_one_or_none()
            if row is None:
                return Decision("missing")
            old = await self._current_value(session, row)
            proposal = _view(row, old)
            if row.status is not PendingStatus.pending:
                return Decision("already", proposal)
            if not keep:
                outcome = "rejected"
                await self._set_status(session, row, PendingStatus.rejected)
            elif row.subject is PendingSubject.emotional_state:
                outcome = await self._approve_emotion(session, row)
            else:
                outcome = "approved"
                await self._approve_fact(session, row)
            closed = await self._close_batch_if_done(session, row.review_batch_id)
            return Decision(outcome, proposal, closed)

    @staticmethod
    async def _set_status(session, row: PendingFact, status: PendingStatus) -> None:
        await session.execute(
            update(PendingFact).where(PendingFact.id == row.id).values(status=status, decided_at=func.now())
        )

    async def _approve_fact(self, session, row: PendingFact) -> None:
        # approved FIRST: the insert trigger of core_facts demands an approved proposal for source='approved_review'
        await self._set_status(session, row, PendingStatus.approved)
        if row.change is ChangeType.delete:
            target = (
                await session.execute(
                    select(CoreFact).where(CoreFact.id == row.target_fact_id, CoreFact.valid_to.is_(None))
                )
            ).scalars().first()
            if target is not None:
                await session.execute(update(CoreFact).where(CoreFact.id == target.id).values(valid_to=func.now()))
            return
        current_fact = (
            await session.execute(select(CoreFact).where(CoreFact.key == row.key, CoreFact.valid_to.is_(None)))
        ).scalars().first()
        if current_fact is not None:
            if (current_fact.value_text, current_fact.value_num, current_fact.value_unit) == (
                row.value_text, row.value_num, row.value_unit
            ):  # fmt: skip
                return  # the user changed it to exactly this meanwhile: nothing new to version
            await session.execute(update(CoreFact).where(CoreFact.id == current_fact.id).values(valid_to=func.now()))
        session.add(
            CoreFact(
                user_id=row.user_id,
                persona_id=None,
                category=row.category or FactCategory.other,
                key=row.key,
                value_text=row.value_text,
                value_num=row.value_num,
                value_unit=row.value_unit,
                value_json=row.value_json,
                priority=row.priority if row.priority is not None else DEFAULT_FACT_PRIORITY,
                source=FactSource.approved_review,
                source_pending_id=row.id,
            )
        )
        await session.flush()

    async def _approve_emotion(self, session, row: PendingFact) -> Literal["approved", "expired"]:
        now = (await session.execute(select(func.now()))).scalar_one()
        if row.emotion_expires_at is None or row.emotion_expires_at <= now:
            await self._set_status(session, row, PendingStatus.expired)  # a state that already ended cannot be confirmed
            return "expired"
        await self._set_status(session, row, PendingStatus.approved)
        state = EmotionalState(
            user_id=row.user_id,
            label=row.emotion_label,
            description=row.emotion_description,
            intensity=row.emotion_intensity,
            priority=row.emotion_priority,
            source=EmotionSource.user_confirmed,
            mood=row.emotion_mood,  # exactly what the card showed (the database insists on it)
            source_pending_id=row.id,
            started_at=now,
            expires_at=row.emotion_expires_at,
        )
        session.add(state)
        await session.flush()
        for key in dict.fromkeys(row.emotion_override_keys or []):
            principle = (
                await session.execute(
                    select(CoreFact.id).where(
                        CoreFact.key == key, CoreFact.category == FactCategory.principle, CoreFact.valid_to.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if principle is not None:  # unknown keys are ignored: the code resolves ids, the model never does
                session.add(EmotionalOverride(user_id=row.user_id, emotional_state_id=state.id, core_fact_id=principle))
        await session.flush()
        return "approved"

    @staticmethod
    async def _close_batch_if_done(session, batch_id: uuid.UUID | None) -> bool:
        if batch_id is None:
            return False
        open_items = (
            await session.execute(
                text("SELECT count(*) FROM civ.pending_facts WHERE review_batch_id = :b AND status = 'pending'"),
                {"b": batch_id},
            )
        ).scalar_one()
        if open_items:
            return False
        result = await session.execute(
            update(ReviewBatch)
            .where(ReviewBatch.id == batch_id, ReviewBatch.closed_at.is_(None), ReviewBatch.sent_at.is_not(None))
            .values(closed_at=func.now())
        )
        return result.rowcount == 1

    # ------------------------------------------------------------ the button handler (aiogram)

    async def on_callback(self, tenant: TenantInfo, query: CallbackQuery) -> None:
        parsed = parse_callback(query.data or "")
        if parsed is None:
            await self._answer(query, "Неизвестная кнопка")
            return
        decision = await self.decide(tenant.id, *parsed)
        await self._answer(query, ANSWERS[decision.outcome])
        message = query.message
        if message is None or decision.proposal is None:
            return
        footer = FOOTERS.get(decision.outcome)
        if footer is not None:  # the card stays as a record, minus the buttons
            try:
                await self.bot.edit_message_text(
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    text=f"{render_card(decision.proposal, tenant.timezone)}\n\n{footer}",
                )
            except TelegramAPIError:
                log.warning("could not edit the review card of tenant %s", tenant.id, exc_info=True)
        if decision.batch_closed:
            try:
                await self.bot.send_message(
                    chat_id=message.chat.id, text=ALL_DONE, message_thread_id=getattr(message, "message_thread_id", None)
                )
            except TelegramAPIError:
                log.warning("could not send the review closing note to tenant %s", tenant.id, exc_info=True)

    @staticmethod
    async def _answer(query: CallbackQuery, text_: str) -> None:
        try:
            await query.answer(text_)
        except TelegramAPIError:
            log.warning("could not answer a callback query", exc_info=True)

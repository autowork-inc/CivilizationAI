"""Spaced repetition: multiple-choice flashcards that come back by the SM-2 schedule.

Two halves, and the model only touches the first:

  * ``create_cards(deck, cards)``: offered only when the user's OWN message asks for it ("сделай карточки", "создай тест по теме", "запомнить это",
    also as a caption of a document: the offer depends on the words of the user, not on the attachment). The check is a regex in code. Every
    card is validated by code (``parse_cards``: a non-empty question, EXACTLY four different options, ``correct_index`` 0-3) and the database
    refuses anything else (migration 0014). What the user reads about it is a fixed text written by code; the model's own claim is never sent.
  * the schedule: a Beat task visits the tenants that have a card due (``civ.cards_due_tenants``), and ``Cards.send_due`` claims at most
    ``SR_PER_TICK`` cards (outside the tenant's quiet hours, at most ``SR_DAILY_LIMIT`` per local day, never more than ``SR_MAX_UNANSWERED`` waiting
    for an answer) and sends each as a message with four buttons ``sr:<card id>:<option index>``. A press is ONE transaction under ``FOR UPDATE``:
    the answer is graded by code against ``correct_index``, SM-2 gives the next interval, and the card is edited into the full analysis
    (the explanation and when it comes back). A press answers only a card the bot has SENT and that has not been answered since; a second
    press changes nothing.

The clock of every comparison between rows is the database's (``now()``): the sender and the answerer may run on different machines.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from zoneinfo import ZoneInfo

from ai_civ.bot import diplomat, personas, texts, xp
from ai_civ.bot.proactive import in_quiet_hours
from ai_civ.bot.tools.base import Offered, ToolContext, ToolResult
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import CardState, GroupState, OnboardingState
from ai_civ.db.models import Card, CardReview, User
from ai_civ.memory.llm import ToolCall, ToolSpec

log = logging.getLogger(__name__)

TOOL_NAME = "create_cards"
DEFAULT_DECK = "Общее"
OPTIONS = 4
MAX_QUESTION_CHARS = 400
MAX_OPTION_CHARS = 100
MAX_EXPLANATION_CHARS = 600
LETTERS = "ABCD"

# ---------------------------------------------------------------- the marker check (code, not prompt)

_MARKERS = re.compile(
    r"(?:сдела|созда|состав|подготов|накида|сгенери)\w*\W+(?:\w+\W+){0,3}?(?:карточк|флеш|тест(?:ы|а|у|ов)?(?!\w)|квиз|викторин)\w*"
    r"|запомнить\W+(?:\w+\W+){0,2}?(?:это|эти|эту|этот|материал|тему|слова|определени)\w*"
    r"|flash\W?cards?"
    r"|quiz\W+me"
    r"|(?:make|create)\W+(?:\w+\W+){0,2}?(?:quiz|cards?)(?!\w)",
    re.IGNORECASE,
)


def wants_cards(message: str) -> bool:
    """True when the USER's own words ask for flashcards / a quiz. ``ё`` is folded into ``е``."""
    return _MARKERS.search(message.replace("ё", "е").replace("Ё", "Е")) is not None


# ---------------------------------------------------------------- the function the model sees

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Make multiple-choice flashcards for spaced repetition from what the user asked to learn (their message, or the document they attached). "
        "Call it only because the user's message explicitly asks for cards, a quiz or to memorize something. Every card has ONE question, "
        "EXACTLY four different answer options, the index (0-3) of the single right option and a short explanation. Use only facts that are "
        "in the material or the user's words: never invent. The SYSTEM tells the user the cards were made."
    ),
    parameters={
        "type": "object",
        "properties": {
            "deck": {"type": "string", "description": f"The topic of the cards, a short name in the user's language, at most {diplomat.MAX_TITLE_CHARS} characters."},
            "cards": {
                "type": "array",
                "description": "The cards.",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": f"The question, at most {MAX_QUESTION_CHARS} characters."},
                        "options": {
                            "type": "array",
                            "description": f"Exactly {OPTIONS} different answer options, each at most {MAX_OPTION_CHARS} characters.",
                            "items": {"type": "string"},
                            "minItems": OPTIONS,
                            "maxItems": OPTIONS,
                        },
                        "correct_index": {"type": "integer", "description": "The index (0-3) of the right option in `options`."},
                        "explanation": {"type": "string", "description": f"Why that option is right, at most {MAX_EXPLANATION_CHARS} characters."},
                    },
                    "required": ["question", "options", "correct_index"],
                },
            },
        },
        "required": ["deck", "cards"],
    },
)

INSTRUCTIONS = (
    "## Flashcards\n"
    "The user asked for flashcards (or a quiz): call create_cards with the cards, built only from the material or the user's own words. Each card\n"
    "has four different options and exactly one right one; vary the position of the right option. The SYSTEM then tells the user the cards were\n"
    "made and sends them for review later. Do not list the cards, never say they were saved, and do not describe how the repetition works:\n"
    "reply in one or two sentences."
)


def render_note(created: Created) -> str:
    lines = [
        "## Flashcards (made by the system)",
        "The system has made the cards below and tells the user so itself right after your reply; they will come back for review later.",
        "Do not list them, do not say they were saved and do not explain the schedule: reply briefly to the user's message.",
    ]
    lines += [f"- {d.deck}: {d.created} new" for d in created.decks if d.created]
    return "\n".join(lines)


# ---------------------------------------------------------------- validating a call (the model proposes, the code decides)


@dataclass(frozen=True)
class CardSpec:
    question: str
    options: tuple[str, str, str, str]
    correct_index: int
    explanation: str


@dataclass(frozen=True)
class CardsSpec:
    deck: str
    cards: tuple[CardSpec, ...]


def _line(value: Any, limit: int) -> str | None:
    """One clean line, or None: not a string, empty, with control / bidi / zero-width characters, or longer than ``limit`` (never cut)."""
    if not isinstance(value, str):
        return None
    line = " ".join(value.split())  # line breaks and runs of spaces collapse first: a model often writes a question in two lines
    if personas.has_hidden_chars(line):  # what is still hidden after that (control, bidi, zero-width) is refused
        return None
    return line if line and len(line) <= limit and any(ch.isalnum() for ch in line) else None


def parse_card(raw: Any) -> CardSpec | None:
    """None = the card is unusable: no question, not exactly four different non-empty options, an index outside 0-3."""
    if not isinstance(raw, dict):
        return None
    question = _line(raw.get("question"), MAX_QUESTION_CHARS)
    options = raw.get("options")
    index = raw.get("correct_index")
    if question is None or not isinstance(options, list) or len(options) != OPTIONS:
        return None
    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < OPTIONS:
        return None
    cleaned = [_line(option, MAX_OPTION_CHARS) for option in options]
    if any(option is None for option in cleaned) or len({option.casefold() for option in cleaned if option}) != OPTIONS:
        return None
    explanation = raw.get("explanation")
    note = ""
    if isinstance(explanation, str) and not personas.has_hidden_chars(explanation):
        note = " ".join(explanation.split())[:MAX_EXPLANATION_CHARS]  # a model's comment may be cut; the question and the options never are
    return CardSpec(question, tuple(cleaned), index, note)  # type: ignore[arg-type]


def parse_cards(args: dict[str, Any], max_cards: int) -> CardsSpec | None:
    """None = no usable card at all. Unusable cards are dropped one by one (the rest stay); at most ``max_cards`` are kept."""
    raw_cards = args.get("cards")
    if not isinstance(raw_cards, list):
        return None
    deck = diplomat.clean_title(args.get("deck")) or DEFAULT_DECK
    cards: list[CardSpec] = []
    seen: set[str] = set()
    for raw in raw_cards:
        card = parse_card(raw)
        if card is None or card.question.casefold() in seen:
            continue
        seen.add(card.question.casefold())
        cards.append(card)
        if len(cards) == max_cards:
            break
    return CardsSpec(deck, tuple(cards)) if cards else None


# ---------------------------------------------------------------- SM-2

MIN_EASE, MAX_EASE, MAX_INTERVAL = 1.3, 3.5, 365
QUALITY_RIGHT, QUALITY_WRONG = 4, 1  # a multiple-choice answer is either right (SM-2 grade 4) or wrong (grade 1)


@dataclass(frozen=True)
class Sm2:
    state: str
    interval_days: int
    ease: float
    reps: int  # consecutive correct answers
    lapses: int


NEW_CARD = Sm2(CardState.new.value, 0, 2.5, 0, 0)


def sm2_next(cur: Sm2, correct: bool) -> Sm2:
    """The SuperMemo-2 step. Wrong: the repetitions start over, the card comes back tomorrow. Right: 1 day, then 6, then ``interval * ease``.
    The ease follows the original formula for grade 4 (unchanged) and grade 1 (-0.54), never below 1.3."""
    q = QUALITY_RIGHT if correct else QUALITY_WRONG
    ease = round(min(MAX_EASE, max(MIN_EASE, cur.ease + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)))), 2)
    if not correct:
        return Sm2(CardState.learning.value, 1, ease, 0, cur.lapses + 1)
    reps = cur.reps + 1
    if reps == 1:
        interval = 1
    elif reps == 2:
        interval = 6
    else:
        interval = max(cur.interval_days + 1, int(cur.interval_days * ease + 0.5))
    state = CardState.review.value if reps >= 2 else CardState.learning.value
    return Sm2(state, min(interval, MAX_INTERVAL), ease, reps, cur.lapses)


# ---------------------------------------------------------------- the card (what the user reads: written by code)

CALLBACK_PREFIX = "sr"
CALLBACK_RE = re.compile(r"^sr:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):([0-3])$")
BUTTON_CHARS = 40  # a long option is shortened on its button only; the message always shows it in full
ALERT_LIMIT = 200  # Telegram's limit of an answerCallbackQuery text


def callback_data(card_id: uuid.UUID, option: int) -> str:
    return f"{CALLBACK_PREFIX}:{card_id}:{option}"  # 41 bytes of Telegram's 64


def parse_callback(data: str) -> tuple[uuid.UUID, int] | None:
    match = CALLBACK_RE.match(data)
    return None if match is None else (uuid.UUID(match.group(1)), int(match.group(2)))


def question_text(deck: str, question: str, options: Sequence[str]) -> str:
    lines = [texts.CARD_HEADER.format(deck=deck), "", question, ""]
    lines += [f"{LETTERS[i]}) {option}" for i, option in enumerate(options)]
    return "\n".join(lines)


def question_markup(card_id: uuid.UUID, options: Sequence[str]) -> InlineKeyboardMarkup:
    rows = []
    for i, option in enumerate(options):
        label = option if len(option) <= BUTTON_CHARS else option[: BUTTON_CHARS - 1] + "…"
        rows.append([InlineKeyboardButton(text=f"{LETTERS[i]}) {label}", callback_data=callback_data(card_id, i))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dataclass(frozen=True)
class Analysis:
    """Everything the post-answer card shows, as the database stored it."""

    deck: str
    question: str
    options: tuple[str, ...]
    correct_index: int
    explanation: str
    chosen: int
    interval_days: int


def when_back(days: int) -> str:
    return texts.CARD_BACK_TOMORROW if days <= 1 else texts.CARD_BACK_DAYS.format(days=days)


def analysis_text(a: Analysis) -> str:
    right = a.chosen == a.correct_index
    lines = [f"{'✅' if right else '❌'} {a.deck}", "", a.question, ""]
    for i, option in enumerate(a.options):
        mark = "✅" if i == a.correct_index else ("❌" if i == a.chosen else "▫️")
        lines.append(f"{mark} {LETTERS[i]}) {option}")
    if a.explanation:
        lines += ["", f"💡 {a.explanation}"]
    lines += ["", texts.CARD_BACK.format(when=when_back(a.interval_days))]
    return "\n".join(lines)


def alert_text(correct: bool) -> str:
    return (texts.CARD_RIGHT if correct else texts.CARD_WRONG)[:ALERT_LIMIT]


# ---------------------------------------------------------------- the engine


@dataclass(frozen=True)
class CreatedDeck:
    deck: str
    created: int
    duplicates: int


@dataclass(frozen=True)
class Created:
    decks: tuple[CreatedDeck, ...]
    full: bool = False  # the per-user cap stopped (some of) the cards


@dataclass(frozen=True)
class DueCard:
    review_id: uuid.UUID
    card_id: uuid.UUID
    deck: str
    question: str
    options: tuple[str, ...]
    previous_sent_at: datetime | None  # to give the claim back if the send fails


@dataclass(frozen=True)
class Claim:
    chat_id: int
    cards: tuple[DueCard, ...]


@dataclass(frozen=True)
class Answered:
    outcome: str  # correct | wrong | settled | gone
    analysis: Analysis | None = None


class Cards:
    def __init__(self, db: Database, bot: Bot, settings: Settings) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings

    # ------------------------------------------------------------ creating

    async def create(self, tenant_id: uuid.UUID, calls: Sequence[ToolCall]) -> Created | None:
        """The cards made for these calls (at most ``SR_MAX_CARDS_PER_CALL`` in all), or None when nothing usable came. Never raises (except
        cancellation): an unusable call and a database failure just produce no cards (logged)."""
        budget = self.settings.sr_max_cards_per_call
        specs: list[CardsSpec] = []
        for call in calls:
            if call.name != TOOL_NAME or budget <= 0:
                continue
            try:
                spec = parse_cards(call.args, budget)
            except Exception:  # noqa: BLE001 - a malformed call must not break the reply
                log.warning("create_cards: unreadable arguments", exc_info=True)
                spec = None
            if spec is None:
                log.info("create_cards: dropped a call without a usable card")
                continue
            budget -= len(spec.cards)
            specs.append(spec)
        if not specs:
            return None
        try:
            return await asyncio.wait_for(self._insert(tenant_id, specs), timeout=self.settings.diplomat_timeout_seconds)
        except Exception:  # noqa: BLE001 - no cards, no announcement: the reply still goes out
            log.warning("create_cards: could not save the cards of tenant %s", tenant_id, exc_info=True)
            return None

    async def _insert(self, tenant_id: uuid.UUID, specs: list[CardsSpec]) -> Created | None:
        decks: list[CreatedDeck] = []
        full = False
        async with self.db.tenant_session(tenant_id) as session:
            room = self.settings.sr_max_cards - (await session.execute(select(func.count()).select_from(Card))).scalar_one()
            for spec in specs:
                made = duplicates = 0
                for card in spec.cards:
                    if room <= 0:
                        full = True
                        break
                    card_id = (
                        await session.execute(
                            pg_insert(Card)
                            .values(
                                user_id=tenant_id,
                                deck=spec.deck,
                                question=card.question,
                                options=list(card.options),
                                correct_index=card.correct_index,
                                explanation=card.explanation,
                            )
                            .on_conflict_do_nothing(index_elements=[Card.user_id, Card.deck, Card.question])
                            .returning(Card.id)
                        )
                    ).scalar_one_or_none()
                    if card_id is None:
                        duplicates += 1
                        continue
                    await session.execute(pg_insert(CardReview).values(user_id=tenant_id, card_id=card_id))  # state new, due now
                    made += 1
                    room -= 1
                decks.append(CreatedDeck(spec.deck, made, duplicates))
        if not any(d.created or d.duplicates for d in decks) and not full:
            return None  # nothing was made and nothing was already there: there is nothing to announce
        return Created(tuple(decks), full)

    # ------------------------------------------------------------ sending what is due

    async def claim_due(self, tenant_id: uuid.UUID, *, now: datetime | None = None) -> Claim | None:
        """Which cards go to this tenant RIGHT NOW? ONE transaction under ``FOR UPDATE`` of the tenant's row, and the claim is written in it
        (``last_sent_at``), so overlapping ticks and retries cannot send one card twice. Nothing while onboarding is not done, the group is not
        active or the local clock is inside quiet hours; at most ``SR_DAILY_LIMIT`` cards per local day, ``SR_PER_TICK`` per tick, and none while
        ``SR_MAX_UNANSWERED`` are waiting for an answer."""
        s = self.settings
        now = now or datetime.now(UTC)
        async with self.db.tenant_session(tenant_id) as session:
            user = (
                await session.execute(
                    select(User.timezone, User.onboarding_state, User.group_state, User.quiet_hours_start, User.quiet_hours_end, User.forum_chat_id)
                    .where(User.id == tenant_id)
                    .with_for_update()
                )
            ).one_or_none()
            if user is None or user.forum_chat_id is None:
                return None
            if OnboardingState(user.onboarding_state) is not OnboardingState.done or GroupState(user.group_state) is not GroupState.active:
                return None
            local_now = now.astimezone(ZoneInfo(user.timezone))
            if in_quiet_hours(local_now.time(), user.quiet_hours_start, user.quiet_hours_end):
                return None
            midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
            sent_today = (await session.execute(select(func.count()).select_from(CardReview).where(CardReview.last_sent_at >= midnight))).scalar_one()
            waiting = (
                await session.execute(
                    select(func.count()).select_from(CardReview).where(
                        CardReview.last_sent_at.is_not(None), or_(CardReview.reviewed_at.is_(None), CardReview.last_sent_at > CardReview.reviewed_at)
                    )
                )
            ).scalar_one()
            room = min(s.sr_daily_limit - sent_today, s.sr_per_tick, s.sr_max_unanswered - waiting)
            if room <= 0:
                return None
            db_now = (await session.execute(select(func.now()))).scalar_one()
            rows = (
                await session.execute(
                    select(CardReview, Card)
                    .join(Card, and_(Card.user_id == CardReview.user_id, Card.id == CardReview.card_id))
                    .where(
                        CardReview.next_review_at <= db_now,
                        or_(
                            CardReview.last_sent_at.is_(None),
                            CardReview.last_sent_at < CardReview.next_review_at,
                            CardReview.last_sent_at < db_now - timedelta(hours=s.sr_resend_hours),
                        ),
                    )
                    .order_by(CardReview.next_review_at, CardReview.id)
                    .limit(room)
                    .with_for_update(of=CardReview, skip_locked=True)
                )
            ).all()
            if not rows:
                return None
            due = tuple(DueCard(r.id, c.id, c.deck, c.question, tuple(c.options), r.last_sent_at) for r, c in rows)
            await session.execute(update(CardReview).where(CardReview.id.in_([d.review_id for d in due])).values(last_sent_at=db_now))
            return Claim(user.forum_chat_id, due)

    async def release(self, tenant_id: uuid.UUID, cards: Sequence[DueCard]) -> None:
        """Give claims back (the send failed): the cards are due again on the next tick."""
        async with self.db.tenant_session(tenant_id) as session:
            for card in cards:
                await session.execute(update(CardReview).where(CardReview.id == card.review_id).values(last_sent_at=card.previous_sent_at))

    async def send_due(self, tenant_id: uuid.UUID, *, now: datetime | None = None) -> int:
        """Claim and send. Fail-fast: the first failed send gives back its claim and the ones after it, then raises (the Celery task retries);
        the cards already sent stay sent. At-least-once: a crash between a send and its commit cannot lose a card, only repeat it."""
        claim = await self.claim_due(tenant_id, now=now)
        if claim is None:
            return 0
        for index, card in enumerate(claim.cards):
            try:
                await self.bot.send_message(
                    chat_id=claim.chat_id,
                    text=question_text(card.deck, card.question, card.options),
                    reply_markup=question_markup(card.card_id, card.options),
                )
            except BaseException:
                await self.release(tenant_id, claim.cards[index:])
                raise
        return len(claim.cards)

    # ------------------------------------------------------------ the buttons

    async def press(self, tenant_id: uuid.UUID, card_id: uuid.UUID, option: int) -> Answered:
        """``_press`` plus the XP of a right answer: awarded only AFTER the answer has committed (``card:<id>:<tenant-local date>``)."""
        result = await self._press(tenant_id, card_id, option)
        if result.outcome == "correct":
            await xp.award(self.db, tenant_id, xp.CARD_CATEGORY, xp.card_ref(card_id), daily=True)
        return result

    async def _press(self, tenant_id: uuid.UUID, card_id: uuid.UUID, option: int) -> Answered:
        """ONE transaction under ``FOR UPDATE`` of the card's review row. Raises on a database failure: nothing was changed then."""
        async with self.db.tenant_session(tenant_id) as session:
            row = (
                await session.execute(
                    select(CardReview, Card)
                    .join(Card, and_(Card.user_id == CardReview.user_id, Card.id == CardReview.card_id))
                    .where(CardReview.card_id == card_id)
                    .with_for_update(of=CardReview)
                )
            ).one_or_none()
            if row is None:
                return Answered("gone")
            review, card = row
            if review.last_sent_at is None:  # the bot never sent it: a forged button
                return Answered("gone")
            awaiting = review.reviewed_at is None or review.last_sent_at > review.reviewed_at
            if not awaiting:
                shown = Analysis(card.deck, card.question, tuple(card.options), card.correct_index, card.explanation, review.last_chosen or 0, review.interval_days)
                return Answered("settled", shown)
            correct = option == card.correct_index
            nxt = sm2_next(Sm2(review.state, review.interval_days, float(review.ease), review.reps, review.lapses), correct)
            now = (await session.execute(select(func.now()))).scalar_one()
            review.state, review.interval_days, review.ease = nxt.state, nxt.interval_days, Decimal(str(nxt.ease))
            review.reps, review.lapses = nxt.reps, nxt.lapses
            review.reviewed_at, review.last_chosen = now, option
            review.next_review_at = now + timedelta(days=nxt.interval_days)
            shown = Analysis(card.deck, card.question, tuple(card.options), card.correct_index, card.explanation, option, nxt.interval_days)
            return Answered("correct" if correct else "wrong", shown)

    async def on_callback(self, tenant: Any, query: CallbackQuery) -> None:
        parsed = parse_callback(query.data or "")
        if parsed is None:
            await self._answer(query, texts.PROPOSAL_UNKNOWN_BUTTON)
            return
        result = await self.press(tenant.id, *parsed)
        if result.outcome == "gone":
            await self._answer(query, texts.CARD_GONE)
            return
        if result.outcome == "settled":
            await self._answer(query, texts.CARD_SETTLED)
        else:
            await self._answer(query, alert_text(result.outcome == "correct"), alert=True)
        message = query.message
        if message is None or result.analysis is None:
            return
        try:
            await self.bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id, text=analysis_text(result.analysis))
        except TelegramAPIError:  # includes "message is not modified" (a double click); the answer is already stored
            log.info("could not edit the card of tenant %s", tenant.id, exc_info=True)

    @staticmethod
    async def _answer(query: CallbackQuery, text_: str, *, alert: bool = False) -> None:
        try:
            await query.answer(text_, show_alert=alert)
        except TelegramAPIError:
            log.warning("could not answer a callback query", exc_info=True)


# ---------------------------------------------------------------- the tool


def created_text(created: Created) -> str:
    lines = []
    for deck in created.decks:
        if deck.created or deck.duplicates:
            line = texts.CARDS_MADE.format(deck=deck.deck, count=deck.created)
            lines.append(line + (texts.CARDS_DUPLICATES.format(count=deck.duplicates) if deck.duplicates else ""))
    if created.full:
        lines.append(texts.CARDS_FULL)
    return "\n".join(lines)


class CardsTool:
    """Offered only when the user's own message asks for cards (``wants_cards``). ``untrusted_ok``: it may be offered on a reply built from an
    attachment (the point of "make cards from this document"), because the offer depends on the user's words and the effect, a validated card
    that the user can see and that is only ever SENT as a quiz, is harmless whatever the document says."""

    name = "cards"
    functions = frozenset({TOOL_NAME})
    offer_rank = 45
    untrusted_ok = True

    def __init__(self, engine: Cards, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        turn = ctx.turn
        if not self.settings.sr_enabled or turn is None or not wants_cards(turn.text):
            return None
        tenant = ctx.tenant

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            created = await self.engine.create(tenant.id, calls)
            if created is None:
                return None
            return ToolResult(notes=(render_note(created),), phrase=True, text=created_text(created) or None)

        return Offered((TOOL,), INSTRUCTIONS, run, run_rank=45)

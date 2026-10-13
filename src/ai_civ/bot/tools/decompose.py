"""Task decomposition: when the user EXPLICITLY asks to break a goal into steps ("декомпозируй", "разбей на шаги", "сделай план", "распиши задачу")
the model may call ``decompose_task(goal, steps)``. The result is a root task with its steps (``civ.tasks``, migration 0013) and ONE card, written by
code, with a button row per open step: ✅ done, 🚫 dropped and, for a step that has a planned time, 📅 "put a slot into the calendar".

The division of labour is the one of every tool here (the model proposes, the code decides):
  * the function is OFFERED only when the user's own message contains a marker. The check is a regex in code (``wants_decomposition``), not a
    sentence in the prompt: a message without a marker never gives the model the function at all;
  * every argument is validated (``parse_plan``): titles are one clean line, 2..``TASKS_MAX_STEPS`` steps, an optional step time is the user's
    wall clock that CODE turns into an instant (a time that has passed or lies beyond a year is dropped, the step stays without a time);
  * what the user reads about it is the card; the words the model wrote next to the call are never sent (the reply is phrased again with a note
    that says what the system did), and the card is not stored in ``messages``;
  * a slot is never made silently: 📅 creates an ``event_drafts`` row (``task_id`` set) and the existing "put it into the calendar?" card
    (``qe:`` buttons); only that button writes the event, and ``QuickEvents.decide`` then links it to the step (``task_events``).

A press is ONE transaction under ``FOR UPDATE`` of the step (then its root): idempotent, a second press shows what was decided. The root closes
by itself when its last step is closed (``done`` if at least one step was done, otherwise ``dropped``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ.bot import diplomat, quick_events, texts, xp
from ai_civ.bot.tools.base import Card, Offered, ToolContext, ToolResult
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import DraftStatus, TaskSource, TaskStatus
from ai_civ.db.models import EventDraft, Task, TaskEvent
from ai_civ.memory.llm import ToolCall, ToolSpec

log = logging.getLogger(__name__)

TOOL_NAME = "decompose_task"
MIN_STEPS = 2
SLOT_MINUTES = diplomat.DEFAULT_MINUTES  # a step has no duration of its own: its slot is the default one

# ---------------------------------------------------------------- the marker check (code, not prompt)

_MARKERS = re.compile(
    r"декомпоз\w*"
    r"|разб(?:е[йи]|ить)\w*\W+(?:\w+\W+){0,4}?на\W+(?:шаг|этап|подзадач|част|пункт)\w*"
    r"|сдела(?:й|ть)\w*\W+(?:мне\W+|нам\W+)?(?:\w+\W+){0,2}?план\w*"
    r"|распиши\w*\W+(?:\w+\W+){0,4}?(?:задач|шаг|этап|пункт)\w*"
    r"|распиши\w*\W+(?:\w+\W+){0,3}?по\W+(?:шаг|этап|пункт)\w*"
    r"|decompose\w*"
    r"|break\W+(?:it\W+|this\W+|that\W+)?down\b",
    re.IGNORECASE,
)


def wants_decomposition(message: str) -> bool:
    """True when the USER's own words ask for a breakdown. ``ё`` is folded into ``е`` so that "разбёй" and "разбей" are one word."""
    return _MARKERS.search(message.replace("ё", "е").replace("Ё", "Е")) is not None


# ---------------------------------------------------------------- the function the model sees

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Break ONE goal that the user asked you to decompose into concrete steps. Call it only because the user's message explicitly asks "
        "for a breakdown or a plan of a task. The SYSTEM shows the user the list as a card with buttons: nothing else is needed from you. "
        f"Give {MIN_STEPS}-10 short, concrete, ordered steps. Put a `start` on a step ONLY when the user named a time for it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": f"The task being broken down, a short phrase in the user's language, at most {diplomat.MAX_TITLE_CHARS} characters."},
            "steps": {
                "type": "array",
                "description": "The steps in the order they should be done.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": f"One concrete action, a short phrase, at most {diplomat.MAX_TITLE_CHARS} characters."},
                        "start": {
                            "type": "string",
                            "description": "OPTIONAL. Only if the user named a time for this step: the USER's local date and time, 24-hour, "
                            "format YYYY-MM-DDTHH:MM, no time zone suffix. Never invent a time.",
                        },
                    },
                    "required": ["title"],
                },
            },
        },
        "required": ["goal", "steps"],
    },
)

INSTRUCTIONS = (
    "## Task breakdown\n"
    "The user asked you to break a task into steps: call decompose_task with the goal and the steps. The SYSTEM then sends the list under\n"
    "your reply as a card with buttons for marking steps done or dropped. Do not write the steps out yourself, never say the list was saved\n"
    "or recorded, and do not describe the buttons: reply briefly (one or two sentences), for example what the first step is."
)


def render_note(plan: TaskPlan) -> str:
    lines = [
        "## Task breakdown (made by the system)",
        "The system has broken the task down and shows the user the list as a card with buttons right under your reply.",
        "Do not repeat the steps, do not say it was saved or recorded and do not mention the buttons: reply briefly to the user's message.",
        f"Task: {plan.goal}",
    ]
    lines += [f"- {step.title}" for step in plan.steps]
    return "\n".join(lines)


# ---------------------------------------------------------------- validating a call


@dataclass(frozen=True)
class StepSpec:
    title: str
    due: datetime | None  # aware, UTC


@dataclass(frozen=True)
class PlanSpec:
    goal: str
    steps: tuple[StepSpec, ...]


def parse_plan(args: dict[str, Any], timezone: str, now: datetime, max_steps: int) -> PlanSpec | None:
    """None = the call is unusable (no goal, fewer than two usable steps): dropped silently - the user asked for a plan, but a half-plan the
    model garbled is worse than none (the reply is phrased again and says it could not)."""
    goal = diplomat.clean_title(args.get("goal"))
    raw_steps = args.get("steps")
    if goal is None or not isinstance(raw_steps, list):
        return None
    zone = ZoneInfo(timezone)
    steps: list[StepSpec] = []
    seen: set[str] = set()
    for raw in raw_steps:
        if isinstance(raw, str):
            raw = {"title": raw}
        if not isinstance(raw, dict):
            continue
        title = diplomat.clean_title(raw.get("title"))
        if title is None or title.casefold() in seen:
            continue
        seen.add(title.casefold())
        due = diplomat.parse_start(raw.get("start"), zone) if raw.get("start") is not None else None
        if due is not None and not now + diplomat.MIN_LEAD <= due <= now + diplomat.MAX_LEAD:
            due = None  # a time that has passed (or is absurdly far) is not a plan: the step stays, without a time
        steps.append(StepSpec(title, due))
        if len(steps) == max_steps:
            break
    if len(steps) < MIN_STEPS:
        return None
    return PlanSpec(goal, tuple(steps))


# ---------------------------------------------------------------- the card (what the user reads: written by code)

CALLBACK_PREFIX = "tk"
CALLBACK_RE = re.compile(r"^tk:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}):(d|x|p)$")
ACTIONS = {"d": "done", "x": "dropped", "p": "slot"}


def callback_data(step_id: uuid.UUID, action: str) -> str:
    return f"{CALLBACK_PREFIX}:{step_id}:{action}"  # 41 bytes of Telegram's 64


def parse_callback(data: str) -> tuple[uuid.UUID, str] | None:
    match = CALLBACK_RE.match(data)
    return None if match is None else (uuid.UUID(match.group(1)), match.group(2))


@dataclass(frozen=True)
class StepView:
    id: uuid.UUID
    position: int
    title: str
    status: str
    due: datetime | None
    planned: bool  # a calendar event is linked to the step (the user confirmed the slot)


@dataclass(frozen=True)
class TaskPlan:
    root_id: uuid.UUID
    goal: str
    status: str
    steps: tuple[StepView, ...]


_STEP_MARK = {TaskStatus.open.value: "◻️", TaskStatus.done.value: "✅", TaskStatus.dropped.value: "🚫"}
_ROOT_MARK = {TaskStatus.open.value: "📋", TaskStatus.done.value: "✅", TaskStatus.dropped.value: "🚫"}


def card_text(plan: TaskPlan, timezone: str, now: datetime | None = None) -> str:
    done = sum(1 for s in plan.steps if s.status == TaskStatus.done.value)
    lines = [texts.TASK_HEADER.format(mark=_ROOT_MARK.get(plan.status, "📋"), goal=plan.goal, done=done, total=len(plan.steps))]
    for step in plan.steps:
        line = f"{_STEP_MARK.get(step.status, '◻️')} {step.position + 1}. {step.title}"
        if step.due is not None:
            when = texts.when_text(step.due, step.due + timedelta(minutes=SLOT_MINUTES), timezone, now)
            line += f" — {when}{' 📅' if step.planned else ''}"
        lines.append(line)
    return "\n".join(lines)


def _slot_offered(step: StepView, now: datetime) -> bool:
    return step.status == TaskStatus.open.value and step.due is not None and step.due > now and not step.planned


def card_markup(plan: TaskPlan, now: datetime | None = None) -> InlineKeyboardMarkup | None:
    """One row per OPEN step; a closed step has no buttons. None = nothing left to press (the keyboard is removed)."""
    now = now or datetime.now(UTC)
    rows: list[list[InlineKeyboardButton]] = []
    for step in plan.steps:
        if step.status != TaskStatus.open.value:
            continue
        n = step.position + 1
        row = [
            InlineKeyboardButton(text=texts.TASK_DONE.format(n=n), callback_data=callback_data(step.id, "d")),
            InlineKeyboardButton(text=texts.TASK_DROP.format(n=n), callback_data=callback_data(step.id, "x")),
        ]
        if _slot_offered(step, now):
            row.append(InlineKeyboardButton(text=texts.TASK_SLOT.format(n=n), callback_data=callback_data(step.id, "p")))
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


# ---------------------------------------------------------------- the engine


@dataclass(frozen=True)
class Pressed:
    outcome: str  # done | dropped | settled | slot | waiting | planned | no_slot | limit | gone
    plan: TaskPlan | None = None
    draft: quick_events.Draft | None = None  # outcome == "slot": the draft whose "put it into the calendar?" card must be sent


class Tasks:
    def __init__(self, db: Database, bot: Bot, settings: Settings) -> None:
        self.db = db
        self.bot = bot
        self.settings = settings

    # ------------------------------------------------------------ creating a plan

    async def create(
        self,
        tenant_id: uuid.UUID,
        timezone: str,
        persona_id: uuid.UUID | None,
        calls: Sequence[ToolCall],
        *,
        now: datetime | None = None,
    ) -> TaskPlan | None:
        """The plan made for the FIRST usable call (one breakdown per message), or None. Never raises (except cancellation): an unusable call, an
        open task with the same goal, too many open tasks and a database failure all just produce no card (logged)."""
        now = now or datetime.now(UTC)
        for call in calls:
            if call.name != TOOL_NAME:
                continue
            try:
                spec = parse_plan(call.args, timezone, now, self.settings.tasks_max_steps)
            except Exception:  # noqa: BLE001 - a malformed call must not break the reply
                log.warning("decompose_task: unreadable arguments %r", call.args, exc_info=True)
                spec = None
            if spec is None:
                log.info("decompose_task: dropped a call with unusable arguments")
                continue
            try:
                return await asyncio.wait_for(self._insert(tenant_id, persona_id, spec), timeout=self.settings.diplomat_timeout_seconds)
            except Exception:  # noqa: BLE001 - no plan, no card: the reply still goes out
                log.warning("decompose_task: could not save the plan of tenant %s", tenant_id, exc_info=True)
                return None
        return None

    async def _insert(self, tenant_id: uuid.UUID, persona_id: uuid.UUID | None, spec: PlanSpec) -> TaskPlan | None:
        async with self.db.tenant_session(tenant_id) as session:
            open_roots = (
                await session.execute(
                    select(func.count()).select_from(Task).where(Task.parent_task_id.is_(None), Task.status == TaskStatus.open.value)
                )
            ).scalar_one()
            if open_roots >= self.settings.tasks_max_open:
                log.info("decompose_task: tenant %s has %d open tasks; no new one", tenant_id, open_roots)
                return None
            root_id = (
                await session.execute(
                    pg_insert(Task)
                    .values(user_id=tenant_id, title=spec.goal, persona_id=persona_id, source=TaskSource.chat_decompose.value)
                    .on_conflict_do_nothing(
                        index_elements=[Task.user_id, Task.title],
                        index_where=text("parent_task_id IS NULL AND status = 'open'"),
                    )
                    .returning(Task.id)
                )
            ).scalar_one_or_none()
            if root_id is None:  # the same goal is already open: its card is in the chat
                log.info("decompose_task: %r is already an open task of tenant %s", spec.goal, tenant_id)
                return None
            for position, step in enumerate(spec.steps):
                session.add(
                    Task(
                        user_id=tenant_id,
                        parent_task_id=root_id,
                        position=position,
                        title=step.title,
                        due=step.due,
                        persona_id=persona_id,
                        source=TaskSource.chat_decompose.value,
                    )
                )
            await session.flush()
            return await self._load(session, root_id)

    @staticmethod
    async def _load(session: Any, root_id: uuid.UUID) -> TaskPlan:
        root = (await session.execute(select(Task).where(Task.id == root_id))).scalar_one()
        steps = (
            await session.execute(select(Task).where(Task.parent_task_id == root_id).order_by(Task.position, Task.created_at))
        ).scalars().all()
        linked = set(
            (await session.execute(select(TaskEvent.task_id).where(TaskEvent.task_id.in_([s.id for s in steps])))).scalars().all()
        ) if steps else set()
        views = tuple(StepView(s.id, s.position, s.title, s.status, s.due, s.id in linked) for s in steps)
        return TaskPlan(root.id, root.title, root.status, views)

    # ------------------------------------------------------------ the buttons

    async def press(self, tenant_id: uuid.UUID, timezone: str, step_id: uuid.UUID, action: str) -> Pressed:
        """``_press`` plus the XP of a finished step: awarded only AFTER the press has committed (idempotent by ``task:<step id>``)."""
        result = await self._press(tenant_id, timezone, step_id, action)
        if result.outcome == "done":
            await xp.award(self.db, tenant_id, xp.TASK_CATEGORY, xp.task_ref(step_id))
        return result

    async def _press(self, tenant_id: uuid.UUID, timezone: str, step_id: uuid.UUID, action: str) -> Pressed:
        """ONE transaction under ``FOR UPDATE`` (the step, then its root). Idempotent. Raises on a database failure: nothing was changed then."""
        async with self.db.tenant_session(tenant_id) as session:
            step = (await session.execute(select(Task).where(Task.id == step_id).with_for_update())).scalar_one_or_none()
            if step is None or step.parent_task_id is None:  # only steps have buttons
                return Pressed("gone")
            root = (await session.execute(select(Task).where(Task.id == step.parent_task_id).with_for_update())).scalar_one()
            now = (await session.execute(select(func.now()))).scalar_one()  # the database clock, like every expiry here
            if step.status != TaskStatus.open.value:
                return Pressed("settled", await self._load(session, root.id))
            if action in ("d", "x"):
                step.status = TaskStatus.done.value if action == "d" else TaskStatus.dropped.value
                step.updated_at = now
                await session.flush()
                siblings = (await session.execute(select(Task.status).where(Task.parent_task_id == root.id))).scalars().all()
                if TaskStatus.open.value not in siblings:
                    root.status = TaskStatus.done.value if TaskStatus.done.value in siblings else TaskStatus.dropped.value
                    root.updated_at = now
                    await session.flush()
                return Pressed(step.status, await self._load(session, root.id))
            return await self._slot(session, timezone, step, root, now)

    async def _slot(self, session: Any, timezone: str, step: Task, root: Task, now: datetime) -> Pressed:
        plan = await self._load(session, root.id)
        if step.due is None or step.due <= now:
            return Pressed("no_slot", plan)
        if next(s for s in plan.steps if s.id == step.id).planned:
            return Pressed("planned", plan)
        if (
            await session.execute(
                select(EventDraft.id).where(EventDraft.task_id == step.id, EventDraft.status == DraftStatus.pending, EventDraft.expires_at > now)
            )
        ).first() is not None:
            return Pressed("waiting", plan)  # its "put it into the calendar?" card is already in the chat
        await session.execute(  # an open draft whose time ran out must not block the same step again
            update(EventDraft).where(EventDraft.status == DraftStatus.pending, EventDraft.expires_at <= now).values(status=DraftStatus.expired, decided_at=now)
        )
        open_drafts = (await session.execute(select(func.count()).select_from(EventDraft).where(EventDraft.status == DraftStatus.pending))).scalar_one()
        if open_drafts >= self.settings.quick_events_max_pending:
            return Pressed("limit", plan)
        end = step.due + timedelta(minutes=SLOT_MINUTES)
        draft_id = (
            await session.execute(
                pg_insert(EventDraft)
                .values(
                    user_id=step.user_id,
                    persona_id=step.persona_id,
                    task_id=step.id,
                    title=step.title,
                    starts_at=step.due,
                    ends_at=end,
                    timezone=timezone,
                    expires_at=now + timedelta(hours=self.settings.quick_events_ttl_hours),
                )
                .on_conflict_do_nothing(index_elements=[EventDraft.user_id, EventDraft.title, EventDraft.starts_at], index_where=text("status = 'pending'"))
                .returning(EventDraft.id)
            )
        ).scalar_one_or_none()
        if draft_id is None:  # the same title at the same moment is already waiting (made another way): nothing new to ask
            return Pressed("waiting", plan)
        return Pressed("slot", plan, quick_events.Draft(draft_id, step.title, step.due, end, timezone))

    async def on_callback(self, tenant: Any, query: CallbackQuery) -> None:
        parsed = parse_callback(query.data or "")
        if parsed is None:
            await self._answer(query, texts.PROPOSAL_UNKNOWN_BUTTON)
            return
        result = await self.press(tenant.id, tenant.timezone, *parsed)
        await self._answer(query, texts.TASK_TOAST[result.outcome])
        message = query.message
        if message is None or result.plan is None:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                text=card_text(result.plan, tenant.timezone),
                reply_markup=card_markup(result.plan),
            )
        except TelegramAPIError:  # includes "message is not modified" (a double click); the decision is already made
            log.info("could not edit the task card of tenant %s", tenant.id, exc_info=True)
        if result.draft is not None:  # the usual "put it into the calendar?" card (qe: buttons) in the same place
            try:
                await self.bot.send_message(
                    chat_id=message.chat.id,
                    text=quick_events.card_text(result.draft, tenant.timezone),
                    reply_markup=quick_events.card_markup(result.draft.id),
                    message_thread_id=message.message_thread_id if message.is_topic_message else None,
                )
            except TelegramAPIError:  # the draft expires by itself; the step can be offered again after that
                log.warning("could not send the slot card of tenant %s", tenant.id, exc_info=True)

    @staticmethod
    async def _answer(query: CallbackQuery, text_: str) -> None:
        try:
            await query.answer(text_)
        except TelegramAPIError:
            log.warning("could not answer a callback query", exc_info=True)


# ---------------------------------------------------------------- the tool


class DecomposeTool:
    """Offered only when the user's own message asks for a breakdown (``wants_decomposition``). Never in the interview or in proactive messages
    (they do not go through ``_answer`` with a user turn), never on a message with an attachment (those replies have no tools at all)."""

    name = "decompose"
    functions = frozenset({TOOL_NAME})
    offer_rank = 25

    def __init__(self, engine: Tasks, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        turn = ctx.turn
        if not self.settings.tasks_enabled or turn is None or not wants_decomposition(turn.text):
            return None
        tenant = ctx.tenant

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            plan = await self.engine.create(tenant.id, tenant.timezone, ctx.persona_id, calls)
            if plan is None:
                return None
            card = Card(card_text(plan, tenant.timezone), card_markup(plan))
            return ToolResult(notes=(render_note(plan),), phrase=True, cards=(card,))

        return Offered((TOOL,), INSTRUCTIONS, run, run_rank=25)

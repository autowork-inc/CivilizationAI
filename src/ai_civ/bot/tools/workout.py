"""Workout tracker (v2.0, PR 4.1, migration 0018): ``log_workout`` turns a message like "жим ногами 100 кг 3х10" into a session with sets.

The division of labour is the one of every tool here (the model proposes, the code decides):
  * the function is OFFERED only when CODE finds a workout cue AND a digit in the user's own message (``wants_workout``);
  * Zero-Hallucination: every number of a set (weight, reps, how many sets, RPE, target) is accepted only if it STANDS in the user's own message
    (``stated_numbers``); an exercise with a number the user did not write is dropped as a whole, never half-logged. A voice note arrives as its
    transcript, so spoken digits work the same way (numbers spelled out in words cannot be checked and are refused);
  * names are made canonical by CODE: the user's dictionary (``civ.exercises`` + synonyms), then a built-in catalog (copied into the dictionary on
    first use), and only then a custom exercise - which needs the name to stand in the user's message;
  * what the user reads is a card written by code (the model's words next to the call are never sent), with the progressive-overload hint;
  * the hint is computed by code from the stored sets (``overload``): the two latest sessions of the exercise, at the same working weight, every working
    set reached its target reps -> add 5 kg for legs, 2.5 kg for everything else. The model never suggests a weight;
  * a logged workout pays +10 XP in ``body`` after the commit (``workout:<session id>``; the daily cap of 100 XP lives in the database).

The target of reps is what the user said ("цель 10", "по плану 3х10"): the model may pass it only when the message states it. A set without one inherits
the latest target the user stated for that exercise, so it has to be said once. Without a target there is no hint: "done" is relative to a goal.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ.bot import diplomat, texts, xp
from ai_civ.bot.tools.base import Card, Offered, ToolContext, ToolResult, UserTurn
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.models import Exercise, WorkoutSession, WorkoutSet
from ai_civ.memory.llm import ToolCall, ToolSpec

log = logging.getLogger(__name__)

TOOL_NAME = "log_workout"
GROUPS = ("chest", "back", "shoulders", "arms", "legs", "core", "other")
STEP_KG = {"legs": Decimal("5")}  # the step of the overload per muscle group
DEFAULT_STEP_KG = Decimal("2.5")
HISTORY = 2  # sessions the overload rule looks at
MAX_COUNT = 20  # identical sets one entry may stand for ("3х10" -> count 3)
MAX_NOTES_CHARS = 200

# ---------------------------------------------------------------- normalising names


def norm_key(value: str) -> str:
    """The form names are compared in: case-folded, ``ё`` = ``е``, punctuation and runs of spaces collapse into one space."""
    return " ".join(re.sub(r"[^\w]+", " ", value.replace("ё", "е").replace("Ё", "Е").casefold()).split())


@dataclass(frozen=True)
class CatalogEntry:
    name: str
    group: str
    aliases: tuple[str, ...]


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry("Жим лёжа", "chest", ("жим штанги лёжа", "bench press", "bench")),
    CatalogEntry("Жим гантелей лёжа", "chest", ("жим гантелей", "dumbbell bench press")),
    CatalogEntry("Отжимания", "chest", ("push-ups", "pushups")),
    CatalogEntry("Жим стоя", "shoulders", ("армейский жим", "жим штанги стоя", "overhead press", "military press", "ohp")),
    CatalogEntry("Жим гантелей сидя", "shoulders", ("жим гантелей на плечи", "dumbbell shoulder press")),
    CatalogEntry("Подтягивания", "back", ("pull-ups", "pullups")),
    CatalogEntry("Тяга верхнего блока", "back", ("тяга блока к груди", "lat pulldown")),
    CatalogEntry("Тяга штанги в наклоне", "back", ("тяга в наклоне", "barbell row", "bent over row")),
    CatalogEntry("Становая тяга", "legs", ("становая", "deadlift")),
    CatalogEntry("Приседания со штангой", "legs", ("приседания", "присед", "присед со штангой", "squat", "back squat")),
    CatalogEntry("Жим ногами", "legs", ("жим платформы", "жим ногами в тренажёре", "leg press")),
    CatalogEntry("Румынская тяга", "legs", ("romanian deadlift", "rdl")),
    CatalogEntry("Выпады", "legs", ("выпады с гантелями", "lunges")),
    CatalogEntry("Подъём на носки", "legs", ("икры", "calf raise")),
    CatalogEntry("Сгибание рук со штангой", "arms", ("подъём штанги на бицепс", "бицепс", "bicep curl", "biceps curl")),
    CatalogEntry("Французский жим", "arms", ("skull crushers", "french press")),
    CatalogEntry("Разгибание рук на блоке", "arms", ("трицепс на блоке", "triceps pushdown")),
    CatalogEntry("Планка", "core", ("plank",)),
    CatalogEntry("Скручивания", "core", ("пресс", "crunches")),
)
_CATALOG_INDEX: dict[str, CatalogEntry] = {}
for _entry in CATALOG:
    for _label in (_entry.name, *_entry.aliases):
        _CATALOG_INDEX.setdefault(norm_key(_label), _entry)

# ---------------------------------------------------------------- the cue check (code, not prompt)

_CUES = re.compile(
    r"тренировк|тренирова|потренир|workout|\bgym\b|\bв\s+зал[еу]?\b|\bиз\s+зала\b"
    r"|\bжим\w*|\bприсед\w*|\bстанов(?:ая|ую|ой)\b|подтягив\w*|отжим\w*|\bвыпад\w*|подход\w*|гантел\w*|штанг\w*"
    r"|\bbench\b|\bsquats?\b|\bdeadlift\b|\bleg\s*press\b"
    r"|\d\s*[xх×]\s*\d"  # the shape of "3х10" (Latin x, Cyrillic х, ×)
    r"|\d\s*(?:повтор\w*|reps?\b)",
    re.IGNORECASE,
)
_LONG_CATALOG_KEYS = tuple(key for key in _CATALOG_INDEX if len(key) >= 5)


def wants_workout(message: str) -> bool:
    """True when the USER's own message looks like a workout report: a digit AND a cue (a workout word, the shape "3х10", or a known exercise name)."""
    if not re.search(r"\d", message):
        return False
    if _CUES.search(message.replace("ё", "е").replace("Ё", "Е")) is not None:
        return True
    padded = f" {norm_key(message)} "
    return any(f" {key} " in padded for key in _LONG_CATALOG_KEYS)


# ---------------------------------------------------------------- the user's own numbers

_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def stated_numbers(user_text: str) -> set[Decimal]:
    """Every number that STANDS in the user's message. ``5,5`` = ``5.5`` = ``5.50``. A list written without spaces ("10,10,8") is read both ways: as the
    decimal it could be and as the numbers it could list - the check errs towards the user's words, never towards the model's."""
    found: set[Decimal] = set()
    for token in _NUMBER.findall(user_text):
        parts = re.split(r"[.,]", token)
        if len(parts) == 1:
            found.add(Decimal(parts[0]))
        elif len(parts) == 2:
            found.add(Decimal(f"{parts[0]}.{parts[1]}"))
            if len(parts[1]) >= 2:  # "10,10" may be a list; "62,5" is a decimal
                found.update(Decimal(p) for p in parts)
        else:
            found.update(Decimal(p) for p in parts)
    return found


def decimal_of(value: Any) -> Decimal | None:
    """A finite number the model passed (a JSON number or a plain numeric string); anything else is None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        number = Decimal(repr(value))
    elif isinstance(value, str) and re.fullmatch(r"\s*\d+(?:[.,]\d+)?\s*", value):
        number = Decimal(value.strip().replace(",", "."))
    else:
        return None
    return number if number.is_finite() else None


# ---------------------------------------------------------------- the function the model sees

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Log the workout the user just REPORTED having done (past tense: sets, weights, reps). Call it only when the message reports a finished workout "
        "or exercise, never for plans, questions or advice. EVERY number you pass (weight, reps, count, rpe, target_reps) must be written in the user's "
        "message exactly: copy them, never compute, round, convert or guess. Leave out whatever the user did not say. The SYSTEM shows the user a card "
        "with the saved workout: nothing else is needed from you."
    ),
    parameters={
        "type": "object",
        "properties": {
            "exercises": {
                "type": "array",
                "description": "The exercises of this workout, in the order the user listed them.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The exercise as the user named it, a short phrase in the user's language."},
                        "muscle_group": {"type": "string", "enum": list(GROUPS), "description": "The main muscle group of the exercise."},
                        "target_reps": {
                            "type": "number",
                            "description": "ONLY if the user stated a target or a plan for the reps of this exercise ('цель 10', 'по плану 3х10'): that number.",
                        },
                        "sets": {
                            "type": "array",
                            "description": "The sets. Identical sets are ONE entry with `count` (the '3' of '3х10').",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "weight": {"type": "number", "description": "Weight in kg as the user wrote it; leave out for body weight."},
                                    "reps": {"type": "number", "description": "Repetitions of the set, as the user wrote them."},
                                    "count": {"type": "number", "description": "How many identical sets this entry stands for, ONLY if the user wrote it."},
                                    "rpe": {"type": "number", "description": "RPE 1-10, ONLY if the user wrote it."},
                                },
                                "required": ["reps"],
                            },
                        },
                    },
                    "required": ["name", "sets"],
                },
            },
            "notes": {"type": "string", "description": "OPTIONAL. A short piece of the user's own message about how it went, copied word for word."},
        },
        "required": ["exercises"],
    },
)

INSTRUCTIONS = (
    "## Workout log\n"
    "The user may be reporting a finished workout: then call log_workout with the exercises and sets, copying every number exactly as the user wrote it\n"
    "(never invent, convert or compute one; leave out what was not said). The SYSTEM then sends the saved workout under your reply as a card, with a\n"
    "recommendation for the next weights when there is one. Do not repeat the numbers, never say it was saved or recorded, and never suggest weights\n"
    "yourself: reply briefly (one or two sentences), for example acknowledge the effort."
)


def render_note(saved: Saved) -> str:
    lines = [
        "## Workout log (made by the system)",
        "The system has saved the workout and shows the user the details (and a recommendation, when there is one) as a card right under your reply.",
        "Do not repeat the numbers, do not say it was saved or recorded, do not suggest weights or loads: reply briefly, acknowledging the effort.",
    ]
    lines += [f"- {entry.name}" for entry in saved.entries]
    return "\n".join(lines)


# ---------------------------------------------------------------- validating a call


@dataclass(frozen=True)
class SetSpec:
    weight: Decimal
    reps: int
    rpe: Decimal | None


@dataclass(frozen=True)
class ExerciseSpec:
    name: str
    group: str  # the model's word, kept only for a CUSTOM exercise
    target_reps: int | None
    sets: tuple[SetSpec, ...]


@dataclass(frozen=True)
class WorkoutSpec:
    exercises: tuple[ExerciseSpec, ...]
    notes: str


def _whole(number: Decimal | None, low: int, high: int) -> int | None:
    if number is None or number != number.to_integral_value() or not low <= number <= high:
        return None
    return int(number)


def _parse_set(raw: Any, said: set[Decimal]) -> list[SetSpec] | None:
    """None = the set is unusable (an unreadable or unstated number): the exercise it belongs to is dropped."""
    if not isinstance(raw, dict):
        return None
    reps_number = decimal_of(raw.get("reps"))
    reps = _whole(reps_number, 1, 500)
    if reps is None or reps_number not in said:
        return None
    weight = Decimal(0)
    if raw.get("weight") not in (None, 0, 0.0, "0", ""):
        parsed = decimal_of(raw.get("weight"))
        if parsed is None or not Decimal(0) < parsed <= 1000 or parsed != parsed.quantize(Decimal("0.01")) or parsed not in said:
            return None
        weight = parsed
    count = 1
    if raw.get("count") is not None:
        count_number = decimal_of(raw.get("count"))
        count = _whole(count_number, 1, MAX_COUNT) or 0
        if count == 0 or (count > 1 and count_number not in said):
            return None  # "1" is the default and needs no witness; any other count must stand in the message
    rpe = None
    if raw.get("rpe") is not None:
        rpe = decimal_of(raw.get("rpe"))
        if rpe is None or not Decimal(1) <= rpe <= 10 or rpe != rpe.quantize(Decimal("0.1")) or rpe not in said:
            return None
    return [SetSpec(weight, reps, rpe)] * count


def _hidden(value: str) -> bool:
    return re.search(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩﻿]", value) is not None


def parse_workout(args: dict[str, Any], user_text: str, max_exercises: int, max_sets: int) -> WorkoutSpec | None:
    """None = the call is unusable (no exercise survived, or more than ``max_sets`` sets): dropped silently, nothing is written. An exercise with ANY
    unreadable or unstated number is dropped as a whole (a half-logged exercise would misreport the workout; the card shows what was kept)."""
    raw_exercises = args.get("exercises")
    if not isinstance(raw_exercises, list):
        return None
    said = stated_numbers(user_text)
    kept: list[ExerciseSpec] = []
    for raw in raw_exercises:
        if not isinstance(raw, dict) or not isinstance(raw.get("sets"), list) or not raw["sets"]:
            continue
        name = diplomat.clean_title(raw.get("name"))
        if name is None:
            continue
        sets: list[SetSpec] = []
        for raw_set in raw["sets"]:
            parsed = _parse_set(raw_set, said)
            if parsed is None:
                sets = []
                break
            sets += parsed
        if not sets:
            continue
        target = None
        if raw.get("target_reps") is not None:
            target_number = decimal_of(raw.get("target_reps"))
            target = _whole(target_number, 1, 500)
            if target is None or target_number not in said:
                continue
        group = str(raw.get("muscle_group", "")).strip().lower()
        kept.append(ExerciseSpec(name, group if group in GROUPS else "other", target, tuple(sets)))
        if len(kept) == max_exercises:
            break
    if not kept or sum(len(e.sets) for e in kept) > max_sets:
        return None
    notes = ""
    raw_notes = args.get("notes")
    if isinstance(raw_notes, str):
        one_line = " ".join(raw_notes.split())
        if one_line and len(one_line) <= MAX_NOTES_CHARS and not _hidden(one_line) and norm_key(one_line) in norm_key(user_text):
            notes = one_line  # only the user's own words are kept as the note
    return WorkoutSpec(tuple(kept), notes)


# ---------------------------------------------------------------- progressive overload (code, never the model)


@dataclass(frozen=True)
class LoggedSet:
    weight: Decimal
    reps: int
    target_reps: int | None


@dataclass(frozen=True)
class Overload:
    weight: Decimal  # the working weight of both sessions
    step: Decimal
    next_weight: Decimal


def overload(sessions: Sequence[Sequence[LoggedSet]], group: str) -> Overload | None:
    """``sessions`` = the sets of the latest sessions of ONE exercise, newest first. A recommendation exists only if the ``HISTORY`` latest sessions
    were at the same working weight (the heaviest of the session; a lighter set is a warm-up) and EVERY working set of both reached the target reps
    (a set without a target does not count as done). Then: +5 kg for legs, +2.5 kg for any other group."""
    if len(sessions) < HISTORY:
        return None
    weights: set[Decimal] = set()
    for sets in sessions[:HISTORY]:
        if not sets:
            return None
        top = max(s.weight for s in sets)
        working = [s for s in sets if s.weight == top]
        if top <= 0 or any(s.target_reps is None or s.reps < s.target_reps for s in working):
            return None
        weights.add(top)
    if len(weights) != 1:
        return None
    (weight,) = weights
    step = STEP_KG.get(group, DEFAULT_STEP_KG)
    return Overload(weight, step, weight + step)


# ---------------------------------------------------------------- the card (what the user reads: written by code)


@dataclass(frozen=True)
class SavedExercise:
    name: str
    group: str
    sets: tuple[SetSpec, ...]
    hint: Overload | None


@dataclass(frozen=True)
class Saved:
    session_id: uuid.UUID
    entries: tuple[SavedExercise, ...]
    notes: str


def fmt(number: Decimal) -> str:
    """Russian style: ``2,5``, ``60`` (no trailing zeros)."""
    return format(number.normalize(), "f").replace(".", ",")


def _set_text(item: SetSpec) -> str:
    base = f"{fmt(item.weight)}×{item.reps}" if item.weight > 0 else f"{item.reps} повт."
    return f"{base} @{fmt(item.rpe)}" if item.rpe is not None else base


def card_text(saved: Saved, xp_code: str | None = None) -> str:
    lines = [texts.WORKOUT_HEADER]
    for entry in saved.entries:
        lines.append(f"• {entry.name}: " + ", ".join(_set_text(item) for item in entry.sets))
    if saved.notes:
        lines.append(f"📝 {saved.notes}")
    for entry in saved.entries:
        if entry.hint is not None:
            lines.append(
                texts.WORKOUT_HINT.format(
                    name=entry.name, sessions=HISTORY, inc=fmt(entry.hint.step), now=fmt(entry.hint.weight), next=fmt(entry.hint.next_weight)
                )
            )
    if xp_code == "awarded":
        lines.append(texts.WORKOUT_XP.format(amount=xp.AWARD))
    return "\n".join(lines)


# ---------------------------------------------------------------- the engine


class Workouts:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    async def log(self, tenant_id: uuid.UUID, turn: UserTurn, calls: Sequence[ToolCall]) -> tuple[Saved, str] | None:
        """``(what was saved, the XP code)`` for the FIRST usable call (one workout per message), or None. Never raises (except cancellation): an unusable
        call, a replayed message, the daily limit and a database failure all just produce no card (logged)."""
        for call in calls:
            if call.name != TOOL_NAME:
                continue
            try:
                spec = parse_workout(call.args, turn.text, self.settings.workout_max_exercises, self.settings.workout_max_sets)
            except Exception:  # noqa: BLE001 - a malformed call must not break the reply
                log.warning("log_workout: unreadable arguments %r", call.args, exc_info=True)
                spec = None
            if spec is None:
                log.info("log_workout: dropped a call with unusable arguments")
                continue
            try:
                saved = await asyncio.wait_for(self._save(tenant_id, turn, spec), timeout=self.settings.workout_timeout_seconds)
            except Exception:  # noqa: BLE001 - no workout, no card: the reply still goes out
                log.warning("log_workout: could not save the workout of tenant %s", tenant_id, exc_info=True)
                return None
            if saved is None:
                return None
            code = await xp.award(self.db, tenant_id, xp.BODY_CATEGORY, xp.workout_ref(saved.session_id))  # after the commit; never raises
            return saved, code
        return None

    async def _save(self, tenant_id: uuid.UUID, turn: UserTurn, spec: WorkoutSpec) -> Saved | None:
        """ONE transaction: the exercises (found or made), the session, the sets and the hints computed from what is stored."""
        said_text = norm_key(turn.text)
        async with self.db.tenant_session(tenant_id) as session:
            recent = (
                await session.execute(
                    select(func.count()).select_from(WorkoutSession).where(WorkoutSession.created_at > func.now() - text("interval '24 hours'"))
                )
            ).scalar_one()
            if recent >= self.settings.workout_max_per_day:
                log.info("log_workout: tenant %s reached %d workouts in 24 h", tenant_id, recent)
                return None
            known = list((await session.execute(select(Exercise))).scalars().all())
            resolved: list[tuple[Exercise, ExerciseSpec]] = []
            for item in spec.exercises:
                exercise = await self._resolve(session, tenant_id, known, item, said_text)
                if exercise is not None:
                    resolved.append((exercise, item))
            if not resolved:
                return None
            session_id = (
                await session.execute(
                    pg_insert(WorkoutSession)
                    .values(user_id=tenant_id, notes=spec.notes, source_message_id=turn.message_id)
                    .on_conflict_do_nothing(index_elements=[WorkoutSession.user_id, WorkoutSession.source_message_id], index_where=text("source_message_id IS NOT NULL"))
                    .returning(WorkoutSession.id)
                )
            ).scalar_one_or_none()
            if session_id is None:  # this message was logged already (a replayed update): nothing new to show
                return None
            order: dict[uuid.UUID, int] = {}
            targets: dict[uuid.UUID, int | None] = {}
            for exercise, item in resolved:
                if exercise.id not in targets:
                    targets[exercise.id] = await self._inherited_target(session, tenant_id, exercise.id)
                if item.target_reps is not None:
                    targets[exercise.id] = item.target_reps  # said once, it also covers what follows for the same exercise
                target = targets[exercise.id]
                for one in item.sets:
                    order[exercise.id] = order.get(exercise.id, 0) + 1
                    session.add(
                        WorkoutSet(
                            user_id=tenant_id,
                            session_id=session_id,
                            exercise_id=exercise.id,
                            set_order=order[exercise.id],
                            weight=one.weight,
                            reps=one.reps,
                            rpe=one.rpe,
                            target_reps=target,
                        )
                    )
            await session.flush()
            entries: list[SavedExercise] = []
            seen: set[uuid.UUID] = set()
            for exercise, _ in resolved:
                if exercise.id in seen:  # the same exercise listed twice: one line with all its sets
                    continue
                seen.add(exercise.id)
                sets = tuple(s for e, i in resolved if e.id == exercise.id for s in i.sets)
                history = await self._history(session, tenant_id, exercise.id)
                entries.append(SavedExercise(exercise.name, exercise.muscle_group, sets, overload(history, exercise.muscle_group)))
            return Saved(session_id, tuple(entries), spec.notes)

    async def _resolve(self, session: Any, tenant_id: uuid.UUID, known: list[Exercise], item: ExerciseSpec, said_text: str) -> Exercise | None:
        """The canonical exercise for the model's name: the user's dictionary (name or synonym), the catalog (copied into the dictionary), or a custom one
        - only when the name stands in the user's message. An ambiguous synonym resolves to nothing (the exercise is skipped, never guessed)."""
        key = norm_key(item.name)
        if not key:
            return None
        hits = {e.id: e for e in known if key == norm_key(e.name) or key in {norm_key(a) for a in e.aliases}}
        if len(hits) > 1:
            log.info("log_workout: %r matches %d exercises of tenant %s; skipped", item.name, len(hits), tenant_id)
            return None
        if hits:
            return next(iter(hits.values()))
        entry = _CATALOG_INDEX.get(key)
        if entry is not None:
            return await self._make(session, tenant_id, known, entry.name, entry.group, list(entry.aliases))
        if f" {key} " not in f" {said_text} ":  # a custom exercise must be the user's own word, not the model's
            log.info("log_workout: %r is not in the user's message; skipped", item.name)
            return None
        if len(known) >= self.settings.workout_max_dictionary:
            log.info("log_workout: tenant %s reached the dictionary limit", tenant_id)
            return None
        return await self._make(session, tenant_id, known, item.name, item.group, [])

    @staticmethod
    async def _make(session: Any, tenant_id: uuid.UUID, known: list[Exercise], name: str, group: str, aliases: list[str]) -> Exercise:
        made = (
            await session.execute(
                pg_insert(Exercise)
                .values(user_id=tenant_id, name=name, muscle_group=group, aliases=aliases)
                .on_conflict_do_nothing(index_elements=[Exercise.user_id, Exercise.name])
                .returning(Exercise.id)
            )
        ).scalar_one_or_none()
        query = select(Exercise).where(Exercise.id == made) if made is not None else select(Exercise).where(Exercise.name == name)
        exercise = (await session.execute(query)).scalar_one()
        if all(e.id != exercise.id for e in known):
            known.append(exercise)
        return exercise

    @staticmethod
    async def _inherited_target(session: Any, tenant_id: uuid.UUID, exercise_id: uuid.UUID) -> int | None:
        row = (
            await session.execute(
                text(
                    "SELECT s.target_reps FROM civ.workout_sets s JOIN civ.workout_sessions ws ON ws.user_id = s.user_id AND ws.id = s.session_id "
                    "WHERE s.user_id = CAST(:u AS uuid) AND s.exercise_id = CAST(:e AS uuid) AND s.target_reps IS NOT NULL "
                    "ORDER BY ws.started_at DESC, s.set_order DESC LIMIT 1"
                ),
                {"u": str(tenant_id), "e": str(exercise_id)},
            )
        ).first()
        return None if row is None else int(row[0])

    @staticmethod
    async def _history(session: Any, tenant_id: uuid.UUID, exercise_id: uuid.UUID) -> list[list[LoggedSet]]:
        """The sets of the ``HISTORY`` latest sessions that contain the exercise (the one just logged included), newest session first."""
        rows = (
            await session.execute(
                text(
                    "WITH latest AS ("
                    " SELECT ws.id, ws.started_at FROM civ.workout_sessions ws"
                    " WHERE ws.user_id = CAST(:u AS uuid)"
                    "   AND EXISTS (SELECT 1 FROM civ.workout_sets x WHERE x.user_id = ws.user_id AND x.session_id = ws.id AND x.exercise_id = CAST(:e AS uuid))"
                    " ORDER BY ws.started_at DESC, ws.id DESC LIMIT :n) "
                    "SELECT l.id, s.weight, s.reps, s.target_reps FROM latest l "
                    "JOIN civ.workout_sets s ON s.user_id = CAST(:u AS uuid) AND s.session_id = l.id AND s.exercise_id = CAST(:e AS uuid) "
                    "ORDER BY l.started_at DESC, l.id DESC, s.set_order"
                ),
                {"u": str(tenant_id), "e": str(exercise_id), "n": HISTORY},
            )
        ).all()
        sessions: dict[uuid.UUID, list[LoggedSet]] = {}
        for session_id, weight, reps, target in rows:
            sessions.setdefault(session_id, []).append(LoggedSet(Decimal(weight), int(reps), None if target is None else int(target)))
        return list(sessions.values())


# ---------------------------------------------------------------- the tool


class WorkoutTool:
    """Offered only when the user's own message looks like a workout report (``wants_workout``). Never in the interview or in proactive messages, never on
    a message with an attachment (those replies have no tools at all)."""

    name = "workout"
    functions = frozenset({TOOL_NAME})
    offer_rank = 47

    def __init__(self, engine: Workouts, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        turn = ctx.turn
        if not self.settings.workout_enabled or turn is None or not wants_workout(turn.text):
            return None
        tenant = ctx.tenant

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            done = await self.engine.log(tenant.id, turn, calls)
            if done is None:
                return None
            saved, code = done
            return ToolResult(notes=(render_note(saved),), phrase=True, cards=(Card(card_text(saved, code)),))

        return Offered((TOOL,), INSTRUCTIONS, run, run_rank=47)

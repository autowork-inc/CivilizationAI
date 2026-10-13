"""v2.0, PR 4.1: the workout tracker. The points under test: the model is OFFERED ``log_workout`` only when CODE finds a workout cue AND a digit in the user's
own message; EVERY number of a set (weight, reps, count, RPE, target) must stand in that message or the exercise is dropped (Zero-Hallucination); names are made
canonical by code (the dictionary, its synonyms, a built-in catalog, a custom word of the user); the progressive-overload hint is computed by code from the stored
sets (2 latest sessions, same working weight, every working set reached its target: +5 kg legs / +2.5 kg the rest) and is attached to the card; the card is written
by code (the model's own words next to the call are never sent); a logged workout pays +10 XP in ``body`` (``workout:<session id>``, the daily cap holds).
Real PostgreSQL (RLS, composite FKs, CHECKs), the real aiogram Dispatcher on a fake Telegram wire, a fake Gemini whose "model" decides in the test what to call."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.exc import DBAPIError, IntegrityError

from ai_civ.bot import texts, xp
from ai_civ.bot.tools import UserTurn, workout
from ai_civ.bot.tools.workout import LoggedSet, Workouts, overload, parse_workout, stated_numbers, wants_workout
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_message, new_user, owner_exec, tenant_exec

DB_ERRORS = (IntegrityError, DBAPIError)
LEGS = "Жим ногами: 100 кг 3х10, цель 10"
LEGS_AGAIN = "Жим ногами 100 кг 3х10"


def st(reps: Any, weight: Any = None, count: Any = None, rpe: Any = None) -> dict[str, Any]:
    return {k: v for k, v in {"reps": reps, "weight": weight, "count": count, "rpe": rpe}.items() if v is not None}


def ex(name: str, *sets: dict[str, Any], group: str | None = None, target: Any = None) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "sets": list(sets)}
    if group is not None:
        item["muscle_group"] = group
    if target is not None:
        item["target_reps"] = target
    return item


def wcall(*exercises: dict[str, Any], notes: str | None = None) -> ToolCall:
    args: dict[str, Any] = {"exercises": list(exercises)}
    if notes is not None:
        args["notes"] = notes
    return ToolCall(workout.TOOL_NAME, args)


def leg_call(target: Any = 10) -> ToolCall:
    return wcall(ex("Жим ногами", st(10, 100, 3), target=target))


# ================================================================== pure: the cue check, the numbers, the tool, the call


@pytest.mark.parametrize(
    "said",
    [
        "Сегодня жим ногами 100 кг 3х10",
        "Потренировался: присед 80 на 5",
        "был в зале, сделал 3 подхода по 12",
        "Bench press 60kg 3x8",
        "становая 120 на 5",
        "подтягивания 4 по 8",
        "3×12 отжимания",
        "Leg press 140 kg 4 sets",
        "Тренировка 45 минут, штанга 50",
        "ролики для пресса 3 подхода",
        "жим гантелей 22 кг",
        "сделал 5 повторений",
    ],
)
def test_a_workout_report_with_a_number_is_a_cue(said):
    assert wants_workout(said)


@pytest.mark.parametrize(
    "said",
    [
        "Сегодня жим ногами и присед",  # a cue but no number
        "Купил 2 кг яблок",
        "Встреча в 15:30",
        "Хочу начать тренироваться",
        "У меня 3 задачи на сегодня",
        "",
    ],
)
def test_talk_without_a_cue_or_without_a_number_is_not(said):
    assert not wants_workout(said)


def test_a_known_exercise_name_is_a_cue_even_without_a_workout_word():
    assert wants_workout("румынская тяга 90")
    assert wants_workout("Сгибание рук со штангой 30")


@pytest.mark.parametrize(
    ("said", "present", "absent"),
    [
        ("жим 62,5 на 8", {"62.5", "8"}, {"62", "5"}),
        ("жим 62.50 кг", {"62.5", "62.50", "50"}, set()),  # a two-digit fraction may be a list
        ("10,10,8 повторов", {"10", "8"}, set()),
        ("100 кг 3х10", {"100", "3", "10"}, {"30"}),
        ("x160", {"160"}, {"60", "16"}),  # a number is never a piece of a bigger one
    ],
)
def test_stated_numbers(said, present, absent):
    found = stated_numbers(said)
    assert {Decimal(p) for p in present} <= found
    assert not ({Decimal(a) for a in absent} & found)


def test_the_tool_needs_exercises_with_sets_and_is_plain_json():
    schema = workout.TOOL.parameters
    assert workout.TOOL.name == "log_workout" == workout.TOOL_NAME
    assert schema["required"] == ["exercises"]
    item = schema["properties"]["exercises"]["items"]
    assert set(item["required"]) == {"name", "sets"} and item["properties"]["sets"]["items"]["required"] == ["reps"]
    assert item["properties"]["muscle_group"]["enum"] == list(workout.GROUPS)
    assert json.dumps(schema)


def test_the_instructions_forbid_claiming_it_was_saved_and_suggesting_weights():
    low = workout.INSTRUCTIONS.lower()
    assert "never say it was saved" in low and "never suggest weights" in low and "copying every number" in low


def test_the_catalog_has_no_name_collisions_and_every_group_is_known():
    labels = [label for entry in workout.CATALOG for label in (entry.name, *entry.aliases)]
    assert len({workout.norm_key(label) for label in labels}) == len(labels)
    assert {entry.group for entry in workout.CATALOG} <= set(workout.GROUPS)


@pytest.mark.parametrize(("raw", "key"), [("Жим лёжа", "жим лежа"), ("  LEG   Press! ", "leg press"), ("push-ups", "push ups"), ("", "")])
def test_names_are_compared_in_one_normal_form(raw, key):
    assert workout.norm_key(raw) == key


SAID = "Жим ногами: 100 кг 3х10, цель 10. RPE 8. Жим лёжа 62,5 на 8"


def test_a_valid_call_becomes_a_workout():
    spec = parse_workout(wcall(ex("Жим ногами", st(10, 100, 3, rpe=8), target=10), ex("Жим лёжа", st(8, "62,5"))).args, SAID, 12, 60)
    assert spec is not None
    legs, bench = spec.exercises
    assert (legs.name, legs.target_reps, len(legs.sets)) == ("Жим ногами", 10, 3)
    assert all((s.weight, s.reps, s.rpe) == (Decimal(100), 10, Decimal(8)) for s in legs.sets)
    assert bench.sets[0].weight == Decimal("62.5") and bench.sets[0].reps == 8 and bench.target_reps is None


def test_body_weight_sets_need_no_weight_and_a_default_count_needs_no_witness():
    spec = parse_workout(wcall(ex("Подтягивания", st(8, count=1), st(0 + 6, 0))).args, "подтягивания 8 и 6", 12, 60)
    assert spec is not None and [(s.weight, s.reps) for s in spec.exercises[0].sets] == [(Decimal(0), 8), (Decimal(0), 6)]


@pytest.mark.parametrize(
    "bad_set",
    [
        st(12, 100),  # reps the user never wrote
        st(10, 90),  # a weight the user never wrote
        st(10, 100, 4),  # four sets, the user wrote three
        st(10, 100, rpe=7),  # an RPE the user never wrote
        st(10, 100, 1.5),  # a fractional count
        st(0, 100),
        st(501, 100),
        st(10, -100),
        st(10, 1001),
        st(10, "сто"),
        st(10, 100, "три"),
        st(True, 100),
        st(10, float("nan")),
        st(10, 100.123),
        st("десять", 100),
        {"weight": 100},  # no reps at all
        "10x100",
    ],
)
def test_a_number_the_user_did_not_write_drops_the_exercise(bad_set):
    said = "жим ногами 100 кг 3х10, цель 10, 1001 и 501"
    # one good exercise next to the bad one: only the bad one goes
    spec = parse_workout(wcall(ex("Жим ногами", st(10, 100), bad_set), ex("Жим лёжа", st(10, 100))).args, said, 12, 60)
    assert spec is not None and [e.name for e in spec.exercises] == ["Жим лёжа"]


@pytest.mark.parametrize("target", [8, 12, 0, 501, "десять", True, 9.5])
def test_a_target_the_user_did_not_state_drops_the_exercise(target):
    assert parse_workout(wcall(ex("Жим ногами", st(10, 100), target=target)).args, "жим ногами 100 кг 10", 12, 60) is None


def test_unusable_calls_are_dropped_as_a_whole():
    said = "жим 100 кг 10"
    for args in (
        {},
        {"exercises": "жим"},
        {"exercises": []},
        {"exercises": [{"name": "Жим", "sets": []}]},
        {"exercises": [{"name": "", "sets": [st(10)]}]},
        {"exercises": [{"name": "Жим‮evil", "sets": [st(10)]}]},
        {"exercises": [{"name": "я" * 81, "sets": [st(10)]}]},
        {"exercises": ["жим"]},
    ):
        assert parse_workout(args, said, 12, 60) is None


def test_too_many_sets_or_exercises_are_capped():
    said = " ".join(str(n) for n in range(1, 30))
    many_sets = wcall(ex("Жим", st(10, count=20), st(10, count=20), st(10, count=20), st(10, count=20))).args  # 80 sets
    assert parse_workout(many_sets, said + " 10 20", 12, 60) is None
    names = [ex(f"Жим {n}", st(10)) for n in range(1, 8)]
    spec = parse_workout({"exercises": names}, "10 " + " ".join(f"жим {n}" for n in range(1, 8)), 3, 60)
    assert spec is not None and len(spec.exercises) == 3


def test_notes_are_kept_only_when_they_are_the_users_own_words():
    said = "Жим ногами 100 кг на 10, колени ныли"
    ok = parse_workout(wcall(ex("Жим ногами", st(10, 100)), notes="Колени ныли").args, said, 12, 60)
    assert ok is not None and ok.notes == "Колени ныли"
    for notes in ("Отличная тренировка!", "колени​ныли", "ныли" * 60):
        spec = parse_workout(wcall(ex("Жим ногами", st(10, 100)), notes=notes).args, said, 12, 60)
        assert spec is not None and spec.notes == ""  # the call stays, the invented note goes


# ---------------------------------------------------------------- progressive overload


def sess(*sets: tuple[int, int, int | None]) -> list[LoggedSet]:
    return [LoggedSet(Decimal(w), r, t) for w, r, t in sets]


def test_two_sessions_at_the_same_weight_with_every_target_reached_add_5_kg_for_legs():
    hint = overload([sess((100, 10, 10), (100, 10, 10), (100, 10, 10)), sess((100, 10, 10), (100, 12, 10))], "legs")
    assert hint is not None and (hint.weight, hint.step, hint.next_weight) == (100, 5, 105)


@pytest.mark.parametrize("group", ["chest", "back", "shoulders", "arms", "core", "other"])
def test_every_other_group_adds_2_5_kg(group):
    hint = overload([sess((60, 8, 8)), sess((60, 8, 8))], group)
    assert hint is not None and (hint.step, hint.next_weight) == (Decimal("2.5"), Decimal("62.5"))


@pytest.mark.parametrize(
    "history",
    [
        [],  # nothing logged
        [sess((100, 10, 10))],  # one session is not "two in a row"
        [sess((100, 10, 10)), sess((100, 9, 10))],  # a missed target in the older session
        [sess((100, 9, 10)), sess((100, 10, 10))],  # ... or in the newer one
        [sess((100, 10, None)), sess((100, 10, 10))],  # no target = nothing to be "done" against
        [sess((105, 10, 10)), sess((100, 10, 10))],  # the weight changed: the older session is no evidence for the new one
        [sess((0, 10, 10)), sess((0, 10, 10))],  # body weight: no weight to add to
        [[], sess((100, 10, 10))],
    ],
)
def test_no_recommendation_unless_the_rule_holds(history):
    assert overload(history, "legs") is None


def test_a_lighter_warm_up_set_is_not_a_working_set():
    warm_up_then_work = sess((40, 5, None), (100, 10, 10), (100, 10, 10))
    assert overload([warm_up_then_work, warm_up_then_work], "legs") is not None


def test_only_the_two_latest_sessions_count():
    assert overload([sess((100, 10, 10)), sess((100, 10, 10)), sess((100, 3, 10))], "legs") is not None


def test_the_card_lists_the_sets_the_hint_and_the_xp_line():
    legs_sets = tuple(workout.SetSpec(Decimal(100), 10, None) for _ in range(2)) + (workout.SetSpec(Decimal(100), 8, Decimal("8.5")),)
    pull = (workout.SetSpec(Decimal(0), 8, None),)
    saved = workout.Saved(
        uuid.uuid4(),
        (
            workout.SavedExercise("Жим ногами", "legs", legs_sets, workout.Overload(Decimal(100), Decimal(5), Decimal(105))),
            workout.SavedExercise("Подтягивания", "back", pull, None),
        ),
        "колени ныли",
    )
    card = workout.card_text(saved, "awarded").split("\n")
    assert card[0] == texts.WORKOUT_HEADER == "🏋️ Тренировка записана"
    assert card[1] == "• Жим ногами: 100×10, 100×10, 100×8 @8,5" and card[2] == "• Подтягивания: 8 повт." and card[3] == "📝 колени ныли"
    assert card[4] == "💡 Жим ногами: 2 занятия подряд все целевые повторения выполнены — в следующий раз можно добавить 5 кг: 100 → 105 кг."
    assert card[5] == "⭐ +10 XP — тело"
    assert "XP" not in workout.card_text(saved, "daily_cap") and "XP" not in workout.card_text(saved)
    assert workout.fmt(Decimal("62.50")) == "62,5" and workout.fmt(Decimal("100.00")) == "100"


# ================================================================== fixtures and helpers


@pytest_asyncio.fixture
async def workout_env(owner_engine, db):
    """``bot_env`` with the tracker on, as in production (the function is still offered only on a cue and a number)."""
    env = await build_bot_env(owner_engine, db, workout_enabled=True)
    yield env
    await env.bot.session.close()


def engine_of(env, **settings_kw: Any) -> Workouts:
    if not settings_kw:
        return env.orchestrator.workouts
    return Workouts(env.db, Settings(_env_file=None, **settings_kw))


async def log(env, said: str, *calls: ToolCall, **settings_kw: Any):
    """What the engine saves for a message of the user (a real ``messages`` row: the session points at it)."""
    message_id = await new_message(env.owner_engine, env.user_id, said)
    return await engine_of(env, **settings_kw).log(env.user_id, UserTurn(message_id, said), calls)


async def rows(env, sql: str, **kw: Any):
    return await owner_exec(env.owner_engine, sql, u=env.user_id, **kw)


COUNT_SESSIONS = "SELECT count(*) FROM civ.workout_sessions WHERE user_id = :u"
COUNT_SETS = "SELECT count(*) FROM civ.workout_sets WHERE user_id = :u"
COUNT_EXERCISES = "SELECT count(*) FROM civ.exercises WHERE user_id = :u"
SETS = (
    "SELECT e.name, s.set_order, s.weight, s.reps, s.rpe, s.target_reps FROM civ.workout_sets s JOIN civ.exercises e ON e.user_id = s.user_id AND e.id = s.exercise_id "
    "WHERE s.user_id = :u ORDER BY s.created_at, e.name, s.set_order"
)
XP = "SELECT amount, category, source_ref FROM civ.xp_ledger WHERE user_id = :u ORDER BY created_at"


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


def model_logs(env, *calls: ToolCall, text: str = "Записал всё в журнал!", then: str = "Огонь, так держать.") -> None:
    """The fake model calls the function - and, as a real one might, ALSO claims it saved everything."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


# ================================================================== the database


async def test_the_workout_tables_belong_to_one_tenant_and_the_others_cannot_see_them(workout_env, owner_engine, db):
    assert await log(workout_env, LEGS, leg_call()) is not None
    stranger = await new_user(owner_engine)
    for table in ("exercises", "workout_sessions", "workout_sets"):
        assert await tenant_exec(db, stranger, f"SELECT count(*) FROM civ.{table}") == [(0,)]
    assert await tenant_exec(db, workout_env.user_id, "SELECT count(*) FROM civ.workout_sets") == [(3,)]


async def test_the_composite_foreign_keys_refuse_another_tenants_rows(workout_env, owner_engine, db):
    await log(workout_env, LEGS, leg_call())
    [(session_id, exercise_id)] = await rows(workout_env, "SELECT DISTINCT session_id, exercise_id FROM civ.workout_sets WHERE user_id = :u")
    stranger = await new_user(owner_engine)
    insert = "INSERT INTO civ.workout_sets (user_id, session_id, exercise_id, set_order, reps) VALUES (:u, :s, :e, 1, 5)"
    with pytest.raises(DB_ERRORS):  # the stranger's set under MY session and MY exercise
        await tenant_exec(db, stranger, insert, u=stranger, s=session_id, e=exercise_id)
    own_exercise = (await tenant_exec(db, stranger, "INSERT INTO civ.exercises (user_id, name) VALUES (:u, 'Своё') RETURNING id", u=stranger))[0][0]
    with pytest.raises(DB_ERRORS):  # ... or my session with their own exercise
        await tenant_exec(db, stranger, insert, u=stranger, s=session_id, e=own_exercise)
    message = await new_message(owner_engine, workout_env.user_id, "моё сообщение")
    with pytest.raises(DB_ERRORS):  # a session that points at MY message
        await tenant_exec(db, stranger, "INSERT INTO civ.workout_sessions (user_id, source_message_id) VALUES (:u, :m)", u=stranger, m=message)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO civ.exercises (user_id, name, muscle_group) VALUES (:u, 'Х', 'brain')",
        "INSERT INTO civ.exercises (user_id, name) VALUES (:u, E'два\\nряда')",
        "INSERT INTO civ.exercises (user_id, name) VALUES (:u, repeat('я', 81))",
        "INSERT INTO civ.exercises (user_id, name) VALUES (:u, ' пробел')",
        "INSERT INTO civ.exercises (user_id, name, aliases) VALUES (:u, 'Х', ARRAY(SELECT 'а' || g FROM generate_series(1, 21) g))",
        "INSERT INTO civ.exercises (user_id, name, aliases) VALUES (:u, 'Х', ARRAY['ok', E'bad\\nalias'])",
        "INSERT INTO civ.exercises (user_id, name, aliases) VALUES (:u, 'Х', ARRAY['ok', NULL])",
        "INSERT INTO civ.workout_sessions (user_id, notes) VALUES (:u, repeat('я', 501))",
        "INSERT INTO civ.workout_sessions (user_id, notes) VALUES (:u, E'bad\\x07bell')",
    ],
)
async def test_the_checks_refuse_a_bad_exercise_or_note(workout_env, db, sql):
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, workout_env.user_id, sql, u=workout_env.user_id)


async def test_an_exercise_may_have_no_synonyms_and_a_name_is_unique_per_tenant(workout_env, owner_engine, db):
    u = workout_env.user_id
    await tenant_exec(db, u, "INSERT INTO civ.exercises (user_id, name, aliases) VALUES (:u, 'Планка', '{}')", u=u)
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, u, "INSERT INTO civ.exercises (user_id, name) VALUES (:u, 'Планка')", u=u)
    other = await new_user(owner_engine)  # another tenant may use the same name
    await tenant_exec(db, other, "INSERT INTO civ.exercises (user_id, name) VALUES (:u, 'Планка')", u=other)


@pytest.mark.parametrize(
    "values",
    ["weight = -1", "weight = 1000.01", "reps = 0", "reps = 501", "rpe = 0.5", "rpe = 10.5", "target_reps = 0", "set_order = 0", "set_order = 101"],
)
async def test_the_checks_refuse_a_bad_set(workout_env, db, values):
    await log(workout_env, LEGS, leg_call())
    with pytest.raises(DB_ERRORS):  # the application role has no UPDATE on sets, so the CHECK is probed with an INSERT of the same shape
        column, _, value = values.partition(" = ")
        base = {"weight": "100", "reps": "5", "rpe": "NULL", "target_reps": "NULL", "set_order": "9"}
        base[column] = value
        await tenant_exec(
            db,
            workout_env.user_id,
            "INSERT INTO civ.workout_sets (user_id, session_id, exercise_id, set_order, weight, reps, rpe, target_reps) "
            f"SELECT user_id, session_id, exercise_id, {base['set_order']}, {base['weight']}, {base['reps']}, {base['rpe']}, {base['target_reps']} "
            "FROM civ.workout_sets LIMIT 1",
        )


async def test_a_set_is_never_edited_and_an_exercise_with_a_history_cannot_be_deleted(workout_env, db):
    await log(workout_env, LEGS, leg_call())
    u = workout_env.user_id
    with pytest.raises(DB_ERRORS):  # sets are made or removed, never edited
        await tenant_exec(db, u, "UPDATE civ.workout_sets SET reps = 99")
    with pytest.raises(DB_ERRORS):  # NO ACTION: the history stays
        await tenant_exec(db, u, "DELETE FROM civ.exercises")
    await tenant_exec(db, u, "DELETE FROM civ.workout_sessions")  # a session goes with its sets
    assert await rows(workout_env, COUNT_SETS) == [(0,)]
    await tenant_exec(db, u, "DELETE FROM civ.exercises")  # now nothing refers to the exercise


async def test_a_set_number_is_unique_within_a_session_and_an_exercise(workout_env, db):
    await log(workout_env, LEGS, leg_call())
    with pytest.raises(DB_ERRORS):
        await tenant_exec(
            db,
            workout_env.user_id,
            "INSERT INTO civ.workout_sets (user_id, session_id, exercise_id, set_order, reps) SELECT user_id, session_id, exercise_id, set_order, 5 FROM civ.workout_sets LIMIT 1",
        )


async def test_one_message_logs_at_most_one_workout(workout_env, db):
    message = await new_message(workout_env.owner_engine, workout_env.user_id, LEGS)
    u = workout_env.user_id
    await tenant_exec(db, u, "INSERT INTO civ.workout_sessions (user_id, source_message_id) VALUES (:u, :m)", u=u, m=message)
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, u, "INSERT INTO civ.workout_sessions (user_id, source_message_id) VALUES (:u, :m)", u=u, m=message)
    await tenant_exec(db, u, "INSERT INTO civ.workout_sessions (user_id) VALUES (:u)", u=u)  # a session with no message is not limited
    await tenant_exec(db, u, "INSERT INTO civ.workout_sessions (user_id) VALUES (:u)", u=u)


# ================================================================== the engine: saving, names, numbers


async def test_a_workout_is_saved_with_ordered_sets_and_canonical_names(workout_env):
    saved, code = await log(
        workout_env, "Потренировался: leg press 100 кг 3х10 RPE 8, цель 10; подтягивания 8 и 6", wcall(
            ex("leg press", st(10, 100, 3, rpe=8), target=10), ex("Подтягивания", st(8), st(6))
        )
    )  # fmt: skip
    assert code == "awarded" and [e.name for e in saved.entries] == ["Жим ногами", "Подтягивания"]
    assert await rows(workout_env, SETS) == [
        ("Жим ногами", 1, Decimal("100.00"), 10, Decimal("8.0"), 10),
        ("Жим ногами", 2, Decimal("100.00"), 10, Decimal("8.0"), 10),
        ("Жим ногами", 3, Decimal("100.00"), 10, Decimal("8.0"), 10),
        ("Подтягивания", 1, Decimal("0.00"), 8, None, None),
        ("Подтягивания", 2, Decimal("0.00"), 6, None, None),
    ]
    assert await rows(workout_env, "SELECT name, muscle_group, aliases FROM civ.exercises WHERE user_id = :u ORDER BY name") == [
        ("Жим ногами", "legs", ["жим платформы", "жим ногами в тренажёре", "leg press"]),
        ("Подтягивания", "back", ["pull-ups", "pullups"]),
    ]
    [(source,)] = await rows(workout_env, "SELECT source_message_id FROM civ.workout_sessions WHERE user_id = :u")
    assert source is not None


async def test_the_catalog_copy_is_made_once_and_synonyms_find_it(workout_env):
    await log(workout_env, "жим ногами 100 кг 10", wcall(ex("Жим ногами", st(10, 100))))
    await log(workout_env, "Leg Press 110 kg 10", wcall(ex("Leg Press", st(10, 110))))
    await log(workout_env, "жим платформы 120 на 10", wcall(ex("жим платформы", st(10, 120))))
    assert await rows(workout_env, COUNT_EXERCISES) == [(1,)] and await rows(workout_env, COUNT_SESSIONS) == [(3,)]


async def test_the_users_own_synonym_resolves_to_their_exercise(workout_env):
    u = workout_env.user_id
    await owner_exec(workout_env.owner_engine, "INSERT INTO civ.exercises (user_id, name, muscle_group, aliases) VALUES (:u, 'Мой присед', 'legs', ARRAY['сквот'])", u=u)
    saved, _ = await log(workout_env, "Сквот 80 кг на 5", wcall(ex("Сквот", st(5, 80))))
    assert [e.name for e in saved.entries] == ["Мой присед"] and await rows(workout_env, COUNT_EXERCISES) == [(1,)]


async def test_an_ambiguous_synonym_resolves_to_nothing(workout_env):
    u = workout_env.user_id
    for name in ("Первое", "Второе"):
        await owner_exec(workout_env.owner_engine, "INSERT INTO civ.exercises (user_id, name, aliases) VALUES (:u, :n, ARRAY['общее'])", u=u, n=name)
    assert await log(workout_env, "общее 50 кг на 10", wcall(ex("общее", st(10, 50)))) is None
    assert await rows(workout_env, COUNT_SESSIONS) == [(0,)]


async def test_a_custom_exercise_must_be_the_users_own_word(workout_env):
    saved, _ = await log(workout_env, "ролики для пресса 3 подхода по 12", wcall(ex("Ролики для пресса", st(12, count=3), group="core")))
    assert [e.name for e in saved.entries] == ["Ролики для пресса"]
    assert await rows(workout_env, "SELECT muscle_group FROM civ.exercises WHERE user_id = :u") == [("core",)]
    # the model's name is not in the message: nothing is made, nothing is logged
    assert await log(workout_env, "что-то 3 по 12", wcall(ex("Тяга Зерчера", st(12, 3)))) is None
    assert await rows(workout_env, COUNT_EXERCISES) == [(1,)]


async def test_a_wrong_group_from_the_model_becomes_other(workout_env):
    await log(workout_env, "гребля 3 подхода по 12", wcall(ex("гребля", st(12, count=3), group="brain")))
    assert await rows(workout_env, "SELECT muscle_group FROM civ.exercises WHERE user_id = :u") == [("other",)]


async def test_the_dictionary_cap_stops_new_custom_exercises_but_not_known_ones(workout_env):
    await log(workout_env, "жим ногами 100 кг 10", wcall(ex("Жим ногами", st(10, 100))), workout_max_dictionary=1)
    assert await log(workout_env, "планка 3 по 60", wcall(ex("кувырок", st(60, 3))), workout_max_dictionary=1) is None
    assert await log(workout_env, "жим ногами 110 кг 10", wcall(ex("Жим ногами", st(10, 110))), workout_max_dictionary=1) is not None


async def test_the_same_exercise_listed_twice_is_one_line_with_continued_set_numbers(workout_env):
    saved, _ = await log(workout_env, "жим ногами 100 на 10, потом жим ногами 110 на 8", wcall(ex("Жим ногами", st(10, 100)), ex("жим ногами", st(8, 110))))
    assert len(saved.entries) == 1 and len(saved.entries[0].sets) == 2
    assert [(r[1], r[2]) for r in await rows(workout_env, SETS)] == [(1, Decimal("100.00")), (2, Decimal("110.00"))]


async def test_a_replayed_message_does_not_log_the_workout_twice(workout_env):
    said = LEGS
    message_id = await new_message(workout_env.owner_engine, workout_env.user_id, said)
    turn = UserTurn(message_id, said)
    assert await workout_env.orchestrator.workouts.log(workout_env.user_id, turn, [leg_call()]) is not None
    assert await workout_env.orchestrator.workouts.log(workout_env.user_id, turn, [leg_call()]) is None
    assert await rows(workout_env, COUNT_SESSIONS) == [(1,)] and await rows(workout_env, COUNT_SETS) == [(3,)]
    assert await rows(workout_env, XP) == [(10, "body", await _ref(workout_env))]


async def _ref(env) -> str:
    [(session_id,)] = await rows(env, "SELECT id FROM civ.workout_sessions WHERE user_id = :u")
    return xp.workout_ref(session_id)


async def test_a_call_with_an_invented_number_logs_nothing(workout_env):
    assert await log(workout_env, "жим ногами 100 кг 3х10", wcall(ex("Жим ногами", st(10, 120, 3)))) is None  # 120 was never written
    assert await rows(workout_env, COUNT_SESSIONS) == [(0,)] and await rows(workout_env, COUNT_EXERCISES) == [(0,)] and await rows(workout_env, XP) == []


async def test_only_the_first_usable_call_of_a_message_makes_a_workout(workout_env):
    bad = wcall(ex("Жим ногами", st(99)))
    saved, _ = await log(workout_env, "жим ногами 100 на 10, жим лёжа 60 на 8", bad, wcall(ex("Жим ногами", st(10, 100))), wcall(ex("Жим лёжа", st(8, 60))))
    assert [e.name for e in saved.entries] == ["Жим ногами"] and await rows(workout_env, COUNT_SESSIONS) == [(1,)]


async def test_calls_that_are_not_ours_log_nothing(workout_env):
    assert await log(workout_env, "жим 100 на 10", ToolCall("create_event", wcall(ex("Жим", st(10, 100))).args)) is None
    assert await rows(workout_env, COUNT_SESSIONS) == [(0,)]


async def test_the_daily_limit_of_workouts(workout_env):
    assert await log(workout_env, "жим ногами 100 на 10", wcall(ex("Жим ногами", st(10, 100))), workout_max_per_day=1) is not None
    assert await log(workout_env, "жим ногами 100 на 11", wcall(ex("Жим ногами", st(11, 100))), workout_max_per_day=1) is None
    assert await rows(workout_env, COUNT_SESSIONS) == [(1,)]


async def test_a_database_failure_logs_nothing_and_never_raises(workout_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Workouts, "_save", boom)
    assert await log(workout_env, LEGS, leg_call()) is None
    assert await rows(workout_env, COUNT_SESSIONS) == [(0,)]


async def test_a_failure_in_the_middle_rolls_the_whole_workout_back(workout_env, monkeypatch):
    async def boom(*a: Any, **k: Any):
        raise RuntimeError("history query failed")

    monkeypatch.setattr(Workouts, "_history", boom)
    assert await log(workout_env, LEGS, leg_call()) is None
    assert await rows(workout_env, COUNT_SESSIONS) == [(0,)] and await rows(workout_env, COUNT_SETS) == [(0,)] and await rows(workout_env, COUNT_EXERCISES) == [(0,)]


# ---------------------------------------------------------------- the progressive overload, end to end


async def test_two_sessions_with_every_target_reached_give_the_hint_on_the_card(workout_env):
    first, _ = await log(workout_env, LEGS, leg_call())
    assert first.entries[0].hint is None  # one session is not "two in a row"
    second, _ = await log(workout_env, LEGS_AGAIN, wcall(ex("Жим ногами", st(10, 100, 3))))  # the target (10) is remembered from the first message
    hint = second.entries[0].hint
    assert hint is not None and (hint.weight, hint.next_weight) == (100, 105)
    assert "💡 Жим ногами" in workout.card_text(second) and "100 → 105 кг" in workout.card_text(second)
    assert {r[5] for r in await rows(workout_env, SETS)} == {10}  # the inherited target is stored on the second session's sets too


async def test_the_step_is_2_5_kg_for_an_upper_body_exercise(workout_env):
    for n in range(2):
        saved, _ = await log(workout_env, f"Жим лёжа 60 кг 3х8, цель 8 №{n}", wcall(ex("Жим лёжа", st(8, 60, 3), target=8)))
    assert saved.entries[0].hint is not None and saved.entries[0].hint.next_weight == Decimal("62.5") and saved.entries[0].hint.step == Decimal("2.5")


async def test_a_missed_target_or_no_target_gives_no_hint(workout_env):
    await log(workout_env, "Жим лёжа 60 кг 3х8, цель 8", wcall(ex("Жим лёжа", st(8, 60, 3), target=8)))
    missed, _ = await log(workout_env, "Жим лёжа 60 кг 2 подхода по 8 и 7", wcall(ex("Жим лёжа", st(8, 60, 2), st(7, 60))))
    assert missed.entries[0].hint is None
    await log(workout_env, "Присед 80 кг 3х5", wcall(ex("Присед", st(5, 80, 3))))  # never any target
    again, _ = await log(workout_env, "Присед 80 кг 3х5 снова", wcall(ex("Присед", st(5, 80, 3))))
    assert again.entries[0].hint is None


async def test_a_heavier_second_session_is_not_evidence_for_a_hint(workout_env):
    await log(workout_env, LEGS, leg_call())
    heavier, _ = await log(workout_env, "Жим ногами 110 кг 3х10", wcall(ex("Жим ногами", st(10, 110, 3))))
    assert heavier.entries[0].hint is None


async def test_one_tenants_history_never_feeds_anothers_hint(workout_env, owner_engine, db):
    await log(workout_env, LEGS, leg_call())
    stranger = await new_user(owner_engine)
    other = Workouts(db, Settings(_env_file=None))
    message = await new_message(owner_engine, stranger, LEGS_AGAIN)
    saved, _ = await other.log(stranger, UserTurn(message, LEGS_AGAIN), [wcall(ex("Жим ногами", st(10, 100, 3)))])
    assert saved.entries[0].hint is None  # the stranger's first session; mine does not count


# ---------------------------------------------------------------- the XP


async def test_a_logged_workout_pays_10_xp_in_body_once(workout_env):
    saved, code = await log(workout_env, LEGS, leg_call())
    assert code == "awarded" and await rows(workout_env, XP) == [(10, "body", f"workout:{saved.session_id}")]
    assert "⭐ +10 XP — тело" in workout.card_text(saved, code)


async def test_the_daily_cap_of_100_xp_holds_and_the_workout_is_still_saved(workout_env):
    await owner_exec(
        workout_env.owner_engine, "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 100, 'focus', 'seed')", u=workout_env.user_id
    )
    saved, code = await log(workout_env, LEGS, leg_call())
    assert code == "daily_cap" and await rows(workout_env, COUNT_SESSIONS) == [(1,)]
    assert await rows(workout_env, "SELECT count(*) FROM civ.xp_ledger WHERE user_id = :u AND category = 'body'") == [(0,)]
    assert "XP" not in workout.card_text(saved, code)


async def test_a_failed_workout_pays_nothing(workout_env):
    await log(workout_env, "жим ногами 100 кг 3х10", wcall(ex("Жим ногами", st(10, 120))))
    assert await rows(workout_env, XP) == []


# ================================================================== the whole flow through the Dispatcher


def card_messages(env, thread_id: int | None = None) -> list[Any]:
    return [m for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id and m.text.startswith(texts.WORKOUT_HEADER)]


async def test_a_workout_message_gets_the_function_and_the_reply_is_a_card_the_model_did_not_write(workout_env):
    env = workout_env
    model_logs(env, leg_call())
    await env.feed(LEGS)
    offered = env.chat.tool_calls[0]
    assert [t.name for t in offered["tools"]] == ["log_workout"] and "## Workout log" in offered["system"]
    assert not any("записал" in t.lower() for t in says(env))  # the model's own words next to the call are never sent
    assert "Огонь, так держать." in says(env)  # the reply was phrased again, with the note of what the system did
    assert "Workout log (made by the system)" in env.chat.calls[-1]["system"] and "Жим ногами" in env.chat.calls[-1]["system"]
    [card] = card_messages(env)
    assert "• Жим ногами: 100×10, 100×10, 100×10" in card.text and "⭐ +10 XP — тело" in card.text and card.reply_markup is None
    assert await rows(env, COUNT_SESSIONS) == [(1,)] and await rows(env, COUNT_SETS) == [(3,)]
    assert not any("🏋️" in (m["content"] or "") for m in await env.messages())  # the card is not a word of the assistant: not remembered as one


async def test_the_second_message_carries_the_overload_hint_on_its_card(workout_env):
    env = workout_env
    model_logs(env, leg_call())
    await env.feed(LEGS)
    model_logs(env, wcall(ex("Жим ногами", st(10, 100, 3))))
    await env.feed(LEGS_AGAIN)
    first, second = card_messages(env)
    assert "💡" not in first.text
    assert "💡 Жим ногами: 2 занятия подряд все целевые повторения выполнены — в следующий раз можно добавить 5 кг: 100 → 105 кг." in second.text
    assert await rows(env, "SELECT count(*) FROM civ.xp_ledger WHERE user_id = :u AND category = 'body'") == [(2,)]


async def test_a_number_the_model_invented_gives_no_card_and_its_claim_is_not_sent(workout_env):
    env = workout_env
    model_logs(env, wcall(ex("Жим ногами", st(10, 120, 3))), then="Не получилось записать, напишите ещё раз.")
    await env.feed(LEGS)
    assert card_messages(env) == [] and await rows(env, COUNT_SESSIONS) == [(0,)] and await rows(env, XP) == []
    assert not any("записал" in t.lower() for t in says(env))  # a function of ours was called and nothing ran: the words are not trusted


async def test_talk_without_a_cue_or_a_number_is_never_offered_the_function(workout_env):
    env = workout_env
    env.chat.responder = lambda system, turns: "Расскажите подробнее."
    for said in ("Хочу начать тренироваться, но времени нет", "Купил 2 кг яблок", "Сегодня жим ногами и присед"):
        await env.feed(said)
    assert env.chat.tool_calls == [] and len(env.chat.calls) == 3
    assert all("Workout log" not in c["system"] for c in env.chat.calls)


async def test_the_switch_turns_the_function_off_even_on_a_workout(owner_engine, db):
    env = await build_bot_env(owner_engine, db, workout_enabled=False)
    try:
        env.chat.responder = lambda system, turns: "Хорошо."
        await env.feed(LEGS)
        assert env.chat.tool_calls == [] and card_messages(env) == []
    finally:
        await env.bot.session.close()


async def test_a_voice_report_is_logged_like_a_typed_one(workout_env):
    env = workout_env
    env.chat.transcriber = lambda data, mime: LEGS
    model_logs(env, leg_call())
    await env.feed_voice()
    assert len(card_messages(env)) == 1 and await rows(env, COUNT_SETS) == [(3,)]


async def test_a_redelivered_update_logs_the_workout_once(workout_env):
    env = workout_env
    model_logs(env, leg_call())
    update = env.update(LEGS)
    await env.feed_update(update)
    await env.feed_update(update)
    assert await rows(env, COUNT_SESSIONS) == [(1,)] and len(card_messages(env)) == 1


async def test_a_database_failure_still_sends_the_reply_without_a_card(workout_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Workouts, "_save", boom)
    env = workout_env
    model_logs(env, leg_call(), then="Не получилось сохранить, повторите.")
    await env.feed(LEGS)
    assert card_messages(env) == [] and "Не получилось сохранить, повторите." in says(env)
    assert not any("записал" in t.lower() for t in says(env))


async def test_the_function_is_not_offered_on_an_attachment_reply(workout_env):
    """A document or photo reply has no tools at all (its content is data, never instructions): the registry only asks tools that are ``untrusted_ok``."""
    from ai_civ.bot.tools.base import ToolContext, ToolRegistry

    registry = ToolRegistry([workout.WorkoutTool(workout_env.orchestrator.workouts, workout_env.settings)])
    ctx = ToolContext(workout_env.tenant, None, UserTurn(uuid.uuid4(), LEGS))
    assert len(await registry.offer(ctx)) == 1
    assert await registry.offer(ctx, untrusted=True) == []


def test_the_function_name_is_unique_in_the_production_registry(workout_env):
    names = [name for tool in workout_env.orchestrator.tools.tools for name in tool.functions]
    assert names.count("log_workout") == 1 and len(names) == len(set(names))

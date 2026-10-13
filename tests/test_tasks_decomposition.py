"""v2.0, PR 3.1: tasks and their decomposition. The points under test: the model is OFFERED ``decompose_task`` only when CODE finds a marker in the user's
own message; a call is validated by code (2..N clean steps, a step time is the user's wall clock turned into an instant by code); what the user reads
is a card written by code (the model's own words next to the call are never sent); marking a step is one idempotent transaction; a calendar slot is
NEVER made silently - the 📅 button only creates an ``event_drafts`` row and the usual "put it into the calendar?" card, and only that button's
confirmation writes the event and links it to the step. Real PostgreSQL (RLS, composite FKs, the depth trigger), the real aiogram Dispatcher on a
fake Telegram wire, a fake Gemini whose "model" decides in the test what to call."""

from __future__ import annotations

import random

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy.exc import DBAPIError, IntegrityError

from ai_civ.bot import texts
from ai_civ.bot.tools import decompose
from ai_civ.bot.tools.decompose import Tasks, parse_plan, wants_decomposition
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_persona, new_user, owner_exec, tenant_exec
from tests.tg_fakes import make_callback_update

CPH = "Europe/Copenhagen"
ASK = "Разбей на шаги: подготовить презентацию к пятнице"
GOAL = "Подготовить презентацию"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
DB_ERRORS = (IntegrityError, DBAPIError)


def local(hours: float) -> tuple[str, datetime]:
    """The wall-clock text the model would pass for a start ``hours`` ahead (Copenhagen) and the instant the code must derive."""
    instant = (datetime.now(UTC) + timedelta(hours=hours)).replace(second=0, microsecond=0)
    return instant.astimezone(ZoneInfo(CPH)).strftime("%Y-%m-%dT%H:%M"), instant


def call(goal: str = GOAL, steps: list[Any] | None = None) -> ToolCall:
    steps = steps if steps is not None else [{"title": "Собрать тезисы"}, {"title": "Сделать слайды"}, {"title": "Прогнать вслух"}]
    return ToolCall(decompose.TOOL_NAME, {"goal": goal, "steps": steps})


# ================================================================== pure: the marker check, the tool, the call


@pytest.mark.parametrize(
    "said",
    [
        "Декомпозируй задачу: выучить датский",
        "декомпозируй",
        "Разбей на шаги подготовку к экзамену",
        "разбей эту большую задачу на шаги",
        "Помоги разбить диплом на этапы",
        "Сделай план: переезд в Орхус",
        "сделай мне план подготовки к марафону",
        "Распиши задачу по шагам",
        "распиши мне переезд по шагам, пожалуйста",
        "РАЗБЕЙ ЭТО НА ШАГИ",
        "Please break it down into steps",
        "decompose this project",
    ],
)
def test_an_explicit_request_for_a_breakdown_is_a_marker(said):
    assert wants_decomposition(said)


@pytest.mark.parametrize(
    "said",
    [
        "Хочу выучить датский",
        "Расскажи про план Маршалла",
        "У меня много задач на этой неделе",
        "Разбей мне стакан :)",  # a word of the family, not the request
        "я составил план на день",
        "сделаю план завтра",
        "",
    ],
)
def test_ordinary_talk_is_not_a_marker(said):
    assert not wants_decomposition(said)


def test_the_tool_needs_a_goal_and_steps_and_is_plain_json():
    schema = decompose.TOOL.parameters
    assert decompose.TOOL.name == "decompose_task" == decompose.TOOL_NAME
    assert set(schema["required"]) == {"goal", "steps"}
    assert schema["properties"]["steps"]["items"]["required"] == ["title"]  # a step time is optional
    assert json.dumps(schema)


def test_the_instructions_forbid_claiming_it_was_saved_and_writing_the_steps_out():
    low = decompose.INSTRUCTIONS.lower()
    assert "never say the list was saved" in low and "do not write the steps out" in low


def test_a_valid_call_becomes_a_plan():
    spec = parse_plan(call().args, CPH, NOW, 10)
    assert spec is not None and spec.goal == GOAL
    assert [s.title for s in spec.steps] == ["Собрать тезисы", "Сделать слайды", "Прогнать вслух"] and all(s.due is None for s in spec.steps)


def test_plain_strings_are_accepted_as_steps_and_duplicates_collapse():
    spec = parse_plan({"goal": GOAL, "steps": ["Один", " один ", {"title": "Два"}, "  "]}, CPH, NOW, 10)
    assert spec is not None and [s.title for s in spec.steps] == ["Один", "Два"]


@pytest.mark.parametrize(
    "args",
    [
        {"goal": GOAL, "steps": [{"title": "Один"}]},  # one step is not a breakdown
        {"goal": GOAL, "steps": [{"title": "Один"}, {"title": "‮evil"}]},  # one usable step is left
        {"goal": GOAL, "steps": []},
        {"goal": GOAL, "steps": "первое, второе"},
        {"goal": GOAL},
        {"goal": "", "steps": [{"title": "а"}, {"title": "б"}]},
        {"goal": 5, "steps": [{"title": "а"}, {"title": "б"}]},
        {"goal": "x" * 81, "steps": [{"title": "а"}, {"title": "б"}]},
        {"goal": GOAL, "steps": [{"title": 1}, {"title": None}, 7, None]},
    ],
)
def test_an_unusable_call_is_dropped(args):
    assert parse_plan(args, CPH, NOW, 10) is None


def test_more_steps_than_the_cap_are_cut_not_refused():
    spec = parse_plan({"goal": GOAL, "steps": [{"title": f"Шаг {i}"} for i in range(30)]}, CPH, NOW, 4)
    assert spec is not None and [s.title for s in spec.steps] == ["Шаг 0", "Шаг 1", "Шаг 2", "Шаг 3"]


def test_a_step_time_is_the_users_wall_clock_turned_into_an_instant_by_code():
    spec = parse_plan({"goal": GOAL, "steps": [{"title": "а", "start": "2026-10-08T19:30"}, {"title": "б"}]}, CPH, NOW, 10)
    assert spec is not None
    assert spec.steps[0].due == datetime(2026, 10, 8, 17, 30, tzinfo=UTC)  # Copenhagen is UTC+2 in October (summer time)
    assert spec.steps[1].due is None


@pytest.mark.parametrize("start", ["2026-10-01T10:00", "2031-01-01T10:00", "завтра", "2026-10-08", 5, ""])
def test_a_time_that_passed_or_is_unreadable_keeps_the_step_but_drops_the_time(start):
    spec = parse_plan({"goal": GOAL, "steps": [{"title": "а", "start": start}, {"title": "б"}]}, CPH, NOW, 10)
    assert spec is not None and spec.steps[0].due is None and len(spec.steps) == 2


def test_the_callback_data_round_trips_and_fits_telegram():
    sid = uuid.uuid4()
    for action in "dxp":
        data = decompose.callback_data(sid, action)
        assert len(data.encode()) <= 64 and decompose.parse_callback(data) == (sid, action)
    for bad in ("", "tk:", f"tk:{sid}:z", f"tk:{sid}", f"qe:{sid}:d", "tk:not-a-uuid:d", f"tk:{sid}:d:x"):
        assert decompose.parse_callback(bad) is None


# ================================================================== fixtures and helpers


@pytest_asyncio.fixture
async def task_env(owner_engine, db):
    """``bot_env`` with decomposition on, as in production (the function is still offered only on a marker)."""
    env = await build_bot_env(owner_engine, db, tasks_enabled=True)
    yield env
    await env.bot.session.close()


def engine_of(env, **settings_kw: Any) -> Tasks:
    if not settings_kw:
        return env.orchestrator.tasks
    return Tasks(env.db, env.bot, Settings(_env_file=None, **settings_kw))


async def make_plan(env, *steps: Any, goal: str = GOAL, persona_id: uuid.UUID | None = None, **settings_kw: Any):
    spec = [{"title": t} if isinstance(t, str) else t for t in steps]
    return await engine_of(env, **settings_kw).create(env.user_id, CPH, persona_id, [call(goal, spec)])


async def rows(env, sql: str, **kw: Any):
    return await owner_exec(env.owner_engine, sql, u=env.user_id, **kw)


def card_messages(env, prefix: str = "tk:", thread_id: int | None = None) -> list[Any]:
    return [
        m for m in env.session.sent()
        if m.reply_markup is not None and m.chat_id == env.chat_id and m.message_thread_id == thread_id
        and m.reply_markup.inline_keyboard[0][0].callback_data.startswith(prefix)
    ]  # fmt: skip


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


async def press(env, data: str, *, tg: int | None = None, chat_id: int | None = None, thread_id: int | None = None) -> None:
    await env.dispatcher.feed_update(
        env.bot,
        make_callback_update(data=data, user_id=tg or env.telegram_user_id, chat_id=chat_id or env.chat_id, message_id=888, thread_id=thread_id),
    )


def model_decomposes(env, *calls: ToolCall, text: str = "Всё сохранил в задачи!", then: str = "Начните с тезисов.") -> None:
    """The fake model calls the function - and, as a real one might, ALSO claims it saved everything."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


COUNT_TASKS = "SELECT count(*) FROM civ.tasks WHERE user_id = :u"
COUNT_ROOTS = "SELECT count(*) FROM civ.tasks WHERE user_id = :u AND parent_task_id IS NULL"
COUNT_EVENTS = "SELECT count(*) FROM civ.events WHERE user_id = :u"
COUNT_LINKS = "SELECT count(*) FROM civ.task_events WHERE user_id = :u"
COUNT_DRAFTS = "SELECT count(*) FROM civ.event_drafts WHERE user_id = :u"


# ================================================================== the database


async def test_a_task_belongs_to_one_tenant_and_the_others_cannot_see_it(task_env, owner_engine, db):
    await make_plan(task_env, "а", "б")
    stranger = await new_user(owner_engine)
    assert await tenant_exec(db, stranger, "SELECT count(*) FROM civ.tasks") == [(0,)]
    assert await tenant_exec(db, stranger, "SELECT count(*) FROM civ.task_events") == [(0,)]
    assert await tenant_exec(db, task_env.user_id, "SELECT count(*) FROM civ.tasks") == [(3,)]


async def test_a_step_cannot_have_steps_and_a_task_with_steps_cannot_become_one(task_env, db):
    plan = await make_plan(task_env, "а", "б")
    with pytest.raises(DB_ERRORS, match="step cannot have steps"):
        await tenant_exec(
            db, task_env.user_id, "INSERT INTO civ.tasks (user_id, parent_task_id, title, source) VALUES (:u, :p, 'внук', 'chat_decompose')", u=task_env.user_id, p=plan.steps[0].id
        )
    other = await make_plan(task_env, "в", "г", goal="Другая цель")
    with pytest.raises(DB_ERRORS, match="cannot become a step"):
        await tenant_exec(db, task_env.user_id, "UPDATE civ.tasks SET parent_task_id = :p WHERE id = :r", p=other.root_id, r=plan.root_id)


async def test_the_composite_foreign_keys_refuse_another_tenants_rows(task_env, owner_engine, db):
    mine = await make_plan(task_env, "а", "б")
    stranger = await new_user(owner_engine)
    with pytest.raises(DB_ERRORS):  # a step under MY root, written by the stranger
        await tenant_exec(
            db, stranger, "INSERT INTO civ.tasks (user_id, parent_task_id, title, source) VALUES (:u, :p, 'чужой', 'chat_decompose')", u=stranger, p=mine.root_id
        )
    foreign_persona = await new_persona(owner_engine, task_env.user_id, "gym")
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, stranger, "INSERT INTO civ.tasks (user_id, persona_id, title, source) VALUES (:u, :p, 'х', 'chat_decompose')", u=stranger, p=foreign_persona)
    [(event,)] = await owner_exec(
        owner_engine,
        "INSERT INTO civ.events (user_id, title, starts_at, ends_at, timezone, created_via) "
        "VALUES (:u, 'e', now() + interval '1 day', now() + interval '1 day 1 hour', 'UTC', 'mini_app') RETURNING id",
        u=task_env.user_id,
    )
    with pytest.raises(DB_ERRORS):  # the link of my step to my event, written by the stranger
        await tenant_exec(db, stranger, "INSERT INTO civ.task_events (user_id, task_id, event_id) VALUES (:u, :t, :e)", u=stranger, t=mine.steps[0].id, e=event)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO civ.tasks (user_id, title, source, status) VALUES (:u, 'х', 'chat_decompose', 'finished')",
        "INSERT INTO civ.tasks (user_id, title, source) VALUES (:u, 'х', 'the_model_said_so')",
        "INSERT INTO civ.tasks (user_id, title, source) VALUES (:u, repeat('я', 81), 'chat_decompose')",
        "INSERT INTO civ.tasks (user_id, title, source) VALUES (:u, E'два\\nряда', 'chat_decompose')",
    ],
)
async def test_the_checks_refuse_a_bad_status_source_or_title(task_env, db, sql):
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, task_env.user_id, sql, u=task_env.user_id)


async def test_the_same_goal_cannot_be_open_twice_but_a_closed_one_may_come_back(task_env, db):
    first = await make_plan(task_env, "а", "б")
    assert first is not None and await make_plan(task_env, "в", "г") is None
    await tenant_exec(db, task_env.user_id, "UPDATE civ.tasks SET status = 'done' WHERE id = :r", r=first.root_id)
    assert await make_plan(task_env, "в", "г") is not None


# ================================================================== the engine: making a plan


async def test_a_plan_is_a_root_with_ordered_steps_in_the_topic_it_was_made_in(task_env, owner_engine):
    persona = await new_persona(owner_engine, task_env.user_id, "work", thread_id=7)
    plan = await make_plan(task_env, "Собрать тезисы", "Сделать слайды", "Прогнать вслух", persona_id=persona)
    assert plan is not None and (plan.goal, plan.status) == (GOAL, "open")
    assert [(s.position, s.title, s.status) for s in plan.steps] == [(0, "Собрать тезисы", "open"), (1, "Сделать слайды", "open"), (2, "Прогнать вслух", "open")]
    stored = await rows(
        task_env,
        "SELECT title, parent_task_id IS NULL, persona_id, source FROM civ.tasks WHERE user_id = :u ORDER BY parent_task_id NULLS FIRST, position",
    )
    assert [r[0] for r in stored] == [GOAL, "Собрать тезисы", "Сделать слайды", "Прогнать вслух"] and stored[0][1] is True
    assert all(r[2] == persona and r[3] == "chat_decompose" for r in stored)
    assert await rows(task_env, COUNT_DRAFTS) == [(0,)] and await rows(task_env, COUNT_EVENTS) == [(0,)]  # no slot was made


async def test_a_step_time_is_stored_as_an_instant(task_env):
    text, instant = local(30)
    plan = await make_plan(task_env, {"title": "Слайды", "start": text}, "Тезисы")
    assert plan is not None and plan.steps[0].due == instant and plan.steps[1].due is None


async def test_only_the_first_usable_call_of_a_message_makes_a_plan(task_env):
    calls = [call(steps=[{"title": "один"}]), call("Первая цель"), call("Вторая цель")]  # the first one is unusable
    plan = await engine_of(task_env).create(task_env.user_id, CPH, None, calls)
    assert plan is not None and plan.goal == "Первая цель"
    assert await rows(task_env, COUNT_ROOTS) == [(1,)]


async def test_calls_that_are_not_ours_make_nothing(task_env):
    assert await engine_of(task_env).create(task_env.user_id, CPH, None, [ToolCall("create_event", call().args)]) is None
    assert await rows(task_env, COUNT_TASKS) == [(0,)]


async def test_too_many_open_tasks_make_no_new_plan(task_env):
    assert await make_plan(task_env, "а", "б", goal="Цель 1", tasks_max_open=1) is not None
    assert await make_plan(task_env, "а", "б", goal="Цель 2", tasks_max_open=1) is None


async def test_a_database_failure_makes_no_plan_and_never_raises(task_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Tasks, "_insert", boom)
    assert await make_plan(task_env, "а", "б") is None
    assert await rows(task_env, COUNT_TASKS) == [(0,)]


# ================================================================== the engine: the buttons


async def test_done_and_dropped_close_a_step_and_the_root_closes_with_its_last_step(task_env):
    plan = await make_plan(task_env, "а", "б", "в")
    a, b, c = (s.id for s in plan.steps)
    engine = engine_of(task_env)
    first = await engine.press(task_env.user_id, CPH, a, "d")
    assert (first.outcome, first.plan.status) == ("done", "open")
    second = await engine.press(task_env.user_id, CPH, b, "x")
    assert (second.outcome, [s.status for s in second.plan.steps]) == ("dropped", ["done", "dropped", "open"])
    last = await engine.press(task_env.user_id, CPH, c, "d")
    assert (last.plan.status, [s.status for s in last.plan.steps]) == ("done", ["done", "dropped", "done"])


async def test_a_root_whose_steps_were_all_dropped_is_dropped(task_env):
    plan = await make_plan(task_env, "а", "б")
    engine = engine_of(task_env)
    for step in plan.steps:
        result = await engine.press(task_env.user_id, CPH, step.id, "x")
    assert result.plan.status == "dropped"


async def test_a_second_press_shows_what_was_decided_and_changes_nothing(task_env):
    plan = await make_plan(task_env, "а", "б")
    engine = engine_of(task_env)
    await engine.press(task_env.user_id, CPH, plan.steps[0].id, "d")
    again = await engine.press(task_env.user_id, CPH, plan.steps[0].id, "x")  # "dropped" after "done"
    assert (again.outcome, again.plan.steps[0].status) == ("settled", "done")


async def test_parallel_presses_of_one_step_decide_it_exactly_once(task_env):
    plan = await make_plan(task_env, "а", "б")
    results = await asyncio.gather(*(engine_of(task_env).press(task_env.user_id, CPH, plan.steps[0].id, "d") for _ in range(6)))
    assert sorted(r.outcome for r in results) == ["done"] + ["settled"] * 5


async def test_parallel_presses_of_different_steps_close_the_root_once_without_a_deadlock(task_env):
    plan = await make_plan(task_env, "а", "б", "в", "г")
    results = await asyncio.gather(*(engine_of(task_env).press(task_env.user_id, CPH, s.id, "d") for s in plan.steps))
    assert all(r.outcome == "done" for r in results)
    assert await rows(task_env, "SELECT status FROM civ.tasks WHERE user_id = :u AND parent_task_id IS NULL") == [("done",)]


async def test_a_root_a_stranger_and_a_missing_id_are_gone(task_env, owner_engine):
    plan = await make_plan(task_env, "а", "б")
    stranger = await new_user(owner_engine)
    engine = engine_of(task_env)
    assert (await engine.press(task_env.user_id, CPH, plan.root_id, "d")).outcome == "gone"  # only steps have buttons
    assert (await engine.press(task_env.user_id, CPH, uuid.uuid4(), "d")).outcome == "gone"
    assert (await engine.press(stranger, CPH, plan.steps[0].id, "d")).outcome == "gone"  # RLS: the row does not exist for them
    assert await rows(task_env, "SELECT count(*) FROM civ.tasks WHERE user_id = :u AND status = 'open'") == [(3,)]


# ================================================================== the engine: slots (never silent)


async def test_the_slot_button_makes_a_draft_and_no_event(task_env):
    text, instant = local(30)
    plan = await make_plan(task_env, {"title": "Слайды", "start": text}, "Тезисы")
    result = await engine_of(task_env).press(task_env.user_id, CPH, plan.steps[0].id, "p")
    assert result.outcome == "slot" and result.draft is not None
    assert (result.draft.title, result.draft.start, result.draft.end - result.draft.start) == ("Слайды", instant, timedelta(minutes=60))
    assert await rows(task_env, "SELECT status, task_id, starts_at, timezone FROM civ.event_drafts WHERE user_id = :u") == [
        ("pending", plan.steps[0].id, instant, CPH)
    ]
    assert await rows(task_env, COUNT_EVENTS) == [(0,)] and await rows(task_env, COUNT_LINKS) == [(0,)]


async def test_a_second_slot_press_while_the_card_waits_makes_no_second_draft(task_env):
    text, _ = local(30)
    plan = await make_plan(task_env, {"title": "Слайды", "start": text}, "Тезисы")
    engine = engine_of(task_env)
    await engine.press(task_env.user_id, CPH, plan.steps[0].id, "p")
    again = await engine.press(task_env.user_id, CPH, plan.steps[0].id, "p")
    assert (again.outcome, again.draft) == ("waiting", None)
    assert await rows(task_env, COUNT_DRAFTS) == [(1,)]


async def test_parallel_slot_presses_make_exactly_one_draft(task_env):
    text, _ = local(30)
    plan = await make_plan(task_env, {"title": "Слайды", "start": text}, "Тезисы")
    results = await asyncio.gather(*(engine_of(task_env).press(task_env.user_id, CPH, plan.steps[0].id, "p") for _ in range(6)))
    assert sorted(r.outcome for r in results) == ["slot"] + ["waiting"] * 5


async def test_a_step_without_a_future_time_has_no_slot(task_env, owner_engine):
    plan = await make_plan(task_env, "Тезисы", "Слайды")
    assert (await engine_of(task_env).press(task_env.user_id, CPH, plan.steps[0].id, "p")).outcome == "no_slot"
    text, _ = local(30)
    timed = await make_plan(task_env, {"title": "Позже", "start": text}, "Ещё", goal="Вторая цель")
    await owner_exec(owner_engine, "UPDATE civ.tasks SET due = now() - interval '1 hour' WHERE id = :s", s=timed.steps[0].id)  # the time has passed since
    assert (await engine_of(task_env).press(task_env.user_id, CPH, timed.steps[0].id, "p")).outcome == "no_slot"
    assert await rows(task_env, COUNT_DRAFTS) == [(0,)]


async def test_a_closed_step_gets_no_slot(task_env):
    text, _ = local(30)
    plan = await make_plan(task_env, {"title": "Слайды", "start": text}, "Тезисы")
    engine = engine_of(task_env)
    await engine.press(task_env.user_id, CPH, plan.steps[0].id, "x")
    assert (await engine.press(task_env.user_id, CPH, plan.steps[0].id, "p")).outcome == "settled"


async def test_too_many_open_drafts_refuse_a_new_slot(task_env):
    text, _ = local(30)
    plan = await make_plan(task_env, {"title": "Слайды", "start": text}, "Тезисы")
    engine = Tasks(task_env.db, task_env.bot, Settings(_env_file=None, quick_events_max_pending=0))
    assert (await engine.press(task_env.user_id, CPH, plan.steps[0].id, "p")).outcome == "limit"


async def test_confirming_the_slot_writes_the_event_and_links_it_to_the_step(task_env):
    text, instant = local(30)
    plan = await make_plan(task_env, {"title": "Слайды", "start": text}, "Тезисы")
    pressed = await engine_of(task_env).press(task_env.user_id, CPH, plan.steps[0].id, "p")
    quick = task_env.orchestrator.quick
    assert (await quick.decide(task_env.user_id, pressed.draft.id, True)).outcome == "confirmed"
    [(event, via, vis, starts)] = await rows(task_env, "SELECT id, created_via, visibility::text, starts_at FROM civ.events WHERE user_id = :u")
    assert (via, vis, starts) == ("bot_confirmed", "busy_only", instant)
    assert await rows(task_env, "SELECT task_id, event_id FROM civ.task_events WHERE user_id = :u") == [(plan.steps[0].id, event)]
    again = await engine_of(task_env).press(task_env.user_id, CPH, plan.steps[0].id, "p")
    assert again.outcome == "planned" and again.plan.steps[0].planned  # one slot per step
    await quick.decide(task_env.user_id, pressed.draft.id, True)  # a double click of "Yes" changes nothing
    assert await rows(task_env, COUNT_LINKS) == [(1,)] and await rows(task_env, COUNT_EVENTS) == [(1,)]


async def test_declining_the_slot_links_nothing_and_a_new_slot_may_be_offered(task_env):
    text, _ = local(30)
    plan = await make_plan(task_env, {"title": "Слайды", "start": text}, "Тезисы")
    pressed = await engine_of(task_env).press(task_env.user_id, CPH, plan.steps[0].id, "p")
    assert (await task_env.orchestrator.quick.decide(task_env.user_id, pressed.draft.id, False)).outcome == "dismissed"
    assert await rows(task_env, COUNT_LINKS) == [(0,)]
    assert (await engine_of(task_env).press(task_env.user_id, CPH, plan.steps[0].id, "p")).outcome == "slot"


async def test_deleting_the_event_removes_only_the_link(task_env, owner_engine):
    text, _ = local(30)
    plan = await make_plan(task_env, {"title": "Слайды", "start": text}, "Тезисы")
    pressed = await engine_of(task_env).press(task_env.user_id, CPH, plan.steps[0].id, "p")
    await task_env.orchestrator.quick.decide(task_env.user_id, pressed.draft.id, True)
    await owner_exec(owner_engine, "DELETE FROM civ.events WHERE user_id = :u", u=task_env.user_id)
    assert await rows(task_env, COUNT_LINKS) == [(0,)] and await rows(task_env, COUNT_TASKS) == [(3,)]


# ================================================================== the card


async def test_the_card_shows_the_steps_the_marks_and_only_the_buttons_that_make_sense(task_env):
    text, _ = local(30)
    plan = await make_plan(task_env, {"title": "Слайды", "start": text}, "Тезисы", "Прогон")
    shown = decompose.card_text(plan, CPH)
    assert shown.splitlines()[0] == f"📋 {GOAL} — 0/3" and "◻️ 1. Слайды — " in shown and "◻️ 2. Тезисы" in shown
    keyboard = decompose.card_markup(plan).inline_keyboard
    assert [[b.text for b in row] for row in keyboard] == [["✅ 1", "🚫 1", "📅 1"], ["✅ 2", "🚫 2"], ["✅ 3", "🚫 3"]]  # a slot only for the timed step
    closed = await engine_of(task_env).press(task_env.user_id, CPH, plan.steps[1].id, "d")
    assert "✅ 2. Тезисы" in decompose.card_text(closed.plan, CPH)
    assert [row[0].text for row in decompose.card_markup(closed.plan).inline_keyboard] == ["✅ 1", "✅ 3"]  # a closed step has no buttons


async def test_a_card_with_nothing_left_to_press_has_no_keyboard(task_env):
    plan = await make_plan(task_env, "а", "б")
    engine = engine_of(task_env)
    await engine.press(task_env.user_id, CPH, plan.steps[0].id, "d")
    last = await engine.press(task_env.user_id, CPH, plan.steps[1].id, "x")
    assert decompose.card_markup(last.plan) is None and decompose.card_text(last.plan, CPH).startswith(f"✅ {GOAL} — 1/2")


# ================================================================== the whole flow through the Dispatcher


async def test_a_marker_message_gets_the_function_and_the_reply_is_a_card_the_model_did_not_write(task_env):
    env = task_env
    model_decomposes(env, call())
    await env.feed(ASK)
    offered = env.chat.tool_calls[0]
    assert [t.name for t in offered["tools"]] == ["decompose_task"] and "## Task breakdown" in offered["system"]
    assert not any("сохранил" in t.lower() for t in says(env))  # the model's own words next to the call are never sent
    assert "Начните с тезисов." in says(env)  # the reply was phrased again, with the note of what the system did
    assert "Task breakdown (made by the system)" in env.chat.calls[-1]["system"] and "Собрать тезисы" in env.chat.calls[-1]["system"]
    [card] = card_messages(env)
    assert card.text.startswith(f"📋 {GOAL} — 0/3") and "◻️ 1. Собрать тезисы" in card.text
    assert [b.callback_data[:3] for row in card.reply_markup.inline_keyboard for b in row] == ["tk:"] * 6
    assert await rows(env, COUNT_TASKS) == [(4,)]
    assert not any("◻️" in (m["content"] or "") for m in await env.messages())  # the card is not a word of the assistant: not remembered as one


async def test_a_message_without_a_marker_is_never_offered_the_function(task_env):
    env = task_env
    env.chat.responder = lambda system, turns: "Расскажите подробнее."
    await env.feed("Хочу подготовить презентацию к пятнице, а времени мало")
    assert env.chat.tool_calls == [] and len(env.chat.calls) == 1  # the plain path: no function declarations at all
    assert "Task breakdown" not in env.chat.calls[0]["system"]


async def test_the_switch_turns_the_function_off_even_on_a_marker(owner_engine, db):
    env = await build_bot_env(owner_engine, db, tasks_enabled=False)
    try:
        env.chat.responder = lambda system, turns: "Хорошо."
        await env.feed(ASK)
        assert env.chat.tool_calls == [] and card_messages(env) == []
    finally:
        await env.bot.session.close()


async def test_a_garbled_call_gives_no_card_and_the_models_claim_is_not_sent(task_env):
    env = task_env
    model_decomposes(env, call(steps=[{"title": "единственный"}]), then="Не получилось разложить, опишите подробнее.")
    await env.feed(ASK)
    assert card_messages(env) == [] and await rows(env, COUNT_TASKS) == [(0,)]
    assert not any("сохранил" in t.lower() for t in says(env))  # a function of ours was called and nothing ran: the words are not trusted


async def test_the_card_goes_to_the_topic_the_user_wrote_in(task_env, owner_engine):
    env = task_env
    await new_persona(owner_engine, env.user_id, "work", thread_id=55, title="Работа")
    model_decomposes(env, call())
    await env.feed(ASK, thread_id=55)
    assert len(card_messages(env, thread_id=55)) == 1 and card_messages(env) == []
    assert {r[0] for r in await rows(env, "SELECT persona_id FROM civ.tasks WHERE user_id = :u")} != {None}


async def test_a_voice_request_is_decomposed_like_a_typed_one(task_env):
    env = task_env
    env.chat.transcriber = lambda data, mime: ASK
    model_decomposes(env, call())
    await env.feed_voice()
    assert len(card_messages(env)) == 1


async def test_pressing_done_edits_the_card_and_answers_the_press(task_env):
    env = task_env
    model_decomposes(env, call())
    await env.feed(ASK)
    [card] = card_messages(env)
    first_done = card.reply_markup.inline_keyboard[0][0].callback_data
    await press(env, first_done)
    [edit] = env.session.edits()
    assert edit.text.startswith(f"📋 {GOAL} — 1/3") and "✅ 1. Собрать тезисы" in edit.text
    assert [row[0].text for row in edit.reply_markup.inline_keyboard] == ["✅ 2", "✅ 3"]
    assert [a.text for a in env.session.callback_answers()] == [texts.TASK_TOAST["done"]]
    await press(env, first_done)  # the same button again
    assert env.session.callback_answers()[-1].text == texts.TASK_TOAST["settled"]
    assert await rows(env, "SELECT count(*) FROM civ.tasks WHERE user_id = :u AND status = 'done'") == [(1,)]


async def test_closing_every_step_removes_the_buttons(task_env):
    env = task_env
    model_decomposes(env, call())
    await env.feed(ASK)
    [card] = card_messages(env)
    for row in card.reply_markup.inline_keyboard:
        await press(env, row[0].callback_data)
    last = env.session.edits()[-1]
    assert last.text.startswith(f"✅ {GOAL} — 3/3") and not (last.reply_markup and last.reply_markup.inline_keyboard)
    assert await rows(env, "SELECT status FROM civ.tasks WHERE user_id = :u AND parent_task_id IS NULL") == [("done",)]


async def test_the_slot_button_sends_the_usual_calendar_card_and_only_its_yes_writes_the_event(task_env):
    env = task_env
    text, instant = local(30)
    model_decomposes(env, call(steps=[{"title": "Слайды", "start": text}, {"title": "Тезисы"}]))
    await env.feed(ASK)
    [card] = card_messages(env)
    slot = next(b.callback_data for row in card.reply_markup.inline_keyboard for b in row if b.text.startswith("📅"))
    await press(env, slot)
    [qe] = card_messages(env, "qe:")
    assert qe.text.startswith("Занести в календарь: «Слайды»")
    assert await rows(env, COUNT_EVENTS) == [(0,)]  # a draft and a question, nothing in the calendar yet
    await press(env, qe.reply_markup.inline_keyboard[0][0].callback_data)
    assert await rows(env, "SELECT starts_at FROM civ.events WHERE user_id = :u") == [(instant,)]
    assert await rows(env, COUNT_LINKS) == [(1,)]
    await press(env, slot)  # the slot button once more: it is in the calendar already
    assert env.session.callback_answers()[-1].text == texts.TASK_TOAST["planned"] and len(card_messages(env, "qe:")) == 1


async def test_a_press_for_a_step_without_a_time_says_so_and_sends_no_card(task_env):
    env = task_env
    model_decomposes(env, call())
    await env.feed(ASK)
    [(step,)] = await rows(env, "SELECT id FROM civ.tasks WHERE user_id = :u AND position = 0 AND parent_task_id IS NOT NULL")
    await press(env, decompose.callback_data(step, "p"))
    assert env.session.callback_answers()[-1].text == texts.TASK_TOAST["no_slot"] and card_messages(env, "qe:") == []


async def test_a_strangers_press_reaches_nothing(task_env, owner_engine):
    env = task_env
    model_decomposes(env, call())
    await env.feed(ASK)
    [card] = card_messages(env)
    data = card.reply_markup.inline_keyboard[0][0].callback_data
    stranger_tg, stranger_chat = random.randrange(10**9, 10**12), -int(f"100{random.randrange(10**9, 10**10)}")  # fresh ids: other tests leave tenants with fixed ones behind
    await new_user(owner_engine, telegram_user_id=stranger_tg, forum_chat_id=stranger_chat)
    await press(env, data, tg=stranger_tg, chat_id=stranger_chat)
    assert env.session.callback_answers()[-1].text == texts.TASK_TOAST["gone"]  # RLS: the row does not exist for them
    assert await rows(env, "SELECT count(*) FROM civ.tasks WHERE user_id = :u AND status = 'done'") == [(0,)]


async def test_a_forged_payload_is_an_unknown_button(task_env):
    env = task_env
    await press(env, "tk:garbage:d")
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_UNKNOWN_BUTTON


async def test_a_database_failure_on_a_press_is_visible_and_changes_nothing(task_env, monkeypatch):
    env = task_env
    model_decomposes(env, call())
    await env.feed(ASK)
    [card] = card_messages(env)

    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Tasks, "press", boom)
    await press(env, card.reply_markup.inline_keyboard[0][0].callback_data)
    assert env.session.callback_answers()[-1].text == texts.FAILURE
    assert await rows(env, "SELECT count(*) FROM civ.tasks WHERE user_id = :u AND status = 'done'") == [(0,)]


async def test_the_same_request_twice_makes_one_open_task(task_env):
    env = task_env
    model_decomposes(env, call(), then="Ок.")
    await env.feed(ASK)
    await env.feed(ASK + " ещё раз")
    assert len(card_messages(env)) == 1 and await rows(env, COUNT_ROOTS) == [(1,)]

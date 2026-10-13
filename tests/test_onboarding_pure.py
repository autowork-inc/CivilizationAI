"""Onboarding without a database: validation of every answer, the summary, the confirmation, the state machine."""

from __future__ import annotations

import json
from datetime import time
from typing import Any

import pytest

from ai_civ.bot import onboarding
from ai_civ.bot.onboarding import (
    CONFIRM_STEP,
    STEP_BY_KEY,
    STEPS,
    Progress,
    parse_confirmation,
    parse_step,
    render_summary,
)
from ai_civ.bot.proactive import TRIGGERS, in_quiet_hours
from ai_civ.db.enums import OnboardingState


def parse(key: str, payload: Any, answer: str) -> onboarding.Parsed:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return parse_step(STEP_BY_KEY[key], raw, answer)


# ---------------------------------------------------------------- the script itself


def test_the_script_has_the_agreed_blocks_and_every_scheduled_trigger_exists():
    assert [s.key for s in STEPS] == ["name", "place", "occupation", "style", "goals", "principles", "schedule"]
    assert CONFIRM_STEP == len(STEPS) + 1
    assert set(onboarding.SCHEDULED_TRIGGERS) <= set(TRIGGERS)  # a typo here would silently drop a reminder
    for step in STEPS:  # every step has a usable response schema and a numbered question
        assert step.schema["type"] == "object" and onboarding.question_text(STEPS.index(step) + 1).startswith("Вопрос")


# ---------------------------------------------------------------- grounding: nothing the user did not write


def test_a_name_must_occur_in_the_answer_so_the_model_cannot_invent_one():
    ok = parse("name", {"name": "Анна", "address_as": "Аня"}, "Меня зовут Анна, можно Аня")
    assert ok.data == {"name": "Анна", "address_as": "Аня"}
    invented = parse("name", {"name": "Мария", "address_as": "Мария"}, "Меня зовут Анна")
    assert invented.data is None and "имя" in invented.problem


def test_an_address_that_is_not_in_the_answer_falls_back_to_the_name():
    assert parse("name", {"name": "Анна", "address_as": "Анечка"}, "Анна").data == {"name": "Анна", "address_as": "Анна"}


def test_the_city_must_occur_in_the_answer_and_the_zone_must_be_a_real_iana_zone():
    ok = parse("place", {"city": "Копенгаген", "timezone": "Europe/Copenhagen"}, "Живу в Копенгагене")
    assert ok.data == {"city": "Копенгаген", "timezone": "Europe/Copenhagen"}
    assert parse("place", {"city": "Париж", "timezone": "Europe/Paris"}, "Живу в Копенгагене").data is None
    bogus = parse("place", {"city": "Копенгаген", "timezone": "Mars/Olympus"}, "Копенгаген")
    assert bogus.data is None and "часовой пояс" in bogus.problem
    assert parse("place", {"city": "Копенгаген", "timezone": "UTC"}, "Копенгаген").data is None  # not a place
    assert parse("place", {"city": "", "timezone": ""}, "не скажу").data is None


@pytest.mark.parametrize("raw", ["not json", "[]", '{"name": "Анна"}', '{"name": "Анна", "address_as": "Аня", "x": 1}'])
def test_malformed_model_output_asks_again_instead_of_storing_anything(raw):
    parsed = parse("name", raw, "Меня зовут Анна")
    assert parsed.data is None and parsed.problem


# ---------------------------------------------------------------- the other blocks


def test_style_needs_at_least_one_wish_and_keeps_unspecified_out_of_the_facts():
    empty = parse("style", {"formality": "unspecified", "length": "unspecified", "tone": "unspecified", "notes": ""}, "хз")
    assert empty.data is None
    ok = parse("style", {"formality": "ty", "length": "unspecified", "tone": "strict", "notes": ""}, "на ты и строго")
    assert ok.data is not None
    facts = {f.key: f.value for f in STEP_BY_KEY["style"].facts(ok.data)}
    assert set(facts) == {"communication.formality", "communication.tone"}  # no 'length' fact was invented
    assert "informal" in facts["communication.formality"]


def test_goals_are_numbered_by_the_code_and_capped():
    many = {"goals": [f"цель {i}" for i in range(9)]}
    data = parse("goals", many, "много целей").data
    assert data is not None and len(data["goals"]) == onboarding.MAX_GOALS
    facts = STEP_BY_KEY["goals"].facts(data)
    assert [f.key for f in facts] == [f"goal.{i}" for i in range(1, onboarding.MAX_GOALS + 1)]
    assert parse("goals", {"goals": ["", "  "]}, "...").data is None


def test_principles_get_validated_unique_keys_and_none_is_a_valid_answer():
    items = [
        {"slug": "no_danish", "text": "не ем выпечку"},
        {"slug": "no_danish", "text": "не ем датскую выпечку"},  # duplicate slug
        {"slug": "Bad Slug!", "text": "дропается"},  # invalid slug: dropped, not repaired
        {"slug": "sleep", "text": ""},  # empty text: dropped
    ]
    data = parse("principles", {"has_principles": True, "principles": items}, "...").data
    assert data is not None
    assert [p["slug"] for p in data["principles"]] == ["no_danish", "no_danish_2"]
    assert [f.key for f in STEP_BY_KEY["principles"].facts(data)] == ["principle.no_danish", "principle.no_danish_2"]
    assert all(f.priority >= 60 for f in STEP_BY_KEY["principles"].facts(data))  # an inferred emotion cannot override them
    assert parse("principles", {"has_principles": False, "principles": []}, "нет").data == {"principles": []}
    assert parse("principles", {"has_principles": True, "principles": []}, "есть, но не скажу").data is None


def test_schedule_resolves_defaults_off_and_shows_what_was_defaulted():
    payload = {"wake_time": "07:00", "sleep_time": "23:00", "meal_check_time": "13:30", "work_start_time": "off"}
    data = parse("schedule", payload, "...").data
    assert data == {
        "wake": "07:00", "sleep": "23:00", "meal_check": "13:30", "work_start": "off", "defaulted": [],
    }  # fmt: skip
    partial = parse("schedule", {**payload, "sleep_time": "", "meal_check_time": "", "work_start_time": ""}, "...").data
    assert partial is not None
    assert partial["sleep"] == "22:00" and partial["meal_check"] == "13:00" and partial["work_start"] == "08:00"
    assert set(partial["defaulted"]) == {"sleep", "meal_check", "work_start"}
    assert "(по умолчанию)" in STEP_BY_KEY["schedule"].summary(partial)


def test_a_default_work_reminder_follows_the_wake_up_time():
    data = parse("schedule", {"wake_time": "06:30", "sleep_time": "22:00", "meal_check_time": "", "work_start_time": ""}, "x")
    assert data.data is not None and data.data["work_start"] == "07:30"


@pytest.mark.parametrize(
    "payload",
    [
        {"wake_time": "", "sleep_time": "", "meal_check_time": "", "work_start_time": ""},  # nothing said at all
        {"wake_time": "7am", "sleep_time": "", "meal_check_time": "", "work_start_time": ""},  # not HH:MM
        {"wake_time": "25:00", "sleep_time": "", "meal_check_time": "", "work_start_time": ""},
        {"wake_time": "07:00", "sleep_time": "07:00", "meal_check_time": "", "work_start_time": ""},  # no window
        {"wake_time": "off", "sleep_time": "", "meal_check_time": "", "work_start_time": ""},  # only reminders can be off
    ],
)
def test_unusable_schedules_are_rejected(payload):
    assert parse("schedule", payload, "...").data is None


def test_a_reminder_inside_the_users_own_quiet_hours_is_rejected_with_an_explanation():
    parsed = parse(
        "schedule",
        {"wake_time": "07:00", "sleep_time": "23:00", "meal_check_time": "23:30", "work_start_time": "09:00"},
        "...",
    )
    assert parsed.data is None and "тихие часы" in parsed.problem and "23:30" in parsed.problem


def test_quiet_hours_may_wrap_around_midnight():
    assert in_quiet_hours(time(23, 30), time(22, 0), time(8, 0))
    assert in_quiet_hours(time(3, 0), time(22, 0), time(8, 0))
    assert not in_quiet_hours(time(8, 0), time(22, 0), time(8, 0))  # the end is exclusive
    assert not in_quiet_hours(time(13, 0), time(22, 0), time(8, 0))
    assert in_quiet_hours(time(14, 0), time(13, 0), time(15, 0))  # a window inside one day
    assert not in_quiet_hours(time(15, 0), time(13, 0), time(15, 0))


# ---------------------------------------------------------------- confirmation


@pytest.mark.parametrize("text", ["да", "Да!", " всё верно ", "ОК", "yes", "👍", "+"])
def test_confirmation_yes(text):
    assert parse_confirmation(text).kind == "yes"


@pytest.mark.parametrize("text, step", [("4", 4), ("пункт 3", 3), ("№2", 2), ("7.", 7)])
def test_confirmation_number_reopens_that_point(text, step):
    assert parse_confirmation(text) == onboarding.Confirmation("redo", step)


@pytest.mark.parametrize("text", ["8", "0", "нет", "да, но поправь 3", "может быть", ""])
def test_confirmation_anything_else_is_unclear(text):
    assert parse_confirmation(text).kind == "unclear"


# ---------------------------------------------------------------- state machine (no database, scripted model)

ANSWERS: dict[str, tuple[str, dict[str, Any]]] = {
    "name": ("Меня зовут Анна, можно Аня", {"name": "Анна", "address_as": "Аня"}),
    "place": ("Я из Алматы", {"city": "Алматы", "timezone": "Asia/Almaty"}),
    "occupation": ("Работаю программистом", {"occupation": "программист"}),
    "style": ("На ты, коротко", {"formality": "ty", "length": "brief", "tone": "unspecified", "notes": ""}),
    "goals": ("Найти работу", {"goals": ["найти работу"]}),
    "principles": ("Нет", {"has_principles": False, "principles": []}),
    "schedule": (
        "Встаю в 7, сплю в 23",
        {"wake_time": "07:00", "sleep_time": "23:00", "meal_check_time": "13:00", "work_start_time": "09:00"},
    ),
}


async def run_interview(progress: Progress, upto: int = len(STEPS)) -> Progress:
    async def ask(step: onboarding.Step, text: str) -> str:
        return json.dumps(ANSWERS[step.key][1])

    for step in STEPS[:upto]:
        turn = await onboarding.next_turn(ask, progress, ANSWERS[step.key][0])
        progress = turn.progress
    return progress


async def test_the_first_message_only_starts_the_interview():
    async def ask(step, text):  # pragma: no cover - must not be called
        raise AssertionError("the model must not be asked about the opening message")

    turn = await onboarding.next_turn(ask, Progress(OnboardingState.pending, 0, {}), "привет")
    assert turn.progress == Progress(OnboardingState.in_progress, 1, {})
    assert turn.reply.startswith(onboarding.INTRO) and "Вопрос 1 из 7" in turn.reply


async def test_a_full_interview_ends_in_a_summary_and_a_confirmation_finishes():
    progress = await run_interview(Progress(OnboardingState.in_progress, 1, {}))
    assert progress.step == CONFIRM_STEP and set(progress.answers) == set(STEP_BY_KEY)
    summary = render_summary(progress.answers)
    assert "1. Имя: Анна (обращаться: Аня)" in summary and "Asia/Almaty" in summary and "Принципы: нет" in summary

    async def never(step, text):  # pragma: no cover
        raise AssertionError("confirmation is deterministic: no model call")

    turn = await onboarding.next_turn(never, progress, "да")
    assert turn.finished and turn.progress == progress


async def test_a_wrong_answer_changes_nothing_and_repeats_the_question():
    async def ask(step, text):
        return json.dumps({"name": "Мария", "address_as": "Мария"})  # not what the user wrote

    start = Progress(OnboardingState.in_progress, 1, {})
    turn = await onboarding.next_turn(ask, start, "Меня зовут Анна")
    assert turn.progress == start and not turn.finished
    assert turn.reply.endswith(onboarding.question_text(1))


async def test_correcting_a_point_returns_straight_to_the_summary():
    progress = await run_interview(Progress(OnboardingState.in_progress, 1, {}))

    async def ask(step, text):
        assert step.key == "goals"
        return json.dumps({"goals": ["выучить датский"]})

    redo = await onboarding.next_turn(ask, progress, "5")
    assert redo.progress.step == 5 and redo.progress.returning and redo.reply.startswith(onboarding.REDO_PREFIX)
    back = await onboarding.next_turn(ask, redo.progress, "Выучить датский")
    assert back.progress.step == CONFIRM_STEP and not back.progress.returning  # NOT question 6
    assert back.progress.answers["goals"] == {"goals": ["выучить датский"]} and "выучить датский" in back.reply
    assert back.progress.answers["name"] == progress.answers["name"]  # the rest is untouched


async def test_an_unclear_confirmation_repeats_the_summary():
    progress = await run_interview(Progress(OnboardingState.in_progress, 1, {}))
    turn = await onboarding.next_turn(None, progress, "ну так себе")  # type: ignore[arg-type]
    assert turn.progress == progress and turn.reply.startswith(onboarding.CONFIRM_UNCLEAR) and "1. Имя" in turn.reply


def test_the_facts_of_a_full_interview_are_global_onboarding_facts_with_valid_keys():
    import re

    answers = {key: parse(key, ANSWERS[key][1], ANSWERS[key][0]).data for key in ANSWERS}
    facts = onboarding.facts_from_answers(answers)  # type: ignore[arg-type]
    assert {f.key for f in facts} == {
        "name", "address_as", "city", "occupation", "communication.formality", "communication.length", "goal.1",
    }  # fmt: skip
    for fact in facts:
        assert re.match(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$", fact.key) and len(fact.key) <= 100 and fact.value

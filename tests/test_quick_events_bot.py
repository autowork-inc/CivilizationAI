"""Module 5, step 5, part 3: quick events. The user says what they ARE DOING ("иду на 2 часа смотреть стенку"); the model may call
``create_event(title, start, duration_minutes)``; the code validates every argument, writes a draft into ``civ.event_drafts`` and sends a
card with two buttons UNDER the model's reply. Only the button turns the draft into a row of ``civ.events``. Real PostgreSQL, the real
aiogram Dispatcher on a fake Telegram wire, a fake Gemini whose "model" decides in the test what to call. The point under test: whatever
the user reads about the calendar is written by CODE from what the database says - a model that claims "saved" changes nothing."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ai_civ import tasks
from ai_civ.bot import diplomat, quick_events, texts
from ai_civ.bot.quick_events import Decision, Draft, QuickEvents
from ai_civ.celery_app import app
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolReply
from ai_civ.runtime import Runtime
from tests.helpers import new_persona, owner_exec
from tests.test_diplomat_bot import FREE, befriend
from tests.tg_fakes import make_callback_update

CPH = "Europe/Copenhagen"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # a Saturday; 14:00 in Copenhagen


def plan(title: str = "Стенка", start: str = "2026-10-03T16:00", **kw: Any) -> dict[str, Any]:
    return {"title": title, "start": start, **kw}


def create_call(title: str = "Стенка", start: str = "2026-10-03T16:00", **kw: Any) -> ToolCall:
    return ToolCall(quick_events.TOOL_NAME, plan(title, start, **kw))


# ================================================================== pure: the tool and the call


def test_the_tool_asks_for_a_title_and_a_start_and_is_plain_json():
    schema = quick_events.TOOL.parameters
    assert quick_events.TOOL.name == "create_event" == quick_events.TOOL_NAME
    assert set(schema["required"]) == {"title", "start"}  # the duration is optional: the code has a default
    assert "no time zone suffix" in schema["properties"]["start"]["description"] and "'now'" in schema["properties"]["start"]["description"]
    assert json.dumps(schema)
    assert quick_events.TOOL.name not in {t.name for t in diplomat.TOOLS}  # offered to everybody, separately from the contact functions


def test_the_instructions_forbid_saying_the_event_is_saved():
    low = quick_events.INSTRUCTIONS.lower()
    assert "never say the event was saved" in low and "button" in low


def test_a_valid_plan_is_turned_into_instants_by_code():
    event = quick_events.parse_quick(plan("  Стенка   вечером ", duration_minutes=120), CPH, NOW)
    assert event is not None and event.title == "Стенка вечером"
    assert event.start == datetime(2026, 10, 3, 14, 0, tzinfo=UTC)  # 16:00 on the USER's wall clock (UTC+2 that day)
    assert event.end - event.start == timedelta(hours=2)
    default = quick_events.parse_quick(plan(), CPH, NOW)
    assert default is not None and default.end - default.start == timedelta(minutes=diplomat.DEFAULT_MINUTES)


@pytest.mark.parametrize("word", ["now", "NOW", "  Now  "])
def test_the_word_now_is_our_clock_to_the_minute_never_the_models(word):
    event = quick_events.parse_quick(plan(start=word, duration_minutes=90), "Asia/Tokyo", NOW.replace(second=37, microsecond=5))
    assert event is not None and event.start == NOW and event.end == NOW + timedelta(minutes=90)  # no zone is involved: an instant


def test_a_plan_that_began_a_while_ago_and_is_still_going_is_fine_one_that_is_over_is_not():
    def parse(start: str, minutes: int):
        return quick_events.parse_quick(plan(start=start, duration_minutes=minutes), CPH, NOW)

    assert parse("2026-10-03T09:00", 480) is not None  # began 5 hours ago (07:00 UTC), ends 15:00 UTC
    assert parse("2026-10-03T09:00", 60) is None  # began 5 hours ago and is already over
    assert parse("2026-10-02T23:30", 1440) is None  # began 14.5 hours ago: older than MAX_PAST even though it is still going
    assert parse("2026-10-03T13:50", 15) is not None  # began 10 minutes ago (11:50 UTC) and ends at 12:05 UTC, after "now"


def test_a_plan_beyond_a_year_is_refused():
    assert quick_events.parse_quick(plan(start="2027-10-02T19:00"), CPH, NOW) is not None
    assert quick_events.parse_quick(plan(start="2027-10-05T19:00"), CPH, NOW) is None


@pytest.mark.parametrize("title", ["", "   ", None, 5, ["x"], "x" * 81, "bidi‮evil", "bell\x07"])
def test_an_unusable_title_drops_the_call(title):
    assert quick_events.parse_quick(plan(title=title), CPH, NOW) is None


@pytest.mark.parametrize("start", ["2026-10-03", "soon", "16:00", "2026-13-45T10:00", 20261003, None, ["2026-10-03T16:00"], ""])
def test_a_start_without_a_date_and_a_time_drops_the_call(start):
    assert quick_events.parse_quick(plan(start=start), CPH, NOW) is None


@pytest.mark.parametrize("minutes", [14, 1441, 0, -5, True, "abc", 1.5, [], {"m": 1}])
def test_an_unreadable_or_out_of_range_duration_drops_the_call(minutes):
    assert quick_events.parse_quick(plan(duration_minutes=minutes), CPH, NOW) is None


def test_a_start_that_carries_its_own_zone_is_converted_not_reinterpreted():
    event = quick_events.parse_quick(plan(start="2026-10-03T14:00:00+02:00"), "Asia/Tokyo", NOW)
    assert event is not None and event.start == datetime(2026, 10, 3, 12, 0, tzinfo=UTC)


# ================================================================== pure: what the user reads


def test_a_button_carries_a_short_checked_payload():
    did = uuid.uuid4()
    for confirm in (True, False):
        data = quick_events.callback_data(did, confirm)
        assert len(data.encode()) == 41 <= 64
        assert quick_events.parse_callback(data) == (did, confirm)
    for bad in ("", "qe:", f"qe:{did}", f"qe:{did}:x", f"qe:{did}:y:z", "qe:not-a-uuid:y", f"ep:{did}:y", f"qe:{str(did).upper()}:y"):
        assert quick_events.parse_callback(bad) is None


def draft_of(title: str = "Стенка", tz: str = CPH) -> Draft:
    start = datetime(2026, 10, 3, 14, 0, tzinfo=UTC)
    return Draft(uuid.uuid4(), title, start, start + timedelta(hours=2), tz)


def test_the_card_is_a_question_in_the_users_zone_with_two_buttons():
    draft = draft_of()
    assert quick_events.card_text(draft, CPH) == texts.QUICK_CARD.format(title="Стенка", when=texts.when_text(draft.start, draft.end, CPH))
    assert "сохран" not in texts.QUICK_CARD.lower() and texts.QUICK_CARD.rstrip().endswith("?")  # a card asks; it never reports
    [row] = quick_events.card_markup(draft.id).inline_keyboard
    assert [(b.text, b.callback_data) for b in row] == [
        (texts.QUICK_YES, quick_events.callback_data(draft.id, True)),
        (texts.QUICK_NO, quick_events.callback_data(draft.id, False)),
    ]


def test_the_final_text_is_the_state_the_database_reported_in_the_zone_the_draft_was_made_in():
    start, end = datetime(2026, 10, 10, 17, 0, tzinfo=UTC), datetime(2026, 10, 10, 19, 0, tzinfo=UTC)
    for outcome, template in [
        ("confirmed", texts.QUICK_CONFIRMED), ("already_confirmed", texts.QUICK_CONFIRMED),
        ("dismissed", texts.QUICK_DISMISSED), ("already_dismissed", texts.QUICK_DISMISSED), ("expired", texts.QUICK_EXPIRED),
    ]:  # fmt: skip
        got = quick_events.final_text(Decision(outcome, "Стенка", start, end, "Asia/Tokyo"))
        assert got == template.format(title="Стенка", when=texts.when_text(start, end, "Asia/Tokyo"))
    assert quick_events.final_text(Decision("gone")) == texts.QUICK_GONE
    assert quick_events.final_text(Decision("confirmed")) == texts.QUICK_GONE  # nothing to show: never a half-filled card
    assert set(texts.QUICK_TOAST) == {"confirmed", "dismissed", "already_confirmed", "already_dismissed", "expired", "gone"}


def test_the_note_for_the_model_says_the_event_is_not_saved():
    note = quick_events.render_note([draft_of("Стенка"), draft_of("Йога")], CPH)
    assert "NOT saved" in note and "do not mention the buttons" in note
    assert "- Стенка, " in note and "- Йога, " in note


def test_no_quick_event_text_claims_the_event_is_in_the_calendar_before_the_button():
    assert texts.QUICK_CARD.startswith("Занести")  # an offer
    assert "календар" not in texts.QUICK_NO.lower() and "занёс" not in texts.QUICK_YES.lower()  # buttons are verbs, not claims


def test_quick_events_are_on_by_default_for_everyone():
    s = Settings(_env_file=None)
    assert s.quick_events_enabled and s.quick_events_max_calls == 3 and s.quick_events_max_pending == 10 and s.quick_events_ttl_hours == 24


# ================================================================== helpers for the database tests


def future(hours: float = 3) -> tuple[str, datetime]:
    """The wall-clock text the model would pass for a start ``hours`` ahead, and the instant the code must derive (Copenhagen)."""
    instant = (datetime.now(UTC) + timedelta(hours=hours)).replace(second=0, microsecond=0)
    return instant.astimezone(ZoneInfo(CPH)).strftime("%Y-%m-%dT%H:%M"), instant


async def drafts_of(env) -> list[tuple[Any, ...]]:
    return await owner_exec(
        env.owner_engine,
        "SELECT status, title, starts_at, ends_at, timezone, persona_id, event_id, id FROM civ.event_drafts WHERE user_id = :u "
        "ORDER BY created_at, id",
        u=env.user_id,
    )


async def events_of(env, user_id: uuid.UUID | None = None) -> list[tuple[Any, ...]]:
    return await owner_exec(
        env.owner_engine,
        "SELECT title, starts_at, ends_at, timezone, visibility::text, persona_id, created_via, id FROM civ.events "
        "WHERE user_id = :u AND created_via = 'bot_confirmed' ORDER BY created_at, id",
        u=user_id or env.user_id,
    )


def quick_of(env, **settings_kw: Any) -> QuickEvents:
    """The engine of this stack, or one with other limits (same database, same fake Telegram)."""
    if not settings_kw:
        return env.orchestrator.quick
    return QuickEvents(env.db, env.bot, Settings(_env_file=None, **settings_kw))


def cards(env, chat_id: int | None = None, thread_id: int | None = None) -> list[Any]:
    """The messages the bot sent with quick-event buttons."""
    return [
        m for m in env.session.sent()
        if m.reply_markup is not None and m.chat_id == (chat_id or env.chat_id) and m.message_thread_id == thread_id
        and m.reply_markup.inline_keyboard[0][0].callback_data.startswith("qe:")
    ]  # fmt: skip


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


async def press(env, data: str, *, tg: int | None = None, chat_id: int | None = None, thread_id: int | None = None) -> None:
    await env.dispatcher.feed_update(
        env.bot,
        make_callback_update(data=data, user_id=tg or env.telegram_user_id, chat_id=chat_id or env.chat_id, message_id=555, thread_id=thread_id),
    )


def model_plans(env, *calls: ToolCall, text: str = "Записал в календарь!", then: str = "Хорошей тренировки!") -> None:
    """The fake model calls the function(s) - and, as the real one might, ALSO claims the event is already saved."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


# ================================================================== drafts (the database)


async def test_a_valid_call_becomes_a_pending_draft_in_the_users_zone(quick_env):
    env = quick_env
    start_text, start = future()
    [draft] = await quick_of(env).draft(env.user_id, CPH, None, [create_call("  Стенка ", start_text, duration_minutes=120)])
    assert (draft.title, draft.start, draft.end, draft.timezone) == ("Стенка", start, start + timedelta(hours=2), CPH)
    [(status, title, starts_at, ends_at, tz, persona, event_id, draft_id)] = await drafts_of(env)
    assert (status, title, starts_at, ends_at, tz, persona, event_id) == ("pending", "Стенка", start, start + timedelta(hours=2), CPH, None, None)
    assert draft_id == draft.id
    assert await events_of(env) == []  # a draft is not a calendar entry


async def test_the_same_plan_twice_makes_one_draft_and_one_card_even_across_messages(quick_env):
    env = quick_env
    start_text, _ = future()
    twice = [create_call(start=start_text), create_call(start=start_text)]
    assert len(await quick_of(env).draft(env.user_id, CPH, None, twice)) == 1
    assert await quick_of(env).draft(env.user_id, CPH, None, [create_call(start=start_text)]) == []  # its card is already in the chat
    assert len(await drafts_of(env)) == 1
    other_start, _ = future(5)
    assert len(await quick_of(env).draft(env.user_id, CPH, None, [create_call(start=other_start)])) == 1  # another time: another plan


async def test_calls_that_are_not_ours_or_not_usable_make_nothing(quick_env):
    env = quick_env
    bad = [ToolCall("ask_contact", plan()), create_call(start="soon"), create_call(title=""), create_call(duration_minutes=3)]
    assert await quick_of(env).draft(env.user_id, CPH, None, bad) == [] and await drafts_of(env) == []


async def test_a_chatty_model_is_capped_per_message_and_per_user(quick_env):
    env = quick_env
    many = [create_call(f"План {i}", future(3 + i)[0]) for i in range(5)]
    engine = quick_of(env, quick_events_max_calls=2, quick_events_max_pending=3)
    assert len(await engine.draft(env.user_id, CPH, None, many)) == 2  # QUICK_EVENTS_MAX_CALLS
    assert len(await engine.draft(env.user_id, CPH, None, many[2:])) == 1  # only one slot of QUICK_EVENTS_MAX_PENDING is left
    assert len(await drafts_of(env)) == 3
    first = (await drafts_of(env))[0][7]
    assert (await engine.decide(env.user_id, first, False)).outcome == "dismissed"  # an answered card frees its slot
    assert len(await engine.draft(env.user_id, CPH, None, many[3:])) == 1


async def test_an_open_draft_whose_time_ran_out_does_not_block_the_same_plan(quick_env):
    env = quick_env
    start_text, _ = future()
    [old] = await quick_of(env).draft(env.user_id, CPH, None, [create_call(start=start_text)])
    await owner_exec(
        env.owner_engine,
        "UPDATE civ.event_drafts SET created_at = now() - interval '2 days', expires_at = now() - interval '1 day' WHERE id = :d",
        d=old.id,
    )
    [new] = await quick_of(env).draft(env.user_id, CPH, None, [create_call(start=start_text)])
    assert new.id != old.id
    assert sorted(row[0] for row in await drafts_of(env)) == ["expired", "pending"]


async def test_a_topic_is_remembered_on_the_draft_and_a_database_failure_makes_no_draft(quick_env, monkeypatch):
    env = quick_env
    persona = await new_persona(env.owner_engine, env.user_id, "sport", thread_id=612)
    [draft] = await quick_of(env).draft(env.user_id, CPH, persona, [create_call(start=future()[0])])
    assert (await drafts_of(env))[0][5] == persona and draft.id

    async def boom(self, *args: Any, **kwargs: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(QuickEvents, "_insert", boom)
    assert await quick_of(env).draft(env.user_id, CPH, None, [create_call("Другое", future(7)[0])]) == []  # never raises


# ================================================================== the button (the database)


async def one_draft(env, title: str = "Стенка", persona: uuid.UUID | None = None) -> Draft:
    [draft] = await quick_of(env).draft(env.user_id, CPH, persona, [create_call(title, future()[0], duration_minutes=120)])
    return draft


async def test_confirming_creates_exactly_one_event_that_is_busy_time_from_the_bot(quick_env):
    env = quick_env
    persona = await new_persona(env.owner_engine, env.user_id, "sport", thread_id=612)
    draft = await one_draft(env, persona=persona)
    decision = await quick_of(env).decide(env.user_id, draft.id, True)
    assert decision == Decision("confirmed", "Стенка", draft.start, draft.end, CPH)
    [(title, starts_at, ends_at, tz, visibility, event_persona, via, event_id)] = await events_of(env)
    assert (title, starts_at, ends_at, tz, visibility, event_persona, via) == (
        "Стенка", draft.start, draft.end, CPH, "busy_only", persona, "bot_confirmed",
    )  # fmt: skip
    [(status, *_rest, linked, _id)] = await drafts_of(env)
    assert status == "confirmed" and linked == event_id


async def test_a_second_press_shows_what_was_decided_and_never_creates_a_second_event(quick_env):
    env = quick_env
    engine = quick_of(env)
    draft = await one_draft(env)
    assert (await engine.decide(env.user_id, draft.id, True)).outcome == "confirmed"
    assert (await engine.decide(env.user_id, draft.id, True)).outcome == "already_confirmed"
    assert (await engine.decide(env.user_id, draft.id, False)).outcome == "already_confirmed"  # "No" after "Yes" changes nothing
    assert len(await events_of(env)) == 1


async def test_dismissing_creates_nothing_and_a_later_yes_cannot_revive_it(quick_env):
    env = quick_env
    engine = quick_of(env)
    draft = await one_draft(env)
    assert (await engine.decide(env.user_id, draft.id, False)).outcome == "dismissed"
    assert (await engine.decide(env.user_id, draft.id, True)).outcome == "already_dismissed"
    assert (await engine.decide(env.user_id, draft.id, False)).outcome == "already_dismissed"
    assert await events_of(env) == [] and (await drafts_of(env))[0][0] == "dismissed"


async def test_a_card_that_went_stale_expires_instead_of_creating_an_event(quick_env):
    env = quick_env
    engine = quick_of(env)
    draft = await one_draft(env)
    await owner_exec(
        env.owner_engine,
        "UPDATE civ.event_drafts SET created_at = now() - interval '2 days', expires_at = now() - interval '1 day' WHERE id = :d",
        d=draft.id,
    )
    assert (await engine.decide(env.user_id, draft.id, True)).outcome == "expired"
    assert (await engine.decide(env.user_id, draft.id, True)).outcome == "expired"  # and stays so
    assert await events_of(env) == [] and (await drafts_of(env))[0][0] == "expired"


async def test_eight_simultaneous_yes_presses_make_one_event(quick_env):
    env = quick_env
    engine = quick_of(env)
    draft = await one_draft(env)
    outcomes = await asyncio.gather(*(engine.decide(env.user_id, draft.id, True) for _ in range(8)))
    assert sorted(o.outcome for o in outcomes) == ["already_confirmed"] * 7 + ["confirmed"]
    assert len(await events_of(env)) == 1


async def test_yes_and_no_at_the_same_moment_end_in_one_consistent_state(quick_env):
    env = quick_env
    engine = quick_of(env)
    draft = await one_draft(env)
    await asyncio.gather(*(engine.decide(env.user_id, draft.id, i % 2 == 0) for i in range(6)))
    [(status, *_rest)] = await drafts_of(env)
    assert status in ("confirmed", "dismissed") and len(await events_of(env)) == (1 if status == "confirmed" else 0)


async def test_somebody_elses_draft_is_gone_not_forbidden_and_stays_pending(quick_env):
    env = quick_env
    draft = await one_draft(env)
    stranger = await befriend(env, granted=None)  # even a connected person
    assert (await quick_of(env).decide(stranger, draft.id, True)) == Decision("gone")  # RLS: the row does not exist for them
    assert await events_of(env) == [] and await events_of(env, stranger) == [] and (await drafts_of(env))[0][0] == "pending"
    assert await quick_of(env).decide(env.user_id, uuid.uuid4(), True) == Decision("gone")


# ================================================================== through the real Dispatcher


async def test_the_model_replies_and_then_code_sends_the_card_and_nothing_is_saved_until_the_button(quick_env):
    env = quick_env
    start_text, start = future()
    model_plans(env, create_call("Стенка", start_text, duration_minutes=120))
    await env.feed("Я сейчас иду на 2 часа смотреть стенку")

    [offered] = env.chat.tool_calls
    assert [t.name for t in offered["tools"]] == ["create_event"] and quick_events.INSTRUCTIONS in offered["system"]
    [phrased] = env.chat.calls  # the model is asked AGAIN, without functions, to phrase the reply...
    assert "Calendar suggestion" in phrased["system"] and "NOT saved" in phrased["system"] and "Стенка" in phrased["system"]
    [card] = cards(env)
    when = texts.when_text(start, start + timedelta(hours=2), CPH)
    assert says(env) == ["Хорошей тренировки!", texts.QUICK_CARD.format(title="Стенка", when=when)]  # ...its reply, THEN the card
    assert all("Записал в календарь" not in t for t in says(env))  # the model's own claim, written next to the call, is never sent
    draft_id = (await drafts_of(env))[0][7]
    assert [b.callback_data for b in card.reply_markup.inline_keyboard[0]] == [
        quick_events.callback_data(draft_id, True), quick_events.callback_data(draft_id, False)
    ]
    assert [m["content"] for m in await env.messages() if m["role"] == "assistant"] == ["Хорошей тренировки!"]  # the card is not "memory"
    assert await events_of(env) == []  # not yet

    await press(env, card.reply_markup.inline_keyboard[0][0].callback_data)
    [(title, starts_at, ends_at, tz, *_rest)] = await events_of(env)
    assert (title, starts_at, ends_at, tz) == ("Стенка", start, start + timedelta(hours=2), CPH)
    assert env.session.edits()[-1].text == texts.QUICK_CONFIRMED.format(title="Стенка", when=when)
    assert env.session.callback_answers()[-1].text == texts.QUICK_TOAST["confirmed"]


async def test_pressing_the_card_twice_or_both_buttons_leaves_one_event(quick_env):
    env = quick_env
    model_plans(env, create_call(start=future()[0]))
    await env.feed("Иду смотреть стенку")
    [card] = cards(env)
    yes, no = (b.callback_data for b in card.reply_markup.inline_keyboard[0])
    await press(env, yes)
    await press(env, yes)
    await press(env, no)
    assert len(await events_of(env)) == 1
    assert [a.text for a in env.session.callback_answers()] == [texts.QUICK_TOAST[k] for k in ("confirmed", "already_confirmed", "already_confirmed")]


async def test_no_dismisses_the_card_and_creates_nothing(quick_env):
    env = quick_env
    model_plans(env, create_call(start=future()[0]))
    await env.feed("Иду смотреть стенку")
    [card] = cards(env)
    await press(env, card.reply_markup.inline_keyboard[0][1].callback_data)
    assert await events_of(env) == [] and (await drafts_of(env))[0][0] == "dismissed"
    assert env.session.edits()[-1].text.startswith("Не заношу") and env.session.callback_answers()[-1].text == texts.QUICK_TOAST["dismissed"]


async def test_the_word_now_starts_the_plan_at_the_bots_clock(quick_env):
    env = quick_env
    before = datetime.now(UTC)
    model_plans(env, create_call(start="now", duration_minutes=120))
    await env.feed("Я сейчас иду на 2 часа смотреть стенку")
    [(_status, _title, starts_at, ends_at, *_rest)] = await drafts_of(env)
    assert before.replace(second=0, microsecond=0) <= starts_at <= datetime.now(UTC) and ends_at - starts_at == timedelta(hours=2)


async def test_in_a_topic_the_card_goes_to_that_topic_and_the_draft_remembers_it(quick_env):
    env = quick_env
    persona = await new_persona(env.owner_engine, env.user_id, "sport", thread_id=612)
    model_plans(env, create_call("Тренировка", future()[0]))
    await env.feed("Иду на тренировку", thread_id=612)
    [card] = cards(env, thread_id=612)
    assert cards(env) == [] and (await drafts_of(env))[0][5] == persona
    await press(env, card.reply_markup.inline_keyboard[0][0].callback_data, thread_id=612)
    assert (await events_of(env))[0][5] == persona


async def test_a_call_the_code_cannot_use_never_becomes_a_claim_or_a_card(quick_env):
    env = quick_env
    model_plans(env, create_call(start="soon"), text="Занёс в календарь!", then="Понял, расскажите когда.")
    await env.feed("Иду смотреть стенку")
    assert says(env) == ["Понял, расскажите когда."] and cards(env) == [] and await drafts_of(env) == []  # the model's words are not trusted


async def test_a_database_failure_costs_the_card_not_the_reply(quick_env, monkeypatch):
    env = quick_env

    async def boom(self, *args: Any, **kwargs: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(QuickEvents, "_insert", boom)
    model_plans(env, create_call(start=future()[0]), text="Записал!", then="Хорошей тренировки!")
    await env.feed("Иду смотреть стенку")
    assert says(env) == ["Хорошей тренировки!"] and texts.FAILURE not in says(env) and cards(env) == []


async def test_a_plain_message_without_a_plan_is_answered_as_usual_and_has_no_card(quick_env):
    env = quick_env
    env.chat.responder = lambda system, turns: "Привет!"
    await env.feed("Привет, как дела?")
    assert says(env) == ["Привет!"] and cards(env) == [] and await drafts_of(env) == []


async def test_a_user_with_contacts_is_offered_all_three_functions_and_a_plan_still_works(quick_env):
    env = quick_env
    await befriend(env, granted=FREE)
    model_plans(env, create_call(start=future()[0]))
    await env.feed("Иду смотреть стенку")
    [offered] = env.chat.tool_calls
    assert [t.name for t in offered["tools"]] == ["create_event", "ask_contact", "propose_event"]
    assert len(cards(env)) == 1 and says(env)[0] == "Хорошей тренировки!"


async def test_a_plan_and_a_question_about_a_friend_in_one_message(quick_env):
    env = quick_env
    await befriend(env, granted=FREE)
    today = datetime.now(ZoneInfo("UTC")).date()
    ask = ToolCall("ask_contact", {"contact": "c1", "info": FREE, "date_from": today.isoformat(), "date_to": (today + timedelta(days=1)).isoformat()})
    model_plans(env, create_call(start=future()[0]), ask, text="", then="Саша свободен. Удачной тренировки!")
    await env.feed("Иду смотреть стенку, а Саша свободен?")
    assert len(cards(env)) == 1 and says(env)[0] == "Саша свободен. Удачной тренировки!"
    [phrased] = env.chat.calls
    assert "Calendar suggestion" in phrased["system"] and "Contact data" in phrased["system"]  # one phrasing turn for both


async def test_with_the_feature_off_the_model_is_never_offered_the_function(bot_env):
    env = bot_env  # QUICK_EVENTS_ENABLED=false in this stack
    await env.feed("Иду смотреть стенку")
    assert env.chat.tool_calls == [] and len(env.chat.calls) == 1


async def test_somebody_elses_card_is_gone_for_them_and_still_waiting_for_its_owner(quick_env):
    env = quick_env
    model_plans(env, create_call(start=future()[0]))
    await env.feed("Иду смотреть стенку")
    [card] = cards(env)
    stranger = await befriend(env, granted=None)
    [(tg, chat)] = [(r[0], r[1]) for r in await owner_exec(env.owner_engine, "SELECT telegram_user_id, forum_chat_id FROM civ.users WHERE id = :u", u=stranger)]
    await press(env, card.reply_markup.inline_keyboard[0][0].callback_data, tg=tg, chat_id=chat)
    assert env.session.callback_answers()[-1].text == texts.QUICK_GONE and env.session.edits()[-1].text == texts.QUICK_GONE
    assert await events_of(env) == [] and await events_of(env, stranger) == [] and (await drafts_of(env))[0][0] == "pending"


async def test_a_garbage_payload_is_an_unknown_button_and_changes_nothing(quick_env):
    env = quick_env
    await press(env, "qe:garbage")
    [answer] = env.session.callback_answers()
    assert answer.text == texts.PROPOSAL_UNKNOWN_BUTTON and env.session.edits() == []


async def test_a_failing_database_leaves_the_buttons_and_says_so(quick_env, monkeypatch):
    env = quick_env
    model_plans(env, create_call(start=future()[0]))
    await env.feed("Иду смотреть стенку")
    [card] = cards(env)

    async def boom(self, *args: Any, **kwargs: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(QuickEvents, "decide", boom)
    await press(env, card.reply_markup.inline_keyboard[0][0].callback_data)
    [answer] = env.session.callback_answers()
    assert answer.text == texts.FAILURE and answer.show_alert and env.session.edits() == []  # the card keeps its buttons
    assert (await drafts_of(env))[0][0] == "pending" and await events_of(env) == []


async def test_an_editing_failure_after_the_decision_does_not_undo_it(quick_env):
    from aiogram.exceptions import TelegramBadRequest

    env = quick_env
    model_plans(env, create_call(start=future()[0]))
    await env.feed("Иду смотреть стенку")
    [card] = cards(env)
    env.session.fail_on("editMessageText", TelegramBadRequest, message="Bad Request: message is not modified")
    await press(env, card.reply_markup.inline_keyboard[0][0].callback_data)
    assert len(await events_of(env)) == 1  # the decision is made; only the cosmetic edit failed
    assert env.session.callback_answers()[-1].text == texts.QUICK_TOAST["confirmed"]


# ================================================================== housekeeping (Celery Beat)


def runtime_of(env) -> Runtime:
    return Runtime(db=env.db, settings=env.settings, llm=env.router, embedder=None, chat=env.chat)


async def test_purge_events_is_a_beat_task_and_expires_then_deletes_drafts(quick_env):
    env = quick_env
    assert "ai_civ.purge_events" in set(app.tasks)
    scheduled = {e["task"]: e["schedule"] for e in app.conf.beat_schedule.values()}
    assert "ai_civ.purge_events" in scheduled  # a daily crontab, like purge_social

    async def insert(title: str, status: str, created: timedelta, expires_in: timedelta, decided: timedelta | None) -> uuid.UUID:
        """A draft whose clock is wound back: it was made ``created`` ago, runs out ``expires_in`` from now, was decided ``decided`` ago."""
        rows = await owner_exec(
            env.owner_engine,
            "INSERT INTO civ.event_drafts (user_id, title, starts_at, ends_at, timezone, status, created_at, expires_at, decided_at) "
            "VALUES (:u, :t, now() + interval '1 day', now() + interval '1 day 1 hour', 'UTC', :s, now() - CAST(:created AS interval), "
            "now() + CAST(:expires AS interval), now() - CAST(:decided AS interval)) RETURNING id",
            u=env.user_id, t=title, s=status, created=created, expires=expires_in, decided=decided,
        )  # fmt: skip
        return rows[0][0]

    day = timedelta(days=1)
    stale_open = await insert("stale open", "pending", 3 * day, -2 * day, None)  # ran out of time, never answered -> expired
    fresh_open = await insert("fresh open", "pending", timedelta(minutes=1), timedelta(hours=1), None)  # still waiting -> untouched
    old_decided = await insert("old decided", "confirmed", 20 * day, -19 * day, 10 * day)  # decided long ago (TTL 7 days) -> deleted
    new_decided = await insert("new decided", "dismissed", 2 * day, -day, day)  # decided yesterday -> kept for a while

    async def states() -> dict[uuid.UUID, str]:
        rows = await owner_exec(env.owner_engine, "SELECT id, status FROM civ.event_drafts WHERE user_id = :u", u=env.user_id)
        return {r[0]: r[1] for r in rows}

    stats = await tasks.purge_events_async(runtime_of(env))
    assert stats.drafts_expired >= 1 and stats.drafts_purged >= 1  # (other tests' leftovers may add to the counts)
    state = await states()
    assert state[stale_open] == "expired" and state[fresh_open] == "pending" and state[new_decided] == "dismissed"
    assert old_decided not in state

    again = await tasks.purge_events_async(runtime_of(env))  # idempotent: a re-run only finds less to do
    assert await states() == state and again.drafts_expired == 0 and again.drafts_purged == 0

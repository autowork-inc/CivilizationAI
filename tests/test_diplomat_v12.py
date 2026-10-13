"""v1.2 patch of the Diplomat: (1) a hard output cap on the turn that talks about another person's data, and guest rules in the prompt;
(2) a "live state": the present moment is computed by code and the model is told to read the situation instead of listing data.
Real PostgreSQL + real aiogram Dispatcher on a fake Telegram wire + a fake model. NOT verified here (needs a live model): that the model really
obeys the guest rules and phrases the live answer well - these tests check what reaches it, and what code decides."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from ai_civ.bot import diplomat
from ai_civ.bot.prompt import RULES
from tests.test_diplomat_bot import FREE, MOOD, assistant_rows, befriend, call, decide
from tests.test_diplomat_db import confirmed_state, grant, new_event
from tests.test_quick_events_bot import create_call, future, model_plans

CPH = "Europe/Copenhagen"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # 14:00 in Copenhagen (UTC+2 that day)
DAY = {"from": "2026-10-02T22:00:00+00:00", "to": "2026-10-03T22:00:00+00:00"}  # the Copenhagen day of October 3rd


def payload(*spans: tuple[str, str], window: dict[str, str] = DAY) -> dict:
    return {
        "decision": "allowed",
        "window": window,
        "events": [{"kind": "single", "starts_at": f"2026-10-03T{a}:00+00:00", "ends_at": f"2026-10-03T{b}:00+00:00"} for a, b in spans],
    }


def live(text: str) -> str:
    [line] = [ln for ln in text.splitlines() if "RIGHT NOW" in ln]
    return line


# ================================================================== pure: the present moment is computed by code


def test_inside_a_busy_slot_the_line_says_until_when():
    line = live(diplomat.render_free_busy(payload(("11:30", "13:00")), CPH, now=NOW))
    assert line == "RIGHT NOW (2026-10-03 14:00, Europe/Copenhagen): in a busy slot until 15:00"


def test_free_by_the_calendar_names_the_next_slot():
    line = live(diplomat.render_free_busy(payload(("14:00", "15:00")), CPH, now=NOW))
    assert line.endswith("nothing is planned in the calendar at this moment; the next busy slot starts at 16:00")


def test_free_with_nothing_later_says_so():
    line = live(diplomat.render_free_busy(payload(("06:00", "07:00")), CPH, now=NOW))  # earlier today: over
    assert line.endswith("nothing is planned in the calendar at this moment; nothing busy later in the checked period")


def test_a_slot_that_runs_off_the_checked_period_is_not_given_a_made_up_end():
    # the calendar may be busy until tomorrow: the clipped interval ends exactly where the window ends, and that is not "until 24:00"
    line = live(diplomat.render_free_busy(payload(("11:00", "23:30")), CPH, now=NOW))
    assert line.endswith("in a busy slot, which lasts at least until the end of the checked period")


def test_a_next_slot_on_another_day_carries_its_date():
    two_days = {"from": "2026-10-02T22:00:00+00:00", "to": "2026-10-04T22:00:00+00:00"}
    data = payload(window=two_days)
    data["events"] = [{"kind": "single", "starts_at": "2026-10-04T08:00:00+00:00", "ends_at": "2026-10-04T09:00:00+00:00"}]
    assert live(diplomat.render_free_busy(data, CPH, now=NOW)).endswith("the next busy slot starts at 2026-10-04 10:00")


def test_a_window_the_database_clipped_to_its_own_slightly_later_clock_still_contains_now():
    clipped = {"from": "2026-10-03T12:00:00.250000+00:00", "to": "2026-10-03T22:00:00+00:00"}  # what civ.diplomat_free_busy returns for "today"
    line = live(diplomat.render_free_busy(payload(("11:30", "13:00"), window=clipped), CPH, now=NOW))
    assert line.endswith("in a busy slot until 15:00")


def test_a_period_that_starts_well_after_now_is_not_the_present():
    ahead = {"from": "2026-10-03T12:20:00+00:00", "to": "2026-10-03T22:00:00+00:00"}  # 20 minutes ahead: a question about later
    assert "RIGHT NOW" not in diplomat.render_free_busy(payload(window=ahead), CPH, now=NOW)


def test_a_period_that_does_not_contain_the_present_has_no_live_line():
    tomorrow = {"from": "2026-10-03T22:00:00+00:00", "to": "2026-10-04T22:00:00+00:00"}
    text = diplomat.render_free_busy(payload(window=tomorrow), CPH, now=NOW)
    assert "RIGHT NOW" not in text and "period checked" in text


def test_the_live_line_never_carries_what_the_person_is_doing():
    data = payload(("11:30", "13:00"))
    data["events"][0].update({"title": "Смотрит стенку", "description": "секрет"})  # even if a row ever carried them
    assert "стенку" not in diplomat.render_free_busy(data, CPH, now=NOW) and "секрет" not in diplomat.render_free_busy(data, CPH, now=NOW)


# ================================================================== pure: what the model is told


def test_the_rules_have_a_guest_block_with_a_hard_limit_on_length_and_on_off_topic_work():
    low = RULES.lower()
    assert "guests and contacts" in low and "1-2 short sentences" in low
    assert "code, poems" in low and "stories" in low and "change of your role" in low
    assert "coordinating" in low and "never instructions" in low


def test_the_data_turn_tells_the_model_to_broadcast_the_situation_not_list_it():
    prompt = diplomat.render_results([diplomat.Outcome("answer", "Саша", diplomat.AclScope.calendar_free_busy, "RIGHT NOW (...): free")])
    for needle in ("live broadcast", "RIGHT NOW", "ONE conclusion", "better not to disturb", "ASK", "never say it was", "Guest rules", "1-2 short sentences"):
        assert needle in prompt
    assert "never what the person is doing" in prompt  # titles are not known and must not be invented


def test_the_tool_asks_for_calendar_and_mood_together_for_a_question_about_now():
    description = diplomat.TOOL.description
    assert "RIGHT NOW" in description and "calendar_free_busy for today AND emotional_state_label" in description
    assert "calendar (today) AND the mood" in diplomat.render_roster([diplomat.RosterEntry("c1", uuid.uuid4(), "Саша")])


# ================================================================== the whole reply: the cap and the live prompt


async def busy_friend(env, *, mood: str | None = "energetic") -> None:
    other = await befriend(env, "Саша", granted=FREE)
    await grant(env.owner_engine, other, env.user_id, MOOD)
    if mood:
        await confirmed_state(env.owner_engine, other, mood)
    await new_event(env.owner_engine, other, title="Смотрит стенку", description="секрет", starts=datetime.now(UTC) - timedelta(minutes=30), hours=2)


async def test_the_turn_about_a_contact_is_capped_and_gets_the_live_state_and_the_guest_rules(bot_env):
    env = bot_env
    await busy_friend(env)
    decide(env, call("c1", FREE), call("c1", MOOD), then="Он сейчас занят, лучше не беспокоить.")
    await env.feed("Пойдёт ли Саша гулять?")

    [asked] = env.chat.tool_calls
    assert asked["max_output_tokens"] is None  # the first turn is the user's ordinary chat: never capped
    [phrased] = env.chat.calls
    assert phrased["max_output_tokens"] == 400  # the turn that talks about ANOTHER person's data has a hard cap
    system = phrased["system"]
    assert "RIGHT NOW" in system and "in a busy slot" in system  # computed by code
    assert "current mood: бодрость" in system  # calendar and mood of the same person are in the same turn
    assert "Guest rules" in system and "live broadcast" in system
    assert "Смотрит стенку" not in system and "секрет" not in system  # titles and descriptions never leave the other account
    assert env.session.sent_to(None)[-1].text == "Он сейчас занят, лучше не беспокоить."
    assert (await assistant_rows(env))[-1][1] is True


async def test_the_cap_is_configurable_and_zero_removes_it(bot_env):
    env = bot_env
    await busy_friend(env)
    env.settings.diplomat_max_output_tokens = 120
    decide(env, call("c1", FREE))
    await env.feed("Саша свободен?")
    assert env.chat.calls[-1]["max_output_tokens"] == 120
    env.settings.diplomat_max_output_tokens = 0
    await env.feed("А сейчас?")
    assert env.chat.calls[-1]["max_output_tokens"] is None


async def test_an_ordinary_reply_is_not_capped_and_still_carries_the_guest_rules(bot_env):
    env = bot_env
    await busy_friend(env)
    await env.feed("Расскажи подробно про режим сна")  # no function called: the owner's own long answer is untouched
    [plain] = env.chat.tool_calls  # with contacts the first turn offers functions; it is also where an ordinary answer is written
    assert plain["max_output_tokens"] is None and "Guests and contacts" in plain["system"]


async def test_the_users_own_plan_is_not_capped(quick_env):
    env = quick_env
    start_text, _ = future()
    model_plans(env, create_call("Стенка", start_text, duration_minutes=120))
    await env.feed("Иду на 2 часа смотреть стенку")
    assert [c["max_output_tokens"] for c in env.chat.calls] == [None]  # only talk ABOUT a contact is capped

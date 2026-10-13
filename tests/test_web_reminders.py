"""Screen "Напоминания": quiet hours and the time of each proactive trigger. Real PostgreSQL + RLS; the real Beat dispatcher
(``bot.schedule.claim_due``) is used to prove that what the screen saves is what actually fires."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, date, datetime, timedelta

from ai_civ.bot import schedule
from ai_civ.bot.proactive import TRIGGERS
from tests.web_helpers import alice, bob, client  # noqa: F401

GRACE = timedelta(minutes=30)


def meal(local_time="13:00", enabled=True):
    return {"local_time": local_time, "enabled": enabled}


async def rows(actor, where="TRUE"):
    return await actor.sql(
        f"SELECT trigger_key, local_time::text, enabled, last_fired_on FROM civ.proactive_schedule WHERE user_id = :u AND {where} ORDER BY 1"
    )


async def quiet(actor):
    return (await actor.sql("SELECT quiet_hours_start::text, quiet_hours_end::text FROM civ.users WHERE id = :u"))[0]


# ---------------------------------------------------------------- reading


async def test_a_fresh_user_sees_every_known_trigger_switched_off_with_its_default_time(alice):
    body = (await alice.get("/api/reminders")).json()

    assert body["timezone"] == "Europe/Copenhagen" and body["grace_minutes"] == 30
    assert body["quiet_hours"] == {"start": "22:00", "end": "08:00"}
    assert [r["trigger_key"] for r in body["reminders"]] == [key for key, t in TRIGGERS.items() if t.scheduled]  # check_in has no time of its own (it is not on this screen)
    by_key = {r["trigger_key"]: r for r in body["reminders"]}
    assert by_key["meal_check"] == {
        "trigger_key": "meal_check", "title": "Напоминание о еде", "description": "Спрошу, поели ли вы и что именно.",
        "local_time": "13:00", "enabled": False, "configured": False, "last_fired_on": None,
    }  # fmt: skip
    assert (by_key["work_start"]["local_time"], by_key["work_start"]["enabled"]) == ("09:00", False)
    assert await rows(alice) == []  # reading writes nothing


async def test_rows_of_retired_triggers_stay_invisible_and_untouched(alice):
    await alice.sql("INSERT INTO civ.proactive_schedule (user_id, trigger_key, local_time) VALUES (:u, 'retired_thing', '10:00')")
    keys = [r["trigger_key"] for r in (await alice.get("/api/reminders")).json()["reminders"]]
    assert "retired_thing" not in keys
    assert (await alice.put("/api/reminders/retired_thing", meal())).status_code == 404
    assert [r[0] for r in await rows(alice)] == ["retired_thing"]


# ---------------------------------------------------------------- setting a reminder


async def test_saving_a_time_creates_then_updates_one_row_and_returns_the_new_state(alice):
    first = await alice.put("/api/reminders/meal_check", meal("12:30"))
    assert first.status_code == 200
    saved = next(r for r in first.json()["reminders"] if r["trigger_key"] == "meal_check")
    assert (saved["local_time"], saved["enabled"], saved["configured"]) == ("12:30", True, True)
    other = next(r for r in first.json()["reminders"] if r["trigger_key"] == "work_start")
    assert other["configured"] is False  # the other trigger is untouched

    await alice.put("/api/reminders/meal_check", meal("14:15", enabled=False))

    assert [(r[0], r[1], r[2]) for r in await rows(alice)] == [("meal_check", "14:15:00", False)]  # still ONE row


async def test_both_time_notations_are_accepted_and_garbage_is_a_422_in_russian(alice):
    for ok in ("09:30", "09:30:00", " 09:30 ", "00:00", "23:59"):
        assert (await alice.put("/api/reminders/work_start", meal(ok, enabled=False))).status_code == 200, ok
    for bad in ("9:30", "25:00", "12:60", "", "noon", "12:00+02:00", "12:00:30", "13.30"):
        response = await alice.put("/api/reminders/work_start", meal(bad, enabled=False))
        assert response.status_code == 422, bad
        assert "ЧЧ:ММ" in response.json()["error"]["message"]
    assert (await alice.put("/api/reminders/work_start", {"local_time": "10:00"})).status_code == 422  # `enabled` is required
    assert (await alice.put("/api/reminders/work_start", {**meal("10:00"), "last_fired_on": "2020-01-01"})).status_code == 422
    assert (await rows(alice))[0][1] == "23:59:00"  # nothing from the bad calls got in


async def test_unknown_or_malformed_trigger_keys_are_refused(alice):
    assert (await alice.put("/api/reminders/teleport", meal())).status_code == 404
    for key in ("Meal", "1abc", "a-b", "x" * 41):
        assert (await alice.put(f"/api/reminders/{key}", meal())).status_code in (404, 422), key
    assert await rows(alice) == []


async def test_what_the_screen_saves_is_what_the_beat_dispatcher_fires(alice, db):
    """The loop is closed: the time (in the PROFILE time zone) is exactly what Module 3's claim_due acts on."""
    await alice.put("/api/reminders/meal_check", meal("12:30"))
    at = lambda hour, minute: datetime(2026, 1, 15, hour, minute, tzinfo=UTC)  # noqa: E731  Copenhagen is UTC+1 in January

    assert await schedule.claim_due(db, alice.user_id, now=at(11, 20), grace=GRACE) == []  # 12:20 local: not yet
    due = await schedule.claim_due(db, alice.user_id, now=at(11, 35), grace=GRACE)  # 12:35 local
    assert [d.trigger_key for d in due] == ["meal_check"]
    assert await schedule.claim_due(db, alice.user_id, now=at(11, 40), grace=GRACE) == []  # once a day

    await alice.sql("UPDATE civ.proactive_schedule SET last_fired_on = NULL WHERE user_id = :u")
    await alice.put("/api/reminders/meal_check", meal("12:30", enabled=False))
    assert await schedule.claim_due(db, alice.user_id, now=at(11, 35), grace=GRACE) == []  # switched off: silent


async def test_a_new_time_in_the_users_own_zone_follows_a_zone_change(alice, db):
    await alice.put("/api/reminders/meal_check", meal("12:30"))
    await alice.put("/api/profile/timezone", {"timezone": "Asia/Almaty"})  # UTC+5
    assert (await alice.get("/api/reminders")).json()["timezone"] == "Asia/Almaty"
    now = datetime(2026, 1, 15, 7, 35, tzinfo=UTC)  # 12:35 in Almaty
    assert [d.trigger_key for d in await schedule.claim_due(db, alice.user_id, now=now, grace=GRACE)] == ["meal_check"]


async def test_changing_the_time_never_touches_last_fired_on_so_a_nudge_cannot_come_twice_a_day(alice):
    await alice.put("/api/reminders/meal_check", meal("13:00"))
    await alice.sql("UPDATE civ.proactive_schedule SET last_fired_on = '2026-01-15' WHERE user_id = :u")

    response = await alice.put("/api/reminders/meal_check", meal("15:00"))

    assert next(r for r in response.json()["reminders"] if r["trigger_key"] == "meal_check")["last_fired_on"] == "2026-01-15"
    assert (await rows(alice))[0][3] == date(2026, 1, 15)


# ---------------------------------------------------------------- quiet hours and the invariant


async def test_a_reminder_moved_into_the_quiet_hours_moves_them_instead_of_failing(alice):
    """The window is [start, end): 22:00 is in, 08:00 is out. 07:00 sits in the morning part of 22:00-08:00, so the END is pulled back to 06:45."""
    response = await alice.put("/api/reminders/meal_check", meal("07:00"))

    assert response.status_code == 200
    body = response.json()
    assert body["quiet_hours"] == {"start": "22:00", "end": "06:45"}
    assert body["quiet_hours_adjusted"] == {"previous_start": "22:00", "previous_end": "08:00", "start": "22:00", "end": "06:45"}
    assert await quiet(alice) == ("22:00:00", "06:45:00")  # saved in the same transaction as the reminder
    assert [(r[0], r[1], r[2]) for r in await rows(alice)] == [("meal_check", "07:00:00", True)]


async def test_a_reminder_in_the_evening_part_pushes_the_start_so_the_night_is_not_wiped_out(alice):
    body = (await alice.put("/api/reminders/meal_check", meal("23:00"))).json()

    assert body["quiet_hours"] == {"start": "23:15", "end": "08:00"}  # pulling the end back would have left only 22:00-22:45 of quiet
    assert body["quiet_hours_adjusted"]["previous_start"] == "22:00"
    assert await quiet(alice) == ("23:15:00", "08:00:00")


async def test_a_reminder_outside_the_quiet_hours_or_a_disabled_one_changes_nothing(alice):
    for outside in ("08:00", "13:00", "21:59"):  # 08:00 is the first minute after the window
        body = (await alice.put("/api/reminders/meal_check", meal(outside))).json()
        assert body["quiet_hours_adjusted"] is None and body["quiet_hours"] == {"start": "22:00", "end": "08:00"}, outside
    body = (await alice.put("/api/reminders/work_start", meal("23:00", enabled=False))).json()  # off: remembered, harmless
    assert body["quiet_hours_adjusted"] is None and await quiet(alice) == ("22:00:00", "08:00:00")


async def test_an_adjustment_never_puts_another_enabled_reminder_inside_the_window(alice):
    await alice.put("/api/reminders/work_start", meal("08:30"))
    await alice.put("/api/reminders/meal_check", meal("07:00"))  # shrinks to 22:00-06:45

    start, end = await quiet(alice)
    assert (start, end) == ("22:00:00", "06:45:00")
    for _, local_time, enabled, _ in await rows(alice):
        assert not (enabled and (local_time >= start or local_time < end))


async def test_a_window_too_short_to_move_still_refuses_with_the_reason(alice):
    await alice.put("/api/reminders/quiet-hours", {"start": "12:00", "end": "12:20"})

    response = await alice.put("/api/reminders/meal_check", meal("12:10"))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "in_quiet_hours" and "слишком короткие" in response.json()["error"]["message"]
    assert await rows(alice) == [] and await quiet(alice) == ("12:00:00", "12:20:00")


async def test_quiet_hours_can_be_changed_including_across_midnight(alice):
    for start, end in (("23:30", "07:15"), ("01:00", "06:00"), ("20:00", "20:01")):
        response = await alice.put("/api/reminders/quiet-hours", {"start": start, "end": end})
        assert response.status_code == 200 and response.json()["quiet_hours"] == {"start": start, "end": end}
        assert await quiet(alice) == (f"{start}:00", f"{end}:00")


async def test_equal_start_and_end_or_a_bad_time_is_refused_and_changes_nothing(alice):
    for body in ({"start": "22:00", "end": "22:00"}, {"start": "25:00", "end": "08:00"}, {"start": "22:00"}, {"start": "22:00", "end": "08:00", "x": 1}):
        assert (await alice.put("/api/reminders/quiet-hours", body)).status_code == 422, body
    assert await quiet(alice) == ("22:00:00", "08:00:00")


async def test_quiet_hours_that_would_swallow_an_enabled_reminder_are_refused_with_the_reason(alice):
    await alice.put("/api/reminders/meal_check", meal("13:00"))
    await alice.put("/api/reminders/work_start", meal("09:00", enabled=False))

    response = await alice.put("/api/reminders/quiet-hours", {"start": "08:00", "end": "14:00"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "reminder_in_quiet_hours" and error["conflicts"] == ["meal_check"]  # the disabled 09:00 one does not count
    assert "Напоминание о еде" in error["message"] and "13:00" in error["message"] and "Начало работы" not in error["message"]
    assert await quiet(alice) == ("22:00:00", "08:00:00")

    await alice.put("/api/reminders/meal_check", meal("13:00", enabled=False))  # switch it off ...
    assert (await alice.put("/api/reminders/quiet-hours", {"start": "08:00", "end": "14:00"})).status_code == 200  # ... now it fits


async def test_the_dispatcher_honours_the_quiet_hours_that_were_saved(alice, db):
    await alice.put("/api/reminders/meal_check", meal("12:30"))
    await alice.put("/api/reminders/quiet-hours", {"start": "12:45", "end": "16:00"})  # starts AFTER the reminder: allowed
    now = datetime(2026, 1, 15, 11, 50, tzinfo=UTC)  # 12:50 local: the reminder is in its grace window but we are in quiet hours
    assert await schedule.claim_due(db, alice.user_id, now=now, grace=GRACE) == []


async def test_two_parallel_requests_cannot_jointly_break_the_rule(alice):
    """Alone each is fine (quiet 12-14 with the reminder at 10:00; the reminder at 13:00 with quiet 22-08); together the order decides:
    reminder first -> the window is refused (422); window first -> the reminder moves it (200 + 200). Either way the final state holds the rule."""
    seen = set()
    for _ in range(6):
        await alice.put("/api/reminders/quiet-hours", {"start": "22:00", "end": "08:00"})
        await alice.put("/api/reminders/meal_check", meal("10:00"))

        a, b = await asyncio.gather(
            alice.put("/api/reminders/quiet-hours", {"start": "12:00", "end": "14:00"}),
            alice.put("/api/reminders/meal_check", meal("13:00")),
        )

        seen.add((a.status_code, b.status_code))
        assert (a.status_code, b.status_code) in {(200, 200), (422, 200)}
        start, end = await quiet(alice)
        [(_, local_time, enabled, _)] = await rows(alice)
        inside = (start <= local_time < end) if start < end else (local_time >= start or local_time < end)
        assert not (enabled and inside)  # the invariant holds in the final state


# ---------------------------------------------------------------- isolation


async def test_reminders_of_two_tenants_never_mix(alice, bob):
    await alice.put("/api/reminders/meal_check", meal("12:30"))
    await alice.put("/api/reminders/quiet-hours", {"start": "23:00", "end": "07:00"})

    seen = (await bob.get("/api/reminders")).json()

    assert seen["quiet_hours"] == {"start": "22:00", "end": "08:00"} and seen["timezone"] == "Asia/Tokyo"
    assert all(r["configured"] is False for r in seen["reminders"])
    await bob.put("/api/reminders/meal_check", meal("19:00"))  # his own row for the same trigger key
    assert [(r[0], r[1]) for r in await rows(alice)] == [("meal_check", "12:30:00")]  # hers is untouched
    assert (await quiet(bob)) == ("22:00:00", "08:00:00")


async def test_everything_here_needs_a_signature(client):
    for method, path, body in (
        ("get", "/api/reminders", None),
        ("put", "/api/reminders/meal_check", meal()),
        ("put", "/api/reminders/quiet-hours", {"start": "22:00", "end": "08:00"}),
    ):
        response = await getattr(client, method)(path, **({} if body is None else {"json": body}))
        assert response.status_code == 401, path


# ---------------------------------------------------------------- the pure rule


import pytest  # noqa: E402
from datetime import time  # noqa: E402

from ai_civ.web.reminders import shrink_quiet_hours  # noqa: E402


@pytest.mark.parametrize(
    ("moment", "start", "end", "expected"),
    [
        ((7, 0), (22, 0), (8, 0), ((22, 0), (6, 45))),  # the morning part: the end comes back
        ((23, 0), (22, 0), (8, 0), ((23, 15), (8, 0))),  # the evening part: the start moves on
        ((3, 0), (22, 0), (8, 0), ((22, 0), (2, 45))),  # a tie between the two sides goes to the end
        ((22, 0), (22, 0), (8, 0), ((22, 15), (8, 0))),  # exactly at the start
        ((7, 59), (22, 0), (8, 0), ((22, 0), (7, 44))),
        ((13, 0), (12, 0), (14, 0), ((12, 0), (12, 45))),  # a window inside one day: tie goes to the end
        ((12, 10), (12, 0), (12, 20), None),  # too short for a 15 minute gap on either side
    ],
)
def test_the_quiet_window_is_shrunk_just_enough_and_never_grows(moment, start, end, expected):
    result = shrink_quiet_hours(time(*moment), time(*start), time(*end))
    if expected is None:
        assert result is None
        return
    assert result == (time(*expected[0]), time(*expected[1]))
    new_start, new_end = result
    from ai_civ.bot.proactive import in_quiet_hours

    assert not in_quiet_hours(time(*moment), new_start, new_end)  # the reminder is out
    assert new_start != new_end


# ---------------------------------------------------------------- check-ins and the start of the day


async def per_day(actor):
    return (await actor.sql("SELECT checkin_per_day FROM civ.users WHERE id = :u"))[0][0]


async def test_a_fresh_user_has_the_default_checkins_and_sees_todays_times(alice):
    body = (await alice.get("/api/reminders")).json()
    assert body["checkins"]["per_day"] is None
    today = body["checkins"]["today"]
    assert 1 <= len(today) <= 4 and today == sorted(today) and all(len(t) == 5 for t in today)
    assert all("08:00" < t < "22:00" for t in today)  # inside the active hours of the default 22:00-08:00 quiet window


async def test_the_checkins_are_set_to_a_number_at_once_in_the_database(alice):
    for n in (1, 2, 4):
        body = (await alice.put("/api/reminders/checkins", {"per_day": n})).json()
        assert await per_day(alice) == n and body["checkins"]["per_day"] == n
        assert len(body["checkins"]["today"]) <= n
    assert len((await alice.put("/api/reminders/checkins", {"per_day": 1})).json()["checkins"]["today"]) == 1


async def test_zero_switches_the_checkins_off_and_null_returns_the_default(alice):
    off = (await alice.put("/api/reminders/checkins", {"per_day": 0})).json()
    assert await per_day(alice) == 0 and off["checkins"] == {"per_day": 0, "today": []}
    back = (await alice.put("/api/reminders/checkins", {"per_day": None})).json()
    assert await per_day(alice) is None and back["checkins"]["today"]


async def test_bad_checkin_counts_are_422_and_change_nothing(alice):
    await alice.put("/api/reminders/checkins", {"per_day": 2})
    for bad in ({"per_day": 5}, {"per_day": -1}, {"per_day": "many"}, {"per_day": 2, "x": 1}):
        response = await alice.put("/api/reminders/checkins", bad)
        assert response.status_code == 422, bad
    assert await per_day(alice) == 2


async def test_the_screen_shows_exactly_the_times_the_dispatcher_will_use(alice, bob):
    """The preview and the claim path share ``thin_slots``/``daily_slots``: what the screen lists for today is what ``claim_checkin`` would accept."""
    from ai_civ.config import Settings

    await alice.put("/api/reminders/checkins", {"per_day": 2})
    shown = (await alice.get("/api/reminders")).json()["checkins"]["today"]
    local = (await alice.sql("SELECT (now() AT TIME ZONE timezone)::date, quiet_hours_start, quiet_hours_end FROM civ.users WHERE id = :u"))[0]
    expected = schedule.todays_checkins(Settings(_env_file=None), alice.user_id, local[0], local[1], local[2], 2)
    assert shown == [t.strftime("%H:%M") for t in expected] and len(shown) == 2


async def test_the_checkins_of_one_tenant_are_not_touched_by_another(alice, bob):
    await alice.put("/api/reminders/checkins", {"per_day": 0})
    assert await per_day(bob) is None and (await bob.get("/api/reminders")).json()["checkins"]["per_day"] is None


async def test_the_start_of_the_day_is_the_end_of_the_quiet_hours(alice):
    body = (await alice.put("/api/reminders/quiet-hours", {"start": "23:00", "end": "07:30"})).json()
    assert body["quiet_hours"] == {"start": "23:00", "end": "07:30"}
    assert await quiet(alice) == ("23:00:00", "07:30:00")  # saved at once; the Beat dispatcher reads it on its next tick
    today = body["checkins"]["today"]
    assert all("07:30" < t < "23:00" for t in today)  # the check-ins follow the new day


async def test_a_checkin_time_is_not_a_reminder_and_the_route_does_not_swallow_the_word(alice):
    assert (await alice.put("/api/reminders/checkins", {"local_time": "10:00", "enabled": True})).status_code == 422
    assert [r[0] for r in await rows(alice)] == []

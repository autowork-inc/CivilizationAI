"""Screen "Расписание": events, RRULE series, single-occurrence cancellation, time zones, optimistic concurrency, isolation."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from tests.web_helpers import alice, bob, client  # noqa: F401

WINDOW = {"start": "2026-03-20T00:00:00Z", "end": "2026-04-13T00:00:00Z"}


def ev(**kw):
    """A valid event body: Monday 2026-03-23 09:00-10:00 Copenhagen (UTC+1, a week before the DST change)."""
    base = {
        "title": "Тренировка",
        "starts_at": "2026-03-23T09:00:00+01:00",
        "ends_at": "2026-03-23T10:00:00+01:00",
        "timezone": "Europe/Copenhagen",
    }
    return {**base, **kw}


def weekly(**kw):
    return ev(recurrence_rule="FREQ=WEEKLY;BYDAY=MO", **kw)


async def listing(actor, **window):
    response = await actor.get("/api/events", params={**WINDOW, **window})
    assert response.status_code == 200, response.text
    return response.json()


async def rows(actor, where="TRUE"):
    return await actor.sql(f"SELECT id, title, status::text, parent_event_id, created_via FROM civ.events WHERE user_id = :u AND {where} ORDER BY starts_at")


# ---------------------------------------------------------------- creating


async def test_a_single_event_is_stored_as_a_mini_app_event_and_listed(alice):
    response = await alice.post("/api/events", ev(description="  зал  ", visibility="shared"))

    assert response.status_code == 201
    created = response.json()
    assert (created["kind"], created["title"], created["description"], created["visibility"], created["status"]) == ("single", "Тренировка", "зал", "shared", "planned")
    assert created["starts_at"].startswith("2026-03-23T08:00:00")  # stored as an instant: 09:00 +01:00 = 08:00 UTC
    assert [r[4] for r in await rows(alice)] == ["mini_app"]  # created_via: the CHECK allows only mini_app / bot_confirmed
    data = await listing(alice)
    assert data["profile_timezone"] == "Europe/Copenhagen" and [o["id"] for o in data["occurrences"]] == [created["id"]]


async def test_an_event_planned_in_another_zone_is_kept_in_that_zone_and_flagged(alice):
    created = (await alice.post("/api/events", ev(timezone="Asia/Tokyo", starts_at="2026-03-23T09:00:00+09:00", ends_at="2026-03-23T10:00:00+09:00"))).json()
    assert (created["timezone"], created["timezone_mismatch"]) == ("Asia/Tokyo", True)  # the profile says Copenhagen
    same = (await alice.post("/api/events", ev(title="Дома"))).json()
    assert same["timezone_mismatch"] is False


async def test_invalid_events_are_422_in_one_error_shape_and_nothing_is_written(alice, bob):
    foreign_persona = await bob.persona("chess")
    bad = [
        ev(starts_at="2026-03-23T09:00:00"),  # no UTC offset
        ev(ends_at="2026-03-23T09:00:00+01:00"),  # ends when it starts
        ev(ends_at="2026-03-23T08:00:00+01:00"),
        ev(ends_at="2026-06-23T10:00:00+01:00"),  # months long
        ev(title="   "),
        ev(title="x", timezone="EST"),
        ev(timezone="Mars/Olympus"),
        ev(visibility="public"),
        ev(recurrence_rule="FREQ=SECONDLY"),
        ev(recurrence_rule="nonsense"),
        ev(recurrence_rule="FREQ=DAILY;UNTIL=20260101T000000"),
        ev(created_via="bot_confirmed"),  # not the client's to choose
        ev(status="done"),
        ev(persona_id=str(foreign_persona)),  # another tenant's topic
        ev(persona_id=str(uuid.uuid4())),
    ]
    for body in bad:
        response = await alice.post("/api/events", body)
        assert response.status_code == 422, body
        error = response.json()["error"]
        assert error["code"] == "invalid_request" and error["message"], body
    assert await rows(alice) == []


async def test_the_message_for_a_broken_rule_is_in_russian_and_points_at_the_field(alice):
    error = (await alice.post("/api/events", ev(recurrence_rule="FREQ=HOURLY"))).json()["error"]
    assert "раз в день, неделю" in error["message"]  # our own Russian text, shown to the user as it is
    assert error["fields"] and error["fields"][0]["message"] == error["message"]


async def test_an_event_can_belong_to_one_of_the_users_own_topics(alice):
    persona = await alice.persona("workout")
    created = (await alice.post("/api/events", ev(persona_id=str(persona)))).json()
    assert created["persona_id"] == str(persona)
    assert [p["code"] for p in (await alice.get("/api/personas")).json()] == ["workout"]


# ---------------------------------------------------------------- series


async def test_a_weekly_series_expands_in_local_time_across_the_dst_change(alice):
    created = (await alice.post("/api/events", weekly())).json()

    data = await listing(alice)

    occ = data["occurrences"]
    assert [o["kind"] for o in occ] == ["series"] * 3 and {o["id"] for o in occ} == {created["id"]}
    starts = [datetime.fromisoformat(o["starts_at"]).strftime("%m-%d %H:%M") for o in occ]
    assert starts == ["03-23 08:00", "03-30 07:00", "04-06 07:00"]  # always 09:00 on the Copenhagen wall clock: 08:00 UTC, then 07:00 UTC
    assert all(o["original_start"] == o["starts_at"] and o["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=MO" for o in occ)
    ends = [datetime.fromisoformat(o["ends_at"]) - datetime.fromisoformat(o["starts_at"]) for o in occ]
    assert {e.total_seconds() for e in ends} == {3600}


async def test_events_outside_the_window_are_not_listed_and_the_window_is_bounded(alice):
    await alice.post("/api/events", ev())
    assert (await listing(alice, start="2026-05-01T00:00:00Z", end="2026-05-30T00:00:00Z"))["occurrences"] == []
    for params in (
        {"start": "2026-03-20T00:00:00Z", "end": "2026-03-20T00:00:00Z"},
        {"start": "2026-03-20T00:00:00Z", "end": "2027-12-20T00:00:00Z"},  # > 400 days
        {"start": "2026-03-20T00:00:00", "end": "2026-03-30T00:00:00"},  # naive
        {"start": "garbage", "end": "2026-03-30T00:00:00Z"},
    ):
        assert (await alice.get("/api/events", params=params)).status_code == 422, params


async def test_cancelling_one_occurrence_makes_an_exception_row_and_hides_it_idempotently(alice):
    series = (await alice.post("/api/events", weekly())).json()
    second = (await listing(alice))["occurrences"][1]

    first = await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": second["original_start"]})
    again = await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": second["original_start"]})

    assert (first.status_code, first.json()) == (200, {"created": True}) and again.json() == {"created": False}
    starts = [o["original_start"][:10] for o in (await listing(alice))["occurrences"]]
    assert starts == ["2026-03-23", "2026-04-06"]  # the 30th is gone, the rest stay
    exceptions = await rows(alice, "parent_event_id IS NOT NULL")
    assert len(exceptions) == 1 and exceptions[0][2] == "cancelled" and str(exceptions[0][3]) == series["id"]
    master = await alice.sql("SELECT recurrence_rule FROM civ.events WHERE id = :i", i=series["id"])
    assert master[0][0] == "FREQ=WEEKLY;BYDAY=MO"  # the series itself is untouched


async def test_only_real_occurrences_of_a_series_can_be_cancelled(alice):
    series = (await alice.post("/api/events", weekly())).json()
    single = (await alice.post("/api/events", ev(title="Разовое"))).json()

    off_day = await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": "2026-03-24T08:00:00Z"})
    old_utc_hour = await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": "2026-03-30T08:00:00Z"})  # wrong after DST
    not_a_series = await alice.post(f"/api/events/{single['id']}/occurrences/cancel", {"original_start": single["starts_at"]})
    naive = await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": "2026-03-30T07:00:00"})
    unknown = await alice.post(f"/api/events/{uuid.uuid4()}/occurrences/cancel", {"original_start": "2026-03-30T07:00:00Z"})

    assert [r.status_code for r in (off_day, old_utc_hour, not_a_series, naive, unknown)] == [422, 422, 400, 422, 404]
    assert await rows(alice, "parent_event_id IS NOT NULL") == []


async def test_parallel_cancels_of_one_occurrence_create_exactly_one_exception(alice):
    series = (await alice.post("/api/events", weekly())).json()
    target = (await listing(alice))["occurrences"][2]["original_start"]

    results = await asyncio.gather(*[alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": target}) for _ in range(4)])

    assert all(r.status_code == 200 for r in results) and sum(r.json()["created"] for r in results) == 1
    assert len(await rows(alice, "parent_event_id IS NOT NULL")) == 1


# ---------------------------------------------------------------- editing: optimistic concurrency and exceptions


async def test_an_edit_needs_the_version_the_page_saw(alice):
    created = (await alice.post("/api/events", ev())).json()

    ok = await alice.patch(f"/api/events/{created['id']}", {"expected_updated_at": created["updated_at"], "title": "Бег", "visibility": "private"})
    stale = await alice.patch(f"/api/events/{created['id']}", {"expected_updated_at": created["updated_at"], "title": "Плавание"})

    assert ok.status_code == 200 and ok.json()["event"]["title"] == "Бег" and ok.json()["event"]["visibility"] == "private"
    assert ok.json()["event"]["updated_at"] != created["updated_at"]
    assert stale.status_code == 409 and stale.json()["error"]["current_updated_at"]
    assert (await rows(alice))[0][1] == "Бег"  # the stale writer changed nothing


async def test_parallel_edits_of_one_event_are_ordered_one_wins(alice):
    created = (await alice.post("/api/events", ev())).json()

    results = await asyncio.gather(
        *[alice.patch(f"/api/events/{created['id']}", {"expected_updated_at": created["updated_at"], "title": f"версия {i}"}) for i in range(4)]
    )

    assert sorted(r.status_code for r in results) == [200, 409, 409, 409]


async def test_changing_the_series_rule_or_start_drops_its_exceptions_but_a_title_does_not(alice):
    series = (await alice.post("/api/events", weekly())).json()
    target = (await listing(alice))["occurrences"][1]["original_start"]
    await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": target})

    retitled = (await alice.patch(f"/api/events/{series['id']}", {"expected_updated_at": series["updated_at"], "title": "Новое имя"})).json()
    assert retitled["exceptions_reset"] == 0 and len(await rows(alice, "parent_event_id IS NOT NULL")) == 1

    moved = (await alice.patch(f"/api/events/{series['id']}", {"expected_updated_at": retitled["event"]["updated_at"], "recurrence_rule": "FREQ=WEEKLY;BYDAY=TU"})).json()
    assert moved["exceptions_reset"] == 1 and await rows(alice, "parent_event_id IS NOT NULL") == []
    assert moved["event"]["recurrence_rule"] == "FREQ=WEEKLY;BYDAY=TU"


async def test_the_repetition_can_be_removed_and_a_description_cleared(alice):
    series = (await alice.post("/api/events", weekly(description="зал"))).json()

    body = (await alice.patch(f"/api/events/{series['id']}", {"expected_updated_at": series["updated_at"], "recurrence_rule": None, "description": None})).json()

    assert (body["event"]["kind"], body["event"]["recurrence_rule"], body["event"]["description"]) == ("single", None, None)
    assert [o["kind"] for o in (await listing(alice))["occurrences"]] == ["single"]


async def test_bad_edits_are_rejected_and_change_nothing(alice, bob):
    created = (await alice.post("/api/events", ev())).json()
    stamp = created["updated_at"]
    bad = [
        {"expected_updated_at": stamp},  # nothing to change
        {"expected_updated_at": stamp, "ends_at": "2026-03-23T08:00:00+01:00"},  # ends before it starts
        {"expected_updated_at": stamp, "title": None},
        {"expected_updated_at": stamp, "title": "  "},
        {"expected_updated_at": stamp, "timezone": "EST"},
        {"expected_updated_at": stamp, "recurrence_rule": "FREQ=SECONDLY"},
        {"expected_updated_at": stamp, "persona_id": str(await bob.persona("chess"))},
        {"expected_updated_at": "2026-03-23T08:00:00", "title": "x"},  # naive stamp
        {"expected_updated_at": stamp, "status": "done"},
        {"expected_updated_at": stamp, "starts_at": None},
    ]
    for body in bad:
        assert (await alice.patch(f"/api/events/{created['id']}", body)).status_code == 422, body
    assert [r[1] for r in await rows(alice)] == ["Тренировка"]


async def test_a_removed_occurrence_cannot_be_edited_but_deleting_its_exception_brings_it_back(alice):
    series = (await alice.post("/api/events", weekly())).json()
    target = (await listing(alice))["occurrences"][1]["original_start"]
    await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": target})
    [(exception_id, *_)] = await rows(alice, "parent_event_id IS NOT NULL")
    [(stamp,)] = await alice.sql("SELECT updated_at FROM civ.events WHERE id = :i", i=exception_id)

    edit = await alice.patch(f"/api/events/{exception_id}", {"expected_updated_at": stamp.isoformat(), "title": "x"})
    assert edit.status_code == 400 and edit.json()["error"]["code"] == "occurrence_cancelled"

    assert (await alice.delete(f"/api/events/{exception_id}", params={"expected_updated_at": stamp.isoformat()})).status_code == 204
    assert len((await listing(alice))["occurrences"]) == 3  # the occurrence is back


# ---------------------------------------------------------------- deleting


async def test_deleting_a_series_removes_its_exceptions_and_needs_the_current_version(alice):
    series = (await alice.post("/api/events", weekly())).json()
    target = (await listing(alice))["occurrences"][1]["original_start"]
    await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": target})
    [(stamp,)] = await alice.sql("SELECT updated_at FROM civ.events WHERE id = :i", i=series["id"])

    stale = await alice.delete(f"/api/events/{series['id']}", params={"expected_updated_at": "2020-01-01T00:00:00Z"})
    gone = await alice.delete(f"/api/events/{series['id']}", params={"expected_updated_at": stamp.isoformat()})

    assert stale.status_code == 409 and gone.status_code == 204
    assert await rows(alice) == []  # the master and its exception are both gone (ON DELETE CASCADE)
    assert (await alice.delete(f"/api/events/{series['id']}", params={"expected_updated_at": stamp.isoformat()})).status_code == 404


# ---------------------------------------------------------------- isolation


async def test_schedules_of_two_tenants_never_mix(alice, bob):
    mine = (await alice.post("/api/events", ev(title="Секрет Алисы"))).json()
    await bob.post("/api/events", ev(title="Дело Боба"))

    assert [o["title"] for o in (await listing(bob))["occurrences"]] == ["Дело Боба"]
    assert "Алисы" not in (await bob.get("/api/events", params=WINDOW)).text

    patch = await bob.patch(f"/api/events/{mine['id']}", {"expected_updated_at": mine["updated_at"], "title": "hacked"})
    delete = await bob.delete(f"/api/events/{mine['id']}", params={"expected_updated_at": mine["updated_at"]})
    cancel = await bob.post(f"/api/events/{mine['id']}/occurrences/cancel", {"original_start": mine["starts_at"]})
    assert (patch.status_code, delete.status_code, cancel.status_code) == (404, 404, 404)  # as if it did not exist
    assert [r[1] for r in await rows(alice)] == ["Секрет Алисы"]

    assert (await bob.get("/api/personas")).json() == []
    await alice.persona("mine")
    assert (await bob.get("/api/personas")).json() == []

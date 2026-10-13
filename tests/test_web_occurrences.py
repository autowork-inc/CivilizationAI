"""Editing, moving and removing ONE occurrence of a series (exception rows). Real PostgreSQL + RLS."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

from tests.test_web_schedule import WINDOW, ev, listing, rows, weekly
from tests.web_helpers import alice, bob, client  # noqa: F401


async def series_with_occurrences(actor, **kw):
    series = (await actor.post("/api/events", weekly(**kw))).json()
    return series, (await listing(actor))["occurrences"]  # Mon 2026-03-23, 03-30 (after DST), 04-06


def titles(data):
    return [(o["original_start"] or o["starts_at"])[:10] + " " + o["title"] for o in data["occurrences"]]


async def override(actor, series, original_start, **fields):
    return await actor.post(f"/api/events/{series['id']}/occurrences/override", {"original_start": original_start, **fields})


# ---------------------------------------------------------------- creating an exception (override)


async def test_changing_the_title_of_one_occurrence_leaves_the_rest_of_the_series_alone(alice):
    series, occ = await series_with_occurrences(alice, description="зал")

    response = await override(alice, series, occ[1]["original_start"], title="Лёгкая тренировка", description="")

    assert response.status_code == 201
    exc = response.json()
    assert (exc["kind"], exc["series_id"], exc["title"], exc["description"], exc["status"]) == ("exception", series["id"], "Лёгкая тренировка", None, "planned")
    assert exc["original_start"] == occ[1]["original_start"] and exc["starts_at"] == occ[1]["starts_at"]  # same time, DST-correct (07:00Z)
    assert exc["ends_at"] == occ[1]["ends_at"] and exc["id"] != series["id"]
    data = await listing(alice)
    assert [(o["kind"], o["title"]) for o in data["occurrences"]] == [("series", "Тренировка"), ("exception", "Лёгкая тренировка"), ("series", "Тренировка")]
    master = await alice.sql("SELECT title, description, recurrence_rule FROM civ.events WHERE id = :i", i=series["id"])
    assert master[0] == ("Тренировка", "зал", "FREQ=WEEKLY;BYDAY=MO")  # the series itself is untouched
    [(parent, status, via)] = await alice.sql("SELECT parent_event_id, status::text, created_via FROM civ.events WHERE id = :i", i=exc["id"])
    assert (str(parent), status, via) == (series["id"], "planned", "mini_app")


async def test_moving_an_occurrence_keeps_its_duration_and_hides_the_original_slot(alice):
    series, occ = await series_with_occurrences(alice)

    moved = (await override(alice, series, occ[1]["original_start"], starts_at="2026-03-31T17:00:00+02:00")).json()  # Tue, 17:00 CEST

    assert moved["starts_at"].startswith("2026-03-31T15:00:00") and moved["ends_at"].startswith("2026-03-31T16:00:00")  # still one hour
    assert moved["original_start"] == occ[1]["original_start"]  # remembers which occurrence it replaced
    starts = [o["starts_at"][:13] for o in (await listing(alice))["occurrences"]]
    assert starts == [occ[0]["starts_at"][:13], "2026-03-31T15", occ[2]["starts_at"][:13]]  # the Monday 30th slot is gone


async def test_an_occurrence_can_be_moved_out_of_and_into_the_window(alice):
    series, occ = await series_with_occurrences(alice)
    out = await override(alice, series, occ[0]["original_start"], starts_at="2026-06-01T10:00:00+02:00")  # far outside the window
    assert out.status_code == 201
    assert [o["kind"] for o in (await listing(alice))["occurrences"]] == ["series", "series"]  # gone from this window, no ghost slot

    later = await listing(alice, start="2026-05-25T00:00:00Z", end="2026-06-10T00:00:00Z")
    assert [(o["kind"], o["starts_at"][:10]) for o in later["occurrences"] if o["kind"] == "exception"] == [("exception", "2026-06-01")]

    into = await override(alice, series, occ[2]["original_start"], starts_at="2026-03-25T08:00:00+01:00")  # earlier, still in the window
    assert into.status_code == 201


async def test_a_change_may_set_start_end_visibility_topic_and_all_day_explicitly(alice):
    topic = await alice.persona("workout")
    series, occ = await series_with_occurrences(alice)

    exc = (
        await override(
            alice, series, occ[2]["original_start"],
            starts_at="2026-04-06T18:00:00+02:00", ends_at="2026-04-06T19:30:00+02:00", visibility="shared", persona_id=str(topic),
        )
    ).json()  # fmt: skip

    assert (exc["visibility"], exc["persona_id"]) == ("shared", str(topic))
    assert datetime.fromisoformat(exc["ends_at"]) - datetime.fromisoformat(exc["starts_at"]) == timedelta(minutes=90)


async def test_the_topic_of_the_series_is_inherited_and_can_be_removed_for_one_occurrence(alice):
    topic = await alice.persona("workout")
    series, occ = await series_with_occurrences(alice, persona_id=str(topic))
    inherited = (await override(alice, series, occ[0]["original_start"], title="А")).json()
    removed = (await override(alice, series, occ[1]["original_start"], persona_id=None)).json()
    assert (inherited["persona_id"], removed["persona_id"]) == (str(topic), None)


async def test_bad_overrides_are_refused_and_write_nothing(alice, bob):
    series, occ = await series_with_occurrences(alice)
    single = (await alice.post("/api/events", ev(title="Разовое"))).json()
    original = occ[1]["original_start"]
    bad = [
        ({}, 422),  # nothing to change
        ({"title": "  "}, 422),
        ({"title": None}, 422),
        ({"starts_at": "2026-03-31T17:00:00"}, 422),  # no UTC offset
        ({"starts_at": "2026-03-31T17:00:00Z", "ends_at": "2026-03-31T16:00:00Z"}, 422),  # ends before it starts
        ({"ends_at": "2026-03-29T00:00:00Z"}, 422),  # only the end: before the occurrence's own start
        ({"starts_at": "2026-03-31T17:00:00Z", "ends_at": "2026-06-01T00:00:00Z"}, 422),  # months long
        ({"visibility": "public"}, 422),
        ({"persona_id": str(await bob.persona("chess"))}, 422),  # somebody else's topic
        ({"persona_id": str(uuid.uuid4())}, 422),
        ({"timezone": "Asia/Tokyo"}, 422),  # belongs to the whole series
        ({"recurrence_rule": "FREQ=DAILY"}, 422),
        ({"status": "cancelled"}, 422),
    ]
    for fields, status in bad:
        response = await override(alice, series, original, **fields)
        assert response.status_code == status, fields
    assert (await override(alice, series, "2026-03-31T07:00:00Z", title="x")).status_code == 422  # not an occurrence of the series
    assert (await override(alice, series, "2026-03-30T08:00:00Z", title="x")).status_code == 422  # the pre-DST UTC hour
    assert (await override(alice, series, "2026-03-30T07:00:00", title="x")).status_code == 422  # naive
    assert (await override(alice, single, single["starts_at"], title="x")).status_code == 400  # not a series
    assert (await alice.post(f"/api/events/{uuid.uuid4()}/occurrences/override", {"original_start": original, "title": "x"})).status_code == 404
    assert await rows(alice, "parent_event_id IS NOT NULL") == []


async def test_an_occurrence_that_already_has_an_exception_is_a_409_not_an_overwrite(alice):
    series, occ = await series_with_occurrences(alice)
    first = (await override(alice, series, occ[1]["original_start"], title="Первый")).json()

    again = await override(alice, series, occ[1]["original_start"], title="Второй")

    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "conflict" and error["exception_id"] == first["id"] and error["current_updated_at"]
    assert [o["title"] for o in (await listing(alice))["occurrences"] if o["kind"] == "exception"] == ["Первый"]

    await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": occ[2]["original_start"]})
    assert (await override(alice, series, occ[2]["original_start"], title="Воскрес")).status_code == 409  # a removed one is restored by deleting its row


async def test_parallel_changes_of_one_occurrence_create_exactly_one_exception(alice):
    series, occ = await series_with_occurrences(alice)

    results = await asyncio.gather(*[override(alice, series, occ[1]["original_start"], title=f"версия {i}") for i in range(4)])

    assert sorted(r.status_code for r in results) == [201, 409, 409, 409]
    assert len(await rows(alice, "parent_event_id IS NOT NULL")) == 1


# ---------------------------------------------------------------- editing an existing exception


async def test_an_existing_exception_is_edited_with_patch_and_its_own_version(alice):
    series, occ = await series_with_occurrences(alice)
    exc = (await override(alice, series, occ[1]["original_start"], title="Сдвинутая", starts_at="2026-03-31T17:00:00+02:00")).json()

    ok = await alice.patch(f"/api/events/{exc['id']}", {"expected_updated_at": exc["updated_at"], "title": "Ещё раз", "starts_at": "2026-04-01T09:00:00+02:00", "ends_at": "2026-04-01T09:45:00+02:00", "visibility": "private"})
    stale = await alice.patch(f"/api/events/{exc['id']}", {"expected_updated_at": exc["updated_at"], "title": "Устарело"})

    assert ok.status_code == 200
    event = ok.json()["event"]
    assert (event["kind"], event["title"], event["visibility"], event["original_start"], event["series_id"]) == ("exception", "Ещё раз", "private", occ[1]["original_start"], series["id"])
    assert event["starts_at"].startswith("2026-04-01T07:00:00") and ok.json()["exceptions_reset"] == 0
    assert stale.status_code == 409 and stale.json()["error"]["current_updated_at"]
    assert [o["title"] for o in (await listing(alice))["occurrences"] if o["kind"] == "exception"] == ["Ещё раз"]


async def test_the_zone_and_the_repetition_cannot_be_changed_on_one_occurrence(alice):
    series, occ = await series_with_occurrences(alice)
    exc = (await override(alice, series, occ[1]["original_start"], title="X")).json()
    for body in ({"timezone": "Asia/Tokyo"}, {"recurrence_rule": "FREQ=DAILY"}, {"recurrence_rule": None}):
        response = await alice.patch(f"/api/events/{exc['id']}", {"expected_updated_at": exc["updated_at"], **body})
        assert response.status_code == 400 and response.json()["error"]["code"] == "exception_field", body
    for body in ({"ends_at": "2020-01-01T00:00:00Z"}, {"title": None}, {"title": "  "}):
        assert (await alice.patch(f"/api/events/{exc['id']}", {"expected_updated_at": exc["updated_at"], **body})).status_code == 422, body


# ---------------------------------------------------------------- removing and restoring


async def test_a_changed_occurrence_can_still_be_removed_and_removal_is_idempotent(alice):
    series, occ = await series_with_occurrences(alice)
    await override(alice, series, occ[1]["original_start"], title="Сдвинутая", starts_at="2026-03-31T17:00:00+02:00")

    first = await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": occ[1]["original_start"]})
    again = await alice.post(f"/api/events/{series['id']}/occurrences/cancel", {"original_start": occ[1]["original_start"]})

    assert (first.json(), again.json()) == ({"created": True}, {"created": False})  # the change was removed, the second call had nothing to do
    assert [o["kind"] for o in (await listing(alice))["occurrences"]] == ["series", "series"]
    exceptions = await rows(alice, "parent_event_id IS NOT NULL")
    assert len(exceptions) == 1 and exceptions[0][2] == "cancelled"  # still ONE row, now cancelled


async def test_deleting_the_exception_row_restores_the_regular_occurrence(alice):
    series, occ = await series_with_occurrences(alice)
    exc = (await override(alice, series, occ[1]["original_start"], title="Сдвинутая", starts_at="2026-03-31T17:00:00+02:00")).json()

    gone = await alice.delete(f"/api/events/{exc['id']}", params={"expected_updated_at": exc["updated_at"]})

    assert gone.status_code == 204
    assert [(o["kind"], o["title"]) for o in (await listing(alice))["occurrences"]] == [("series", "Тренировка")] * 3
    assert await rows(alice, "parent_event_id IS NOT NULL") == []


async def test_changing_the_series_rule_drops_the_changed_occurrences_too(alice):
    series, occ = await series_with_occurrences(alice)
    await override(alice, series, occ[1]["original_start"], title="Сдвинутая")
    patched = await alice.patch(f"/api/events/{series['id']}", {"expected_updated_at": series["updated_at"], "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO;INTERVAL=2"})
    assert patched.json()["exceptions_reset"] == 1
    assert all(o["kind"] == "series" for o in (await listing(alice))["occurrences"])


async def test_get_one_event_returns_the_stored_row_for_series_and_exceptions(alice):
    series, occ = await series_with_occurrences(alice)
    exc = (await override(alice, series, occ[1]["original_start"], title="X")).json()

    master = (await alice.get(f"/api/events/{series['id']}")).json()
    changed = (await alice.get(f"/api/events/{exc['id']}")).json()

    assert (master["kind"], master["recurrence_rule"], master["series_id"], master["updated_at"]) == ("series", "FREQ=WEEKLY;BYDAY=MO", None, series["updated_at"])
    assert (changed["kind"], changed["series_id"], changed["original_start"]) == ("exception", series["id"], occ[1]["original_start"])
    assert (await alice.get(f"/api/events/{uuid.uuid4()}")).status_code == 404


# ---------------------------------------------------------------- isolation


async def test_nobody_can_change_or_read_another_tenants_occurrences(alice, bob):
    series, occ = await series_with_occurrences(alice)
    exc = (await override(alice, series, occ[1]["original_start"], title="Моё")).json()

    steal = await bob.post(f"/api/events/{series['id']}/occurrences/override", {"original_start": occ[0]["original_start"], "title": "hacked"})
    patch = await bob.patch(f"/api/events/{exc['id']}", {"expected_updated_at": exc["updated_at"], "title": "hacked"})
    read = await bob.get(f"/api/events/{exc['id']}")

    assert (steal.status_code, patch.status_code, read.status_code) == (404, 404, 404)
    assert [o["title"] for o in (await listing(alice))["occurrences"] if o["kind"] == "exception"] == ["Моё"]
    assert (await listing(bob))["occurrences"] == []
    assert len(await rows(alice, "parent_event_id IS NOT NULL")) == 1

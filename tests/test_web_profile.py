"""Screen "Мозг": reading the Core profile, versioned edits (one transaction, FOR UPDATE), the time zone. Real PostgreSQL + RLS."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from tests.web_helpers import alice, bob, client  # noqa: F401


async def test_the_profile_lists_current_non_principle_facts_and_the_time_zone(alice):
    await alice.fact("city", value="Копенгаген", priority=60)
    await alice.fact("principle.sleep", value="спать 8 часов", category="principle")
    old = await alice.fact("weight", value="80", category="body")
    await alice.sql("UPDATE civ.core_facts SET valid_to = now() WHERE id = :i", i=old)  # a closed version

    response = await alice.get("/api/profile")

    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "Europe/Copenhagen"
    assert sorted(f["key"] for f in body["facts"]) == ["city", "name"]  # no principles, no history
    city = next(f for f in body["facts"] if f["key"] == "city")
    assert (city["value_text"], city["category"], city["priority"], city["source"]) == ("Копенгаген", "identity", 60, "user_manual")


async def test_editing_a_fact_closes_the_old_version_and_inserts_the_next_in_one_step(alice):
    fact_id = await alice.fact("city", value="Копенгаген", priority=60)

    response = await alice.put("/api/profile/facts/city", {"expected_fact_id": str(fact_id), "value_text": "  Орхус  "})

    assert response.status_code == 200
    new = response.json()
    assert (new["key"], new["value_text"], new["priority"], new["source"]) == ("city", "Орхус", 60, "user_manual")
    assert new["id"] != str(fact_id)
    versions = await alice.versions("city")
    assert [(v[1], v[2]) for v in versions] == [("Копенгаген", True), ("Орхус", False)]  # history kept, one current row
    assert (await alice.current_facts("city"))["city"][0] == uuid.UUID(new["id"])


async def test_a_numeric_edit_keeps_the_unit_and_the_text_when_not_sent(alice):
    await alice.sql(
        "INSERT INTO civ.core_facts (user_id, category, key, value_num, value_unit, value_text, priority, source) "
        "VALUES (:u, 'body', 'weight', 80, 'kg', 'утром', 70, 'onboarding')"
    )
    [(fact_id,)] = await alice.sql("SELECT id FROM civ.core_facts WHERE user_id = :u AND key = 'weight'")

    response = await alice.put("/api/profile/facts/weight", {"expected_fact_id": str(fact_id), "value_num": 78.5})

    new = response.json()
    assert (new["value_num"], new["value_unit"], new["value_text"], new["priority"]) == (78.5, "kg", "утром", 70)


async def test_a_stale_edit_is_a_409_with_the_current_version_and_changes_nothing(alice):
    first = await alice.fact("city", value="Копенгаген")
    ok = await alice.put("/api/profile/facts/city", {"expected_fact_id": str(first), "value_text": "Орхус"})

    stale = await alice.put("/api/profile/facts/city", {"expected_fact_id": str(first), "value_text": "Берлин"})

    assert stale.status_code == 409
    error = stale.json()["error"]
    assert error["code"] == "conflict" and error["current_fact_id"] == ok.json()["id"]
    assert (await alice.current_facts("city"))["city"][1] == "Орхус"  # the stale writer did not overwrite the winner
    assert len(await alice.versions("city")) == 2


async def test_two_parallel_edits_of_one_fact_never_leave_two_current_versions(alice):
    fact_id = await alice.fact("city", value="Копенгаген")

    async def edit(value):
        return await alice.put("/api/profile/facts/city", {"expected_fact_id": str(fact_id), "value_text": value})

    results = await asyncio.gather(edit("Орхус"), edit("Берлин"), edit("Осло"))

    assert sorted(r.status_code for r in results) == [200, 409, 409]  # FOR UPDATE orders them; two lose cleanly
    winner = next(r for r in results if r.status_code == 200).json()
    versions = await alice.versions("city")
    assert len(versions) == 2 and sum(1 for v in versions if not v[2]) == 1  # exactly one current row
    assert (await alice.current_facts("city"))["city"][1] == winner["value_text"]


async def test_deleting_a_fact_closes_it_on_purpose_and_keeps_the_history(alice):
    fact_id = await alice.fact("city", value="Копенгаген")

    response = await alice.delete("/api/profile/facts/city", params={"expected_fact_id": str(fact_id)})

    assert response.status_code == 204
    assert "city" not in await alice.current_facts("city")
    assert [(v[1], v[2]) for v in await alice.versions("city")] == [("Копенгаген", True)]
    again = await alice.delete("/api/profile/facts/city", params={"expected_fact_id": str(fact_id)})
    assert again.status_code == 404


async def test_principles_are_not_edited_on_the_profile_screen(alice):
    fact_id = await alice.fact("principle.sleep", value="спать 8 часов", category="principle")
    response = await alice.put("/api/profile/facts/principle.sleep", {"expected_fact_id": str(fact_id), "value_text": "x"})
    assert response.status_code == 400 and response.json()["error"]["code"] == "wrong_screen"
    assert (await alice.current_facts("principle.sleep"))["principle.sleep"][1] == "спать 8 часов"


async def test_bad_edits_are_422_in_one_error_shape(alice):
    fact_id = await alice.fact("city", value="Копенгаген")
    for body in (
        {"expected_fact_id": str(fact_id)},  # nothing to change
        {"expected_fact_id": str(fact_id), "value_text": "   "},  # blank
        {"expected_fact_id": str(fact_id), "value_text": "x" * 2000},
        {"expected_fact_id": str(fact_id), "value_num": 1e13},
        {"expected_fact_id": "not-a-uuid", "value_text": "x"},
        {"expected_fact_id": str(fact_id), "value_text": "x", "priority": 100},  # fields that are not editable here
        {"expected_fact_id": str(fact_id), "value_text": "x", "source": "approved_review"},
    ):
        response = await alice.put("/api/profile/facts/city", body)
        assert response.status_code == 422, body
        error = response.json()["error"]
        assert error["code"] == "invalid_request" and error["message"] and isinstance(error["fields"], list)
    assert (await alice.current_facts("city"))["city"][0] == fact_id  # nothing was written


async def test_a_malformed_key_never_reaches_the_database(alice):
    for key in ("Name", "1abc", "a b", "a..b", "x" * 101):
        response = await alice.put(f"/api/profile/facts/{key}", {"expected_fact_id": str(uuid.uuid4()), "value_text": "x"})
        assert response.status_code in (404, 422), key


async def test_the_time_zone_changes_only_for_the_caller_and_only_to_a_real_iana_zone(alice, bob):
    ok = await alice.put("/api/profile/timezone", {"timezone": "Asia/Almaty"})
    assert ok.status_code == 200 and ok.json() == {"timezone": "Asia/Almaty"}
    assert (await alice.get("/api/me")).json()["timezone"] == "Asia/Almaty"
    assert (await bob.get("/api/me")).json()["timezone"] == "Asia/Tokyo"  # untouched

    for bad in ("EST", "+03:00", "Mars/Olympus", "", "Europe/", "x" * 100):
        response = await alice.put("/api/profile/timezone", {"timezone": bad})
        assert response.status_code == 422, bad
    assert (await alice.get("/api/profile")).json()["timezone"] == "Asia/Almaty"
    assert (await alice.put("/api/profile/timezone", {"timezone": "UTC"})).status_code == 200


async def test_the_timezone_endpoint_cannot_be_used_to_touch_other_columns(alice):
    before = await alice.sql("SELECT forum_chat_id, telegram_user_id, onboarding_state FROM civ.users WHERE id = :u")
    response = await alice.put("/api/profile/timezone", {"timezone": "UTC", "forum_chat_id": 1, "telegram_user_id": 2})
    assert response.status_code == 422
    assert await alice.sql("SELECT forum_chat_id, telegram_user_id, onboarding_state FROM civ.users WHERE id = :u") == before


async def test_tenants_are_isolated_on_every_profile_route(alice, bob):
    alice_fact = await alice.fact("city", value="Копенгаген")
    assert [f["key"] for f in (await bob.get("/api/profile")).json()["facts"]] == ["name"]  # only his own

    # Bob has no "city": Alice's version id gives him nothing (404), and nothing of Alice's changes
    edit = await bob.put("/api/profile/facts/city", {"expected_fact_id": str(alice_fact), "value_text": "hacked"})
    delete = await bob.delete("/api/profile/facts/city", params={"expected_fact_id": str(alice_fact)})
    assert (edit.status_code, delete.status_code) == (404, 404)

    # Bob has his own "city": Alice's id is simply a stale version of HIS fact (409), and again nothing changes
    bob_fact = await bob.fact("city", value="Токио")
    edit = await bob.put("/api/profile/facts/city", {"expected_fact_id": str(alice_fact), "value_text": "hacked"})
    assert edit.status_code == 409 and edit.json()["error"]["current_fact_id"] == str(bob_fact)  # HIS version, never hers
    assert (await alice.current_facts("city"))["city"][:2] == (alice_fact, "Копенгаген")
    assert (await bob.current_facts("city"))["city"][1] == "Токио"


# ================================================================== GET /api/profile/memories: the indexed episodic history


VECTOR = [0.1] * 1536


async def episode(actor, content: str, *, importance: int = 5, created_at: str | None = None) -> uuid.UUID:
    from tests.helpers import new_episode

    episode_id = await new_episode(actor.owner_engine, actor.user_id, content, VECTOR)
    await actor.sql(
        "UPDATE civ.episodic_memories SET importance = :i, created_at = COALESCE(CAST(:c AS timestamptz), created_at) WHERE id = :e",
        i=importance, c=None if created_at is None else datetime.fromisoformat(created_at), e=episode_id,
    )  # fmt: skip
    return episode_id


async def test_memories_are_listed_newest_first_with_a_short_snippet_and_no_embedding(alice):
    old = await episode(alice, "Обсуждали поездку в Орхус", importance=3, created_at="2026-10-01T10:00:00+00:00")
    new = await episode(alice, "Начал бегать по утрам\n\nи   записался на 10 км", importance=8, created_at="2026-10-05T10:00:00+00:00")

    response = await alice.get("/api/profile/memories")

    assert response.status_code == 200
    body = response.json()
    assert [m["id"] for m in body["items"]] == [str(new), str(old)]
    assert body["items"][0]["snippet"] == "Начал бегать по утрам и записался на 10 км"  # one line
    assert set(body["items"][0]) == {"id", "created_at", "snippet", "importance"} and body["items"][0]["importance"] == 8
    assert body["items"][0]["created_at"].startswith("2026-10-05T10:00:00")
    assert (body["limit"], body["offset"], body["has_more"]) == (20, 0, False)


async def test_a_long_memory_is_cut_to_a_snippet(alice):
    await episode(alice, "слово " * 100)
    [item] = (await alice.get("/api/profile/memories")).json()["items"]
    assert len(item["snippet"]) == 200 and item["snippet"].endswith("…")


async def test_memories_are_paged_by_limit_and_offset(alice):
    ids = [await episode(alice, f"запись {n}", created_at=f"2026-10-0{n}T10:00:00+00:00") for n in range(1, 6)]  # 1..5, oldest first

    first = (await alice.get("/api/profile/memories", params={"limit": 2})).json()
    second = (await alice.get("/api/profile/memories", params={"limit": 2, "offset": 2})).json()
    last = (await alice.get("/api/profile/memories", params={"limit": 2, "offset": 4})).json()
    beyond = (await alice.get("/api/profile/memories", params={"limit": 2, "offset": 50})).json()

    assert [m["id"] for m in first["items"]] == [str(ids[4]), str(ids[3])] and first["has_more"] is True
    assert [m["id"] for m in second["items"]] == [str(ids[2]), str(ids[1])] and second["has_more"] is True
    assert [m["id"] for m in last["items"]] == [str(ids[0])] and last["has_more"] is False
    assert beyond["items"] == [] and beyond["has_more"] is False


async def test_the_default_page_is_twenty(alice):
    for n in range(23):
        await episode(alice, f"запись {n}")
    body = (await alice.get("/api/profile/memories")).json()
    assert len(body["items"]) == 20 and body["has_more"] is True and body["limit"] == 20


async def test_a_page_never_shows_another_tenants_memories(alice, bob):
    mine = await episode(alice, "моя запись")
    await episode(bob, "чужая запись")

    assert [m["id"] for m in (await alice.get("/api/profile/memories")).json()["items"]] == [str(mine)]
    assert [m["snippet"] for m in (await bob.get("/api/profile/memories")).json()["items"]] == ["чужая запись"]


async def test_bad_paging_is_422_in_the_common_error_shape_and_no_signature_is_401(alice, client):
    for params in ({"limit": 0}, {"limit": 101}, {"limit": "x"}, {"offset": -1}):
        response = await alice.get("/api/profile/memories", params=params)
        assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_request", params
    assert (await client.get("/api/profile/memories")).status_code == 401
    empty = (await alice.get("/api/profile/memories")).json()
    assert empty["items"] == [] and empty["has_more"] is False


async def test_the_memories_route_is_read_only(alice):
    for send in (alice.post, alice.put, alice.delete):
        assert (await send("/api/profile/memories")).status_code == 405


# ================================================================== the screen "Мозг" shows words, not machine keys


async def sections(actor) -> dict[str, list[dict]]:
    body = (await actor.get("/api/profile/facts")).json()
    return {g["label"]: g["facts"] for g in body["groups"]}


async def test_facts_are_grouped_into_human_sections_and_no_key_is_the_title(alice):
    await alice.fact("work.trading_bot", value="Торговый бот на Python", category="other")
    await alice.fact("work_linkedin_go", value="Подать заявки на LinkedIn", category="other")  # an older underscore spelling
    await alice.fact("preference.morning_run", value="Бегаю по утрам", category="preference")
    await alice.fact("habit.cold_shower", value="Холодный душ", category="preference")
    await alice.fact("health.lactose", value="Непереносимость лактозы", category="health")
    await alice.fact("plan.dentist", value="Стоматолог в пятницу", category="other")
    await alice.fact("goal.1", value="Выучить датский", category="goal")
    await alice.fact("city", value="Копенгаген", category="identity")

    got = await sections(alice)

    assert {f["value_text"] for f in got["Проекты"]} == {"Торговый бот на Python", "Подать заявки на LinkedIn"}
    assert {f["value_text"] for f in got["Привычки"]} == {"Бегаю по утрам", "Холодный душ"}
    assert [f["value_text"] for f in got["Здоровье"]] == ["Непереносимость лактозы"]
    assert [f["value_text"] for f in got["Планы"]] == ["Стоматолог в пятницу"]
    assert [(f["title"], f["value_text"]) for f in got["Цели"]] == [("Цель 1", "Выучить датский")]
    assert any(f["title"] == "Город" for f in got["О вас"])
    # free-form facts have no title at all: the page shows their text, never ``work.trading_bot``
    free = [f for g in ("Проекты", "Привычки", "Здоровье", "Планы") for f in got[g]]
    assert free and all(f["title"] is None for f in free)


async def test_the_sections_come_in_a_fixed_order_and_empty_ones_are_left_out(alice):
    await alice.fact("health.lactose", value="Непереносимость лактозы", category="health")
    await alice.fact("goal.1", value="Выучить датский", category="goal")
    labels = [g["label"] for g in (await alice.get("/api/profile/facts")).json()["groups"]]
    assert labels == [l for l in ("О вас", "Цели", "Здоровье") if l in labels] and "Проекты" not in labels
    assert labels.index("Цели") < labels.index("Здоровье")


async def test_blank_facts_are_not_shown_and_principles_never_appear(alice):
    await alice.fact("work.empty_one", value="   ", category="other")
    await alice.fact("principle.sleep", value="Спать 8 часов", category="principle")
    shown = [f["value_text"] for facts in (await sections(alice)).values() for f in facts]
    assert "Спать 8 часов" not in shown and "   " not in shown
    assert all((v or "").strip() for v in shown)


async def test_the_profile_carries_the_same_groups_and_the_key_stays_only_as_the_edit_handle(alice):
    fact_id = await alice.fact("work.trading_bot", value="Торговый бот", category="other")
    body = (await alice.get("/api/profile")).json()
    projects = next(g for g in body["groups"] if g["label"] == "Проекты")
    assert projects["group"] == "projects" and projects["facts"][0]["value_text"] == "Торговый бот"
    assert {f["id"] for f in body["facts"]} >= {str(fact_id)}  # the flat list is still there, with group / title filled in
    flat = next(f for f in body["facts"] if f["id"] == str(fact_id))
    assert (flat["group"], flat["title"], flat["key"]) == ("projects", None, "work.trading_bot")
    edited = (await alice.put("/api/profile/facts/work.trading_bot", {"expected_fact_id": str(fact_id), "value_text": "Бот v2"})).json()
    assert edited["value_text"] == "Бот v2" and edited["group"] == "projects"  # editing by the key still works


async def test_facts_of_another_tenant_are_not_listed(alice, bob):
    await alice.fact("work.secret", value="Только у Алисы", category="other")
    shown = [f["value_text"] for facts in (await sections(bob)).values() for f in facts]
    assert "Только у Алисы" not in shown
    assert (await client_get_unsigned(alice)).status_code == 401


async def client_get_unsigned(actor):
    return await actor.http.get("/api/profile/facts")

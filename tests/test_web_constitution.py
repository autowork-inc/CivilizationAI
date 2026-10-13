"""Screen "Конституция": principles, the "overridden by a state" verdict from the SQL view, and the manual-edit RESET of overrides."""

from __future__ import annotations

import asyncio
import uuid

from tests.web_helpers import alice, bob, client  # noqa: F401


async def overrides_of(actor, fact_id) -> int:
    return (await actor.sql("SELECT count(*) FROM civ.emotional_overrides WHERE core_fact_id = :f", f=fact_id))[0][0]


async def sweets(actor, priority=50):
    return await actor.fact("principle.no_sweets", value="никаких сладостей", category="principle", priority=priority)


# ---------------------------------------------------------------- reading: the view's verdict is passed through, not recomputed


async def test_the_list_shows_who_overrides_a_principle_straight_from_the_sql_view(alice):
    fact_id = await sweets(alice, priority=50)
    sleep = await alice.fact("principle.sleep", value="спать 8 часов", category="principle", priority=90)
    state = await alice.state("влюблённость", priority=100)
    await alice.override(state, fact_id)
    await alice.override(state, sleep)  # linked, but the state (100) must beat the principle strictly: 100 > 90 -> overrides too
    await alice.fact("city", value="не принцип")

    principles = {p["key"]: p for p in (await alice.get("/api/principles")).json()}

    assert set(principles) == {"principle.no_sweets", "principle.sleep"}  # facts of other categories are not principles
    sw = principles["principle.no_sweets"]
    assert (sw["overridden"], sw["overridden_by"], sw["override_priority"], sw["override_mode"]) == (True, "влюблённость", 100, "suspend")
    assert sw["fact_id"] == str(fact_id) and sw["text"] == "никаких сладостей"
    assert principles["principle.sleep"]["overridden"] is True


async def test_a_state_that_is_weaker_expired_or_dismissed_overrides_nothing(alice):
    weak = await sweets(alice, priority=80)
    await alice.override(await alice.state("слабая", priority=80), weak)  # equal priority: not strictly higher
    strong = await alice.fact("principle.a", value="a", category="principle", priority=10)
    await alice.override(await alice.state("истекла", priority=90, hours=-0.5), strong)  # expired half an hour ago
    third = await alice.fact("principle.b", value="b", category="principle", priority=10)
    await alice.override(await alice.state("отклонена", priority=90, status="dismissed"), third)

    principles = (await alice.get("/api/principles")).json()

    assert [p["overridden"] for p in principles] == [False, False, False]
    assert all(p["overridden_by"] is None and p["override_priority"] is None for p in principles)


# ---------------------------------------------------------------- creating


async def test_a_new_principle_gets_a_safe_key_from_its_own_words(alice):
    response = await alice.post("/api/principles", {"text": "  Не есть   сладкое после шести  "})

    assert response.status_code == 201
    created = response.json()
    assert created["key"] == "principle.ne_est_sladkoe_posle_shesti" and created["text"] == "Не есть сладкое после шести"
    assert (created["priority"], created["overridden"]) == (70, False)
    row = (await alice.current_facts("principle.%"))[created["key"]]
    assert (row[1], row[3], row[4]) == ("Не есть сладкое после шести", "user_manual", "principle")


async def test_the_same_words_twice_make_two_principles_not_a_collision(alice):
    first = (await alice.post("/api/principles", {"text": "Спать 8 часов"})).json()
    second = (await alice.post("/api/principles", {"text": "Спать 8 часов", "priority": 90})).json()
    assert first["key"] == "principle.spat_8_chasov" and second["key"] == "principle.spat_8_chasov_2" and second["priority"] == 90


async def test_bad_new_principles_are_422(alice):
    for body in ({"text": "   "}, {"text": ""}, {}, {"text": "ok", "priority": 101}, {"text": "ok", "priority": -1}, {"text": "ok", "key": "x"}):
        assert (await alice.post("/api/principles", body)).status_code == 422, body
    assert await alice.current_facts("principle.%") == {}


async def test_a_very_long_principle_is_cut_to_the_same_bound_as_the_interview(alice):
    created = (await alice.post("/api/principles", {"text": "слово " * 100})).json()
    assert len(created["text"]) <= 300


# ---------------------------------------------------------------- editing resets the overrides


async def test_a_manual_edit_lifts_the_temporary_override_and_the_rule_is_back_at_full_priority(alice):
    fact_id = await sweets(alice, priority=50)
    state = await alice.state("влюблённость", priority=100)
    await alice.override(state, fact_id)
    assert (await alice.get("/api/principles")).json()[0]["overridden"] is True

    response = await alice.put("/api/principles/principle.no_sweets", {"expected_fact_id": str(fact_id), "text": "сладкое только по праздникам"})

    assert response.status_code == 200
    body = response.json()
    assert body["overrides_cleared"] == 1
    assert (body["principle"]["text"], body["principle"]["overridden"], body["principle"]["priority"]) == ("сладкое только по праздникам", False, 50)
    assert body["principle"]["fact_id"] != str(fact_id)
    assert await overrides_of(alice, fact_id) == 0  # the link rows are gone, not just hidden
    [listed] = (await alice.get("/api/principles")).json()
    assert listed["overridden"] is False and listed["text"] == "сладкое только по праздникам"
    # the state itself stays: it still colours the tone of voice, it only lost its power over this rule
    assert (await alice.sql("SELECT status::text FROM civ.emotional_states WHERE id = :s", s=state))[0][0] == "active"
    assert [(v[1], v[2], v[3]) for v in await alice.versions("principle.no_sweets")] == [
        ("никаких сладостей", True, "user_manual"),
        ("сладкое только по праздникам", False, "user_manual"),
    ]


async def test_changing_only_the_priority_also_resets_and_keeps_the_text(alice):
    fact_id = await sweets(alice, priority=50)
    await alice.override(await alice.state("усталость", priority=70), fact_id)

    body = (await alice.put("/api/principles/principle.no_sweets", {"expected_fact_id": str(fact_id), "priority": 95})).json()

    assert (body["principle"]["text"], body["principle"]["priority"], body["overrides_cleared"]) == ("никаких сладостей", 95, 1)


async def test_saving_unchanged_values_is_a_no_op_it_neither_versions_nor_resets(alice):
    fact_id = await sweets(alice, priority=50)
    await alice.override(await alice.state("усталость", priority=70), fact_id)

    body = (await alice.put("/api/principles/principle.no_sweets", {"expected_fact_id": str(fact_id), "text": "никаких сладостей", "priority": 50})).json()

    assert body["overrides_cleared"] == 0 and body["principle"]["fact_id"] == str(fact_id)
    assert len(await alice.versions("principle.no_sweets")) == 1 and await overrides_of(alice, fact_id) == 1


async def test_restore_lifts_overrides_without_touching_the_text_and_is_idempotent(alice):
    fact_id = await sweets(alice)
    first, second = await alice.state("раз", priority=90), await alice.state("два", priority=80)
    await alice.override(first, fact_id)
    await alice.override(second, fact_id)

    r1 = await alice.post("/api/principles/principle.no_sweets/restore", {"expected_fact_id": str(fact_id)})
    r2 = await alice.post("/api/principles/principle.no_sweets/restore", {"expected_fact_id": str(fact_id)})

    assert (r1.status_code, r1.json()) == (200, {"overrides_cleared": 2}) and r2.json() == {"overrides_cleared": 0}
    assert len(await alice.versions("principle.no_sweets")) == 1  # the same version: only the links went
    assert (await alice.get("/api/principles")).json()[0]["overridden"] is False


async def test_an_override_created_later_for_the_new_version_wins_again_by_the_same_rule(alice):
    fact_id = await sweets(alice, priority=50)
    new_id = (await alice.put("/api/principles/principle.no_sweets", {"expected_fact_id": str(fact_id), "text": "новое"})).json()["principle"]["fact_id"]
    await alice.override(await alice.state("праздник", priority=99), uuid.UUID(new_id))
    assert (await alice.get("/api/principles")).json()[0]["overridden_by"] == "праздник"


# ---------------------------------------------------------------- concurrency, deletion, validation


async def test_a_stale_principle_edit_is_409_and_parallel_edits_leave_one_current_version(alice):
    fact_id = await sweets(alice)

    async def edit(text):
        return await alice.put("/api/principles/principle.no_sweets", {"expected_fact_id": str(fact_id), "text": text})

    results = await asyncio.gather(edit("раз"), edit("два"), edit("три"))

    assert sorted(r.status_code for r in results) == [200, 409, 409]
    assert all(r.json()["error"]["code"] == "conflict" for r in results if r.status_code == 409)
    versions = await alice.versions("principle.no_sweets")
    assert len(versions) == 2 and sum(1 for v in versions if not v[2]) == 1


async def test_deleting_a_principle_closes_it(alice):
    fact_id = await sweets(alice)
    assert (await alice.delete("/api/principles/principle.no_sweets", params={"expected_fact_id": str(fact_id)})).status_code == 204
    assert (await alice.get("/api/principles")).json() == []
    assert (await alice.delete("/api/principles/principle.no_sweets", params={"expected_fact_id": str(fact_id)})).status_code == 404
    assert [(v[1], v[2]) for v in await alice.versions("principle.no_sweets")] == [("никаких сладостей", True)]


async def test_only_principle_keys_are_accepted_here(alice):
    fact_id = await alice.fact("city", value="x")
    for method, path in (("put", "/api/principles/city"), ("post", "/api/principles/city/restore")):
        response = await getattr(alice, method)(path, {"expected_fact_id": str(fact_id), "text": "x"})
        assert response.status_code == 422, path
    assert (await alice.delete("/api/principles/city", params={"expected_fact_id": str(fact_id)})).status_code == 422


async def test_bad_edits_are_422(alice):
    fact_id = await sweets(alice)
    for body in ({"expected_fact_id": str(fact_id)}, {"expected_fact_id": str(fact_id), "text": "  "}, {"expected_fact_id": str(fact_id), "priority": 101}):
        assert (await alice.put("/api/principles/principle.no_sweets", body)).status_code == 422, body
    assert len(await alice.versions("principle.no_sweets")) == 1


async def test_principles_of_two_tenants_never_mix(alice, bob):
    alice_fact = await sweets(alice)
    alice_state = await alice.state("секрет Алисы", priority=100)
    await alice.override(alice_state, alice_fact)
    bob_fact = await bob.fact("principle.no_sweets", value="правило Боба", category="principle")

    assert [p["text"] for p in (await bob.get("/api/principles")).json()] == ["правило Боба"]
    assert "секрет Алисы" not in (await bob.get("/api/principles")).text

    stolen = await bob.put("/api/principles/principle.no_sweets", {"expected_fact_id": str(alice_fact), "text": "hacked"})
    assert stolen.status_code == 409 and stolen.json()["error"]["current_fact_id"] == str(bob_fact)
    restore = await bob.post("/api/principles/principle.no_sweets/restore", {"expected_fact_id": str(alice_fact)})
    assert restore.status_code == 409
    assert await overrides_of(alice, alice_fact) == 1  # Alice's override survived Bob's attempts
    assert (await alice.current_facts("principle.%"))["principle.no_sweets"][1] == "никаких сладостей"

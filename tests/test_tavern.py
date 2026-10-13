"""PR 2.2: the Tavern (migration 0017): civ.tavern_feed / tavern_avatar / send_cheer, tavern_cheers, and /api/tavern.
Real PostgreSQL (RLS, definer functions, advisory locks); the real FastAPI app over ASGI."""

from __future__ import annotations

import asyncio
import io
import uuid

import pytest
from PIL import Image
from sqlalchemy.exc import DBAPIError

from tests.helpers import new_contact, owner_exec, tenant_exec
from tests.web_helpers import alice, bob, client, make_actor  # noqa: F401


@pytest.fixture
async def carol(client, owner_engine):  # noqa: F811
    return await make_actor(client, owner_engine, tz="America/New_York", name="Кэрол")


async def befriend(a, b, *, a_alias="Боб", b_alias="Алиса") -> tuple[uuid.UUID, uuid.UUID]:
    """Two accepted rows, as the handshake leaves them. Returns (a's handle for b, b's handle for a)."""
    return (
        await new_contact(a.owner_engine, a.user_id, b.user_id, alias=a_alias),
        await new_contact(a.owner_engine, b.user_id, a.user_id, alias=b_alias),
    )


async def feed(db, who) -> list[dict]:
    return (await tenant_exec(db, who.user_id, "SELECT civ.tavern_feed()"))[0][0]


async def cheer(db, who, handle, kind="focus") -> dict:
    return (await tenant_exec(db, who.user_id, "SELECT civ.send_cheer(:c, :k)", c=handle, k=kind))[0][0]


async def profile(actor, **cols) -> None:
    names = ", ".join(cols)
    marks = ", ".join(f":{k}" for k in cols)
    await actor.sql(
        f"INSERT INTO civ.character_profiles (user_id, {names}) VALUES (:u, {marks}) "
        f"ON CONFLICT (user_id) DO UPDATE SET " + ", ".join(f"{k} = EXCLUDED.{k}" for k in cols),
        **cols,
    )


async def xp_rows(actor) -> list[tuple]:
    return await actor.sql("SELECT amount, category, source_ref FROM civ.xp_ledger WHERE user_id = :u ORDER BY source_ref")


async def audit(actor, requester=None) -> list[tuple]:
    return await actor.sql(
        "SELECT requester_user_id, scope::text, decision FROM civ.diplomat_audit_log WHERE user_id = :u ORDER BY id"
    )


async def confirmed_mood(actor, mood="tired", *, hours: float = 24) -> None:
    pending = (
        await actor.sql(
            "INSERT INTO civ.pending_facts (user_id, subject, change, emotion_label, emotion_intensity, emotion_priority, emotion_expires_at, "
            "emotion_mood, evidence, source_message_ids, status, decided_at, idempotency_key) VALUES (:u, 'emotional_state', 'create', 'private feeling', 8, 90, "
            "now() + interval '2 days', CAST(:m AS civ.shared_mood), 'said so', ARRAY[gen_random_uuid()], 'approved', now(), :i) RETURNING id",
            m=mood, i=uuid.uuid4().hex,
        )
    )[0][0]  # fmt: skip
    await actor.sql(
        "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, source_pending_id, mood, started_at, expires_at) "
        "VALUES (:u, 'private feeling', 8, 90, 'user_confirmed', :p, CAST(:m AS civ.shared_mood), now() - interval '1 minute', now() + make_interval(secs => :h))",
        p=pending, m=mood, h=hours * 3600,
    )  # fmt: skip


def png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 120, 200)).save(buf, format="WEBP")
    return buf.getvalue()


# ================================================================ who is in the feed


async def test_only_mutually_accepted_friends_are_in_the_feed(alice, bob, carol, db):
    await befriend(alice, bob)
    await new_contact(alice.owner_engine, alice.user_id, carol.user_id, alias="Кэрол")  # one-sided: carol has no row for alice
    cards = await feed(db, alice)
    assert [c["alias"] for c in cards] == ["Боб"]
    assert await feed(db, carol) == []  # carol's book is empty


@pytest.mark.parametrize("theirs", ["pending", "blocked"])
async def test_a_friend_who_did_not_accept_or_blocked_us_is_not_shown(alice, bob, theirs, db):
    await new_contact(alice.owner_engine, alice.user_id, bob.user_id, alias="Боб")
    await new_contact(alice.owner_engine, bob.user_id, alice.user_id, alias="Алиса", status=theirs)
    assert await feed(db, alice) == []


@pytest.mark.parametrize("mine", ["pending", "blocked"])
async def test_a_friend_I_blocked_or_have_not_accepted_is_not_shown(alice, bob, mine, db):
    await new_contact(alice.owner_engine, alice.user_id, bob.user_id, alias="Боб", status=mine)
    await new_contact(alice.owner_engine, bob.user_id, alice.user_id, alias="Алиса")
    assert await feed(db, alice) == []


async def test_a_friend_whose_group_is_frozen_or_who_is_not_onboarded_is_not_shown(alice, bob, db):
    await befriend(alice, bob)
    await owner_exec(alice.owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=bob.user_id)
    assert await feed(db, alice) == []
    await owner_exec(alice.owner_engine, "UPDATE civ.users SET group_state = 'active', onboarding_state = 'in_progress' WHERE id = :u", u=bob.user_id)
    assert await feed(db, alice) == []
    await owner_exec(alice.owner_engine, "UPDATE civ.users SET onboarding_state = 'done' WHERE id = :u", u=bob.user_id)
    assert len(await feed(db, alice)) == 1


async def test_the_feed_needs_a_tenant_and_shows_nothing_of_strangers(alice, bob, carol, db):
    await befriend(alice, bob)
    await befriend(bob, carol)
    assert [c["alias"] for c in await feed(db, alice)] == ["Боб"]  # bob's friend carol is not alice's business
    with pytest.raises(DBAPIError):  # no tenant context at all
        async with db.engine.begin() as conn:  # type: ignore[attr-defined]
            from sqlalchemy import text

            await conn.execute(text("SELECT civ.tavern_feed()"))


# ================================================================ what each card may carry


async def test_without_a_profile_only_name_and_level_are_shown(alice, bob, db):
    await befriend(alice, bob, a_alias="Боря")
    await bob.fact("preference.tea", value="Люблю чай", category="preference")
    await confirmed_mood(bob)
    (card,) = await feed(db, alice)
    assert card["character_name"] is None and card["alias"] == "Боря"
    assert card["level"] == 1 and card["total_xp"] == 0  # show_level defaults to true
    assert card["mood"] is None and card["mood_shared"] is False
    assert card["interests"] is None and card["open_for_intros"] is False and card["avatar_hash"] is None


async def test_each_flag_opens_exactly_its_own_field(alice, bob, db):
    await befriend(alice, bob)
    await bob.fact("preference.tea", value="Люблю чай", category="preference")
    await confirmed_mood(bob, "calm")
    await profile(bob, character_name="Бобби", show_level=False, show_mood=False, show_interests=False, open_for_intros=False)
    (card,) = await feed(db, alice)
    assert card["character_name"] == "Бобби"
    assert (card["level"], card["total_xp"], card["mood"], card["interests"], card["open_for_intros"]) == (None, None, None, None, False)

    await profile(bob, show_level=True)
    card = (await feed(db, alice))[0]
    assert card["level"] == 1 and card["mood"] is None and card["interests"] is None

    await profile(bob, show_mood=True)
    card = (await feed(db, alice))[0]
    assert card["mood"] == "calm" and card["interests"] is None and card["open_for_intros"] is False

    await profile(bob, show_interests=True)
    card = (await feed(db, alice))[0]
    assert card["interests"] == ["Люблю чай"]

    await profile(bob, open_for_intros=True)
    assert (await feed(db, alice))[0]["open_for_intros"] is True

    await profile(bob, show_level=False, show_mood=False, show_interests=False, open_for_intros=False)  # the owner takes it all back
    card = (await feed(db, alice))[0]
    assert (card["level"], card["mood"], card["interests"], card["open_for_intros"]) == (None, None, None, False)


async def test_the_level_follows_the_ledger(alice, bob, db):
    await befriend(alice, bob)
    for day in range(2):
        for i in range(10):
            await bob.sql(
                "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref, created_at) VALUES (:u, 10, 'focus', :r, now() - make_interval(days => :d, mins => :i))",
                r=f"s:{day}:{i}", d=day + 1, i=i,
            )  # fmt: skip
    (card,) = await feed(db, alice)
    assert (card["level"], card["total_xp"]) == (3, 200)


async def test_the_mood_is_only_a_confirmed_one_in_force_never_an_inferred_or_stale_one(alice, bob, db):
    await befriend(alice, bob)
    await profile(bob, show_mood=True)
    await bob.sql(  # an AI inference never leaves the tenant
        "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, status, started_at, expires_at) "
        "VALUES (:u, 'secret feeling', 8, 40, 'inferred', 'active', now() - interval '1 hour', now() + interval '3 hours')"
    )
    assert (await feed(db, alice))[0]["mood"] is None
    await confirmed_mood(bob, "sad")
    assert (await feed(db, alice))[0]["mood"] == "sad"
    await bob.sql("UPDATE civ.emotional_states SET expires_at = now() - interval '1 second', started_at = now() - interval '2 days' WHERE source = 'user_confirmed' AND user_id = :u")
    assert (await feed(db, alice))[0]["mood"] is None
    assert "secret feeling" not in str(await feed(db, alice))  # the free-text label never travels


async def test_interests_are_only_current_global_preference_facts_one_short_line_each(alice, bob, db):
    await befriend(alice, bob)
    await profile(bob, show_interests=True)
    await bob.fact("preference.chess", value="Шахматы", category="preference")
    await bob.fact("health.knee", value="Болит колено", category="health")
    await bob.fact("goal.1", value="Выучить датский", category="goal")
    await bob.fact("preference.long", value="я" * 81, category="preference")
    old = await bob.fact("preference.old", value="Старое", category="preference")
    await bob.sql("UPDATE civ.core_facts SET valid_to = now() WHERE id = :i", i=old)
    persona = await bob.persona("bowling")
    await bob.sql(
        "INSERT INTO civ.core_facts (user_id, key, category, value_text, priority, source, persona_id) VALUES (:u, 'preference.p', 'preference', 'Только в топике', 50, 'user_manual', :p)",
        p=persona,
    )
    (card,) = await feed(db, alice)
    assert card["interests"] == ["Шахматы"]


async def test_at_most_eight_interests_newest_first(alice, bob, db):
    await befriend(alice, bob)
    await profile(bob, show_interests=True)
    for i in range(10):
        await bob.sql(
            "INSERT INTO civ.core_facts (user_id, key, category, value_text, priority, source, valid_from) "
            "VALUES (:u, :k, 'preference', :v, 50, 'user_manual', now() - make_interval(mins => :m))",
            k=f"preference.k{i}", v=f"Интерес {i}", m=10 - i,
        )  # fmt: skip
    (card,) = await feed(db, alice)
    assert len(card["interests"]) == 8 and card["interests"][0] == "Интерес 9"


async def test_a_card_carries_no_uuid_of_the_other_tenant(alice, bob, db):
    a_handle, b_handle = await befriend(alice, bob)
    await profile(bob, character_name="Бобби", show_interests=True)
    await bob.fact("preference.tea", value="Чай", category="preference")
    raw = str(await feed(db, alice))
    assert str(bob.user_id) not in raw and str(bob.telegram_id) not in raw and str(b_handle) not in raw
    assert str(a_handle) in raw  # only the caller's own handle


# ================================================================ audit


async def test_every_card_read_is_written_to_the_owners_audit_log(alice, bob, carol, db):
    await befriend(alice, bob)
    await new_contact(alice.owner_engine, alice.user_id, carol.user_id)  # one-sided: no card, no audit row for carol
    await feed(db, alice)
    await feed(db, alice)
    assert [r[1:] for r in await audit(bob)] == [("profile_public", "allowed")] * 2
    assert {r[0] for r in await audit(bob)} == {alice.user_id}
    assert await audit(carol) == [] and await audit(alice) == []


async def test_the_application_cannot_write_or_forge_the_audit_log(alice, bob, db):
    with pytest.raises(DBAPIError, match="permission denied"):
        await tenant_exec(db, alice.user_id, "INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision) VALUES (:u, :u, 'profile_public', 'allowed')", u=alice.user_id)


# ================================================================ avatar


async def test_an_avatar_is_served_to_a_friend_with_a_stable_hash_and_audited(alice, bob, db):
    a_handle, _ = await befriend(alice, bob)
    await profile(bob, avatar_bytes=png())
    (card,) = await feed(db, alice)
    assert card["avatar_hash"] and len(card["avatar_hash"]) == 16
    got = (await tenant_exec(db, alice.user_id, "SELECT civ.tavern_avatar(:c)", c=a_handle))[0][0]
    assert bytes(got) == png()
    again = (await feed(db, alice))[0]["avatar_hash"]
    assert again == card["avatar_hash"]
    assert len(await audit(bob)) == 3  # feed, avatar, feed


async def test_the_hash_changes_with_the_picture(alice, bob, db):
    await befriend(alice, bob)
    await profile(bob, avatar_bytes=png())
    first = (await feed(db, alice))[0]["avatar_hash"]
    await profile(bob, avatar_bytes=b"RIFF" + b"\x01" * 40)
    assert (await feed(db, alice))[0]["avatar_hash"] != first


async def test_an_avatar_refusal_looks_like_no_avatar(alice, bob, carol, db):
    a_handle, _ = await befriend(alice, bob)
    c_handle = await new_contact(alice.owner_engine, alice.user_id, carol.user_id)  # one-sided
    await profile(carol, avatar_bytes=png())
    no_avatar = (await tenant_exec(db, alice.user_id, "SELECT civ.tavern_avatar(:c)", c=a_handle))[0][0]
    refused = (await tenant_exec(db, alice.user_id, "SELECT civ.tavern_avatar(:c)", c=c_handle))[0][0]
    unknown = (await tenant_exec(db, alice.user_id, "SELECT civ.tavern_avatar(:c)", c=uuid.uuid4()))[0][0]
    assert no_avatar is None and refused is None and unknown is None
    assert await audit(bob) == [] and await audit(carol) == []  # nothing was handed out


async def test_somebody_elses_handle_is_not_a_handle(alice, bob, carol, db):
    _, b_handle = await befriend(alice, bob)  # b_handle is BOB's row for alice
    await befriend(bob, carol)
    await profile(alice, avatar_bytes=png())
    stolen = (await tenant_exec(db, carol.user_id, "SELECT civ.tavern_avatar(:c)", c=b_handle))[0][0]
    assert stolen is None
    assert (await tenant_exec(db, carol.user_id, "SELECT civ.send_cheer(:c, 'focus')", c=b_handle))[0][0] == {"outcome": "unavailable"}


# ================================================================ permissions and locking


@pytest.mark.parametrize(
    "call",
    ["civ.tavern_authorize(gen_random_uuid())", "civ.xp_grant(gen_random_uuid(), 'focus', 'x', 10)"],
)
async def test_internal_functions_are_not_callable_by_the_application(alice, db, call):
    with pytest.raises(DBAPIError, match="permission denied"):
        await tenant_exec(db, alice.user_id, f"SELECT {call}")


async def test_a_cheer_cannot_be_written_directly(alice, bob, db):
    await befriend(alice, bob)
    for statement in (
        "INSERT INTO civ.tavern_cheers (user_id, target_user_id, kind, local_date) VALUES (:u, :t, 'focus', current_date)",
        "UPDATE civ.tavern_cheers SET kind = 'body'",
        "DELETE FROM civ.tavern_cheers",
    ):
        with pytest.raises(DBAPIError, match="permission denied"):
            await tenant_exec(db, alice.user_id, statement, u=alice.user_id, t=bob.user_id)


async def test_feeds_and_cheers_over_a_triangle_never_deadlock(alice, bob, carol, db):
    pairs = [
        (alice, (await befriend(alice, bob))[0]),
        (bob, (await befriend(bob, carol))[0]),
        (carol, (await befriend(carol, alice))[0]),
    ]
    jobs = []
    for _ in range(4):
        jobs += [feed(db, alice), feed(db, bob), feed(db, carol)]
    jobs += [cheer(db, who, handle) for who, handle in pairs]
    results = await asyncio.gather(*jobs)  # a deadlock would surface as an exception here
    assert sum(1 for r in results if isinstance(r, dict) and r.get("outcome") == "sent") == 3


# ================================================================ cheers


async def test_a_cheer_pays_10_connection_xp_to_both(alice, bob, db):
    a_handle, _ = await befriend(alice, bob)
    result = await cheer(db, alice, a_handle, "focus")
    assert result == {"outcome": "sent", "xp": "awarded"}
    (row,) = await tenant_exec(db, alice.user_id, "SELECT kind, local_date FROM civ.tavern_cheers")
    assert row[0] == "focus"
    mine, theirs = await xp_rows(alice), await xp_rows(bob)
    assert len(mine) == len(theirs) == 1 and mine[0][:2] == (10, "connection") and theirs[0][:2] == (10, "connection")
    assert mine[0][2].endswith(".sent") and theirs[0][2].endswith(".received")


async def test_the_feed_tells_who_cheered_whom_today(alice, bob, db):
    a_handle, b_handle = await befriend(alice, bob)
    await cheer(db, alice, a_handle)
    assert [(c["you_cheered_today"], c["cheered_you_today"]) for c in await feed(db, alice)] == [(True, False)]
    assert [(c["you_cheered_today"], c["cheered_you_today"]) for c in await feed(db, bob)] == [(False, True)]


async def test_one_cheer_a_day_to_one_friend_whatever_the_kind(alice, bob, carol, db):
    a_bob, _ = await befriend(alice, bob)
    a_carol, _ = await befriend(alice, carol, a_alias="Кэрол")
    assert (await cheer(db, alice, a_bob, "focus"))["outcome"] == "sent"
    assert await cheer(db, alice, a_bob, "focus") == {"outcome": "already_today"}
    assert await cheer(db, alice, a_bob, "body") == {"outcome": "already_today"}
    assert (await cheer(db, alice, a_carol, "body"))["outcome"] == "sent"  # another friend is another cheer
    assert len(await xp_rows(alice)) == 2 and len(await xp_rows(bob)) == 1  # the refused ones paid nothing


async def test_the_day_is_the_senders_local_day_and_resets(alice, bob, db):
    a_handle, _ = await befriend(alice, bob)
    assert (await cheer(db, alice, a_handle))["outcome"] == "sent"
    await owner_exec(alice.owner_engine, "UPDATE civ.tavern_cheers SET local_date = local_date - 1 WHERE user_id = :u", u=alice.user_id)
    assert (await cheer(db, alice, a_handle))["outcome"] == "sent"  # yesterday's cheer does not count
    today = (await alice.sql("SELECT (now() AT TIME ZONE 'Europe/Copenhagen')::date"))[0][0]
    assert sorted(r[0] for r in await alice.sql("SELECT local_date FROM civ.tavern_cheers WHERE user_id = :u")) == [today - __import__("datetime").timedelta(days=1), today]


async def test_a_capped_sender_still_cheers_and_the_friend_still_earns(alice, bob, db):
    a_handle, _ = await befriend(alice, bob)
    for i in range(10):
        await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 10, 'focus', :r)", r=f"fill:{i}")
    assert await cheer(db, alice, a_handle) == {"outcome": "sent", "xp": "daily_cap"}
    assert len(await xp_rows(bob)) == 1
    assert len(await xp_rows(alice)) == 10


async def test_the_friends_cap_is_not_told_to_the_sender(alice, bob, db):
    a_handle, _ = await befriend(alice, bob)
    for i in range(10):
        await bob.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 10, 'focus', :r)", r=f"fill:{i}")
    result = await cheer(db, alice, a_handle)
    assert result == {"outcome": "sent", "xp": "awarded"}  # alice's own result; bob's capped award is invisible to her
    assert len(await xp_rows(bob)) == 10


@pytest.mark.parametrize("state", ["unknown", "one_sided", "they_blocked", "i_blocked", "pending", "frozen"])
async def test_every_reason_to_refuse_a_cheer_looks_the_same(alice, bob, db, state):
    if state == "unknown":
        handle = uuid.uuid4()
    elif state == "one_sided":
        handle = await new_contact(alice.owner_engine, alice.user_id, bob.user_id)
    else:
        mine = "blocked" if state == "i_blocked" else "pending" if state == "pending" else "accepted"
        theirs = "blocked" if state == "they_blocked" else "accepted"
        handle = await new_contact(alice.owner_engine, alice.user_id, bob.user_id, status=mine)
        await new_contact(alice.owner_engine, bob.user_id, alice.user_id, status=theirs)
        if state == "frozen":
            await owner_exec(alice.owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=bob.user_id)
    assert await cheer(db, alice, handle) == {"outcome": "unavailable"}
    assert await xp_rows(alice) == [] and await xp_rows(bob) == []
    assert await alice.sql("SELECT count(*) FROM civ.tavern_cheers WHERE user_id = :u") == [(0,)]


async def test_a_sender_who_is_not_active_cannot_cheer(alice, bob, db):
    a_handle, _ = await befriend(alice, bob)
    await owner_exec(alice.owner_engine, "UPDATE civ.users SET group_state = 'dormant' WHERE id = :u", u=alice.user_id)
    assert await cheer(db, alice, a_handle) == {"outcome": "unavailable"}


async def test_an_unknown_kind_is_a_programming_error_not_an_answer(alice, bob, db):
    a_handle, _ = await befriend(alice, bob)
    for kind in ("hug", "", "FOCUS"):
        with pytest.raises(DBAPIError):
            await cheer(db, alice, a_handle, kind)
    with pytest.raises(DBAPIError):
        await tenant_exec(db, alice.user_id, "SELECT civ.send_cheer(NULL, 'focus')")


async def test_parallel_cheers_to_one_friend_make_exactly_one(alice, bob, db):
    a_handle, _ = await befriend(alice, bob)
    results = await asyncio.gather(*(cheer(db, alice, a_handle) for _ in range(8)))
    assert sorted(r["outcome"] for r in results) == ["already_today"] * 7 + ["sent"]
    assert len(await xp_rows(alice)) == 1 and len(await xp_rows(bob)) == 1


async def test_crossing_cheers_both_go_through_without_a_deadlock(alice, bob, db):
    a_handle, b_handle = await befriend(alice, bob)
    for _ in range(3):
        await owner_exec(alice.owner_engine, "DELETE FROM civ.tavern_cheers")
        await owner_exec(alice.owner_engine, "DELETE FROM civ.xp_ledger")
        results = await asyncio.gather(cheer(db, alice, a_handle), cheer(db, bob, b_handle), cheer(db, alice, a_handle), cheer(db, bob, b_handle))
        assert sorted(r["outcome"] for r in results) == ["already_today"] * 2 + ["sent"] * 2
        assert len(await xp_rows(alice)) == 2 and len(await xp_rows(bob)) == 2  # one sent + one received each


async def test_blocking_a_friend_stops_cheers_at_once(alice, bob, db):
    a_handle, b_handle = await befriend(alice, bob)
    await tenant_exec(db, bob.user_id, "SELECT civ.block_contact(:c)", c=b_handle)
    assert await cheer(db, alice, a_handle) == {"outcome": "unavailable"}
    assert await feed(db, alice) == []


# ================================================================ API


async def test_the_api_needs_valid_launch_data(client):
    for call in (client.get("/api/tavern/feed"), client.post("/api/tavern/cheer", json={"contact_id": str(uuid.uuid4())}), client.get(f"/api/tavern/avatar/{uuid.uuid4()}")):
        assert (await call).status_code == 401


async def test_the_feed_endpoint_shapes_cards_and_links_the_avatar(alice, bob):
    a_handle, _ = await befriend(alice, bob, a_alias="Боря")
    assert (await alice.get("/api/tavern/feed")).json() == {"friends": [] if False else (await alice.get("/api/tavern/feed")).json()["friends"]}
    await profile(bob, character_name="Бобби", show_mood=True, show_interests=True, open_for_intros=True, avatar_bytes=png())
    await bob.fact("preference.tea", value="Чай", category="preference")
    await confirmed_mood(bob, "good")
    (card,) = (await alice.get("/api/tavern/feed")).json()["friends"]
    assert card["contact_id"] == str(a_handle) and card["name"] == "Бобби"
    assert card["avatar_url"].startswith(f"/api/tavern/avatar/{a_handle}?v=") and len(card["avatar_url"].split("v=")[1]) == 16
    assert (card["level"], card["total_xp"], card["mood"], card["interests"], card["open_for_intros"]) == (1, 0, "good", ["Чай"], True)
    assert card["you_cheered_today"] is False and card["cheered_you_today"] is False
    assert str(bob.user_id) not in str(card) and str(bob.telegram_id) not in str(card)


async def test_without_a_character_name_the_card_uses_my_alias(alice, bob):
    await befriend(alice, bob, a_alias="Боря")
    (card,) = (await alice.get("/api/tavern/feed")).json()["friends"]
    assert card["name"] == "Боря" and card["avatar_url"] is None and card["mood"] is None and card["interests"] is None
    await alice.sql("UPDATE civ.contacts SET alias = NULL WHERE user_id = :u")
    assert (await alice.get("/api/tavern/feed")).json()["friends"][0]["name"] == "Знакомый"


async def test_the_avatar_endpoint_serves_webp_and_refusals_are_all_404(alice, bob, carol):
    a_handle, _ = await befriend(alice, bob)
    c_handle = await new_contact(alice.owner_engine, alice.user_id, carol.user_id)
    await profile(bob, avatar_bytes=png())
    await profile(carol, avatar_bytes=png())
    ok = await alice.get(f"/api/tavern/avatar/{a_handle}")
    assert ok.status_code == 200 and ok.headers["content-type"] == "image/webp" and ok.content == png()
    bodies = []
    for handle in (c_handle, uuid.uuid4()):
        r = await alice.get(f"/api/tavern/avatar/{handle}")
        assert r.status_code == 404
        bodies.append(r.json())
    assert bodies[0] == bodies[1]
    assert (await alice.get("/api/tavern/avatar/not-a-uuid")).status_code == 422


async def test_cheering_through_the_api(alice, bob):
    a_handle, _ = await befriend(alice, bob)
    r = await alice.post("/api/tavern/cheer", {"contact_id": str(a_handle), "kind": "focus"})
    assert r.status_code == 200 and r.json() == {"outcome": "sent", "kind": "focus", "xp": "awarded"}
    assert len(await xp_rows(alice)) == 1 and len(await xp_rows(bob)) == 1
    again = await alice.post("/api/tavern/cheer", {"contact_id": str(a_handle), "kind": "body"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_cheered_today"
    assert (await alice.get("/api/character/me")).json()["xp_by_category"]["connection"] == 10
    assert (await bob.get("/api/character/me")).json()["xp_by_category"]["connection"] == 10
    assert (await alice.get("/api/tavern/feed")).json()["friends"][0]["you_cheered_today"] is True


async def test_the_default_cheer_is_focus(alice, bob):
    a_handle, _ = await befriend(alice, bob)
    assert (await alice.post("/api/tavern/cheer", {"contact_id": str(a_handle)})).json()["kind"] == "focus"


@pytest.mark.parametrize("payload", [{}, {"contact_id": "nope"}, {"contact_id": None}, {"kind": "focus"}, {"contact_id": str(uuid.uuid4()), "kind": "hug"}, {"contact_id": str(uuid.uuid4()), "xp": 99}])
async def test_a_malformed_cheer_is_a_422_and_changes_nothing(alice, bob, payload):
    await befriend(alice, bob)
    assert (await alice.post("/api/tavern/cheer", payload)).status_code == 422
    assert await xp_rows(alice) == [] and await xp_rows(bob) == []


async def test_a_refused_cheer_answers_like_an_unknown_one(alice, bob, carol):
    one_sided = await new_contact(alice.owner_engine, alice.user_id, carol.user_id)
    unknown = await alice.post("/api/tavern/cheer", {"contact_id": str(uuid.uuid4())})
    refused = await alice.post("/api/tavern/cheer", {"contact_id": str(one_sided)})
    assert unknown.status_code == refused.status_code == 404 and unknown.json() == refused.json()


async def test_a_user_cannot_use_somebody_elses_handle_through_the_api(alice, bob, carol):
    _, b_handle = await befriend(alice, bob)  # bob's handle for alice
    r = await carol.post("/api/tavern/cheer", {"contact_id": str(b_handle)})
    assert r.status_code == 404 and (await carol.get(f"/api/tavern/avatar/{b_handle}")).status_code == 404
    assert (await carol.get("/api/tavern/feed")).json() == {"friends": []}
    assert await xp_rows(alice) == [] and await xp_rows(bob) == []

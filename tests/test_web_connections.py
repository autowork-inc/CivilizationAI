"""Screen "Связи" (Module 5, step 3): contacts, invite links, grants and the journal of who asked. Real PostgreSQL + RLS and the real
FastAPI app; the handshake itself runs through the real ``civ.accept_invite``."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from ai_civ.bot import invites
from tests.helpers import new_contact, new_user
from tests.web_helpers import alice, bob, client, make_actor  # noqa: F401

FREE = "calendar_free_busy"
MOOD = "emotional_state_label"


async def link_up(alice, bob, *, alice_calls="Боб", bob_calls="Алиса", status="accepted"):
    """Two connected people as the handshake leaves them (each side's own row). Returns (alice's contact id, bob's contact id)."""
    mine = await new_contact(alice.owner_engine, alice.user_id, bob.user_id, alias=alice_calls, status=status)
    theirs = await new_contact(alice.owner_engine, bob.user_id, alice.user_id, alias=bob_calls, status=status)
    return mine, theirs


async def active_grants(actor, to=None) -> list[str]:
    rows = await actor.sql(
        "SELECT scope::text FROM civ.access_grants WHERE user_id = :u AND revoked_at IS NULL "
        "AND (CAST(:g AS uuid) IS NULL OR grantee_user_id = :g) ORDER BY 1",
        g=to,
    )
    return [r[0] for r in rows]


def contact_of(body, contact_id):
    return next(c for c in body["contacts"] if c["id"] == str(contact_id))


# ---------------------------------------------------------------- reading


async def test_a_new_user_sees_empty_lists_and_the_offered_scopes(alice):
    body = (await alice.get("/api/connections")).json()

    assert body["contacts"] == [] and body["invites"] == []
    assert body["limits"] == {"max_open_invites": 5, "invite_ttl_hours": 72}
    assert [s["key"] for s in body["scopes"]] == [FREE, MOOD]  # only these two are offered, in this order
    assert all(s["title"] and s["description"] for s in body["scopes"])
    audit = (await alice.get("/api/connections/audit")).json()
    assert audit == {"entries": [], "next_before": None}


async def test_the_list_shows_my_contacts_only_with_blocked_ones_last_and_never_exposes_other_peoples_ids(alice, bob):
    carol = await make_actor(alice.http, alice.owner_engine, name="Кэрол")
    await link_up(alice, bob, alice_calls="Боб")
    await new_contact(alice.owner_engine, alice.user_id, carol.user_id, alias="Аня", status="blocked")
    await new_contact(alice.owner_engine, carol.user_id, alice.user_id, alias="Алиса")

    mine = (await alice.get("/api/connections")).json()
    assert [(c["alias"], c["status"]) for c in mine["contacts"]] == [("Боб", "accepted"), ("Аня", "blocked")]
    assert all(set(c) == {"id", "alias", "status", "connected_since", "grants"} for c in mine["contacts"])  # no tenant / telegram ids
    everything = json.dumps(mine) + (await alice.get("/api/connections/audit")).text
    for secret in (alice.user_id, bob.user_id, carol.user_id, alice.telegram_id, bob.telegram_id):
        assert str(secret) not in everything

    theirs = (await bob.get("/api/connections")).json()
    assert [(c["alias"], c["status"]) for c in theirs["contacts"]] == [("Алиса", "accepted")]  # Bob sees his own address book only


async def test_what_i_allowed_is_shown_to_me_and_not_to_the_other_person(alice, bob):
    mine, theirs = await link_up(alice, bob)
    await alice.put(f"/api/connections/contacts/{mine}/grants/{FREE}", {"enabled": True})

    seen_by_alice = contact_of((await alice.get("/api/connections")).json(), mine)
    seen_by_bob = contact_of((await bob.get("/api/connections")).json(), theirs)
    assert seen_by_alice["grants"] == {FREE: True, MOOD: False}
    assert seen_by_bob["grants"] == {FREE: False, MOOD: False}  # Alice's permission is not Bob's own switch


async def test_every_route_needs_signed_launch_data(client, alice):
    contact = await new_contact(alice.owner_engine, alice.user_id, (await new_user(alice.owner_engine)))
    routes = [
        ("GET", "/api/connections"), ("GET", "/api/connections/audit"), ("POST", "/api/connections/invites"),
        ("DELETE", "/api/connections/invites"), ("DELETE", f"/api/connections/invites/{contact}"),
        ("PUT", f"/api/connections/contacts/{contact}/alias"), ("POST", f"/api/connections/contacts/{contact}/block"),
        ("POST", f"/api/connections/contacts/{contact}/unblock"), ("DELETE", f"/api/connections/contacts/{contact}"),
        ("PUT", f"/api/connections/contacts/{contact}/grants/{FREE}"),
    ]  # fmt: skip
    for method, path in routes:
        response = await client.request(method, path, json={} if method in ("PUT", "POST") else None)
        assert response.status_code == 401, (method, path)


# ---------------------------------------------------------------- invites


async def test_an_invite_comes_back_as_a_link_shown_once_and_a_share_url(alice, bob, db):
    response = await alice.post("/api/connections/invites")
    assert response.status_code == 200
    body = response.json()

    assert body["link"].startswith("https://t.me/PerfectPerson_bot?start=connect_")
    token = body["link"].rsplit("connect_", 1)[1]
    share = urlparse(body["share_url"])
    assert (share.scheme, share.netloc, share.path) == ("https", "t.me", "/share/url")
    assert parse_qs(share.query)["url"] == [body["link"]]  # the link survives URL-encoding intact
    assert parse_qs(share.query)["text"][0]  # and there is a message to go with it

    listed = body["connections"]["invites"]
    assert len(listed) == 1 and set(listed[0]) == {"id", "created_at", "expires_at"}
    assert token not in (await alice.get("/api/connections")).text  # the link is never shown again: only its hash is stored
    [stored] = await alice.sql("SELECT display_name, token_hash = :h FROM civ.contact_invites WHERE user_id = :u", h=invites.hash_token(token))
    assert stored == ("Алиса", True)  # the name Bob will see comes from the Core Profile, not from the page

    async with db.tenant_session(bob.user_id) as session:  # and the link really is a working handshake
        peeked = await invites.peek_invite(session, invites.hash_token(token))
    assert peeked.state is invites.InviteState.ok and peeked.inviter_name == "Алиса"


async def test_a_handshake_makes_the_new_contact_appear_and_the_invite_leave_the_list(alice, bob, db):
    body = (await alice.post("/api/connections/invites")).json()
    token = body["link"].rsplit("connect_", 1)[1]
    async with db.tenant_session(bob.user_id) as session:
        peeked = await invites.peek_invite(session, invites.hash_token(token))
        await invites.accept_invite(session, peeked.invite_id, "Боб")

    after = (await alice.get("/api/connections")).json()
    assert after["invites"] == []  # used
    assert [(c["alias"], c["status"], c["grants"]) for c in after["contacts"]] == [("Боб", "accepted", {FREE: False, MOOD: False})]


async def test_at_most_five_open_links_and_revoking_one_makes_room(alice):
    ids = []
    for _ in range(5):
        ids.append((await alice.post("/api/connections/invites")).json()["connections"]["invites"][0]["id"])
    refused = await alice.post("/api/connections/invites")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "too_many_invites"
    assert "5" in refused.json()["error"]["message"]

    revoked = await alice.delete(f"/api/connections/invites/{ids[0]}")
    assert revoked.status_code == 200 and len(revoked.json()["invites"]) == 4
    assert (await alice.post("/api/connections/invites")).status_code == 200


async def test_revoking_all_links_and_revoking_a_dead_one(alice):
    first = (await alice.post("/api/connections/invites")).json()["connections"]["invites"][0]["id"]
    await alice.post("/api/connections/invites")

    assert (await alice.delete("/api/connections/invites")).json()["invites"] == []
    assert (await alice.delete("/api/connections/invites")).status_code == 200  # nothing to revoke is fine
    gone = await alice.delete(f"/api/connections/invites/{first}")
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "not_found"


async def test_somebody_elses_link_cannot_be_revoked(alice, bob):
    mine = (await alice.post("/api/connections/invites")).json()["connections"]["invites"][0]["id"]
    assert (await bob.delete(f"/api/connections/invites/{mine}")).status_code == 404
    assert (await bob.delete("/api/connections/invites")).status_code == 200  # revoke-all only touches Bob's own (none)
    assert len((await alice.get("/api/connections")).json()["invites"]) == 1


async def test_a_group_that_is_not_active_cannot_create_links_but_can_still_manage_contacts(alice, bob):
    mine, _ = await link_up(alice, bob)
    await alice.sql("UPDATE civ.users SET group_state = 'frozen' WHERE id = :u")

    refused = await alice.post("/api/connections/invites")
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_ready"
    assert (await alice.post(f"/api/connections/contacts/{mine}/block")).status_code == 200  # protecting yourself is always possible


# ---------------------------------------------------------------- aliases


async def test_renaming_a_contact_changes_only_my_own_address_book(alice, bob):
    mine, theirs = await link_up(alice, bob)

    response = await alice.put(f"/api/connections/contacts/{mine}/alias", {"alias": "  Любимый   брат "})

    assert response.status_code == 200
    assert contact_of(response.json(), mine)["alias"] == "Любимый брат"  # whitespace tidied
    assert contact_of((await bob.get("/api/connections")).json(), theirs)["alias"] == "Алиса"


@pytest.mark.parametrize("alias", ["", "   ", "x" * 41, "Бо‮б", "Бо​б", "Бо\x07б"])
async def test_a_bad_alias_is_refused_in_russian_and_changes_nothing(alice, bob, alias):
    mine, _ = await link_up(alice, bob)
    response = await alice.put(f"/api/connections/contacts/{mine}/alias", {"alias": alias})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_request"
    assert any(word in response.json()["error"]["message"] for word in ("имя", "имени"))
    assert contact_of((await alice.get("/api/connections")).json(), mine)["alias"] == "Боб"


async def test_alias_requests_are_strict(alice, bob):
    mine, _ = await link_up(alice, bob)
    for body in ({}, {"alias": "ok", "status": "accepted"}, {"alias": 5}, {"alias": None}):
        assert (await alice.put(f"/api/connections/contacts/{mine}/alias", body)).status_code == 422, body
    assert (await alice.put("/api/connections/contacts/not-a-uuid/alias", {"alias": "ok"})).status_code == 422


# ---------------------------------------------------------------- foreign / vanished contacts

ACTIONS = [
    ("PUT", "alias", {"alias": "Hacked"}),
    ("POST", "block", None),
    ("POST", "unblock", None),
    ("DELETE", "", None),
    ("PUT", f"grants/{FREE}", {"enabled": True}),
]


@pytest.mark.parametrize(("method", "tail", "body"), ACTIONS)
async def test_somebody_elses_contact_id_is_a_404_for_every_action_and_changes_nothing(alice, bob, method, tail, body):
    mine, _ = await link_up(alice, bob)
    path = f"/api/connections/contacts/{mine}" + (f"/{tail}" if tail else "")

    response = await bob.http.request(method, path, json=body, headers=bob.headers)  # Bob uses Alice's row id

    assert response.status_code == 404 and response.json()["error"]["code"] == "not_found"
    assert contact_of((await alice.get("/api/connections")).json(), mine)["alias"] == "Боб"
    assert await active_grants(alice) == []


@pytest.mark.parametrize(("method", "tail", "body"), ACTIONS)
async def test_a_contact_that_vanished_meanwhile_is_a_404_not_a_crash(alice, method, tail, body):
    import uuid

    path = f"/api/connections/contacts/{uuid.uuid4()}" + (f"/{tail}" if tail else "")
    response = await alice.http.request(method, path, json=body, headers=alice.headers)
    assert response.status_code == 404 and response.json()["error"]["message"]


# ---------------------------------------------------------------- block / unblock / remove


async def test_blocking_shows_as_blocked_with_no_permissions_and_unblocking_gives_none_back(alice, bob):
    mine, theirs = await link_up(alice, bob)
    await alice.put(f"/api/connections/contacts/{mine}/grants/{FREE}", {"enabled": True})
    await alice.put(f"/api/connections/contacts/{mine}/grants/{MOOD}", {"enabled": True})

    blocked = contact_of((await alice.post(f"/api/connections/contacts/{mine}/block")).json(), mine)
    assert (blocked["status"], blocked["grants"]) == ("blocked", {FREE: False, MOOD: False})
    assert await active_grants(alice) == []
    assert contact_of((await bob.get("/api/connections")).json(), theirs)["status"] == "accepted"  # Bob is not told

    unblocked = contact_of((await alice.post(f"/api/connections/contacts/{mine}/unblock")).json(), mine)
    assert (unblocked["status"], unblocked["grants"]) == ("accepted", {FREE: False, MOOD: False})


async def test_removing_a_contact_removes_it_from_both_address_books(alice, bob):
    mine, theirs = await link_up(alice, bob)
    await alice.put(f"/api/connections/contacts/{mine}/grants/{FREE}", {"enabled": True})

    response = await alice.delete(f"/api/connections/contacts/{mine}")

    assert response.status_code == 200 and response.json()["contacts"] == []
    assert (await bob.get("/api/connections")).json()["contacts"] == []
    assert await active_grants(alice) == []
    assert (await alice.delete(f"/api/connections/contacts/{mine}")).status_code == 404  # already gone


async def test_a_blocked_contact_cannot_be_removed_until_it_is_unblocked(alice, bob):
    mine, _ = await link_up(alice, bob)
    await alice.post(f"/api/connections/contacts/{mine}/block")

    refused = await alice.delete(f"/api/connections/contacts/{mine}")

    assert refused.status_code == 409 and refused.json()["error"]["code"] == "contact_blocked"
    assert contact_of((await alice.get("/api/connections")).json(), mine)["status"] == "blocked"
    await alice.post(f"/api/connections/contacts/{mine}/unblock")
    assert (await alice.delete(f"/api/connections/contacts/{mine}")).status_code == 200


# ---------------------------------------------------------------- grants


async def test_a_grant_is_switched_on_and_off_from_the_page(alice, bob):
    mine, _ = await link_up(alice, bob)

    on = await alice.put(f"/api/connections/contacts/{mine}/grants/{FREE}", {"enabled": True})
    assert contact_of(on.json(), mine)["grants"] == {FREE: True, MOOD: False}
    assert await active_grants(alice, bob.user_id) == [FREE]

    again = await alice.put(f"/api/connections/contacts/{mine}/grants/{FREE}", {"enabled": True})  # idempotent
    assert contact_of(again.json(), mine)["grants"] == {FREE: True, MOOD: False}

    off = await alice.put(f"/api/connections/contacts/{mine}/grants/{FREE}", {"enabled": False})
    assert contact_of(off.json(), mine)["grants"] == {FREE: False, MOOD: False}
    assert await active_grants(alice) == []


async def test_an_expired_grant_is_shown_as_off(alice, bob):
    mine, _ = await link_up(alice, bob)
    await alice.sql(
        "INSERT INTO civ.access_grants (user_id, grantee_user_id, scope, granted_at, expires_at) "
        "VALUES (:u, :b, 'calendar_free_busy', now() - interval '2 days', now() - interval '1 day')",
        b=bob.user_id,
    )
    assert contact_of((await alice.get("/api/connections")).json(), mine)["grants"][FREE] is False


@pytest.mark.parametrize("scope", ["calendar_details", "profile_public", "persona_status", "the_whole_brain", "x" * 41])
async def test_only_the_offered_scopes_can_be_switched(alice, bob, scope):
    mine, _ = await link_up(alice, bob)
    response = await alice.put(f"/api/connections/contacts/{mine}/grants/{scope}", {"enabled": True})
    assert response.status_code in (404, 422)
    assert await active_grants(alice) == []


async def test_a_grant_needs_an_accepted_contact(alice, bob):
    mine, _ = await link_up(alice, bob, status="pending")
    refused = await alice.put(f"/api/connections/contacts/{mine}/grants/{FREE}", {"enabled": True})
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "not_accepted"
    assert contact_of((await alice.get("/api/connections")).json(), mine)["status"] == "pending"
    assert await active_grants(alice) == []

    off = await alice.put(f"/api/connections/contacts/{mine}/grants/{FREE}", {"enabled": False})  # withdrawing is never refused
    assert off.status_code == 200


async def test_grant_bodies_are_strict(alice, bob):
    mine, _ = await link_up(alice, bob)
    for body in ({}, {"enabled": "maybe"}, {"enabled": True, "scope": MOOD}, {"enabled": None}):
        assert (await alice.put(f"/api/connections/contacts/{mine}/grants/{FREE}", body)).status_code == 422, body
    assert await active_grants(alice) == []


# ---------------------------------------------------------------- the journal


async def seed_audit(actor, requester, scope, decision, *, minutes_ago: float = 0):
    await actor.sql(
        "INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision, created_at) "
        "VALUES (:u, :r, CAST(:s AS civ.acl_scope), :d, now() - make_interval(secs => :ago))",
        r=requester, s=scope, d=decision, ago=minutes_ago * 60,
    )  # fmt: skip


async def test_the_journal_lists_who_asked_newest_first_by_my_own_names(alice, bob):
    await link_up(alice, bob, alice_calls="Боб")
    stranger = await new_user(alice.owner_engine)  # asked once, then was removed from the address book (or never was in it)
    await seed_audit(alice, bob.user_id, FREE, "allowed", minutes_ago=180)
    await seed_audit(alice, stranger, MOOD, "denied", minutes_ago=120)
    await seed_audit(alice, bob.user_id, "calendar_details", "denied", minutes_ago=60)

    entries = (await alice.get("/api/connections/audit")).json()["entries"]

    assert [(e["requester"], e["scope"], e["decision"]) for e in entries] == [
        ("Боб", "calendar_details", "denied"), (None, MOOD, "denied"), ("Боб", FREE, "allowed"),
    ]  # fmt: skip
    assert [e["scope_title"] for e in entries] == ["Подробности календаря", "Моё состояние", "Занят или свободен"]
    assert all(set(e) == {"id", "created_at", "requester", "scope", "scope_title", "decision"} for e in entries)
    assert str(bob.user_id) not in json.dumps(entries)


async def test_the_journal_is_private_and_paged(alice, bob):
    await link_up(alice, bob)
    for index in range(5):
        await seed_audit(alice, bob.user_id, FREE, "allowed", minutes_ago=5 - index)
    await seed_audit(bob, alice.user_id, FREE, "allowed")  # Bob's journal, about Bob

    first = (await alice.get("/api/connections/audit", params={"limit": 2})).json()
    second = (await alice.get("/api/connections/audit", params={"limit": 2, "before": first["next_before"]})).json()
    third = (await alice.get("/api/connections/audit", params={"limit": 2, "before": second["next_before"]})).json()

    pages = [first, second, third]
    assert [len(p["entries"]) for p in pages] == [2, 2, 1]
    assert [p["next_before"] is None for p in pages] == [False, False, True]
    ids = [e["id"] for p in pages for e in p["entries"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 5  # no overlaps, no gaps, newest first
    assert len((await bob.get("/api/connections/audit")).json()["entries"]) == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"limit": "many"}, {"before": 0}, {"before": "x"}])
async def test_journal_paging_parameters_are_validated(alice, params):
    assert (await alice.get("/api/connections/audit", params=params)).status_code == 422


async def test_the_journal_cannot_be_written_through_the_api(alice):
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        response = await alice.http.request(method, "/api/connections/audit", json={}, headers=alice.headers)
        assert response.status_code in (404, 405)


# ---------------------------------------------------------------- the whole story

async def test_the_story_end_to_end(alice, bob, db):
    """Link -> handshake -> rename -> allow -> a request is journaled -> block -> unblock -> remove, all through the API."""
    token = (await alice.post("/api/connections/invites")).json()["link"].rsplit("connect_", 1)[1]
    async with db.tenant_session(bob.user_id) as session:
        peeked = await invites.peek_invite(session, invites.hash_token(token))
        await invites.accept_invite(session, peeked.invite_id, "Боб")
    contact = (await alice.get("/api/connections")).json()["contacts"][0]["id"]

    await alice.put(f"/api/connections/contacts/{contact}/alias", {"alias": "Боб (спортзал)"})
    await alice.put(f"/api/connections/contacts/{contact}/grants/{MOOD}", {"enabled": True})
    await seed_audit(alice, bob.user_id, MOOD, "allowed")
    entry = (await alice.get("/api/connections/audit")).json()["entries"][0]
    assert (entry["requester"], entry["decision"]) == ("Боб (спортзал)", "allowed")

    await alice.post(f"/api/connections/contacts/{contact}/block")
    assert await active_grants(alice) == []
    await alice.post(f"/api/connections/contacts/{contact}/unblock")
    assert (await alice.delete(f"/api/connections/contacts/{contact}")).json()["contacts"] == []
    assert (await alice.get("/api/connections/audit")).json()["entries"][0]["requester"] is None  # the journal outlives the contact

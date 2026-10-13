"""Module 5, step 2 in PostgreSQL: invites, the handshake (``civ.peek_invite`` / ``accept_invite``), the notification outbox and its
housekeeping. Real database, the application role (RLS enforced) for everything the bot does, the owner only to seed or to look."""

from __future__ import annotations

import asyncio
import json
import random
import uuid
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import text

from ai_civ.bot import invites
from ai_civ.bot.invites import CreateOutcome, HandshakeOutcome, InviteState
from tests.helpers import new_contact, new_user, owner_exec, pg_error, tenant_exec

DAY = timedelta(days=1)


async def make_invite(db, inviter: uuid.UUID, name: str = "Alice", ttl: timedelta = 3 * DAY):
    token = invites.new_token()
    async with db.tenant_session(inviter) as session:
        created = await invites.create_invite(session, token_hash=invites.hash_token(token), display_name=name, ttl=ttl)
    return token, created


async def peek(db, who: uuid.UUID, token: str):
    async with db.tenant_session(who) as session:
        return await invites.peek_invite(session, invites.hash_token(token))


async def accept(db, who: uuid.UUID, invite_id: uuid.UUID, name: str = "Bob"):
    async with db.tenant_session(who) as session:
        return await invites.accept_invite(session, invite_id, name)


async def contact_rows(owner_engine, a: uuid.UUID, b: uuid.UUID):
    return await owner_exec(
        owner_engine,
        "SELECT user_id, contact_user_id, alias, status::text FROM civ.contacts "
        "WHERE (user_id = :a AND contact_user_id = :b) OR (user_id = :b AND contact_user_id = :a) ORDER BY alias",
        a=a,
        b=b,
    )


async def notifications_of(owner_engine, *users: uuid.UUID):
    return await owner_exec(
        owner_engine,
        "SELECT user_id, kind, payload, idempotency_key, status::text FROM civ.notification_outbox WHERE user_id = ANY(:u)",
        u=list(users),
    )


async def pair(owner_engine):
    return await new_user(owner_engine), await new_user(owner_engine)


# ================================================================== create_invite


async def test_an_invite_stores_only_the_hash_of_its_token(db, owner_engine):
    a = await new_user(owner_engine)
    token, created = await make_invite(db, a, "Alice")
    assert created.outcome is CreateOutcome.created and created.invite_id is not None

    [row] = await owner_exec(owner_engine, "SELECT user_id, token_hash, display_name, used_at, revoked_at, expires_at - created_at FROM civ.contact_invites WHERE id = :i", i=created.invite_id)
    assert row[0] == a and bytes(row[1]) == invites.hash_token(token) and row[2] == "Alice" and row[3] is None and row[4] is None
    assert row[5] == 3 * DAY
    everything = (await owner_exec(owner_engine, "SELECT t::text FROM civ.contact_invites t WHERE id = :i", i=created.invite_id))[0][0]
    assert token not in everything  # a leaked table holds no working link


async def test_invites_are_created_only_for_somebody_who_is_ready(db, owner_engine):
    pending = await new_user(owner_engine, onboarding_state="pending")
    frozen = await new_user(owner_engine)
    await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=frozen)
    for who in (pending, frozen):
        _, created = await make_invite(db, who)
        assert (created.outcome, created.invite_id, created.expires_at) == (CreateOutcome.not_ready, None, None)
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.contact_invites WHERE user_id = ANY(:u)", u=[pending, frozen]) == [(0,)]


async def test_at_most_five_open_invites_even_when_requested_at_once(db, owner_engine):
    a = await new_user(owner_engine)
    results = await asyncio.gather(*[make_invite(db, a) for _ in range(9)])
    assert Counter(created.outcome for _, created in results) == {CreateOutcome.created: 5, CreateOutcome.too_many: 4}
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.contact_invites WHERE user_id = :a", a=a) == [(5,)]

    # a used or expired invite does not count against the limit
    token, _ = results[[c.outcome for _, c in results].index(CreateOutcome.created)]
    await owner_exec(owner_engine, "UPDATE civ.contact_invites SET created_at = now() - interval '2 days', expires_at = now() - interval '1 day' WHERE user_id = :a AND token_hash = :h", a=a, h=invites.hash_token(token))
    assert (await make_invite(db, a))[1].outcome is CreateOutcome.created


@pytest.mark.parametrize("name", ["", " Alice", "Alice ", "Al\nice", "Al\tice", "Alice‮", "⁦Alice", "x" * 41])
async def test_a_dirty_display_name_is_refused_by_the_database(db, owner_engine, name):
    a = await new_user(owner_engine)
    with pg_error("22023"):
        await make_invite(db, a, name)


@pytest.mark.parametrize(
    ("digest", "ttl"),
    [(b"short", DAY), (invites.hash_token("x" * 22), timedelta(seconds=10)), (invites.hash_token("y" * 22), 31 * DAY)],
)
async def test_a_bad_hash_or_ttl_is_refused(db, owner_engine, digest, ttl):
    a = await new_user(owner_engine)
    with pg_error("22023"):
        async with db.tenant_session(a) as session:
            await invites.create_invite(session, token_hash=digest, display_name="Alice", ttl=ttl)


async def test_the_application_cannot_write_invites_or_notifications_directly(db, owner_engine):
    a, b = await pair(owner_engine)
    token, created = await make_invite(db, a)
    with pg_error("42501"):
        await tenant_exec(db, a, "INSERT INTO civ.contact_invites (user_id, token_hash, display_name, expires_at) VALUES (:u, sha256('x'), 'Eve', now() + interval '1 day')", u=a)
    with pg_error("42501"):  # not even to resurrect a revoked one or to extend one
        await tenant_exec(db, a, "UPDATE civ.contact_invites SET expires_at = expires_at + interval '1 year'")
    with pg_error("42501"):
        await tenant_exec(db, a, "DELETE FROM civ.contact_invites")
    with pg_error("42501"):
        await tenant_exec(db, a, "INSERT INTO civ.notification_outbox (user_id, kind, idempotency_key) VALUES (:u, 'contact_accepted', 'forged')", u=a)
    # reading your own is fine, and nobody else's is visible
    assert await tenant_exec(db, a, "SELECT count(*) FROM civ.contact_invites") == [(1,)]
    assert await tenant_exec(db, b, "SELECT count(*) FROM civ.contact_invites") == [(0,)]


async def test_every_function_needs_a_tenant_and_only_the_application_may_call_them(db, owner_engine):
    with pg_error("42704"):  # no tenant context at all: fail-fast, the caller is never taken from an argument
        async with db.engine.begin() as conn:
            await conn.execute(text("SELECT * FROM civ.peek_invite(sha256('x'))"))
    rows = await owner_exec(
        owner_engine,
        "SELECT p.proname, has_function_privilege('public', p.oid, 'EXECUTE'), has_function_privilege('ai_civ_app', p.oid, 'EXECUTE') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'civ' AND p.proname IN "
        "('create_invite', 'revoke_invites', 'peek_invite', 'accept_invite', 'pending_notification_tenants', 'purge_social', 'lock_pair') "
        "ORDER BY 1",
    )
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        "accept_invite": (False, True),
        "create_invite": (False, True),
        "lock_pair": (False, False),  # internal: only the functions (running as the owner) may take the pair lock
        "pending_notification_tenants": (False, True),
        "peek_invite": (False, True),
        "purge_social": (False, True),
        "revoke_invites": (False, True),
    }
    secdef = await owner_exec(
        owner_engine, "SELECT proname FROM pg_proc WHERE proname IN ('create_invite', 'revoke_invites', 'peek_invite', 'accept_invite') AND prosecdef"
    )
    assert len(secdef) == 4


# ================================================================== peek_invite


async def test_peek_says_ok_to_a_stranger_and_own_to_the_inviter(db, owner_engine):
    a, b = await pair(owner_engine)
    token, created = await make_invite(db, a, "Alice")

    seen = await peek(db, b, token)
    assert (seen.state, seen.invite_id, seen.inviter_name) == (InviteState.ok, created.invite_id, "Alice")
    assert (await peek(db, a, token)).state is InviteState.own
    assert await contact_rows(owner_engine, a, b) == []  # looking changes nothing


async def test_every_dead_link_looks_exactly_the_same(db, owner_engine):
    a, b = await pair(owner_engine)
    unknown = invites.new_token()
    used, used_inv = await make_invite(db, a)
    revoked, _ = await make_invite(db, a)
    expired, expired_inv = await make_invite(db, a)
    blocked, _ = await make_invite(db, a)
    await owner_exec(owner_engine, "UPDATE civ.contact_invites SET used_at = now(), used_by = :b WHERE id = :i", b=await new_user(owner_engine), i=used_inv.invite_id)
    await owner_exec(owner_engine, "UPDATE civ.contact_invites SET revoked_at = now() WHERE token_hash = :h", h=invites.hash_token(revoked))
    await owner_exec(owner_engine, "UPDATE civ.contact_invites SET created_at = now() - interval '2 days', expires_at = now() - interval '1 day' WHERE id = :i", i=expired_inv.invite_id)

    answers = [await peek(db, b, token) for token in (unknown, used, revoked, expired)]
    assert {(p.state, p.invite_id, p.inviter_name) for p in answers} == {(InviteState.invalid, None, None)}

    await new_contact(owner_engine, a, b, status="blocked")  # the inviter blocked B: also just "invalid", no hint of why
    assert (await peek(db, b, blocked)).state is InviteState.invalid


async def test_peek_needs_a_tenant_who_finished_onboarding(db, owner_engine):
    a = await new_user(owner_engine)
    newcomer = await new_user(owner_engine, onboarding_state="in_progress")
    token, _ = await make_invite(db, a)
    seen = await peek(db, newcomer, token)
    assert (seen.state, seen.invite_id, seen.inviter_name) == (InviteState.not_ready, None, None)


# ================================================================== accept_invite


async def test_accepting_connects_both_sides_in_one_step_and_tells_the_inviter(db, owner_engine):
    a, b = await pair(owner_engine)
    token, created = await make_invite(db, a, "Alice (A)")

    result = await accept(db, b, created.invite_id, "Bob (B)")

    assert (result.outcome, result.inviter_name) == (HandshakeOutcome.connected, "Alice (A)")
    rows = await contact_rows(owner_engine, a, b)
    assert sorted((r[0] == a, r[2], r[3]) for r in rows) == sorted([(True, "Bob (B)", "accepted"), (False, "Alice (A)", "accepted")])
    [(consumed_by,)] = await owner_exec(owner_engine, "SELECT used_by FROM civ.contact_invites WHERE id = :i AND used_at IS NOT NULL", i=created.invite_id)
    assert consumed_by == b
    a_side = next(r for r in await owner_exec(owner_engine, "SELECT id, user_id FROM civ.contacts WHERE user_id = :a", a=a))
    [note] = await notifications_of(owner_engine, a, b)
    assert note[0] == a and note[1] == "contact_accepted" and note[3] == f"contact_accepted.{created.invite_id}" and note[4] == "pending"
    payload = json.loads(note[2]) if isinstance(note[2], str) else note[2]
    assert uuid.UUID(payload["contact_id"]) == a_side[0]  # points at A's own row: the worker reads the name from there
    assert result.contact_id is not None
    # RLS: each side sees only its own row, and the notification is invisible to B
    assert await tenant_exec(db, b, "SELECT count(*) FROM civ.contacts") == [(1,)]
    assert await tenant_exec(db, b, "SELECT count(*) FROM civ.notification_outbox") == [(0,)]
    assert await tenant_exec(db, a, "SELECT count(*) FROM civ.notification_outbox") == [(1,)]
    # the link is spent
    assert (await peek(db, await new_user(owner_engine), token)).state is InviteState.invalid


async def test_a_double_click_connects_once(db, owner_engine):
    a, b = await pair(owner_engine)
    _, created = await make_invite(db, a)

    results = await asyncio.gather(*[accept(db, b, created.invite_id) for _ in range(8)])

    assert Counter(r.outcome for r in results) == {HandshakeOutcome.connected: 1, HandshakeOutcome.already_connected: 7}
    assert len(await contact_rows(owner_engine, a, b)) == 2
    assert len(await notifications_of(owner_engine, a, b)) == 1  # A is told once
    again = await accept(db, b, created.invite_id)  # a click long after: still just "already connected"
    assert again.outcome is HandshakeOutcome.already_connected


async def test_two_people_racing_for_one_link_only_one_gets_it(db, owner_engine):
    a, b, c = [await new_user(owner_engine) for _ in range(3)]
    _, created = await make_invite(db, a)

    results = await asyncio.gather(accept(db, b, created.invite_id), accept(db, c, created.invite_id))

    assert Counter(r.outcome for r in results) == {HandshakeOutcome.connected: 1, HandshakeOutcome.invalid: 1}
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.contacts WHERE user_id = :a", a=a) == [(1,)]
    assert len(await notifications_of(owner_engine, a)) == 1


async def test_crossing_invites_neither_deadlock_nor_connect_twice(db, owner_engine):
    """A invites B while B invites A, every link is clicked twice at the same moment: the pair lock is symmetric."""
    for _ in range(6):
        a, b = await pair(owner_engine)
        _, from_a = await make_invite(db, a)
        _, from_b = await make_invite(db, b)
        calls = [(b, from_a.invite_id), (a, from_b.invite_id), (b, from_a.invite_id), (a, from_b.invite_id)]
        random.shuffle(calls)

        results = await asyncio.wait_for(asyncio.gather(*[accept(db, who, inv) for who, inv in calls]), timeout=30)

        assert Counter(r.outcome for r in results) == {HandshakeOutcome.connected: 1, HandshakeOutcome.already_connected: 3}
        assert len(await contact_rows(owner_engine, a, b)) == 2 and {r[3] for r in await contact_rows(owner_engine, a, b)} == {"accepted"}
        assert len(await notifications_of(owner_engine, a, b)) == 1  # exactly one side was told


async def test_a_second_link_between_people_who_are_already_connected_changes_nothing(db, owner_engine):
    a, b = await pair(owner_engine)
    _, first = await make_invite(db, a)
    await accept(db, b, first.invite_id)
    second_token, second = await make_invite(db, a)

    assert (await peek(db, b, second_token)).state is InviteState.already_connected
    assert (await accept(db, b, second.invite_id)).outcome is HandshakeOutcome.already_connected
    assert await owner_exec(owner_engine, "SELECT used_at FROM civ.contact_invites WHERE id = :i", i=second.invite_id) == [(None,)]  # not spent
    assert len(await notifications_of(owner_engine, a)) == 1


async def test_a_pending_row_is_promoted_and_existing_aliases_survive(db, owner_engine):
    a, b = await pair(owner_engine)
    await new_contact(owner_engine, a, b, status="pending", alias="Bobby (mine)")
    await new_contact(owner_engine, b, a, status="accepted", alias="Boss")  # b's row existed already, with its own alias
    _, created = await make_invite(db, a, "Alice")

    assert (await accept(db, b, created.invite_id, "Bob")).outcome is HandshakeOutcome.connected

    rows = {(r[0] == a): (r[2], r[3]) for r in await contact_rows(owner_engine, a, b)}
    assert rows == {True: ("Bobby (mine)", "accepted"), False: ("Boss", "accepted")}  # promoted; nobody's own label overwritten


async def test_a_person_the_inviter_blocked_cannot_get_through_and_nothing_changes(db, owner_engine):
    a, b = await pair(owner_engine)
    await new_contact(owner_engine, a, b, status="blocked")
    _, created = await make_invite(db, a)

    assert (await accept(db, b, created.invite_id)).outcome is HandshakeOutcome.invalid  # indistinguishable from a dead link

    assert [(r[3]) for r in await contact_rows(owner_engine, a, b)] == ["blocked"]  # still blocked, nothing was added for b either
    assert await notifications_of(owner_engine, a) == []


async def test_accepting_lifts_the_acceptors_own_block(db, owner_engine):
    a, b = await pair(owner_engine)
    await new_contact(owner_engine, b, a, status="blocked")  # b blocked a earlier; opening a's link and pressing Accept is the decision
    _, created = await make_invite(db, a)

    assert (await accept(db, b, created.invite_id)).outcome is HandshakeOutcome.connected
    assert {r[3] for r in await contact_rows(owner_engine, a, b)} == {"accepted"}


async def test_the_other_outcomes_of_accept(db, owner_engine):
    a, b = await pair(owner_engine)
    newcomer = await new_user(owner_engine, onboarding_state="pending")
    _, created = await make_invite(db, a)

    assert (await accept(db, a, created.invite_id)).outcome is HandshakeOutcome.own  # your own link
    assert (await accept(db, b, uuid.uuid4())).outcome is HandshakeOutcome.invalid  # an id that does not exist
    assert (await accept(db, newcomer, created.invite_id)).outcome is HandshakeOutcome.not_ready
    with pg_error("22023"):
        await accept(db, b, created.invite_id, "Bo\nb")
    assert await contact_rows(owner_engine, a, b) == [] and await notifications_of(owner_engine, a) == []  # none of them changed anything
    assert (await accept(db, b, created.invite_id)).outcome is HandshakeOutcome.connected  # and the link still works after all that


async def test_a_revoked_or_expired_link_cannot_be_accepted_by_its_id_either(db, owner_engine):
    a, b = await pair(owner_engine)
    _, one = await make_invite(db, a)
    _, two = await make_invite(db, a)
    async with db.tenant_session(a) as session:
        assert await invites.revoke_invites(session) == 2
    async with db.tenant_session(a) as session:
        assert await invites.revoke_invites(session) == 0  # nothing open any more
    await owner_exec(owner_engine, "UPDATE civ.contact_invites SET revoked_at = NULL, created_at = now() - interval '2 days', expires_at = now() - interval '1 day' WHERE id = :i", i=two.invite_id)

    for inv in (one, two):
        assert (await accept(db, b, inv.invite_id)).outcome is HandshakeOutcome.invalid
    assert await contact_rows(owner_engine, a, b) == []


async def test_revoking_touches_only_the_callers_open_invites(db, owner_engine):
    a, b = await pair(owner_engine)
    token_a, _ = await make_invite(db, a)
    token_b, _ = await make_invite(db, b)
    async with db.tenant_session(a) as session:
        assert await invites.revoke_invites(session) == 1
    assert (await peek(db, b, token_a)).state is InviteState.invalid
    assert (await peek(db, a, token_b)).state is InviteState.ok  # b's link is untouched


# ================================================================== notification outbox: who is dispatched, housekeeping


async def accepted_pair(db, owner_engine):
    a, b = await pair(owner_engine)
    _, created = await make_invite(db, a)
    await accept(db, b, created.invite_id)
    return a, b


async def test_only_tenants_that_can_be_written_to_are_dispatched(db, owner_engine):
    ready_a, _ = await accepted_pair(db, owner_engine)
    frozen_a, _ = await accepted_pair(db, owner_engine)
    await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=frozen_a)
    homeless_a, _ = await accepted_pair(db, owner_engine)
    await owner_exec(owner_engine, "UPDATE civ.users SET forum_chat_id = NULL WHERE id = :u", u=homeless_a)

    tenants = await db.tenants_with_pending_notifications(limit=1000)

    assert ready_a in tenants and frozen_a not in tenants and homeless_a not in tenants
    assert len(tenants) == len(set(tenants))  # one entry per tenant, however many rows it has
    assert len(await db.tenants_with_pending_notifications(limit=1)) == 1  # the limit holds
    await owner_exec(owner_engine, "UPDATE civ.notification_outbox SET status = 'done', processed_at = now() WHERE user_id = :u", u=ready_a)
    assert ready_a not in await db.tenants_with_pending_notifications(limit=1000)  # delivered: not offered again
    await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'active' WHERE id = :u", u=frozen_a)
    assert frozen_a in await db.tenants_with_pending_notifications(limit=1000)  # waited for the group, then dispatched


async def test_housekeeping_gives_up_on_the_undeliverable_and_deletes_the_old(db, owner_engine):
    a, b = await pair(owner_engine)
    for key, status, age, attempts, lease in (
        ("stuck", "processing", timedelta(hours=1), 5, timedelta(minutes=-1)),  # out of attempts, lease over: died on the last try -> reaped
        ("old", "pending", timedelta(days=40), 0, None),  # never deliverable for too long -> expired
        ("fresh", "pending", timedelta(hours=1), 0, None),  # untouched
        ("leased", "processing", timedelta(days=40), 1, timedelta(minutes=10)),  # a worker holds it right now: left alone
        ("done_old", "done", timedelta(days=10), 1, None),  # delivered long ago -> purged
        ("done_new", "done", timedelta(hours=1), 1, None),
    ):
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.notification_outbox (user_id, kind, idempotency_key, status, attempts, created_at, locked_until, processed_at) "
            "VALUES (:u, 'contact_accepted', :k, CAST(:s AS civ.outbox_status), :n, now() - CAST(:age AS interval), "
            "now() + CAST(:lease AS interval), CASE WHEN CAST(:s AS text) = 'done' THEN now() - CAST(:age AS interval) END)",
            u=a, k=key, s=status, n=attempts, age=age, lease=lease,
        )  # fmt: skip
    ended = [await make_invite(db, b) for _ in range(3)]
    await owner_exec(owner_engine, "UPDATE civ.contact_invites SET created_at = now() - interval '90 days', expires_at = now() - interval '60 days' WHERE id = :i", i=ended[0][1].invite_id)
    await owner_exec(owner_engine, "UPDATE civ.contact_invites SET created_at = now() - interval '90 days', expires_at = now() - interval '60 days', revoked_at = now() - interval '45 days' WHERE id = :i", i=ended[1][1].invite_id)

    stats = await db.purge_social(done_ttl_days=7, notification_max_age_days=30, invite_ttl_days=30)

    assert (stats.reaped, stats.expired, stats.purged, stats.invites) == (1, 1, 1, 2)
    states = dict(await owner_exec(owner_engine, "SELECT idempotency_key, status::text FROM civ.notification_outbox WHERE user_id = :u", u=a))
    assert states == {"stuck": "dead", "old": "dead", "fresh": "pending", "leased": "processing", "done_new": "done"}
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.contact_invites WHERE user_id = :b", b=b) == [(1,)]  # the live one stays

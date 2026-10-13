"""Module 5, step 3 in PostgreSQL: the functions of migration 0006 that manage the address book (rename / block / unblock / remove a
contact, switch a grant, revoke one invite). Real database; the application role (RLS enforced) calls the functions, the owner seeds
and looks."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import text

from ai_civ.bot import invites
from ai_civ.bot.invites import HandshakeOutcome
from tests.helpers import new_contact, new_user, owner_exec, pg_error, tenant_exec

FREE = "calendar_free_busy"
MOOD = "emotional_state_label"


async def call(db, who: uuid.UUID, sql: str, **params):
    """One function call as ``who`` (own transaction); returns the scalar."""
    return (await tenant_exec(db, who, sql, **params))[0][0]


async def connected(owner_engine, a_alias: str | None = "Bob", b_alias: str | None = "Alice"):
    """A and B, connected the way the handshake leaves them: two accepted rows. Returns (a, b, a's row id, b's row id)."""
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    return a, b, await new_contact(owner_engine, a, b, alias=a_alias), await new_contact(owner_engine, b, a, alias=b_alias)


async def grant(owner_engine, owner: uuid.UUID, grantee: uuid.UUID, scope: str = FREE) -> None:
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.access_grants (user_id, grantee_user_id, scope) VALUES (:o, :g, CAST(:s AS civ.acl_scope))",
        o=owner,
        g=grantee,
        s=scope,
    )


async def active_grants(owner_engine, owner: uuid.UUID, grantee: uuid.UUID | None = None) -> list[str]:
    rows = await owner_exec(
        owner_engine,
        "SELECT scope::text FROM civ.access_grants WHERE user_id = :o AND revoked_at IS NULL "
        "AND (CAST(:g AS uuid) IS NULL OR grantee_user_id = :g) ORDER BY 1",
        o=owner,
        g=grantee,
    )
    return [r[0] for r in rows]


async def rows_of(owner_engine, *users: uuid.UUID) -> dict[tuple[uuid.UUID, uuid.UUID], tuple[str | None, str]]:
    rows = await owner_exec(
        owner_engine,
        "SELECT user_id, contact_user_id, alias, status::text FROM civ.contacts WHERE user_id = ANY(:u)",
        u=list(users),
    )
    return {(r[0], r[1]): (r[2], r[3]) for r in rows}


# ================================================================== privileges


async def test_the_functions_are_for_the_application_only_and_need_a_tenant(db, owner_engine):
    names = ("revoke_invite", "rename_contact", "block_contact", "unblock_contact", "remove_contact", "set_grant")
    rows = await owner_exec(
        owner_engine,
        "SELECT p.proname, has_function_privilege('public', p.oid, 'EXECUTE'), has_function_privilege('ai_civ_app', p.oid, 'EXECUTE'), "
        "p.prosecdef, p.proconfig::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'civ' AND p.proname = ANY(:n) ORDER BY 1",
        n=list(names),
    )
    assert [r[0] for r in rows] == sorted(names)
    for name, public, app, definer, config in rows:
        assert (public, app, definer) == (False, True, True), name
        assert "search_path=civ, pg_temp" in config, name  # a SECURITY DEFINER function must pin its search_path

    with pg_error("42704"):  # no tenant context: the caller is never taken from an argument
        async with db.engine.begin() as conn:
            await conn.execute(text("SELECT civ.block_contact(gen_random_uuid())"))


async def test_the_application_still_cannot_write_contacts_directly(db, owner_engine):
    a, b, row, _ = await connected(owner_engine)
    with pg_error("42501"):
        await tenant_exec(db, a, "UPDATE civ.contacts SET status = 'blocked' WHERE id = :i", i=row)
    with pg_error("42501"):
        await tenant_exec(db, a, "UPDATE civ.contacts SET alias = 'x' WHERE id = :i", i=row)


# ================================================================== rename


async def test_rename_changes_only_my_own_row(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)
    assert await call(db, a, "SELECT civ.rename_contact(:c, :n)", c=a_row, n="Sasha from work") == "ok"
    assert await rows_of(owner_engine, a, b) == {(a, b): ("Sasha from work", "accepted"), (b, a): ("Alice", "accepted")}
    assert await call(db, a, "SELECT civ.rename_contact(:c, :n)", c=a_row, n="Sasha from work") == "ok"  # idempotent


async def test_somebody_elses_contact_id_is_simply_not_found(db, owner_engine):
    a, b, a_row, b_row = await connected(owner_engine)
    assert await call(db, b, "SELECT civ.rename_contact(:c, :n)", c=a_row, n="Hacked") == "not_found"  # A's row, B calling
    assert await call(db, a, "SELECT civ.rename_contact(:c, :n)", c=uuid.uuid4(), n="Nobody") == "not_found"
    assert await rows_of(owner_engine, a, b) == {(a, b): ("Bob", "accepted"), (b, a): ("Alice", "accepted")}


@pytest.mark.parametrize("name", ["", " Sasha", "Sasha ", "Sa\nsha", "Sa\tsha", "Sasha‮", "⁦Sasha", "x" * 41])
async def test_a_dirty_alias_is_refused_by_the_database(db, owner_engine, name):
    a, _, a_row, _ = await connected(owner_engine)
    with pg_error("22023"):
        await call(db, a, "SELECT civ.rename_contact(:c, :n)", c=a_row, n=name)
    with pg_error("22023"):
        await call(db, a, "SELECT civ.rename_contact(NULL, 'x')")


# ================================================================== block / unblock


async def test_blocking_withdraws_every_grant_and_is_silent_for_the_other_side(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)
    await grant(owner_engine, a, b, FREE)
    await grant(owner_engine, a, b, MOOD)
    await grant(owner_engine, b, a, FREE)  # what B gave A is B's business

    assert await call(db, a, "SELECT civ.block_contact(:c)", c=a_row) == "ok"

    assert await rows_of(owner_engine, a, b) == {(a, b): ("Bob", "blocked"), (b, a): ("Alice", "accepted")}  # B's row: untouched
    assert await active_grants(owner_engine, a) == []
    assert await active_grants(owner_engine, b) == [FREE]
    assert await call(db, a, "SELECT civ.block_contact(:c)", c=a_row) == "ok"  # idempotent


async def test_blocking_a_contact_that_is_not_yours_changes_nothing(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)
    await grant(owner_engine, a, b)
    assert await call(db, b, "SELECT civ.block_contact(:c)", c=a_row) == "not_found"
    assert await call(db, a, "SELECT civ.block_contact(:c)", c=uuid.uuid4()) == "not_found"
    assert (await rows_of(owner_engine, a))[(a, b)][1] == "accepted"
    assert await active_grants(owner_engine, a) == [FREE]


async def test_unblocking_restores_the_contact_but_gives_nothing_back(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)
    await grant(owner_engine, a, b)
    await call(db, a, "SELECT civ.block_contact(:c)", c=a_row)

    assert await call(db, a, "SELECT civ.unblock_contact(:c)", c=a_row) == "ok"

    assert (await rows_of(owner_engine, a))[(a, b)] == ("Bob", "accepted")
    assert await active_grants(owner_engine, a) == []  # default deny again
    assert await call(db, a, "SELECT civ.unblock_contact(:c)", c=a_row) == "ok"  # idempotent
    assert await call(db, b, "SELECT civ.unblock_contact(:c)", c=a_row) == "not_found"


async def test_unblock_does_not_depend_on_what_the_other_side_did(db, owner_engine):
    """It must not reveal that the other side blocked (or removed) us: the answer and the result are the same."""
    a, b, a_row, b_row = await connected(owner_engine)
    await call(db, a, "SELECT civ.block_contact(:c)", c=a_row)
    await call(db, b, "SELECT civ.block_contact(:c)", c=b_row)  # B blocked A as well

    assert await call(db, a, "SELECT civ.unblock_contact(:c)", c=a_row) == "ok"

    assert await rows_of(owner_engine, a, b) == {(a, b): ("Bob", "accepted"), (b, a): ("Alice", "blocked")}


# ================================================================== remove


async def test_removing_a_contact_ends_the_connection_on_both_sides_and_takes_the_grants_with_it(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)
    await grant(owner_engine, a, b, FREE)
    await grant(owner_engine, b, a, MOOD)
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision) VALUES (:a, :b, 'calendar_free_busy', 'allowed')",
        a=a,
        b=b,
    )

    assert await call(db, a, "SELECT civ.remove_contact(:c)", c=a_row) == "removed"

    assert await rows_of(owner_engine, a, b) == {}
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.access_grants WHERE user_id = ANY(:u)", u=[a, b]) == [(0,)]
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.diplomat_audit_log WHERE user_id = :a", a=a) == [(1,)]  # the journal stays
    assert await call(db, a, "SELECT civ.remove_contact(:c)", c=a_row) == "not_found"  # already gone


async def test_a_blocked_contact_is_not_removed_and_a_foreign_one_never_is(db, owner_engine):
    a, b, a_row, b_row = await connected(owner_engine)
    await call(db, a, "SELECT civ.block_contact(:c)", c=a_row)

    assert await call(db, a, "SELECT civ.remove_contact(:c)", c=a_row) == "blocked"
    assert await call(db, b, "SELECT civ.remove_contact(:c)", c=a_row) == "not_found"  # B has no say over A's row
    assert await rows_of(owner_engine, a, b) == {(a, b): ("Bob", "blocked"), (b, a): ("Alice", "accepted")}


async def test_removing_does_not_erase_the_block_the_other_side_has_on_us(db, owner_engine):
    a, b, a_row, b_row = await connected(owner_engine)
    await call(db, b, "SELECT civ.block_contact(:c)", c=b_row)  # B blocked A

    assert await call(db, a, "SELECT civ.remove_contact(:c)", c=a_row) == "removed"

    assert await rows_of(owner_engine, a, b) == {(b, a): ("Alice", "blocked")}  # B's block is B's own decision


async def test_removing_a_one_sided_pair_works_and_a_new_invite_reconnects_the_two(db, owner_engine):
    a, b, a_row, b_row = await connected(owner_engine)
    await owner_exec(owner_engine, "DELETE FROM civ.contacts WHERE id = :i", i=b_row)  # B's row is gone (a direct DELETE, say)

    assert await call(db, a, "SELECT civ.remove_contact(:c)", c=a_row) == "removed"
    assert await rows_of(owner_engine, a, b) == {}

    token = invites.new_token()
    async with db.tenant_session(a) as session:
        created = await invites.create_invite(session, token_hash=invites.hash_token(token), display_name="Alice", ttl=timedelta(days=1))
    async with db.tenant_session(b) as session:
        accepted = await invites.accept_invite(session, created.invite_id, "Bob")
    assert accepted.outcome is HandshakeOutcome.connected
    assert set(await rows_of(owner_engine, a, b)) == {(a, b), (b, a)}


# ================================================================== grants


async def test_a_grant_is_switched_on_and_off_and_both_are_idempotent(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)

    assert await call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=FREE) == "ok"
    assert await call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=FREE) == "ok"
    assert await active_grants(owner_engine, a, b) == [FREE]
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.access_grants WHERE user_id = :a", a=a) == [(1,)]  # not two rows

    assert await call(db, a, "SELECT civ.set_grant(:c, :s, false)", c=a_row, s=FREE) == "ok"
    assert await call(db, a, "SELECT civ.set_grant(:c, :s, false)", c=a_row, s=FREE) == "ok"
    assert await active_grants(owner_engine, a, b) == []

    assert await call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=FREE) == "ok"  # again: a NEW row, the old one is history
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.access_grants WHERE user_id = :a", a=a) == [(2,)]
    assert await active_grants(owner_engine, a, b) == [FREE]


async def test_the_scopes_are_independent(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)
    await call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=FREE)
    await call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=MOOD)
    await call(db, a, "SELECT civ.set_grant(:c, :s, false)", c=a_row, s=FREE)
    assert await active_grants(owner_engine, a, b) == [MOOD]


async def test_only_an_accepted_contact_can_be_given_access_but_it_can_always_be_withdrawn(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)
    pending = await new_contact(owner_engine, a, await new_user(owner_engine), status="pending")
    await call(db, a, "SELECT civ.block_contact(:c)", c=a_row)

    assert await call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=FREE) == "not_accepted"  # blocked
    assert await call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=pending, s=FREE) == "not_accepted"
    assert await call(db, a, "SELECT civ.set_grant(:c, :s, false)", c=a_row, s=FREE) == "ok"  # withdrawing is never refused
    assert await active_grants(owner_engine, a) == []


async def test_set_grant_rejects_foreign_contacts_and_unknown_scopes(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)
    assert await call(db, b, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=FREE) == "not_found"
    assert await call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=uuid.uuid4(), s=FREE) == "not_found"
    assert await active_grants(owner_engine, a) == [] and await active_grants(owner_engine, b) == []
    with pg_error("22P02"):
        await call(db, a, "SELECT civ.set_grant(:c, 'the_whole_brain', true)", c=a_row)
    with pg_error("22023"):
        await call(db, a, "SELECT civ.set_grant(:c, :s, NULL)", c=a_row, s=FREE)


async def test_an_expired_grant_is_replaced_not_mistaken_for_one_in_force(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.access_grants (user_id, grantee_user_id, scope, granted_at, expires_at) "
        "VALUES (:a, :b, 'calendar_free_busy', now() - interval '2 days', now() - interval '1 day')",
        a=a,
        b=b,
    )
    assert await call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=FREE) == "ok"

    rows = await owner_exec(
        owner_engine,
        "SELECT revoked_at IS NOT NULL, expires_at IS NULL FROM civ.access_grants WHERE user_id = :a ORDER BY granted_at",
        a=a,
    )
    assert rows == [(True, False), (False, True)]  # the stale one closed, a fresh unlimited one open


async def test_parallel_switches_leave_exactly_one_active_grant(db, owner_engine):
    a, b, a_row, _ = await connected(owner_engine)
    results = await asyncio.gather(*[call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=FREE) for _ in range(8)])
    assert results == ["ok"] * 8
    assert await active_grants(owner_engine, a, b) == [FREE]


# ================================================================== races between the functions


async def test_a_grant_can_never_outlive_a_concurrent_block(db, owner_engine):
    """block_contact and set_grant take the same pair lock and the contact row lock: whichever runs second sees the other's result."""
    for _ in range(6):
        a, b, a_row, _ = await connected(owner_engine)
        outcomes = await asyncio.gather(
            *[call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=FREE) for _ in range(3)],
            call(db, a, "SELECT civ.block_contact(:c)", c=a_row),
            *[call(db, a, "SELECT civ.set_grant(:c, :s, true)", c=a_row, s=MOOD) for _ in range(3)],
        )
        assert set(outcomes) <= {"ok", "not_accepted"}
        assert (await rows_of(owner_engine, a))[(a, b)][1] == "blocked"
        assert await active_grants(owner_engine, a) == []  # no matter who won each race


async def test_two_people_removing_each_other_at_once_do_not_deadlock(db, owner_engine):
    for _ in range(6):
        a, b, a_row, b_row = await connected(owner_engine)
        first, second = await asyncio.wait_for(
            asyncio.gather(
                call(db, a, "SELECT civ.remove_contact(:c)", c=a_row), call(db, b, "SELECT civ.remove_contact(:c)", c=b_row)
            ),
            timeout=30,
        )
        assert sorted([first, second]) == ["not_found", "removed"]  # one of them ended it, the other found nothing left
        assert await rows_of(owner_engine, a, b) == {}


async def test_a_block_survives_the_other_side_removing_at_the_same_moment(db, owner_engine):
    for _ in range(6):
        a, b, a_row, b_row = await connected(owner_engine)
        await asyncio.wait_for(
            asyncio.gather(
                call(db, a, "SELECT civ.block_contact(:c)", c=a_row), call(db, b, "SELECT civ.remove_contact(:c)", c=b_row)
            ),
            timeout=30,
        )
        left = await rows_of(owner_engine, a, b)
        # either B removed first (everything gone, A's block found nothing) or A blocked first (A's block is kept, B's row goes)
        assert left in ({}, {(a, b): ("Bob", "blocked")})


# ================================================================== revoke one invite


async def make(db, who: uuid.UUID, name: str = "Alice"):
    token = invites.new_token()
    async with db.tenant_session(who) as session:
        created = await invites.create_invite(session, token_hash=invites.hash_token(token), display_name=name, ttl=timedelta(days=1))
    return token, created.invite_id


async def test_revoking_one_invite_leaves_the_others_and_kills_only_that_link(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    first_token, first = await make(db, a)
    _, second = await make(db, a)

    async with db.tenant_session(a) as session:
        assert await invites.revoke_invite(session, first) is True
        assert await invites.revoke_invite(session, first) is False  # nothing left to revoke
    async with db.tenant_session(b) as session:
        peeked = await invites.peek_invite(session, invites.hash_token(first_token))
        accepted = await invites.accept_invite(session, first, "Bob")
    assert peeked.state is invites.InviteState.invalid and accepted.outcome is HandshakeOutcome.invalid
    assert await owner_exec(owner_engine, "SELECT id FROM civ.contact_invites WHERE user_id = :a AND revoked_at IS NULL", a=a) == [(second,)]


async def test_one_cannot_revoke_somebody_elses_or_a_used_invite(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    _, mine = await make(db, a)
    _, used = await make(db, a)
    async with db.tenant_session(b) as session:
        await invites.accept_invite(session, used, "Bob")

    async with db.tenant_session(b) as session:
        assert await invites.revoke_invite(session, mine) is False  # not B's invite
    async with db.tenant_session(a) as session:
        assert await invites.revoke_invite(session, used) is False  # already used: the connection is not undone by this
        assert await invites.revoke_invite(session, uuid.uuid4()) is False
    assert await owner_exec(owner_engine, "SELECT revoked_at IS NULL FROM civ.contact_invites WHERE id = :i", i=mine) == [(True,)]
    assert len(await rows_of(owner_engine, a, b)) == 2


async def test_revoke_and_accept_racing_for_one_invite_have_exactly_one_winner(db, owner_engine):
    for _ in range(6):
        a, b = await new_user(owner_engine), await new_user(owner_engine)
        _, invite_id = await make(db, a)

        async def revoke():
            async with db.tenant_session(a) as session:
                return await invites.revoke_invite(session, invite_id)

        async def accept():
            async with db.tenant_session(b) as session:
                return (await invites.accept_invite(session, invite_id, "Bob")).outcome

        revoked, outcome = await asyncio.wait_for(asyncio.gather(revoke(), accept()), timeout=30)
        connected_rows = len(await rows_of(owner_engine, a, b))
        assert (revoked, outcome, connected_rows) in (
            (True, HandshakeOutcome.invalid, 0),  # the revocation won
            (False, HandshakeOutcome.connected, 2),  # the acceptance won; the connection stands
        )

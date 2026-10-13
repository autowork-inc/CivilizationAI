"""ACL foundation (contacts / grants) and the hard schedule (events)."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.helpers import new_contact, new_persona, new_user, owner_exec, pg_error, tenant_exec

# Contacts are NOT written by the application role any more (migration 0005: only civ.accept_invite creates a connection), so these
# tests seed them as the owner (``new_contact``). Grants are still the user's own business and go through their tenant session.
ADD_CONTACT = "INSERT INTO civ.contacts (user_id, contact_user_id, status) VALUES (:u, :c, 'accepted')"  # as the application: refused
ADD_GRANT = (
    "INSERT INTO civ.access_grants (user_id, grantee_user_id, scope) VALUES (:u, :g, CAST(:s AS civ.acl_scope))"
)

# ------------------------------------------------------------------ contacts and grants


async def test_the_sasha_and_olya_scenario(db, owner_engine):
    """'Sasha may request my emotional_state, Olya only my free calendar slots.'"""
    me, sasha, olya = [await new_user(owner_engine) for _ in range(3)]
    for friend in (sasha, olya):
        await new_contact(owner_engine, me, friend)
    await tenant_exec(db, me, ADD_GRANT, u=me, g=sasha, s="emotional_state_label")
    await tenant_exec(db, me, ADD_GRANT, u=me, g=olya, s="calendar_free_busy")

    def granted(who):
        return tenant_exec(
            db, me, "SELECT scope::text FROM civ.access_grants WHERE grantee_user_id = :g AND revoked_at IS NULL", g=who
        )

    assert [r[0] for r in await granted(sasha)] == ["emotional_state_label"]
    assert [r[0] for r in await granted(olya)] == ["calendar_free_busy"]
    # default deny: Sasha has no calendar scope, Olya has no emotional one
    assert await tenant_exec(
        db, me, "SELECT count(*) FROM civ.access_grants WHERE grantee_user_id = :g AND scope = 'calendar_free_busy'", g=sasha
    ) == [(0,)]
    # the grantee cannot read (or edit) the owner's ACL: tenant isolation
    assert await tenant_exec(db, sasha, "SELECT count(*) FROM civ.access_grants") == [(0,)]
    assert await tenant_exec(db, sasha, "SELECT count(*) FROM civ.contacts") == [(0,)]


async def test_grant_rules(db, owner_engine):
    a, b, stranger = [await new_user(owner_engine) for _ in range(3)]
    with pg_error("23514"):  # no contact with yourself (the table rule holds for the owner too)
        await new_contact(owner_engine, a, a)
    await new_contact(owner_engine, a, b)
    with pg_error("23503"):  # grants only to people in your contacts
        await tenant_exec(db, a, ADD_GRANT, u=a, g=stranger, s="calendar_free_busy")
    await tenant_exec(db, a, ADD_GRANT, u=a, g=b, s="emotional_state_label")
    with pg_error("23505"):  # one ACTIVE grant per (grantee, scope)
        await tenant_exec(db, a, ADD_GRANT, u=a, g=b, s="emotional_state_label")
    await tenant_exec(db, a, ADD_GRANT, u=a, g=b, s="calendar_free_busy")  # another scope is fine
    # revoke, then grant again: history is kept, a new active row is allowed
    assert await tenant_exec(
        db, a, "UPDATE civ.access_grants SET revoked_at = now() WHERE scope = 'emotional_state_label'"
    ) == 1
    await tenant_exec(db, a, ADD_GRANT, u=a, g=b, s="emotional_state_label")
    assert await tenant_exec(db, a, "SELECT count(*) FROM civ.access_grants WHERE scope = 'emotional_state_label'") == [(2,)]
    with pg_error("23514"):
        await tenant_exec(
            db, a, "UPDATE civ.access_grants SET expires_at = granted_at WHERE scope = 'calendar_free_busy'"
        )
    # removing the contact removes the grants given to it
    assert await tenant_exec(db, a, "DELETE FROM civ.contacts WHERE contact_user_id = :b", b=b) == 1
    assert await tenant_exec(db, a, "SELECT count(*) FROM civ.access_grants") == [(0,)]


async def test_the_application_cannot_write_contacts_it_can_only_read_and_remove_its_own(db, owner_engine):
    """The hole of 0001: "accepted" could be self-declared. Now INSERT and UPDATE on civ.contacts are refused (42501) for the
    application role, even for the tenant's own rows; SELECT (RLS-filtered) and DELETE stay."""
    a, b = [await new_user(owner_engine) for _ in range(2)]
    contact = await new_contact(owner_engine, a, b, status="pending")

    with pg_error("42501"):
        await tenant_exec(db, a, ADD_CONTACT, u=a, c=await new_user(owner_engine))
    with pg_error("42501"):  # nor can a pending contact be promoted by its owner
        await tenant_exec(db, a, "UPDATE civ.contacts SET status = 'accepted' WHERE id = :c", c=contact)
    with pg_error("42501"):
        await tenant_exec(db, a, "UPDATE civ.contacts SET alias = 'Boss' WHERE id = :c", c=contact)

    assert await tenant_exec(db, a, "SELECT status::text FROM civ.contacts WHERE id = :c", c=contact) == [("pending",)]
    assert await tenant_exec(db, b, "SELECT count(*) FROM civ.contacts") == [(0,)]  # b sees nothing of a's book
    assert await tenant_exec(db, a, "DELETE FROM civ.contacts WHERE id = :c", c=contact) == 1


async def test_a_grant_needs_an_accepted_contact_and_losing_that_status_revokes_the_grants(db, owner_engine):
    a, b, c = [await new_user(owner_engine) for _ in range(3)]
    pending = await new_contact(owner_engine, a, b, status="pending")
    await new_contact(owner_engine, a, c, status="accepted")

    with pg_error("23514", "not accepted"):  # the FK only proved that a row exists; a PENDING contact gets nothing
        await tenant_exec(db, a, ADD_GRANT, u=a, g=b, s="calendar_free_busy")
    await owner_exec(owner_engine, "UPDATE civ.contacts SET status = 'blocked' WHERE id = :p", p=pending)
    with pg_error("23514", "not accepted"):  # nor a blocked one
        await tenant_exec(db, a, ADD_GRANT, u=a, g=b, s="calendar_free_busy")

    await tenant_exec(db, a, ADD_GRANT, u=a, g=c, s="calendar_free_busy")
    await tenant_exec(db, a, ADD_GRANT, u=a, g=c, s="emotional_state_label")
    await owner_exec(owner_engine, "UPDATE civ.contacts SET status = 'blocked' WHERE user_id = :a AND contact_user_id = :c", a=a, c=c)
    assert await tenant_exec(db, a, "SELECT count(*) FROM civ.access_grants WHERE revoked_at IS NULL") == [(0,)]  # revoked at once
    assert await tenant_exec(db, a, "SELECT count(*) FROM civ.access_grants") == [(2,)]  # history is kept


# ------------------------------------------------------------------ events

ADD_EVENT = (
    "INSERT INTO civ.events (user_id, title, starts_at, ends_at, timezone, created_via{cols}) "
    "VALUES (:u, 'x', now(), now() + :d * interval '1 hour', :tz, :via{vals})"
)


def event_sql(cols: str = "", vals: str = "") -> str:
    return ADD_EVENT.format(cols=cols, vals=vals)


async def test_event_basic_constraints(db, owner_engine):
    u = await new_user(owner_engine)
    ok = {"u": u, "d": 1, "tz": "Europe/Copenhagen", "via": "mini_app"}
    assert await tenant_exec(db, u, event_sql(), **ok) == 1
    assert await tenant_exec(db, u, event_sql(), **{**ok, "via": "bot_confirmed"}) == 1
    with pg_error("23514"):  # an event cannot end before it starts
        await tenant_exec(db, u, event_sql(), **{**ok, "d": 0})
    with pg_error("23514"):  # only the Mini App or a user-confirmed proposal may create events (never raw LLM output)
        await tenant_exec(db, u, event_sql(), **{**ok, "via": "llm"})


async def test_event_timezone_must_be_a_real_iana_zone(db, owner_engine):
    u = await new_user(owner_engine)
    ok = {"u": u, "d": 1, "via": "mini_app"}
    for tz in ("UTC", "Europe/Copenhagen", "America/New_York", "Asia/Tokyo"):
        assert await tenant_exec(db, u, event_sql(), tz=tz, **ok) == 1
    for tz in ("UTC+3", "Copenhagen", "Mars/Phobos", ""):
        with pg_error("22023", "invalid IANA timezone"):
            await tenant_exec(db, u, event_sql(), tz=tz, **ok)


async def test_recurrence_exceptions(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    base = {"u": a, "d": 1, "tz": "UTC", "via": "mini_app"}
    master = (
        await tenant_exec(db, a, event_sql(", recurrence_rule", ", 'FREQ=WEEKLY'") + " RETURNING id", **base)
    )[0][0]
    exc = event_sql(", parent_event_id, original_start, recurrence_rule", ", :p, :os, :rr")
    occurrence = datetime(2026, 9, 25, 9, 0, tzinfo=UTC)  # fixed: now() differs between transactions

    with pg_error("23514"):  # exception without the occurrence it replaces
        await tenant_exec(db, a, event_sql(", parent_event_id", ", :p"), p=master, **base)
    with pg_error("23514"):  # occurrence without a parent series
        await tenant_exec(db, a, event_sql(", original_start", ", now()"), **base)
    with pg_error("23514"):  # an exception cannot recur itself
        await tenant_exec(db, a, exc, p=master, os=occurrence, rr="FREQ=DAILY", **base)
    assert await tenant_exec(db, a, exc, p=master, os=occurrence, rr=None, **base) == 1
    with pg_error("23505"):  # one exception per occurrence
        await tenant_exec(db, a, exc, p=master, os=occurrence, rr=None, **base)
    with pg_error("23503"):  # a series of another tenant cannot be referenced
        await tenant_exec(db, b, exc, p=master, os=occurrence, rr=None, **{**base, "u": b})
    # deleting the series deletes its exceptions
    assert await tenant_exec(db, a, "DELETE FROM civ.events WHERE id = :p", p=master) == 1
    assert await tenant_exec(db, a, "SELECT count(*) FROM civ.events WHERE parent_event_id IS NOT NULL") == [(0,)]


async def test_event_belongs_to_a_topic_and_updated_at_is_touched(db, owner_engine):
    u = await new_user(owner_engine)
    discipline = await new_persona(owner_engine, u, "discipline")
    event = (
        await tenant_exec(
            db, u,
            event_sql(", persona_id", ", :p") + " RETURNING id",
            u=u, d=1, tz="UTC", via="mini_app", p=discipline,
        )
    )[0][0]  # fmt: skip
    before = (await tenant_exec(db, u, "SELECT updated_at FROM civ.events WHERE id = :e", e=event))[0][0]
    await tenant_exec(db, u, "UPDATE civ.events SET status = 'done' WHERE id = :e", e=event)
    row = (await tenant_exec(db, u, "SELECT updated_at, persona_id, status::text FROM civ.events WHERE id = :e", e=event))[0]
    assert row[0] > before and row[1] == discipline and row[2] == "done"
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.events WHERE persona_id = :p", p=discipline) == [(1,)]

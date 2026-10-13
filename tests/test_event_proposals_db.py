"""Events from conversation in PostgreSQL: the functions and tables of migration 0008. Real database; the application role (RLS enforced) acts,
the owner seeds and looks. The contract under test:
  * a proposal crosses tenants, so the application can NOT write it - only civ.propose_event / civ.respond_event_proposal can, and WHO is
    calling comes only from the tenant context (never from an argument);
  * every refusal answers the same 'unavailable' (the asker cannot tell a blocked pair from an unknown contact);
  * nothing reaches a calendar before the recipient accepts, and then BOTH calendars get a row, in ONE transaction, exactly once;
  * a decision is final and idempotent; limits cannot be raced past; drafts are an ordinary tenant table."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tests.helpers import new_contact, new_persona, new_user, owner_exec, pg_error, tenant_exec

UNAVAILABLE = ("unavailable", None, None)


def now() -> datetime:
    return datetime.now(UTC)


def soon(hours: float = 24) -> datetime:
    return now() + timedelta(hours=hours)


async def pair(owner_engine, *, a_tz: str = "Europe/Copenhagen", b_tz: str = "Asia/Tokyo"):
    """proposer A and recipient B, connected the way the handshake leaves them. -> (a, b, A's row for B, B's row for A)."""
    a, b = await new_user(owner_engine, tz=a_tz), await new_user(owner_engine, tz=b_tz)
    return a, b, await new_contact(owner_engine, a, b, alias="Bob"), await new_contact(owner_engine, b, a, alias="Alice")


async def propose(db, who: uuid.UUID, contact_id: uuid.UUID, title: str = "Chess", start: datetime | None = None, hours: float = 1.0):
    start = start or soon()
    rows = await tenant_exec(
        db, who, "SELECT * FROM civ.propose_event(:c, :t, :s, :e)", c=contact_id, t=title, s=start, e=start + timedelta(hours=hours)
    )
    return rows[0]


async def respond(db, who: uuid.UUID, proposal_id: uuid.UUID, accept: bool = True):
    rows = await tenant_exec(db, who, "SELECT * FROM civ.respond_event_proposal(:p, :a)", p=proposal_id, a=accept)
    return rows[0]


async def proposal(owner_engine, proposal_id: uuid.UUID) -> dict[str, Any]:
    rows = await owner_exec(
        owner_engine,
        "SELECT status, decided_at, proposer_event_id, recipient_event_id, expires_at, created_at, starts_at FROM civ.event_proposals WHERE id = :i",
        i=proposal_id,
    )
    keys = ("status", "decided_at", "proposer_event_id", "recipient_event_id", "expires_at", "created_at", "starts_at")
    return dict(zip(keys, rows[0], strict=True))


async def meetings(owner_engine, *users: uuid.UUID) -> list[tuple[Any, ...]]:
    return await owner_exec(
        owner_engine,
        "SELECT user_id, title, timezone, visibility::text, status::text, starts_at, ends_at FROM civ.events "
        "WHERE created_via = 'peer_proposal' AND user_id = ANY(:u) ORDER BY user_id, starts_at",
        u=list(users),
    )


async def notifications(owner_engine, user_id: uuid.UUID, kind: str | None = None) -> list[tuple[Any, ...]]:
    rows = await owner_exec(
        owner_engine,
        "SELECT kind, payload, idempotency_key, status::text FROM civ.notification_outbox "
        "WHERE user_id = :u AND (CAST(:k AS text) IS NULL OR kind = :k) AND kind LIKE 'event\\_proposal%' ORDER BY id",
        u=user_id,
        k=kind,
    )
    return [(r[0], json.loads(r[1]) if isinstance(r[1], str) else r[1], r[2], r[3]) for r in rows]  # jsonb may arrive as text


async def count_proposals(owner_engine, proposer: uuid.UUID) -> int:
    return (await owner_exec(owner_engine, "SELECT count(*) FROM civ.event_proposals WHERE user_id = :u", u=proposer))[0][0]


async def seed_proposal(
    owner_engine,
    a: uuid.UUID,
    b: uuid.UUID,
    *,
    title: str = "Old",
    starts: datetime | None = None,
    status: str = "pending",
    created: datetime | None = None,
    expires: datetime | None = None,
    decided: datetime | None = None,
) -> uuid.UUID:
    """A proposal written by the OWNER, with the clock under control (the functions always stamp 'now')."""
    created = created or now()
    starts = starts or soon(72)
    if status != "pending" and decided is None:
        decided = now()
    rows = await owner_exec(
        owner_engine,
        "INSERT INTO civ.event_proposals (user_id, recipient_user_id, title, starts_at, ends_at, status, created_at, expires_at, decided_at) "
        "VALUES (:a, :b, :t, CAST(:s AS timestamptz), CAST(:s AS timestamptz) + interval '1 hour', :st, :c, :x, :d) RETURNING id",
        a=a, b=b, t=title, s=starts, st=status, c=created, x=expires or created + timedelta(hours=48), d=decided,
    )  # fmt: skip
    return rows[0][0]


# ================================================================== privileges and who-is-calling


async def test_the_functions_are_for_the_application_only_and_run_as_the_owner(db, owner_engine):
    rows = await owner_exec(
        owner_engine,
        "SELECT p.proname, has_function_privilege('public', p.oid, 'EXECUTE'), has_function_privilege('ai_civ_app', p.oid, 'EXECUTE'), "
        "p.prosecdef, p.proconfig::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'civ' AND p.proname IN ('propose_event', 'respond_event_proposal', 'purge_events') ORDER BY 1",
    )
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        "propose_event": (False, True),
        "purge_events": (False, True),
        "respond_event_proposal": (False, True),
    }
    for name, _, _, definer, config in rows:
        assert definer and "search_path=civ, pg_temp" in config, name

    with pg_error("42704"):  # no tenant context: WHO calls is never an argument
        async with db.engine.begin() as conn:
            await conn.execute(text("SELECT * FROM civ.propose_event(gen_random_uuid(), 'x', now() + interval '1 day', now() + interval '2 days')"))
    with pg_error("42704"):
        async with db.engine.begin() as conn:
            await conn.execute(text("SELECT * FROM civ.respond_event_proposal(gen_random_uuid(), true)"))


async def test_the_application_cannot_write_a_proposal_and_a_third_person_cannot_read_it(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    c = await new_user(owner_engine)
    _, pid, _ = await propose(db, a, a_row)

    async def sql(who: uuid.UUID, statement: str) -> Any:
        return await tenant_exec(db, who, statement, a=a, b=b, p=pid)

    for who in (a, b):  # neither party may write the row directly: not the proposer, not the recipient
        with pg_error("42501"):
            await sql(who, "UPDATE civ.event_proposals SET status = 'accepted', decided_at = now() WHERE id = :p")
        with pg_error("42501"):
            await sql(who, "DELETE FROM civ.event_proposals WHERE id = :p")
    with pg_error("42501"):
        await sql(a, "INSERT INTO civ.event_proposals (user_id, recipient_user_id, title, starts_at, ends_at, expires_at) "
                     "VALUES (:a, :b, 'x', now() + interval '1 day', now() + interval '2 days', now() + interval '1 hour')")  # fmt: skip

    assert [r[0] for r in await sql(a, "SELECT id FROM civ.event_proposals")] == [pid]  # both parties read it...
    assert [r[0] for r in await sql(b, "SELECT id FROM civ.event_proposals")] == [pid]
    assert await sql(c, "SELECT id FROM civ.event_proposals") == []  # ...and nobody else


# ================================================================== propose_event


async def test_a_proposal_notifies_the_recipient_and_touches_no_calendar(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    outcome, pid, expires = await propose(db, a, a_row, "Партия в шахматы", start=soon(24 * 5))
    assert outcome == "proposed" and pid is not None

    row = await proposal(owner_engine, pid)
    assert row["status"] == "pending" and row["decided_at"] is None
    assert row["expires_at"] == expires == row["created_at"] + timedelta(hours=48)  # far start: 48 hours to answer
    assert await meetings(owner_engine, a, b) == []  # nothing is planned before the recipient says yes
    assert await notifications(owner_engine, a) == []  # the proposer is not notified of their own proposal
    [(kind, payload, key, status)] = await notifications(owner_engine, b)
    assert (kind, payload, key, status) == ("event_proposal", {"proposal_id": str(pid)}, f"event_proposal.{pid}", "pending")


async def test_a_proposal_for_a_near_start_expires_when_it_starts(db, owner_engine):
    a, _, a_row, _ = await pair(owner_engine)
    start = soon(3)
    _, pid, expires = await propose(db, a, a_row, start=start)
    assert expires == start == (await proposal(owner_engine, pid))["expires_at"]


async def test_a_replayed_proposal_returns_the_open_one(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    start = soon()
    first = await propose(db, a, a_row, "Chess", start)
    again = await propose(db, a, a_row, "Chess", start)
    assert first[0] == "proposed" and again == ("already_proposed", first[1], first[2])
    assert await count_proposals(owner_engine, a) == 1 and len(await notifications(owner_engine, b)) == 1
    other = await propose(db, a, a_row, "Chess", start + timedelta(hours=1))  # another start is another proposal
    assert other[0] == "proposed" and other[1] != first[1]


async def test_parallel_identical_proposals_make_exactly_one(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    start = soon()
    results = await asyncio.wait_for(asyncio.gather(*[propose(db, a, a_row, "Chess", start) for _ in range(6)]), timeout=60)
    assert sorted(r[0] for r in results) == ["already_proposed"] * 5 + ["proposed"]
    assert len({r[1] for r in results}) == 1
    assert await count_proposals(owner_engine, a) == 1 and len(await notifications(owner_engine, b)) == 1


async def _stranger(owner_engine, a, b, a_row):
    return uuid.uuid4()


async def _foreign_row(owner_engine, a, b, a_row):  # a contact row that belongs to somebody else is no handle for the caller
    return (await owner_exec(owner_engine, "SELECT id FROM civ.contacts WHERE user_id = :b", b=b))[0][0]


async def _they_removed_us(owner_engine, a, b, a_row):  # a one-sided pair
    await owner_exec(owner_engine, "DELETE FROM civ.contacts WHERE user_id = :b AND contact_user_id = :a", a=a, b=b)


async def _they_blocked_us(owner_engine, a, b, a_row):
    await owner_exec(owner_engine, "UPDATE civ.contacts SET status = 'blocked' WHERE user_id = :b AND contact_user_id = :a", a=a, b=b)


async def _we_blocked_them(owner_engine, a, b, a_row):
    await owner_exec(owner_engine, "UPDATE civ.contacts SET status = 'blocked' WHERE id = :r", r=a_row)


async def _not_accepted_yet(owner_engine, a, b, a_row):
    await owner_exec(owner_engine, "UPDATE civ.contacts SET status = 'pending' WHERE id = :r", r=a_row)


async def _recipient_group_frozen(owner_engine, a, b, a_row):
    await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :b", b=b)


async def _recipient_not_onboarded(owner_engine, a, b, a_row):
    await owner_exec(owner_engine, "UPDATE civ.users SET onboarding_state = 'in_progress' WHERE id = :b", b=b)


async def _caller_group_dormant(owner_engine, a, b, a_row):
    await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'dormant' WHERE id = :a", a=a)


@pytest.mark.parametrize(
    "mutate",
    [_stranger, _foreign_row, _they_removed_us, _they_blocked_us, _we_blocked_them, _not_accepted_yet, _recipient_group_frozen,
     _recipient_not_onboarded, _caller_group_dormant],
    ids=lambda f: f.__name__.strip("_"),
)  # fmt: skip
async def test_every_refusal_looks_the_same_and_leaves_nothing_behind(db, owner_engine, mutate):
    a, b, a_row, _ = await pair(owner_engine)
    handle = await mutate(owner_engine, a, b, a_row) or a_row
    assert await propose(db, a, handle) == UNAVAILABLE
    assert await count_proposals(owner_engine, a) == 0
    assert await notifications(owner_engine, b) == []


async def test_bad_input_is_an_error_not_an_outcome(db, owner_engine):
    a, _, a_row, _ = await pair(owner_engine)
    for title in ("", " Chess", "Chess ", "two\nlines", "tab\there", "‮evil", "x" * 81):
        with pg_error("22023"):
            await propose(db, a, a_row, title)
    assert (await propose(db, a, a_row, "x" * 80))[0] == "proposed"  # the limit is inclusive
    with pg_error("22023"):
        await propose(db, a, a_row, "Chess", start=now() - timedelta(hours=1))  # the past
    with pg_error("22023"):
        await propose(db, a, a_row, "Chess", start=now() + timedelta(days=400))  # beyond a year
    with pg_error("22023"):
        await propose(db, a, a_row, "Chess", hours=0)  # an empty period
    with pg_error("22023"):
        await propose(db, a, a_row, "Chess", hours=25)  # longer than a day
    with pg_error("22023"):
        await tenant_exec(db, a, "SELECT * FROM civ.propose_event(NULL, 'x', now() + interval '1 day', now() + interval '2 days')")
    assert await count_proposals(owner_engine, a) == 1


async def test_at_most_five_open_proposals_per_pair_and_a_replay_still_finds_its_own(db, owner_engine):
    a, _, a_row, _ = await pair(owner_engine)
    starts = [soon(24 + i) for i in range(6)]
    for start in starts[:5]:
        assert (await propose(db, a, a_row, "Chess", start))[0] == "proposed"
    assert await propose(db, a, a_row, "Chess", starts[5]) == ("too_many", None, None)
    assert (await propose(db, a, a_row, "Chess", starts[0]))[0] == "already_proposed"  # a replay is not a new proposal


async def test_parallel_proposals_cannot_race_past_the_open_limit(db, owner_engine):
    a, _, a_row, _ = await pair(owner_engine)
    results = await asyncio.wait_for(
        asyncio.gather(*[propose(db, a, a_row, "Chess", soon(24 + i)) for i in range(9)]), timeout=60
    )
    assert sorted(r[0] for r in results) == ["proposed"] * 5 + ["too_many"] * 4
    assert await count_proposals(owner_engine, a) == 5


async def test_the_daily_limits_per_pair_and_per_proposer(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    for i in range(10):  # ten decided proposals in the last 24 hours: nothing is open, but the pair has had enough
        await seed_proposal(owner_engine, a, b, title=f"Old {i}", status="declined", starts=soon(100 + i))
    assert await propose(db, a, a_row) == ("too_many", None, None)

    # 30 in 24 hours overall: three other recipients, ten each - then even a fresh contact is refused
    spare = await asyncio.gather(*[new_user(owner_engine) for _ in range(3)])
    for other in spare:
        for i in range(10):
            await seed_proposal(owner_engine, a, other, title=f"Old {i}", status="declined", starts=soon(100 + i))
    fresh = await new_user(owner_engine)
    fresh_row = await new_contact(owner_engine, a, fresh, alias="Eve")
    await new_contact(owner_engine, fresh, a, alias="Al")
    assert await propose(db, a, fresh_row) == ("too_many", None, None)

    old = now() - timedelta(hours=25)  # proposals older than a day no longer count
    await owner_exec(owner_engine, "UPDATE civ.event_proposals SET created_at = CAST(:o AS timestamptz), expires_at = CAST(:o AS timestamptz) + interval '1 hour', decided_at = CAST(:o AS timestamptz) + interval '1 minute' WHERE user_id = :a", o=old, a=a)  # fmt: skip
    assert (await propose(db, a, fresh_row))[0] == "proposed"


async def test_an_open_proposal_that_ran_out_of_time_does_not_block_a_new_one(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    start = soon(24 * 6)
    stale = await seed_proposal(
        owner_engine, a, b, title="Chess", starts=start, created=now() - timedelta(days=3), expires=now() - timedelta(days=1)
    )
    outcome, pid, _ = await propose(db, a, a_row, "Chess", start)
    assert outcome == "proposed" and pid != stale
    assert (await proposal(owner_engine, stale))["status"] == "expired"


# ================================================================== respond_event_proposal


async def test_accepting_puts_the_meeting_into_both_calendars_once(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine, a_tz="Europe/Copenhagen", b_tz="Asia/Tokyo")
    start = soon(30)
    _, pid, _ = await propose(db, a, a_row, "Партия в шахматы", start, hours=1.5)

    outcome, event_id = await respond(db, b, pid, True)
    assert outcome == "accepted" and event_id is not None

    mine, theirs = sorted(await meetings(owner_engine, a, b), key=lambda r: r[0] != b)  # b's row first
    assert tuple(mine[:5]) == (b, "Партия в шахматы", "Asia/Tokyo", "busy_only", "planned")  # each side in their OWN time zone
    assert tuple(theirs[:5]) == (a, "Партия в шахматы", "Europe/Copenhagen", "busy_only", "planned")
    assert mine[5] == theirs[5] == start and mine[6] == theirs[6] == start + timedelta(hours=1.5)

    row = await proposal(owner_engine, pid)
    assert row["status"] == "accepted" and row["decided_at"] is not None and row["recipient_event_id"] == event_id
    assert row["proposer_event_id"] is not None and row["proposer_event_id"] != event_id
    [(kind, payload, key, _)] = await notifications(owner_engine, a, "event_proposal_answer")  # the author is told
    assert (kind, payload, key) == ("event_proposal_answer", {"proposal_id": str(pid), "accepted": True}, f"event_proposal_answer.{pid}")

    assert await respond(db, b, pid, True) == ("already_accepted", event_id)  # a second click: the same answer, no second event
    assert await respond(db, b, pid, False) == ("already_accepted", event_id)  # a decision is final
    assert len(await meetings(owner_engine, a, b)) == 2 and len(await notifications(owner_engine, a, "event_proposal_answer")) == 1


async def test_declining_creates_no_event_and_tells_the_author(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    _, pid, _ = await propose(db, a, a_row)
    assert await respond(db, b, pid, False) == ("declined", None)
    assert await meetings(owner_engine, a, b) == []
    row = await proposal(owner_engine, pid)
    assert row["status"] == "declined" and row["decided_at"] is not None
    [(_, payload, _, _)] = await notifications(owner_engine, a, "event_proposal_answer")
    assert payload == {"proposal_id": str(pid), "accepted": False}
    assert await respond(db, b, pid, False) == ("already_declined", None)
    assert await respond(db, b, pid, True) == ("already_declined", None)  # final: a declined proposal cannot be revived
    assert await meetings(owner_engine, a, b) == []


async def test_only_the_recipient_can_answer(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    stranger = await new_user(owner_engine)
    _, pid, _ = await propose(db, a, a_row)
    unavailable = ("unavailable", None)
    assert await respond(db, a, pid, True) == unavailable  # not even the author
    assert await respond(db, stranger, pid, True) == unavailable
    assert await respond(db, b, uuid.uuid4(), True) == unavailable
    with pg_error("22023"):
        await tenant_exec(db, b, "SELECT * FROM civ.respond_event_proposal(NULL, true)")
    with pg_error("22023"):
        await tenant_exec(db, b, "SELECT * FROM civ.respond_event_proposal(:p, NULL)", p=pid)
    assert (await proposal(owner_engine, pid))["status"] == "pending" and await meetings(owner_engine, a, b) == []


async def test_an_expired_proposal_cannot_be_accepted(db, owner_engine):
    a, b, *_ = await pair(owner_engine)
    pid = await seed_proposal(owner_engine, a, b, created=now() - timedelta(days=3), expires=now() - timedelta(hours=1))
    assert await respond(db, b, pid, True) == ("expired", None)
    assert (await proposal(owner_engine, pid))["status"] == "expired"  # the expiry is recorded, not rolled back
    assert await respond(db, b, pid, True) == ("expired", None)
    assert await meetings(owner_engine, a, b) == []


@pytest.mark.parametrize("who", ["recipient", "proposer"])
async def test_a_pair_that_is_no_longer_mutual_cannot_be_answered(db, owner_engine, who):
    a, b, a_row, b_row = await pair(owner_engine)
    _, pid, _ = await propose(db, a, a_row)
    await owner_exec(owner_engine, "UPDATE civ.contacts SET status = 'blocked' WHERE id = :r", r=b_row if who == "recipient" else a_row)
    assert await respond(db, b, pid, True) == ("unavailable", None)
    assert (await proposal(owner_engine, pid))["status"] == "pending" and await meetings(owner_engine, a, b) == []


async def test_a_frozen_group_cannot_answer_either(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    _, pid, _ = await propose(db, a, a_row)
    await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :a", a=a)
    assert await respond(db, b, pid, True) == ("unavailable", None)
    assert await meetings(owner_engine, a, b) == []


async def test_parallel_acceptances_create_exactly_one_event_per_side(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    _, pid, _ = await propose(db, a, a_row)
    results = await asyncio.wait_for(asyncio.gather(*[respond(db, b, pid, True) for _ in range(8)]), timeout=60)
    assert sorted(r[0] for r in results) == ["accepted"] + ["already_accepted"] * 7
    assert len({r[1] for r in results}) == 1
    assert len(await meetings(owner_engine, a, b)) == 2 and len(await notifications(owner_engine, a, "event_proposal_answer")) == 1


async def test_accept_against_decline_has_exactly_one_winner(db, owner_engine):
    for _ in range(3):
        a, b, a_row, _ = await pair(owner_engine)
        _, pid, _ = await propose(db, a, a_row)
        accepted, declined = await asyncio.wait_for(asyncio.gather(respond(db, b, pid, True), respond(db, b, pid, False)), timeout=60)
        final = (await proposal(owner_engine, pid))["status"]
        if final == "accepted":
            assert accepted[0] == "accepted" and declined[0] == "already_accepted" and len(await meetings(owner_engine, a, b)) == 2
        else:
            assert final == "declined" and declined[0] == "declined" and accepted[0] == "already_declined"
            assert await meetings(owner_engine, a, b) == []
        assert len(await notifications(owner_engine, a, "event_proposal_answer")) == 1


async def test_crossing_proposals_do_not_deadlock(db, owner_engine):
    a, b, a_row, b_row = await pair(owner_engine)
    for _ in range(4):
        calls = [propose(db, a, a_row, "Chess", soon(24)), propose(db, b, b_row, "Tea", soon(25)), propose(db, a, a_row, "Go", soon(26)),
                 propose(db, b, b_row, "Run", soon(27))]  # fmt: skip
        results = await asyncio.wait_for(asyncio.gather(*calls), timeout=60)
        assert {r[0] for r in results} <= {"proposed", "already_proposed", "too_many"}
    assert await count_proposals(owner_engine, a) >= 1 and await count_proposals(owner_engine, b) >= 1


# ================================================================== housekeeping


async def test_purge_events_expires_then_deletes_old_decided_rows(db, owner_engine):
    a, b, *_ = await pair(owner_engine)
    persona = await new_persona(owner_engine, a)
    old = now() - timedelta(days=60)
    stale_open = await seed_proposal(owner_engine, a, b, title="A", created=now() - timedelta(days=3), expires=now() - timedelta(days=1))
    fresh_open = await seed_proposal(owner_engine, a, b, title="B")
    old_done = await seed_proposal(owner_engine, a, b, title="C", status="declined", created=old, decided=old)
    new_done = await seed_proposal(owner_engine, a, b, title="D", status="declined")

    async def draft(title: str, status: str, *, created: datetime, expires: datetime, decided: datetime | None) -> uuid.UUID:
        return (
            await owner_exec(
                owner_engine,
                "INSERT INTO civ.event_drafts (user_id, persona_id, title, starts_at, ends_at, timezone, status, created_at, expires_at, decided_at) "
                "VALUES (:u, :p, :t, CAST(:c AS timestamptz), CAST(:c AS timestamptz) + interval '1 hour', 'UTC', :s, :c, :x, :d) RETURNING id",
                u=a, p=persona, t=title, s=status, c=created, x=expires, d=decided,
            )
        )[0][0]  # fmt: skip

    stale_draft = await draft("a", "pending", created=now() - timedelta(days=3), expires=now() - timedelta(days=2), decided=None)
    fresh_draft = await draft("b", "pending", created=now(), expires=now() + timedelta(days=1), decided=None)
    old_draft = await draft("c", "dismissed", created=old, expires=old + timedelta(days=1), decided=old)
    new_draft = await draft("d", "confirmed", created=now(), expires=now() + timedelta(days=1), decided=now())

    [counts] = await tenant_exec(db, a, "SELECT * FROM civ.purge_events(30, 30)")  # all tenants: other tests' leftovers may add to the counts
    assert all(n >= 1 for n in counts)

    async def statuses(table: str, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
        rows = await owner_exec(owner_engine, f"SELECT id, status FROM civ.{table} WHERE id = ANY(:i)", i=ids)
        return {r[0]: r[1] for r in rows}

    assert await statuses("event_proposals", [stale_open, fresh_open, old_done, new_done]) == {
        stale_open: "expired", fresh_open: "pending", new_done: "declined"
    }  # fmt: skip
    assert await statuses("event_drafts", [stale_draft, fresh_draft, old_draft, new_draft]) == {
        stale_draft: "expired", fresh_draft: "pending", new_draft: "confirmed"
    }  # fmt: skip


# ================================================================== drafts: an ordinary tenant table


async def _draft(db, who: uuid.UUID, *, title: str = "Rest", persona: uuid.UUID | None = None, tz: str = "Europe/Copenhagen",
                 hours: float = 2, status: str = "pending", decided: str = "NULL", event: uuid.UUID | None = None, owner: uuid.UUID | None = None):  # fmt: skip
    return await tenant_exec(
        db, who,
        "INSERT INTO civ.event_drafts (user_id, persona_id, title, starts_at, ends_at, timezone, status, decided_at, event_id, expires_at) "
        f"VALUES (:o, :p, :t, now(), now() + make_interval(secs => :h), :tz, :s, {decided}, :e, now() + interval '1 day') RETURNING id",
        o=owner or who, p=persona, t=title, tz=tz, h=hours * 3600, s=status, e=event,
    )  # fmt: skip


async def test_drafts_are_private_to_their_tenant(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    [(draft_id,)] = await _draft(db, a)
    assert [r[0] for r in await tenant_exec(db, a, "SELECT id FROM civ.event_drafts")] == [draft_id]
    assert await tenant_exec(db, b, "SELECT id FROM civ.event_drafts") == []
    assert await tenant_exec(db, b, "UPDATE civ.event_drafts SET status = 'dismissed', decided_at = now() WHERE id = :i", i=draft_id) == 0
    assert await tenant_exec(db, b, "DELETE FROM civ.event_drafts WHERE id = :i", i=draft_id) == 0
    with pg_error("42501"):  # writing a row of somebody else is refused by RLS
        await _draft(db, b, owner=a)


async def test_draft_rules_are_kept_by_the_database(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    for bad in ("", "two\nlines", "x" * 81, " lead"):
        with pg_error("23514"):
            await _draft(db, a, title=bad)
    with pg_error("23514"):
        await _draft(db, a, hours=25)  # longer than a day
    with pg_error("23514"):
        await _draft(db, a, hours=0)
    with pg_error("23514"):
        await _draft(db, a, status="bogus")
    with pg_error("23514"):
        await _draft(db, a, decided="now()")  # an open draft has not been decided
    with pg_error("23514"):
        await _draft(db, a, status="dismissed")  # a decided one has
    with pg_error("23514"):
        await _draft(db, a, status="dismissed", decided="now()", event=uuid.uuid4())  # only a confirmed draft points at an event
    with pg_error("22023"):
        await _draft(db, a, tz="Mars/Olympus")
    with pg_error("23503"):  # a topic of another tenant cannot be referenced (composite FK)
        await _draft(db, a, persona=await new_persona(owner_engine, b))
    await _draft(db, a, title="Same")
    with pg_error("23505"):
        await tenant_exec(db, a, "INSERT INTO civ.event_drafts (user_id, title, starts_at, ends_at, timezone, expires_at) "
                                 "SELECT user_id, title, starts_at, ends_at, timezone, expires_at FROM civ.event_drafts WHERE title = 'Same'")  # fmt: skip


async def test_confirming_a_draft_is_one_transaction_that_ends_in_an_event(db, owner_engine):
    a = await new_user(owner_engine)
    persona = await new_persona(owner_engine, a)
    [(draft_id,)] = await _draft(db, a, persona=persona, title="Смотреть стенку", hours=2)
    async with db.tenant_session(a) as session:  # what the button's handler does
        draft = (await session.execute(text("SELECT title, starts_at, ends_at, timezone, persona_id FROM civ.event_drafts WHERE id = :i AND status = 'pending' FOR UPDATE"), {"i": draft_id})).one()  # fmt: skip
        event_id = (
            await session.execute(
                text("INSERT INTO civ.events (user_id, persona_id, title, starts_at, ends_at, timezone, created_via) "
                     "VALUES (:u, :p, :t, :s, :e, :z, 'bot_confirmed') RETURNING id"),
                {"u": a, "p": draft.persona_id, "t": draft.title, "s": draft.starts_at, "e": draft.ends_at, "z": draft.timezone},
            )
        ).scalar_one()  # fmt: skip
        await session.execute(text("UPDATE civ.event_drafts SET status = 'confirmed', decided_at = now(), event_id = :e WHERE id = :i"), {"e": event_id, "i": draft_id})  # fmt: skip
    [(status, linked)] = await owner_exec(owner_engine, "SELECT status, event_id FROM civ.event_drafts WHERE id = :i", i=draft_id)
    assert (status, linked) == ("confirmed", event_id)

    await owner_exec(owner_engine, "DELETE FROM civ.events WHERE id = :e", e=event_id)  # deleting the event leaves the draft, unlinked
    assert (await owner_exec(owner_engine, "SELECT status, event_id FROM civ.event_drafts WHERE id = :i", i=draft_id))[0] == ("confirmed", None)


# ================================================================== the source label of an event


async def test_peer_proposal_is_a_known_source_and_nothing_else_is(owner_engine):
    a = await new_user(owner_engine)
    sql = (
        "INSERT INTO civ.events (user_id, title, starts_at, ends_at, timezone, created_via) "
        "VALUES (:u, 'x', now(), now() + interval '1 hour', 'UTC', :v)"
    )
    for source in ("mini_app", "bot_confirmed", "peer_proposal"):
        assert await owner_exec(owner_engine, sql, u=a, v=source) == 1
    with pg_error("23514"):
        await owner_exec(owner_engine, sql, u=a, v="telepathy")

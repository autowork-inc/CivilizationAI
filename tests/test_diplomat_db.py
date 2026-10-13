"""Module 5, step 4 in PostgreSQL: the Diplomat functions of migration 0007. Real database; the application role (RLS enforced) asks,
the owner seeds and looks. The contract under test (CLAUDE.md, "Социальный граф / ACL"): BOTH rows of the pair accepted, a grant in force,
an audit row in the same transaction, a refusal as a RETURN VALUE that does not say why, and nothing but busy time / a confirmed mood out."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from tests.helpers import new_contact, new_user, owner_exec, pg_error, tenant_exec

FREE = "calendar_free_busy"
MOOD = "emotional_state_label"
UNAVAILABLE = {"decision": "unavailable"}


def now() -> datetime:
    return datetime.now(UTC)


def decode(raw: Any) -> dict[str, Any]:
    return json.loads(raw) if isinstance(raw, str) else raw


async def ask_free(db, who: uuid.UUID, contact_id: uuid.UUID, start: datetime | None = None, end: datetime | None = None) -> dict[str, Any]:
    start = start or now()
    end = end or start + timedelta(days=30)
    rows = await tenant_exec(db, who, "SELECT civ.diplomat_free_busy(:c, :f, :t)", c=contact_id, f=start, t=end)
    return decode(rows[0][0])


async def ask_mood(db, who: uuid.UUID, contact_id: uuid.UUID) -> dict[str, Any]:
    return decode((await tenant_exec(db, who, "SELECT civ.diplomat_mood(:c)", c=contact_id))[0][0])


async def pair(owner_engine, *, granted: str | None = FREE, constraints: dict[str, Any] | None = None):
    """asker A and owner B, connected the way the handshake leaves them. B granted A ``granted``. -> (a, b, A's row for B, B's row for A)."""
    a, b = await new_user(owner_engine, tz="Europe/Copenhagen"), await new_user(owner_engine, tz="UTC")
    a_row, b_row = await new_contact(owner_engine, a, b, alias="Bob"), await new_contact(owner_engine, b, a, alias="Alice")
    if granted is not None:
        await grant(owner_engine, b, a, granted, constraints)
    return a, b, a_row, b_row


async def grant(owner_engine, owner: uuid.UUID, grantee: uuid.UUID, scope: str = FREE, constraints: dict[str, Any] | None = None) -> None:
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.access_grants (user_id, grantee_user_id, scope, constraints) "
        "VALUES (:o, :g, CAST(:s AS civ.acl_scope), CAST(:c AS jsonb))",
        o=owner, g=grantee, s=scope, c=json.dumps(constraints or {}),
    )  # fmt: skip


async def audit(owner_engine, owner: uuid.UUID, asker: uuid.UUID | None = None) -> list[tuple[str, str]]:
    rows = await owner_exec(
        owner_engine,
        "SELECT scope::text, decision FROM civ.diplomat_audit_log WHERE user_id = :o "
        "AND (CAST(:a AS uuid) IS NULL OR requester_user_id = :a) ORDER BY id",
        o=owner, a=asker,
    )  # fmt: skip
    return [(r[0], r[1]) for r in rows]


async def new_event(
    owner_engine,
    user_id: uuid.UUID,
    *,
    title: str = "Secret meeting",
    starts: datetime,
    hours: float = 1,
    visibility: str = "busy_only",
    status: str = "planned",
    rule: str | None = None,
    tz: str = "Europe/Copenhagen",
    parent: uuid.UUID | None = None,
    original_start: datetime | None = None,
    description: str | None = "agenda: fire the CTO",
) -> uuid.UUID:
    rows = await owner_exec(
        owner_engine,
        "INSERT INTO civ.events (user_id, title, description, starts_at, ends_at, timezone, status, recurrence_rule, parent_event_id, "
        "original_start, visibility, created_via) VALUES (:u, :t, :d, :s, :e, :tz, CAST(:st AS civ.event_status), :r, :p, :o, "
        "CAST(:v AS civ.event_visibility), 'mini_app') RETURNING id",
        u=user_id, t=title, d=description, s=starts, e=starts + timedelta(hours=hours), tz=tz, st=status, r=rule, p=parent,
        o=original_start, v=visibility,
    )  # fmt: skip
    return rows[0][0]


async def confirmed_state(
    owner_engine, user_id: uuid.UUID, mood: str | None, *, priority: int = 80, hours: float = 24, status: str = "active"
) -> uuid.UUID:
    """A mood state the user CONFIRMED: an approved proposal carrying the same mood, then the state that points at it."""
    pending = (
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.pending_facts (user_id, subject, change, emotion_label, emotion_intensity, emotion_priority, "
            "emotion_expires_at, emotion_mood, evidence, source_message_ids, status, decided_at, idempotency_key) "
            "VALUES (:u, 'emotional_state', 'create', 'private feeling', 8, :p, now() + interval '2 days', CAST(:m AS civ.shared_mood), "
            "'said so', ARRAY[gen_random_uuid()], 'approved', now(), :i) RETURNING id",
            u=user_id, p=priority, m=mood, i=uuid.uuid4().hex,
        )
    )[0][0]  # fmt: skip
    return (
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, source_pending_id, mood, status, started_at, "
            "expires_at) VALUES (:u, 'private feeling', 8, :p, 'user_confirmed', :pf, CAST(:m AS civ.shared_mood), "
            "CAST(:st AS civ.emotion_status), now() - interval '1 minute', now() + make_interval(secs => :h)) RETURNING id",
            u=user_id, p=priority, pf=pending, m=mood, st=status, h=hours * 3600,
        )
    )[0][0]  # fmt: skip


# ================================================================== privileges


async def test_the_functions_are_for_the_application_only_and_the_gate_is_internal(db, owner_engine):
    rows = await owner_exec(
        owner_engine,
        "SELECT p.proname, has_function_privilege('public', p.oid, 'EXECUTE'), has_function_privilege('ai_civ_app', p.oid, 'EXECUTE'), "
        "p.prosecdef, p.proconfig::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "WHERE n.nspname = 'civ' AND p.proname LIKE 'diplomat\\_%' ORDER BY 1",
    )
    assert {r[0]: (r[1], r[2]) for r in rows} == {
        "diplomat_authorize": (False, False),  # only the other functions (running as the owner) may call the gate
        "diplomat_free_busy": (False, True),
        "diplomat_mood": (False, True),
    }
    for name, _, _, definer, config in rows:
        assert definer and "search_path=civ, pg_temp" in config, name

    with pg_error("42704"):  # no tenant context: WHO asks is never an argument
        async with db.engine.begin() as conn:
            await conn.execute(text("SELECT civ.diplomat_mood(gen_random_uuid())"))
    a, *_ = await pair(owner_engine)
    with pg_error("42501"):  # the gate itself is not callable by the application
        await tenant_exec(db, a, "SELECT * FROM civ.diplomat_authorize(gen_random_uuid(), 'calendar_free_busy')")


async def test_the_application_cannot_write_the_audit_log(db, owner_engine):
    a, b, *_ = await pair(owner_engine)
    with pg_error("42501"):
        await tenant_exec(db, a, "INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision) VALUES (:b, :a, 'calendar_free_busy', 'allowed')", a=a, b=b)  # fmt: skip


async def test_bad_arguments_raise_and_leave_no_audit_row(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    t = now()
    with pg_error("22023"):
        await tenant_exec(db, a, "SELECT civ.diplomat_free_busy(NULL, :f, :t)", f=t, t=t + timedelta(days=1))
    with pg_error("22023"):
        await ask_free(db, a, a_row, t, t)  # an empty period
    with pg_error("22023"):
        await tenant_exec(db, a, "SELECT civ.diplomat_mood(NULL)")
    assert await audit(owner_engine, b) == []


# ================================================================== the decision and the audit


async def test_an_allowed_ask_answers_and_writes_one_audit_row_in_the_same_transaction(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    await new_event(owner_engine, b, starts=now() + timedelta(hours=2), visibility="busy_only")
    payload = await ask_free(db, a, a_row)
    assert payload["decision"] == "allowed" and len(payload["events"]) == 1
    assert await audit(owner_engine, b, a) == [(FREE, "allowed")]
    assert await audit(owner_engine, a) == []  # the log belongs to the data OWNER


async def test_nothing_that_identifies_anything_leaves_the_owners_tenant(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    event = await new_event(owner_engine, b, starts=now() + timedelta(hours=2), title="Therapy with Dr Moreau", visibility="shared")
    series = await new_event(owner_engine, b, starts=now() + timedelta(days=1), rule="FREQ=DAILY", title="Secret daily")
    await new_event(owner_engine, b, starts=now() + timedelta(days=2), parent=series, original_start=now() + timedelta(days=2), title="Moved secret")
    payload = await ask_free(db, a, a_row)
    blob = json.dumps(payload)
    for secret in ("Therapy", "Moreau", "agenda", "fire the CTO", "Secret", "Moved secret", str(event), str(series), str(b)):
        assert secret not in blob, secret
    assert {item["kind"] for item in payload["events"]} == {"single", "series", "exception"}


async def test_no_grant_is_denied_and_audited_as_denied(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine, granted=None)
    assert await ask_free(db, a, a_row) == UNAVAILABLE
    assert await audit(owner_engine, b, a) == [(FREE, "denied")]


async def test_a_grant_for_another_scope_does_not_open_this_one(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine, granted=MOOD)
    assert await ask_free(db, a, a_row) == UNAVAILABLE
    assert await audit(owner_engine, b, a) == [(FREE, "denied")]
    assert (await ask_mood(db, a, a_row))["decision"] == "allowed"


async def _break(owner_engine, how: str, a: uuid.UUID, b: uuid.UUID, a_row: uuid.UUID) -> None:
    updates = {
        "grant_revoked": "UPDATE civ.access_grants SET revoked_at = now() + interval '0 seconds' WHERE user_id = :b",
        "grant_expired": "UPDATE civ.access_grants SET granted_at = now() - interval '2 days', expires_at = now() - interval '1 day' WHERE user_id = :b",
    }
    if how in updates:
        await owner_exec(owner_engine, updates[how], a=a, b=b)
    elif how == "my_row_pending":
        await owner_exec(owner_engine, "UPDATE civ.contacts SET status = 'pending' WHERE id = :r", r=a_row)
    elif how == "my_row_blocked":
        await owner_exec(owner_engine, "UPDATE civ.contacts SET status = 'blocked' WHERE id = :r", r=a_row)
    elif how == "their_row_removed":
        await owner_exec(owner_engine, "DELETE FROM civ.contacts WHERE user_id = :b AND contact_user_id = :a", a=a, b=b)
    elif how == "their_row_blocked":
        await owner_exec(owner_engine, "UPDATE civ.contacts SET status = 'blocked' WHERE user_id = :b AND contact_user_id = :a", a=a, b=b)
    elif how == "their_row_pending":
        await owner_exec(owner_engine, "UPDATE civ.contacts SET status = 'pending' WHERE user_id = :b AND contact_user_id = :a", a=a, b=b)
    elif how == "owner_onboarding":
        await owner_exec(owner_engine, "UPDATE civ.users SET onboarding_state = 'in_progress' WHERE id = :b", b=b)
    elif how == "owner_group_frozen":
        await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :b", b=b)
    elif how == "owner_group_dormant":
        await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'dormant' WHERE id = :b", b=b)
    else:
        raise AssertionError(how)


# (how the access was lost, does the OWNER still get a "denied" row in their log?)
REASONS = [
    ("grant_revoked", True),
    ("grant_expired", True),
    ("my_row_pending", False),
    ("my_row_blocked", False),
    ("their_row_removed", False),
    ("their_row_blocked", False),
    ("their_row_pending", False),
    ("owner_onboarding", False),
    ("owner_group_frozen", False),
    ("owner_group_dormant", False),
]


@pytest.mark.parametrize(("how", "audited"), REASONS)
async def test_every_reason_for_refusal_answers_exactly_the_same(db, owner_engine, how, audited):
    a, b, a_row, _ = await pair(owner_engine)
    await new_event(owner_engine, b, starts=now() + timedelta(hours=2))
    assert (await ask_free(db, a, a_row))["decision"] == "allowed"  # it worked before the access was lost
    before = len(await audit(owner_engine, b, a))
    await _break(owner_engine, how, a, b, a_row)
    # the asker gets the same JSON whatever the reason: they cannot learn that they were blocked, removed, revoked or rate limited
    assert await ask_free(db, a, a_row) == UNAVAILABLE
    assert (await ask_mood(db, a, a_row)) == UNAVAILABLE
    after = await audit(owner_engine, b, a)
    # a real, mutual contact who asked without a grant is recorded as a refusal; every other case leaves the owner's log alone
    assert len(after) - before == (2 if audited else 0)  # one refusal per ask (the calendar and the mood)
    if audited:
        assert after[before:] == [(FREE, "denied"), (MOOD, "denied")]


async def test_somebody_elses_contact_id_is_simply_unavailable_and_unaudited(db, owner_engine):
    a, b, a_row, b_row = await pair(owner_engine)
    c = await new_user(owner_engine)
    assert await ask_free(db, c, a_row) == UNAVAILABLE  # a stranger holding A's handle
    assert await ask_free(db, b, a_row) == UNAVAILABLE  # even B: the handle is only valid for its owner
    assert await ask_free(db, a, uuid.uuid4()) == UNAVAILABLE
    assert await audit(owner_engine, b) == [] and await audit(owner_engine, c) == [] and await audit(owner_engine, a) == []
    assert (await ask_free(db, b, b_row)) == UNAVAILABLE  # B asking A: A granted nothing
    assert await audit(owner_engine, a, b) == [(FREE, "denied")]


async def test_unblocking_returns_the_contact_but_not_the_grant(db, owner_engine):
    a, b, a_row, b_row = await pair(owner_engine)
    assert (await ask_free(db, a, a_row))["decision"] == "allowed"
    assert (await tenant_exec(db, b, "SELECT civ.block_contact(:c)", c=b_row))[0][0] == "ok"
    assert await ask_free(db, a, a_row) == UNAVAILABLE
    assert (await tenant_exec(db, b, "SELECT civ.unblock_contact(:c)", c=b_row))[0][0] == "ok"
    assert await ask_free(db, a, a_row) == UNAVAILABLE  # default-deny again: the owner has to grant anew
    assert (await tenant_exec(db, b, "SELECT civ.set_grant(:c, 'calendar_free_busy', true)", c=b_row))[0][0] == "ok"
    assert (await ask_free(db, a, a_row))["decision"] == "allowed"


# ================================================================== rate limit


async def seed_audit(owner_engine, owner: uuid.UUID, asker: uuid.UUID, n: int, *, age: timedelta = timedelta(minutes=1), scope: str = FREE) -> None:
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision, created_at) "
        "SELECT :o, :a, CAST(:s AS civ.acl_scope), 'allowed', now() - CAST(:age AS interval) FROM generate_series(1, :n)",
        o=owner, a=asker, s=scope, age=age, n=n,
    )  # fmt: skip


async def test_the_thirty_first_ask_in_an_hour_is_refused_and_writes_nothing(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    await seed_audit(owner_engine, b, a, 29)
    assert (await ask_free(db, a, a_row))["decision"] == "allowed"  # the 30th
    assert len(await audit(owner_engine, b, a)) == 30
    assert await ask_free(db, a, a_row) == UNAVAILABLE  # the 31st
    assert len(await audit(owner_engine, b, a)) == 30  # a limited ask cannot flood the owner's log either
    await grant(owner_engine, b, a, MOOD)
    assert (await ask_mood(db, a, a_row))["decision"] == "allowed"  # the limit is per scope


async def test_asks_older_than_an_hour_do_not_count(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    await seed_audit(owner_engine, b, a, 40, age=timedelta(hours=2))
    assert (await ask_free(db, a, a_row))["decision"] == "allowed"


async def test_denied_asks_count_too_so_a_stranger_cannot_flood_the_log(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine, granted=None)
    for _ in range(35):
        await ask_free(db, a, a_row)
    assert len(await audit(owner_engine, b, a)) == 30


async def test_parallel_asks_cannot_beat_the_limit(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    await seed_audit(owner_engine, b, a, 24)
    results = await asyncio.gather(*(ask_free(db, a, a_row) for _ in range(10)))
    assert sum(r["decision"] == "allowed" for r in results) == 6
    assert len(await audit(owner_engine, b, a)) == 30


# ================================================================== what the calendar shows


async def test_private_and_cancelled_events_do_not_exist_for_the_asker(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    t = now() + timedelta(hours=3)
    await new_event(owner_engine, b, starts=t, visibility="private")
    await new_event(owner_engine, b, starts=t, status="cancelled")
    await new_event(owner_engine, b, starts=t, status="rescheduled")
    assert (await ask_free(db, a, a_row))["events"] == []
    await new_event(owner_engine, b, starts=t, visibility="busy_only")
    await new_event(owner_engine, b, starts=t + timedelta(hours=5), visibility="shared")
    assert len((await ask_free(db, a, a_row))["events"]) == 2


async def test_the_window_is_clipped_to_the_horizon_of_the_grant(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine, constraints={"horizon_days": 2})
    await new_event(owner_engine, b, starts=now() + timedelta(days=1))
    await new_event(owner_engine, b, starts=now() + timedelta(days=3))
    payload = await ask_free(db, a, a_row, now(), now() + timedelta(days=30))
    assert payload["window"]["horizon_days"] == 2 and len(payload["events"]) == 1
    end = datetime.fromisoformat(payload["window"]["to"])
    assert now() + timedelta(days=1, hours=23) < end <= now() + timedelta(days=2, minutes=1)


@pytest.mark.parametrize(
    ("constraints", "horizon"),
    [({}, 7), ({"horizon_days": 99}, 14), ({"horizon_days": 0}, 1), ({"horizon_days": "lots"}, 7), ({"horizon_days": 3.9}, 3), ({"horizon_days": -5}, 1)],
)
async def test_the_horizon_is_sanitised_inside_the_database(db, owner_engine, constraints, horizon):
    a, _, a_row, _ = await pair(owner_engine, constraints=constraints)
    assert (await ask_free(db, a, a_row))["window"]["horizon_days"] == horizon


async def test_the_past_and_an_inverted_period_show_nothing(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    await new_event(owner_engine, b, starts=now() - timedelta(days=2))
    payload = await ask_free(db, a, a_row, now() - timedelta(days=5), now() - timedelta(days=1))
    assert payload["decision"] == "allowed" and payload["events"] == []
    assert payload["window"]["from"] == payload["window"]["to"]  # the answer says: nothing of that period can be shown


async def test_a_running_event_overlapping_the_start_of_the_window_counts(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    await new_event(owner_engine, b, starts=now() - timedelta(minutes=30), hours=2)
    assert len((await ask_free(db, a, a_row))["events"]) == 1


async def test_series_and_their_exceptions(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    start = now() + timedelta(days=1)
    series = await new_event(owner_engine, b, starts=start, rule="FREQ=DAILY")
    # day 2: moved; day 3: cancelled; day 4: made private (suppressed WITHOUT revealing the new time)
    await new_event(owner_engine, b, starts=start + timedelta(days=1, hours=3), parent=series, original_start=start + timedelta(days=1))
    await new_event(owner_engine, b, starts=start + timedelta(days=2), parent=series, original_start=start + timedelta(days=2), status="cancelled")
    await new_event(owner_engine, b, starts=start + timedelta(days=3, hours=5), parent=series, original_start=start + timedelta(days=3), visibility="private")
    payload = await ask_free(db, a, a_row, now(), now() + timedelta(days=10))
    events = payload["events"]
    assert [e["kind"] for e in events].count("series") == 1
    exceptions = [e for e in events if e["kind"] == "exception"]
    assert len(exceptions) == 3
    suppressed = sorted(e["suppress"] for e in exceptions)
    assert suppressed == [False, True, True]
    for e in exceptions:
        assert e["series_ref"] == 1
        assert (e["starts_at"] is None) == e["suppress"]  # a suppressed occurrence carries no time at all


async def test_a_private_series_stays_invisible_but_its_shared_exception_is_an_event_of_its_own(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    start = now() + timedelta(days=1)
    series = await new_event(owner_engine, b, starts=start, rule="FREQ=DAILY", visibility="private")
    await new_event(owner_engine, b, starts=start + timedelta(days=1), parent=series, original_start=start + timedelta(days=1), visibility="private")
    assert (await ask_free(db, a, a_row))["events"] == []  # a private exception of a private series: nothing
    await new_event(owner_engine, b, starts=start + timedelta(days=2), parent=series, original_start=start + timedelta(days=2), visibility="shared")
    events = (await ask_free(db, a, a_row))["events"]
    assert [e["kind"] for e in events] == ["single"]


async def test_events_of_other_people_never_mix_in(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine)
    c = await new_user(owner_engine)
    await new_event(owner_engine, c, starts=now() + timedelta(hours=2))
    await new_event(owner_engine, a, starts=now() + timedelta(hours=2))  # the asker's own calendar is not the contact's
    assert (await ask_free(db, a, a_row))["events"] == []


# ================================================================== mood


async def test_only_a_confirmed_mood_in_force_is_shared(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine, granted=MOOD)
    assert await ask_mood(db, a, a_row) == {"decision": "allowed", "mood": None}  # allowed, but nothing to say
    # a declared and an inferred state are never shared, even with a mood word on the proposal side
    await owner_exec(owner_engine, "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, expires_at) VALUES (:b, 'x', 5, 50, 'user_declared', now() + interval '1 day')", b=b)  # fmt: skip
    await owner_exec(owner_engine, "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, expires_at) VALUES (:b, 'y', 5, 50, 'inferred', now() + interval '1 day')", b=b)  # fmt: skip
    assert (await ask_mood(db, a, a_row))["mood"] is None
    await confirmed_state(owner_engine, b, "tired", priority=70)
    assert await ask_mood(db, a, a_row) == {"decision": "allowed", "mood": "tired"}
    await confirmed_state(owner_engine, b, "stressed", priority=90)
    assert (await ask_mood(db, a, a_row))["mood"] == "stressed"  # the strongest wins
    assert "private feeling" not in json.dumps(await ask_mood(db, a, a_row))  # the free-text label never leaves


async def test_an_expired_or_dismissed_mood_is_not_shared(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine, granted=MOOD)
    expired = await confirmed_state(owner_engine, b, "sad", hours=1)
    await owner_exec(owner_engine, "UPDATE civ.emotional_states SET started_at = now() - interval '3 days', expires_at = now() - interval '2 days' WHERE id = :i", i=expired)  # fmt: skip
    await confirmed_state(owner_engine, b, "calm", status="dismissed")
    assert (await ask_mood(db, a, a_row))["mood"] is None


async def test_a_state_without_a_mood_is_not_a_mood(db, owner_engine):
    a, b, a_row, _ = await pair(owner_engine, granted=MOOD)
    await confirmed_state(owner_engine, b, None)
    assert (await ask_mood(db, a, a_row))["mood"] is None


async def test_the_database_keeps_an_unconfirmed_mood_from_existing(owner_engine):
    b = await new_user(owner_engine)
    with pg_error("23514"):  # CHECK: only a state the user confirmed may carry a mood
        await owner_exec(owner_engine, "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, mood, expires_at) VALUES (:b, 'x', 5, 50, 'inferred', 'sad', now() + interval '1 day')", b=b)  # fmt: skip
    with pg_error("23514"):
        await owner_exec(owner_engine, "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, mood, expires_at) VALUES (:b, 'x', 5, 50, 'user_declared', 'sad', now() + interval '1 day')", b=b)  # fmt: skip


async def test_a_confirmed_state_must_carry_exactly_the_mood_the_user_approved(owner_engine):
    b = await new_user(owner_engine)
    pending = (
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.pending_facts (user_id, subject, change, emotion_label, emotion_intensity, emotion_priority, emotion_expires_at, "
            "emotion_mood, evidence, source_message_ids, status, decided_at, idempotency_key) VALUES (:b, 'emotional_state', 'create', 'x', 8, 90, "
            "now() + interval '1 day', 'tired', 'said so', ARRAY[gen_random_uuid()], 'approved', now(), :i) RETURNING id",
            b=b, i=uuid.uuid4().hex,
        )
    )[0][0]  # fmt: skip
    insert = (
        "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, source_pending_id, mood, expires_at) "
        "VALUES (:b, 'x', 8, 90, 'user_confirmed', :p, CAST(:m AS civ.shared_mood), now() + interval '1 day')"
    )
    with pg_error("23514"):
        await owner_exec(owner_engine, insert, b=b, p=pending, m="sad")  # a different word than the card showed
    with pg_error("23514"):
        await owner_exec(owner_engine, insert, b=b, p=pending, m=None)
    await owner_exec(owner_engine, insert, b=b, p=pending, m="tired")


async def test_a_mood_cannot_ride_on_a_core_fact_proposal(owner_engine):
    b = await new_user(owner_engine)
    with pg_error("23514"):
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.pending_facts (user_id, subject, change, category, key, value_text, emotion_mood, evidence, source_message_ids, "
            "idempotency_key) VALUES (:b, 'core_fact', 'create', 'body', 'weight', '80', 'sad', 'x', ARRAY[gen_random_uuid()], :i)",
            b=b, i=uuid.uuid4().hex,
        )  # fmt: skip


# ================================================================== races


async def test_a_block_and_an_ask_never_leave_an_unaudited_answer(db, owner_engine):
    """Whatever the interleaving: an ask that was answered is in the owner's log, and once the block has committed nothing is answered."""
    for _ in range(6):
        a, b, a_row, b_row = await pair(owner_engine)
        blocked, asked = await asyncio.gather(
            tenant_exec(db, b, "SELECT civ.block_contact(:c)", c=b_row), ask_free(db, a, a_row)
        )
        assert blocked[0][0] == "ok"
        log = await audit(owner_engine, b, a)
        if asked["decision"] == "allowed":
            assert log == [(FREE, "allowed")]  # it was answered BEFORE the block, and it was recorded
        else:
            assert asked == UNAVAILABLE and len(log) <= 1
        assert await ask_free(db, a, a_row) == UNAVAILABLE  # after the block: never
        assert len(await audit(owner_engine, b, a)) == len(log)  # (a blocked pair leaves no trace)


async def test_a_revoked_grant_is_never_honoured_by_a_later_ask(db, owner_engine):
    a, b, a_row, b_row = await pair(owner_engine)
    results = await asyncio.gather(
        tenant_exec(db, b, "SELECT civ.set_grant(:c, 'calendar_free_busy', false)", c=b_row), ask_free(db, a, a_row)
    )
    assert results[0][0][0] == "ok"
    assert await ask_free(db, a, a_row) == UNAVAILABLE

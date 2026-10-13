"""Tier 1: versioned facts, Emotional Overrides, quarantine (pending_facts) and the human-in-the-loop guards."""

from __future__ import annotations

import pytest

from tests.helpers import (
    new_fact,
    new_pending,
    new_persona,
    new_user,
    owner_exec,
    pg_error,
    tenant_exec,
)

INSERT_FACT = (
    "INSERT INTO civ.core_facts (user_id, category, key, value_text, source) "
    "VALUES (:u, 'body', :k, '80', 'user_manual')"
)


# ------------------------------------------------------------------ core_facts


async def test_one_current_version_per_key_and_history_is_kept(db, owner_engine):
    u = await new_user(owner_engine)
    await tenant_exec(db, u, INSERT_FACT, u=u, k="weight")
    with pg_error("23505"):
        await tenant_exec(db, u, INSERT_FACT, u=u, k="weight")
    # close the current version (separate transaction: valid_to must be later than valid_from), then add the next one
    assert await tenant_exec(db, u, "UPDATE civ.core_facts SET valid_to = now() WHERE key = 'weight' AND valid_to IS NULL") == 1
    await tenant_exec(db, u, INSERT_FACT, u=u, k="weight")
    rows = await tenant_exec(db, u, "SELECT valid_to IS NULL FROM civ.core_facts WHERE key = 'weight' ORDER BY valid_from")
    assert [r[0] for r in rows] == [False, True]


async def test_fact_versions_are_immutable_except_valid_to(db, owner_engine):
    u = await new_user(owner_engine)
    persona, other = await new_persona(owner_engine, u, "job_hunt"), await new_persona(owner_engine, u, "discipline")
    fact = await new_fact(owner_engine, u, "weight", category="body", value="80", persona_id=persona)
    for change in ("value_text = '90'", "priority = 99", "key = 'height'", "category = 'goal'"):
        with pg_error("23000", "versioned"):
            await tenant_exec(db, u, f"UPDATE civ.core_facts SET {change} WHERE id = :f", f=fact)
    # the link to a topic may be cleared (what deleting a persona does) but never re-pointed
    with pg_error("23000", "versioned"):
        await tenant_exec(db, u, "UPDATE civ.core_facts SET persona_id = :p WHERE id = :f", p=other, f=fact)
    assert await tenant_exec(db, u, "UPDATE civ.core_facts SET persona_id = NULL WHERE id = :f", f=fact) == 1
    assert await tenant_exec(db, u, "UPDATE civ.core_facts SET valid_to = now() WHERE id = :f", f=fact) == 1
    with pg_error("23000", "already closed"):  # a closed version cannot be reopened or re-closed
        await tenant_exec(db, u, "UPDATE civ.core_facts SET valid_to = NULL WHERE id = :f", f=fact)


async def test_fact_key_format_and_value_required(db, owner_engine):
    u = await new_user(owner_engine)
    for bad in ("Weight!", "weight kg", "1weight", "principle..x", ""):
        with pg_error("23514"):
            await tenant_exec(db, u, INSERT_FACT, u=u, k=bad)
    await tenant_exec(db, u, INSERT_FACT, u=u, k="principle.no_danish")
    with pg_error("23514"):
        await tenant_exec(
            db, u, "INSERT INTO civ.core_facts (user_id, category, key, source) VALUES (:u, 'body', 'x', 'user_manual')", u=u
        )


async def test_approved_review_facts_need_an_approved_proposal(db, owner_engine):
    u = await new_user(owner_engine)
    pending = await new_pending(owner_engine, u, status="pending")
    approved = await new_pending(owner_engine, u, status="approved")
    rejected = await new_pending(owner_engine, u, status="rejected")
    emotion = await new_pending(owner_engine, u, subject="emotional_state", status="approved")
    sql = (
        "INSERT INTO civ.core_facts (user_id, category, key, value_text, source, source_pending_id) "
        "VALUES (:u, 'body', :k, '80', CAST(:s AS civ.fact_source), :p)"
    )
    for pid in (pending, rejected, emotion):  # not approved / wrong subject -> guard trigger
        with pg_error("23514", "APPROVED"):
            await tenant_exec(db, u, sql, u=u, k="weight", s="approved_review", p=pid)
    with pg_error("23514"):  # approved_review without any proposal -> CHECK
        await tenant_exec(db, u, sql, u=u, k="weight", s="approved_review", p=None)
    with pg_error("23514"):  # a proposal id on a non-review source -> CHECK
        await tenant_exec(db, u, sql, u=u, k="weight", s="user_manual", p=approved)
    assert await tenant_exec(db, u, sql, u=u, k="weight", s="approved_review", p=approved) == 1


# ------------------------------------------------------------------ pending_facts (quarantine)


async def test_pending_decision_is_final(db, owner_engine):
    u = await new_user(owner_engine)
    p = await new_pending(owner_engine, u)
    assert await tenant_exec(db, u, "UPDATE civ.pending_facts SET status = 'approved', decided_at = now() WHERE id = :p", p=p) == 1
    with pg_error("23000", "can no longer change"):
        await tenant_exec(db, u, "UPDATE civ.pending_facts SET status = 'rejected', decided_at = now() WHERE id = :p", p=p)
    with pg_error("23000", "can no longer change"):
        await tenant_exec(db, u, "UPDATE civ.pending_facts SET evidence = 'edited' WHERE id = :p", p=p)


async def test_pending_status_and_decided_at_are_consistent(db, owner_engine):
    u = await new_user(owner_engine)
    p = await new_pending(owner_engine, u)
    with pg_error("23514"):  # decided without a timestamp
        await tenant_exec(db, u, "UPDATE civ.pending_facts SET status = 'approved' WHERE id = :p", p=p)
    with pg_error("23514"):  # timestamp without a decision
        await tenant_exec(db, u, "UPDATE civ.pending_facts SET decided_at = now() WHERE id = :p", p=p)


async def test_pending_payload_shapes(db, owner_engine):
    u = await new_user(owner_engine)
    base = (
        "INSERT INTO civ.pending_facts (user_id, subject, change, {cols}, evidence, source_message_ids, idempotency_key) "
        "VALUES (:u, CAST(:sub AS civ.pending_subject), CAST(:chg AS civ.change_type), {vals}, :ev, "
        "ARRAY[gen_random_uuid()], gen_random_uuid()::text)"
    )
    fact = ("category, key, value_text", "'body', 'weight', '80'")
    emotion = (
        "emotion_label, emotion_intensity, emotion_priority, emotion_expires_at",
        "'in love', 9, 100, now() + interval '2 days'",
    )

    async def propose(sub, chg, cols, vals, ev="quote"):
        return await tenant_exec(db, u, base.format(cols=cols, vals=vals), u=u, sub=sub, chg=chg, ev=ev)

    assert await propose("core_fact", "create", *fact) == 1
    assert await propose("emotional_state", "create", *emotion) == 1  # priority 100 is allowed in quarantine
    with pg_error("23514"):  # fact proposal carrying emotion columns
        await propose("core_fact", "create", fact[0] + ", emotion_label", fact[1] + ", 'x'")
    with pg_error("23514"):  # emotion proposal missing its expiry
        await propose("emotional_state", "create", "emotion_label, emotion_intensity, emotion_priority", "'x', 5, 50")
    with pg_error("23514"):  # emotion proposal may only 'create'
        await propose("emotional_state", "delete", *emotion)
    with pg_error("23514"):  # delete / update need a target fact
        await propose("core_fact", "delete", "category", "'body'")
    with pg_error("23514"):
        await propose("core_fact", "update", *fact)
    with pg_error("23514"):  # no evidence, no fact
        await propose("core_fact", "create", *fact, ev="   ")
    with pg_error("23514"):  # no source messages, no fact
        await tenant_exec(
            db,
            u,
            "INSERT INTO civ.pending_facts (user_id, change, category, key, value_text, evidence, source_message_ids, "
            "idempotency_key) VALUES (:u, 'create', 'body', 'w', '1', 'q', '{}', 'k')",
            u=u,
        )


async def test_pending_retry_is_idempotent(db, owner_engine):
    u = await new_user(owner_engine)
    sql = (
        "INSERT INTO civ.pending_facts (user_id, change, category, key, value_text, evidence, source_message_ids, "
        "idempotency_key) VALUES (:u, 'create', 'body', 'weight', '80', 'q', ARRAY[gen_random_uuid()], 'gc:2026-10-02:1') "
        "ON CONFLICT (user_id, idempotency_key) DO NOTHING"
    )
    assert await tenant_exec(db, u, sql, u=u) == 1
    assert await tenant_exec(db, u, sql, u=u) == 0  # the retried GC run does not create a duplicate
    plain = sql.split(" ON CONFLICT")[0]
    with pg_error("23505"):
        await tenant_exec(db, u, plain, u=u)


# ------------------------------------------------------------------ emotional states and overrides


async def test_inferred_emotions_are_capped_at_60(db, owner_engine):
    u = await new_user(owner_engine)
    sql = (
        "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, source_pending_id, expires_at) "
        "VALUES (:u, 'in love', 9, :pr, CAST(:src AS civ.emotion_source), :p, now() + interval '3 days')"
    )

    async def add(pr, src, p=None):
        return await tenant_exec(db, u, sql, u=u, pr=pr, src=src, p=p)

    assert await add(60, "inferred") == 1
    with pg_error("23514", "inferred_cap"):
        await add(61, "inferred")
    assert await add(100, "user_declared") == 1
    approved_emotion = await new_pending(owner_engine, u, subject="emotional_state", status="approved")
    approved_fact = await new_pending(owner_engine, u, status="approved")
    with pg_error("23514"):  # user_confirmed must come from a proposal
        await add(100, "user_confirmed")
    with pg_error("23514", "APPROVED"):  # ... an APPROVED proposal of the right subject
        await add(100, "user_confirmed", approved_fact)
    assert await add(100, "user_confirmed", approved_emotion) == 1  # the quarantine -> user -> priority 100 path


async def test_emotional_state_ranges(db, owner_engine):
    u = await new_user(owner_engine)
    sql = (
        "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, started_at, expires_at) "
        "VALUES (:u, 'x', :i, :pr, 'user_declared', now(), now() + :d * interval '1 day')"
    )
    for params in ({"i": 11, "pr": 50, "d": 1}, {"i": 0, "pr": 50, "d": 1}, {"i": 5, "pr": 101, "d": 1}, {"i": 5, "pr": 50, "d": 0}):
        with pg_error("23514"):
            await tenant_exec(db, u, sql, u=u, **params)


async def test_override_targets_must_be_current_principles(db, owner_engine):
    u = await new_user(owner_engine)
    principle = await new_fact(owner_engine, u, "principle.no_danish")
    body = await new_fact(owner_engine, u, "weight", category="body")
    old = await new_fact(owner_engine, u, "principle.old")
    await owner_exec(owner_engine, "UPDATE civ.core_facts SET valid_to = now() WHERE id = :f", f=old)
    state = (
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, expires_at) "
            "VALUES (:u, 'in love', 9, 100, 'user_declared', now() + interval '3 days') RETURNING id",
            u=u,
        )
    )[0][0]
    sql = "INSERT INTO civ.emotional_overrides (user_id, emotional_state_id, core_fact_id) VALUES (:u, :s, :f)"
    for bad in (body, old):
        with pg_error("23514", "CURRENT fact of category principle"):
            await tenant_exec(db, u, sql, u=u, s=state, f=bad)
    assert await tenant_exec(db, u, sql, u=u, s=state, f=principle) == 1
    with pg_error("23505"):
        await tenant_exec(db, u, sql, u=u, s=state, f=principle)


async def _view_row(db, u, fact):
    rows = await tenant_exec(
        db, u, "SELECT is_overridden, overridden_by, override_priority FROM civ.effective_principles WHERE fact_id = :f", f=fact
    )
    assert len(rows) == 1
    return tuple(rows[0])


async def _add_state(owner_engine, u, fact, *, label, priority, source="user_declared", status="active", past=False):
    started, expires = ("now() - interval '2 days'", "now() - interval '1 day'") if past else ("now()", "now() + interval '3 days'")
    state = (
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, status, started_at, expires_at) "
            f"VALUES (:u, :l, 9, :pr, CAST(:src AS civ.emotion_source), CAST(:st AS civ.emotion_status), {started}, {expires}) "
            "RETURNING id",
            u=u, l=label, pr=priority, src=source, st=status,
        )
    )[0][0]  # fmt: skip
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.emotional_overrides (user_id, emotional_state_id, core_fact_id, mode) VALUES (:u, :s, :f, 'suspend')",
        u=u, s=state, f=fact,
    )  # fmt: skip
    return state


async def test_emotional_override_wins_only_when_it_outranks_the_principle(db, owner_engine):
    u = await new_user(owner_engine)
    soft = await new_fact(owner_engine, u, "principle.no_danish", priority=50)
    fundamental = await new_fact(owner_engine, u, "principle.no_alcohol", priority=90)
    untouched = await new_fact(owner_engine, u, "principle.sleep_early", priority=50)

    assert await _view_row(db, u, soft) == (False, None, None)  # no override yet

    await _add_state(owner_engine, u, soft, label="in love", priority=100)
    assert await _view_row(db, u, soft) == (True, "in love", 100)  # the user's example: 100 beats 50
    assert await _view_row(db, u, untouched) == (False, None, None)  # other principles are unaffected

    # a state that does not outrank the principle is inert
    await _add_state(owner_engine, u, fundamental, label="bad day", priority=60, source="inferred")
    assert await _view_row(db, u, fundamental) == (False, None, None)  # inferred cap 60 < 90: cannot break it
    await _add_state(owner_engine, u, fundamental, label="crisis", priority=95)
    assert await _view_row(db, u, fundamental) == (True, "crisis", 95)  # only a user-level state (<=100) can


async def test_overrides_ignore_expired_dismissed_and_pick_the_strongest(db, owner_engine):
    u = await new_user(owner_engine)
    fact = await new_fact(owner_engine, u, "principle.no_danish", priority=40)
    await _add_state(owner_engine, u, fact, label="expired", priority=100, past=True)
    assert await _view_row(db, u, fact) == (False, None, None)
    await _add_state(owner_engine, u, fact, label="dismissed", priority=100, status="dismissed")
    assert await _view_row(db, u, fact) == (False, None, None)
    await _add_state(owner_engine, u, fact, label="weak", priority=70)
    await _add_state(owner_engine, u, fact, label="strong", priority=90)
    assert await _view_row(db, u, fact) == (True, "strong", 90)


async def test_closed_principle_version_drops_out_of_the_view(db, owner_engine):
    u = await new_user(owner_engine)
    fact = await new_fact(owner_engine, u, "principle.no_danish", priority=40)
    await _add_state(owner_engine, u, fact, label="in love", priority=100)
    await owner_exec(owner_engine, "UPDATE civ.core_facts SET valid_to = now() WHERE id = :f", f=fact)
    assert await tenant_exec(db, u, "SELECT count(*) FROM civ.effective_principles") == [(0,)]


# ------------------------------------------------------------------ persona deletion keeps memory


async def test_deleting_a_persona_keeps_everything_attached_to_it(db, owner_engine):
    u = await new_user(owner_engine)
    persona = await new_persona(owner_engine, u, "job_hunt")
    fact = await new_fact(owner_engine, u, "weight", category="body", persona_id=persona)
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.messages (user_id, persona_id, role, content) VALUES (:u, :p, 'user', 'hi')",
        u=u, p=persona,
    )  # fmt: skip
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.events (user_id, persona_id, title, starts_at, ends_at, timezone, created_via) "
        "VALUES (:u, :p, 'x', now(), now() + interval '1 hour', 'UTC', 'mini_app')",
        u=u, p=persona,
    )  # fmt: skip
    assert await tenant_exec(db, u, "DELETE FROM civ.personas WHERE id = :p", p=persona) == 1
    # ON DELETE SET NULL (persona_id): rows survive, user_id is untouched, only the persona link is cleared
    assert (await tenant_exec(db, u, "SELECT persona_id, user_id FROM civ.core_facts WHERE id = :f", f=fact))[0] == (None, u)
    assert await tenant_exec(db, u, "SELECT count(*) FROM civ.messages WHERE persona_id IS NULL") == [(1,)]
    assert await tenant_exec(db, u, "SELECT count(*) FROM civ.events WHERE persona_id IS NULL") == [(1,)]


@pytest.mark.parametrize("tz", ["UTC+3", "Mars/Phobos", "garbage", ""])
async def test_users_reject_invalid_timezones(owner_engine, tz):
    with pg_error("22023", "invalid IANA timezone"):
        await new_user(owner_engine, tz=tz)

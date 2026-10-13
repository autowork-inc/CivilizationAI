"""Shared helpers for the database tests (they run against a REAL PostgreSQL, never mocks)."""

from __future__ import annotations

import json
import os
import random
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from ai_civ.db import Database

OWNER_URL = os.environ.get("TEST_OWNER_DATABASE_URL")  # migration/owner role (not a superuser is fine)
APP_PASSWORD = "ai_civ_app_test_pw"
APP_URL = (
    make_url(OWNER_URL).set(username="ai_civ_app", password=APP_PASSWORD).render_as_string(hide_password=False)
    if OWNER_URL
    else None
)
EMBEDDING_DIM = 1536


def sqlstate_of(exc: BaseException) -> str | None:
    """SQLSTATE of a driver error wrapped by SQLAlchemy (asyncpg)."""
    candidates = [getattr(exc, "orig", None), getattr(getattr(exc, "orig", None), "__cause__", None), exc]
    for cand in candidates:
        for attr in ("sqlstate", "pgcode"):
            code = getattr(cand, attr, None)
            if code:
                return str(code)
    return None


@contextmanager
def pg_error(code: str, match: str | None = None):
    """Expect a PostgreSQL error with the given SQLSTATE.

    23505 unique | 23503 foreign key | 23514 check (also used by guard triggers) | 23000 integrity (guard triggers)
    42501 permission denied / RLS | 22023 invalid parameter | 42704 undefined object (unset GUC) | 22P02 bad uuid
    """
    with pytest.raises(DBAPIError) as excinfo:
        yield
    got = sqlstate_of(excinfo.value)
    assert got == code, f"expected SQLSTATE {code}, got {got}: {str(excinfo.value)[:300]}"
    if match:
        assert match in str(excinfo.value), f"{match!r} not in: {str(excinfo.value)[:300]}"


async def owner_exec(owner_engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with owner_engine.begin() as conn:
        res = await conn.execute(text(sql), params)
        return res.all() if res.returns_rows else res.rowcount


async def tenant_exec(db: Database, user_id: uuid.UUID, sql: str, **params: Any) -> Any:
    """Run one statement in its own tenant transaction (application role, RLS enforced)."""
    async with db.tenant_session(user_id) as session:
        res = await session.execute(text(sql), params)
        return res.all() if res.returns_rows else res.rowcount


def vec(*pairs: tuple[int, float]) -> str:
    """pgvector text literal of EMBEDDING_DIM dimensions with the given (index, value) components."""
    values = [0.0] * EMBEDDING_DIM
    for index, value in pairs:
        values[index] = value
    return "[" + ",".join(str(v) for v in values) + "]"


# ---------------------------------------------------------------- seeding (as owner: bypasses RLS)


async def new_user(
    owner_engine: AsyncEngine,
    tz: str = "UTC",
    *,
    telegram_user_id: int | None = None,
    forum_chat_id: int | None = None,
    onboarding_state: str = "done",  # tests that are not about onboarding use a tenant that is already let in
) -> uuid.UUID:
    user_id = uuid.uuid4()
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.users (id, telegram_user_id, forum_chat_id, timezone, onboarding_state) "
        "VALUES (:id, :tg, :chat, :tz, :ob)",
        id=user_id,
        tg=telegram_user_id if telegram_user_id is not None else random.randrange(10**9, 10**15),
        chat=forum_chat_id if forum_chat_id is not None else -random.randrange(10**9, 10**15),
        tz=tz,
        ob=onboarding_state,
    )
    return user_id


async def new_contact(
    owner_engine: AsyncEngine, user_id: uuid.UUID, contact_user_id: uuid.UUID, *, status: str = "accepted", alias: str | None = None
) -> uuid.UUID:
    """One contact row (user_id's address book entry for contact_user_id). Seeded as the OWNER: the application role can no
    longer write ``civ.contacts`` (migration 0005) - connections come into being only through ``civ.accept_invite``."""
    rows = await owner_exec(
        owner_engine,
        "INSERT INTO civ.contacts (user_id, contact_user_id, alias, status) "
        "VALUES (:u, :c, :a, CAST(:s AS civ.contact_status)) RETURNING id",
        u=user_id,
        c=contact_user_id,
        a=alias,
        s=status,
    )
    return rows[0][0]


async def new_persona(
    owner_engine: AsyncEngine,
    user_id: uuid.UUID,
    code: str = "foodguide",
    *,
    thread_id: int | None = None,
    system_prompt: str = "prompt",
    title: str | None = None,
) -> uuid.UUID:
    rows = await owner_exec(
        owner_engine,
        "INSERT INTO civ.personas (user_id, code, title, system_prompt, telegram_thread_id) "
        "VALUES (:u, :c, :t, :sp, :th) RETURNING id",
        u=user_id,
        c=code,
        t=title or code.title(),
        sp=system_prompt,
        th=thread_id,
    )
    return rows[0][0]


async def new_fact(
    owner_engine: AsyncEngine,
    user_id: uuid.UUID,
    key: str,
    *,
    category: str = "principle",
    priority: int = 50,
    value: str = "v",
    persona_id: uuid.UUID | None = None,
) -> uuid.UUID:
    rows = await owner_exec(
        owner_engine,
        "INSERT INTO civ.core_facts (user_id, persona_id, category, key, value_text, priority, source) "
        "VALUES (:u, :p, CAST(:c AS civ.fact_category), :k, :v, :pr, 'user_manual') RETURNING id",
        u=user_id,
        p=persona_id,
        c=category,
        k=key,
        v=value,
        pr=priority,
    )
    return rows[0][0]


async def new_pending(
    owner_engine: AsyncEngine,
    user_id: uuid.UUID,
    *,
    subject: str = "core_fact",
    status: str = "pending",
    key: str | None = "weight",
    idem: str | None = None,
) -> uuid.UUID:
    """A valid 'create' proposal of the requested subject in the requested status."""
    idem = idem or uuid.uuid4().hex
    decided = "now()" if status != "pending" else "NULL"
    if subject == "core_fact":
        sql = (
            "INSERT INTO civ.pending_facts (user_id, subject, change, category, key, value_text, evidence, "
            "source_message_ids, status, decided_at, idempotency_key) "
            f"VALUES (:u, 'core_fact', 'create', 'body', :k, '80 kg', 'said so', ARRAY[gen_random_uuid()], "
            f"CAST(:s AS civ.pending_status), {decided}, :i) RETURNING id"
        )
    else:
        sql = (
            "INSERT INTO civ.pending_facts (user_id, subject, change, emotion_label, emotion_intensity, "
            "emotion_priority, emotion_expires_at, evidence, source_message_ids, status, decided_at, "
            "idempotency_key) VALUES (:u, 'emotional_state', 'create', 'in love', 9, 100, now() + interval '3 days', "
            f"'said so', ARRAY[gen_random_uuid()], CAST(:s AS civ.pending_status), {decided}, :i) RETURNING id"
        )
    params: dict[str, Any] = {"u": user_id, "s": status, "i": idem}
    if subject == "core_fact":
        params["k"] = key
    rows = await owner_exec(owner_engine, sql, **params)
    return rows[0][0]


async def seed_full_user(owner_engine: AsyncEngine, user_id: uuid.UUID, other_id: uuid.UUID) -> dict[str, Any]:
    """One row (at least) in every tenant table of user_id; contacts/grants/audit also involve other_id."""
    ids: dict[str, Any] = {}
    ids["persona"] = await new_persona(owner_engine, user_id)
    ids["batch"] = (
        await owner_exec(
            owner_engine, "INSERT INTO civ.review_batches (user_id, sent_at) VALUES (:u, now()) RETURNING id", u=user_id
        )
    )[0][0]
    ids["pending_fact"] = await new_pending(owner_engine, user_id, status="approved")
    ids["pending_emotion"] = await new_pending(owner_engine, user_id, subject="emotional_state")
    ids["principle"] = await new_fact(owner_engine, user_id, "principle.no_danish", priority=50)
    ids["fact_from_review"] = (
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.core_facts (user_id, persona_id, category, key, value_num, value_unit, source, "
            "source_pending_id) VALUES (:u, :p, 'body', 'weight', 80, 'kg', 'approved_review', :pf) RETURNING id",
            u=user_id,
            p=ids["persona"],
            pf=ids["pending_fact"],
        )
    )[0][0]
    ids["state"] = (
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, expires_at) "
            "VALUES (:u, 'in love', 9, 100, 'user_declared', now() + interval '3 days') RETURNING id",
            u=user_id,
        )
    )[0][0]
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.emotional_overrides (user_id, emotional_state_id, core_fact_id) VALUES (:u, :s, :f)",
        u=user_id,
        s=ids["state"],
        f=ids["principle"],
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.messages (user_id, persona_id, role, content, telegram_message_id) "
        "VALUES (:u, :p, 'user', 'hello', 1)",
        u=user_id,
        p=ids["persona"],
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.memory_outbox (user_id, operation, payload, idempotency_key) "
        "VALUES (:u, 'embed_episode', '{}', 'k1')",
        u=user_id,
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.episodic_memories (user_id, persona_id, idempotency_key, kind, content, embedding, "
        "embedding_model, occurred_from, occurred_to) VALUES (:u, :p, 'e1', 'dialogue_chunk', 'we talked', "
        "CAST(:v AS extensions.vector), 'text-embedding-3-small', now(), now())",
        u=user_id,
        p=ids["persona"],
        v=vec((0, 1.0)),
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.contacts (user_id, contact_user_id, status) VALUES (:u, :o, 'accepted')",
        u=user_id,
        o=other_id,
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.access_grants (user_id, grantee_user_id, scope) VALUES (:u, :o, 'calendar_free_busy')",
        u=user_id,
        o=other_id,
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision) "
        "VALUES (:u, :o, 'calendar_free_busy', 'allowed')",
        u=user_id,
        o=other_id,
    )
    # a log row in the OTHER tenant that points at user_id as the requester
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision) "
        "VALUES (:o, :u, 'emotional_state_label', 'denied')",
        u=user_id,
        o=other_id,
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.proactive_schedule (user_id, trigger_key, local_time) VALUES (:u, 'meal_check', '13:00')",
        u=user_id,
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.contact_invites (user_id, token_hash, display_name, expires_at) "
        "VALUES (:u, sha256(convert_to(:t, 'UTF8')), 'Alice', now() + interval '1 day')",
        u=user_id,
        t=uuid.uuid4().hex,
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.notification_outbox (user_id, kind, payload, idempotency_key) "
        "VALUES (:u, 'contact_accepted', '{}', 'seed')",
        u=user_id,
    )
    ids["event"] = (
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.events (user_id, persona_id, title, starts_at, ends_at, timezone, recurrence_rule, "
            "created_via) VALUES (:u, :p, 'Workout', now(), now() + interval '1 hour', 'Europe/Copenhagen', "
            "'FREQ=WEEKLY', 'mini_app') RETURNING id",
            u=user_id,
            p=ids["persona"],
        )
    )[0][0]
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.events (user_id, title, starts_at, ends_at, timezone, parent_event_id, original_start, "
        "created_via) VALUES (:u, 'Workout (moved)', now(), now() + interval '1 hour', 'Europe/Copenhagen', :e, "
        "now() - interval '7 days', 'mini_app')",
        u=user_id,
        e=ids["event"],
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.event_proposals (user_id, recipient_user_id, title, starts_at, ends_at, expires_at) "
        "VALUES (:u, :o, 'Chess', now() + interval '1 day', now() + interval '1 day 1 hour', now() + interval '1 hour')",
        u=user_id,
        o=other_id,
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.event_drafts (user_id, persona_id, title, starts_at, ends_at, timezone, expires_at) "
        "VALUES (:u, :p, 'Rest', now(), now() + interval '2 hours', 'Europe/Copenhagen', now() + interval '1 day')",
        u=user_id,
        p=ids["persona"],
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.message_attachments (user_id, message_id, kind, content_kind, mime_type, size_bytes, sha256, telegram_file_id) "
        "SELECT :u, id, 'photo', 'image', 'image/jpeg', 10, repeat('a', 64), 'f' FROM civ.messages WHERE user_id = :u LIMIT 1",
        u=user_id,
    )
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.proactive_checkin_log (user_id, slot_date, slot_time) VALUES (:u, current_date, '10:30')",
        u=user_id,
    )
    # the tables of the v2.0 features (tasks, cards, RPG, workouts, recipes, inventory, places, capsules)
    ids["task"] = (await owner_exec(owner_engine, "INSERT INTO civ.tasks (user_id, title, source) VALUES (:u, 'Goal', 'mini_app') RETURNING id", u=user_id))[0][0]
    await owner_exec(owner_engine, "INSERT INTO civ.task_events (user_id, task_id, event_id) VALUES (:u, :t, :e)", u=user_id, t=ids["task"], e=ids["event"])
    ids["card"] = (
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.cards (user_id, deck, question, options, correct_index) VALUES (:u, 'D', 'Q?', ARRAY['a', 'b', 'c', 'd'], 1) RETURNING id",
            u=user_id,
        )
    )[0][0]
    await owner_exec(owner_engine, "INSERT INTO civ.card_reviews (user_id, card_id, next_review_at) VALUES (:u, :c, now())", u=user_id, c=ids["card"])
    await owner_exec(owner_engine, "INSERT INTO civ.character_profiles (user_id) VALUES (:u)", u=user_id)
    await owner_exec(owner_engine, "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 10, 'focus', 'seed')", u=user_id)
    await owner_exec(owner_engine, "INSERT INTO civ.tavern_cheers (user_id, target_user_id, kind, local_date) VALUES (:u, :o, 'focus', current_date)", u=user_id, o=other_id)
    ids["exercise"] = (await owner_exec(owner_engine, "INSERT INTO civ.exercises (user_id, name) VALUES (:u, 'Squat') RETURNING id", u=user_id))[0][0]
    ids["workout"] = (await owner_exec(owner_engine, "INSERT INTO civ.workout_sessions (user_id) VALUES (:u) RETURNING id", u=user_id))[0][0]
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.workout_sets (user_id, session_id, exercise_id, set_order, reps) VALUES (:u, :s, :e, 1, 10)",
        u=user_id, s=ids["workout"], e=ids["exercise"],
    )  # fmt: skip
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.recipe_history (user_id, title, prep_time_minutes, ingredients_used, instructions) VALUES (:u, 'Omelette', 10, '[\"egg\"]', '1. Cook')",
        u=user_id,
    )
    ids["item"] = (await owner_exec(owner_engine, "INSERT INTO civ.inventory_items (user_id, name, unit) VALUES (:u, 'Creatine', 'г') RETURNING id", u=user_id))[0][0]
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.inventory_movements (user_id, item_id, change_amount, quantity_after, reason) VALUES (:u, :i, 100, 100, 'manual_restock')",
        u=user_id, i=ids["item"],
    )  # fmt: skip
    ids["place"] = (await owner_exec(owner_engine, "INSERT INTO civ.places (user_id, name, latitude, longitude) VALUES (:u, 'Shop', 55.0, 12.0) RETURNING id", u=user_id))[0][0]
    await owner_exec(owner_engine, "INSERT INTO civ.geo_notes (user_id, place_id, content) VALUES (:u, :p, 'Buy milk')", u=user_id, p=ids["place"])
    await owner_exec(owner_engine, "INSERT INTO civ.time_capsules (user_id, message, deliver_at) VALUES (:u, 'Hello me', now() + interval '30 days')", u=user_id)
    await owner_exec(owner_engine, "INSERT INTO civ.daily_mood (user_id, local_date, score, message_count) VALUES (:u, current_date, 1, 5)", u=user_id)
    await owner_exec(owner_engine, "INSERT INTO civ.user_sessions (user_id, refresh_token_hash, expires_at) VALUES (:u, 'hash', now() + interval '30 days')", u=user_id)
    return ids


async def new_outbox(
    owner_engine: AsyncEngine,
    user_id: uuid.UUID,
    *,
    operation: str = "compress_day",
    payload: dict[str, Any] | None = None,
    key: str | None = None,
    max_attempts: int = 5,
) -> int:
    rows = await owner_exec(
        owner_engine,
        "INSERT INTO civ.memory_outbox (user_id, operation, payload, idempotency_key, max_attempts) "
        "VALUES (:u, CAST(:op AS civ.outbox_operation), CAST(:p AS jsonb), :k, :m) RETURNING id",
        u=user_id,
        op=operation,
        p=json.dumps(payload or {}),
        k=key or uuid.uuid4().hex,
        m=max_attempts,
    )
    return rows[0][0]


async def outbox_row(owner_engine: AsyncEngine, outbox_id: int) -> dict[str, Any]:
    rows = await owner_exec(
        owner_engine,
        "SELECT status::text, attempts, locked_until, last_error, processed_at, "
        "(locked_until > now()) AS lease_active FROM civ.memory_outbox WHERE id = :i",
        i=outbox_id,
    )
    status, attempts, locked_until, last_error, processed_at, lease_active = rows[0]
    return {
        "status": status,
        "attempts": attempts,
        "locked_until": locked_until,
        "last_error": last_error,
        "processed_at": processed_at,
        "lease_active": lease_active,
    }


async def expire_lease(owner_engine: AsyncEngine, outbox_id: int) -> None:
    await owner_exec(
        owner_engine, "UPDATE civ.memory_outbox SET locked_until = now() - interval '1 second' WHERE id = :i", i=outbox_id
    )


async def new_message(
    owner_engine: AsyncEngine,
    user_id: uuid.UUID,
    content: str,
    *,
    role: str = "user",
    persona_id: uuid.UUID | None = None,
    created_at: datetime | None = None,
) -> uuid.UUID:
    rows = await owner_exec(
        owner_engine,
        "INSERT INTO civ.messages (user_id, persona_id, role, content, created_at) "
        "VALUES (:u, :p, CAST(:r AS civ.message_role), :c, COALESCE(:t, now())) RETURNING id",
        u=user_id,
        p=persona_id,
        r=role,
        c=content,
        t=created_at,
    )
    return rows[0][0]


async def new_episode(
    owner_engine: AsyncEngine,
    user_id: uuid.UUID,
    content: str,
    embedding: list[float],
    *,
    key: str | None = None,
    model: str = "test",
) -> uuid.UUID:
    rows = await owner_exec(
        owner_engine,
        "INSERT INTO civ.episodic_memories (user_id, idempotency_key, kind, content, embedding, embedding_model, "
        "occurred_from, occurred_to) VALUES (:u, :k, 'daily_summary', :c, CAST(:v AS extensions.vector), :m, now(), now()) "
        "RETURNING id",
        u=user_id,
        k=key or uuid.uuid4().hex,
        c=content,
        m=model,
        v="[" + ",".join(str(x) for x in embedding) + "]",
    )
    return rows[0][0]


TENANT_TABLES = (
    "personas", "review_batches", "pending_facts", "core_facts", "emotional_states", "emotional_overrides",
    "messages", "memory_outbox", "episodic_memories", "contacts", "access_grants", "diplomat_audit_log", "events",
    "proactive_schedule", "contact_invites", "notification_outbox", "event_proposals", "event_drafts",
    "message_attachments", "proactive_checkin_log", "tasks", "task_events", "cards", "card_reviews",
    "character_profiles", "xp_ledger", "tavern_cheers", "exercises", "workout_sessions", "workout_sets", "recipe_history", "inventory_items", "inventory_movements", "places", "geo_notes", "time_capsules", "daily_mood", "user_sessions",
)  # fmt: skip

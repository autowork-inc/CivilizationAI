"""Tier 3 / Outbox / Tier 2: messages, crash-safe outbox claiming, pgvector search under RLS."""

from __future__ import annotations

from sqlalchemy import text

from tests.helpers import new_user, owner_exec, pg_error, tenant_exec, vec

# ------------------------------------------------------------------ messages


async def test_replayed_telegram_update_cannot_duplicate_a_message(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    sql = "INSERT INTO civ.messages (user_id, role, content, telegram_message_id) VALUES (:u, 'user', 'hi', :t)"
    assert await tenant_exec(db, a, sql, u=a, t=7) == 1
    with pg_error("23505"):
        await tenant_exec(db, a, sql, u=a, t=7)
    assert await tenant_exec(db, a, sql, u=a, t=None) == 1  # messages without a Telegram id (system) may repeat
    assert await tenant_exec(db, a, sql, u=a, t=None) == 1
    assert await tenant_exec(db, b, sql, u=b, t=7) == 1  # message ids are per tenant (each has its own forum chat)


# ------------------------------------------------------------------ outbox


def _outbox(extra_cols: str = "", extra_vals: str = "") -> str:
    return (
        f"INSERT INTO civ.memory_outbox (user_id, operation, payload, idempotency_key{extra_cols}) "
        f"VALUES (:u, 'compress_day', '{{}}', :k{extra_vals})"
    )


async def test_outbox_constraints(db, owner_engine):
    u = await new_user(owner_engine)
    assert await tenant_exec(db, u, _outbox(), u=u, k="day:2026-10-02") == 1
    with pg_error("23505"):  # the same logical task cannot be enqueued twice
        await tenant_exec(db, u, _outbox(), u=u, k="day:2026-10-02")
    with pg_error("23514"):  # 'done' must carry processed_at ...
        await tenant_exec(db, u, _outbox(", status", ", 'done'"), u=u, k="a")
    with pg_error("23514"):  # ... and a pending task must not
        await tenant_exec(db, u, _outbox(", processed_at", ", now()"), u=u, k="b")
    with pg_error("23514"):
        await tenant_exec(db, u, _outbox(", attempts", ", -1"), u=u, k="c")
    with pg_error("23514"):
        await tenant_exec(db, u, _outbox(", max_attempts", ", 0"), u=u, k="d")


async def test_done_rows_are_kept_until_the_ttl_purge(db, owner_engine):
    u = await new_user(owner_engine)
    rows = {
        "old_done": ("done", "now() - interval '8 days'"),
        "fresh_done": ("done", "now() - interval '1 day'"),
        "old_dead": ("dead", "now() - interval '30 days'"),
        "open": ("pending", "NULL"),
    }
    for key, (status, processed) in rows.items():
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.memory_outbox (user_id, operation, payload, idempotency_key, status, processed_at) "
            f"VALUES (:u, 'embed_episode', '{{}}', :k, CAST(:s AS civ.outbox_status), {processed})",
            u=u, k=key, s=status,
        )  # fmt: skip
    purged = await tenant_exec(
        db, u, "DELETE FROM civ.memory_outbox WHERE status = 'done' AND processed_at < now() - interval '7 days'"
    )
    assert purged == 1  # the Celery TTL task: only old DONE rows go; fresh done, dead and open rows stay
    left = {r[0] for r in await tenant_exec(db, u, "SELECT idempotency_key FROM civ.memory_outbox")}
    assert left == {"fresh_done", "old_dead", "open"}


CLAIM = (
    "SELECT id FROM civ.memory_outbox "
    "WHERE status IN ('pending', 'failed', 'processing') AND attempts < max_attempts "
    "AND (locked_until IS NULL OR locked_until < now()) "
    "ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1"
)


async def test_two_workers_never_claim_the_same_task_and_dead_leases_are_reclaimed(db, owner_engine):
    u = await new_user(owner_engine)
    ids = []
    for key in ("t1", "t2", "t3"):  # separate transactions => increasing created_at
        ids.append((await owner_exec(owner_engine, _outbox() + " RETURNING id", u=u, k=key))[0][0])

    async with db.tenant_session(u) as worker1:
        first = (await worker1.execute(text(CLAIM))).scalar_one()
        async with db.tenant_session(u) as worker2:  # runs while worker1 still holds its row lock
            second = (await worker2.execute(text(CLAIM))).scalar_one()
        assert first == ids[0] and second == ids[1]  # SKIP LOCKED: no double processing

    # worker "crashed" after claiming t1: lease still valid -> nobody takes it; lease expired -> it is reclaimed
    await owner_exec(
        owner_engine,
        "UPDATE civ.memory_outbox SET status = 'processing', attempts = 1, locked_until = now() + interval '10 minutes' "
        "WHERE id = :i",
        i=ids[0],
    )
    assert (await tenant_exec(db, u, CLAIM))[0][0] == ids[1]
    await owner_exec(owner_engine, "UPDATE civ.memory_outbox SET locked_until = now() - interval '1 minute' WHERE id = :i", i=ids[0])
    assert (await tenant_exec(db, u, CLAIM))[0][0] == ids[0]
    # out of attempts -> not claimable any more (to be marked 'dead' for manual review)
    await owner_exec(owner_engine, "UPDATE civ.memory_outbox SET attempts = max_attempts WHERE id = :i", i=ids[0])
    assert (await tenant_exec(db, u, CLAIM))[0][0] == ids[1]


# ------------------------------------------------------------------ episodic memory / pgvector

INSERT_EPISODE = (
    "INSERT INTO civ.episodic_memories (user_id, idempotency_key, kind, content, embedding, embedding_model, "
    "occurred_from, occurred_to) VALUES (:u, :k, 'dialogue_chunk', :c, CAST(:v AS vector), "
    "'text-embedding-3-small', now(), now())"
)


async def test_vector_search_is_ordered_and_tenant_scoped(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    await tenant_exec(db, a, INSERT_EPISODE, u=a, k="near", c="n", v=vec((0, 1.0)))
    await tenant_exec(db, a, INSERT_EPISODE, u=a, k="mid", c="m", v=vec((0, 0.8), (1, 0.6)))
    await tenant_exec(db, a, INSERT_EPISODE, u=a, k="far", c="f", v=vec((1, 1.0)))
    await tenant_exec(db, b, INSERT_EPISODE, u=b, k="b_identical", c="secret of b", v=vec((0, 1.0)))
    rows = await tenant_exec(
        db, a,
        "SELECT idempotency_key, embedding <=> CAST(:q AS vector) AS dist FROM civ.episodic_memories "
        "ORDER BY embedding <=> CAST(:q AS vector) LIMIT 10",
        q=vec((0, 1.0)),
    )  # fmt: skip
    assert [r[0] for r in rows] == ["near", "mid", "far"]  # b's identical vector is invisible to a
    assert abs(rows[0][1]) < 1e-9 and abs(rows[1][1] - 0.2) < 1e-6 and abs(rows[2][1] - 1.0) < 1e-9


async def test_episode_upsert_is_idempotent(db, owner_engine):
    u = await new_user(owner_engine)
    upsert = (
        INSERT_EPISODE
        + " ON CONFLICT (user_id, idempotency_key) DO UPDATE SET content = EXCLUDED.content, embedding = EXCLUDED.embedding"
    )
    await tenant_exec(db, u, upsert, u=u, k="day:1", c="first run", v=vec((0, 1.0)))
    await tenant_exec(db, u, upsert, u=u, k="day:1", c="retried run", v=vec((1, 1.0)))
    assert await tenant_exec(db, u, "SELECT content FROM civ.episodic_memories") == [("retried run",)]


async def test_wrong_embedding_dimension_is_rejected(db, owner_engine):
    u = await new_user(owner_engine)
    with pg_error("22000", "1536 dimensions"):
        await tenant_exec(db, u, INSERT_EPISODE, u=u, k="bad", c="x", v="[1,2,3]")


async def test_hnsw_cosine_index_exists_and_is_valid(owner_engine):
    rows = await owner_exec(
        owner_engine,
        "SELECT am.amname, i.indisvalid, oc.opcname FROM pg_class c "
        "JOIN pg_index i ON i.indexrelid = c.oid JOIN pg_am am ON am.oid = c.relam "
        "JOIN pg_opclass oc ON oc.oid = i.indclass[0] "
        "WHERE c.relname = 'ix_episodic_memories_embedding_hnsw'",
    )
    assert rows == [("hnsw", True, "vector_cosine_ops")]

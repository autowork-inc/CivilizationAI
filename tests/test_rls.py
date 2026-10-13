"""Tenant isolation: Row Level Security, composite tenant-safe FKs, privileges, tenant bootstrap, cascade delete."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from ai_civ.db import RlsNotEnforcedError
from tests.helpers import (
    TENANT_TABLES,
    new_fact,
    new_persona,
    new_user,
    owner_exec,
    pg_error,
    seed_full_user,
    tenant_exec,
)


async def test_every_table_has_user_id_and_rls(owner_engine):
    """Project rule: multi-tenant by design. Also guards FUTURE migrations (every new table must comply)."""
    rows = await owner_exec(
        owner_engine,
        "SELECT c.relname, c.relrowsecurity, "
        "  EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'user_id' AND NOT a.attisdropped), "
        "  (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'civ' AND c.relkind = 'r' AND c.relname <> 'alembic_version'",
    )
    names = {r[0] for r in rows}
    assert names == set(TENANT_TABLES) | {"users"}
    for name, rls, has_user_id, n_policies in rows:
        assert rls, f"{name}: RLS is not enabled"
        assert n_policies >= 1, f"{name}: no policy"
        assert has_user_id or name == "users", f"{name}: no user_id column"


async def test_tenant_sees_only_own_rows(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    pa, pb = await new_persona(owner_engine, a), await new_persona(owner_engine, b)
    fa = await new_fact(owner_engine, a, "weight", category="body")
    await new_fact(owner_engine, b, "weight", category="body")
    assert {r[0] for r in await tenant_exec(db, a, "SELECT id FROM civ.personas")} == {pa}
    assert pb not in {r[0] for r in await tenant_exec(db, a, "SELECT id FROM civ.personas")}
    assert [r[0] for r in await tenant_exec(db, a, "SELECT id FROM civ.core_facts")] == [fa]
    assert [r[0] for r in await tenant_exec(db, a, "SELECT id FROM civ.users")] == [a]


async def test_no_tenant_context_fails_fast(db):
    # fresh connection: GUC never set -> "unrecognized configuration parameter"
    with pg_error("42704"):
        async with db.engine.begin() as conn:
            await conn.execute(text("SELECT count(*) FROM civ.core_facts"))


async def test_reused_connection_without_tenant_also_fails(db, owner_engine):
    a = await new_user(owner_engine)
    async with db.engine.connect() as conn:
        async with conn.begin():
            await conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(a)})
        # same physical connection, transaction ended: the GUC reads back as '' -> uuid cast error (still an error)
        with pg_error("22P02"):
            async with conn.begin():
                await conn.execute(text("SELECT count(*) FROM civ.core_facts"))


async def test_cannot_insert_row_for_another_tenant(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    with pg_error("42501", "row-level security"):
        await tenant_exec(
            db, a, "INSERT INTO civ.personas (user_id, code, title, system_prompt) VALUES (:b, 'x', 'X', 'p')", b=b
        )


async def test_cannot_move_or_touch_another_tenants_rows(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    pa, pb = await new_persona(owner_engine, a), await new_persona(owner_engine, b)
    with pg_error("42501", "row-level security"):  # WITH CHECK: new row would belong to tenant b
        await tenant_exec(db, a, "UPDATE civ.personas SET user_id = :b WHERE id = :p", b=b, p=pa)
    assert await tenant_exec(db, a, "UPDATE civ.personas SET title = 'hacked' WHERE id = :p", p=pb) == 0
    assert await tenant_exec(db, a, "DELETE FROM civ.personas WHERE id = :p", p=pb) == 0
    still = await owner_exec(owner_engine, "SELECT title FROM civ.personas WHERE id = :p", p=pb)
    assert still[0][0] != "hacked"


async def test_composite_fk_blocks_cross_tenant_reference(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    persona_of_b = await new_persona(owner_engine, b)
    with pg_error("23503"):
        await tenant_exec(
            db,
            a,
            "INSERT INTO civ.core_facts (user_id, persona_id, category, key, value_text, source) "
            "VALUES (:a, :p, 'body', 'weight', '80', 'user_manual')",
            a=a,
            p=persona_of_b,
        )


async def test_view_runs_with_invoker_rights(db, owner_engine):
    """effective_principles is security_invoker: tenant RLS applies through it. A plain view would leak."""
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    fa = await new_fact(owner_engine, a, "principle.a")
    await new_fact(owner_engine, b, "principle.b")
    assert [r[0] for r in await tenant_exec(db, a, "SELECT fact_id FROM civ.effective_principles")] == [fa]

    # negative control: the same view WITHOUT security_invoker really does bypass RLS (proves the option matters)
    await owner_exec(owner_engine, "CREATE OR REPLACE VIEW civ.tmp_leaky AS SELECT user_id FROM civ.core_facts")
    try:
        await owner_exec(owner_engine, "GRANT SELECT ON civ.tmp_leaky TO ai_civ_app")
        leaked = {r[0] for r in await tenant_exec(db, a, "SELECT user_id FROM civ.tmp_leaky")}
        assert b in leaked, "expected a default (security definer) view to leak other tenants"
    finally:
        await owner_exec(owner_engine, "DROP VIEW civ.tmp_leaky")


async def test_audit_log_is_read_only_for_the_app_role(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision) "
        "VALUES (:a, :b, 'calendar_free_busy', 'allowed'), (:b, :a, 'calendar_free_busy', 'denied')",
        a=a,
        b=b,
    )
    rows = await tenant_exec(db, a, "SELECT user_id, requester_user_id FROM civ.diplomat_audit_log")
    assert [(r[0], r[1]) for r in rows] == [(a, b)]
    with pg_error("42501", "permission denied"):
        await tenant_exec(
            db,
            a,
            "INSERT INTO civ.diplomat_audit_log (user_id, requester_user_id, scope, decision) "
            "VALUES (:a, :b, 'calendar_free_busy', 'allowed')",
            a=a,
            b=b,
        )


async def test_app_role_cannot_touch_alembic_version(db, owner_engine):
    a = await new_user(owner_engine)
    with pg_error("42501", "permission denied"):
        await tenant_exec(db, a, "SELECT * FROM civ.alembic_version")


async def test_resolve_tenant_id_bootstrap(db, owner_engine):
    a = await new_user(owner_engine)
    tg, chat = (await owner_exec(owner_engine, "SELECT telegram_user_id, forum_chat_id FROM civ.users WHERE id = :a", a=a))[0]
    assert await db.resolve_tenant_id(telegram_user_id=tg) == a
    assert await db.resolve_tenant_id(forum_chat_id=chat) == a
    assert await db.resolve_tenant_id(telegram_user_id=1) is None
    # the tenant table itself stays closed without a tenant context
    with pg_error("42704"):
        async with db.engine.begin() as conn:
            await conn.execute(text("SELECT * FROM civ.users"))
    with pg_error("22023"):
        await db.resolve_tenant_id(telegram_user_id=tg, forum_chat_id=chat)
    with pg_error("22023"):
        await db.resolve_tenant_id()


async def test_verify_rls_enforced(db, owner_db):
    await db.verify_rls_enforced()  # runtime role: fine
    with pytest.raises(RlsNotEnforcedError):  # owner bypasses RLS: the app must refuse to start with it
        await owner_db.verify_rls_enforced()


async def test_tenant_session_requires_uuid(db):
    with pytest.raises(TypeError):
        async with db.tenant_session("not-a-uuid"):  # type: ignore[arg-type]
            pass


async def test_deleting_a_user_cascades_everything(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    await seed_full_user(owner_engine, a, b)
    for table in TENANT_TABLES:  # sanity: the seed really filled every table
        n = (await owner_exec(owner_engine, f"SELECT count(*) FROM civ.{table} WHERE user_id = :a", a=a))[0][0]
        assert n >= 1, f"seed left {table} empty"

    assert await tenant_exec(db, a, "DELETE FROM civ.users WHERE id = :a", a=a) == 1

    for table in TENANT_TABLES:
        n = (await owner_exec(owner_engine, f"SELECT count(*) FROM civ.{table} WHERE user_id = :a", a=a))[0][0]
        assert n == 0, f"{table} still has rows of the deleted tenant"
    # b's audit row that named a as requester survives, anonymised
    row = (
        await owner_exec(
            owner_engine, "SELECT requester_user_id FROM civ.diplomat_audit_log WHERE user_id = :b", b=b
        )
    )[0]
    assert row[0] is None
    assert (await owner_exec(owner_engine, "SELECT count(*) FROM civ.users WHERE id = :b", b=b))[0][0] == 1
    assert uuid.UUID(str(b))

"""Migration hygiene: models == schema, downgrade removes everything, offline SQL generation works."""

from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import _enable_app_login, alembic
from tests.helpers import OWNER_URL, new_user, owner_exec


async def _civ_objects() -> dict[str, list[str]]:
    engine = create_async_engine(OWNER_URL, poolclass=NullPool)
    queries = {
        "tables": "SELECT tablename FROM pg_tables WHERE schemaname = 'civ'",
        "views": "SELECT viewname FROM pg_views WHERE schemaname = 'civ'",
        "functions": "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'civ'",
        "types": "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
        "WHERE n.nspname = 'civ' AND t.typtype IN ('e', 'c') AND t.typrelid = 0",
    }
    result = {}
    async with engine.connect() as conn:
        for name, sql in queries.items():
            result[name] = sorted(r[0] for r in (await conn.execute(text(sql))).all())
    await engine.dispose()
    return result


def test_models_match_the_migration(migrated):
    """`alembic check`: tables, columns, indexes, unique constraints and FKs in the ORM models == the database."""
    result = alembic("check")
    assert result.returncode == 0, result.stderr[-1500:]
    assert "No new upgrade operations detected" in result.stdout + result.stderr


def test_downgrade_removes_everything_and_upgrade_is_repeatable(migrated):
    try:
        assert alembic("downgrade", "base").returncode == 0
        left = asyncio.run(_civ_objects())
        # only Alembic's own version table may remain (schema civ is kept on purpose)
        assert left == {"tables": ["alembic_version"], "views": [], "functions": [], "types": []}
    finally:
        up = alembic("upgrade", "head")
        asyncio.run(_enable_app_login())
    assert up.returncode == 0, up.stderr[-1500:]
    assert len(asyncio.run(_civ_objects())["tables"]) == 40  # 39 tables + alembic_version (0026 added user_sessions)


def test_downgrade_of_0008_keeps_the_meetings_and_drops_their_notifications(migrated):
    async def seed() -> uuid.UUID:
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            user = await new_user(engine)
            await owner_exec(
                engine,
                "INSERT INTO civ.events (user_id, title, starts_at, ends_at, timezone, created_via) "
                "VALUES (:u, 'Chess', now(), now() + interval '1 hour', 'UTC', 'peer_proposal')",
                u=user,
            )
            await owner_exec(
                engine,
                "INSERT INTO civ.notification_outbox (user_id, kind, payload, idempotency_key) "
                "VALUES (:u, 'event_proposal', '{}', 'downgrade-test')",
                u=user,
            )
            return user
        finally:
            await engine.dispose()

    async def look(user: uuid.UUID) -> tuple[list[str], int]:
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            sources = [r[0] for r in await owner_exec(engine, "SELECT created_via FROM civ.events WHERE user_id = :u", u=user)]
            left = (await owner_exec(engine, "SELECT count(*) FROM civ.notification_outbox WHERE user_id = :u", u=user))[0][0]
            return sources, left
        finally:
            await engine.dispose()

    user = asyncio.run(seed())
    try:
        down = alembic("downgrade", "0007_diplomat")
        assert down.returncode == 0, down.stderr[-1500:]
        assert asyncio.run(look(user)) == (["bot_confirmed"], 0)  # the meeting stays; only its label goes back to the old pair of values
        left = asyncio.run(_civ_objects())
        assert "event_proposals" not in left["tables"] and "event_drafts" not in left["tables"]
        assert not {"propose_event", "respond_event_proposal", "purge_events", "is_clean_title"} & set(left["functions"])
    finally:
        up = alembic("upgrade", "head")
        asyncio.run(_enable_app_login())
    assert up.returncode == 0, up.stderr[-1500:]


def test_downgrade_of_0010_drops_the_chat_facts_keeps_the_others_and_upgrade_works_again(migrated):
    async def seed() -> uuid.UUID:
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            user = await new_user(engine)
            [(msg,)] = await owner_exec(
                engine,
                "INSERT INTO civ.messages (user_id, role, content, telegram_message_id) VALUES (:u, 'user', 'я бегаю', 1) RETURNING id",
                u=user,
            )
            await owner_exec(
                engine,
                "INSERT INTO civ.core_facts (user_id, category, key, value_text, priority, source, source_message_id) "
                "VALUES (:u, 'preference', 'habit.run', 'Бегает', 50, CAST('chat_inline' AS civ.fact_source), :m)",
                u=user,
                m=msg,
            )
            await owner_exec(
                engine,
                "INSERT INTO civ.core_facts (user_id, category, key, value_text, priority, source) "
                "VALUES (:u, 'identity', 'city', 'Орхус', 60, 'onboarding')",
                u=user,
            )
            return user
        finally:
            await engine.dispose()

    async def look(user: uuid.UUID) -> tuple[list[str], bool]:
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            keys = [r[0] for r in await owner_exec(engine, "SELECT key FROM civ.core_facts WHERE user_id = :u ORDER BY key", u=user)]
            column = await owner_exec(
                engine,
                "SELECT 1 FROM information_schema.columns WHERE table_schema = 'civ' AND table_name = 'core_facts' AND column_name = 'source_message_id'",
            )
            return keys, bool(column)
        finally:
            await engine.dispose()

    user = asyncio.run(seed())
    try:
        down = alembic("downgrade", "0009_topic_titles")
        assert down.returncode == 0, down.stderr[-1500:]
        assert asyncio.run(look(user)) == (["city"], False)  # the chat fact is gone with its column; the onboarding fact stays
    finally:
        up = alembic("upgrade", "head")
        asyncio.run(_enable_app_login())
    assert up.returncode == 0, up.stderr[-1500:]
    assert asyncio.run(look(user)) == (["city"], True)  # and the schema is whole again (the enum value never left)


def test_offline_sql_generation(migrated):
    result = alembic("upgrade", "head", "--sql")
    assert result.returncode == 0, result.stderr[-1500:]
    sql = result.stdout
    assert "CREATE TABLE civ.core_facts" in sql and "ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions" in sql


def test_downgrade_of_0023_removes_the_penalties_keeps_the_earnings_and_upgrade_works_again(migrated):
    async def seed() -> uuid.UUID:
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            user = await new_user(engine)
            await owner_exec(engine, "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 30, 'body', 'earned:1')", u=user)
            await owner_exec(engine, "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, -10, 'body', 'decay:body:x:2026-10-07')", u=user)
            await owner_exec(engine, "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 0, 'focus', 'penalty:task:marker')", u=user)
            return user
        finally:
            await engine.dispose()

    async def amounts(user: uuid.UUID) -> list[int]:
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            return [r[0] for r in await owner_exec(engine, "SELECT amount FROM civ.xp_ledger WHERE user_id = :u ORDER BY amount", u=user)]
        finally:
            await engine.dispose()

    user = asyncio.run(seed())
    assert asyncio.run(amounts(user)) == [-10, 0, 30]
    try:
        down = alembic("downgrade", "0022_time_capsules")
        assert down.returncode == 0, down.stderr[-1500:]
        assert asyncio.run(amounts(user)) == [30]  # the penalties cannot exist under the old CHECK: gone; the earnings stay
        left = asyncio.run(_civ_objects())
        assert not {"apply_xp_penalty", "xp_streak_days", "xp_ledger_floor"} & set(left["functions"])
    finally:
        up = alembic("upgrade", "head")
        asyncio.run(_enable_app_login())
    assert up.returncode == 0, up.stderr[-1500:]
    assert {"apply_xp_penalty", "xp_streak_days", "xp_ledger_floor"} <= set(asyncio.run(_civ_objects())["functions"])
    assert asyncio.run(amounts(user)) == [30]


def test_downgrade_of_0024_drops_the_mood_estimates_and_the_checkin_preference_and_upgrade_works_again(migrated):
    async def seed() -> uuid.UUID:
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            user = await new_user(engine)
            await owner_exec(engine, "INSERT INTO civ.daily_mood (user_id, local_date, score, message_count) VALUES (:u, current_date, 1, 3)", u=user)
            await owner_exec(engine, "UPDATE civ.users SET checkin_per_day = 2 WHERE id = :u", u=user)
            return user
        finally:
            await engine.dispose()

    async def has_column() -> bool:
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            rows = await owner_exec(
                engine, "SELECT 1 FROM information_schema.columns WHERE table_schema = 'civ' AND table_name = 'users' AND column_name = 'checkin_per_day'"
            )
            return bool(rows)
        finally:
            await engine.dispose()

    user = asyncio.run(seed())
    assert asyncio.run(has_column())
    try:
        down = alembic("downgrade", "0023_xp_decay")
        assert down.returncode == 0, down.stderr[-1500:]
        assert "daily_mood" not in asyncio.run(_civ_objects())["tables"] and not asyncio.run(has_column())
    finally:
        up = alembic("upgrade", "head")
        asyncio.run(_enable_app_login())
    assert up.returncode == 0, up.stderr[-1500:]
    assert "daily_mood" in asyncio.run(_civ_objects())["tables"] and asyncio.run(has_column())
    assert user  # the tenant itself survives the round trip


def test_downgrade_of_0025_drops_relocate_group_and_upgrade_works_again(migrated):
    try:
        down = alembic("downgrade", "0024_daily_mood")
        assert down.returncode == 0, down.stderr[-1500:]
        assert "relocate_group" not in asyncio.run(_civ_objects())["functions"]
    finally:
        up = alembic("upgrade", "head")
        asyncio.run(_enable_app_login())
    assert up.returncode == 0, up.stderr[-1500:]
    assert "relocate_group" in asyncio.run(_civ_objects())["functions"]


def test_downgrade_of_0026_removes_the_app_support_and_the_app_people_and_upgrade_works_again(migrated):
    async def seed() -> uuid.UUID:
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            rows = await owner_exec(engine, "SELECT tenant_id FROM civ.register_app_user('Downgrade@Example.com', 'hash', 'UTC')")
            return rows[0][0]
        finally:
            await engine.dispose()

    async def count(sql: str) -> int:
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            return (await owner_exec(engine, sql))[0][0]
        finally:
            await engine.dispose()

    app_user = asyncio.run(seed())
    assert asyncio.run(count("SELECT count(*) FROM civ.personas WHERE user_id = '%s'" % app_user)) == 2
    try:
        down = alembic("downgrade", "0025_relocate_group")
        assert down.returncode == 0, down.stderr[-1500:]
        left = asyncio.run(_civ_objects())
        assert "user_sessions" not in left["tables"] and "register_app_user" not in left["functions"]
        assert asyncio.run(count("SELECT count(*) FROM civ.users WHERE id = '%s'" % app_user)) == 0
    finally:
        up = alembic("upgrade", "head")
        asyncio.run(_enable_app_login())
    assert up.returncode == 0, up.stderr[-1500:]
    left = asyncio.run(_civ_objects())
    assert "user_sessions" in left["tables"] and "resolve_tenant_by_email" in left["functions"]

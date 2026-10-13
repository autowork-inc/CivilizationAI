"""Database tests need a real PostgreSQL 15+ with pgvector.

Required environment:
    TEST_OWNER_DATABASE_URL = postgresql+asyncpg://<owner>:<pw>@host:port/<db>
The owner role must be able to CREATE SCHEMA / CREATE ROLE (Supabase "postgres" does), and the extension must already
exist in the database: CREATE SCHEMA extensions; CREATE EXTENSION vector WITH SCHEMA extensions;
WARNING: the fixture below runs `alembic downgrade base` + `upgrade head` and gives the role ai_civ_app a known test
password (roles are cluster-wide). Use a throwaway database on a throwaway server. As a safety net the run is refused
unless the database name ends with "_test" (override: CIV_ALLOW_DESTRUCTIVE_TESTS=1).
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ai_civ.db import Database
from tests.helpers import APP_PASSWORD, APP_URL, OWNER_URL

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def alembic(*args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "MIGRATION_DATABASE_URL": OWNER_URL or ""}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


async def _enable_app_login() -> None:
    engine = create_async_engine(OWNER_URL, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.execute(text(f"ALTER ROLE ai_civ_app WITH LOGIN PASSWORD '{APP_PASSWORD}'"))
    await engine.dispose()


@pytest.fixture(scope="session")
def migrated() -> None:
    """Fresh schema: downgrade base -> upgrade head, then allow ai_civ_app to log in (as an operator would)."""
    if not OWNER_URL:
        pytest.skip("TEST_OWNER_DATABASE_URL is not set: database tests skipped")
    database = make_url(OWNER_URL).database or ""
    if not database.endswith("_test") and os.environ.get("CIV_ALLOW_DESTRUCTIVE_TESTS") != "1":
        pytest.exit(
            f"Refusing to run: these tests DROP the civ schema and reset the password of role ai_civ_app, "
            f"but the database {database!r} does not end with '_test'. Point TEST_OWNER_DATABASE_URL at a throwaway database.",
            returncode=2,
        )
    for step in (("downgrade", "base"), ("upgrade", "head")):
        result = alembic(*step)
        assert result.returncode == 0, f"alembic {' '.join(step)} failed:\n{result.stderr[-2000:]}"
    asyncio.run(_enable_app_login())


@pytest_asyncio.fixture
async def owner_engine(migrated):
    engine = create_async_engine(OWNER_URL, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db(migrated):
    """Application-side Database: connects as ai_civ_app, Row Level Security enforced."""
    database = Database(create_async_engine(APP_URL, poolclass=NullPool))
    yield database
    await database.dispose()


@pytest_asyncio.fixture
async def bot_env(owner_engine, db):
    """Module 2: a tenant wired to the real Orchestrator and aiogram Dispatcher (fake Telegram wire, fake Gemini)."""
    from tests.bot_env import build_bot_env

    env = await build_bot_env(owner_engine, db)
    yield env
    await env.bot.session.close()


@pytest_asyncio.fixture
async def quick_env(owner_engine, db):
    """``bot_env`` with quick events on, as in production: the model is offered ``create_event`` whether or not the user has contacts."""
    from tests.bot_env import build_bot_env

    env = await build_bot_env(owner_engine, db, quick_events_enabled=True)
    yield env
    await env.bot.session.close()


@pytest_asyncio.fixture
async def rename_env(owner_engine, db):
    """``bot_env`` with autonomous topic renaming on, as in production: inside a topic of the bot's own the model is offered ``rename_current_topic``."""
    from tests.bot_env import build_bot_env

    env = await build_bot_env(owner_engine, db, topic_rename_enabled=True)
    yield env
    await env.bot.session.close()


@pytest_asyncio.fixture
async def fact_env(owner_engine, db):
    """``bot_env`` with instant memory on, as in production: every reply to a message of 8+ characters offers the model ``save_fact``."""
    from tests.bot_env import build_bot_env

    env = await build_bot_env(owner_engine, db, save_fact_enabled=True)
    yield env
    await env.bot.session.close()


@pytest_asyncio.fixture
async def onboarding_env(owner_engine, db):
    """Like ``bot_env`` but the tenant has just been registered: onboarding is 'pending'."""
    from tests.bot_env import build_bot_env

    env = await build_bot_env(owner_engine, db, onboarding_state="pending")
    yield env
    await env.bot.session.close()


@pytest_asyncio.fixture
async def bare_env(owner_engine, db):
    """Module 5: the bot stack with no tenant at all - a person registers by adding the bot to a group."""
    from tests.bot_env import build_bare_env

    env = await build_bare_env(owner_engine, db)
    yield env
    await env.bot.session.close()


@pytest_asyncio.fixture
async def owner_db(migrated):
    """Database bound to the OWNER role - only to prove that verify_rls_enforced() rejects it."""
    database = Database(create_async_engine(OWNER_URL, poolclass=NullPool))
    yield database
    await database.dispose()

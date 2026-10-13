"""One-shot database bootstrap: ``python -m ai_civ.cli.init_db`` (the ``migrate`` service of docker-compose.yml runs it before anything else).

  1. ``alembic upgrade head`` under MIGRATION_DATABASE_URL (the OWNER role). Migration 0001 creates the runtime role ``ai_civ_app`` as NOLOGIN
     (if missing) - nobody has to run SQL by hand.
  2. Enables that role: ``ALTER ROLE ai_civ_app WITH LOGIN PASSWORD '<the password from DATABASE_URL>'``. The password comes ONLY from .env;
     the placeholder ``CHANGE_ME`` is refused. Running it again re-applies the password, so changing .env and restarting rotates it.
  3. Proves the result the way the bot will use it: connects with DATABASE_URL and checks that Row Level Security applies to that role.

Every step is idempotent: on an up-to-date database the run changes nothing but (harmlessly) re-sets the same password. Any failure exits non-zero,
and with it the services that wait for ``migrate`` (``service_completed_successfully``) do not start - a broken database never gets a bot on top of it.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from ai_civ.config import ConfigError, get_database_url, get_migration_database_url, get_settings
from ai_civ.db import Database

APP_ROLE = "ai_civ_app"
PLACEHOLDER = "CHANGE_ME"
_POOLER_SUFFIX = re.compile(r"^" + APP_ROLE + r"\.[A-Za-z0-9_-]+$")  # Supabase poolers address a role as <role>.<project-ref>


def app_credentials(database_url: str) -> tuple[str, str]:
    """(role name, password) of the runtime role from DATABASE_URL. Raises ConfigError for anything that must not be applied."""
    url = make_url(database_url)
    user = url.username or ""
    if user != APP_ROLE and not _POOLER_SUFFIX.match(user):
        raise ConfigError(f"DATABASE_URL must log in as {APP_ROLE} (the runtime role, subject to RLS), not {user!r}")
    password = url.password or ""
    if not password or PLACEHOLDER in password:
        raise ConfigError(f"set a real password for {APP_ROLE} in DATABASE_URL (it is still empty or {PLACEHOLDER})")
    return APP_ROLE, password


async def enable_app_login(owner: AsyncEngine, role: str, password: str) -> None:
    """LOGIN + password for the runtime role. The statement is built by PostgreSQL itself (``format('%I', '%L')``), so no quoting happens here."""
    async with owner.begin() as conn:
        exists = (await conn.execute(text("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = :r"), {"r": role})).first()
        if exists is None:
            raise RuntimeError(f"role {role} does not exist: the migrations did not create it")
        statement = (
            await conn.execute(text("SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', CAST(:r AS text), CAST(:p AS text))"), {"r": role, "p": password})
        ).scalar_one()
        await conn.execute(text(statement))


def run_migrations(cwd: Path) -> None:
    if not (cwd / "alembic.ini").is_file():
        raise RuntimeError(f"alembic.ini not found in {cwd} (run from the project directory)")
    result = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=cwd, env=os.environ.copy(), timeout=900)
    if result.returncode != 0:
        raise RuntimeError(f"alembic upgrade head failed (exit code {result.returncode})")


async def verify_runtime_login(database_url: str) -> None:
    """The bot's own view: connect as ai_civ_app and make sure RLS is enforced for it (raises otherwise)."""
    db = Database(create_async_engine(database_url, poolclass=NullPool))
    try:
        await db.verify_rls_enforced()
    finally:
        await db.dispose()


async def bootstrap(cwd: Path) -> None:
    settings = get_settings()
    owner_url = get_migration_database_url(settings)
    app_url = get_database_url(settings)
    role, password = app_credentials(app_url)  # fail before touching the database when .env still holds placeholders
    print("init-db: applying migrations (alembic upgrade head)", flush=True)
    await asyncio.to_thread(run_migrations, cwd)
    owner = create_async_engine(owner_url, poolclass=NullPool)
    try:
        await enable_app_login(owner, role, password)
    finally:
        await owner.dispose()
    print(f"init-db: role {role} can log in", flush=True)
    await verify_runtime_login(app_url)
    print("init-db: ready (Row Level Security is enforced for the runtime role)", flush=True)


def main() -> int:
    try:
        asyncio.run(bootstrap(Path(os.environ.get("ALEMBIC_DIR", "."))))
    except (ConfigError, RuntimeError) as exc:
        print(f"init-db: FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # a connection refused, a bad password, a migration error: say so and fail the stack
        print(f"init-db: FAILED: {exc!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

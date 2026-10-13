"""Alembic environment (async). Reads MIGRATION_DATABASE_URL - the OWNER role, never the runtime role."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import ai_civ.db.models  # noqa: F401  (registers all models on Base.metadata)
from ai_civ.config import get_migration_database_url
from ai_civ.db.base import SCHEMA, Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Same search_path as the runtime role: lets reflection resolve the pgvector type living in schema "extensions".
SEARCH_PATH = f"{SCHEMA}, extensions, public"


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    if type_ == "schema":
        return name == SCHEMA
    if type_ == "table":
        return name != "alembic_version"
    return True


def compare_type(context, inspected_column, metadata_column, inspected_type, metadata_type):
    """Enums: equal if name and values match. Reflection omits the schema for types on the search_path, the models
    always state it, which Alembic would otherwise report as a type change. Everything else: default comparison."""
    if isinstance(metadata_type, sa.Enum) and isinstance(inspected_type, sa.Enum):
        return metadata_type.name != inspected_type.name or list(metadata_type.enums) != list(inspected_type.enums)
    return None


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_name=include_name,
        compare_type=compare_type,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout (alembic upgrade head --sql) without a database connection."""
    _configure(url=get_migration_database_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(get_migration_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        # The version table lives in schema civ, so the schema must exist before Alembic touches it.
        # Committed separately: otherwise SQLAlchemy 2.x autobegin would swallow Alembic's own transaction.
        await connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        await connection.execute(text(f"SET search_path TO {SEARCH_PATH}"))
        await connection.commit()
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

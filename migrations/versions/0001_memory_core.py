"""Module 1 "Brain & Memory": schema civ, pgvector, Core Profile, Emotional Overrides, quarantine, Outbox,
episodic memory, ACL foundation, schedule, Row Level Security.

Revision ID: 0001_memory_core
Revises:
Create Date: 2026-10-03

The DDL lives in plain SQL files (migrations/sql/0001/*.sql) so that it can be reviewed and syntax-highlighted as SQL.
Statements are separated by a line that contains only "--;;" (dollar-quoted function bodies make a naive split on ';'
unsafe). Each statement is sent to the driver on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0001_memory_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / "0001"
UP_FILES = (
    "01_foundation.sql",
    "02_identity.sql",
    "03_brain_core.sql",
    "04_memory.sql",
    "05_social_schedule.sql",
    "06_security.sql",
)
DOWN_FILES = ("99_down.sql",)
DELIMITER = "--;;"


def _statements(filename: str) -> list[str]:
    text = (SQL_DIR / filename).read_text(encoding="utf-8")
    chunks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.strip() == DELIMITER:
            chunks.append("\n".join(current))
            current = []
        else:
            current.append(line)
    chunks.append("\n".join(current))
    # drop chunks that contain only comments / whitespace
    return [
        chunk.strip()
        for chunk in chunks
        if any(ln.strip() and not ln.strip().startswith("--") for ln in chunk.splitlines())
    ]


def _run(files: Sequence[str]) -> None:
    # Resolve vector / schema-qualified names the same way the runtime role does. LOCAL: ends with the transaction.
    op.execute("SET LOCAL search_path TO civ, extensions, public")
    for filename in files:
        for statement in _statements(filename):
            # op.execute works online and in --sql (offline) mode. SQL files must not contain ":name" sequences
            # (SQLAlchemy text() would treat them as bind parameters); "::type" casts are fine.
            op.execute(statement)


def upgrade() -> None:
    _run(UP_FILES)


def downgrade() -> None:
    _run(DOWN_FILES)

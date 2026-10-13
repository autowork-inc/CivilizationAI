"""Topic titles: who set the current name and when (autonomous topic renaming with a cooldown).

Revision ID: 0009_topic_titles
Revises: 0008_event_proposals
Create Date: 2026-10-04

Same SQL-file mechanism as 0001 - 0008 (statements separated by a line containing only "--;;").
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "0009_topic_titles"
down_revision: str | None = "0008_event_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SQL_DIR = Path(__file__).resolve().parent.parent / "sql" / "0009"
UP_FILES = ("01_topic_titles.sql",)
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
    return [
        chunk.strip()
        for chunk in chunks
        if any(ln.strip() and not ln.strip().startswith("--") for ln in chunk.splitlines())
    ]


def _run(files: Sequence[str]) -> None:
    op.execute("SET LOCAL search_path TO civ, extensions, public")
    for filename in files:
        for statement in _statements(filename):
            op.execute(statement)


def upgrade() -> None:
    _run(UP_FILES)


def downgrade() -> None:
    _run(DOWN_FILES)

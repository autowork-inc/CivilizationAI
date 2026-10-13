"""Tier 2 retrieval: nearest episodic memories of ONE tenant (pgvector cosine distance, under RLS)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from ai_civ.db import Database
from ai_civ.db.models import EpisodicMemory


@dataclass(frozen=True)
class Fragment:
    id: uuid.UUID
    content: str
    occurred_from: datetime
    distance: float


async def has_episodes(db: Database, user_id: uuid.UUID, *, embedding_model: str | None = None) -> bool:
    """Cheap pre-check: does the tenant have anything to search? Lets the caller skip the query embedding entirely.
    With ``embedding_model`` only rows embedded by that model count (see ``search_episodes``)."""
    query = select(EpisodicMemory.id).limit(1)
    if embedding_model is not None:
        query = query.where(EpisodicMemory.embedding_model == embedding_model)
    async with db.tenant_session(user_id) as session:
        return (await session.execute(query)).first() is not None


async def search_episodes(
    db: Database,
    user_id: uuid.UUID,
    query_embedding: list[float],
    *,
    top_k: int,
    embedding_model: str | None = None,
) -> list[Fragment]:
    """Top-k episodes by cosine distance. The tenant filter is enforced by RLS inside ``tenant_session``.

    ``embedding_model``: when given, only rows embedded by that model are compared - distances between vectors of
    different models mean nothing, so after a model change the old rows must not be mixed in.

    Note: with pgvector >= 0.8.0 add ``SET LOCAL hnsw.iterative_scan = relaxed_order`` here so a filtered HNSW scan cannot
    return fewer rows than requested (the checked-against server runs 0.6.0, which has no such setting).
    """
    if top_k < 1:
        raise ValueError("top_k must be >= 1")
    distance = EpisodicMemory.embedding.cosine_distance(query_embedding).label("distance")
    query = select(EpisodicMemory.id, EpisodicMemory.content, EpisodicMemory.occurred_from, distance)
    if embedding_model is not None:
        query = query.where(EpisodicMemory.embedding_model == embedding_model)
    async with db.tenant_session(user_id) as session:
        rows = (await session.execute(query.order_by(distance, EpisodicMemory.id).limit(top_k))).all()
    return [Fragment(id=r.id, content=r.content, occurred_from=r.occurred_from, distance=float(r.distance)) for r in rows]

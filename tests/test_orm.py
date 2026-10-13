"""The ORM models + Database.tenant_session work against the real schema (the onboarding path, end to end)."""

from __future__ import annotations

import random
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select

from ai_civ.db.enums import (
    EpisodeKind,
    FactCategory,
    FactSource,
    OutboxOperation,
    OutboxStatus,
)
from ai_civ.db.models import CoreFact, EpisodicMemory, Event, MemoryOutbox, Persona, User
from tests.helpers import EMBEDDING_DIM, new_user


def unit_vector(index: int) -> list[float]:
    values = [0.0] * EMBEDDING_DIM
    values[index] = 1.0
    return values


async def test_orm_onboarding_roundtrip(db):
    uid = uuid.uuid4()  # the app generates the tenant id, binds the session to it, then creates the user row
    now = datetime.now(UTC)
    async with db.tenant_session(uid) as session:
        session.add(
            User(
                id=uid,
                telegram_user_id=random.randrange(10**9, 10**15),
                forum_chat_id=-random.randrange(10**9, 10**15),
                timezone="Europe/Copenhagen",
            )
        )
        await session.flush()
        persona = Persona(user_id=uid, code="foodguide", title="Foodguide", system_prompt="You are a nutrition coach")
        session.add(persona)
        await session.flush()
        session.add_all(
            [
                CoreFact(
                    user_id=uid, persona_id=persona.id, category=FactCategory.body, key="weight",
                    value_num=Decimal("80.5"), value_unit="kg", source=FactSource.user_manual,
                ),
                EpisodicMemory(
                    user_id=uid, persona_id=persona.id, idempotency_key="day:2026-10-02", kind=EpisodeKind.daily_summary,
                    content="Talked about breakfast", embedding=unit_vector(0),
                    embedding_model="text-embedding-3-small", occurred_from=now, occurred_to=now,
                ),
                EpisodicMemory(
                    user_id=uid, persona_id=persona.id, idempotency_key="day:2026-10-03", kind=EpisodeKind.daily_summary,
                    content="Talked about training", embedding=unit_vector(1),
                    embedding_model="text-embedding-3-small", occurred_from=now, occurred_to=now,
                ),
                Event(
                    user_id=uid, persona_id=persona.id, title="Doctor", starts_at=now + timedelta(days=1),
                    ends_at=now + timedelta(days=1, hours=1), timezone="Europe/Copenhagen", created_via="mini_app",
                ),
                MemoryOutbox(
                    user_id=uid, operation=OutboxOperation.embed_episode, payload={"day": "2026-10-02"},
                    idempotency_key="embed:2026-10-02",
                ),
            ]
        )

    async with db.tenant_session(uid) as session:
        fact = (await session.execute(select(CoreFact))).scalar_one()
        assert fact.category is FactCategory.body and fact.value_num == Decimal("80.5") and fact.valid_to is None
        nearest = (
            await session.execute(
                select(EpisodicMemory).order_by(EpisodicMemory.embedding.cosine_distance(unit_vector(1))).limit(1)
            )
        ).scalar_one()
        assert nearest.idempotency_key == "day:2026-10-03"  # pgvector operator through the ORM, under RLS
        event = (await session.execute(select(Event))).scalar_one()
        assert event.timezone == "Europe/Copenhagen" and event.status.value == "planned"
        task = (await session.execute(select(MemoryOutbox))).scalar_one()
        assert task.status is OutboxStatus.pending and task.attempts == 0 and task.payload == {"day": "2026-10-02"}


async def test_orm_cannot_cross_tenants(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    async with db.tenant_session(a) as session:
        session.add(Persona(user_id=a, code="x", title="X", system_prompt="p"))
    async with db.tenant_session(b) as session:
        assert (await session.execute(select(Persona))).all() == []  # b sees nothing of a

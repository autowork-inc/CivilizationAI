"""Fixtures for Mini App API tests: real PostgreSQL (RLS), the real FastAPI app over ASGI, and "Telegram" that signs launch data.

``alice`` and ``bob`` are two registered tenants, each acting through its own signed ``Authorization: tma ...`` header. Import the
fixtures into a test module:  ``from tests.web_helpers import alice, bob, client  # noqa: F401``.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest_asyncio

from ai_civ.config import Settings
from ai_civ.web.app import create_app
from tests.helpers import new_fact, new_persona, new_user, owner_exec
from tests.test_web_auth import TOKEN, make_init_data


@pytest_asyncio.fixture
async def client(db):
    app = create_app(Settings(_env_file=None, telegram_bot_token=TOKEN), db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://web.test") as http:
        yield http


@dataclass
class Actor:
    """A registered tenant calling the API with launch data signed for ITS Telegram id."""

    http: httpx.AsyncClient
    owner_engine: Any
    user_id: uuid.UUID
    telegram_id: int

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"tma {make_init_data(user_id=self.telegram_id)}"}

    async def get(self, path: str, **kw: Any) -> httpx.Response:
        return await self.http.get(path, headers=self.headers, **kw)

    async def post(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.post(path, json=json, headers=self.headers, **kw)

    async def put(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.put(path, json=json, headers=self.headers, **kw)

    async def patch(self, path: str, json: Any = None, **kw: Any) -> httpx.Response:
        return await self.http.patch(path, json=json, headers=self.headers, **kw)

    async def delete(self, path: str, **kw: Any) -> httpx.Response:
        return await self.http.delete(path, headers=self.headers, **kw)

    # ---- direct database access (as the owner: sees every tenant, used to ARRANGE and to VERIFY)
    async def sql(self, query: str, **params: Any) -> list[Any]:
        return await owner_exec(self.owner_engine, query, u=self.user_id, **params)

    async def fact(self, key: str, *, value: str = "v", category: str = "identity", priority: int = 50) -> uuid.UUID:
        return await new_fact(self.owner_engine, self.user_id, key, category=category, value=value, priority=priority)

    async def persona(self, code: str = "bowling") -> uuid.UUID:
        return await new_persona(self.owner_engine, self.user_id, code, thread_id=random.randrange(10, 10**6), title=code.title())

    async def current_facts(self, like: str = "%") -> dict[str, tuple[Any, ...]]:
        rows = await self.sql(
            "SELECT key, id, value_text, priority, source::text, category::text FROM civ.core_facts "
            "WHERE user_id = :u AND valid_to IS NULL AND key LIKE :k",
            k=like,
        )
        return {r[0]: tuple(r[1:]) for r in rows}

    async def versions(self, key: str) -> list[Any]:
        return await self.sql(
            "SELECT id, value_text, valid_to IS NOT NULL AS closed, source::text FROM civ.core_facts "
            "WHERE user_id = :u AND key = :k ORDER BY valid_from, created_at",
            k=key,
        )

    async def state(self, label: str, *, priority: int, hours: float = 5, status: str = "active") -> uuid.UUID:
        """An emotional state (user_declared, so any priority is allowed) that is live for ``hours`` more hours."""
        rows = await self.sql(
            "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, status, started_at, expires_at) "
            "VALUES (:u, :l, 8, :p, 'user_declared', CAST(:st AS civ.emotion_status), now() - interval '1 hour', "
            "now() + make_interval(secs => :s)) RETURNING id",
            l=label, p=priority, st=status, s=hours * 3600,
        )  # fmt: skip
        return rows[0][0]

    async def override(self, state_id: uuid.UUID, fact_id: uuid.UUID) -> None:
        await self.sql(
            "INSERT INTO civ.emotional_overrides (user_id, emotional_state_id, core_fact_id) VALUES (:u, :s, :f)",
            s=state_id, f=fact_id,
        )  # fmt: skip


async def make_actor(client, owner_engine, *, tz: str = "Europe/Copenhagen", name: str | None = None) -> Actor:
    telegram_id = random.randrange(10**9, 10**12)
    user_id = await new_user(owner_engine, tz=tz, telegram_user_id=telegram_id)
    actor = Actor(client, owner_engine, user_id, telegram_id)
    if name is not None:
        await actor.fact("name", value=name, priority=90)
    return actor


@pytest_asyncio.fixture
async def alice(client, owner_engine) -> Actor:
    return await make_actor(client, owner_engine, name="Алиса")


@pytest_asyncio.fixture
async def bob(client, owner_engine) -> Actor:
    return await make_actor(client, owner_engine, tz="Asia/Tokyo", name="Боб")

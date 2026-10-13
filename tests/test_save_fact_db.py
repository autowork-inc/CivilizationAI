"""v2.0, instant memory (migration 0010): what the DATABASE says a fact written from a chat may be. The bot's code is the first fence (see
``test_save_fact_bot.py``); these tests prove the last one: even a bug in the code - or a hand-written statement under the application role - cannot
put a high-priority fact, a principle, a fact without a source message, or a fact "said" by the assistant into Core. Real PostgreSQL, RLS on."""

from __future__ import annotations

import random
import uuid
from typing import Any

from tests.helpers import new_user, owner_exec, pg_error, tenant_exec

INSERT_INLINE = (
    "INSERT INTO civ.core_facts (user_id, category, key, value_text, priority, source, source_message_id) "
    "VALUES (:u, CAST(:c AS civ.fact_category), :k, 'v', :p, CAST('chat_inline' AS civ.fact_source), :m) RETURNING id"
)


async def message(owner_engine, user_id: uuid.UUID, role: str = "user") -> uuid.UUID:
    rows = await owner_exec(
        owner_engine,
        "INSERT INTO civ.messages (user_id, role, content, telegram_message_id) "
        "VALUES (:u, CAST(:r AS civ.message_role), 'text', :t) RETURNING id",
        u=user_id,
        r=role,
        t=random.randrange(10**6, 10**9),
    )
    return rows[0][0]


async def inline(db, user_id: uuid.UUID, message_id: Any, *, category: str = "preference", key: str = "habit.run", priority: int = 50):
    return await tenant_exec(db, user_id, INSERT_INLINE, u=user_id, c=category, k=key, p=priority, m=message_id)


async def test_the_source_value_and_the_provenance_column_exist(owner_engine):
    [(values,)] = await owner_exec(owner_engine, "SELECT enum_range(NULL::civ.fact_source)::text")
    assert "chat_inline" in values
    [(column,)] = await owner_exec(
        owner_engine,
        "SELECT data_type FROM information_schema.columns WHERE table_schema = 'civ' AND table_name = 'core_facts' AND column_name = 'source_message_id'",
    )
    assert column == "uuid"
    constraints = {
        r[0]
        for r in await owner_exec(
            owner_engine,
            "SELECT conname FROM pg_constraint WHERE connamespace = 'civ'::regnamespace "
            "AND conname IN ('fk_core_facts_source_message', 'ck_core_facts_inline', 'uq_messages_user_id_id')",
        )
    }
    assert constraints == {"fk_core_facts_source_message", "ck_core_facts_inline", "uq_messages_user_id_id"}


async def test_a_fact_from_the_users_own_message_is_accepted_at_the_ceiling(db, owner_engine):
    user = await new_user(owner_engine)
    [(fact_id,)] = await inline(db, user, await message(owner_engine, user), priority=60)
    [(source, priority, msg)] = await owner_exec(
        owner_engine, "SELECT source::text, priority, source_message_id FROM civ.core_facts WHERE id = :i", i=fact_id
    )
    assert (source, priority) == ("chat_inline", 60) and msg is not None


async def test_the_priority_ceiling_is_60_like_the_emotions_the_model_infers(db, owner_engine):
    user = await new_user(owner_engine)
    with pg_error("23514", "ck_core_facts_inline"):
        await inline(db, user, await message(owner_engine, user), priority=61)


async def test_a_chat_fact_is_never_a_principle(db, owner_engine):
    user = await new_user(owner_engine)
    with pg_error("23514", "ck_core_facts_inline"):
        await inline(db, user, await message(owner_engine, user), category="principle", key="principle.x")


async def test_a_chat_fact_must_name_the_message_it_came_from(db, owner_engine):
    user = await new_user(owner_engine)
    with pg_error("23514", "USER message"):  # the BEFORE INSERT guard answers first; the CHECK (message_id IS NOT NULL) is the second fence
        await inline(db, user, None)


async def test_the_message_must_be_the_users_not_the_assistants(db, owner_engine):
    user = await new_user(owner_engine)
    with pg_error("23514", "USER message"):
        await inline(db, user, await message(owner_engine, user, role="assistant"))


async def test_the_message_must_belong_to_the_same_tenant_and_exist(db, owner_engine):
    mine, other = await new_user(owner_engine), await new_user(owner_engine)
    foreign = await message(owner_engine, other)
    with pg_error("23514", "USER message"):  # the guard fires before the foreign key: somebody else's message is not "mine"
        await inline(db, mine, foreign)
    with pg_error("23514", "USER message"):
        await inline(db, mine, uuid.uuid4())


async def test_the_other_sources_still_need_no_message(db, owner_engine):
    user = await new_user(owner_engine)
    for source in ("onboarding", "user_manual"):
        await tenant_exec(
            db,
            user,
            "INSERT INTO civ.core_facts (user_id, category, key, value_text, priority, source) "
            f"VALUES (:u, 'identity', 'k_{source}', 'v', 90, CAST('{source}' AS civ.fact_source))",
            u=user,
        )


async def test_a_chat_fact_is_an_immutable_version_like_every_other(db, owner_engine):
    user = await new_user(owner_engine)
    [(fact_id,)] = await inline(db, user, await message(owner_engine, user))
    with pg_error("23000", "versioned"):
        await tenant_exec(db, user, "UPDATE civ.core_facts SET value_text = 'changed' WHERE id = :i", i=fact_id)
    await tenant_exec(db, user, "UPDATE civ.core_facts SET valid_to = now() + interval '1 second' WHERE id = :i", i=fact_id)  # closing is allowed


async def test_another_tenant_cannot_see_a_chat_fact(db, owner_engine):
    mine, other = await new_user(owner_engine), await new_user(owner_engine)
    await inline(db, mine, await message(owner_engine, mine))
    assert await tenant_exec(db, other, "SELECT id FROM civ.core_facts WHERE source::text = 'chat_inline'") == []
    assert len(await tenant_exec(db, mine, "SELECT id FROM civ.core_facts WHERE source::text = 'chat_inline'")) == 1

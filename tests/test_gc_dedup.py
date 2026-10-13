"""Module 3: the GC does not propose the same thing again every night (pending or just refused), but is not muted for good."""

from __future__ import annotations

import uuid

from ai_civ.memory import gc
from tests.fakes import FakeLLM
from tests.helpers import new_message, new_user, owner_exec
from tests.test_gc import at, build_ctx, claim, new_numeric_fact, table


def coffee(value: str = "black coffee") -> dict:
    return {
        "summary": "s", "importance": 3, "emotions": [],
        "facts": [{"category": "preference", "key": "preference.coffee", "value_text": value,
                   "evidence": "I like black coffee", "source_refs": ["m1"], "confidence": 0.9}],
    }  # fmt: skip


def joy() -> dict:
    return {
        "summary": "s", "importance": 3, "facts": [],
        "emotions": [{"label": "Over the moon", "intensity": 10, "priority": 90, "expires_in_hours": 72,
                      "evidence": "I am over the moon", "source_refs": ["m1"], "confidence": 0.9}],
    }  # fmt: skip


async def night(db, owner_engine, user, reply: dict, n: int) -> None:
    """One more nightly run over the same day's messages (a distinct outbox key, as a different night would have)."""
    ctx = build_ctx(db, FakeLLM(lambda _: reply))
    await gc.compress_day(ctx, await claim(db, owner_engine, user, key=f"compress_day:none:night-{n}-{uuid.uuid4().hex[:6]}"))


async def proposals(owner_engine, user) -> list[tuple]:
    return await table(
        owner_engine, user,
        "SELECT key, value_text, value_num, emotion_label, status::text FROM civ.pending_facts WHERE user_id = :u ORDER BY created_at",
    )  # fmt: skip


async def test_a_proposal_that_is_still_pending_is_not_proposed_again_the_next_night(db, owner_engine):
    user = await new_user(owner_engine)
    await new_message(owner_engine, user, "I like black coffee", created_at=at(1))
    await night(db, owner_engine, user, coffee(), 1)
    await night(db, owner_engine, user, coffee("Black  Coffee"), 2)  # same value, different spacing / case
    assert await proposals(owner_engine, user) == [("preference.coffee", "black coffee", None, None, "pending")]


async def test_a_numeric_proposal_is_compared_by_value_not_by_text(db, owner_engine):
    user = await new_user(owner_engine)
    await new_message(owner_engine, user, "I weigh 82 kg", created_at=at(1))
    await new_numeric_fact(owner_engine, user, "weight", 80, "kg")

    def weight(num) -> dict:
        return {"summary": "s", "importance": 3, "emotions": [], "facts": [
            {"category": "body", "key": "weight", "value_num": num, "value_unit": "kg", "evidence": "I weigh 82 kg",
             "source_refs": ["m1"], "confidence": 0.9}]}  # fmt: skip

    await night(db, owner_engine, user, weight(82), 1)
    await night(db, owner_engine, user, weight(82.0), 2)
    rows = await proposals(owner_engine, user)
    assert len(rows) == 1 and rows[0][0] == "weight" and rows[0][4] == "pending"


async def test_a_refused_proposal_is_not_offered_again_within_the_cooldown_but_a_new_value_is(db, owner_engine):
    user = await new_user(owner_engine)
    await new_message(owner_engine, user, "I like black coffee", created_at=at(1))
    await night(db, owner_engine, user, coffee(), 1)
    await owner_exec(owner_engine, "UPDATE civ.pending_facts SET status = 'rejected', decided_at = now() WHERE user_id = :u", u=user)

    await night(db, owner_engine, user, coffee(), 2)  # the user said "delete" a moment ago: do not ask again
    assert [r[4] for r in await proposals(owner_engine, user)] == ["rejected"]

    await night(db, owner_engine, user, coffee("black coffee with milk"), 3)  # a different claim is a different question
    assert [(r[1], r[4]) for r in await proposals(owner_engine, user)] == [
        ("black coffee", "rejected"), ("black coffee with milk", "pending"),
    ]  # fmt: skip


async def test_after_the_cooldown_a_refused_proposal_may_come_back(db, owner_engine):
    user = await new_user(owner_engine)
    await new_message(owner_engine, user, "I like black coffee", created_at=at(1))
    await owner_exec(  # a decided row cannot be edited (trigger), so the old refusal is inserted with its date
        owner_engine,
        "INSERT INTO civ.pending_facts (user_id, subject, change, category, key, value_text, evidence, source_message_ids, "
        "status, decided_at, idempotency_key) VALUES (:u, 'core_fact', 'create', 'preference', 'preference.coffee', "
        "'black coffee', 'said so', ARRAY[gen_random_uuid()], 'rejected', now() - interval '40 days', 'old-refusal')",
        u=user,
    )
    await night(db, owner_engine, user, coffee(), 1)
    assert [r[4] for r in await proposals(owner_engine, user)] == ["rejected", "pending"]  # 40 days > the 30-day cooldown


async def test_an_emotion_waiting_for_the_users_decision_is_not_proposed_twice(db, owner_engine):
    user = await new_user(owner_engine)
    await new_message(owner_engine, user, "I am over the moon", created_at=at(1))
    await night(db, owner_engine, user, joy(), 1)
    await night(db, owner_engine, user, joy(), 2)
    rows = await proposals(owner_engine, user)
    assert len(rows) == 1 and rows[0][3] == "Over the moon"


async def test_dedup_is_per_tenant(db, owner_engine):
    a, b = await new_user(owner_engine), await new_user(owner_engine)
    for user in (a, b):
        await new_message(owner_engine, user, "I like black coffee", created_at=at(1))
    await night(db, owner_engine, a, coffee(), 1)
    await night(db, owner_engine, b, coffee(), 1)  # a's pending proposal must not suppress b's
    assert len(await proposals(owner_engine, a)) == 1 and len(await proposals(owner_engine, b)) == 1

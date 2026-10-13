"""Prompt assembly against the REAL database: Core Profile (Tier 1), Effective Principles (view, strict-priority rule),
Context Buffer (Tier 3: topic only, 72 h window), tenant isolation."""

from __future__ import annotations

import json

import pytest

from ai_civ.bot.prompt import build_prompt, load_history, load_profile
from ai_civ.config import Settings
from tests.helpers import new_fact, new_message, new_persona, new_user, owner_exec

SETTINGS = Settings(_env_file=None)


async def build(db, user_id, persona_id, **kw):
    return await build_prompt(
        db, user_id=user_id, timezone="UTC", persona_id=persona_id, persona_prompt="PERSONA", settings=SETTINGS, **kw
    )


async def new_state(owner_engine, user_id, label, priority, *, hours=3, override: object | None = None):
    sid = (
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, expires_at) "
            "VALUES (:u, :l, 7, :p, 'user_declared', now() + make_interval(hours => :h)) RETURNING id",
            u=user_id, l=label, p=priority, h=hours,
        )  # fmt: skip
    )[0][0]
    if override is not None:
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.emotional_overrides (user_id, emotional_state_id, core_fact_id) VALUES (:u, :s, :f)",
            u=user_id, s=sid, f=override,
        )  # fmt: skip
    return sid


async def test_core_profile_lists_current_hard_facts_with_numbers_from_the_database(owner_engine, db):
    user = await new_user(owner_engine)
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.core_facts (user_id, category, key, value_num, value_unit, source, priority) "
        "VALUES (:u, 'body', 'weight', 80.50, 'kg', 'user_manual', 70)",
        u=user,
    )
    await new_fact(owner_engine, user, "goal.marathon", category="goal", value="run a marathon", priority=40)
    old = await new_fact(owner_engine, user, "city", category="identity", value="Copenhagen", priority=99)
    await owner_exec(owner_engine, "UPDATE civ.core_facts SET valid_to = now() WHERE id = :i", i=old)  # superseded version
    await new_fact(owner_engine, user, "principle.sleep", category="principle", value="sleep 8h", priority=90)

    async with db.tenant_session(user) as session:
        profile = await load_profile(session, None, max_facts=10)
    assert [f.key for f in profile.facts] == ["weight", "goal.marathon"]  # by priority; closed version and principle excluded
    assert profile.facts[0].value == "80.5 kg"  # Decimal('80.50') normalised, still a number from the database


async def test_persona_scope_global_facts_plus_own_never_another_personas(owner_engine, db):
    user = await new_user(owner_engine)
    food, job = await new_persona(owner_engine, user, "foodguide"), await new_persona(owner_engine, user, "job_hunt")
    await new_fact(owner_engine, user, "diet", category="preference", value="vegetarian", persona_id=food)
    await new_fact(owner_engine, user, "cv_language", category="preference", value="English", persona_id=job)
    await new_fact(owner_engine, user, "name", category="identity", value="Anna")
    async with db.tenant_session(user) as session:
        in_food = {f.key for f in (await load_profile(session, food, max_facts=10)).facts}
        in_general = {f.key for f in (await load_profile(session, None, max_facts=10)).facts}
    assert in_food == {"diet", "name"} and in_general == {"name"}


async def test_the_facts_cap_is_applied_and_reported(owner_engine, db):
    user = await new_user(owner_engine)
    for i in range(5):
        await new_fact(owner_engine, user, f"fact_{i}", category="other", priority=10 + i)
    async with db.tenant_session(user) as session:
        profile = await load_profile(session, None, max_facts=3)
    assert profile.facts_truncated and [f.key for f in profile.facts] == ["fact_4", "fact_3", "fact_2"]


async def test_a_stronger_active_state_overrides_a_principle_in_the_prompt_the_view_decides_not_the_llm(owner_engine, db):
    user = await new_user(owner_engine)
    sweets = await new_fact(owner_engine, user, "principle.no_sweets", category="principle", value="no sweets", priority=50)
    sleep = await new_fact(owner_engine, user, "principle.sleep", category="principle", value="sleep 8h", priority=90)
    await new_state(owner_engine, user, "in love", 100, override=sweets)  # 100 > 50 -> wins
    await new_state(owner_engine, user, "tired", 80, override=sleep)  # 80 < 90 -> must NOT win
    await new_message(owner_engine, user, "hi")

    prompt = await build(db, user, None)
    lines = {line.split(":")[0]: line for line in prompt.system.splitlines() if line.startswith("- principle.")}
    assert "TEMPORARILY OVERRIDDEN" in lines["- principle.no_sweets [priority 50]"] and '"in love"' in lines["- principle.no_sweets [priority 50]"]
    assert "OVERRIDDEN" not in lines["- principle.sleep [priority 90]"]


async def test_an_expired_state_and_an_equal_priority_state_do_not_override(owner_engine, db):
    user = await new_user(owner_engine)
    p = await new_fact(owner_engine, user, "principle.a", category="principle", value="a", priority=60)
    q = await new_fact(owner_engine, user, "principle.b", category="principle", value="b", priority=60)
    await new_state(owner_engine, user, "equal", 60, override=p)  # not STRICTLY greater
    sid = await new_state(owner_engine, user, "was strong", 100, override=q)
    await owner_exec(
        owner_engine,
        "UPDATE civ.emotional_states SET started_at = now() - interval '2 days', expires_at = now() - interval '1 day' WHERE id = :i",
        i=sid,
    )
    await new_message(owner_engine, user, "hi")
    system = (await build(db, user, None)).system
    principle_lines = [line for line in system.splitlines() if line.startswith("- principle.")]
    assert len(principle_lines) == 2 and not any("OVERRIDDEN" in line for line in principle_lines)


async def test_context_buffer_is_this_topic_only_inside_72_hours_oldest_first(owner_engine, db):
    user = await new_user(owner_engine)
    food, job = await new_persona(owner_engine, user, "foodguide"), await new_persona(owner_engine, user, "job_hunt")
    await new_message(owner_engine, user, "too old", persona_id=food, created_at=await _ago(owner_engine, hours=80))
    await new_message(owner_engine, user, "borderline ok", persona_id=food, created_at=await _ago(owner_engine, hours=70))
    await new_message(owner_engine, user, "other topic", persona_id=job)
    await new_message(owner_engine, user, "general chatter")
    await new_message(owner_engine, user, "answer", role="assistant", persona_id=food, created_at=await _ago(owner_engine, hours=1))
    await new_message(owner_engine, user, "latest question", persona_id=food)

    prompt = await build(db, user, food)
    assert [(t.role, t.text) for t in prompt.turns] == [
        ("user", "borderline ok"),
        ("model", "answer"),
        ("user", "latest question"),
    ]
    general = await build(db, user, None)
    assert [t.text for t in general.turns] == ["general chatter"]


async def test_the_newest_messages_win_when_the_window_holds_more_than_the_limit(owner_engine, db):
    user = await new_user(owner_engine)
    for i in range(5):
        await new_message(owner_engine, user, f"m{i}", created_at=await _ago(owner_engine, hours=5 - i))
    async with db.tenant_session(user) as session:
        history = await load_history(session, None, hours=72, limit=3)
    assert [m.content for m in history] == ["m2", "m3", "m4"]


async def test_a_reply_needs_a_user_message_to_answer(owner_engine, db):
    user = await new_user(owner_engine)
    with pytest.raises(ValueError, match="does not end with a user message"):
        await build(db, user, None)  # empty buffer
    await new_message(owner_engine, user, "I asked", role="user")
    await new_message(owner_engine, user, "I answered", role="assistant")
    with pytest.raises(ValueError):
        await build(db, user, None)  # ends with the assistant


async def test_proactive_prompt_ends_with_a_system_trigger_that_is_not_in_the_database(owner_engine, db):
    user = await new_user(owner_engine)
    food = await new_persona(owner_engine, user, "foodguide")
    prompt = await build(db, user, food, proactive_instruction="Ask whether the user has eaten.")
    assert "## Proactive mode" in prompt.system
    assert len(prompt.turns) == 1 and prompt.turns[0].role == "user"
    assert "[system trigger, not written by the user] Ask whether the user has eaten." in prompt.turns[0].text
    rows = await owner_exec(owner_engine, "SELECT count(*) FROM civ.messages WHERE user_id = :u", u=user)
    assert rows[0][0] == 0  # building a prompt never writes


async def test_another_tenants_data_never_reaches_the_prompt(owner_engine, db):
    mine, theirs = await new_user(owner_engine), await new_user(owner_engine)
    await new_fact(owner_engine, theirs, "secret", category="other", value="THEIR-SECRET")
    await new_fact(owner_engine, theirs, "principle.x", category="principle", value="THEIR-PRINCIPLE")
    await new_message(owner_engine, theirs, "THEIR-MESSAGE")
    await new_message(owner_engine, mine, "mine")
    prompt = await build(db, mine, None)
    blob = prompt.system + json.dumps([t.text for t in prompt.turns])
    assert "THEIR" not in blob


async def _ago(owner_engine, *, hours: int):
    return (await owner_exec(owner_engine, "SELECT now() - make_interval(hours => :h)", h=hours))[0][0]

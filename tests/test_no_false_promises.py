"""Step 0 of "events from conversation": the model must never claim an action it has no function for ("I wrote to him", "I will let you know").
A prompt is only a request, so these tests pin down that the request is actually sent in EVERY place the model is called: with contacts (a
function-calling turn), without contacts (a plain turn) and on the second turn that phrases the Diplomat's results. The structural half of the
guarantee (confirmations are produced by code, after the commit) arrives with the functions themselves."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from ai_civ.bot import diplomat
from ai_civ.bot.diplomat import RosterEntry
from ai_civ.bot.prompt import RULES
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.test_diplomat_bot import befriend, call, two_days

BAN = "NEVER say or imply that you did something you have no function for"


def test_the_rules_forbid_claiming_an_action_without_a_function():
    rules = " ".join(RULES.split())  # the prompt wraps its lines
    assert BAN in rules
    for claim in ("написал", "отправил", "передал", "позвал", "записал", "напомню", "дам знать"):
        assert f'"{claim}"' in rules  # the exact verbs the model used, so the ban is concrete
    assert "unless a function result in this very conversation says it was done" in rules
    assert "Never promise to report back later" in rules


def test_the_contacts_section_says_that_asking_is_read_only_and_inviting_has_one_door():
    text = " ".join(diplomat.render_roster([RosterEntry("c1", uuid.uuid4(), "Саша")]).split())
    assert "ask_contact only READS" in text
    assert "It sends NOTHING to the contact" in text
    assert "The ONLY way to invite a contact is the function propose_event" in text
    assert "nothing enters any calendar until they accept" in text
    assert "the SYSTEM itself tells the user what was sent" in text
    assert "Apart from these two functions you cannot write to a contact" in text
    assert "Never say you wrote, sent, passed on or invited unless propose_event ran" in text


def test_an_empty_calendar_is_reported_as_a_calendar_fact_not_as_a_free_person():
    window = {"from": datetime(2026, 10, 5, 8, tzinfo=UTC).isoformat(), "to": datetime(2026, 10, 6, 8, tzinfo=UTC).isoformat(), "horizon_days": 7}
    text = diplomat.render_free_busy({"decision": "allowed", "window": window, "events": []}, "UTC")
    assert "no busy time" in text
    assert "ONLY what the contact has put into their calendar" in text
    assert "not that the person is surely free" in text and "по календарю свободен" in text


async def test_a_user_with_contacts_gets_the_ban_and_the_read_only_statement_in_the_function_turn(bot_env):
    env = bot_env
    await befriend(env)
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="Привет!")
    await env.feed("Позови Сашу в шахматы")
    [offered] = env.chat.tool_calls
    assert BAN in offered["system"] and "ask_contact only READS" in offered["system"]


async def test_a_user_without_contacts_gets_the_ban_too(bot_env):
    env = bot_env  # quick events off: the plain reply path
    await env.feed("Напиши Саше, что я зайду вечером")
    assert env.chat.tool_calls == []  # there is nobody to ask, so no function is offered...
    [plain] = env.chat.calls
    assert BAN in plain["system"]  # ...and that is exactly when a model is most tempted to pretend


async def test_with_quick_events_a_user_without_contacts_is_offered_only_the_calendar_function_and_the_ban(quick_env):
    env = quick_env
    await env.feed("Напиши Саше, что я зайду вечером")
    [offered] = env.chat.tool_calls
    assert [t.name for t in offered["tools"]] == ["create_event"]  # nobody to ask or invite: still no function that "writes to Саша"
    assert BAN in offered["system"]
    assert env.chat.calls == []


async def test_the_turn_that_phrases_the_diplomats_answer_carries_the_ban_and_the_calendar_caveat(bot_env):
    env = bot_env
    await befriend(env)
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="", calls=(call("c1", **two_days()),))
    env.chat.responder = lambda system, turns: "Саша свободен."
    await env.feed("Когда Саша свободен?")
    [informed] = env.chat.calls
    assert BAN in informed["system"]
    assert "ONLY what the contact has put into their calendar" in informed["system"]


def test_an_unknown_function_name_is_ignored_not_treated_as_done():
    """A model that invents a function (say, send_message) must not make the bot believe anything happened."""
    import asyncio

    from ai_civ.config import Settings

    class NoDb:  # the Diplomat never touches the database for a function it does not know
        pass

    outcomes = asyncio.run(
        diplomat.Diplomat(NoDb(), Settings(_env_file=None)).run(  # type: ignore[arg-type]
            uuid.uuid4(), "UTC", [RosterEntry("c1", uuid.uuid4(), "Саша")], [ToolCall("send_message", {"contact": "c1", "text": "hi"})]
        )
    )
    assert outcomes == []

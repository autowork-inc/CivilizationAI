"""Module 5, step 4 in the bot: Function Calling for the Diplomat (``bot/diplomat.py`` + ``Orchestrator._answer``), the pure rendering of what the
database returned, and the two places where the Diplomat touches the rest of the system (the nightly GC, the review card).
Real PostgreSQL, real aiogram Dispatcher on a fake Telegram wire, fake Gemini whose "model" decides in the test whether to call the function."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ai_civ.bot import diplomat, texts
from ai_civ.bot.diplomat import Outcome, RosterEntry
from ai_civ.config import Settings
from ai_civ.db.enums import AclScope, SharedMood
from ai_civ.memory import gc
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.fakes import FakeLLM
from tests.helpers import new_contact, new_message, new_user, owner_exec
from tests.test_diplomat_db import confirmed_state, grant, new_event
from tests.test_gc import PAYLOAD, at, build_ctx, claim, table

CPH = ZoneInfo("Europe/Copenhagen")
FREE = "calendar_free_busy"
MOOD = "emotional_state_label"


def call(contact: str = "c1", info: str = FREE, **kw) -> ToolCall:
    return ToolCall("ask_contact", {"contact": contact, "info": info, **kw})


def two_days() -> dict[str, str]:
    today = datetime.now(CPH).date()
    return {"date_from": today.isoformat(), "date_to": (today + timedelta(days=1)).isoformat()}


def roster() -> list[RosterEntry]:
    return [RosterEntry("c1", uuid.uuid4(), "Саша"), RosterEntry("c2", uuid.uuid4(), "Оля")]


async def befriend(env, alias: str = "Саша", *, granted: str | None = FREE) -> uuid.UUID:
    """A second tenant, connected both ways with the bot's user; the contact granted ``granted`` to the bot's user."""
    other = await new_user(env.owner_engine, tz="UTC")
    await new_contact(env.owner_engine, env.user_id, other, alias=alias)
    await new_contact(env.owner_engine, other, env.user_id, alias="Алиса")
    if granted is not None:
        await grant(env.owner_engine, other, env.user_id, granted)
    return other


def decide(env, *calls: ToolCall, then: str = "Вот что узнал."):
    """The fake model asks the function(s) once and, when asked again with the results, answers ``then``."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="", calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


async def audit(env, owner: uuid.UUID) -> list[tuple[str, str]]:
    rows = await owner_exec(
        env.owner_engine,
        "SELECT scope::text, decision FROM civ.diplomat_audit_log WHERE user_id = :o AND requester_user_id = :a ORDER BY id",
        o=owner, a=env.user_id,
    )  # fmt: skip
    return [(r[0], r[1]) for r in rows]


async def assistant_rows(env) -> list[tuple[str, bool]]:
    rows = await owner_exec(
        env.owner_engine,
        "SELECT content, third_party_data FROM civ.messages WHERE user_id = :u AND role = 'assistant' ORDER BY created_at, id",
        u=env.user_id,
    )
    return [(r[0], r[1]) for r in rows]


def last_text(env) -> str:
    return env.session.sent_to(None)[-1].text


# ================================================================== pure: the tool call is validated by code


def test_the_tool_offers_a_closed_set_of_information_and_requires_a_contact():
    schema = diplomat.TOOL.parameters
    assert set(schema["required"]) == {"contact", "info"}
    assert schema["properties"]["info"]["enum"] == ["calendar_free_busy", "emotional_state_label"]
    assert "enum" not in schema["properties"]["contact"]  # an enum would FORCE a wrong person onto "someone I do not know"


def test_a_contact_ref_must_come_from_the_list():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    r = roster()
    assert diplomat.parse_ask({"contact": "c2", "info": MOOD}, r, "UTC", now).entry == r[1]
    assert diplomat.parse_ask({"contact": " C1 ", "info": MOOD}, r, "UTC", now).entry == r[0]
    for bad in ("c9", "none", "", "Саша", "c1; DROP TABLE", None, 7, {"x": 1}):
        assert diplomat.parse_ask({"contact": bad, "info": MOOD}, r, "UTC", now).entry is None


def test_an_unknown_kind_of_information_is_not_asked_at_all():
    now = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    for bad in ("calendar_details", "profile_public", "persona_status", "everything", "", None):
        assert diplomat.parse_ask({"contact": "c1", "info": bad}, roster(), "UTC", now) is None


def test_the_period_is_parsed_by_code_defaulted_ordered_and_capped():
    now = datetime(2026, 10, 3, 23, 30, tzinfo=UTC)  # already October 4th in Copenhagen
    r = roster()
    ask = diplomat.parse_ask({"contact": "c1", "info": FREE}, r, "Europe/Copenhagen", now)
    assert ask.start == datetime(2026, 10, 4, tzinfo=CPH) and ask.end == datetime(2026, 10, 5, tzinfo=CPH)  # today, the user's today
    ask = diplomat.parse_ask({"contact": "c1", "info": FREE, "date_from": "2026-10-06", "date_to": "2026-10-08"}, r, "Europe/Copenhagen", now)
    assert (ask.start.date().isoformat(), ask.end.date().isoformat()) == ("2026-10-06", "2026-10-09")  # the last day is inclusive
    ask = diplomat.parse_ask({"contact": "c1", "info": FREE, "date_from": "2026-10-08", "date_to": "2026-10-06"}, r, "UTC", now)
    assert ask.end - ask.start == timedelta(days=1)  # an inverted period collapses to one day
    ask = diplomat.parse_ask({"contact": "c1", "info": FREE, "date_from": "2026-10-04", "date_to": "2027-10-04"}, r, "UTC", now)
    assert ask.end - ask.start == timedelta(days=diplomat.MAX_SPAN_DAYS)
    for junk in ("tomorrow", "2026-13-45", 20261004, None, ["2026-10-05"]):
        ask = diplomat.parse_ask({"contact": "c1", "info": FREE, "date_from": junk}, r, "UTC", now)
        assert ask.start == datetime(2026, 10, 3, tzinfo=UTC)  # unreadable -> today


# ================================================================== pure: what the database returned -> the prompt


def iso(moment: datetime) -> str:
    return moment.isoformat()


def window(start: datetime, end: datetime) -> dict:
    return {"from": iso(start), "to": iso(end), "horizon_days": 7}


def test_busy_time_is_merged_clipped_and_rendered_per_local_day():
    w0, w1 = datetime(2026, 10, 5, 8, 0, tzinfo=UTC), datetime(2026, 10, 7, 8, 0, tzinfo=UTC)  # a Monday 10:00 .. Wednesday 10:00 in Copenhagen
    payload = {
        "decision": "allowed",
        "window": window(w0, w1),
        "events": [
            {"kind": "single", "starts_at": iso(datetime(2026, 10, 5, 9, 0, tzinfo=UTC)), "ends_at": iso(datetime(2026, 10, 5, 10, 0, tzinfo=UTC))},
            {"kind": "single", "starts_at": iso(datetime(2026, 10, 5, 9, 30, tzinfo=UTC)), "ends_at": iso(datetime(2026, 10, 5, 11, 0, tzinfo=UTC))},  # overlaps -> merged
            {"kind": "single", "starts_at": iso(datetime(2026, 10, 5, 20, 0, tzinfo=UTC)), "ends_at": iso(datetime(2026, 10, 6, 5, 0, tzinfo=UTC))},  # crosses midnight
            {"kind": "single", "starts_at": iso(datetime(2026, 10, 1, 9, 0, tzinfo=UTC)), "ends_at": iso(datetime(2026, 10, 1, 10, 0, tzinfo=UTC))},  # outside
        ],
    }  # fmt: skip
    text = diplomat.render_free_busy(payload, "Europe/Copenhagen")
    assert "period checked: 2026-10-05 10:00 .. 2026-10-07 10:00 (Europe/Copenhagen)" in text
    assert "2026-10-05 (Monday): busy 11:00-13:00, 22:00-24:00; free otherwise" in text
    assert "2026-10-06 (Tuesday): busy 00:00-07:00; free otherwise" in text
    assert "2026-10-07 (Wednesday): no busy time" in text


def test_a_series_is_expanded_by_code_and_its_exceptions_are_applied():
    start = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)  # 09:00 in Copenhagen (summer time), every day for one hour
    payload = {
        "decision": "allowed",
        "window": window(datetime(2026, 10, 5, 0, 0, tzinfo=UTC), datetime(2026, 10, 9, 0, 0, tzinfo=UTC)),
        "events": [
            {"kind": "series", "ref": 1, "starts_at": iso(start), "ends_at": iso(start + timedelta(hours=1)), "timezone": "Europe/Copenhagen", "rule": "FREQ=DAILY"},
            # Oct 6: moved to 15:00 local (13:00 UTC); Oct 7: cancelled; Oct 8: made private (suppressed, no time)
            {"kind": "exception", "series_ref": 1, "original_start": iso(start + timedelta(days=1)), "suppress": False,
             "starts_at": iso(datetime(2026, 10, 6, 13, 0, tzinfo=UTC)), "ends_at": iso(datetime(2026, 10, 6, 14, 0, tzinfo=UTC))},
            {"kind": "exception", "series_ref": 1, "original_start": iso(start + timedelta(days=2)), "suppress": True, "starts_at": None, "ends_at": None},
            {"kind": "exception", "series_ref": 1, "original_start": iso(start + timedelta(days=3)), "suppress": True, "starts_at": None, "ends_at": None},
        ],
    }  # fmt: skip
    text = diplomat.render_free_busy(payload, "Europe/Copenhagen")
    assert "2026-10-05 (Monday): busy 09:00-10:00; free otherwise" in text
    assert "2026-10-06 (Tuesday): busy 15:00-16:00; free otherwise" in text  # the 09:00 occurrence is gone, the moved one is there
    assert "2026-10-07 (Wednesday): no busy time" in text
    assert "2026-10-08 (Thursday): no busy time" in text


def test_a_window_with_nothing_to_show_says_so_instead_of_claiming_free():
    t = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    text = diplomat.render_free_busy({"decision": "allowed", "window": window(t, t), "events": []}, "UTC")
    assert "nothing of this calendar can be shown" in text and "free" not in text.replace("nothing of this calendar can be shown", "")


@pytest.mark.parametrize(
    "events",
    [
        [{"kind": "mystery"}],  # a row kind we do not know
        [{"kind": "series", "ref": 1, "starts_at": "2026-10-05T07:00:00+00:00", "ends_at": "2026-10-05T08:00:00+00:00", "timezone": "Europe/Copenhagen", "rule": "FREQ=SECONDLY"}],
        [{"kind": "single", "starts_at": "yesterday", "ends_at": "2026-10-05T08:00:00+00:00"}],
        [{"kind": "single", "starts_at": "2026-10-05T07:00:00", "ends_at": "2026-10-05T08:00:00+00:00"}],  # no zone
        "not a list",
    ],
)
def test_an_answer_that_cannot_be_trusted_is_never_turned_into_free_time(events):
    payload = {"decision": "allowed", "window": window(datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 6, tzinfo=UTC)), "events": events}
    with pytest.raises(diplomat.UntrustworthyAnswer):
        diplomat.render_free_busy(payload, "UTC")


def test_every_word_of_the_closed_vocabulary_has_a_russian_noun_and_nothing_else_is_repeated():
    assert set(texts.MOOD_WORDS) == set(SharedMood)
    assert diplomat.render_mood({"mood": "tired"}) == "current mood: усталость (confirmed by the contact)"
    assert "no confirmed mood" in diplomat.render_mood({"mood": None})
    with pytest.raises(diplomat.UntrustworthyAnswer):
        diplomat.render_mood({"mood": "plotting revenge"})  # a word outside the vocabulary is not passed on


def test_refusals_are_fixed_texts_and_a_single_answer_disables_the_shortcut():
    un, nc, er = Outcome("unavailable", "Саша"), Outcome("no_contact"), Outcome("error", "Оля")
    assert diplomat.refusal_text([un]) == texts.DIPLOMAT_UNAVAILABLE
    assert diplomat.refusal_text([nc]) == texts.DIPLOMAT_NO_CONTACT
    assert diplomat.refusal_text([er]) == texts.DIPLOMAT_TRY_LATER
    assert diplomat.refusal_text([un, Outcome("unavailable", "Оля")]) == f"• Саша: {texts.DIPLOMAT_UNAVAILABLE}\n• Оля: {texts.DIPLOMAT_UNAVAILABLE}"
    assert diplomat.refusal_text([un, Outcome("answer", "Оля", AclScope.emotional_state_label, "current mood: x")]) is None
    assert diplomat.refusal_text([]) is None


def test_the_texts_never_explain_why_access_is_closed():
    sentence = texts.DIPLOMAT_UNAVAILABLE.lower()
    for reason in ("заблок", "не разреш", "отозв", "истек", "удал", "лимит", "доступ закрыт"):
        assert reason not in sentence


def test_the_results_section_forbids_guessing_and_dictates_the_refusal_sentence():
    section = diplomat.render_results(
        [Outcome("answer", "Саша", AclScope.calendar_free_busy, "period checked: x\n  d: no busy time"), Outcome("unavailable", "Оля"), Outcome("no_contact")]
    )
    assert "ONLY the lines below" in section and "never explain why" in section
    assert f'- Оля: say exactly "{texts.DIPLOMAT_UNAVAILABLE}"' in section
    assert f'- (not in the user\'s contacts): say exactly "{texts.DIPLOMAT_NO_CONTACT}"' in section


# ================================================================== the Orchestrator: Function Calling end to end


async def test_a_user_without_contacts_never_sees_the_function_and_pays_nothing_extra(bot_env):
    env = bot_env
    await env.feed("Что у меня на ужин?")
    assert env.chat.tool_calls == [] and len(env.chat.calls) == 1  # the plain generate_text path
    assert "## Contacts" not in env.chat.calls[0]["system"]


async def test_the_model_sees_only_aliases_and_refs_and_may_answer_without_calling(bot_env):
    env = bot_env
    other = await befriend(env)
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="Как дела? Всё хорошо.")
    await env.feed("Привет, как жизнь?")
    [offered] = env.chat.tool_calls
    assert offered["tools"] == [diplomat.TOOL, diplomat.PROPOSE_TOOL] == diplomat.TOOLS
    assert "- c1: Саша" in offered["system"] and str(other) not in offered["system"]
    assert last_text(env) == "Как дела? Всё хорошо." and env.chat.calls == []
    assert await audit(env, other) == []  # nothing was asked, nothing is in anybody's log
    assert await assistant_rows(env) == [("Как дела? Всё хорошо.", False)]


async def test_a_granted_ask_is_answered_audited_and_the_reply_is_kept_out_of_long_term_memory(bot_env):
    env = bot_env
    other = await befriend(env)
    await new_event(env.owner_engine, other, starts=datetime.now(UTC) + timedelta(hours=2), title="Therapy with Dr Moreau", visibility="busy_only")
    decide(env, call("c1", **two_days()), then="Саша занят в ближайшие часы.")

    await env.feed("Когда Саша свободен?")

    assert last_text(env) == "Саша занят в ближайшие часы."
    [informed] = env.chat.calls
    assert "## Contact data" in informed["system"] and "- Саша, calendar: period checked:" in informed["system"]
    assert "busy " in informed["system"] and "Therapy" not in informed["system"] and "Moreau" not in informed["system"]
    assert "## Contacts" not in informed["system"]  # the second asking has no function to call again
    assert await audit(env, other) == [(FREE, "allowed")]
    assert await assistant_rows(env) == [("Саша занят в ближайшие часы.", True)]  # flagged: the nightly GC will skip it


async def test_a_mood_ask_is_answered_from_a_confirmed_state_only(bot_env):
    env = bot_env
    other = await befriend(env, granted=MOOD)
    await confirmed_state(env.owner_engine, other, "overwhelmed", priority=85)
    decide(env, call("c1", MOOD), then="У Саши сейчас перегруз.")
    await env.feed("Как там Саша?")
    assert "current mood: перегруз (confirmed by the contact)" in env.chat.calls[0]["system"]
    assert "private feeling" not in env.chat.calls[0]["system"]  # the free-text label never leaves the owner's tenant
    assert await audit(env, other) == [(MOOD, "allowed")]


async def test_no_grant_answers_with_the_fixed_sentence_and_the_model_is_not_asked_again(bot_env):
    env = bot_env
    other = await befriend(env, granted=None)
    decide(env, call("c1", **two_days()), then="(must never be sent)")
    await env.feed("Когда Саша свободен?")
    assert last_text(env) == texts.DIPLOMAT_UNAVAILABLE
    assert env.chat.calls == []  # no second model call: nothing a model could embroider
    assert await audit(env, other) == [(FREE, "denied")]  # the owner sees that somebody asked
    assert await assistant_rows(env) == [(texts.DIPLOMAT_UNAVAILABLE, False)]


@pytest.mark.parametrize(
    "breakage",
    [
        "UPDATE civ.access_grants SET revoked_at = now() WHERE user_id = :o",
        "UPDATE civ.contacts SET status = 'blocked' WHERE user_id = :o",
        "DELETE FROM civ.contacts WHERE user_id = :o",
        "UPDATE civ.users SET group_state = 'frozen' WHERE id = :o",
    ],
)
async def test_whatever_the_reason_the_user_reads_the_same_sentence(bot_env, breakage):
    env = bot_env
    other = await befriend(env)
    await owner_exec(env.owner_engine, breakage, o=other)
    decide(env, call("c1", **two_days()))
    await env.feed("Когда Саша свободен?")
    assert last_text(env) == texts.DIPLOMAT_UNAVAILABLE


async def test_a_person_who_is_not_in_the_address_book_is_told_apart_from_a_closed_door(bot_env):
    env = bot_env
    await befriend(env)
    decide(env, call("none"))
    await env.feed("Когда Вася свободен?")
    assert last_text(env) == texts.DIPLOMAT_NO_CONTACT and env.chat.calls == []
    decide(env, call("c7"))  # a ref the model made up
    await env.feed("А Петя?")
    assert last_text(env) == texts.DIPLOMAT_NO_CONTACT


async def test_asking_about_something_that_can_not_be_asked_is_a_closed_door_without_touching_the_database(bot_env):
    env = bot_env
    other = await befriend(env)
    decide(env, call("c1", "profile_public"))
    await env.feed("Покажи профиль Саши")
    assert last_text(env) == texts.DIPLOMAT_UNAVAILABLE and await audit(env, other) == []


async def test_one_answer_among_refusals_is_phrased_by_the_model_with_the_refusals_dictated(bot_env):
    env = bot_env
    granted, closed = await befriend(env, "Саша"), await befriend(env, "Оля", granted=None)
    decide(env, call("c1", **two_days()), call("c2", **two_days()), then="Саша свободен, про Олю не знаю.")
    await env.feed("Когда свободны Саша и Оля?")
    system = env.chat.calls[0]["system"]
    assert "- Саша, calendar:" in system and f'- Оля: say exactly "{texts.DIPLOMAT_UNAVAILABLE}"' in system
    assert await audit(env, granted) == [(FREE, "allowed")] and await audit(env, closed) == [(FREE, "denied")]
    assert (await assistant_rows(env))[-1][1] is True


async def test_at_most_the_configured_number_of_asks_is_honoured_and_a_repeat_counts_once(bot_env):
    env = bot_env
    others = [await befriend(env, f"Друг{i}") for i in range(5)]
    decide(env, *[call(f"c{i}", **two_days()) for i in range(1, 6)], call("c1", **two_days()))
    await env.feed("Когда все свободны?")
    asked = [await audit(env, o) for o in others]
    assert sum(len(a) for a in asked) == 3  # diplomat_max_calls = 3; the model cannot make the bot fan out
    env.chat.tool_calls.clear()
    decide(env, call("c1", **two_days()), call("c1", **two_days()), call("C1", **two_days()))
    async def total() -> int:
        return sum([len(await audit(env, o)) for o in others])

    before = await total()
    await env.feed("А теперь только первый")
    assert await total() - before == 1


async def test_an_unknown_function_name_is_ignored_and_the_plain_answer_stands(bot_env):
    env = bot_env
    other = await befriend(env)
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="Обычный ответ.", calls=(ToolCall("read_everything", {"contact": "c1"}),))
    await env.feed("Расскажи всё про Сашу")
    assert last_text(env) == "Обычный ответ." and await audit(env, other) == []


async def test_a_failure_of_an_ask_is_try_later_not_the_system_notice_and_not_a_guess(bot_env, monkeypatch):
    env = bot_env
    await befriend(env)

    async def boom(*_args, **_kw):
        raise RuntimeError("database went away")

    monkeypatch.setattr(env.orchestrator.diplomat, "_call", boom)
    decide(env, call("c1", **two_days()))
    await env.feed("Когда Саша свободен?")
    assert last_text(env) == texts.DIPLOMAT_TRY_LATER and last_text(env) != texts.FAILURE
    assert env.chat.calls == []


async def test_a_failure_of_the_tool_model_is_the_usual_notice(bot_env):
    env = bot_env
    await befriend(env)
    env.chat.tool_responder = lambda system, turns, tools: RuntimeError("gemini is down")
    await env.feed("Привет")
    assert last_text(env) == texts.FAILURE


async def test_the_function_can_be_switched_off(owner_engine, db):
    from tests.bot_env import build_bot_env

    env = await build_bot_env(owner_engine, db, diplomat_enabled=False)
    try:
        await befriend(env)
        await env.feed("Привет")
        assert env.chat.tool_calls == [] and len(env.chat.calls) == 1
    finally:
        await env.bot.session.close()


async def test_a_pending_contact_is_not_offered_to_the_model(bot_env):
    env = bot_env
    other = await new_user(env.owner_engine)
    await new_contact(env.owner_engine, env.user_id, other, alias="Ещё не принял", status="pending")
    await env.feed("Привет")
    assert env.chat.tool_calls == []  # no accepted contact -> no function


async def test_a_contact_without_an_alias_still_gets_a_readable_name(bot_env):
    env = bot_env
    other = await new_user(env.owner_engine)
    await new_contact(env.owner_engine, env.user_id, other, alias=None)
    entries = await env.orchestrator.diplomat.roster(env.user_id)
    assert [(e.ref, e.alias) for e in entries] == [("c1", f"{texts.DEFAULT_CONTACT_NAME} 1")]


async def test_the_replayed_update_does_not_ask_twice(bot_env):
    env = bot_env
    other = await befriend(env)
    decide(env, call("c1", **two_days()), then="Готово.")
    update = env.update("Когда Саша свободен?")
    await env.feed_update(update)
    await env.feed_update(update)  # Telegram delivered it again; the answer already exists
    assert await audit(env, other) == [(FREE, "allowed")]


# ================================================================== the rest of the system


async def test_the_nightly_gc_never_reads_a_flagged_message(owner_engine, db):
    user = await new_user(owner_engine)
    await new_message(owner_engine, user, "I weigh 82 kg", created_at=at(1))
    flagged = await new_message(owner_engine, user, "Саша занят до 15:00 и ему грустно", role="assistant", created_at=at(2))
    await owner_exec(owner_engine, "UPDATE civ.messages SET third_party_data = true WHERE id = :i", i=flagged)
    llm = FakeLLM(lambda _: {"summary": "s", "importance": 3, "facts": [], "emotions": []})
    await gc.compress_day(build_ctx(db, llm), await claim(db, owner_engine, user, payload=PAYLOAD))
    prompt = llm.calls[0]["prompt"]
    assert "I weigh 82 kg" in prompt and "Саша" not in prompt
    [(source_ids,)] = await table(owner_engine, user, "SELECT source_message_ids FROM civ.episodic_memories WHERE user_id = :u")
    assert flagged not in source_ids


async def test_a_day_of_flagged_messages_only_is_done_without_a_model_call(owner_engine, db):
    user = await new_user(owner_engine)
    flagged = await new_message(owner_engine, user, "данные друга", role="assistant", created_at=at(1))
    await owner_exec(owner_engine, "UPDATE civ.messages SET third_party_data = true WHERE id = :i", i=flagged)
    llm = FakeLLM(lambda _: pytest.fail("no model call expected"))
    await gc.compress_day(build_ctx(db, llm), await claim(db, owner_engine, user, payload=PAYLOAD))
    assert await table(owner_engine, user, "SELECT 1 FROM civ.episodic_memories WHERE user_id = :u") == []


async def test_the_gc_carries_a_mood_word_of_the_closed_vocabulary_to_the_proposal_only(owner_engine, db):
    user = await new_user(owner_engine)
    await new_message(owner_engine, user, "I am completely overwhelmed by everything", created_at=at(1))
    emotion = {"label": "overwhelmed", "intensity": 9, "priority": 90, "expires_in_hours": 24, "mood": "overwhelmed",
               "evidence": "completely overwhelmed", "source_refs": ["m1"], "confidence": 0.9}  # fmt: skip
    llm = FakeLLM(lambda _: {"summary": "s", "importance": 5, "facts": [], "emotions": [emotion]})
    await gc.compress_day(build_ctx(db, llm), await claim(db, owner_engine, user, payload=PAYLOAD))
    assert await table(owner_engine, user, "SELECT subject::text, emotion_mood::text FROM civ.pending_facts WHERE user_id = :u") == [("emotional_state", "overwhelmed")]
    assert await table(owner_engine, user, "SELECT 1 FROM civ.emotional_states WHERE user_id = :u") == []  # nothing is shareable yet


def test_a_mood_word_outside_the_vocabulary_is_rejected_by_the_schema():
    base = {"label": "x", "intensity": 3, "priority": 10, "expires_in_hours": 1, "evidence": "e", "source_refs": ["m1"], "confidence": 0.5}
    assert gc.GcEmotion(**base).mood is None
    assert gc.GcEmotion(**base, mood="calm").mood is SharedMood.calm
    with pytest.raises(ValueError):
        gc.GcEmotion(**base, mood="murderous")


async def test_keeping_a_mood_proposal_shares_exactly_the_word_the_card_showed(bot_env):
    from tests.test_review import press, service

    env = bot_env
    rows = await owner_exec(
        env.owner_engine,
        "INSERT INTO civ.pending_facts (user_id, subject, change, emotion_label, emotion_intensity, emotion_priority, emotion_expires_at, "
        "emotion_mood, evidence, source_message_ids, idempotency_key) VALUES (:u, 'emotional_state', 'create', 'на пределе', 9, 90, "
        "now() + interval '2 days', 'overwhelmed', 'я на пределе', ARRAY[gen_random_uuid()], :i) RETURNING id",
        u=env.user_id, i=uuid.uuid4().hex,
    )  # fmt: skip
    pid = rows[0][0]
    await service(env).send_weekly(env.tenant)
    card = env.session.sent_to(None)[-1].text
    assert "Запомнить состояние (временно):\n• на пределе, интенсивность 9/10" in card
    assert texts.REVIEW_MOOD_LINE.format(word="перегруз") in card  # the user decides knowing what a contact could be told

    await press(env, pid, True)

    states = await owner_exec(env.owner_engine, "SELECT source::text, mood::text FROM civ.emotional_states WHERE user_id = :u", u=env.user_id)
    assert [tuple(s) for s in states] == [("user_confirmed", "overwhelmed")]


async def test_a_card_without_a_mood_says_nothing_about_contacts(bot_env):
    from tests.helpers import new_pending
    from tests.test_review import service

    env = bot_env
    await new_pending(env.owner_engine, env.user_id, subject="emotional_state")
    await service(env).send_weekly(env.tenant)
    assert "кому вы открыли настроение" not in env.session.sent_to(None)[-1].text


def test_settings_defaults_of_the_diplomat():
    s = Settings(_env_file=None)
    assert (s.diplomat_enabled, s.diplomat_max_calls, s.diplomat_max_contacts) == (True, 3, 40)
    assert json.dumps(diplomat.TOOL.parameters)  # the schema is plain JSON the SDK can send

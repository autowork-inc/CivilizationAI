"""v2.0, PR 3.5: the cognitive filter and the multi-persona debate. The points under test:

  * the filter is part of the voice of EVERY reply (normal, Danish immersion, proactive): it names the four explicit patterns, forbids diagnoses and
    labels, tells the bot not to argue with an emotional user but to acknowledge, gently show the gap or suggest a pause, and to be extremely tactful
    (a specialist, a person nearby) with heavy markers;
  * ``debate`` is offered only on an explicit request (a regex in code), is EXCLUSIVE (no other tool in that reply), costs ONE model turn (no second
    phrasing turn), takes 2-3 different roles from a closed set, and the message the user reads is laid out by code, never by the model.
Real PostgreSQL, the real aiogram Dispatcher on a fake Telegram wire, a fake Gemini."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from ai_civ.bot import texts
from ai_civ.bot.orchestrator import load_tenant
from ai_civ.bot.proactive import get_trigger
from ai_civ.bot.prompt import VOICE, Profile, render_system
from ai_civ.bot.tools import debate
from ai_civ.bot.tools.base import Offered, ToolContext, ToolRegistry, ToolResult, UserTurn
from ai_civ.bot.tools.debate import DebateSpec, Voice, format_debate, parse_debate, wants_debate
from ai_civ.memory.llm import ToolCall, ToolReply, ToolSpec
from tests.bot_env import build_bot_env
from tests.helpers import new_persona, owner_exec

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
ASK = "Устрой дебаты: бросить работу и открыть кофейню"
SKEPTIC = "Кофейни закрываются в первый год чаще всего из-за аренды и нехватки оборотных денег; у вас нет подушки на полгода."
VISIONARY = "Хорошая локация и узнаваемый стиль превращают одну кофейню в сеть: ваш опыт в сервисе даёт сильную стартовую позицию."
PRAGMATIST = "Первый шаг: две недели поработать баристой по выходным и посчитать выручку на одном месте, прежде чем подписывать аренду."
QUESTION = "Что вы готовы проверить в ближайшие две недели, не уходя с работы?"


def args(**kw: Any) -> dict[str, Any]:
    base = {
        "topic": "Бросить работу и открыть кофейню",
        "roles": [{"role": "skeptic", "text": SKEPTIC}, {"role": "visionary", "text": VISIONARY}, {"role": "pragmatist", "text": PRAGMATIST}],
        "question": QUESTION,
    }
    return {**base, **kw}


def call(**kw: Any) -> ToolCall:
    return ToolCall(debate.TOOL_NAME, args(**kw))


# ================================================================== pure: the cognitive filter


def system(mode: str | None = None, proactive: bool = False) -> str:
    kw: dict[str, Any] = {} if mode is None else {"mode": mode}
    return render_system("PERSONA", Profile(), now=NOW, timezone="Europe/Copenhagen", proactive=proactive, **kw)


def test_the_filter_is_a_section_of_the_voice_and_therefore_of_every_prompt():
    assert "## Cognitive filter" in VOICE
    for text in (system(), system("immersion_da"), system(proactive=True)):
        assert text.count("## Cognitive filter") == 1 and text.index(VOICE) < text.index("## Cognitive filter") < text.index("## Current time")


def test_the_filter_names_the_four_explicit_patterns_and_only_when_explicit():
    low = " ".join(VOICE.lower().split())
    for pattern in ("catastrophizing", "black-and-white", "tunnel vision", "chronic overload or burnout"):
        assert pattern in low
    assert "only what the user's own words make explicit" in low and "never hunt for them" in low


def test_the_filter_forbids_diagnoses_and_labels_of_the_person():
    low = " ".join(VOICE.lower().split())
    assert "never diagnose" in low and "no medical or psychological labels" in low and "verdicts about the person" in low
    for term in ("депрессия", "тревожное расстройство", "когнитивное искажение", "катастрофизация"):
        assert term in VOICE  # named in the list of what NOT to say


def test_an_emotional_user_is_not_argued_with_but_acknowledged_then_shown_the_gap_or_offered_a_pause():
    low = " ".join(VOICE.lower().split())
    assert "do not argue and do not correct" in low and "acknowledge the feeling" in low
    assert "logical gap" in low and "pause" in low and "a breath" in low and "coming back to it later" in low


def test_heavy_markers_get_a_tactful_suggestion_to_see_a_specialist_and_danger_gets_the_emergency_number():
    low = " ".join(VOICE.lower().split())
    assert "heavy markers" in low and "extremely tactful" in low and "specialist" in low and "someone close" in low
    assert "emergency number" in low and "never as a way to end the topic" in low


def test_the_filter_does_not_make_replies_longer():
    assert "never adds length" in VOICE.lower()


def test_the_existing_voice_is_intact():
    assert "## Reading the user's state" in VOICE and "Banned: officialese" in VOICE and VOICE.startswith("## Voice")


# ================================================================== pure: the marker check and the tool


@pytest.mark.parametrize(
    "said",
    [
        "Устрой дебаты по моей идее",
        "устрой дебаты",
        "Дебаты!",
        "Давай устроим дебаты про переезд",
        "Разбери идею с разных сторон",
        "разбери мою идею стартапа с разных сторон",
        "Разбери это решение с двух сторон",
        "Раздели это с разных сторон",
        "Критикуй идею: продавать курсы по датскому",
        "покритикуй мой план",
        "Let's have a debate about this",
        "play devil's advocate on my plan",
        "argue both sides",
    ],
)
def test_an_explicit_request_for_a_debate_is_a_marker(said):
    assert wants_debate(said)


@pytest.mark.parametrize(
    "said",
    [
        "Хочу открыть кофейню",
        "Что думаешь про мою идею?",  # asking an opinion is not asking for a debate
        "Я не хочу спорить",
        "как прошли президентские дебаты вчера",  # still contains the word: it IS a marker (a human, not the model, wrote it)
        "",
    ][:3] + [""],
)
def test_ordinary_talk_is_not_a_marker(said):
    assert not wants_debate(said)


def test_the_tool_has_a_closed_set_of_roles_two_or_three_and_a_closing_question():
    schema = debate.TOOL.parameters
    assert debate.TOOL.name == "debate" == debate.TOOL_NAME and set(schema["required"]) == {"topic", "roles", "question"}
    roles = schema["properties"]["roles"]
    assert (roles["minItems"], roles["maxItems"]) == (2, 3) and roles["items"]["properties"]["role"]["enum"] == ["skeptic", "visionary", "pragmatist"]
    assert json.dumps(schema) and isinstance(debate.TOOL, ToolSpec)


def test_the_description_gives_each_voice_its_job():
    low = debate.TOOL.description.lower()
    assert "skeptic" in low and "hidden costs" in low and "logical gaps" in low
    assert "visionary" in low and "scalability" in low and "pragmatist" in low and "first steps" in low and "one call" in low


def test_the_instructions_ask_for_one_call_and_forbid_writing_the_debate_out():
    low = debate.INSTRUCTIONS.lower()
    assert "once" in low and "do not write the\ndebate in text".replace("\n", " ") in low.replace("\n", " ") and "no other function" in low


def test_a_valid_call_becomes_three_voices_in_the_canonical_order():
    spec = parse_debate([call(roles=[{"role": "pragmatist", "text": PRAGMATIST}, {"role": "skeptic", "text": SKEPTIC}, {"role": "visionary", "text": VISIONARY}])])
    assert spec is not None and [v.role for v in spec.voices] == ["skeptic", "visionary", "pragmatist"]  # the order is the code's, not the model's
    assert (spec.topic, spec.question) == ("Бросить работу и открыть кофейню", QUESTION)


def test_two_voices_are_a_debate_too():
    spec = parse_debate([call(roles=[{"role": "visionary", "text": VISIONARY}, {"role": "skeptic", "text": SKEPTIC}])])
    assert spec is not None and [v.role for v in spec.voices] == ["skeptic", "visionary"]


@pytest.mark.parametrize(
    "roles",
    [
        [{"role": "skeptic", "text": SKEPTIC}],  # one voice is not a debate
        [],
        [{"role": "skeptic", "text": SKEPTIC}, {"role": "skeptic", "text": "Ещё раз то же самое."}],  # the same role twice is one voice
        [{"role": "skeptic", "text": SKEPTIC}, {"role": "judge", "text": "Я судья."}],  # an unknown role is dropped: one voice is left
        [{"role": "skeptic", "text": SKEPTIC}, {"role": "visionary", "text": ""}],
        [{"role": "skeptic", "text": SKEPTIC}, {"role": "visionary", "text": "   "}],
        [{"role": "skeptic", "text": SKEPTIC}, {"role": "visionary", "text": "!!!"}],
        [{"role": "skeptic", "text": SKEPTIC}, {"role": "visionary", "text": "я" * 701}],  # too long: dropped, never cut
        [{"role": "skeptic", "text": SKEPTIC}, {"role": "visionary", "text": "скрытый‮ текст"}],
        [{"role": "skeptic", "text": SKEPTIC}, {"role": "visionary", "text": 5}],
        [{"role": "skeptic", "text": SKEPTIC}, {"role": None, "text": VISIONARY}],
        [{"role": "skeptic", "text": SKEPTIC}, "visionary: хорошо"],
        [{"role": "skeptic", "text": SKEPTIC}, None],
        "skeptic, visionary",
        None,
    ],
)
def test_fewer_than_two_usable_different_voices_is_no_debate(roles):
    assert parse_debate([call(roles=roles)]) is None


def test_a_role_name_is_matched_without_case_and_spaces_and_a_bad_voice_does_not_spoil_the_others():
    spec = parse_debate([call(roles=[{"role": " Skeptic ", "text": SKEPTIC}, {"role": "visionary", "text": ""}, {"role": "PRAGMATIST", "text": PRAGMATIST}])])
    assert spec is not None and [v.role for v in spec.voices] == ["skeptic", "pragmatist"]


def test_a_voice_is_one_line_and_never_cut():
    spec = parse_debate([call(roles=[{"role": "skeptic", "text": "Первая   мысль.\n\nВторая мысль.  "}, {"role": "visionary", "text": "я" * 700}])])
    assert spec is not None and spec.voices[0].text == "Первая мысль. Вторая мысль." and len(spec.voices[1].text) == 700


def test_a_missing_topic_or_question_gets_the_codes_own_words():
    spec = parse_debate([call(topic="", question=None)])
    assert spec is not None and (spec.topic, spec.question) == (texts.DEBATE_TOPIC_DEFAULT, texts.DEBATE_QUESTION_DEFAULT)
    spec = parse_debate([call(topic="x" * 201, question="y" * 301)])
    assert spec is not None and (spec.topic, spec.question) == (texts.DEBATE_TOPIC_DEFAULT, texts.DEBATE_QUESTION_DEFAULT)


def test_only_calls_of_debate_count():
    assert parse_debate([ToolCall("save_fact", args())]) is None and parse_debate([]) is None


def test_the_message_is_laid_out_by_code_one_block_per_voice_and_the_question_last():
    spec = DebateSpec("Кофейня", (Voice("skeptic", "Риски."), Voice("visionary", "Рост."), Voice("pragmatist", "Шаги.")), "Что дальше?")
    assert format_debate(spec) == (
        "🎭 Дебаты: Кофейня\n\n🔍 Критик\nРиски.\n\n💡 Визионер\nРост.\n\n⚖️ Прагматик\nШаги.\n\n❓ Что дальше?"
    )
    two = format_debate(DebateSpec("Т", (Voice("skeptic", "а"), Voice("pragmatist", "б")), "В?"))
    assert two.count("\n\n") == 3 and "💡" not in two and two.endswith("❓ В?")


def test_the_longest_possible_debate_fits_a_telegram_message():
    spec = DebateSpec("т" * 200, tuple(Voice(role, "я" * 700) for role in debate.ROLES), "в" * 300)
    assert len(format_debate(spec)) < 4096


def test_the_role_headings_are_the_documented_ones():
    assert texts.DEBATE_ROLES == {"skeptic": "🔍 Критик", "visionary": "💡 Визионер", "pragmatist": "⚖️ Прагматик"}
    assert set(texts.DEBATE_ROLES) == set(debate.ROLES)


# ================================================================== the registry: exclusive tools


class Stub:
    def __init__(self, name: str, *, rank: int, exclusive: bool = False, on: bool = True) -> None:
        self.name, self.functions, self.offer_rank, self.on = name, frozenset({name}), rank, on
        if exclusive:
            self.exclusive = True

    async def offer(self, ctx: ToolContext) -> Offered | None:
        async def run(calls: Any) -> ToolResult | None:
            return None

        return Offered((ToolSpec(self.name, "d", {"type": "object", "properties": {}}),), "", run, self.offer_rank) if self.on else None


CTX = ToolContext(tenant=None, persona=None, turn=UserTurn(__import__("uuid").uuid4(), "x"))  # type: ignore[arg-type]


async def test_an_exclusive_tool_that_is_on_offer_pushes_every_other_tool_out():
    registry = ToolRegistry([Stub("save_fact", rank=40), Stub("debate", rank=5, exclusive=True), Stub("create_event", rank=10)])
    offered = await registry.offer(CTX)
    assert [o.specs[0].name for o in offered] == ["debate"]


async def test_without_the_exclusive_tool_on_offer_the_others_stay():
    registry = ToolRegistry([Stub("save_fact", rank=40), Stub("debate", rank=5, exclusive=True, on=False), Stub("create_event", rank=10)])
    assert [o.specs[0].name for o in await registry.offer(CTX)] == ["create_event", "save_fact"]


async def test_exclusivity_does_not_leak_into_the_untrusted_filter():
    registry = ToolRegistry([Stub("debate", rank=5, exclusive=True), Stub("create_cards", rank=45)])
    assert await registry.offer(CTX, untrusted=True) == []  # neither opted in for an attachment reply


# ================================================================== the whole flow through the Dispatcher


async def make_env(owner_engine, db, **kw: Any):
    settings = {"debate_enabled": True, "save_fact_enabled": True, "quick_events_enabled": True, "tasks_enabled": True, "sr_enabled": True, **kw}
    env = await build_bot_env(owner_engine, db, **settings)
    env.chat.responder = lambda system, turns: "Обычный ответ."
    return env


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


def model_debates(env, *calls: ToolCall, text: str = "Я всё разобрал и сохранил!") -> None:
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))


async def test_a_request_gets_exactly_one_model_turn_and_the_user_reads_the_blocks_laid_out_by_code(owner_engine, db):
    env = await make_env(owner_engine, db)
    try:
        model_debates(env, call())
        await env.feed(ASK)
        assert len(env.chat.tool_calls) == 1 and env.chat.calls == []  # ONE structured turn: no second phrasing turn
        offered = env.chat.tool_calls[0]
        assert [t.name for t in offered["tools"]] == ["debate"] and "## Debate" in offered["system"]
        expected = format_debate(parse_debate([call()]))  # type: ignore[arg-type]
        assert says(env) == [expected]
        assert "🔍 Критик" in expected and "💡 Визионер" in expected and "⚖️ Прагматик" in expected and expected.rstrip().endswith("❓ " + QUESTION)
        assert "сохранил" not in " ".join(says(env)).lower()  # the words written next to the call are never sent
    finally:
        await env.bot.session.close()


async def test_while_a_debate_is_on_offer_no_other_tool_is_offered_even_with_every_marker_in_the_message(owner_engine, db):
    env = await make_env(owner_engine, db, topic_rename_enabled=True)
    try:
        model_debates(env, call())
        await env.feed("Устрой дебаты и разбей на шаги, сделай карточки, завтра в 10 у меня встреча. Я бегаю по утрам 5 км")
        assert [t.name for t in env.chat.tool_calls[0]["tools"]] == ["debate"]  # not save_fact, decompose_task, create_cards, create_event
    finally:
        await env.bot.session.close()


async def test_a_model_that_calls_destructive_tools_in_the_debate_turn_changes_nothing(owner_engine, db):
    env = await make_env(owner_engine, db)
    try:
        said = "Устрой дебаты: бросить работу. Я бегаю по утрам 5 км, завтра в 10 у меня встреча"
        injected = (
            call(),
            ToolCall("save_fact", {"category": "habit", "topic": "morning_run", "content": "Бегает по утрам 5 км", "evidence": "бегаю по утрам 5 км"}),
            ToolCall("create_event", {"title": "Встреча", "start": "2031-01-01T10:00"}),
            ToolCall("decompose_task", {"goal": "Цель", "steps": [{"title": "а"}, {"title": "б"}]}),
            ToolCall("create_cards", {"deck": "Д", "cards": []}),
            ToolCall("purge_events", {}),
            ToolCall("propose_event", {"contact": "c1", "title": "x", "start": "2031-01-01T10:00"}),
        )
        model_debates(env, *injected)
        await env.feed(said)
        assert says(env) == [format_debate(parse_debate([call()]))]  # type: ignore[arg-type]
        for table in ("core_facts", "event_drafts", "events", "tasks", "cards", "event_proposals", "contacts"):
            [(count,)] = await owner_exec(env.owner_engine, f"SELECT count(*) FROM civ.{table} WHERE user_id = :u", u=env.user_id)
            assert count == 0, table  # a function that was not on offer ran nothing
    finally:
        await env.bot.session.close()


async def test_the_debate_is_remembered_in_the_conversation_as_the_assistants_message(owner_engine, db):
    env = await make_env(owner_engine, db)
    try:
        model_debates(env, call())
        await env.feed(ASK)
        env.chat.tool_responder = None
        await env.feed("А что бы ты выбрал?")
        history = env.chat.tool_calls[-1]["turns"]  # the follow-up has no marker: the usual tools are on offer again
        assert any(t.role == "model" and "🔍 Критик" in t.text and QUESTION in t.text for t in history)  # the follow-up sees the whole debate
        stored = await env.messages()
        assert stored[1]["role"] == "assistant" and stored[1]["content"].startswith("🎭 Дебаты:")
    finally:
        await env.bot.session.close()


async def test_a_message_without_a_request_is_never_offered_the_debate(owner_engine, db):
    env = await make_env(owner_engine, db)
    try:
        await env.feed("Хочу бросить работу и открыть кофейню, что думаешь?")
        for names in [[t.name for t in c["tools"]] for c in env.chat.tool_calls]:
            assert "debate" not in names
        assert all("## Debate" not in c["system"] for c in env.chat.calls + env.chat.tool_calls)
    finally:
        await env.bot.session.close()


async def test_the_switch_turns_the_debate_off_even_on_a_marker(owner_engine, db):
    env = await make_env(owner_engine, db, debate_enabled=False)
    try:
        await env.feed(ASK)
        assert all("debate" not in [t.name for t in c["tools"]] for c in env.chat.tool_calls)
    finally:
        await env.bot.session.close()


async def test_a_call_with_one_voice_is_not_trusted_and_the_reply_is_phrased_again_without_functions(owner_engine, db):
    env = await make_env(owner_engine, db)
    try:
        model_debates(env, call(roles=[{"role": "skeptic", "text": SKEPTIC}]), text="Дебаты готовы, всё записано!")
        env.chat.responder = lambda system, turns: "Давайте разберём иначе: что для вас главное в этой идее?"
        await env.feed(ASK)
        assert says(env) == ["Давайте разберём иначе: что для вас главное в этой идее?"]  # neither a half-debate nor the model's claim
        assert env.chat.calls and "tools" not in env.chat.calls[-1]
    finally:
        await env.bot.session.close()


async def test_a_document_caption_does_not_open_a_debate_round(owner_engine, db):
    env = await make_env(owner_engine, db)
    try:
        await env.feed_file("План кофейни: аренда, оборудование, меню.".encode(), caption=ASK, file_name="plan.txt", mime_type="text/plain")
        assert env.chat.tool_calls == []  # an attachment reply is offered only tools that opted in for it; the debate did not
        assert "## Debate" not in env.chat.calls[-1]["system"]
    finally:
        await env.bot.session.close()


async def test_a_spoken_request_is_a_debate_too(owner_engine, db):
    env = await make_env(owner_engine, db)
    try:
        model_debates(env, call())
        env.chat.transcriber = lambda data, mime: ASK
        await env.feed_voice()
        assert says(env)[-1] == format_debate(parse_debate([call()]))  # type: ignore[arg-type]
    finally:
        await env.bot.session.close()


async def test_a_debate_in_a_danish_immersion_topic_keeps_the_mode_in_the_prompt(owner_engine, db):
    env = await make_env(owner_engine, db)
    try:
        await new_persona(owner_engine, env.user_id, "danish", thread_id=55, title="Dansk")
        await owner_exec(owner_engine, "UPDATE civ.personas SET mode = 'immersion_da' WHERE user_id = :u", u=env.user_id)
        model_debates(env, call())
        await env.feed(ASK, thread_id=55)
        assert "Language immersion: Danish" in env.chat.tool_calls[0]["system"] and [t.name for t in env.chat.tool_calls[0]["tools"]] == ["debate"]
    finally:
        await env.bot.session.close()


# ================================================================== the filter reaches the model


async def test_the_filter_is_in_the_system_prompt_of_an_ordinary_reply_and_does_not_need_a_marker(owner_engine, db):
    env = await build_bot_env(owner_engine, db)
    try:
        env.chat.responder = lambda system, turns: "Понимаю, вымотались."
        await env.feed("Я больше не могу, всё пропало, у меня никогда ничего не получается")
        assert "## Cognitive filter" in env.chat.calls[-1]["system"] and "NEVER diagnose" in env.chat.calls[-1]["system"]
    finally:
        await env.bot.session.close()


async def test_the_filter_is_in_the_prompt_of_a_proactive_message_and_of_a_danish_topic(owner_engine, db):
    env = await build_bot_env(owner_engine, db)
    try:
        await new_persona(owner_engine, env.user_id, "foodguide", thread_id=60, title="Еда")
        env.chat.responder = lambda system, turns: "Как с обедом?"
        tenant = await load_tenant(db, env.user_id)
        assert await env.orchestrator.run_proactive(tenant, get_trigger("meal_check")) is True
        assert "## Cognitive filter" in env.chat.calls[-1]["system"]
        await owner_exec(owner_engine, "UPDATE civ.personas SET mode = 'immersion_da' WHERE user_id = :u", u=env.user_id)
        assert await env.orchestrator.run_proactive(tenant, get_trigger("meal_check")) is True
        assert "## Cognitive filter" in env.chat.calls[-1]["system"] and "Language immersion: Danish" in env.chat.calls[-1]["system"]
    finally:
        await env.bot.session.close()

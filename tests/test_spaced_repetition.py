"""v2.0, PR 3.2: spaced repetition. The points under test: the model is OFFERED ``create_cards`` only when CODE finds a marker in the user's own
words (also as the caption of a document, the one tool an attachment reply may have); a card reaches the database only as ONE question with EXACTLY
four different options and a valid index (code AND constraints); the user reads a fixed text, never the model's claim; the Beat dispatcher sends
due cards without breaking quiet hours, the daily limit or the unanswered cap, and never twice; a press grades the answer by code, shows an alert of
a few characters, edits the card into the analysis and moves the SM-2 interval, exactly once. Real PostgreSQL, the real aiogram Dispatcher on a
fake Telegram wire, a fake Gemini."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy.exc import DBAPIError, IntegrityError

from ai_civ import tasks
from ai_civ.bot import texts
from ai_civ.bot.tools import cards
from ai_civ.bot.tools.cards import Cards, Sm2, parse_card, parse_cards, sm2_next, wants_cards
from ai_civ.celery_app import app as celery_app
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_user, owner_exec, tenant_exec
from tests.tg_fakes import make_callback_update

CPH = ZoneInfo("Europe/Copenhagen")
ASK = "Сделай карточки по столицам Скандинавии"
DB_ERRORS = (IntegrityError, DBAPIError)


def card(question: str = "Столица Дании?", options: list[str] | None = None, correct_index: int = 1, explanation: str = "Копенгаген стоит на островах.") -> dict[str, Any]:
    options = options if options is not None else ["Стокгольм", "Копенгаген", "Осло", "Хельсинки"]
    return {"question": question, "options": options, "correct_index": correct_index, "explanation": explanation}


def call(*items: dict[str, Any], deck: str = "Столицы") -> ToolCall:
    return ToolCall(cards.TOOL_NAME, {"deck": deck, "cards": list(items) or [card()]})


def noon() -> datetime:
    """Noon today in the tenant's zone (Copenhagen): outside the default quiet hours (22:00-08:00), whatever the real clock says."""
    return datetime.combine(datetime.now(CPH).date(), time(12, 0), tzinfo=CPH).astimezone(UTC)


def night() -> datetime:
    return datetime.combine(datetime.now(CPH).date(), time(3, 0), tzinfo=CPH).astimezone(UTC)


# ================================================================== pure: the marker check, the tool, the cards


@pytest.mark.parametrize(
    "said",
    [
        "Сделай карточки по этой теме",
        "сделай мне карточки",
        "Создай тест по теме «Реформация»",
        "создай викторину по истории",
        "Подготовь квиз по датскому",
        "Хочу запомнить это",
        "помоги запомнить эти определения",
        "РАЗБЕЙ... нет: СДЕЛАЙ КАРТОЧКИ",
        "make flashcards from this",
        "quiz me on this chapter",
        "create a quiz about Denmark",
    ],
)
def test_an_explicit_request_for_cards_is_a_marker(said):
    assert wants_cards(said)


@pytest.mark.parametrize(
    "said",
    [
        "Запомни, что я бегаю по утрам",  # the imperative is the bot's memory (save_fact), not learning
        "Карточка банка потерялась",
        "Как прошёл тест на ковид?",
        "Расскажи про столицы",
        "У меня завтра тестирование",
        "",
    ],
)
def test_ordinary_talk_is_not_a_marker(said):
    assert not wants_cards(said)


def test_the_tool_needs_a_deck_and_cards_and_asks_for_exactly_four_options():
    schema = cards.TOOL.parameters
    assert cards.TOOL.name == "create_cards" == cards.TOOL_NAME and set(schema["required"]) == {"deck", "cards"}
    options = schema["properties"]["cards"]["items"]["properties"]["options"]
    assert (options["minItems"], options["maxItems"]) == (4, 4) and json.dumps(schema)


def test_the_instructions_forbid_claiming_the_cards_were_saved():
    low = cards.INSTRUCTIONS.lower()
    assert "never say they were saved" in low and "do not list the cards" in low


def test_a_valid_card_is_cleaned_to_one_line_per_field():
    parsed = parse_card(card(question="  Столица   Дании? ", options=[" Стокгольм", "Копенгаген ", "Осло", "Хельсинки"]))
    assert parsed is not None and parsed.question == "Столица Дании?" and parsed.options == ("Стокгольм", "Копенгаген", "Осло", "Хельсинки")
    assert (parsed.correct_index, parsed.explanation) == (1, "Копенгаген стоит на островах.")


@pytest.mark.parametrize(
    "bad",
    [
        {"options": ["а", "б", "в"]},  # three
        {"options": ["а", "б", "в", "г", "д"]},  # five
        {"options": ["а", "б", "в", "а"]},  # a repeated option
        {"options": ["Осло", "осло ", "в", "г"]},  # the same option up to case and spaces
        {"options": ["а", "б", "в", ""]},
        {"options": ["а", "б", "в", "   "]},
        {"options": ["а", "б", "в", None]},
        {"options": ["а", "б", "в", 4]},
        {"options": ["а", "б", "в", "x" * 101]},
        {"options": ["а", "б", "в", "г‮"]},  # a bidi control character
        {"options": "а, б, в, г"},
        {"options": None},
        {"correct_index": 4},
        {"correct_index": -1},
        {"correct_index": True},
        {"correct_index": "1"},
        {"correct_index": 1.0},
        {"correct_index": None},
        {"question": ""},
        {"question": "   "},
        {"question": "x" * 401},
        {"question": 5},
        {"question": "!!!"},
    ],
)
def test_an_unusable_card_is_dropped(bad):
    assert parse_card({**card(), **bad}) is None


def test_line_breaks_and_runs_of_spaces_in_a_question_or_an_option_collapse_instead_of_dropping_the_card():
    parsed = parse_card(card(question="Столица\nДании?\n\n  Выберите", options=["Стокгольм\n(Швеция)", "Копенгаген", "Осло\tНорвегия", "Хельсинки"]))
    assert parsed is not None and parsed.question == "Столица Дании? Выберите"
    assert parsed.options == ("Стокгольм (Швеция)", "Копенгаген", "Осло Норвегия", "Хельсинки")
    assert parse_card(card(question="Скрытый\x00 вопрос?")) is None  # a control character that is not whitespace is still refused
    assert parse_card(card(options=["а", "б", "в", "г\u200bд"])) is None  # so is a zero-width one


def test_the_right_option_may_be_any_of_the_four():
    assert [parse_card(card(correct_index=i)).correct_index for i in range(4)] == [0, 1, 2, 3]  # type: ignore[union-attr]


def test_a_long_explanation_is_cut_but_a_missing_one_is_fine():
    assert len(parse_card(card(explanation="я" * 900)).explanation) == 600  # type: ignore[union-attr]
    assert parse_card({k: v for k, v in card().items() if k != "explanation"}).explanation == ""  # type: ignore[union-attr]


def test_the_bad_cards_of_a_call_are_dropped_one_by_one_and_the_rest_stay():
    spec = parse_cards({"deck": "Д", "cards": [card("а?"), card("б?", options=["1", "1", "2", "3"]), card("в?"), card("а?"), "junk"]}, 10)
    assert spec is not None and [c.question for c in spec.cards] == ["а?", "в?"]  # the repeated question collapses too


def test_a_call_with_no_usable_card_is_dropped_and_a_missing_deck_gets_a_default():
    assert parse_cards({"deck": "Д", "cards": [card(correct_index=9)]}, 10) is None
    assert parse_cards({"deck": "Д", "cards": "нет"}, 10) is None and parse_cards({"deck": "Д"}, 10) is None
    assert parse_cards({"cards": [card()]}, 10).deck == cards.DEFAULT_DECK  # type: ignore[union-attr]
    assert parse_cards({"deck": 5, "cards": [card()]}, 10).deck == cards.DEFAULT_DECK  # type: ignore[union-attr]


def test_more_cards_than_the_cap_are_cut():
    spec = parse_cards({"deck": "Д", "cards": [card(f"вопрос {i}?") for i in range(30)]}, 4)
    assert spec is not None and len(spec.cards) == 4


# ---- SM-2


def test_sm2_right_answers_give_one_day_then_six_then_interval_times_ease():
    state = cards.NEW_CARD
    seen = []
    for _ in range(4):
        state = sm2_next(state, True)
        seen.append((state.state, state.interval_days, state.reps))
    assert seen == [("learning", 1, 1), ("review", 6, 2), ("review", 15, 3), ("review", 38, 4)]  # 6 * 2.5 = 15, 15 * 2.5 = 37.5 -> 38
    assert state.ease == 2.5  # grade 4 leaves the ease alone


def test_sm2_a_wrong_answer_starts_over_comes_back_tomorrow_and_lowers_the_ease():
    state = sm2_next(sm2_next(sm2_next(cards.NEW_CARD, True), True), True)
    wrong = sm2_next(state, False)
    assert (wrong.state, wrong.interval_days, wrong.reps, wrong.lapses, wrong.ease) == ("learning", 1, 0, 1, 1.96)
    assert sm2_next(wrong, True).interval_days == 1  # the repetitions start from the first step again


def test_sm2_the_ease_never_goes_below_1_3_and_the_interval_never_above_a_year():
    state = cards.NEW_CARD
    for _ in range(10):
        state = sm2_next(state, False)
    assert state.ease == 1.3 and state.lapses == 10
    big = sm2_next(Sm2("review", 300, 3.0, 5, 0), True)
    assert big.interval_days == 365


def test_the_callback_data_round_trips_and_fits_telegram():
    cid = uuid.uuid4()
    for option in range(4):
        data = cards.callback_data(cid, option)
        assert data == f"sr:{cid}:{option}" and len(data.encode()) <= 64 and cards.parse_callback(data) == (cid, option)
    for bad in ("", "sr:", f"sr:{cid}:4", f"sr:{cid}:-1", f"sr:{cid}:", f"tk:{cid}:1", "sr:not-a-uuid:1", f"sr:{cid}:1:2"):
        assert cards.parse_callback(bad) is None


def test_the_question_has_four_buttons_and_a_long_option_is_shortened_on_its_button_only():
    cid = uuid.uuid4()
    long = "очень длинный вариант ответа " * 3
    options = ["коротко", long.strip()[:100], "в", "г"]
    markup = cards.question_markup(cid, options)
    assert [row[0].callback_data for row in markup.inline_keyboard] == [f"sr:{cid}:{i}" for i in range(4)]
    assert all(len(row[0].text) <= 3 + cards.BUTTON_CHARS for row in markup.inline_keyboard) and markup.inline_keyboard[1][0].text.endswith("…")
    shown = cards.question_text("Колода", "Вопрос?", options)
    assert options[1] in shown and shown.splitlines()[0] == "🃏 Колода"  # the message shows the option in full


def test_the_alert_is_short_and_exactly_the_verdict():
    assert cards.alert_text(True) == "✅ Верно!" and cards.alert_text(False) == "❌ Ошибка!"
    assert max(len(cards.alert_text(v)) for v in (True, False)) <= cards.ALERT_LIMIT == 200


def test_the_analysis_marks_the_right_option_the_wrong_choice_and_the_explanation():
    a = cards.Analysis("Д", "Вопрос?", ("а", "б", "в", "г"), 1, "Потому что так.", 2, 1)
    shown = cards.analysis_text(a)
    assert shown.splitlines()[0] == "❌ Д" and "✅ B) б" in shown and "❌ C) в" in shown and "▫️ A) а" in shown and "▫️ D) г" in shown
    assert "💡 Потому что так." in shown and shown.endswith("Вернусь к ней завтра.")
    assert cards.analysis_text(cards.Analysis("Д", "В?", ("а", "б", "в", "г"), 1, "", 1, 6)).splitlines()[0] == "✅ Д"
    assert "через 6 дн." in cards.analysis_text(cards.Analysis("Д", "В?", ("а", "б", "в", "г"), 1, "", 1, 6)) and "💡" not in cards.analysis_text(cards.Analysis("Д", "В?", ("а", "б", "в", "г"), 1, "", 1, 6))


# ================================================================== fixtures and helpers


@pytest_asyncio.fixture
async def card_env(owner_engine, db):
    """``bot_env`` with flashcards on, as in production (the function is still offered only on a marker)."""
    env = await build_bot_env(owner_engine, db, sr_enabled=True)
    yield env
    await env.bot.session.close()


def engine_of(env, **settings_kw: Any) -> Cards:
    if not settings_kw:
        return env.orchestrator.cards
    return Cards(env.db, env.bot, Settings(_env_file=None, **settings_kw))


async def make_cards(env, n: int = 1, **settings_kw: Any):
    items = [card(f"Вопрос {i}?", options=[f"а{i}", f"б{i}", f"в{i}", f"г{i}"], correct_index=i % 4) for i in range(n)]
    return await engine_of(env, **settings_kw).create(env.user_id, [call(*items)])


async def rows(env, sql: str, **kw: Any):
    return await owner_exec(env.owner_engine, sql, u=env.user_id, **kw)


async def one_card(env) -> uuid.UUID:
    await make_cards(env, 1)
    return (await rows(env, "SELECT id FROM civ.cards WHERE user_id = :u"))[0][0]


def quiz_messages(env) -> list[Any]:
    return [
        m for m in env.session.sent()
        if m.chat_id == env.chat_id and m.reply_markup is not None and m.reply_markup.inline_keyboard[0][0].callback_data.startswith("sr:")
    ]  # fmt: skip


def says(env) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id is None]


async def press(env, data: str, *, tg: int | None = None, chat_id: int | None = None) -> None:
    await env.dispatcher.feed_update(
        env.bot, make_callback_update(data=data, user_id=tg or env.telegram_user_id, chat_id=chat_id or env.chat_id, message_id=901)
    )


def model_makes(env, *calls: ToolCall, text: str = "Готово, всё записал и сохранил!", then: str = "Отличная тема, учим.") -> None:
    """The fake model calls the function - and, as a real one might, ALSO claims it saved everything."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


async def sent_card(env, n: int = 1) -> uuid.UUID:
    """Make ``n`` cards, send them (the engine, at noon) and return the id of the first."""
    await make_cards(env, n)
    assert await engine_of(env).send_due(env.user_id, now=noon()) >= 1
    return cards.parse_callback(quiz_messages(env)[0].reply_markup.inline_keyboard[0][0].callback_data)[0]  # type: ignore[index]


COUNT_CARDS = "SELECT count(*) FROM civ.cards WHERE user_id = :u"
REVIEW = "SELECT state, interval_days, ease, reps, lapses, last_chosen, reviewed_at IS NOT NULL, last_sent_at IS NOT NULL FROM civ.card_reviews WHERE user_id = :u"


# ================================================================== the database


async def test_a_card_and_its_review_belong_to_one_tenant(card_env, owner_engine, db):
    await make_cards(card_env, 2)
    stranger = await new_user(owner_engine)
    assert await tenant_exec(db, stranger, "SELECT count(*) FROM civ.cards") == [(0,)]
    assert await tenant_exec(db, stranger, "SELECT count(*) FROM civ.card_reviews") == [(0,)]
    assert await tenant_exec(db, card_env.user_id, "SELECT count(*) FROM civ.cards") == [(2,)]


async def test_the_composite_foreign_key_refuses_a_review_of_another_tenants_card(card_env, owner_engine, db):
    mine = await one_card(card_env)
    stranger = await new_user(owner_engine)
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, stranger, "INSERT INTO civ.card_reviews (user_id, card_id) VALUES (:u, :c)", u=stranger, c=mine)


INSERT = "INSERT INTO civ.cards (user_id, deck, question, options, correct_index, explanation) VALUES (:u, :d, :q, {options}, :i, :e)"


@pytest.mark.parametrize(
    "options, extra",
    [
        ("ARRAY['а','б','в']", {}),  # three
        ("ARRAY['а','б','в','г','д']", {}),  # five
        ("ARRAY['а','б','в','а']", {}),
        ("ARRAY['Осло','осло ','в','г']", {}),
        ("ARRAY['а','б','в','']", {}),
        ("ARRAY['а','б','в',NULL]", {}),
        ("ARRAY['а','б','в',repeat('я',101)]", {}),
        ("ARRAY['а','б','в',E'г\\nд']", {}),  # a control character
        ("ARRAY[['а','б'],['в','г']]", {}),  # two-dimensional
        ("ARRAY['а','б','в','г']", {"i": 4}),
        ("ARRAY['а','б','в','г']", {"i": -1}),
        ("ARRAY['а','б','в','г']", {"q": ""}),
        ("ARRAY['а','б','в','г']", {"q": "   "}),
        ("ARRAY['а','б','в','г']", {"q": "я" * 401}),
        ("ARRAY['а','б','в','г']", {"e": "я" * 601}),
        ("ARRAY['а','б','в','г']", {"d": ""}),
    ],
)
async def test_the_constraints_refuse_what_is_not_a_four_option_card(card_env, db, options, extra):
    params = {"u": card_env.user_id, "d": "Д", "q": "Вопрос?", "i": 0, "e": "", **extra}
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, card_env.user_id, INSERT.format(options=options), **params)


async def test_the_constraints_accept_a_good_card(card_env, db):
    await tenant_exec(db, card_env.user_id, INSERT.format(options="ARRAY['а','б','в','г']"), u=card_env.user_id, d="Д", q="Вопрос?", i=3, e="")
    assert await rows(card_env, COUNT_CARDS) == [(1,)]


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE civ.card_reviews SET reviewed_at = now() WHERE user_id = :u",  # answered, but the state is still 'new'
        "UPDATE civ.card_reviews SET state = 'review' WHERE user_id = :u",  # a state of an answered card without an answer
        "UPDATE civ.card_reviews SET state = 'learning', reviewed_at = now() WHERE user_id = :u",  # an answer without the chosen option
        "UPDATE civ.card_reviews SET state = 'mastered' WHERE user_id = :u",
        "UPDATE civ.card_reviews SET ease = 1.0 WHERE user_id = :u",
        "UPDATE civ.card_reviews SET ease = 4.0 WHERE user_id = :u",
        "UPDATE civ.card_reviews SET interval_days = -1 WHERE user_id = :u",
        "UPDATE civ.card_reviews SET reps = -1 WHERE user_id = :u",
        "UPDATE civ.card_reviews SET last_chosen = 4, state = 'learning', reviewed_at = now() WHERE user_id = :u",
    ],
)
async def test_the_review_constraints_refuse_an_impossible_state(card_env, db, sql):
    await one_card(card_env)
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, card_env.user_id, sql, u=card_env.user_id)


async def test_the_same_question_is_not_stored_twice_in_a_deck(card_env):
    first = await engine_of(card_env).create(card_env.user_id, [call(card("Один?"), deck="А")])
    again = await engine_of(card_env).create(card_env.user_id, [call(card("Один?"), deck="А")])
    other_deck = await engine_of(card_env).create(card_env.user_id, [call(card("Один?"), deck="Б")])
    assert (first.decks[0].created, again.decks[0].created, again.decks[0].duplicates, other_deck.decks[0].created) == (1, 0, 1, 1)
    assert await rows(card_env, COUNT_CARDS) == [(2,)]


async def test_deleting_a_card_deletes_its_review(card_env, owner_engine):
    await one_card(card_env)
    await owner_exec(owner_engine, "DELETE FROM civ.cards WHERE user_id = :u", u=card_env.user_id)
    assert await rows(card_env, "SELECT count(*) FROM civ.card_reviews WHERE user_id = :u") == [(0,)]


async def test_the_due_tenants_function_names_only_tenants_with_a_card_to_send(card_env, owner_engine, db):
    async def due() -> set[uuid.UUID]:
        return set(await db.tenants_with_due_cards(limit=100_000, resend_hours=24))

    assert card_env.user_id not in await due()  # no cards
    await make_cards(card_env, 1)
    assert card_env.user_id in await due()  # a new card is due at once
    await rows(card_env, "UPDATE civ.card_reviews SET last_sent_at = now() WHERE user_id = :u")
    assert card_env.user_id not in await due()  # sent after it became due: waiting for the answer
    await rows(card_env, "UPDATE civ.card_reviews SET last_sent_at = now() - interval '25 hours', next_review_at = now() - interval '26 hours' WHERE user_id = :u")
    assert card_env.user_id in await due()  # unanswered for longer than the resend interval
    await rows(card_env, "UPDATE civ.card_reviews SET last_sent_at = now() - interval '25 hours', next_review_at = now() + interval '1 day' WHERE user_id = :u")
    assert card_env.user_id not in await due()  # not due yet
    await rows(card_env, "UPDATE civ.card_reviews SET next_review_at = now() - interval '1 minute', last_sent_at = now() - interval '2 days' WHERE user_id = :u")
    assert card_env.user_id in await due()  # answered earlier, due again


@pytest.mark.parametrize("column, value", [("group_state", "frozen"), ("group_state", "dormant"), ("onboarding_state", "in_progress")])
async def test_the_due_tenants_function_skips_a_tenant_that_cannot_be_written_to(card_env, db, column, value):
    await make_cards(card_env, 1)
    await rows(card_env, f"UPDATE civ.users SET {column} = '{value}' WHERE id = :u")
    assert card_env.user_id not in set(await db.tenants_with_due_cards(limit=100_000, resend_hours=24))


# ================================================================== the engine: making cards


async def test_cards_are_stored_with_a_review_that_is_new_and_due_now(card_env):
    created = await make_cards(card_env, 3)
    assert created is not None and (created.decks[0].deck, created.decks[0].created) == ("Столицы", 3)
    stored = await rows(card_env, "SELECT deck, question, options, correct_index, explanation FROM civ.cards WHERE user_id = :u ORDER BY question")
    assert stored[0] == ("Столицы", "Вопрос 0?", ["а0", "б0", "в0", "г0"], 0, "Копенгаген стоит на островах.") and [r[3] for r in stored] == [0, 1, 2]
    assert {(r[0], r[1], float(r[2]), r[3], r[4], r[5], r[6], r[7]) for r in await rows(card_env, "SELECT state, interval_days, ease, reps, lapses, reviewed_at IS NULL, last_sent_at IS NULL, next_review_at <= now() FROM civ.card_reviews WHERE user_id = :u")} == {
        ("new", 0, 2.5, 0, 0, True, True, True)
    }


async def test_nothing_is_stored_for_a_call_that_has_no_usable_card_or_is_not_ours(card_env):
    engine = engine_of(card_env)
    assert await engine.create(card_env.user_id, [call(card(correct_index=7))]) is None
    assert await engine.create(card_env.user_id, [ToolCall("create_event", call().args)]) is None
    assert await rows(card_env, COUNT_CARDS) == [(0,)]


async def test_the_cards_of_one_message_are_capped_across_calls(card_env):
    items = [card(f"В{i}?", options=[f"а{i}", f"б{i}", f"в{i}", f"г{i}"]) for i in range(8)]
    created = await engine_of(card_env, sr_max_cards_per_call=5).create(card_env.user_id, [call(*items[:4], deck="А"), call(*items[4:], deck="Б")])
    assert sum(d.created for d in created.decks) == 5 and await rows(card_env, COUNT_CARDS) == [(5,)]


async def test_the_per_user_cap_stops_new_cards_and_says_so(card_env):
    assert (await make_cards(card_env, 2, sr_max_cards=2)).full is False
    more = await engine_of(card_env, sr_max_cards=2).create(card_env.user_id, [call(card("Новый?"))])
    assert more is not None and more.full and more.decks[0].created == 0 and await rows(card_env, COUNT_CARDS) == [(2,)]
    assert cards.created_text(more) == texts.CARDS_FULL


async def test_a_database_failure_makes_no_cards_and_never_raises(card_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Cards, "_insert", boom)
    assert await make_cards(card_env, 1) is None and await rows(card_env, COUNT_CARDS) == [(0,)]


# ================================================================== the engine: what is sent and when


async def test_a_due_card_is_sent_once_with_four_buttons_and_is_not_sent_again(card_env):
    await make_cards(card_env, 1)
    assert await engine_of(card_env).send_due(card_env.user_id, now=noon()) == 1
    [message] = quiz_messages(card_env)
    assert message.text.startswith("🃏 Столицы\n\nВопрос 0?") and "A) а0" in message.text and "D) г0" in message.text
    assert len(message.reply_markup.inline_keyboard) == 4 and [row[0].callback_data[-1] for row in message.reply_markup.inline_keyboard] == list("0123")
    assert await engine_of(card_env).send_due(card_env.user_id, now=noon()) == 0  # it waits for its answer, it is not repeated
    assert len(quiz_messages(card_env)) == 1


async def test_nothing_is_sent_in_the_users_quiet_hours(card_env):
    await make_cards(card_env, 1)
    assert await engine_of(card_env).send_due(card_env.user_id, now=night()) == 0 and quiz_messages(card_env) == []
    assert await engine_of(card_env).send_due(card_env.user_id, now=noon()) == 1  # the same card goes out in the day


async def test_changed_quiet_hours_take_effect_at_once(card_env):
    await make_cards(card_env, 1)
    await rows(card_env, "UPDATE civ.users SET quiet_hours_start = '11:00', quiet_hours_end = '13:00' WHERE id = :u")
    assert await engine_of(card_env).send_due(card_env.user_id, now=noon()) == 0
    await rows(card_env, "UPDATE civ.users SET quiet_hours_start = '14:00', quiet_hours_end = '15:00' WHERE id = :u")
    assert await engine_of(card_env).send_due(card_env.user_id, now=noon()) == 1


async def test_a_card_that_is_not_due_yet_is_not_sent(card_env):
    await make_cards(card_env, 1)
    await rows(card_env, "UPDATE civ.card_reviews SET next_review_at = now() + interval '1 day' WHERE user_id = :u")
    assert await engine_of(card_env).send_due(card_env.user_id, now=noon()) == 0


async def test_at_most_per_tick_cards_go_out_at_a_time_the_oldest_due_first(card_env):
    await make_cards(card_env, 5)
    await rows(card_env, "UPDATE civ.card_reviews r SET next_review_at = now() - make_interval(mins => 10 - c.q::int) FROM (SELECT id, substring(question from '[0-9]+')::int AS q FROM civ.cards) c WHERE r.card_id = c.id AND r.user_id = :u")
    engine = engine_of(card_env, sr_per_tick=2, sr_max_unanswered=10, sr_daily_limit=10)
    assert await engine.send_due(card_env.user_id, now=noon()) == 2
    assert [m.text.splitlines()[2] for m in quiz_messages(card_env)] == ["Вопрос 0?", "Вопрос 1?"]
    assert await engine.send_due(card_env.user_id, now=noon()) == 2 and await engine.send_due(card_env.user_id, now=noon()) == 1


async def test_the_daily_limit_counts_what_was_sent_since_the_local_midnight(card_env):
    await make_cards(card_env, 5)
    engine = engine_of(card_env, sr_daily_limit=3, sr_per_tick=10, sr_max_unanswered=10)
    assert await engine.send_due(card_env.user_id, now=noon()) == 3
    assert await engine.send_due(card_env.user_id, now=noon()) == 0  # the limit of the day is used up


async def test_cards_sent_yesterday_do_not_count_for_today(card_env):
    await make_cards(card_env, 4)
    await rows(card_env, "UPDATE civ.card_reviews SET last_sent_at = now() - interval '2 days', next_review_at = now() - interval '1 day', reviewed_at = now() - interval '2 days', state = 'learning', last_chosen = 0, interval_days = 1 WHERE user_id = :u")
    engine = engine_of(card_env, sr_daily_limit=1, sr_per_tick=10, sr_max_unanswered=10)
    assert await engine.send_due(card_env.user_id, now=noon()) == 1  # yesterday's sends are not today's
    assert await engine.send_due(card_env.user_id, now=noon()) == 0


async def test_the_bot_waits_when_too_many_cards_are_unanswered(card_env):
    await make_cards(card_env, 5)
    engine = engine_of(card_env, sr_per_tick=10, sr_daily_limit=10, sr_max_unanswered=2)
    assert await engine.send_due(card_env.user_id, now=noon()) == 2
    assert await engine.send_due(card_env.user_id, now=noon()) == 0  # two are waiting: no third until one is answered
    first = cards.parse_callback(quiz_messages(card_env)[0].reply_markup.inline_keyboard[0][0].callback_data)[0]  # type: ignore[index]
    await engine.press(card_env.user_id, first, 0)
    assert await engine.send_due(card_env.user_id, now=noon()) == 1  # an answered card frees one place


@pytest.mark.parametrize("column, value", [("group_state", "frozen"), ("group_state", "dormant"), ("onboarding_state", "in_progress")])
async def test_nothing_is_sent_to_a_tenant_that_cannot_be_written_to(card_env, column, value):
    await make_cards(card_env, 1)
    await rows(card_env, f"UPDATE civ.users SET {column} = '{value}' WHERE id = :u")
    assert await engine_of(card_env).send_due(card_env.user_id, now=noon()) == 0 and quiz_messages(card_env) == []


async def test_an_unanswered_card_is_offered_again_only_after_the_resend_interval(card_env):
    await make_cards(card_env, 1)
    engine = engine_of(card_env, sr_resend_hours=24, sr_max_unanswered=5)
    assert await engine.send_due(card_env.user_id, now=noon()) == 1
    await rows(card_env, "UPDATE civ.card_reviews SET last_sent_at = now() - interval '23 hours', next_review_at = now() - interval '30 hours' WHERE user_id = :u")
    assert await engine.send_due(card_env.user_id, now=noon()) == 0
    await rows(card_env, "UPDATE civ.card_reviews SET last_sent_at = now() - interval '25 hours', next_review_at = now() - interval '30 hours' WHERE user_id = :u")
    assert await engine.send_due(card_env.user_id, now=noon()) == 1 and len(quiz_messages(card_env)) == 2


async def test_two_parallel_ticks_send_a_card_exactly_once(card_env):
    await make_cards(card_env, 1)
    sent = await asyncio.gather(*(engine_of(card_env).send_due(card_env.user_id, now=noon()) for _ in range(6)))
    assert sum(sent) == 1 and len(quiz_messages(card_env)) == 1


async def test_a_failed_send_gives_the_claim_back_and_raises_and_the_next_tick_sends(card_env, monkeypatch):
    await make_cards(card_env, 2)
    engine = engine_of(card_env, sr_per_tick=2, sr_max_unanswered=5)
    real = Bot.send_message
    calls = {"n": 0}

    async def flaky(self, *a: Any, **k: Any):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("telegram is down")
        return await real(self, *a, **k)

    monkeypatch.setattr(Bot, "send_message", flaky)
    with pytest.raises(RuntimeError):
        await engine.send_due(card_env.user_id, now=noon())
    assert len(quiz_messages(card_env)) == 1  # the first was delivered and stays sent
    assert await rows(card_env, "SELECT count(*) FROM civ.card_reviews WHERE user_id = :u AND last_sent_at IS NULL") == [(1,)]  # the second was given back
    monkeypatch.setattr(Bot, "send_message", real)
    assert await engine.send_due(card_env.user_id, now=noon()) == 1 and len(quiz_messages(card_env)) == 2


# ================================================================== the engine: the answer


async def test_a_right_answer_is_graded_by_code_and_moves_the_interval(card_env):
    cid = await sent_card(card_env)
    result = await engine_of(card_env).press(card_env.user_id, cid, 0)  # card 0: the right option is 0
    assert result.outcome == "correct" and result.analysis.chosen == 0 and result.analysis.interval_days == 1
    [(state, interval, ease, reps, lapses, chosen, reviewed, sent)] = await rows(card_env, REVIEW)
    assert (state, interval, float(ease), reps, lapses, chosen, reviewed, sent) == ("learning", 1, 2.5, 1, 0, 0, True, True)
    [(due_in,)] = await rows(card_env, "SELECT extract(epoch FROM next_review_at - reviewed_at) FROM civ.card_reviews WHERE user_id = :u")
    assert float(due_in) == 86400.0


async def test_a_wrong_answer_comes_back_tomorrow_and_counts_a_lapse(card_env):
    cid = await sent_card(card_env)
    result = await engine_of(card_env).press(card_env.user_id, cid, 3)
    assert result.outcome == "wrong" and result.analysis.correct_index == 0 and result.analysis.chosen == 3
    [(state, interval, ease, reps, lapses, chosen, *_)] = await rows(card_env, REVIEW)
    assert (state, interval, float(ease), reps, lapses, chosen) == ("learning", 1, 1.96, 0, 1, 3)


async def test_a_second_press_changes_nothing_and_shows_the_first_answer(card_env):
    cid = await sent_card(card_env)
    engine = engine_of(card_env)
    await engine.press(card_env.user_id, cid, 0)
    before = await rows(card_env, REVIEW)
    again = await engine.press(card_env.user_id, cid, 2)  # a different option afterwards
    assert again.outcome == "settled" and again.analysis.chosen == 0 and await rows(card_env, REVIEW) == before


async def test_parallel_presses_of_one_card_grade_it_exactly_once(card_env):
    cid = await sent_card(card_env)
    results = await asyncio.gather(*(engine_of(card_env).press(card_env.user_id, cid, 0) for _ in range(6)))
    assert sorted(r.outcome for r in results) == ["correct"] + ["settled"] * 5
    assert (await rows(card_env, REVIEW))[0][3] == 1  # one repetition


async def test_a_card_the_bot_never_sent_cannot_be_answered(card_env):
    await make_cards(card_env, 1)
    cid = (await rows(card_env, "SELECT id FROM civ.cards WHERE user_id = :u"))[0][0]
    assert (await engine_of(card_env).press(card_env.user_id, cid, 0)).outcome == "gone"  # a forged button
    assert (await rows(card_env, REVIEW))[0][:1] == ("new",)


async def test_a_stranger_and_a_missing_card_are_gone(card_env, owner_engine):
    cid = await sent_card(card_env)
    stranger = await new_user(owner_engine)
    assert (await engine_of(card_env).press(stranger, cid, 0)).outcome == "gone"  # RLS: the row does not exist for them
    assert (await engine_of(card_env).press(card_env.user_id, uuid.uuid4(), 0)).outcome == "gone"
    assert (await rows(card_env, REVIEW))[0][6] is False  # still unanswered


async def test_the_schedule_grows_with_right_answers_and_the_card_is_sent_again_when_due(card_env):
    await make_cards(card_env, 1)
    engine = engine_of(card_env, sr_max_unanswered=5, sr_daily_limit=50)
    cid = (await rows(card_env, "SELECT id FROM civ.cards WHERE user_id = :u"))[0][0]
    intervals = []
    for _ in range(3):
        assert await engine.send_due(card_env.user_id, now=noon()) == 1
        intervals.append((await engine.press(card_env.user_id, cid, 0)).analysis.interval_days)
        await rows(card_env, "UPDATE civ.card_reviews SET next_review_at = now() - interval '1 minute', last_sent_at = now() - interval '2 days', reviewed_at = now() - interval '2 days' WHERE user_id = :u")
    assert intervals == [1, 6, 15]
    assert len(quiz_messages(card_env)) == 3


# ================================================================== the whole flow through the Dispatcher


async def test_a_request_gets_the_function_and_the_user_reads_a_fixed_text_not_the_models_claim(card_env):
    env = card_env
    model_makes(env, call(card("Столица Дании?"), card("Столица Норвегии?", options=["Осло", "Берген", "Тромсё", "Ставангер"], correct_index=0)))
    await env.feed(ASK)
    offered = env.chat.tool_calls[0]
    assert [t.name for t in offered["tools"]] == ["create_cards"] and "## Flashcards" in offered["system"]
    assert not any("сохранил" in t.lower() for t in says(env))  # the model's own words next to the call are never sent
    assert says(env) == ["Отличная тема, учим.\n\n🃏 Колода «Столицы»: добавил карточек — 2."]  # the rephrased reply, then the fixed text of the code
    assert await rows(env, COUNT_CARDS) == [(2,)] and quiz_messages(env) == []  # nothing is sent at once: the schedule sends
    assert "Flashcards (made by the system)" in env.chat.calls[-1]["system"]


async def test_a_request_without_a_marker_is_never_offered_the_function(card_env):
    env = card_env
    env.chat.responder = lambda system, turns: "Расскажите подробнее."
    await env.feed("Расскажи про столицы Скандинавии")
    assert env.chat.tool_calls == [] and "Flashcards" not in env.chat.calls[0]["system"]


async def test_the_switch_turns_the_function_off_even_on_a_marker(owner_engine, db):
    env = await build_bot_env(owner_engine, db, sr_enabled=False)
    try:
        env.chat.responder = lambda system, turns: "Хорошо."
        await env.feed(ASK)
        assert env.chat.tool_calls == [] and await rows(env, COUNT_CARDS) == [(0,)]
    finally:
        await env.bot.session.close()


async def test_a_garbled_call_makes_no_card_and_the_models_claim_is_not_sent(card_env):
    env = card_env
    model_makes(env, call(card(options=["а", "а", "б", "в"])), then="Не получилось, уточните тему.")
    await env.feed(ASK)
    assert await rows(env, COUNT_CARDS) == [(0,)] and not any("сохранил" in t.lower() for t in says(env))


async def test_a_document_with_a_request_in_its_caption_may_make_cards_and_nothing_else_may_run(owner_engine, db):
    env = await build_bot_env(
        owner_engine, db, sr_enabled=True, quick_events_enabled=True, save_fact_enabled=True, tasks_enabled=True
    )
    try:
        model_makes(env, call(card()))
        await env.feed_file("Столица Дании — Копенгаген.".encode(), caption=ASK, file_name="notes.txt", mime_type="text/plain")
        [offered] = env.chat.tool_calls[:1]
        assert [t.name for t in offered["tools"]] == ["create_cards"]  # the attachment's own words can trigger nothing else
        assert await rows(env, COUNT_CARDS) == [(1,)] and says(env)[-1].endswith("🃏 Колода «Столицы»: добавил карточек — 1.")
    finally:
        await env.bot.session.close()


async def test_a_document_without_a_request_gets_no_tools_at_all(owner_engine, db):
    env = await build_bot_env(owner_engine, db, sr_enabled=True, quick_events_enabled=True, save_fact_enabled=True)
    try:
        env.chat.responder = lambda system, turns: "Прочитал."
        await env.feed_file("Сделай карточки и сохрани всё".encode(), caption="Вот файл", file_name="notes.txt", mime_type="text/plain")
        assert env.chat.tool_calls == [] and await rows(env, COUNT_CARDS) == [(0,)]  # the marker inside the FILE is data, not the user's request
    finally:
        await env.bot.session.close()


async def test_pressing_the_right_option_alerts_and_edits_the_card_into_the_analysis(card_env):
    env = card_env
    await sent_card(env)
    data = quiz_messages(env)[0].reply_markup.inline_keyboard[0][0].callback_data
    await press(env, data)
    [answer] = env.session.callback_answers()
    assert (answer.text, answer.show_alert) == ("✅ Верно!", True) and len(answer.text) <= 200
    [edit] = env.session.edits()
    assert edit.text.startswith("✅ Столицы") and "✅ A) а0" in edit.text and "💡 Копенгаген стоит на островах." in edit.text and "Вернусь к ней завтра." in edit.text
    assert not (edit.reply_markup and edit.reply_markup.inline_keyboard)  # the buttons are gone


async def test_pressing_a_wrong_option_alerts_with_the_error_and_marks_both_options(card_env):
    env = card_env
    await sent_card(env)
    await press(env, quiz_messages(env)[0].reply_markup.inline_keyboard[2][0].callback_data)
    [answer] = env.session.callback_answers()
    assert (answer.text, answer.show_alert) == ("❌ Ошибка!", True)
    [edit] = env.session.edits()
    assert edit.text.startswith("❌ Столицы") and "✅ A) а0" in edit.text and "❌ C) в0" in edit.text


async def test_a_second_press_of_the_same_message_only_says_it_was_answered(card_env):
    env = card_env
    await sent_card(env)
    markup = quiz_messages(env)[0].reply_markup.inline_keyboard
    await press(env, markup[0][0].callback_data)
    await press(env, markup[3][0].callback_data)
    first, second = env.session.callback_answers()
    assert (first.text, first.show_alert) == ("✅ Верно!", True) and (second.text, bool(second.show_alert)) == (texts.CARD_SETTLED, False)
    assert (await rows(env, REVIEW))[0][3] == 1 and env.session.edits()[-1].text.startswith("✅")  # the first answer is still what is shown


async def test_a_strangers_press_and_a_forged_payload_reach_nothing(card_env, owner_engine):
    env = card_env
    await sent_card(env)
    data = quiz_messages(env)[0].reply_markup.inline_keyboard[0][0].callback_data
    stranger_tg, stranger_chat = 777_000_222, -1001234567891
    await new_user(owner_engine, telegram_user_id=stranger_tg, forum_chat_id=stranger_chat)
    await press(env, data, tg=stranger_tg, chat_id=stranger_chat)
    assert env.session.callback_answers()[-1].text == texts.CARD_GONE
    await press(env, "sr:garbage:1")
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_UNKNOWN_BUTTON
    assert (await rows(env, REVIEW))[0][6] is False and env.session.edits() == []


async def test_a_database_failure_on_a_press_is_visible_and_changes_nothing(card_env, monkeypatch):
    env = card_env
    await sent_card(env)
    data = quiz_messages(env)[0].reply_markup.inline_keyboard[0][0].callback_data

    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Cards, "press", boom)
    await press(env, data)
    assert env.session.callback_answers()[-1].text == texts.FAILURE and (await rows(env, REVIEW))[0][6] is False


# ================================================================== Celery / Beat


def test_the_beat_schedule_visits_the_dispatcher_every_ten_minutes_and_the_tasks_are_registered():
    entry = celery_app.conf.beat_schedule["cards-dispatch"]
    assert (entry["task"], entry["schedule"]) == ("ai_civ.dispatch_cards", 600.0)
    assert {"ai_civ.dispatch_cards", "ai_civ.send_cards"} <= set(celery_app.tasks)
    assert tasks.dispatch_cards.max_retries == tasks.send_cards.max_retries == 3  # bounded autoretry, no loops of our own


async def test_the_dispatcher_offers_exactly_the_tenants_with_a_card_to_send(card_env, db):
    await make_cards(card_env, 1)
    offered: list[uuid.UUID] = []
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, sr_dispatch_limit=100_000))
    await tasks.dispatch_cards_async(rt, offered.append)  # type: ignore[arg-type]
    assert card_env.user_id in offered
    offered.clear()
    await rows(card_env, "UPDATE civ.card_reviews SET last_sent_at = now() WHERE user_id = :u")
    await tasks.dispatch_cards_async(rt, offered.append)  # type: ignore[arg-type]
    assert card_env.user_id not in offered


async def test_the_dispatcher_does_nothing_when_the_feature_is_off(card_env, db):
    await make_cards(card_env, 1)
    offered: list[uuid.UUID] = []
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, sr_enabled=False))
    assert await tasks.dispatch_cards_async(rt, offered.append) == 0 and offered == []  # type: ignore[arg-type]


async def test_a_queue_failure_for_one_tenant_is_reported_after_the_others_were_offered(card_env, db):
    await make_cards(card_env, 1)
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, sr_dispatch_limit=100_000))

    def broken(tenant_id: uuid.UUID) -> None:
        raise ConnectionError("broker is down")

    with pytest.raises(tasks.DispatchError):
        await tasks.dispatch_cards_async(rt, broken)  # type: ignore[arg-type]


async def test_the_worker_task_sends_through_the_engine(card_env, db, monkeypatch):
    await make_cards(card_env, 1)
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, sr_per_tick=2))
    real = Cards.claim_due

    async def at_noon(self, tenant_id, *, now=None):
        return await real(self, tenant_id, now=noon())  # the real clock may be inside the tenant's quiet hours

    monkeypatch.setattr(Cards, "claim_due", at_noon)
    assert await tasks.send_cards_async(rt, card_env.bot, card_env.user_id) == 1  # type: ignore[arg-type]
    assert len(quiz_messages(card_env)) == 1

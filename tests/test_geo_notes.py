"""v2.0, PR 4.4: geo notes and places. The points under test: the model is OFFERED ``save_geo_note`` only when CODE finds a location-bound reminder in the user's own
words; the note must be the user's own words (the place stands in the message, every number does, longer words start like a word of it) and is bound to a saved
place by name / synonym / inflection - otherwise it waits under the place's name and is bound the first time the place is saved; the card never promises what the
bot cannot do (it fires nothing by itself); a sent location is measured against the user's places by haversine in plain Python, the active notes of the place are
listed with a button «✅ Выполнено» each, an unknown place offers to be saved (`/place` as a reply to the location, or the button under a waiting note's name).
Real PostgreSQL (RLS, composite FKs, CHECKs), the real aiogram Dispatcher on a fake Telegram wire, a fake Gemini."""

from __future__ import annotations

import random

import asyncio
import json
import math
import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.exc import DBAPIError, IntegrityError

from ai_civ.bot import texts
from ai_civ.bot.tools import UserTurn, geo
from ai_civ.bot.tools.geo import Geo, haversine_m, parse_note, same_place, same_word, wants_geo_note
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_message, new_user, owner_exec, tenant_exec
from tests.tg_fakes import make_callback_update, make_location_update, make_reply_to_location_update

DB_ERRORS = (IntegrityError, DBAPIError)
SHOP = (Decimal("55.676100"), Decimal("12.568300"))  # the centre of the "магазин" of these tests
NORTH_100M = Decimal("0.0009")  # ~100 m of latitude
ASK = "Напомни в магазине купить молоко"


def near(point: tuple[Decimal, Decimal] = SHOP, north: Decimal = Decimal(0)) -> tuple[Decimal, Decimal]:
    return point[0] + north, point[1]


def gcall(place: str = "магазин", content: str = "купить молоко") -> ToolCall:
    return ToolCall(geo.TOOL_NAME, {"place": place, "content": content})


# ================================================================== pure: distance, names, the cue check, the call


@pytest.mark.parametrize(
    ("a", "b", "metres", "tolerance"),
    [
        ((55.6761, 12.5683), (55.6761, 12.5683), 0.0, 0.001),
        ((55.6761, 12.5683), (56.1629, 10.2039), 157_000, 2_000),  # Copenhagen - Aarhus
        ((0.0, 0.0), (1.0, 0.0), 111_195, 200),  # one degree of latitude
        ((0.0, 0.0), (0.0, 1.0), 111_195, 200),  # one degree of longitude on the equator
        ((55.0, 12.0), (55.001, 12.0), 111.2, 0.5),  # 0.001 degree of latitude
        ((0.0, 0.0), (0.0, 180.0), 20_015_087, 10_000),  # half the Earth
        ((90.0, 0.0), (-90.0, 0.0), 20_015_087, 10_000),  # pole to pole
        ((0.0, 179.9999), (0.0, -179.9999), 22.2, 0.5),  # across the antimeridian
    ],
)
def test_haversine_matches_known_distances(a, b, metres, tolerance):
    assert haversine_m(*a, *b) == pytest.approx(metres, abs=tolerance)
    assert haversine_m(*b, *a) == pytest.approx(haversine_m(*a, *b))  # symmetric


@pytest.mark.parametrize(
    ("value", "limit", "expected"),
    [(55.6761, 90, Decimal("55.676100")), (-90, 90, Decimal("-90.000000")), (90.0000001, 90, None), (181, 180, None), (-181.5, 180, None), (float("nan"), 90, None),
     (float("inf"), 180, None), (True, 90, None), ("55.6", 90, None), (None, 90, None), (12.123456789, 180, Decimal("12.123457"))],
)  # fmt: skip
def test_a_coordinate_is_a_finite_number_inside_its_range_with_six_decimals(value, limit, expected):
    assert geo.coordinate(value, limit) == expected


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("магазин", "магазине", True),
        ("магазин", "магазина", True),
        ("зал", "зале", True),
        ("зал", "зала", True),
        ("дом", "домой", True),
        ("офис", "офисе", True),
        ("работа", "работе", True),
        ("кафе", "кафе", True),
        ("магазин", "магнит", False),
        ("зал", "залив", False),
        ("дом", "доктор", False),
        ("офис", "оптика", False),
    ],
)
def test_the_same_word_in_another_case(a, b, same):
    assert same_word(a, b) is same


def test_place_names_are_compared_word_by_word():
    assert same_place("тренажерный зал", "тренажерном зале") and same_place("продуктовый магазин", "продуктового магазина")
    assert not same_place("зал", "тренажерный зал") and not same_place("", "зал") and not same_place("зал", "")


@pytest.mark.parametrize(
    "said",
    [
        "Напомни в магазине купить молоко",
        "напомни мне завтра в аптеке взять рецепт",
        "Заметка для зала: проверить вес на жиме",
        "заметка в офис — забрать документы",
        "Когда буду в офисе — забрать документы",
        "когда я окажусь возле банка, оплатить счёт",
        "Как только приду домой, выключить свет",
        "remind me at the shop to buy milk",
        "when I'm at the gym check the weight",
        "Напомни около почты отправить посылку",
    ],
)
def test_a_location_bound_reminder_is_a_cue(said):
    assert wants_geo_note(said)


@pytest.mark.parametrize(
    "said",
    [
        "Напомни в 10 позвонить маме",  # a time, not a place
        "Хочу в магазин за молоком",
        "Купил молоко в магазине",
        "Как дела?",
        "Заметка про отпуск",
        "",
    ],
)
def test_ordinary_talk_is_not_a_cue(said):
    assert not wants_geo_note(said)


def test_the_tool_is_plain_json_and_the_instructions_do_not_promise_automatic_reminders():
    schema = geo.TOOL.parameters
    assert geo.TOOL.name == "save_geo_note" == geo.TOOL_NAME and set(schema["required"]) == {"place", "content"} and json.dumps(schema)
    low = geo.INSTRUCTIONS.lower()
    assert "do not promise" in low and "never say it was saved" in low and "you cannot see where the user is" in low


def test_a_valid_call_becomes_a_note():
    spec = parse_note(gcall().args, ASK)
    assert spec is not None and (spec.place, spec.content) == ("магазин", "купить молоко")


def test_the_place_may_be_inflected_in_the_message_and_the_content_may_use_other_forms_of_the_users_words():
    assert parse_note(gcall("зал", "проверить вес на жиме").args, "Заметка для зала: проверить вес на жиме") is not None
    assert parse_note(gcall("магазин", "купить 2 литра молока").args, "напомни в магазине купить 2 литра молока и хлеб") is not None


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (gcall("аптека").args, ASK),  # the place is not in the message
        (gcall(content="купить хлеб").args, ASK),  # the note says what the user did not say
        (gcall(content="купить молоко и шоколадные конфеты").args, ASK),
        (gcall(content="купить 3 молока").args, ASK),  # an invented number
        (gcall("", "купить молоко").args, ASK),
        (gcall("магазин", "").args, ASK),
        (gcall("магазин", "12345").args, "напомни в магазине 12345"),  # no letters at all
        (gcall("магазин‮", "купить молоко").args, ASK),
        (gcall("магазин", "купить​молоко").args, ASK),
        (gcall("м" * 61, "купить молоко").args, ASK),
        (gcall("магазин", "купить молоко " * 20).args, ASK),
        ({"place": 5, "content": "купить молоко"}, ASK),
        ({"place": "магазин"}, ASK),
        ({}, ASK),
    ],
)
def test_a_note_that_is_not_the_users_own_words_is_dropped(args, said):
    assert parse_note(args, said) is None


def test_the_callback_data_round_trip_and_fit_telegram():
    nid = uuid.uuid4()
    data = geo.note_data(nid)
    assert len(data.encode()) <= 64 and geo.parse_note_data(data) == nid
    for bad in ("", "gn:", f"gn:{nid}", f"gn:{nid}:x", f"gs:{nid}:d", "gn:not-a-uuid:d", f"gn:{nid}:d:x"):
        assert geo.parse_note_data(bad) is None
    place = geo.place_data("магазин", Decimal("-55.676100"), Decimal("-179.999999"))
    assert len(place.encode()) <= 64 and geo.parse_place_data(place) == (geo.name_hash("магазин"), Decimal("-55.676100"), Decimal("-179.999999"))
    for bad in ("", "gs:", "gs:zz:1:2", f"gs:{geo.name_hash('а')}:1", f"gs:{geo.name_hash('а')}:99999999999:1", f"gs:{geo.name_hash('а')}:100000000:1", f"gs:{geo.name_hash('а')}:1:200000000"):
        assert geo.parse_place_data(bad) is None
    assert geo.name_hash("Магазин") == geo.name_hash("  магазин! ")  # the hash is of the normal form


def test_the_location_card_in_each_of_its_four_forms():
    lat, lon = SHOP
    views = (geo.NoteView(uuid.uuid4(), "купить молоко"), geo.NoteView(uuid.uuid4(), "взять пакет"))
    text, markup = geo.location_card(geo.Located("магазин", views, 0, ()), lat, lon)
    assert text == "📍 Ты рядом с магазин:\n1. купить молоко\n2. взять пакет"
    assert [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] == [("✅ Выполнено: 1", geo.note_data(views[0].id)), ("✅ Выполнено: 2", geo.note_data(views[1].id))]
    assert geo.location_card(geo.Located("магазин", views[:1], 3, ()), lat, lon)[0].endswith("1. купить молоко\n…и ещё 3")
    assert geo.location_card(geo.Located("магазин", (), 0, ()), lat, lon) == ("📍 магазин", None)
    text, markup = geo.location_card(geo.Located(None, views[:1], 0, ()), lat, lon)
    assert text == "📍 Рядом есть заметки:\n1. купить молоко" and markup is not None
    text, markup = geo.location_card(geo.Located(None, (), 0, ("магазин", "аптека", "банк", "почта")), lat, lon)
    assert text == texts.GEO_UNKNOWN and [b.text for row in markup.inline_keyboard for b in row] == ["💾 Это «магазин»", "💾 Это «аптека»", "💾 Это «банк»"]  # at most 3
    assert geo.location_card(geo.Located(None, (), 0, ()), lat, lon) == (texts.GEO_UNKNOWN, None)


# ================================================================== fixtures and helpers


@pytest_asyncio.fixture
async def geo_env(owner_engine, db):
    """``bot_env`` with the geo features on, as in production (the function is still offered only on a cue)."""
    env = await build_bot_env(owner_engine, db, geo_enabled=True)
    yield env
    await env.bot.session.close()


def engine_of(env, **settings_kw: Any) -> Geo:
    return env.orchestrator.geo if not settings_kw else Geo(env.db, env.bot, Settings(_env_file=None, **settings_kw))


async def rows(env, sql: str, **kw: Any):
    return await owner_exec(env.owner_engine, sql, u=env.user_id, **kw)


async def place(env, name: str = "магазин", point: tuple[Decimal, Decimal] = SHOP, radius: int = 150, aliases: list[str] | None = None, user: uuid.UUID | None = None) -> uuid.UUID:
    [(pid,)] = await owner_exec(
        env.owner_engine,
        "INSERT INTO civ.places (user_id, name, latitude, longitude, radius_meters, aliases) VALUES (:u, :n, :la, :lo, :r, :a) RETURNING id",
        u=user or env.user_id, n=name, la=point[0], lo=point[1], r=radius, a=aliases or [],
    )  # fmt: skip
    return pid


async def note(
    env,
    content: str = "купить молоко",
    *,
    place_id: uuid.UUID | None = None,
    place_name: str | None = None,
    point: tuple[Decimal, Decimal] | None = None,
    status: str = "active",
    user: uuid.UUID | None = None,
) -> uuid.UUID:
    if place_id is None and place_name is None and point is None:
        place_name = "магазин"
    [(nid,)] = await owner_exec(
        env.owner_engine,
        "INSERT INTO civ.geo_notes (user_id, place_id, place_name, latitude, longitude, content, status, archived_at) "
        "VALUES (:u, :p, :pn, :la, :lo, :c, :s, CASE WHEN :s = 'archived' THEN now() END) RETURNING id",
        u=user or env.user_id, p=place_id, pn=place_name, la=None if point is None else point[0], lo=None if point is None else point[1], c=content, s=status,
    )  # fmt: skip
    return nid


async def note_rows(env) -> list[tuple[Any, ...]]:
    return await rows(env, "SELECT content, place_id IS NOT NULL, place_name, status, notified_at IS NOT NULL FROM civ.geo_notes WHERE user_id = :u ORDER BY created_at, content")


async def save(env, said: str, *calls: ToolCall, **settings_kw: Any):
    message_id = await new_message(env.owner_engine, env.user_id, said)
    return await engine_of(env, **settings_kw).save_notes(env.user_id, UserTurn(message_id, said), calls)


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


def last_card(env, thread_id: int | None = None) -> Any:
    return [m for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id][-1]


async def send_location(env, point: tuple[Decimal, Decimal], *, thread_id: int | None = None) -> None:
    await env.feed_update(
        make_location_update(latitude=float(point[0]), longitude=float(point[1]), user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=env.next_message_id(), thread_id=thread_id)
    )


async def send_reply_command(env, text: str, point: tuple[Decimal, Decimal], *, thread_id: int | None = None) -> None:
    await env.feed_update(
        make_reply_to_location_update(
            text=text, latitude=float(point[0]), longitude=float(point[1]), user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=env.next_message_id() + 1, thread_id=thread_id
        )
    )


async def press(env, data: str, *, tg: int | None = None, thread_id: int | None = None) -> None:
    await env.feed_update(make_callback_update(data=data, user_id=tg or env.telegram_user_id, chat_id=env.chat_id, message_id=777, thread_id=thread_id))


def model_saves(env, *calls: ToolCall, text: str = "Записал, напомню, как только придёшь!", then: str = "Договорились.") -> None:
    """The fake model calls the function - and, as a real one might, ALSO promises something the bot cannot do."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


# ================================================================== the database


async def test_places_and_notes_belong_to_one_tenant_and_the_others_cannot_see_them(geo_env, owner_engine, db):
    pid = await place(geo_env)
    await note(geo_env, place_id=pid, place_name="магазин")
    stranger = await new_user(owner_engine)
    for table in ("places", "geo_notes"):
        assert await tenant_exec(db, stranger, f"SELECT count(*) FROM civ.{table}") == [(0,)]
    assert await tenant_exec(db, geo_env.user_id, "SELECT count(*) FROM civ.geo_notes") == [(1,)]


async def test_the_composite_foreign_keys_refuse_another_tenants_rows(geo_env, owner_engine, db):
    mine = await place(geo_env)
    stranger = await new_user(owner_engine)
    with pytest.raises(DB_ERRORS):  # the stranger's note on MY place
        await tenant_exec(db, stranger, "INSERT INTO civ.geo_notes (user_id, place_id, place_name, content) VALUES (:u, :p, 'х', 'чужое')", u=stranger, p=mine)
    message = await new_message(owner_engine, geo_env.user_id, "моё")
    with pytest.raises(DB_ERRORS):  # a note that points at MY message
        await tenant_exec(db, stranger, "INSERT INTO civ.geo_notes (user_id, place_name, content, source_message_id) VALUES (:u, 'х', 'чужое', :m)", u=stranger, m=message)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO civ.places (user_id, name, latitude, longitude) VALUES (:u, 'Х', 90.5, 0)",
        "INSERT INTO civ.places (user_id, name, latitude, longitude) VALUES (:u, 'Х', 0, -180.5)",
        "INSERT INTO civ.places (user_id, name, latitude, longitude, radius_meters) VALUES (:u, 'Х', 0, 0, 9)",
        "INSERT INTO civ.places (user_id, name, latitude, longitude, radius_meters) VALUES (:u, 'Х', 0, 0, 5001)",
        "INSERT INTO civ.places (user_id, name, latitude, longitude) VALUES (:u, E'два\\nряда', 0, 0)",
        "INSERT INTO civ.places (user_id, name, latitude, longitude) VALUES (:u, repeat('я', 81), 0, 0)",
        "INSERT INTO civ.places (user_id, name, latitude, longitude, aliases) VALUES (:u, 'Х', 0, 0, ARRAY['ok', E'bad\\nalias'])",
        "INSERT INTO civ.places (user_id, name, latitude, longitude) VALUES (:u, 'Х', NULL, 0)",
        "INSERT INTO civ.geo_notes (user_id, place_name, content) VALUES (:u, 'х', '')",
        "INSERT INTO civ.geo_notes (user_id, place_name, content) VALUES (:u, 'х', repeat('я', 501))",
        "INSERT INTO civ.geo_notes (user_id, place_name, content) VALUES (:u, 'х', E'два\\x07звонка')",
        "INSERT INTO civ.geo_notes (user_id, place_name, content, status) VALUES (:u, 'х', 'ок', 'deleted')",
        "INSERT INTO civ.geo_notes (user_id, content) VALUES (:u, 'нигде')",  # no anchor at all
        "INSERT INTO civ.geo_notes (user_id, latitude, content) VALUES (:u, 55.5, 'половина точки')",
        "INSERT INTO civ.geo_notes (user_id, latitude, longitude, content) VALUES (:u, 95, 10, 'вне мира')",
        "INSERT INTO civ.geo_notes (user_id, place_name, content, status) VALUES (:u, 'х', 'ок', 'archived')",  # archived without archived_at
        "INSERT INTO civ.geo_notes (user_id, place_name, content, archived_at) VALUES (:u, 'х', 'ок', now())",  # archived_at without archived
        "INSERT INTO civ.geo_notes (user_id, place_name, content) VALUES (:u, E'два\\nряда', 'ок')",
    ],
)
async def test_the_checks_refuse_nonsense(geo_env, db, sql):
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, geo_env.user_id, sql, u=geo_env.user_id)


async def test_a_place_name_is_unique_per_tenant(geo_env, owner_engine):
    await place(geo_env)
    with pytest.raises(DB_ERRORS):
        await place(geo_env)
    other = await new_user(owner_engine)
    await place(geo_env, user=other)  # another tenant may use the same name


async def test_a_note_survives_the_deletion_of_its_place_and_waits_by_name(geo_env, db):
    pid = await place(geo_env)
    await note(geo_env, place_id=pid, place_name="магазин")
    await tenant_exec(db, geo_env.user_id, "DELETE FROM civ.places WHERE id = :p", p=pid)
    assert await rows(geo_env, "SELECT place_id IS NULL, place_name FROM civ.geo_notes WHERE user_id = :u") == [(True, "магазин")]


# ================================================================== the tool: saving a note


async def test_a_note_for_an_unknown_place_waits_under_the_name(geo_env):
    [saved] = await save(geo_env, ASK, gcall())
    assert (saved.place, saved.content, saved.bound) == ("магазин", "купить молоко", False)
    assert await note_rows(geo_env) == [("купить молоко", False, "магазин", "active", False)]
    text = geo.saved_text(saved)
    assert text.startswith("📍 Запомнил для «магазин»: купить молоко. Покажу, когда пришлёшь геопозицию рядом.") and "/place магазин" in text


async def test_a_note_for_a_known_place_is_bound_by_name_synonym_and_inflection(geo_env):
    pid = await place(geo_env, "Магазин", aliases=["супермаркет", "продуктовый"])
    for place_word, said in (("магазин", "напомни в магазине купить молоко"), ("супермаркет", "напомни в супермаркете купить хлеб"), ("Магазина", "напомни у магазина купить сыр")):
        content = {"магазин": "купить молоко", "супермаркет": "купить хлеб", "Магазина": "купить сыр"}[place_word]
        [saved] = await save(geo_env, said, gcall(place_word, content))
        assert saved.bound and saved.place == "Магазин"
    assert {r[1] for r in await note_rows(geo_env)} == {True} and await rows(geo_env, "SELECT DISTINCT place_id FROM civ.geo_notes WHERE user_id = :u") == [(pid,)]
    assert geo.saved_text(saved) == "📍 Запомнил для «Магазин»: купить сыр. Покажу, когда пришлёшь геопозицию рядом."


async def test_the_same_note_is_not_saved_twice(geo_env):
    assert len(await save(geo_env, ASK, gcall())) == 1
    assert await save(geo_env, "напомни в магазине купить молоко ещё раз", gcall()) == []
    assert len(await note_rows(geo_env)) == 1
    assert len(await save(geo_env, "напомни в аптеке купить молоко", gcall("аптека", "купить молоко"))) == 1  # the same words for another place are another note


async def test_a_replayed_message_gets_its_own_notes_back_and_never_a_second_copy(geo_env):
    message_id = await new_message(geo_env.owner_engine, geo_env.user_id, ASK)
    turn = UserTurn(message_id, ASK)
    first = await engine_of(geo_env).save_notes(geo_env.user_id, turn, [gcall()])
    second = await engine_of(geo_env).save_notes(geo_env.user_id, turn, [gcall()])
    assert len(first) == 1 and second == first and len(await note_rows(geo_env)) == 1


async def test_unusable_and_foreign_calls_save_nothing(geo_env):
    assert await save(geo_env, ASK, gcall("аптека")) == []
    assert await save(geo_env, ASK, ToolCall("create_event", gcall().args)) == []
    assert await note_rows(geo_env) == []


async def test_several_notes_of_one_message_are_capped(geo_env):
    said = "напомни в магазине купить молоко, в аптеке взять витамины, в банке оплатить счёт, на почте отправить посылку"
    calls = [gcall("магазин", "купить молоко"), gcall("аптека", "взять витамины"), gcall("банк", "оплатить счёт"), gcall("почта", "отправить посылку")]
    assert len(await save(geo_env, said, *calls)) == 3  # GEO_MAX_CALLS


async def test_the_active_note_cap_stops_new_notes(geo_env):
    await note(geo_env, "старое")
    assert await save(geo_env, ASK, gcall(), geo_max_notes=1) == []
    await rows(geo_env, "UPDATE civ.geo_notes SET status = 'archived', archived_at = now() WHERE user_id = :u")
    assert len(await save(geo_env, ASK, gcall(), geo_max_notes=1)) == 1  # an archived note does not count


async def test_a_database_failure_saves_nothing_and_never_raises(geo_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Geo, "_save_notes", boom)
    assert await save(geo_env, ASK, gcall()) == [] and await note_rows(geo_env) == []


# ================================================================== a sent location


async def locate(env, point: tuple[Decimal, Decimal], **settings_kw: Any) -> geo.Located:
    return await engine_of(env, **settings_kw).locate(env.user_id, *point)


async def test_inside_the_radius_the_notes_of_the_place_are_listed_and_marked_as_shown(geo_env):
    pid = await place(geo_env)
    await note(geo_env, "купить молоко", place_id=pid, place_name="магазин")
    await note(geo_env, "взять пакет", place_id=pid, place_name="магазин")
    located = await locate(geo_env, near(north=NORTH_100M))
    assert located.place == "магазин" and [n.content for n in located.notes] == ["купить молоко", "взять пакет"] and located.more == 0 and located.pending == ()
    assert {r[4] for r in await note_rows(geo_env)} == {True}  # notified_at


async def test_outside_the_radius_it_is_not_there_and_the_boundary_follows_the_radius(geo_env):
    pid = await place(geo_env, radius=150)
    await note(geo_env, place_id=pid, place_name="магазин")
    assert (await locate(geo_env, near(north=Decimal("0.0013")))).place == "магазин"  # ~145 m
    far = await locate(geo_env, near(north=Decimal("0.0017")))  # ~189 m
    assert far.place is None and far.notes == ()
    await rows(geo_env, "UPDATE civ.places SET radius_meters = 300 WHERE user_id = :u")
    assert (await locate(geo_env, near(north=Decimal("0.0017")))).place == "магазин"


async def test_with_overlapping_places_the_nearest_one_wins(geo_env):
    a = await place(geo_env, "магазин", radius=500)
    b = await place(geo_env, "аптека", point=near(north=Decimal("0.0030")), radius=500)
    await note(geo_env, "в магазине", place_id=a, place_name="магазин")
    await note(geo_env, "в аптеке", place_id=b, place_name="аптека")
    located = await locate(geo_env, near(north=Decimal("0.0028")))  # closer to the pharmacy
    assert located.place == "аптека" and [n.content for n in located.notes] == ["в аптеке"]


async def test_the_notes_of_other_places_and_archived_ones_are_not_shown(geo_env):
    a = await place(geo_env, "магазин")
    b = await place(geo_env, "зал", point=(Decimal("48.858400"), Decimal("2.294500")))
    await note(geo_env, "здесь", place_id=a, place_name="магазин")
    await note(geo_env, "там", place_id=b, place_name="зал")
    await note(geo_env, "сделано", place_id=a, place_name="магазин", status="archived")
    assert [n.content for n in (await locate(geo_env, SHOP)).notes] == ["здесь"]


async def test_a_place_without_notes_is_just_named(geo_env):
    await place(geo_env, "дом")
    located = await locate(geo_env, SHOP)
    assert (located.place, located.notes) == ("дом", ())
    assert geo.location_card(located, *SHOP) == ("📍 дом", None)


async def test_a_note_made_on_a_point_is_shown_near_the_point_even_without_a_place(geo_env):
    await note(geo_env, "проверить дверь", point=SHOP)
    close = await locate(geo_env, near(north=NORTH_100M))
    assert close.place is None and [n.content for n in close.notes] == ["проверить дверь"]
    assert (await locate(geo_env, near(north=Decimal("0.0020")))).notes == ()  # ~222 m: outside GEO_NOTE_RADIUS_METERS (150)


async def test_a_waiting_note_is_bound_the_first_time_its_place_is_met(geo_env):
    await note(geo_env, "купить молоко", place_name="Магазине")  # saved by a name the place did not exist under yet
    pid = await place(geo_env, "магазин")
    located = await locate(geo_env, SHOP)
    assert located.place == "магазин" and [n.content for n in located.notes] == ["купить молоко"]
    assert await rows(geo_env, "SELECT place_id FROM civ.geo_notes WHERE user_id = :u") == [(pid,)]


async def test_a_note_waiting_for_another_place_is_not_bound_here(geo_env):
    await place(geo_env, "зал")
    await note(geo_env, "купить молоко", place_name="магазин")
    located = await locate(geo_env, SHOP)
    assert located.place == "зал" and located.notes == () and await rows(geo_env, "SELECT place_id IS NULL FROM civ.geo_notes WHERE user_id = :u") == [(True,)]


async def test_an_unknown_location_lists_the_names_notes_are_waiting_for(geo_env):
    await note(geo_env, "купить молоко", place_name="магазин")
    await note(geo_env, "взять хлеб", place_name="Магазине")  # the same place by another case: one name
    await note(geo_env, "забрать документы", place_name="офис")
    located = await locate(geo_env, SHOP)
    assert located.place is None and located.notes == () and sorted(n.lower() for n in located.pending) == ["магазин", "офис"]


async def test_the_listing_is_capped_and_only_the_shown_notes_are_marked(geo_env):
    pid = await place(geo_env)
    for n in range(5):
        await note(geo_env, f"дело {n}", place_id=pid, place_name="магазин")
    located = await locate(geo_env, SHOP, geo_max_listed=3)
    assert len(located.notes) == 3 and located.more == 2
    assert [r[4] for r in await note_rows(geo_env)].count(True) == 3


async def test_another_tenants_places_and_notes_never_leak(geo_env, owner_engine):
    stranger = await new_user(owner_engine)
    pid = await place(geo_env, "магазин", user=stranger)
    await note(geo_env, "чужое", place_id=pid, place_name="магазин", user=stranger)
    located = await locate(geo_env, SHOP)
    assert located == geo.Located(None, (), 0, ())


# ---------------------------------------------------------------- saving a place


async def test_a_place_is_saved_with_the_default_radius_and_binds_the_waiting_notes(geo_env):
    await note(geo_env, "купить молоко", place_name="магазин")
    await note(geo_env, "взять хлеб", place_name="магазине")
    await note(geo_env, "забрать документы", place_name="офис")
    result = await engine_of(geo_env).save_place(geo_env.user_id, "магазин", *SHOP)
    assert result == geo.PlaceSaved("created", "магазин", 150, 2)
    assert geo._place_reply(result) == "📍 Сохранил место «магазин» (радиус 150 м). Привязал заметок: 2."
    assert await rows(geo_env, "SELECT content, place_id IS NOT NULL FROM civ.geo_notes WHERE user_id = :u ORDER BY content") == [("взять хлеб", True), ("забрать документы", False), ("купить молоко", True)]
    assert await rows(geo_env, "SELECT latitude, longitude, radius_meters FROM civ.places WHERE user_id = :u") == [(SHOP[0], SHOP[1], 150)]


async def test_a_custom_radius_is_kept_and_a_wrong_one_falls_back_to_the_default(geo_env):
    assert (await engine_of(geo_env).save_place(geo_env.user_id, "зал", *SHOP, radius=300)).radius == 300
    assert (await engine_of(geo_env).save_place(geo_env.user_id, "офис", *SHOP, radius=5)).radius == 150
    assert (await engine_of(geo_env).save_place(geo_env.user_id, "дом", *SHOP, radius=99999)).radius == 150


async def test_a_place_that_is_already_saved_by_name_synonym_or_inflection_is_not_saved_again(geo_env):
    await place(geo_env, "Магазин", aliases=["супермаркет"])
    for name in ("магазин", "магазине", "Супермаркет"):
        assert (await engine_of(geo_env).save_place(geo_env.user_id, name, *SHOP)) == geo.PlaceSaved("exists", "Магазин")
    assert await rows(geo_env, "SELECT count(*) FROM civ.places WHERE user_id = :u") == [(1,)]


@pytest.mark.parametrize("name", ["", "   ", "12345", "я" * 61, None, 5, "зал‮"])
async def test_a_bad_place_name_is_refused(geo_env, name):
    assert (await engine_of(geo_env).save_place(geo_env.user_id, name, *SHOP)).outcome == "bad_name"
    assert await rows(geo_env, "SELECT count(*) FROM civ.places WHERE user_id = :u") == [(0,)]


async def test_the_place_cap_stops_new_places(geo_env):
    await place(geo_env, "дом")
    assert (await engine_of(geo_env, geo_max_places=1).save_place(geo_env.user_id, "зал", *SHOP)).outcome == "limit"


async def test_the_button_under_a_waiting_name_saves_that_place_at_the_point_of_the_location(geo_env):
    await note(geo_env, "купить молоко", place_name="магазин")
    data = geo.place_data("магазин", *SHOP)
    _, lat, lon = geo.parse_place_data(data)
    result = await engine_of(geo_env).save_waiting_place(geo_env.user_id, geo.name_hash("магазин"), lat, lon)
    assert result == geo.PlaceSaved("created", "магазин", 150, 1)
    assert await engine_of(geo_env).save_waiting_place(geo_env.user_id, geo.name_hash("нет такого"), lat, lon) is None


# ---------------------------------------------------------------- the button «✅ Выполнено»


async def test_done_archives_the_note_and_rebuilds_the_card_from_the_database(geo_env):
    pid = await place(geo_env)
    first = await note(geo_env, "купить молоко", place_id=pid, place_name="магазин")
    await note(geo_env, "взять пакет", place_id=pid, place_name="магазин")
    result = await engine_of(geo_env).done(geo_env.user_id, first)
    assert result.outcome == "done" and result.text == "📍 Ты рядом с магазин:\n1. взять пакет"
    assert [b.text for row in result.markup.inline_keyboard for b in row] == ["✅ Выполнено: 1"]
    assert await rows(geo_env, "SELECT status, archived_at IS NOT NULL FROM civ.geo_notes WHERE id = :i", i=first) == [("archived", True)]


async def test_closing_the_last_note_says_everything_is_done(geo_env):
    pid = await place(geo_env)
    only = await note(geo_env, place_id=pid, place_name="магазин")
    result = await engine_of(geo_env).done(geo_env.user_id, only)
    assert (result.outcome, result.text, result.markup) == ("done", "📍 магазин — всё выполнено ✅", None)


async def test_done_is_idempotent_and_six_simultaneous_presses_archive_once(geo_env):
    pid = await place(geo_env)
    nid = await note(geo_env, place_id=pid, place_name="магазин")
    results = await asyncio.gather(*(engine_of(geo_env).done(geo_env.user_id, nid) for _ in range(6)))
    assert sorted(r.outcome for r in results) == ["done"] + ["settled"] * 5


async def test_a_note_on_a_point_is_closed_and_its_card_rebuilt_around_the_point(geo_env):
    a = await note(geo_env, "первое", point=SHOP)
    await note(geo_env, "второе", point=near(north=NORTH_100M))
    result = await engine_of(geo_env).done(geo_env.user_id, a)
    assert result.text == "📍 Рядом есть заметки:\n1. второе"


async def test_another_tenants_note_and_an_unknown_one_are_gone(geo_env, owner_engine):
    stranger = await new_user(owner_engine)
    pid = await place(geo_env, user=stranger)
    theirs = await note(geo_env, place_id=pid, place_name="магазин", user=stranger)
    assert (await engine_of(geo_env).done(geo_env.user_id, theirs)).outcome == "gone"
    assert (await engine_of(geo_env).done(geo_env.user_id, uuid.uuid4())).outcome == "gone"
    assert await owner_exec(owner_engine, "SELECT status FROM civ.geo_notes WHERE id = :i", i=theirs) == [("active",)]


# ================================================================== the whole flow through the Dispatcher


async def test_a_reminder_for_a_place_gets_the_function_and_a_card_that_promises_only_what_the_bot_can_do(geo_env):
    env = geo_env
    model_saves(env, gcall())
    await env.feed(ASK)
    offered = env.chat.tool_calls[0]
    assert [t.name for t in offered["tools"]] == ["save_geo_note"] and "## Place notes" in offered["system"]
    assert not any("напомню" in t.lower() for t in says(env))  # the model's own promise next to the call is never sent
    assert "Договорились." in says(env)
    assert "Place notes (made by the system)" in env.chat.calls[-1]["system"]
    card = last_card(env).text
    assert card.startswith("📍 Запомнил для «магазин»: купить молоко. Покажу, когда пришлёшь геопозицию рядом.") and "/place магазин" in card
    assert await note_rows(env) == [("купить молоко", False, "магазин", "active", False)]
    assert not any("📍" in (m["content"] or "") for m in await env.messages())  # the card is not a word of the assistant: not remembered as one


async def test_the_whole_cycle_note_location_save_place_location_done(geo_env):
    env = geo_env
    model_saves(env, gcall())
    await env.feed(ASK)
    await send_location(env, SHOP)  # the place is unknown yet: the card offers to save it under the name the note waits for
    card = last_card(env)
    assert card.text == texts.GEO_UNKNOWN and [b.text for row in card.reply_markup.inline_keyboard for b in row] == ["💾 Это «магазин»"]
    await press(env, card.reply_markup.inline_keyboard[0][0].callback_data)
    assert env.session.edits()[-1].text == "📍 Сохранил место «магазин» (радиус 150 м). Привязал заметок: 1."
    assert await rows(env, "SELECT name, radius_meters FROM civ.places WHERE user_id = :u") == [("магазин", 150)]
    await send_location(env, near(north=NORTH_100M))
    card = last_card(env)
    assert card.text == "📍 Ты рядом с магазин:\n1. купить молоко"
    [[button]] = card.reply_markup.inline_keyboard
    assert button.text == "✅ Выполнено: 1"
    await press(env, button.callback_data)
    assert env.session.edits()[-1].text == "📍 магазин — всё выполнено ✅" and env.session.callback_answers()[-1].text == texts.GEO_TOAST["done"]
    assert await note_rows(env) == [("купить молоко", True, "магазин", "archived", True)]
    await press(env, button.callback_data)  # a second press
    assert env.session.callback_answers()[-1].text == texts.GEO_TOAST["settled"]
    await send_location(env, SHOP)
    assert last_card(env).text == "📍 магазин"


async def test_a_note_for_a_saved_place_is_shown_on_the_first_location(geo_env):
    env = geo_env
    await place(env, "зал", point=(Decimal("48.858400"), Decimal("2.294500")))
    model_saves(env, gcall("зал", "проверить вес на жиме"))
    await env.feed("Заметка для зала: проверить вес на жиме")
    assert "/place" not in last_card(env).text  # the place is known: nothing to ask
    await send_location(env, (Decimal("48.858500"), Decimal("2.294600")))
    assert last_card(env).text == "📍 Ты рядом с зал:\n1. проверить вес на жиме"


async def test_the_place_command_as_a_reply_to_a_location_saves_the_place(geo_env):
    env = geo_env
    await send_reply_command(env, "/place зал", SHOP)
    assert says(env)[-1] == "📍 Сохранил место «зал» (радиус 150 м)."
    await send_reply_command(env, "/place Зал", SHOP)
    assert says(env)[-1] == "Место «зал» уже сохранено."
    await send_reply_command(env, "/place офис 300 м", SHOP)
    assert says(env)[-1] == "📍 Сохранил место «офис» (радиус 300 м)." and await rows(env, "SELECT name, radius_meters FROM civ.places WHERE user_id = :u ORDER BY name") == [("зал", 150), ("офис", 300)]


async def test_the_place_command_needs_a_location_to_reply_to_and_a_name(geo_env):
    env = geo_env
    await env.feed("/place зал")  # not a reply to a location
    assert says(env)[-1] == texts.GEO_NEED_REPLY
    await send_reply_command(env, "/place", SHOP)
    assert says(env)[-1] == texts.GEO_BAD_NAME and await rows(env, "SELECT count(*) FROM civ.places WHERE user_id = :u") == [(0,)]


async def test_the_geonote_command_makes_a_note_on_the_point(geo_env):
    env = geo_env
    await send_reply_command(env, "/geonote проверить дверь", SHOP)
    assert says(env)[-1] == "📍 Запомнил заметку на этой точке: проверить дверь"
    await send_location(env, near(north=NORTH_100M))
    assert last_card(env).text == "📍 Рядом есть заметки:\n1. проверить дверь"
    await send_reply_command(env, "/geonote", SHOP)
    assert says(env)[-1] == texts.GEO_BAD_NOTE
    await env.feed("/geonote проверить дверь")
    assert says(env)[-1].startswith("Ответь этой командой на сообщение с геопозицией")


async def test_a_location_in_a_topic_is_answered_in_that_topic(geo_env):
    env = geo_env
    await place(env, "дом")
    await send_location(env, SHOP, thread_id=55)
    assert [m.text for m in env.session.sent() if m.message_thread_id == 55] == ["📍 дом"] and says(env) == []


async def test_a_location_is_never_stored_as_a_message(geo_env):
    env = geo_env
    await send_location(env, SHOP)
    assert await env.messages() == []
    assert await rows(env, "SELECT count(*) FROM civ.places WHERE user_id = :u") == [(0,)]  # and a bare location creates no place


async def test_the_switch_turns_everything_off(owner_engine, db):
    env = await build_bot_env(owner_engine, db, geo_enabled=False)
    try:
        env.chat.responder = lambda system, turns: "Хорошо."
        await env.feed(ASK)
        assert env.chat.tool_calls == []
        before = len(env.session.sent())
        await send_location(env, SHOP)
        await send_reply_command(env, "/place зал", SHOP)
        assert len(env.session.sent()) == before and await rows(env, "SELECT count(*) FROM civ.places WHERE user_id = :u") == [(0,)]
    finally:
        await env.bot.session.close()


async def test_before_the_end_of_the_interview_a_location_waits(owner_engine, db):
    env = await build_bot_env(owner_engine, db, onboarding_state="pending", geo_enabled=True)
    try:
        await send_location(env, SHOP)
        assert says(env) == [texts.GEO_LATER]
    finally:
        await env.bot.session.close()


async def test_a_stranger_cannot_press_my_buttons(geo_env, owner_engine):
    env = geo_env
    pid = await place(env)
    nid = await note(env, place_id=pid, place_name="магазин")
    stranger = random.randrange(10**9, 10**12)  # fresh ids: other tests leave tenants with fixed ones behind
    await new_user(owner_engine, telegram_user_id=stranger, forum_chat_id=-int(f"100{random.randrange(10**9, 10**10)}"))
    await press(env, geo.note_data(nid), tg=stranger)  # an unknown chat for that tenant: the middleware drops it before any handler
    assert await rows(env, "SELECT status FROM civ.geo_notes WHERE id = :i", i=nid) == [("active",)]


async def test_garbage_buttons_change_nothing(geo_env):
    env = geo_env
    await press(env, "gn:not-a-uuid:d")
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_UNKNOWN_BUTTON
    await press(env, f"gn:{uuid.uuid4()}:d")
    assert env.session.callback_answers()[-1].text == texts.GEO_TOAST["gone"]
    await press(env, "gs:zzzz:1:2")
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_UNKNOWN_BUTTON
    await press(env, geo.place_data("нет такого", *SHOP))
    assert env.session.callback_answers()[-1].text == texts.GEO_TOAST["gone"] and await rows(env, "SELECT count(*) FROM civ.places WHERE user_id = :u") == [(0,)]


async def test_a_reminder_with_an_invented_word_gives_no_card_and_the_models_promise_is_not_sent(geo_env):
    env = geo_env
    model_saves(env, gcall(content="купить шоколадные конфеты"), then="Не получилось записать.")
    await env.feed(ASK)
    assert await note_rows(env) == [] and not any("📍" in t for t in says(env)) and not any("напомню" in t.lower() for t in says(env))


async def test_talk_without_a_cue_is_never_offered_the_function(geo_env):
    env = geo_env
    env.chat.responder = lambda system, turns: "Понял."
    for said in ("Напомни в 10 позвонить маме", "Купил молоко в магазине", "Хочу в магазин"):
        await env.feed(said)
    assert env.chat.tool_calls == [] and all("## Place notes" not in c["system"] for c in env.chat.calls)


async def test_a_voice_reminder_is_saved_like_a_typed_one(geo_env):
    env = geo_env
    env.chat.transcriber = lambda data, mime: ASK
    model_saves(env, gcall())
    await env.feed_voice()
    assert len(await note_rows(env)) == 1


async def test_a_redelivered_update_saves_the_note_once(geo_env):
    env = geo_env
    model_saves(env, gcall())
    update = env.update(ASK)
    await env.feed_update(update)
    await env.feed_update(update)
    assert len(await note_rows(env)) == 1


async def test_a_database_failure_still_sends_the_reply_without_a_card(geo_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Geo, "_save_notes", boom)
    env = geo_env
    model_saves(env, gcall(), then="Не получилось, повторите.")
    await env.feed(ASK)
    assert "Не получилось, повторите." in says(env) and not any("📍" in t for t in says(env))


async def test_a_database_failure_on_a_location_says_so_and_changes_nothing(geo_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Geo, "locate", boom)
    await send_location(geo_env, SHOP)
    assert says(geo_env) == [texts.FAILURE]


def test_the_function_name_is_unique_in_the_production_registry(geo_env):
    names = [name for tool in geo_env.orchestrator.tools.tools for name in tool.functions]
    assert names.count("save_geo_note") == 1 and len(names) == len(set(names))
    assert math.isclose(haversine_m(0, 0, 0, 0), 0.0)

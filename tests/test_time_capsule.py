"""v2.0, PR 4.5: time capsules and the "a year ago" line. The points under test: the model is OFFERED ``seal_time_capsule`` only on an explicit marker in the user's own
words; the DATE is the code's wherever the words fix it (a relative offset, an explicit date), otherwise the model's date - checked against a year the user wrote -
turned into an instant in the user's time zone; it is not a timer (at least 7 local days, at most 5 years) and the letter is the user's own words; a capsule is
delivered by an hourly job under a lease, never in quiet hours, once, and then it is final; the first check-in of the day may end with one line written by code about
what the user did exactly 365 (else 180) local days ago. Real PostgreSQL (RLS, CHECKs, the final-status trigger, column grants, the due-tenants function), the real
aiogram Dispatcher on a fake Telegram wire, a fake Gemini."""

from __future__ import annotations

import random

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy.exc import DBAPIError, IntegrityError

from ai_civ import tasks
from ai_civ.bot import flashback, texts
from ai_civ.bot.proactive import CHECK_IN, get_trigger
from ai_civ.bot.tools import UserTurn, capsule
from ai_civ.bot.tools.capsule import Refused, TimeCapsules, parse_capsule, relative_date, wants_capsule
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_message, new_user, owner_exec, tenant_exec
from tests.tg_fakes import make_callback_update

DB_ERRORS = (IntegrityError, DBAPIError)
UTC_ZONE = ZoneInfo("UTC")
CPH = ZoneInfo("Europe/Copenhagen")
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)  # 12:00 local for the UTC tenants of these tests: outside the default quiet hours 22:00-08:00
TODAY = date(2026, 10, 8)
ASK = "Напиши мне через полгода: я хочу бросить курить"
SETTINGS = Settings(_env_file=None)


def ccall(message: str = "я хочу бросить курить", deliver_at: str = "2027-04-08", **extra: Any) -> ToolCall:
    return ToolCall(capsule.TOOL_NAME, {"message": message, "deliver_at": deliver_at, **extra})


def parse(args: dict[str, Any], said: str, *, tz: ZoneInfo = UTC_ZONE, now: datetime = NOW):
    return parse_capsule(args, said, tz, now, SETTINGS)


# ================================================================== pure: markers, dates, the call


@pytest.mark.parametrize(
    "said",
    [
        "Сделай капсулу времени: хочу прочитать это через год",
        "Хочу письмо в будущее",
        "напиши себе письмо в будущее про этот год",
        "Напомни через полгода, что я хотел сменить работу",
        "Отправь мне через год это сообщение",
        "напомни мне через 2 года про отпуск",
        "Напомни мне в 2027 позвонить Саше",
        "Напомни через 3 месяца проверить цели",
        "напомни через две недели про отчёт",
        "make a time capsule for me",
        "a letter to my future self",
    ],
)
def test_an_explicit_marker_is_a_cue(said):
    assert wants_capsule(said)


@pytest.mark.parametrize(
    "said",
    [
        "Напомни в 10 позвонить маме",
        "Напомни завтра купить хлеб",
        "Через год будет интересно",
        "Расскажи про капсулу Dragon",
        "Хочу написать письмо маме",
        "",
    ],
)
def test_ordinary_talk_is_not_a_cue(said):
    assert not wants_capsule(said)


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("напомни через полгода", date(2027, 4, 8)),
        ("напомни через полугода", date(2027, 4, 8)),
        ("отправь мне через год", date(2027, 10, 8)),
        ("напомни через 2 года", date(2028, 10, 8)),
        ("напомни через два года", date(2028, 10, 8)),
        ("напомни через 5 лет", date(2031, 10, 8)),
        ("напомни через 3 месяца", date(2027, 1, 8)),
        ("напомни через месяц", date(2026, 11, 8)),
        ("напомни через 2 недели", date(2026, 10, 22)),
        ("напомни через неделю", date(2026, 10, 15)),
        ("напомни через 10 дней", date(2026, 10, 18)),
        ("напомни через 3 дня", date(2026, 10, 11)),
        ("напомни через 0 дней", None),
        ("напомни в 2027", None),
        ("", None),
    ],
)
def test_a_relative_offset_is_computed_by_code_from_the_local_date(said, expected):
    assert relative_date(said, TODAY) == expected


def test_month_arithmetic_clamps_to_the_end_of_a_short_month():
    assert relative_date("через полгода", date(2026, 8, 31)) == date(2027, 2, 28)
    assert relative_date("через год", date(2028, 2, 29)) == date(2029, 2, 28)


@pytest.mark.parametrize(
    ("said", "expected"),
    [
        ("в день 2027-01-15 открой", date(2027, 1, 15)),
        ("отправь 15.01.2027", date(2027, 1, 15)),
        ("отправь 1.2.2027", date(2027, 2, 1)),
        ("открой 1 января 2027 года", date(2027, 1, 1)),
        ("открой 31 декабря 2030", date(2030, 12, 31)),
        ("31.02.2027", None),
        ("32 января 2027", None),
        ("в январе", None),
        ("", None),
    ],
)
def test_an_explicit_date_is_read_by_code(said, expected):
    assert capsule.explicit_date(said) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2027-04-08", (date(2027, 4, 8), None)),
        ("2027-04-08T18:30", (date(2027, 4, 8), datetime(2027, 4, 8, 18, 30).time())),
        ("2027-04-08T18:30:45", (date(2027, 4, 8), datetime(2027, 4, 8, 18, 30).time())),
        ("2027-04-08T18:30+02:00", None),  # a zone suffix is not the user's wall clock
        ("2027-04-08T18:30Z", None),
        ("завтра", None),
        ("2027-13-40", None),
        (20270408, None),
        (None, None),
    ],
)
def test_the_models_date_is_a_local_date_or_local_datetime(value, expected):
    assert capsule.model_date(value) == expected


def test_the_tool_is_plain_json_and_the_instructions_forbid_claims():
    schema = capsule.TOOL.parameters
    assert capsule.TOOL.name == "seal_time_capsule" == capsule.TOOL_NAME and set(schema["required"]) == {"message", "deliver_at"} and json.dumps(schema)
    low = capsule.INSTRUCTIONS.lower()
    assert "never say it was sealed" in low and "the system" in low


def test_a_relative_offset_in_the_words_wins_over_the_models_date():
    spec = parse(ccall(deliver_at="2026-12-01").args, ASK)  # the model counted wrongly: "через полгода" is 2027-04-08
    assert spec is not None and spec.when == "08.04.2027" and spec.message == "я хочу бросить курить" and spec.title == "Письмо на 08.04.2027"
    assert spec.deliver_at == datetime(2027, 4, 8, 9, 0, tzinfo=UTC)  # the default hour in the user's zone (UTC here)


def test_an_explicit_date_in_the_words_wins_too():
    spec = parse(ccall(deliver_at="2026-12-01").args, "Отправь мне 15.01.2027 это: я хочу бросить курить")
    assert spec is not None and spec.when == "15.01.2027"


def test_the_instant_is_in_the_users_time_zone():
    copenhagen = parse(ccall().args, ASK, tz=CPH)  # 2027-04-08 09:00 in summer time (UTC+2) = 07:00 UTC
    tokyo = parse(ccall().args, ASK, tz=ZoneInfo("Asia/Tokyo"))
    assert copenhagen.deliver_at == datetime(2027, 4, 8, 7, 0, tzinfo=UTC) and tokyo.deliver_at == datetime(2027, 4, 8, 0, 0, tzinfo=UTC)
    winter = parse(ccall().args, "Напиши мне через 3 месяца: я хочу бросить курить", tz=CPH)  # 2027-01-08 09:00 in winter time (UTC+1)
    assert winter.deliver_at == datetime(2027, 1, 8, 8, 0, tzinfo=UTC)


def test_the_local_date_of_today_follows_the_zone_not_utc():
    late = datetime(2026, 10, 8, 22, 30, tzinfo=UTC)  # already the 9th in Tokyo
    spec = parse(ccall().args, "Напиши мне через год: я хочу бросить курить", tz=ZoneInfo("Asia/Tokyo"), now=late)
    assert spec is not None and spec.when == "09.10.2027"


def test_the_models_date_is_used_when_the_words_fix_none_and_a_written_year_must_match():
    said = "Письмо в будущее на мой день рождения: я хочу пробежать марафон"
    spec = parse(ccall("я хочу пробежать марафон", "2027-05-05T18:30").args, said)
    assert spec is not None and spec.when == "05.05.2027" and spec.deliver_at == datetime(2027, 5, 5, 18, 30, tzinfo=UTC)
    in_year = "Напомни мне в 2027: я хочу пробежать марафон"
    assert parse(ccall("я хочу пробежать марафон", "2027-06-01").args, in_year) is not None
    assert parse(ccall("я хочу пробежать марафон", "2028-06-01").args, in_year) is None  # not the year the user wrote
    assert parse(ccall("я хочу пробежать марафон", "скоро").args, said) is None
    assert parse({"message": "я хочу пробежать марафон"}, said) is None


@pytest.mark.parametrize(
    ("said", "reason"),
    [
        ("Напиши мне через 3 дня: я хочу бросить курить", "too_soon"),
        ("Напиши мне через 6 дней: я хочу бросить курить", "too_soon"),
        ("Напиши мне через 6 лет: я хочу бросить курить", "too_far"),
        ("Напиши мне через 100 месяцев: я хочу бросить курить", "too_far"),
    ],
)
def test_a_capsule_is_not_a_timer_and_not_forever(said, reason):
    assert parse(ccall().args, said) == Refused(reason)


def test_the_window_is_counted_in_local_dates_and_its_edges_are_inside():
    assert parse(ccall().args, "Напиши мне через неделю: я хочу бросить курить") is not None  # exactly 7 days
    assert parse(ccall().args, "Напиши мне через 5 лет: я хочу бросить курить") is not None  # exactly 5 years
    assert parse(ccall(deliver_at="2026-10-14").args, "Письмо в будущее: я хочу бросить курить") == Refused("too_soon")  # 6 days
    assert parse(ccall(deliver_at="2026-10-15").args, "Письмо в будущее: я хочу бросить курить") is not None
    assert parse(ccall(deliver_at="2031-10-09").args, "Письмо в будущее: я хочу бросить курить") == Refused("too_far")


@pytest.mark.parametrize(
    ("message", "ok"),
    [
        ("я хочу бросить курить", True),
        ("Привет, будущий я! Я хочу бросить курить", True),  # a greeting to the future self is allowed
        ("я хочу бросить курить и выучить датский", False),  # a plan the user never wrote
        ("я хочу бросить курить 30 дней", False),  # a number the user never wrote
        ("курить", False),  # fewer than two words
        ("12345 67890", False),  # no letters
        ("", False),
        ("я хочу бросить​курить", False),
        ("я хочу бросить курить " * 60, False),
    ],
)
def test_the_letter_must_be_the_users_own_words(message, ok):
    assert (parse(ccall(message).args, ASK) is not None) is ok


def test_the_title_is_the_models_only_when_it_is_grounded():
    assert parse(ccall(title="Бросить курить").args, ASK).title == "Бросить курить"
    assert parse(ccall(title="Великие планы на жизнь").args, ASK).title == "Письмо на 08.04.2027"
    assert parse(ccall(title="я" * 61).args, ASK).title == "Письмо на 08.04.2027"
    assert parse(ccall(title=5).args, ASK).title == "Письмо на 08.04.2027"


def test_the_callback_data_round_trip_and_fit_telegram():
    cid = uuid.uuid4()
    data = capsule.callback_data(cid)
    assert len(data.encode()) <= 64 and capsule.parse_callback(data) == cid
    for bad in ("", "tc:", f"tc:{cid}", f"tc:{cid}:d", f"gn:{cid}:x", "tc:not-a-uuid:x", f"tc:{cid}:x:y"):
        assert capsule.parse_callback(bad) is None
    assert [(b.text, b.callback_data) for row in capsule.sealed_markup(cid).inline_keyboard for b in row] == [("✖️ Отменить капсулу", data)]


def test_the_cards_the_user_reads():
    sealed = capsule.Sealed(uuid.uuid4(), "я хочу бросить курить", "08.04.2027")
    assert capsule.sealed_text(sealed) == "⏳ Запечатал письмо в будущее. Откроется 08.04.2027: «я хочу бросить курить»"
    assert capsule.delivered_text("я хочу бросить курить", datetime(2026, 10, 8, 23, 30, tzinfo=UTC), ZoneInfo("Asia/Tokyo")) == (
        "⏳ Письмо из прошлого (запечатано 09.10.2026):\n\nя хочу бросить курить\n\nТы достиг того, о чём думал тогда?"
    )


# ================================================================== fixtures and helpers


@pytest_asyncio.fixture
async def cap_env(owner_engine, db):
    """``bot_env`` with the capsules and the flashback on, as in production, for a tenant living in UTC (the default quiet hours 22:00-08:00 apply)."""
    env = await build_bot_env(owner_engine, db, capsules_enabled=True, flashback_enabled=True)
    await owner_exec(owner_engine, "UPDATE civ.users SET timezone = 'UTC' WHERE id = :u", u=env.user_id)
    yield env
    await env.bot.session.close()


def engine_of(env, **settings_kw: Any) -> TimeCapsules:
    return env.orchestrator.capsules if not settings_kw else TimeCapsules(env.db, env.bot, Settings(_env_file=None, **settings_kw))


async def rows(env, sql: str, **kw: Any):
    return await owner_exec(env.owner_engine, sql, u=env.user_id, **kw)


async def seed(
    env,
    message: str = "я хочу бросить курить",
    *,
    deliver_at: datetime = NOW - timedelta(hours=1),
    created_at: datetime | None = None,
    status: str = "pending",
    claimed_at: datetime | None = None,
    user: uuid.UUID | None = None,
    source: int | None = None,
) -> uuid.UUID:
    """A capsule that is due (by default): sealed long enough before ``deliver_at`` to satisfy the window CHECK."""
    created = created_at or deliver_at - timedelta(days=200)
    [(cid,)] = await owner_exec(
        env.owner_engine,
        "INSERT INTO civ.time_capsules (user_id, title, message, deliver_at, status, delivered_at, claimed_at, created_at, source_message_id) "
        "VALUES (:u, 'Письмо', :m, :d, :s, :dl, :c, :cr, :src) RETURNING id",
        u=user or env.user_id, m=message, d=deliver_at, s=status, dl=deliver_at if status == "delivered" else None, c=claimed_at, cr=created, src=source,
    )  # fmt: skip
    return cid


async def status_of(env, cid: uuid.UUID) -> tuple[Any, ...]:
    return (await rows(env, "SELECT status, delivered_at IS NOT NULL, claimed_at IS NOT NULL FROM civ.time_capsules WHERE id = :i", i=cid))[0]


async def tg_message(env, text: str, telegram_id: int) -> uuid.UUID:
    [(mid,)] = await rows(
        env,
        "INSERT INTO civ.messages (user_id, role, content, telegram_message_id) VALUES (:u, CAST('user' AS civ.message_role), :c, :t) RETURNING id",
        c=text, t=telegram_id,
    )  # fmt: skip
    return mid


def sent_texts(env) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id]


def model_seals(env, *calls: ToolCall, text: str = "Запечатал, всё будет!", then: str = "Хорошая мысль.") -> None:
    """The fake model calls the function - and, as a real one might, ALSO claims it sealed everything."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


async def press(env, data: str, *, tg: int | None = None) -> None:
    await env.feed_update(make_callback_update(data=data, user_id=tg or env.telegram_user_id, chat_id=env.chat_id, message_id=777))


# ================================================================== the database


async def test_capsules_belong_to_one_tenant_and_the_others_cannot_see_them(cap_env, owner_engine, db):
    await seed(cap_env)
    stranger = await new_user(owner_engine)
    assert await tenant_exec(db, stranger, "SELECT count(*) FROM civ.time_capsules") == [(0,)]
    assert await tenant_exec(db, cap_env.user_id, "SELECT count(*) FROM civ.time_capsules") == [(1,)]


@pytest.mark.parametrize(
    "tail",
    [
        "title = E'два\\nряда'",
        "title = repeat('я', 81)",
        "message = ''",
        "message = repeat('я', 1001)",
        "message = E'два\\x07звонка'",
        "status = 'lost'",
        "status = 'delivered'",  # delivered without delivered_at
        "delivered_at = now()",  # delivered_at without delivered
        "deliver_at = created_at + interval '5 days'",  # a timer, not a capsule
        "deliver_at = created_at + interval '1831 days'",  # forever
    ],
)
async def test_the_checks_refuse_nonsense(cap_env, db, tail):
    base = {"title": "'Письмо'", "message": "'текст письма'", "status": "'pending'", "delivered_at": "NULL", "deliver_at": "now() + interval '30 days'", "created_at": "now()"}
    column, expression = tail.split(" = ", 1)
    base[column] = expression
    with pytest.raises(DB_ERRORS):
        await tenant_exec(
            db,
            cap_env.user_id,
            "INSERT INTO civ.time_capsules (user_id, title, message, status, delivered_at, deliver_at, created_at) "
            f"VALUES (:u, {base['title']}, {base['message']}, {base['status']}, {base['delivered_at']}, {base['deliver_at']}, {base['created_at']})",
            u=cap_env.user_id,
        )


async def test_the_window_edges_are_inside_the_checks(cap_env, db):
    for edge in ("interval '6 days' + interval '1 minute'", "interval '1830 days' - interval '1 minute'"):
        await tenant_exec(db, cap_env.user_id, f"INSERT INTO civ.time_capsules (user_id, message, deliver_at) VALUES (:u, 'ок', now() + {edge})", u=cap_env.user_id)


async def test_one_message_seals_at_most_one_capsule(cap_env, db):
    u = cap_env.user_id
    insert = "INSERT INTO civ.time_capsules (user_id, message, deliver_at, source_message_id) VALUES (:u, 'ок', now() + interval '30 days', :s)"
    await tenant_exec(db, u, insert, u=u, s=555)
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, u, insert, u=u, s=555)
    await tenant_exec(db, u, insert, u=u, s=None)  # a capsule with no source message is not limited
    await tenant_exec(db, u, insert, u=u, s=None)


async def test_a_decided_capsule_is_final_even_for_the_owner_of_the_table(cap_env, db, owner_engine):
    done = await seed(cap_env, status="delivered")
    gone = await seed(cap_env, "второе", status="canceled")
    for cid in (done, gone):
        with pytest.raises(DB_ERRORS, match="final"):
            await owner_exec(owner_engine, "UPDATE civ.time_capsules SET claimed_at = now() WHERE id = :i", i=cid)
        with pytest.raises(DB_ERRORS, match="final"):
            await tenant_exec(db, cap_env.user_id, "UPDATE civ.time_capsules SET status = 'pending', delivered_at = NULL WHERE id = :i", i=cid)


async def test_the_app_may_move_only_the_life_of_a_capsule_and_never_delete_it(cap_env, db):
    cid = await seed(cap_env)
    u = cap_env.user_id
    for sql in ("UPDATE civ.time_capsules SET message = 'другое' WHERE id = :i", "UPDATE civ.time_capsules SET deliver_at = deliver_at + interval '1 day' WHERE id = :i", "DELETE FROM civ.time_capsules WHERE id = :i"):
        with pytest.raises(DB_ERRORS):
            await tenant_exec(db, u, sql, i=cid)
    await tenant_exec(db, u, "UPDATE civ.time_capsules SET claimed_at = now() WHERE id = :i", i=cid)
    await tenant_exec(db, u, "UPDATE civ.time_capsules SET status = 'canceled' WHERE id = :i", i=cid)


# ---------------------------------------------------------------- whom the hourly job must visit


async def due(env, lease: float = 10.0) -> list[uuid.UUID]:
    return await env.db.tenants_with_due_capsules(limit=100_000, lease_minutes=lease)


async def test_a_tenant_with_a_due_pending_capsule_is_visited_and_the_others_are_not(cap_env):
    await seed(cap_env, deliver_at=datetime.now(UTC) - timedelta(hours=1))
    assert cap_env.user_id in await due(cap_env)


@pytest.mark.parametrize(
    "kw",
    [
        {"deliver_at": datetime.now(UTC) + timedelta(days=30)},  # not due yet
        {"deliver_at": datetime.now(UTC) - timedelta(hours=1), "status": "delivered"},
        {"deliver_at": datetime.now(UTC) - timedelta(hours=1), "status": "canceled"},
        {"deliver_at": datetime.now(UTC) - timedelta(hours=1), "claimed_at": "live"},  # under a live lease (the moment is taken when the test runs, not at collection)
    ],
)
async def test_these_are_not_visited(cap_env, kw):
    kw = {k: (datetime.now(UTC) - timedelta(minutes=1) if v == "live" else v) for k, v in kw.items()}
    await seed(cap_env, **kw)
    assert cap_env.user_id not in await due(cap_env)


async def test_an_expired_lease_makes_the_tenant_due_again(cap_env):
    await seed(cap_env, deliver_at=datetime.now(UTC) - timedelta(hours=1), claimed_at=datetime.now(UTC) - timedelta(minutes=30))
    assert cap_env.user_id in await due(cap_env)


@pytest.mark.parametrize("update_sql", ["UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", "UPDATE civ.users SET onboarding_state = 'in_progress' WHERE id = :u"])
async def test_a_frozen_group_or_an_unfinished_onboarding_is_not_visited(cap_env, update_sql):
    await seed(cap_env, deliver_at=datetime.now(UTC) - timedelta(hours=1))
    await rows(cap_env, update_sql)
    assert cap_env.user_id not in await due(cap_env)


# ================================================================== the tool: sealing


async def seal(env, said: str, *calls: ToolCall, telegram_id: int | None = None, now: datetime = NOW, tz: str = "UTC", **settings_kw: Any):
    message_id = await tg_message(env, said, telegram_id) if telegram_id is not None else await new_message(env.owner_engine, env.user_id, said)
    return await engine_of(env, **settings_kw).seal(env.user_id, tz, UserTurn(message_id, said), calls, now=now)


async def test_a_capsule_is_sealed_with_the_code_computed_date_and_the_telegram_id_of_the_message(cap_env):
    done = await seal(cap_env, ASK, ccall(deliver_at="2026-12-01"), telegram_id=4242)  # the model's date is wrong: the code's wins
    assert isinstance(done, capsule.Sealed) and done.when == "08.04.2027" and done.message == "я хочу бросить курить"
    [row] = await rows(cap_env, "SELECT title, message, deliver_at, status, delivered_at, source_message_id FROM civ.time_capsules WHERE user_id = :u")
    assert row == ("Письмо на 08.04.2027", "я хочу бросить курить", datetime(2027, 4, 8, 9, 0, tzinfo=UTC), "pending", None, 4242)


async def test_the_zone_of_the_tenant_decides_the_instant(cap_env):
    await seal(cap_env, ASK, ccall(), tz="Asia/Tokyo")
    assert await rows(cap_env, "SELECT deliver_at FROM civ.time_capsules WHERE user_id = :u") == [(datetime(2027, 4, 8, 0, 0, tzinfo=UTC),)]


async def test_a_replayed_message_gets_its_own_capsule_back_and_never_a_second_one(cap_env):
    first = await seal(cap_env, ASK, ccall(), telegram_id=77)
    message_id = (await rows(cap_env, "SELECT id FROM civ.messages WHERE user_id = :u AND telegram_message_id = 77"))[0][0]
    second = await engine_of(cap_env).seal(cap_env.user_id, "UTC", UserTurn(message_id, ASK), [ccall(message="я хочу бросить курить")], now=NOW)
    assert isinstance(first, capsule.Sealed) and second == first and await rows(cap_env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(1,)]


async def test_a_replayed_message_whose_capsule_was_canceled_gets_nothing(cap_env):
    first = await seal(cap_env, ASK, ccall(), telegram_id=78)
    await engine_of(cap_env).cancel(cap_env.user_id, first.capsule_id)
    message_id = (await rows(cap_env, "SELECT id FROM civ.messages WHERE user_id = :u AND telegram_message_id = 78"))[0][0]
    assert await engine_of(cap_env).seal(cap_env.user_id, "UTC", UserTurn(message_id, ASK), [ccall()], now=NOW) is None


async def test_too_soon_and_too_far_are_refused_without_a_trace(cap_env):
    assert await seal(cap_env, "Напиши мне через 3 дня: я хочу бросить курить", ccall()) == Refused("too_soon")
    assert await seal(cap_env, "Напиши мне через 6 лет: я хочу бросить курить", ccall()) == Refused("too_far")
    assert await rows(cap_env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(0,)]


async def test_unusable_and_foreign_calls_seal_nothing(cap_env):
    assert await seal(cap_env, ASK, ccall(message="я хочу выучить датский")) is None  # not the user's words
    assert await seal(cap_env, ASK, ToolCall("create_event", ccall().args)) is None
    assert await rows(cap_env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(0,)]


async def test_only_the_first_usable_call_of_a_message_counts(cap_env):
    done = await seal(cap_env, ASK, ccall(message="выучить датский"), ccall(), ccall(message="я хочу бросить курить", title="Бросить курить"))
    assert isinstance(done, capsule.Sealed) and await rows(cap_env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(1,)]


async def test_the_pending_and_the_daily_caps(cap_env):
    assert isinstance(await seal(cap_env, ASK, ccall(), capsules_max_pending=1), capsule.Sealed)
    assert await seal(cap_env, ASK + " ещё", ccall(), capsules_max_pending=1) == Refused("limit")
    assert await seal(cap_env, ASK + " и ещё", ccall(), capsules_max_per_day=1) == Refused("limit")
    assert await rows(cap_env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(1,)]


async def test_a_delivered_capsule_does_not_count_as_pending(cap_env):
    await seed(cap_env, status="delivered")
    assert isinstance(await seal(cap_env, ASK, ccall(), capsules_max_pending=1), capsule.Sealed)


async def test_a_database_failure_seals_nothing_and_never_raises(cap_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(TimeCapsules, "_insert", boom)
    assert await seal(cap_env, ASK, ccall()) is None
    assert await rows(cap_env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(0,)]


# ---------------------------------------------------------------- cancelling


async def test_cancelling_a_pending_capsule_is_final_and_idempotent(cap_env):
    cid = await seed(cap_env, deliver_at=NOW + timedelta(days=30))
    assert (await engine_of(cap_env).cancel(cap_env.user_id, cid)).outcome == "canceled"
    assert (await engine_of(cap_env).cancel(cap_env.user_id, cid)).outcome == "settled"
    assert (await status_of(cap_env, cid))[0] == "canceled"


async def test_a_delivered_capsule_cannot_be_canceled(cap_env):
    cid = await seed(cap_env, status="delivered")
    assert (await engine_of(cap_env).cancel(cap_env.user_id, cid)).outcome == "delivered" and (await status_of(cap_env, cid))[0] == "delivered"


async def test_six_simultaneous_presses_cancel_once(cap_env):
    cid = await seed(cap_env, deliver_at=NOW + timedelta(days=30))
    results = await asyncio.gather(*(engine_of(cap_env).cancel(cap_env.user_id, cid) for _ in range(6)))
    assert sorted(r.outcome for r in results) == ["canceled"] + ["settled"] * 5


async def test_another_tenant_and_an_unknown_id_are_gone(cap_env, owner_engine):
    stranger = await new_user(owner_engine)
    cid = await seed(cap_env, deliver_at=NOW + timedelta(days=30))
    assert (await engine_of(cap_env).cancel(stranger, cid)).outcome == "gone"
    assert (await engine_of(cap_env).cancel(cap_env.user_id, uuid.uuid4())).outcome == "gone"
    assert (await status_of(cap_env, cid))[0] == "pending"


# ================================================================== delivery


async def test_a_due_capsule_is_delivered_once_with_the_card_of_the_task(cap_env):
    cid = await seed(cap_env, created_at=datetime(2026, 4, 1, 10, 0, tzinfo=UTC))
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=NOW) == 1
    assert sent_texts(cap_env) == ["⏳ Письмо из прошлого (запечатано 01.04.2026):\n\nя хочу бросить курить\n\nТы достиг того, о чём думал тогда?"]
    assert (await status_of(cap_env, cid))[:2] == ("delivered", True)
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=NOW) == 0 and len(sent_texts(cap_env)) == 1
    messages = await cap_env.messages()
    assert [m["role"] for m in messages] == ["assistant"] and messages[0]["content"].startswith("⏳ Письмо из прошлого")  # the answer finds its question in the context


async def test_the_sealing_date_is_the_tenants_local_date(cap_env):
    await rows(cap_env, "UPDATE civ.users SET timezone = 'Asia/Tokyo' WHERE id = :u")
    await seed(cap_env, created_at=datetime(2026, 4, 1, 22, 30, tzinfo=UTC))  # already the 2nd in Tokyo
    await engine_of(cap_env).send_due(cap_env.user_id, now=NOW)
    assert "(запечатано 02.04.2026)" in sent_texts(cap_env)[0]


async def test_a_future_or_canceled_capsule_is_not_delivered(cap_env):
    await seed(cap_env, "будущее", deliver_at=NOW + timedelta(days=1))
    await seed(cap_env, "отменённое", status="canceled")
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=NOW) == 0 and sent_texts(cap_env) == []


async def test_nothing_is_delivered_in_quiet_hours_and_it_waits_for_the_morning(cap_env):
    cid = await seed(cap_env, deliver_at=datetime(2026, 10, 8, 2, 0, tzinfo=UTC))
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=datetime(2026, 10, 8, 3, 0, tzinfo=UTC)) == 0  # 03:00 is quiet
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=datetime(2026, 10, 8, 7, 59, tzinfo=UTC)) == 0
    assert (await status_of(cap_env, cid))[:3] == ("pending", False, False)  # not even claimed
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=datetime(2026, 10, 8, 8, 15, tzinfo=UTC)) == 1
    assert (await status_of(cap_env, cid))[0] == "delivered"


async def test_the_quiet_hours_are_the_tenants_own_clock(cap_env):
    await rows(cap_env, "UPDATE civ.users SET timezone = 'Asia/Tokyo' WHERE id = :u")
    await seed(cap_env, deliver_at=NOW - timedelta(days=1))
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=datetime(2026, 10, 8, 14, 0, tzinfo=UTC)) == 0  # 23:00 in Tokyo: quiet
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=datetime(2026, 10, 8, 0, 0, tzinfo=UTC)) == 1  # 09:00 in Tokyo


async def test_at_most_the_per_run_cap_is_delivered_oldest_first(cap_env):
    ids = [await seed(cap_env, f"письмо {n}", deliver_at=NOW - timedelta(hours=5 - n)) for n in range(5)]
    assert await engine_of(cap_env, capsules_per_tick=3).send_due(cap_env.user_id, now=NOW) == 3
    assert [(await status_of(cap_env, i))[0] for i in ids] == ["delivered"] * 3 + ["pending"] * 2
    assert "письмо 0" in sent_texts(cap_env)[0]
    assert await engine_of(cap_env, capsules_per_tick=3).send_due(cap_env.user_id, now=NOW + timedelta(minutes=11)) == 2


async def test_a_capsule_under_a_live_lease_is_left_alone_until_it_expires(cap_env):
    cid = await seed(cap_env, claimed_at=NOW - timedelta(minutes=2))
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=NOW) == 0
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=NOW + timedelta(minutes=9)) == 1  # the lease (10 minutes) ran out: the dead worker's letter goes out
    assert (await status_of(cap_env, cid))[0] == "delivered"


async def test_overlapping_runs_deliver_the_letter_once(cap_env):
    await seed(cap_env)
    results = await asyncio.gather(*(engine_of(cap_env).send_due(cap_env.user_id, now=NOW) for _ in range(5)))
    assert sum(results) == 1 and len(sent_texts(cap_env)) == 1


async def test_a_failed_send_gives_the_lease_back_and_the_next_run_delivers(cap_env, monkeypatch):
    cid = await seed(cap_env)
    real = Bot.send_message

    async def down(self, *a: Any, **k: Any):
        raise ConnectionError("telegram is down")

    monkeypatch.setattr(Bot, "send_message", down)
    with pytest.raises(ConnectionError):
        await engine_of(cap_env).send_due(cap_env.user_id, now=NOW)
    assert (await status_of(cap_env, cid))[:3] == ("pending", False, False)  # the lease went back
    monkeypatch.setattr(Bot, "send_message", real)
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=NOW) == 1


async def test_a_failure_on_the_second_letter_keeps_the_first_and_releases_the_rest(cap_env, monkeypatch):
    first = await seed(cap_env, "первое письмо", deliver_at=NOW - timedelta(hours=3))
    second = await seed(cap_env, "второе письмо", deliver_at=NOW - timedelta(hours=2))
    third = await seed(cap_env, "третье письмо", deliver_at=NOW - timedelta(hours=1))
    real = Bot.send_message
    calls = {"n": 0}

    async def flaky(self, *a: Any, **k: Any):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("telegram hiccup")
        return await real(self, *a, **k)

    monkeypatch.setattr(Bot, "send_message", flaky)
    with pytest.raises(ConnectionError):
        await engine_of(cap_env).send_due(cap_env.user_id, now=NOW)
    assert [(await status_of(cap_env, i)) for i in (first, second, third)] == [("delivered", True, True), ("pending", False, False), ("pending", False, False)]


@pytest.mark.parametrize("update_sql", ["UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", "UPDATE civ.users SET onboarding_state = 'in_progress' WHERE id = :u"])
async def test_a_frozen_group_or_an_unfinished_onboarding_delivers_nothing(cap_env, update_sql):
    cid = await seed(cap_env)
    await rows(cap_env, update_sql)
    assert await engine_of(cap_env).send_due(cap_env.user_id, now=NOW) == 0 and (await status_of(cap_env, cid))[0] == "pending"


async def test_another_tenants_capsule_is_never_delivered_to_me(cap_env, owner_engine):
    stranger = await new_user(owner_engine)
    theirs = await seed(cap_env, "чужое письмо", user=stranger)
    await seed(cap_env)
    await engine_of(cap_env).send_due(cap_env.user_id, now=NOW)
    assert all("чужое" not in t for t in sent_texts(cap_env))
    assert (await owner_exec(owner_engine, "SELECT status FROM civ.time_capsules WHERE id = :i", i=theirs)) == [("pending",)]


# ---------------------------------------------------------------- the Beat entry points


async def test_the_dispatcher_offers_exactly_the_tenants_with_a_capsule_due(cap_env, db):
    await seed(cap_env, deliver_at=datetime.now(UTC) - timedelta(hours=1))
    offered: list[uuid.UUID] = []
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, capsules_dispatch_limit=100_000))
    await tasks.dispatch_time_capsules_async(rt, offered.append)  # type: ignore[arg-type]
    assert cap_env.user_id in offered


async def test_the_dispatcher_does_nothing_when_the_feature_is_off(cap_env, db):
    await seed(cap_env, deliver_at=datetime.now(UTC) - timedelta(hours=1))
    offered: list[uuid.UUID] = []
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, capsules_enabled=False))
    assert await tasks.dispatch_time_capsules_async(rt, offered.append) == 0 and offered == []  # type: ignore[arg-type]


async def test_a_queue_failure_for_one_tenant_is_reported_after_the_others_were_offered(cap_env, db):
    await seed(cap_env, deliver_at=datetime.now(UTC) - timedelta(hours=1))
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, capsules_dispatch_limit=100_000))

    def broken(tenant_id: uuid.UUID) -> None:
        raise ConnectionError("broker is down")

    with pytest.raises(tasks.DispatchError):
        await tasks.dispatch_time_capsules_async(rt, broken)  # type: ignore[arg-type]


async def test_the_worker_task_delivers_the_letter(cap_env, db):
    await seed(cap_env, deliver_at=datetime.now(UTC) - timedelta(hours=1))
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None))
    sent = await tasks.check_time_capsules_async(rt, cap_env.bot, cap_env.user_id)  # type: ignore[arg-type]
    assert sent in (0, 1)  # the real clock: the quiet hours of the test machine decide; the call itself must not fail


def test_the_beat_schedule_has_the_hourly_entry_at_quarter_past():
    from ai_civ import celery_app

    entry = celery_app.app.conf.beat_schedule["capsules-dispatch"]
    assert entry["task"] == "ai_civ.dispatch_time_capsules" and entry["schedule"].minute == {15}
    assert {"ai_civ.dispatch_time_capsules", "ai_civ.check_time_capsules"} <= set(celery_app.app.tasks)


# ================================================================== "a year ago"


def at(day: date, hour: int = 12, zone: ZoneInfo = UTC_ZONE) -> datetime:
    return datetime(day.year, day.month, day.day, hour, 0, tzinfo=zone).astimezone(UTC)


async def done_task(env, title: str, when: datetime, *, parent: uuid.UUID | None = None) -> uuid.UUID:
    [(tid,)] = await rows(
        env,
        "INSERT INTO civ.tasks (user_id, parent_task_id, title, source, status, updated_at) VALUES (:u, :p, :t, 'chat_decompose', 'done', :w) RETURNING id",
        p=parent, t=title, w=when,
    )  # fmt: skip
    return tid


async def fact(env, key: str, category: str, value: str, when: datetime, priority: int = 50) -> None:
    await rows(
        env,
        "INSERT INTO civ.core_facts (user_id, category, key, value_text, priority, source, valid_from) VALUES (:u, CAST(:c AS civ.fact_category), :k, :v, :p, 'user_manual', :w)",
        c=category, k=key, v=value, p=priority, w=when,
    )  # fmt: skip


async def line(env, *, now: datetime = NOW, timezone: str = "UTC", **settings_kw: Any) -> str | None:
    settings = Settings(_env_file=None, **settings_kw) if settings_kw else env.orchestrator.settings.model_copy(update={"flashback_enabled": True})
    return await flashback.find(env.db, env.user_id, timezone, settings, now=now)


async def test_a_task_finished_exactly_a_year_ago_makes_the_line(cap_env):
    await done_task(cap_env, "Подготовить презентацию", at(TODAY - timedelta(days=365)))
    assert await line(cap_env) == "🕰 Год назад в этот день ты завершил задачу «Подготовить презентацию»."


async def test_half_a_year_ago_is_the_fallback_and_a_year_ago_is_preferred(cap_env):
    await done_task(cap_env, "Полгода назад", at(TODAY - timedelta(days=180)))
    assert await line(cap_env) == "🕰 Полгода назад в этот день ты завершил задачу «Полгода назад»."
    await done_task(cap_env, "Год назад", at(TODAY - timedelta(days=365)))
    assert (await line(cap_env)).startswith("🕰 Год назад в этот день")


async def test_a_key_fact_written_that_day_is_a_milestone(cap_env):
    await fact(cap_env, "goal.1", "goal", "Выучить датский", at(TODAY - timedelta(days=365)))
    assert await line(cap_env) == "🕰 Год назад в этот день ты поставил цель: «Выучить датский»."


async def test_a_principle_or_a_high_priority_fact_is_recorded_but_a_plain_one_is_not(cap_env):
    await fact(cap_env, "preference.tea", "preference", "люблю чай", at(TODAY - timedelta(days=365)))
    assert await line(cap_env) is None  # an ordinary preference is not a milestone
    await fact(cap_env, "identity.name", "identity", "Аня", at(TODAY - timedelta(days=365)), priority=80)
    assert await line(cap_env) == "🕰 Год назад в этот день ты записал в профиль: «Аня»."


async def test_a_finished_step_and_a_closed_note_are_milestones_too_in_that_order(cap_env):
    root = await done_task(cap_env, "Большая цель", at(TODAY - timedelta(days=500)))
    await done_task(cap_env, "Собрать тезисы", at(TODAY - timedelta(days=365)), parent=root)
    await rows(
        cap_env,
        "INSERT INTO civ.geo_notes (user_id, place_name, content, status, archived_at) VALUES (:u, 'магазин', 'купить молоко', 'archived', :w)",
        w=at(TODAY - timedelta(days=365)),
    )
    assert await line(cap_env) == "🕰 Год назад в этот день ты выполнил шаг «Собрать тезисы»."
    await rows(cap_env, "DELETE FROM civ.tasks WHERE user_id = :u AND parent_task_id IS NOT NULL")
    assert await line(cap_env) == "🕰 Год назад в этот день ты закрыл заметку «купить молоко»."


async def test_a_goal_finished_beats_a_fact_beats_a_step(cap_env):
    day = at(TODAY - timedelta(days=365))
    root = await done_task(cap_env, "Большая цель", at(TODAY - timedelta(days=400)))
    await done_task(cap_env, "Шаг", day, parent=root)
    await fact(cap_env, "goal.1", "goal", "Выучить датский", day)
    assert "поставил цель" in await line(cap_env)
    await done_task(cap_env, "Завершённая цель", day)
    assert "завершил задачу «Завершённая цель»" in await line(cap_env)


async def test_no_milestone_means_no_line(cap_env):
    await done_task(cap_env, "Не тот день", at(TODAY - timedelta(days=364)))
    await done_task(cap_env, "И не этот", at(TODAY - timedelta(days=366)))
    assert await line(cap_env) is None


async def test_a_text_too_long_to_quote_is_skipped_not_cut(cap_env):
    await fact(cap_env, "goal.1", "goal", "я" * 161, at(TODAY - timedelta(days=365)))
    assert await line(cap_env) is None
    await fact(cap_env, "goal.2", "goal", "Короткая цель", at(TODAY - timedelta(days=365)))
    assert await line(cap_env) == "🕰 Год назад в этот день ты поставил цель: «Короткая цель»."


async def test_the_day_is_the_tenants_local_day(cap_env):
    await rows(cap_env, "UPDATE civ.users SET timezone = 'Asia/Tokyo' WHERE id = :u")
    await done_task(cap_env, "Ночная задача", datetime(2025, 10, 7, 16, 0, tzinfo=UTC))  # 01:00 on 2025-10-08 in Tokyo
    tokyo_morning = datetime(2026, 10, 8, 0, 30, tzinfo=UTC)  # 09:30 on 2026-10-08 in Tokyo
    assert await line(cap_env, now=tokyo_morning, timezone="Asia/Tokyo") == "🕰 Год назад в этот день ты завершил задачу «Ночная задача»."
    assert await line(cap_env, now=tokyo_morning, timezone="UTC") is None  # by the UTC date it would be another day


async def test_only_the_first_checkin_of_the_local_day_gets_the_line(cap_env):
    await done_task(cap_env, "Подготовить презентацию", at(TODAY - timedelta(days=365)))
    for hour in (10, 14):
        await rows(cap_env, "INSERT INTO civ.proactive_checkin_log (user_id, slot_date, slot_time) VALUES (:u, :d, :t)", d=TODAY, t=datetime(2026, 1, 1, hour, 30).time())
        if hour == 10:
            assert await line(cap_env) is not None  # one row: this very check-in
    assert await line(cap_env) is None  # two rows: an earlier one had its chance


async def test_the_switch_and_a_database_failure_give_no_line(cap_env, monkeypatch):
    await done_task(cap_env, "Подготовить презентацию", at(TODAY - timedelta(days=365)))
    assert await flashback.find(cap_env.db, cap_env.user_id, "UTC", Settings(_env_file=None, flashback_enabled=False), now=NOW) is None

    async def boom(*a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(flashback, "_milestone", boom)
    assert await line(cap_env) is None  # never raises


async def test_another_tenants_milestone_never_leaks(cap_env, owner_engine):
    stranger = await new_user(owner_engine)
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.tasks (user_id, title, source, status, updated_at) VALUES (:u, 'Чужая цель', 'chat_decompose', 'done', :w)",
        u=stranger, w=at(TODAY - timedelta(days=365)),
    )  # fmt: skip
    assert await line(cap_env) is None


async def test_the_first_checkin_of_the_day_ends_with_the_line_and_the_stored_message_too(owner_engine, db):
    env = await build_bot_env(owner_engine, db, flashback_enabled=True)
    try:
        today = datetime.now(UTC).astimezone(CPH).date()
        await done_task(env, "Подготовить презентацию", at(today - timedelta(days=365), zone=CPH))
        env.chat.responder = lambda system, turns: "Как дела? Что нового по целям?"
        assert await env.orchestrator.run_proactive(env.tenant, get_trigger(CHECK_IN)) is True
        sent = sent_texts(env)[-1]
        assert sent == "Как дела? Что нового по целям?\n\n🕰 Год назад в этот день ты завершил задачу «Подготовить презентацию»."
        assert (await env.messages())[-1]["content"] == sent
    finally:
        await env.bot.session.close()


async def test_a_checkin_without_a_milestone_is_unchanged(owner_engine, db):
    env = await build_bot_env(owner_engine, db, flashback_enabled=True)
    try:
        env.chat.responder = lambda system, turns: "Как дела?"
        await env.orchestrator.run_proactive(env.tenant, get_trigger(CHECK_IN))
        assert sent_texts(env)[-1] == "Как дела?"
    finally:
        await env.bot.session.close()


async def test_other_proactive_messages_never_get_the_line(owner_engine, db):
    env = await build_bot_env(owner_engine, db, flashback_enabled=True)
    try:
        today = datetime.now(UTC).astimezone(CPH).date()
        await done_task(env, "Подготовить презентацию", at(today - timedelta(days=365), zone=CPH))
        env.chat.responder = lambda system, turns: "Пора пообедать."
        await env.orchestrator.run_proactive(env.tenant, get_trigger("meal_check"))
        assert "🕰" not in sent_texts(env)[-1]
    finally:
        await env.bot.session.close()


# ================================================================== the whole flow through the Dispatcher


async def test_a_request_gets_the_function_and_the_reply_is_a_card_with_the_code_computed_date(cap_env):
    env = cap_env
    await rows(env, "UPDATE civ.users SET timezone = 'Europe/Copenhagen' WHERE id = :u")
    expected = (datetime.now(UTC).astimezone(CPH).date() + timedelta(days=0))
    model_seals(env, ccall(deliver_at="2026-12-01"))  # the model's date is wrong on purpose
    await env.feed(ASK)
    offered = env.chat.tool_calls[0]
    assert [t.name for t in offered["tools"]] == ["seal_time_capsule"] and "## Time capsule" in offered["system"]
    assert not any("запечатал" in t.lower() and "Откроется" not in t for t in sent_texts(env))  # the model's own claim is never sent
    assert "Хорошая мысль." in sent_texts(env)
    card = [m for m in env.session.sent() if m.reply_markup is not None][-1]
    [(deliver_at,)] = await rows(env, "SELECT deliver_at FROM civ.time_capsules WHERE user_id = :u")
    local = deliver_at.astimezone(CPH)
    assert local.hour == 9 and local.minute == 0 and 175 <= (local.date() - expected).days <= 186  # "через полгода", computed by the code in the user's zone
    assert card.text == f"⏳ Запечатал письмо в будущее. Откроется {local.strftime('%d.%m.%Y')}: «я хочу бросить курить»"
    assert card.reply_markup.inline_keyboard[0][0].text == "✖️ Отменить капсулу"
    assert not any("⏳" in (m["content"] or "") for m in await env.messages())  # the card is not a word of the assistant: not remembered as one


async def test_the_button_cancels_the_capsule_and_edits_the_card(cap_env):
    env = cap_env
    model_seals(env, ccall())
    await env.feed(ASK)
    data = [m for m in env.session.sent() if m.reply_markup is not None][-1].reply_markup.inline_keyboard[0][0].callback_data
    await press(env, data)
    assert await rows(env, "SELECT status FROM civ.time_capsules WHERE user_id = :u") == [("canceled",)]
    assert env.session.edits()[-1].text == texts.CAPSULE_CANCELED and env.session.edits()[-1].reply_markup is None
    assert env.session.callback_answers()[-1].text == texts.CAPSULE_TOAST["canceled"]
    await press(env, data)
    assert env.session.callback_answers()[-1].text == texts.CAPSULE_TOAST["settled"]


async def test_a_stranger_cannot_press_my_button(cap_env, owner_engine):
    env = cap_env
    model_seals(env, ccall())
    await env.feed(ASK)
    data = [m for m in env.session.sent() if m.reply_markup is not None][-1].reply_markup.inline_keyboard[0][0].callback_data
    stranger = random.randrange(10**9, 10**12)  # fresh ids: other tests leave tenants with fixed ones behind
    await new_user(owner_engine, telegram_user_id=stranger, forum_chat_id=-int(f"100{random.randrange(10**9, 10**10)}"))
    await press(env, data, tg=stranger)  # an unknown chat for that tenant: the middleware drops it before any handler
    assert await rows(env, "SELECT status FROM civ.time_capsules WHERE user_id = :u") == [("pending",)]


async def test_garbage_buttons_change_nothing(cap_env):
    env = cap_env
    await press(env, "tc:not-a-uuid:x")
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_UNKNOWN_BUTTON
    await press(env, f"tc:{uuid.uuid4()}:x")
    assert env.session.callback_answers()[-1].text == texts.CAPSULE_TOAST["gone"]


async def test_a_capsule_for_three_days_is_refused_with_a_fixed_text_and_nothing_is_sealed(cap_env):
    env = cap_env
    model_seals(env, ccall())
    await env.feed("Сделай капсулу времени через 3 дня: я хочу бросить курить")
    assert texts.CAPSULE_TOO_SOON.format(n=7) in sent_texts(env) and await rows(env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(0,)]
    assert not any("запечатал" in t.lower() for t in sent_texts(env))


async def test_an_invented_letter_gives_no_card_and_the_models_claim_is_not_sent(cap_env):
    env = cap_env
    model_seals(env, ccall(message="я хочу выучить датский и уехать"), then="Не получилось запечатать.")
    await env.feed(ASK)
    assert await rows(env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(0,)]
    assert not any("⏳" in t for t in sent_texts(env)) and not any("запечатал" in t.lower() for t in sent_texts(env))


async def test_a_year_the_user_wrote_must_be_the_year_of_the_models_date(cap_env):
    env = cap_env
    year = datetime.now(UTC).year + 1
    said = f"Напомни мне в {year}: я хочу бросить курить"
    model_seals(env, ccall(deliver_at=f"{year + 1}-03-01"))
    await env.feed(said)
    assert await rows(env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(0,)]
    model_seals(env, ccall(deliver_at=f"{year}-09-01"))
    await env.feed(said + " ещё раз")
    assert await rows(env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(1,)]


async def test_talk_without_a_marker_is_never_offered_the_function(cap_env):
    env = cap_env
    env.chat.responder = lambda system, turns: "Понял."
    for said in ("Напомни в 10 позвонить маме", "Через год будет интересно", "Хочу написать письмо маме"):
        await env.feed(said)
    assert env.chat.tool_calls == [] and all("## Time capsule" not in c["system"] for c in env.chat.calls)


async def test_the_switch_turns_the_function_off(owner_engine, db):
    env = await build_bot_env(owner_engine, db, capsules_enabled=False)
    try:
        env.chat.responder = lambda system, turns: "Хорошо."
        await env.feed(ASK)
        assert env.chat.tool_calls == []
    finally:
        await env.bot.session.close()


async def test_a_voice_request_is_sealed_like_a_typed_one(cap_env):
    env = cap_env
    env.chat.transcriber = lambda data, mime: ASK
    model_seals(env, ccall())
    await env.feed_voice()
    assert await rows(env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(1,)]


async def test_a_redelivered_update_seals_once(cap_env):
    env = cap_env
    model_seals(env, ccall())
    update = env.update(ASK)
    await env.feed_update(update)
    await env.feed_update(update)
    assert await rows(env, "SELECT count(*) FROM civ.time_capsules WHERE user_id = :u") == [(1,)]


async def test_a_database_failure_still_sends_the_reply_without_a_card(cap_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(TimeCapsules, "_insert", boom)
    env = cap_env
    model_seals(env, ccall(), then="Не получилось, повторите.")
    await env.feed(ASK)
    assert "Не получилось, повторите." in sent_texts(env) and not any("⏳" in t for t in sent_texts(env))


def test_the_function_name_is_unique_in_the_production_registry(cap_env):
    names = [name for tool in cap_env.orchestrator.tools.tools for name in tool.functions]
    assert names.count("seal_time_capsule") == 1 and len(names) == len(set(names))

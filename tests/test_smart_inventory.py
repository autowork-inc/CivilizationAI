"""v2.0, PR 4.3: the smart inventory of consumables. The points under test: the model is OFFERED ``update_inventory`` only when CODE finds a purchase / stock cue
(and a digit, or "it ran out") in the user's own message; the quantity and the daily burn must STAND in that message (the only number without a witness is 0 for
"закончился"); every change is one append-only movement written together with the new quantity (the stock is the sum of its movements); the daily Beat job burns
the daily rate for the local days that are due - except a day on which the user corrected the stock by hand - and claims a «⚠️ ... Пора заказать» reminder
(not more often than every 3 days, never in quiet hours, a failed send gives the claim back); the button «Купил / Пополнил» restocks once by the size of the user's
own last restock. Real PostgreSQL (RLS, composite FKs, CHECKs, grants, the due-tenants function), the real aiogram Dispatcher on a fake Telegram wire, a fake Gemini."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from aiogram import Bot
from sqlalchemy.exc import DBAPIError, IntegrityError

from ai_civ import tasks
from ai_civ.bot import texts
from ai_civ.bot.tools import UserTurn, inventory
from ai_civ.bot.tools.inventory import Inventory, parse_unit, parse_update, wants_inventory
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_message, new_user, owner_exec, tenant_exec
from tests.tg_fakes import make_callback_update

DB_ERRORS = (IntegrityError, DBAPIError)
NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)  # 09:00 local for the UTC tenants of these tests: past the 08:00 hour, outside the quiet hours
BOUGHT = "Купил креатин 500г"


def icall(name: str = "креатин", quantity: Any = 500, unit: str = "г", action: str = "restock", **extra: Any) -> ToolCall:
    return ToolCall(inventory.TOOL_NAME, {"item_name": name, "quantity": quantity, "unit": unit, "action": action, **extra})


# ================================================================== pure: the cue check, units, names, the call


@pytest.mark.parametrize(
    "said",
    [
        "Купил креатин 500г",
        "закончился протеин",
        "Осталось 10 капсул омеги",
        "купила стиральный порошок 2 кг",
        "Выпил 2 капсулы омеги",
        "креатин 500 г",  # a known supplement and a number
        "Закончилась бытовая химия",
        "I bought creatine 500 g",
        "заказал протеин 1 кг",
        "Остаток магния 30 шт",
    ],
)
def test_a_purchase_or_stock_report_is_a_cue(said):
    assert wants_inventory(said)


@pytest.mark.parametrize(
    "said",
    [
        "Купил хлеб",  # a cue verb but no number
        "Привет, как дела?",
        "Мне 30 лет",
        "Хочу купить креатин",
        "Встреча в 15:30",
        "",
    ],
)
def test_ordinary_talk_is_not_a_cue(said):
    assert not wants_inventory(said)


@pytest.mark.parametrize(
    ("word", "unit", "factor"),
    [("г", "г", 1), ("грамм", "г", 1), ("КГ", "г", 1000), ("мл", "мл", 1), ("л", "мл", 1000), ("капс.", "капсул", 1), ("капсулы", "капсул", 1), ("порции", "порций", 1), ("шт", "шт", 1), ("таблеток", "шт", 1)],
)
def test_units_are_made_canonical_by_code(word, unit, factor):
    assert parse_unit(word) == (unit, Decimal(factor))


@pytest.mark.parametrize("bad", ["", "метр", None, 5, "килобайт"])
def test_an_unknown_unit_is_refused(bad):
    assert parse_unit(bad) is None


def test_the_tool_is_plain_json_and_requires_the_essentials():
    schema = inventory.TOOL.parameters
    assert inventory.TOOL.name == "update_inventory" == inventory.TOOL_NAME
    assert set(schema["required"]) == {"item_name", "quantity", "unit", "action"}
    assert schema["properties"]["action"]["enum"] == ["restock", "consume", "set"]
    assert json.dumps(schema)


def test_the_instructions_list_the_known_items_and_forbid_claims():
    text = inventory.render_instructions(["Креатин (500 г)"])
    assert "- Креатин (500 г)" in text and "never say it was saved" in text.lower() and "copying every" in text
    assert "Items already tracked" not in inventory.render_instructions([])


def test_a_valid_call_becomes_a_spec():
    spec = parse_update(icall(daily_burn=5).args, "Купил креатин 500г, пью по 5 г в день")
    assert spec is not None and (spec.name, spec.action, spec.quantity, spec.unit, spec.daily_burn) == ("креатин", "restock", Decimal(500), "г", Decimal(5))


def test_kilograms_and_litres_are_the_users_number_times_a_known_factor():
    spec = parse_update(icall("протеин", 1.5, "кг").args, "заказал протеин 1,5 кг")
    assert spec is not None and (spec.quantity, spec.unit) == (Decimal(1500), "г")
    spec = parse_update(icall("омега", 2, "л", daily_burn=50, daily_burn_unit="мл").args, "купил 2 л омеги, по 50 мл в день")
    assert spec is not None and (spec.quantity, spec.daily_burn, spec.unit) == (Decimal(2000), Decimal(50), "мл")


@pytest.mark.parametrize(
    ("args", "said"),
    [
        (icall(quantity=600).args, BOUGHT),  # a quantity the user never wrote
        (icall(quantity=5).args, "купил креатин 500г"),  # 5 is a piece of 500, not a number of the message
        (icall(quantity="много").args, BOUGHT),
        (icall(quantity=True).args, BOUGHT),
        (icall(quantity=float("nan")).args, BOUGHT),
        (icall(quantity=0, action="restock").args, "купил креатин 0г"),  # nothing was bought
        (icall(quantity=0, action="consume").args, "выпил 0 г креатина"),
        (icall(quantity=-5, action="set").args, "осталось -5 г"),
        (icall(quantity=2_000_000).args, "купил креатин 2000000г"),
        (icall(quantity=0.0004).args, "купил креатин 0.0004г"),
        (icall(action="steal").args, BOUGHT),
        (icall(unit="метров").args, BOUGHT),
        (icall(unit=None).args, BOUGHT),
        (icall(name="").args, BOUGHT),
        (icall(name="я" * 81).args, BOUGHT),
        (icall(name="креатин‮").args, BOUGHT),
        (icall(daily_burn=7).args, "Купил креатин 500г, пью по 5 г в день"),  # a burn the user never wrote
        (icall(daily_burn=0).args, "Купил креатин 500г, пью по 0 г в день"),
        (icall(daily_burn=5, daily_burn_unit="мл").args, "Купил креатин 500г, пью по 5 мл в день"),  # another family of units
        (icall(daily_burn=5, daily_burn_unit="метр").args, "Купил креатин 500г, пью по 5 в день"),
        ({"item_name": "креатин", "action": "restock", "unit": "г"}, BOUGHT),  # no quantity at all
    ],
)
def test_an_unstated_or_unusable_number_drops_the_call(args, said):
    assert parse_update(args, said) is None


def test_zero_needs_no_witness_only_for_it_ran_out():
    assert parse_update(icall("протеин", 0, "г", "set").args, "закончился протеин") is not None
    assert parse_update(icall("протеин", 0, "г", "set").args, "осталось 0 г протеина") is not None  # a stated zero
    assert parse_update(icall("протеин", 0, "г", "set").args, "протеин 500г") is None  # nobody said it ran out
    assert parse_update(icall("протеин", 0, "г", "restock").args, "закончился протеин") is None  # ran out is not a purchase


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("креатин", "креатина", True),
        ("шампунь", "шампуня", True),
        ("стиральный порошок", "стирального порошка", True),
        ("магний", "магнит", False),
        ("протеин", "креатин", False),
        ("омега", "омега", True),
        ("порошок", "стиральный порошок", False),
    ],
)
def test_inflected_names_are_the_same_name_and_lookalikes_are_not(a, b, same):
    assert inventory._same_name(a, b) is same


@pytest.mark.parametrize(
    ("days", "text"),
    [("0.5", "меньше чем на день"), ("1", "на 1 день"), ("1.9", "на 1 день"), ("2", "на 2 дня"), ("4.5", "на 4 дня"), ("5", "на 5 дней"), ("11", "на 11 дней"), ("21", "на 21 день"), ("22", "на 22 дня"), ("25", "на 25 дней")],
)
def test_the_days_are_floored_and_declined(days, text):
    assert inventory.lasts_text(Decimal(days)) == text


def test_the_reminder_text_is_the_one_of_the_task():
    assert inventory.reminder_text("Креатин", Decimal("15"), "г", Decimal(5)) == "⚠️ Креатин: осталось на 3 дня (~15 г). Пора заказать новую пачку!"
    assert inventory.reminder_text("Омега-3", Decimal("4"), "капсул", Decimal(2)) == "⚠️ Омега-3: осталось на 2 дня (~4 капсул). Пора заказать новую пачку!"
    assert inventory.reminder_text("Протеин", Decimal("0"), "г", Decimal(30)) == "⚠️ Протеин: осталось меньше чем на день (~0 г). Пора заказать новую пачку!"


def test_the_lines_the_user_reads():
    d = Decimal
    assert inventory.applied_line(inventory.Applied("Креатин", "restock", d(500), d(700), "г", d(5), False)) == "📦 Креатин: +500 г → осталось 700 г (хватит примерно на 140 дней при 5 г/день)"
    assert inventory.applied_line(inventory.Applied("Омега-3", "consume", d(-2), d(8), "капсул", d(0), False)) == "📦 Омега-3: −2 капсул → осталось 8 капсул"
    assert inventory.applied_line(inventory.Applied("Протеин", "set", d(-100), d(0), "г", d(0), False)) == "📦 Протеин: осталось 0 г"


def test_the_callback_data_round_trips_and_fits_telegram():
    item, token = uuid.uuid4(), uuid.uuid4()
    data = inventory.callback_data(item, token)
    assert len(data.encode()) <= 64 and inventory.parse_callback(data) == (item, token.hex[:12])
    for bad in ("", "iv:", f"iv:{item}", f"iv:{item}:zz", f"rc:{item}:{token.hex[:12]}", "iv:not-a-uuid:aaaaaaaaaaaa", f"iv:{item}:{token.hex[:12]}:x"):
        assert inventory.parse_callback(bad) is None
    assert [(b.text, b.callback_data) for row in inventory.reminder_markup(item, token).inline_keyboard for b in row] == [("Купил / Пополнил", data)]


# ================================================================== fixtures and helpers


@pytest_asyncio.fixture
async def inv_env(owner_engine, db):
    """``bot_env`` with the inventory on, as in production, for a tenant living in UTC (the default quiet hours 22:00-08:00 apply)."""
    env = await build_bot_env(owner_engine, db, inventory_enabled=True)
    await owner_exec(owner_engine, "UPDATE civ.users SET timezone = 'UTC' WHERE id = :u", u=env.user_id)
    yield env
    await env.bot.session.close()


def engine_of(env) -> Inventory:
    return env.orchestrator.inventory


async def rows(env, sql: str, **kw: Any):
    return await owner_exec(env.owner_engine, sql, u=env.user_id, **kw)


async def seed(
    env,
    name: str = "Креатин",
    qty: Any = 100,
    unit: str = "г",
    burn: Any = 0,
    *,
    threshold: int = 5,
    last_burn_on: date = date(2026, 10, 5),
    reminded: datetime | None = None,
    token: uuid.UUID | None = None,
    user: uuid.UUID | None = None,
    restock: Any = None,
) -> uuid.UUID:
    """An item (and, when ``qty`` > 0, the movement that explains it: the stock is the sum of the movements)."""
    u = user or env.user_id
    [(item,)] = await owner_exec(
        env.owner_engine,
        "INSERT INTO civ.inventory_items (user_id, name, current_quantity, unit, daily_burn_rate, restock_threshold_days, last_burn_on, last_reminded_at, reminder_token) "
        "VALUES (:u, :n, :q, :unit, :b, :t, :d, :r, :k) RETURNING id",
        u=u, n=name, q=qty, unit=unit, b=burn, t=threshold, d=last_burn_on, r=reminded, k=token,
    )  # fmt: skip
    if Decimal(str(qty)) > 0:
        await movement(env, item, qty, "correction", qty, created=datetime(2026, 9, 1, 12, 0, tzinfo=UTC), user=u)
    if restock is not None:  # the user's own earlier purchase (the size the button repeats), eaten up since: the stock is still the sum of the movements
        await movement(env, item, restock, "manual_restock", Decimal(str(qty)) + Decimal(str(restock)), created=datetime(2026, 9, 2, 12, 0, tzinfo=UTC), user=u)
        await movement(env, item, -Decimal(str(restock)), "daily_burn", qty, created=datetime(2026, 9, 3, 12, 0, tzinfo=UTC), user=u)
    return item


async def movement(env, item: uuid.UUID, change: Any, reason: str, after: Any, *, created: datetime | None = None, user: uuid.UUID | None = None) -> None:
    await owner_exec(
        env.owner_engine,
        "INSERT INTO civ.inventory_movements (user_id, item_id, change_amount, quantity_after, reason, created_at) VALUES (:u, :i, :c, :a, :r, COALESCE(:t, now()))",
        u=user or env.user_id, i=item, c=change, a=after, r=reason, t=created,
    )  # fmt: skip


async def stock(env, name: str = "Креатин") -> tuple[Any, ...]:
    return (await rows(env, "SELECT current_quantity, unit, daily_burn_rate, last_burn_on, last_reminded_at, reminder_token FROM civ.inventory_items WHERE user_id = :u AND name = :n", n=name))[0]


async def moves(env, name: str = "Креатин") -> list[tuple[Any, ...]]:
    return await rows(
        env,
        "SELECT m.change_amount, m.quantity_after, m.reason, m.source_message_id IS NOT NULL FROM civ.inventory_movements m JOIN civ.inventory_items i ON i.user_id = m.user_id AND i.id = m.item_id "
        "WHERE m.user_id = :u AND i.name = :n ORDER BY m.created_at, m.id",
        n=name,
    )


async def assert_stock_is_the_sum_of_movements(env) -> None:
    bad = await rows(
        env,
        "SELECT i.name FROM civ.inventory_items i WHERE i.user_id = :u AND i.current_quantity <> COALESCE((SELECT sum(m.change_amount) FROM civ.inventory_movements m WHERE m.user_id = i.user_id AND m.item_id = i.id), 0)",
    )
    assert bad == []


async def update(env, said: str, *calls: ToolCall, **settings_kw: Any):
    message_id = await new_message(env.owner_engine, env.user_id, said)
    engine = engine_of(env) if not settings_kw else Inventory(env.db, env.bot, Settings(_env_file=None, **settings_kw))
    return await engine.update(env.user_id, UserTurn(message_id, said), calls)


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


def model_updates(env, *calls: ToolCall, text: str = "Записал в учёт!", then: str = "Принято.") -> None:
    """The fake model calls the function - and, as a real one might, ALSO claims it recorded everything."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


async def press(env, data: str, *, message_id: int = 777) -> None:
    await env.dispatcher.feed_update(env.bot, make_callback_update(data=data, user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=message_id))


# ================================================================== the database


async def test_the_tables_belong_to_one_tenant_and_the_others_cannot_see_them(inv_env, owner_engine, db):
    await seed(inv_env)
    stranger = await new_user(owner_engine)
    for table in ("inventory_items", "inventory_movements"):
        assert await tenant_exec(db, stranger, f"SELECT count(*) FROM civ.{table}") == [(0,)]
    assert await tenant_exec(db, inv_env.user_id, "SELECT count(*) FROM civ.inventory_items") == [(1,)]


async def test_the_composite_foreign_keys_refuse_another_tenants_rows(inv_env, owner_engine, db):
    item = await seed(inv_env)
    stranger = await new_user(owner_engine)
    with pytest.raises(DB_ERRORS):  # a movement of the stranger on MY item
        await tenant_exec(db, stranger, "INSERT INTO civ.inventory_movements (user_id, item_id, change_amount, quantity_after, reason) VALUES (:u, :i, 1, 1, 'correction')", u=stranger, i=item)
    mine = await new_message(owner_engine, inv_env.user_id, "моё")
    own = await seed(inv_env, "Своё", 10, user=stranger)
    with pytest.raises(DB_ERRORS):  # a movement that points at MY message
        await tenant_exec(
            db, stranger, "INSERT INTO civ.inventory_movements (user_id, item_id, change_amount, quantity_after, reason, source_message_id) VALUES (:u, :i, 1, 1, 'correction', :m)", u=stranger, i=own, m=mine
        )


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO civ.inventory_items (user_id, name, unit) VALUES (:u, 'Х', 'кг')",
        "INSERT INTO civ.inventory_items (user_id, name, unit) VALUES (:u, E'два\\nряда', 'г')",
        "INSERT INTO civ.inventory_items (user_id, name, unit) VALUES (:u, repeat('я', 81), 'г')",
        "INSERT INTO civ.inventory_items (user_id, name, unit, current_quantity) VALUES (:u, 'Х', 'г', -1)",
        "INSERT INTO civ.inventory_items (user_id, name, unit, daily_burn_rate) VALUES (:u, 'Х', 'г', -1)",
        "INSERT INTO civ.inventory_items (user_id, name, unit, restock_threshold_days) VALUES (:u, 'Х', 'г', 0)",
        "INSERT INTO civ.inventory_items (user_id, name, unit, restock_threshold_days) VALUES (:u, 'Х', 'г', 91)",
        "INSERT INTO civ.inventory_items (user_id, name, unit, reminder_token) VALUES (:u, 'Х', 'г', gen_random_uuid())",  # a reminder card that was never sent
    ],
)
async def test_the_checks_refuse_a_bad_item(inv_env, db, sql):
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, inv_env.user_id, sql, u=inv_env.user_id)


async def test_an_item_name_is_unique_per_tenant(inv_env, owner_engine, db):
    await seed(inv_env)
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, inv_env.user_id, "INSERT INTO civ.inventory_items (user_id, name, unit) VALUES (:u, 'Креатин', 'г')", u=inv_env.user_id)
    other = await new_user(owner_engine)
    await tenant_exec(db, other, "INSERT INTO civ.inventory_items (user_id, name, unit) VALUES (:u, 'Креатин', 'г')", u=other)


@pytest.mark.parametrize(
    "values",
    ["0, 5, 'correction'", "1, -1, 'correction'", "1, 5, 'gift'", "1, 5, 'DAILY_BURN'"],
)
async def test_the_checks_refuse_a_bad_movement(inv_env, db, values):
    item = await seed(inv_env)
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, inv_env.user_id, f"INSERT INTO civ.inventory_movements (user_id, item_id, change_amount, quantity_after, reason) VALUES (:u, :i, {values})", u=inv_env.user_id, i=item)


async def test_the_audit_trail_is_append_only_and_an_item_is_never_deleted_by_the_app(inv_env, db):
    item = await seed(inv_env)
    u = inv_env.user_id
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, u, "UPDATE civ.inventory_movements SET change_amount = 999")
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, u, "DELETE FROM civ.inventory_movements")
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, u, "DELETE FROM civ.inventory_items WHERE id = :i", i=item)
    await tenant_exec(db, u, "UPDATE civ.inventory_items SET current_quantity = 1 WHERE id = :i", i=item)  # the stock itself may move


async def test_one_message_changes_one_item_at_most_once(inv_env, db):
    item = await seed(inv_env)
    message = await new_message(inv_env.owner_engine, inv_env.user_id, BOUGHT)
    u = inv_env.user_id
    insert = "INSERT INTO civ.inventory_movements (user_id, item_id, change_amount, quantity_after, reason, source_message_id) VALUES (:u, :i, 1, 1, 'correction', :m)"
    await tenant_exec(db, u, insert, u=u, i=item, m=message)
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, u, insert, u=u, i=item, m=message)
    other = await seed(inv_env, "Протеин", 5)
    await tenant_exec(db, u, insert, u=u, i=other, m=message)  # another item: allowed


# ---------------------------------------------------------------- whom the daily job must visit (the SQL function)


async def due(env, *, local_hour: int = 0, cooldown: float = 3.0) -> list[uuid.UUID]:
    return await env.db.tenants_with_due_inventory(limit=100_000, local_hour=local_hour, cooldown_days=cooldown)


async def test_a_tenant_with_a_day_to_burn_is_due_and_a_burned_one_is_not(inv_env):
    await seed(inv_env, burn=5, last_burn_on=date.today() - timedelta(days=10))
    assert inv_env.user_id in await due(inv_env)
    await rows(inv_env, "UPDATE civ.inventory_items SET last_burn_on = (now() AT TIME ZONE 'UTC')::date - 1 WHERE user_id = :u")
    assert inv_env.user_id not in await due(inv_env)


async def test_an_item_without_a_burn_rate_is_never_due(inv_env):
    await seed(inv_env, burn=0, last_burn_on=date.today() - timedelta(days=10))
    assert inv_env.user_id not in await due(inv_env)


async def test_running_low_makes_a_tenant_due_until_the_reminder_cooldown(inv_env):
    yesterday = (datetime.now(UTC) - timedelta(days=1)).date()
    await seed(inv_env, qty=20, burn=10, last_burn_on=yesterday)  # 2 days left <= 5
    assert inv_env.user_id in await due(inv_env)
    await rows(inv_env, "UPDATE civ.inventory_items SET last_reminded_at = now() - interval '1 day' WHERE user_id = :u")
    assert inv_env.user_id not in await due(inv_env)
    await rows(inv_env, "UPDATE civ.inventory_items SET last_reminded_at = now() - interval '4 days' WHERE user_id = :u")
    assert inv_env.user_id in await due(inv_env)


async def test_a_well_stocked_item_is_not_due(inv_env):
    await seed(inv_env, qty=1000, burn=10, last_burn_on=(datetime.now(UTC) - timedelta(days=1)).date())
    assert inv_env.user_id not in await due(inv_env)


async def test_the_local_hour_gate_uses_the_tenants_own_clock(inv_env):
    await seed(inv_env, burn=5, last_burn_on=date.today() - timedelta(days=10))
    assert inv_env.user_id not in await due(inv_env, local_hour=24)  # no clock reaches hour 24
    assert inv_env.user_id in await due(inv_env, local_hour=0)


@pytest.mark.parametrize("update_sql", ["UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", "UPDATE civ.users SET onboarding_state = 'in_progress' WHERE id = :u"])
async def test_a_frozen_group_or_an_unfinished_onboarding_is_not_visited(inv_env, update_sql):
    await seed(inv_env, burn=5, last_burn_on=date.today() - timedelta(days=10))
    await rows(inv_env, update_sql)
    assert inv_env.user_id not in await due(inv_env)


# ================================================================== the tool: recording what the user reported


async def test_a_purchase_creates_the_item_with_its_first_movement(inv_env):
    [done] = await update(inv_env, BOUGHT, icall())
    assert (done.name, done.created, done.quantity, done.unit) == ("Креатин", True, Decimal(500), "г")
    qty, unit, burn, last_burn_on, reminded, token = await stock(inv_env)
    assert (qty, unit, burn, reminded, token) == (Decimal(500), "г", Decimal(0), None, None) and last_burn_on == date.today() - timedelta(days=1)
    assert await moves(inv_env) == [(Decimal("500.000"), Decimal("500.000"), "manual_restock", True)]
    assert (await rows(inv_env, "SELECT last_restocked_at IS NOT NULL FROM civ.inventory_items WHERE user_id = :u")) == [(True,)]
    await assert_stock_is_the_sum_of_movements(inv_env)


async def test_restock_consume_and_set_move_the_stock_and_each_leaves_a_movement(inv_env):
    await update(inv_env, "Купил креатин 500г", icall())
    await update(inv_env, "Купил ещё креатин 250г", icall(quantity=250))
    [used] = await update(inv_env, "Использовал креатин 100г", icall(quantity=100, action="consume"))
    assert used.quantity == 650 and used.delta == -100
    [fixed] = await update(inv_env, "Пересчитал: креатина осталось 600г", icall(quantity=600, action="set"))
    assert fixed.quantity == 600 and fixed.delta == -50
    assert [(m[0], m[1], m[2]) for m in await moves(inv_env)] == [
        (500, 500, "manual_restock"), (250, 750, "manual_restock"), (-100, 650, "correction"), (-50, 600, "correction"),
    ]  # fmt: skip
    await assert_stock_is_the_sum_of_movements(inv_env)


async def test_consuming_more_than_there_is_stops_at_zero_and_records_what_was_really_taken(inv_env):
    await seed(inv_env, qty=30)
    [done] = await update(inv_env, "Использовал креатин 100г", icall(quantity=100, action="consume"))
    assert done.quantity == 0 and done.delta == -30
    assert (await moves(inv_env))[-1][:2] == (Decimal("-30.000"), Decimal("0.000"))
    await assert_stock_is_the_sum_of_movements(inv_env)


async def test_it_ran_out_sets_the_stock_to_zero_without_a_number_in_the_message(inv_env):
    await seed(inv_env, "Протеин", 400)
    [done] = await update(inv_env, "закончился протеин", icall("протеин", 0, "г", "set"))
    assert done.quantity == 0 and (await stock(inv_env, "Протеин"))[0] == 0
    assert (await moves(inv_env, "Протеин"))[-1][:2] == (Decimal("-400.000"), Decimal("0.000"))


async def test_it_ran_out_for_something_never_tracked_creates_nothing(inv_env):
    assert await update(inv_env, "закончился протеин", icall("протеин", 0, "г", "set")) == []
    assert await rows(inv_env, "SELECT count(*) FROM civ.inventory_items WHERE user_id = :u") == [(0,)]


async def test_kilograms_are_stored_in_grams(inv_env):
    [done] = await update(inv_env, "заказал протеин 1,5 кг", icall("протеин", 1.5, "кг"))
    assert done.quantity == 1500 and (await stock(inv_env, "Протеин"))[:2] == (Decimal(1500), "г")


async def test_the_daily_burn_the_user_names_is_stored_and_a_later_message_without_one_keeps_it(inv_env):
    await update(inv_env, "Купил креатин 500г, пью по 5 г в день", icall(daily_burn=5))
    assert (await stock(inv_env))[2] == 5
    await update(inv_env, "Купил ещё креатин 100г", icall(quantity=100))
    assert (await stock(inv_env))[:3] == (Decimal(600), "г", Decimal(5))


async def test_a_new_burn_with_an_unchanged_stock_is_still_recorded_but_without_a_movement(inv_env):
    await seed(inv_env, qty=600, burn=5)
    [done] = await update(inv_env, "осталось 600 г креатина, пью по 10 г в день", icall(quantity=600, action="set", daily_burn=10))
    assert done.daily_burn == 10 and (await stock(inv_env))[2] == 10 and len(await moves(inv_env)) == 1  # only the seeding movement


async def test_nothing_changed_is_not_a_change(inv_env):
    await seed(inv_env, qty=600)
    assert await update(inv_env, "осталось 600 г креатина", icall(quantity=600, action="set")) == []


async def test_the_synonyms_of_a_catalog_item_and_inflected_names_find_the_same_item(inv_env):
    await update(inv_env, "Купил омегу 60 капсул", icall("омега", 60, "капсул"))
    await update(inv_env, "купил рыбий жир 30 капсул", icall("рыбий жир", 30, "капсул"))
    await update(inv_env, "купил омеги 10 капсул", icall("Омега-3", 10, "капсул"))
    assert await rows(inv_env, "SELECT name, current_quantity FROM civ.inventory_items WHERE user_id = :u") == [("Омега-3", Decimal(100))]
    await update(inv_env, "купил 2 шампуня", icall("шампунь", 2, "шт"))
    await update(inv_env, "купил ещё 3 шампуня", icall("шампуня", 3, "шт"))
    assert await rows(inv_env, "SELECT name, current_quantity FROM civ.inventory_items WHERE user_id = :u AND name <> 'Омега-3'") == [("Шампунь", Decimal(5))]


async def test_a_new_custom_item_must_be_the_users_own_word(inv_env):
    assert await update(inv_env, "купил 2 шт", icall("гель для душа", 2, "шт")) == []  # the model's name is not in the message
    [done] = await update(inv_env, "купил стиральный порошок 2 шт", icall("стиральный порошок", 2, "шт"))
    assert done.name == "Стиральный порошок"


async def test_a_call_in_another_unit_than_the_item_is_kept_in_is_skipped(inv_env):
    await seed(inv_env, qty=500, unit="г")
    assert await update(inv_env, "купил креатин 10 капсул", icall(quantity=10, unit="капсул")) == []
    assert (await stock(inv_env))[:2] == (Decimal(500), "г")


async def test_consume_cannot_start_an_item(inv_env):
    assert await update(inv_env, "выпил 2 капсулы омеги", icall("омега", 2, "капсул", "consume")) == []
    assert await rows(inv_env, "SELECT count(*) FROM civ.inventory_items WHERE user_id = :u") == [(0,)]


async def test_an_invented_number_changes_nothing(inv_env):
    assert await update(inv_env, BOUGHT, icall(quantity=800)) == []
    assert await rows(inv_env, "SELECT count(*) FROM civ.inventory_items WHERE user_id = :u") == [(0,)]


async def test_one_message_may_change_several_items_and_a_repeated_item_collapses(inv_env):
    done = await update(inv_env, "купил креатин 500г и протеин 1кг", icall(), icall("протеин", 1, "кг"), icall("Креатин", 500))
    assert [d.name for d in done] == ["Креатин", "Протеин"] and await rows(inv_env, "SELECT count(*) FROM civ.inventory_movements WHERE user_id = :u") == [(2,)]


async def test_the_calls_per_message_are_capped(inv_env):
    said = "купил: " + ", ".join(f"товар{n} {n + 1} шт" for n in range(4))
    calls = [icall(f"товар{n}", n + 1, "шт") for n in range(4)]
    done = await update(inv_env, said, *calls, inventory_max_calls=2)
    assert len(done) == 2


async def test_the_item_cap_stops_new_items_but_not_known_ones(inv_env):
    await update(inv_env, BOUGHT, icall(), inventory_max_items=1)
    assert await update(inv_env, "купил протеин 1кг", icall("протеин", 1, "кг"), inventory_max_items=1) == []
    assert len(await update(inv_env, "купил креатин 100г", icall(quantity=100), inventory_max_items=1)) == 1


async def test_a_replayed_message_changes_the_stock_once(inv_env):
    message_id = await new_message(inv_env.owner_engine, inv_env.user_id, BOUGHT)
    turn = UserTurn(message_id, BOUGHT)
    assert len(await engine_of(inv_env).update(inv_env.user_id, turn, [icall()])) == 1
    assert await engine_of(inv_env).update(inv_env.user_id, turn, [icall()]) == []
    assert (await stock(inv_env))[0] == 500 and len(await moves(inv_env)) == 1
    await assert_stock_is_the_sum_of_movements(inv_env)


async def test_restocking_to_a_healthy_level_clears_an_open_reminder(inv_env):
    await seed(inv_env, qty=10, burn=5, reminded=datetime.now(UTC), token=uuid.uuid4())
    await update(inv_env, "Купил креатин 500г", icall())
    _, _, _, _, reminded, token = await stock(inv_env)
    assert reminded is None and token is None
    await seed(inv_env, "Протеин", 10, burn=5, reminded=datetime.now(UTC), token=uuid.uuid4())
    await update(inv_env, "Купил протеин 5г", icall("протеин", 5))  # still low: the reminder (and its cooldown) stays
    assert (await stock(inv_env, "Протеин"))[4] is not None


async def test_another_tenants_items_are_never_touched(inv_env, owner_engine):
    stranger = await new_user(owner_engine)
    theirs = await seed(inv_env, "Креатин", 70, user=stranger)
    await update(inv_env, BOUGHT, icall())
    assert (await owner_exec(owner_engine, "SELECT current_quantity FROM civ.inventory_items WHERE id = :i", i=theirs)) == [(Decimal(70),)]


async def test_a_database_failure_records_nothing_and_never_raises(inv_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Inventory, "_apply", boom)
    assert await update(inv_env, BOUGHT, icall()) == []
    assert await rows(inv_env, "SELECT count(*) FROM civ.inventory_items WHERE user_id = :u") == [(0,)]


async def test_a_failure_in_the_middle_rolls_the_whole_message_back(inv_env, monkeypatch):
    calls = {"n": 0}
    real = Inventory._canonical

    def flaky(name: str, said_key: str):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("second item failed")
        return real(name, said_key)

    monkeypatch.setattr(Inventory, "_canonical", staticmethod(flaky))
    assert await update(inv_env, "купил креатин 500г и протеин 1кг", icall(), icall("протеин", 1, "кг")) == []
    assert await rows(inv_env, "SELECT count(*) FROM civ.inventory_items WHERE user_id = :u") == [(0,)]
    assert await rows(inv_env, "SELECT count(*) FROM civ.inventory_movements WHERE user_id = :u") == [(0,)]


# ================================================================== the daily job: burning the daily rate


async def test_the_burn_of_the_days_since_the_last_run_is_applied_once(inv_env):
    await seed(inv_env, qty=100, burn=10, last_burn_on=date(2026, 10, 5))  # yesterday (10-07) and 10-06 are due
    await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW)
    qty, _, _, last_burn_on, *_ = await stock(inv_env)
    assert qty == 80 and last_burn_on == date(2026, 10, 7)
    assert (await moves(inv_env))[-1] == (Decimal("-20.000"), Decimal("80.000"), "daily_burn", False)
    await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW)  # a second run in the same morning: nothing more
    assert (await stock(inv_env))[0] == 80 and len(await moves(inv_env)) == 2
    await assert_stock_is_the_sum_of_movements(inv_env)


async def test_the_next_morning_burns_exactly_one_more_day(inv_env):
    await seed(inv_env, qty=100, burn=10, last_burn_on=date(2026, 10, 6))
    await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW)
    assert (await stock(inv_env))[0] == 90
    await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW + timedelta(days=1))
    assert (await stock(inv_env))[0] == 80


async def test_a_day_the_user_corrected_the_stock_by_hand_is_not_burned(inv_env):
    await seed(inv_env, qty=100, burn=10, last_burn_on=date(2026, 10, 5))
    await movement(inv_env, await _item(inv_env), 0.5, "correction", 100.5, created=datetime(2026, 10, 7, 12, 0, tzinfo=UTC))  # yesterday: the user said how much is left
    await rows(inv_env, "UPDATE civ.inventory_items SET current_quantity = 100.5 WHERE user_id = :u")
    await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW)
    assert (await stock(inv_env))[0] == Decimal("90.5")  # only 10-06 burned


async def test_a_manual_restock_also_skips_its_day_but_an_old_burn_movement_does_not(inv_env):
    item = await seed(inv_env, qty=100, burn=10, last_burn_on=date(2026, 10, 5))
    await movement(inv_env, item, 10, "manual_restock", 110, created=datetime(2026, 10, 6, 8, 0, tzinfo=UTC))
    await movement(inv_env, item, -10, "daily_burn", 100, created=datetime(2026, 10, 7, 8, 0, tzinfo=UTC))  # a burn is not a hand correction
    await rows(inv_env, "UPDATE civ.inventory_items SET current_quantity = 100 WHERE user_id = :u")
    await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW)
    assert (await stock(inv_env))[0] == 90  # 10-06 skipped (restock), 10-07 burned


async def test_the_local_day_of_the_correction_is_the_tenants_not_utc(inv_env):
    await rows(inv_env, "UPDATE civ.users SET timezone = 'Asia/Tokyo' WHERE id = :u")
    item = await seed(inv_env, qty=100, burn=10, last_burn_on=date(2026, 10, 6))
    # 2026-10-07 16:00 UTC is 01:00 on 10-08 in Tokyo: a correction made on "today" locally, after the day that is burned (10-07 Tokyo)
    await movement(inv_env, item, 1, "correction", 101, created=datetime(2026, 10, 7, 16, 0, tzinfo=UTC))
    await rows(inv_env, "UPDATE civ.inventory_items SET current_quantity = 101 WHERE user_id = :u")
    await engine_of(inv_env).claim_due(inv_env.user_id, now=datetime(2026, 10, 8, 0, 30, tzinfo=UTC))  # 09:30 on 10-08 in Tokyo; the day to burn is 10-07
    assert (await stock(inv_env))[0] == 91


async def _item(env, name: str = "Креатин") -> uuid.UUID:
    return (await rows(env, "SELECT id FROM civ.inventory_items WHERE user_id = :u AND name = :n", n=name))[0][0]


async def test_the_burn_never_takes_the_stock_below_zero(inv_env):
    await seed(inv_env, qty=5, burn=10, last_burn_on=date(2026, 10, 5))
    await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW)
    assert (await stock(inv_env))[0] == 0 and (await moves(inv_env))[-1][:2] == (Decimal("-5.000"), Decimal("0.000"))
    await assert_stock_is_the_sum_of_movements(inv_env)


async def test_a_long_outage_burns_only_the_catch_up_window(owner_engine, db):
    env = await build_bot_env(owner_engine, db, inventory_enabled=True, inventory_max_catchup_days=7)
    try:
        await owner_exec(owner_engine, "UPDATE civ.users SET timezone = 'UTC' WHERE id = :u", u=env.user_id)
        await seed(env, qty=1000, burn=10, last_burn_on=date(2026, 9, 1))
        await env.orchestrator.inventory.claim_due(env.user_id, now=NOW)
        assert (await stock(env))[0] == 930 and (await stock(env))[3] == date(2026, 10, 7)
    finally:
        await env.bot.session.close()


async def test_items_without_a_burn_rate_are_left_alone(inv_env):
    await seed(inv_env, qty=100, burn=0, last_burn_on=date(2026, 10, 1))
    assert await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW) is None
    assert (await stock(inv_env))[:4] == (Decimal(100), "г", Decimal(0), date(2026, 10, 1))


async def test_nothing_happens_before_the_local_hour(inv_env):
    await seed(inv_env, qty=100, burn=10, last_burn_on=date(2026, 10, 5))
    assert await engine_of(inv_env).claim_due(inv_env.user_id, now=datetime(2026, 10, 8, 7, 59, tzinfo=UTC)) is None
    assert (await stock(inv_env))[0] == 100
    await rows(inv_env, "UPDATE civ.users SET timezone = 'Asia/Tokyo' WHERE id = :u")  # 07:59 UTC is 16:59 in Tokyo: the morning there is over, the job runs
    await engine_of(inv_env).claim_due(inv_env.user_id, now=datetime(2026, 10, 8, 7, 59, tzinfo=UTC))
    assert (await stock(inv_env))[0] < 100


@pytest.mark.parametrize("update_sql", ["UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", "UPDATE civ.users SET onboarding_state = 'in_progress' WHERE id = :u"])
async def test_a_frozen_group_or_an_unfinished_onboarding_burns_nothing(inv_env, update_sql):
    await seed(inv_env, qty=100, burn=10, last_burn_on=date(2026, 10, 5))
    await rows(inv_env, update_sql)
    assert await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW) is None and (await stock(inv_env))[0] == 100


# ---------------------------------------------------------------- the reminders


async def test_a_reminder_is_claimed_when_the_stock_runs_low_with_the_text_of_the_task(inv_env):
    await seed(inv_env, qty=70, burn=10, last_burn_on=date(2026, 10, 5))  # after the burn of 2 days: 50 left = 5 days = the threshold
    claim = await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW)
    assert claim is not None and claim.chat_id == inv_env.chat_id and len(claim.reminders) == 1
    [reminder] = claim.reminders
    assert reminder.text == "⚠️ Креатин: осталось на 5 дней (~50 г). Пора заказать новую пачку!"
    _, _, _, _, reminded, token = await stock(inv_env)
    assert reminded == NOW and token == reminder.token


async def test_a_well_stocked_item_gets_no_reminder(inv_env):
    await seed(inv_env, qty=1000, burn=10, last_burn_on=date(2026, 10, 5))
    assert await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW) is None  # burned (980 left), nothing to remind about
    assert (await stock(inv_env))[0] == 980 and (await stock(inv_env))[4] is None


async def test_no_second_reminder_within_three_days_and_a_new_one_after(inv_env):
    await seed(inv_env, qty=100, burn=10, threshold=20, last_burn_on=date(2026, 10, 7))
    first = await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW)
    assert first is not None
    assert await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW + timedelta(days=1)) is None  # the burn went on, the reminder did not
    assert await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW + timedelta(days=2, hours=23)) is None
    third = await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW + timedelta(days=3))
    assert third is not None and third.reminders[0].token != first.reminders[0].token  # a new reminder, a new card
    assert (await stock(inv_env))[0] == 70  # three more days burned along the way


async def test_the_claim_is_one_per_item_even_when_ticks_overlap(inv_env):
    await seed(inv_env, qty=40, burn=10, last_burn_on=date(2026, 10, 7))
    results = await asyncio.gather(*(engine_of(inv_env).claim_due(inv_env.user_id, now=NOW) for _ in range(5)))
    assert sum(1 for r in results if r is not None) == 1
    assert (await stock(inv_env))[0] == 40 and len(await moves(inv_env)) == 1  # nothing was burned twice either


async def test_quiet_hours_postpone_the_reminder_but_not_the_burn(inv_env):
    await rows(inv_env, "UPDATE civ.users SET quiet_hours_start = '07:00', quiet_hours_end = '10:00' WHERE id = :u")
    await seed(inv_env, qty=40, burn=10, last_burn_on=date(2026, 10, 5))
    assert await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW) is None  # 09:00 is quiet
    assert (await stock(inv_env))[0] == 20 and (await stock(inv_env))[4] is None
    claim = await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW.replace(hour=10, minute=5))
    assert claim is not None and len(claim.reminders) == 1 and (await stock(inv_env))[0] == 20  # no second burn


async def test_send_due_sends_the_card_with_the_button_to_the_main_chat(inv_env):
    item = await seed(inv_env, qty=40, burn=10, last_burn_on=date(2026, 10, 7))
    assert await engine_of(inv_env).send_due(inv_env.user_id, now=NOW) == 1
    [card] = [m for m in inv_env.session.sent() if m.chat_id == inv_env.chat_id]
    assert card.text == "⚠️ Креатин: осталось на 4 дня (~40 г). Пора заказать новую пачку!" and card.message_thread_id is None
    [[button]] = card.reply_markup.inline_keyboard
    assert button.text == "Купил / Пополнил" and inventory.parse_callback(button.callback_data) == (item, (await stock(inv_env))[5].hex[:12])
    assert await engine_of(inv_env).send_due(inv_env.user_id, now=NOW) == 0 and len([m for m in inv_env.session.sent()]) == 1


async def test_a_failed_send_gives_the_claim_back_and_the_next_tick_sends(inv_env, monkeypatch):
    await seed(inv_env, qty=40, burn=10, last_burn_on=date(2026, 10, 7))
    real = Bot.send_message

    async def down(self, *a: Any, **k: Any):
        raise ConnectionError("telegram is down")

    monkeypatch.setattr(Bot, "send_message", down)
    with pytest.raises(ConnectionError):
        await engine_of(inv_env).send_due(inv_env.user_id, now=NOW)
    _, _, _, _, reminded, token = await stock(inv_env)
    assert reminded is None and token is None  # the claim went back
    monkeypatch.setattr(Bot, "send_message", real)
    assert await engine_of(inv_env).send_due(inv_env.user_id, now=NOW) == 1


async def test_a_failure_on_the_second_reminder_keeps_the_first_and_releases_the_rest(inv_env, monkeypatch):
    await seed(inv_env, "Креатин", 40, burn=10, last_burn_on=date(2026, 10, 7))
    await seed(inv_env, "Протеин", 30, burn=10, last_burn_on=date(2026, 10, 7))
    real = Bot.send_message
    calls = {"n": 0}

    async def flaky(self, *a: Any, **k: Any):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ConnectionError("telegram hiccup")
        return await real(self, *a, **k)

    monkeypatch.setattr(Bot, "send_message", flaky)
    with pytest.raises(ConnectionError):
        await engine_of(inv_env).send_due(inv_env.user_id, now=NOW)
    assert (await stock(inv_env, "Креатин"))[4] is not None  # sent: stays sent
    assert (await stock(inv_env, "Протеин"))[4] is None  # given back


# ---------------------------------------------------------------- the Beat entry points


async def test_the_dispatcher_offers_exactly_the_tenants_with_something_due(inv_env, db):
    await seed(inv_env, burn=5, last_burn_on=date.today() - timedelta(days=10))
    offered: list[uuid.UUID] = []
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, inventory_dispatch_limit=100_000, inventory_local_hour=0))
    await tasks.dispatch_inventory_async(rt, offered.append)  # type: ignore[arg-type]
    assert inv_env.user_id in offered


async def test_the_dispatcher_does_nothing_when_the_feature_is_off(inv_env, db):
    await seed(inv_env, burn=5, last_burn_on=date.today() - timedelta(days=10))
    offered: list[uuid.UUID] = []
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, inventory_enabled=False))
    assert await tasks.dispatch_inventory_async(rt, offered.append) == 0 and offered == []  # type: ignore[arg-type]


async def test_a_queue_failure_for_one_tenant_is_reported_after_the_others_were_offered(inv_env, db):
    await seed(inv_env, burn=5, last_burn_on=date.today() - timedelta(days=10))
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, inventory_dispatch_limit=100_000, inventory_local_hour=0))

    def broken(tenant_id: uuid.UUID) -> None:
        raise ConnectionError("broker is down")

    with pytest.raises(tasks.DispatchError):
        await tasks.dispatch_inventory_async(rt, broken)  # type: ignore[arg-type]


async def test_the_worker_task_runs_the_burn_and_sends_the_reminder(inv_env, db):
    await seed(inv_env, qty=40, burn=10, last_burn_on=date.today() - timedelta(days=3))
    rt = SimpleNamespace(db=db, settings=Settings(_env_file=None, inventory_local_hour=0))
    sent = await tasks.check_inventory_burn_async(rt, inv_env.bot, inv_env.user_id)  # type: ignore[arg-type]
    assert sent in (0, 1)  # (a real clock: the quiet hours of the test machine decide) - the burn itself is not optional
    assert (await stock(inv_env))[0] <= 20 and (await stock(inv_env))[3] == (datetime.now(UTC) - timedelta(days=1)).date()


def test_the_beat_schedule_has_the_daily_inventory_entry():
    from ai_civ import celery_app

    entry = celery_app.app.conf.beat_schedule["inventory-dispatch"]
    assert entry["task"] == "ai_civ.dispatch_inventory"
    assert {"ai_civ.dispatch_inventory", "ai_civ.check_inventory_burn"} <= set(celery_app.app.tasks)


# ---------------------------------------------------------------- the button


async def claimed(env, *seed_args: Any, **seed_kw: Any) -> tuple[uuid.UUID, str]:
    item = await seed(env, *seed_args, **seed_kw)
    claim = await engine_of(env).claim_due(env.user_id, now=NOW)
    assert claim is not None
    return item, inventory.callback_data(item, claim.reminders[0].token)


async def test_the_button_restocks_by_the_size_of_the_last_restock_once(inv_env):
    _, data = await claimed(inv_env, qty=40, burn=10, last_burn_on=date(2026, 10, 7), restock=500)
    await press(inv_env, data)
    assert (await stock(inv_env))[0] == 540 and (await moves(inv_env))[-1] == (Decimal("500.000"), Decimal("540.000"), "manual_restock", False)
    [edit] = inv_env.session.edits()
    assert edit.text == "✅ Креатин: пополнил на +500 г → осталось 540 г" and edit.reply_markup is None
    assert inv_env.session.callback_answers()[-1].text == texts.INVENTORY_TOAST["restocked"]
    await press(inv_env, data)  # a second press
    assert (await stock(inv_env))[0] == 540 and inv_env.session.callback_answers()[-1].text == texts.INVENTORY_TOAST["settled"]
    assert inv_env.session.edits()[-1].text == texts.INVENTORY_STALE
    await assert_stock_is_the_sum_of_movements(inv_env)


async def test_six_simultaneous_presses_restock_once(inv_env):
    item, data = await claimed(inv_env, qty=40, burn=10, last_burn_on=date(2026, 10, 7), restock=500)
    _, prefix = inventory.parse_callback(data)
    results = await asyncio.gather(*(engine_of(inv_env).press(inv_env.user_id, item, prefix) for _ in range(6)))
    assert sorted(r.outcome for r in results) == ["restocked"] + ["settled"] * 5 and (await stock(inv_env))[0] == 540


async def test_the_button_clears_the_cooldown_only_when_the_stock_is_healthy_again(inv_env):
    _, data = await claimed(inv_env, qty=40, burn=10, last_burn_on=date(2026, 10, 7), restock=500)
    await press(inv_env, data)
    _, _, _, _, reminded, token = await stock(inv_env)
    assert reminded is None and token is None  # 540 g is 54 days: healthy, the next shortage deserves a fresh reminder
    _, data2 = await claimed(inv_env, "Протеин", 40, burn=10, last_burn_on=date(2026, 10, 7), restock=10)
    await press(inv_env, data2)
    _, _, _, _, reminded, token = await stock(inv_env, "Протеин")
    assert reminded is not None and token is None  # 50 g is 5 days: still at the threshold, the cooldown goes on but this card is answered


async def test_a_press_without_any_earlier_purchase_asks_for_the_amount_and_keeps_the_button(inv_env):
    item, data = await claimed(inv_env, qty=40, burn=10, last_burn_on=date(2026, 10, 7))
    await press(inv_env, data)
    assert (await stock(inv_env))[0] == 40 and inv_env.session.callback_answers()[-1].text == texts.INVENTORY_TOAST["no_amount"]
    assert inv_env.session.edits() == [] and (await stock(inv_env))[5] is not None  # the reminder stays open: the user can write the amount
    await update(inv_env, "купил креатин 500г", icall())  # ... and now the button knows the size
    assert (await stock(inv_env))[5] is None  # (the purchase made the stock healthy: the reminder is closed)


async def test_an_old_reminder_card_is_stale_after_a_newer_one(inv_env):
    item, old = await claimed(inv_env, qty=100, burn=10, threshold=20, last_burn_on=date(2026, 10, 7), restock=500)
    newer = await engine_of(inv_env).claim_due(inv_env.user_id, now=NOW + timedelta(days=3))
    assert newer is not None
    await press(inv_env, old)
    assert inv_env.session.callback_answers()[-1].text == texts.INVENTORY_TOAST["stale"] and (await stock(inv_env))[0] == 70
    await press(inv_env, inventory.callback_data(item, newer.reminders[0].token))
    assert (await stock(inv_env))[0] == 570


async def test_a_stranger_cannot_use_my_button(inv_env, owner_engine):
    item, data = await claimed(inv_env, qty=40, burn=10, last_burn_on=date(2026, 10, 7), restock=500)
    stranger = await new_user(owner_engine)
    _, prefix = inventory.parse_callback(data)
    assert (await engine_of(inv_env).press(stranger, item, prefix)).outcome == "gone"
    assert (await stock(inv_env))[0] == 40


async def test_a_garbage_button_changes_nothing(inv_env):
    await press(inv_env, "iv:not-a-uuid:zzzz")
    assert inv_env.session.callback_answers()[-1].text == texts.PROPOSAL_UNKNOWN_BUTTON
    await press(inv_env, f"iv:{uuid.uuid4()}:{'a' * 12}")  # an item that does not exist
    assert inv_env.session.callback_answers()[-1].text == texts.INVENTORY_TOAST["gone"]


# ================================================================== the whole flow through the Dispatcher


async def test_a_purchase_message_gets_the_function_and_the_reply_is_a_card_the_model_did_not_write(inv_env):
    env = inv_env
    await seed(env, "Протеин", 400)
    model_updates(env, icall(daily_burn=5, quantity=500))
    await env.feed("Купил креатин 500г, пью по 5 г в день")
    offered = env.chat.tool_calls[0]
    assert [t.name for t in offered["tools"]] == ["update_inventory"] and "## Inventory" in offered["system"] and "- Протеин (400 г)" in offered["system"]
    assert not any("записал" in t.lower() for t in says(env))  # the model's own words next to the call are never sent
    assert "Принято." in says(env)  # the reply was phrased again, with the note of what the system did
    assert "Inventory (made by the system)" in env.chat.calls[-1]["system"] and "Креатин" in env.chat.calls[-1]["system"]
    assert "📦 Креатин: +500 г → осталось 500 г (хватит примерно на 100 дней при 5 г/день)" in says(env)
    assert (await stock(env))[:3] == (Decimal(500), "г", Decimal(5))
    assert not any("📦" in (m["content"] or "") for m in await env.messages())  # the card is not a word of the assistant: not remembered as one


async def test_it_ran_out_through_the_chat(inv_env):
    env = inv_env
    await seed(env, "Протеин", 400)
    model_updates(env, icall("протеин", 0, "г", "set"))
    await env.feed("закончился протеин")
    assert "📦 Протеин: осталось 0 г" in says(env) and (await stock(env, "Протеин"))[0] == 0


async def test_an_invented_number_gives_no_card_and_the_models_claim_is_not_sent(inv_env):
    env = inv_env
    model_updates(env, icall(quantity=800), then="Не получилось записать.")
    await env.feed(BOUGHT)
    assert not any("📦" in t for t in says(env)) and await rows(env, "SELECT count(*) FROM civ.inventory_items WHERE user_id = :u") == [(0,)]
    assert not any("записал" in t.lower() for t in says(env))  # a function of ours was called and nothing ran: the words are not trusted


async def test_talk_without_a_cue_is_never_offered_the_function(inv_env):
    env = inv_env
    env.chat.responder = lambda system, turns: "Понял."
    for said in ("Купил хлеб", "Привет, мне 30 лет", "Хочу купить креатин"):
        await env.feed(said)
    assert env.chat.tool_calls == [] and all("## Inventory" not in c["system"] for c in env.chat.calls)


async def test_the_switch_turns_the_function_off_even_on_a_purchase(owner_engine, db):
    env = await build_bot_env(owner_engine, db, inventory_enabled=False)
    try:
        env.chat.responder = lambda system, turns: "Хорошо."
        await env.feed(BOUGHT)
        assert env.chat.tool_calls == []
    finally:
        await env.bot.session.close()


async def test_a_voice_report_is_recorded_like_a_typed_one(inv_env):
    env = inv_env
    env.chat.transcriber = lambda data, mime: BOUGHT
    model_updates(env, icall())
    await env.feed_voice()
    assert (await stock(env))[0] == 500


async def test_a_redelivered_update_records_once(inv_env):
    env = inv_env
    model_updates(env, icall())
    update_ = env.update(BOUGHT)
    await env.feed_update(update_)
    await env.feed_update(update_)
    assert (await stock(env))[0] == 500 and len(await moves(env)) == 1


async def test_a_database_failure_still_sends_the_reply_without_a_card(inv_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Inventory, "_apply", boom)
    env = inv_env
    model_updates(env, icall(), then="Не получилось, повторите.")
    await env.feed(BOUGHT)
    assert "Не получилось, повторите." in says(env) and not any("📦" in t for t in says(env))


async def test_the_whole_cycle_buy_burn_remind_press(inv_env):
    env = inv_env
    model_updates(env, icall(quantity=60, daily_burn=10))
    await env.feed("Купил креатин 60г, пью по 10 г в день")
    await rows(env, "UPDATE civ.inventory_items SET last_burn_on = :d WHERE user_id = :u", d=date(2026, 10, 5))
    await rows(env, "UPDATE civ.inventory_movements SET created_at = '2026-09-01 12:00+00' WHERE user_id = :u")  # the purchase was long ago (the real clock is not NOW)
    assert await engine_of(env).send_due(env.user_id, now=NOW) == 1  # 60 - 20 = 40 g = 4 days <= 5
    reminder = [m for m in env.session.sent() if m.reply_markup is not None][-1]
    assert reminder.text == "⚠️ Креатин: осталось на 4 дня (~40 г). Пора заказать новую пачку!"
    await press(env, reminder.reply_markup.inline_keyboard[0][0].callback_data)
    assert (await stock(env))[0] == 100 and env.session.edits()[-1].text == "✅ Креатин: пополнил на +60 г → осталось 100 г"
    await assert_stock_is_the_sum_of_movements(env)


def test_the_function_name_is_unique_in_the_production_registry(inv_env):
    names = [name for tool in inv_env.orchestrator.tools.tools for name in tool.functions]
    assert names.count("update_inventory") == 1 and len(names) == len(set(names))

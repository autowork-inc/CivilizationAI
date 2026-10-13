"""v2.0, PR 4.2: the recipe generator. The points under test: the model is OFFERED ``suggest_recipe`` only on an explicit request found by CODE in the user's own
words or when the message carries a photo; the offer carries the user's food / health / preference facts (allergies are absolute) and the latest recipes that must
not be repeated; the recipe is validated by code and a repeat of a recent one is refused (again inside the transaction); the reply is a card written by code
with the button «🍳 Приготовлено!» (the model's own words next to the call are never sent); the press is one idempotent transaction that closes the history row
and pays +10 XP in ``body`` (``recipe:<recipe id>``, the daily cap holds). Real PostgreSQL (RLS, composite FK, CHECKs, the final-status trigger and the
column grants), the real aiogram Dispatcher on a fake Telegram wire, a fake Gemini whose "model" decides in the test what to call."""

from __future__ import annotations

import random

import asyncio
import json
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.exc import DBAPIError, IntegrityError

from ai_civ.bot import texts, xp
from ai_civ.bot.tools import UserTurn, recipes
from ai_civ.bot.tools.recipes import Recent, RecipeContext, Recipes, is_repeat, parse_recipe, wants_recipe
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_fact, new_message, new_user, owner_exec, tenant_exec
from tests.test_attachments_pure import JPEG, PDF
from tests.tg_fakes import make_callback_update

DB_ERRORS = (IntegrityError, DBAPIError)
ASK = "Придумай рецепт из яиц и сыра"


def good(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "title": "Омлет с сыром",
        "prep_time_minutes": 10,
        "ingredients": ["Яйца — 3 шт", "Сыр — 50 г", "Соль"],
        "steps": ["Взбейте яйца с солью", "Обжарьте на сковороде 3 минуты", "Посыпьте сыром"],
        "nutrition_focus": ["белок", "кальций"],
    }
    args.update(over)
    return args


def rcall(**over: Any) -> ToolCall:
    return ToolCall(recipes.TOOL_NAME, good(**over))


# ================================================================== pure: the request check, the tool, the call


@pytest.mark.parametrize(
    "said",
    [
        "Что приготовить на ужин?",
        "что бы приготовить из курицы",
        "Придумай рецепт из яиц и сыра",
        "дай рецепт борща",
        "Подскажи рецепт",
        "Что мне поесть после тренировки?",
        "что на обед",
        "Приготовить что-нибудь из риса и грибов",
        "Any recipe with eggs?",
        "what should I cook tonight",
        "что можно сготовить из кабачка",
    ],
)
def test_an_explicit_request_for_a_dish_is_a_marker(said):
    assert wants_recipe(said)


@pytest.mark.parametrize(
    "said",
    [
        "Я приготовил борщ, вышло вкусно",
        "Купил продукты на неделю",
        "Хочу похудеть",
        "Что нового?",
        "Расскажи про историю Рима",
        "",
    ],
)
def test_ordinary_talk_is_not_a_marker(said):
    assert not wants_recipe(said)


def test_the_tool_is_plain_json_and_needs_the_whole_recipe():
    schema = recipes.TOOL.parameters
    assert recipes.TOOL.name == "suggest_recipe" == recipes.TOOL_NAME
    assert set(schema["required"]) == {"title", "prep_time_minutes", "ingredients", "steps", "nutrition_focus"}
    assert schema["properties"]["ingredients"]["type"] == "array" and schema["properties"]["steps"]["type"] == "array"
    assert json.dumps(schema)


def test_the_offer_carries_the_constraints_and_forbids_repeats_and_claims():
    from datetime import UTC, datetime

    ctx = RecipeContext(
        ("health.allergy: орехи", "preference.likes: острое"),
        (Recent("Омлет с сыром", ("яйца",), datetime(2026, 10, 1, tzinfo=UTC)),),
    )
    text = recipes.render_instructions(ctx)
    assert "health.allergy: орехи" in text and "ABSOLUTE" in text and "never include such an ingredient" in text
    assert "Омлет с сыром (2026-10-01)" in text and "FORBIDDEN to repeat" in text
    assert "never say it was saved" in text and "Do not write" in text
    empty = recipes.render_instructions(RecipeContext((), ()))
    assert "(no food, health or preference facts stored)" in empty and "FORBIDDEN" not in empty


def test_a_valid_call_becomes_a_recipe_and_the_card_is_written_by_code():
    spec = parse_recipe(good())
    assert spec is not None and (spec.title, spec.prep_time_minutes, spec.focus) == ("Омлет с сыром", 10, ("белок", "кальций"))
    card = recipes.card_text(recipes.view_of(spec)).split("\n")
    assert card[:2] == ["🍽 Омлет с сыром", "⏱ 10 мин"]
    assert "• Яйца — 3 шт" in card and "1. Взбейте яйца с солью" in card and "3. Посыпьте сыром" in card and card[-1] == "🎯 Фокус: белок, кальций"
    assert recipes.numbered(["a", "b"]) == "1. a\n2. b"


def test_whitespace_collapses_duplicates_go_and_a_float_minute_count_is_fine():
    spec = parse_recipe(good(prep_time_minutes=15.0, ingredients=["  Яйца   — 3 шт ", "яйца — 3 шт", "Сыр"], nutrition_focus=["белок", "", "белок", 5, "x" * 31]))
    assert spec is not None and spec.prep_time_minutes == 15 and spec.ingredients == ("Яйца — 3 шт", "Сыр") and spec.focus == ("белок",)


@pytest.mark.parametrize(
    "over",
    [
        {"title": ""},
        {"title": "я" * 81},
        {"title": "Омлет‮evil"},
        {"title": 5},
        {"prep_time_minutes": 0},
        {"prep_time_minutes": 601},
        {"prep_time_minutes": "10"},
        {"prep_time_minutes": True},
        {"prep_time_minutes": 10.5},
        {"ingredients": ["только одно"]},
        {"ingredients": "яйца, сыр"},
        {"ingredients": ["яйца", "я" * 101]},  # the long ingredient goes, one is left
        {"ingredients": [f"продукт {n}" for n in range(0)]},
        {"steps": ["один шаг"]},
        {"steps": "взбить и пожарить"},
        {"steps": ["взбить", "ш​аг"]},  # a hidden character: that step goes, one is left
        {"ingredients": None},
        {"steps": None},
    ],
)
def test_an_unusable_call_is_dropped(over):
    assert parse_recipe(good(**over)) is None


def test_a_missing_focus_is_only_empty_not_fatal():
    args = good()
    del args["nutrition_focus"]
    spec = parse_recipe(args)
    assert spec is not None and spec.focus == ()
    assert recipes.card_text(recipes.view_of(spec)).count("🎯") == 0


def test_a_recipe_that_does_not_fit_one_telegram_message_is_refused_not_cut():
    long_steps = ["ш" * 300] * 12  # 3600 characters of steps
    long_ingredients = ["и" * 100] * 25  # 2500 characters of ingredients
    assert parse_recipe(good(steps=long_steps, ingredients=[f"{n}" + x[1:] for n, x in enumerate(long_ingredients)])) is None
    assert parse_recipe(good(steps=[f"{n}" + "ш" * 299 for n in range(4)])) is not None


def test_counts_are_capped():
    spec = parse_recipe(good(ingredients=[f"продукт {n}" for n in range(40)], steps=[f"шаг {n}" for n in range(30)]))
    assert spec is not None and len(spec.ingredients) == 25 and len(spec.steps) == 12


def recent(title: str, *ingredients: str) -> Recent:
    from datetime import UTC, datetime

    return Recent(title, tuple(ingredients), datetime.now(UTC))


@pytest.mark.parametrize(
    ("title", "ingredients", "old", "expected"),
    [
        ("Омлет с сыром", ["яйца"], recent("Омлет с сыром", "яйца"), True),
        ("омлет  С СЫРОМ!", ["яйца"], recent("Омлет с сыром"), True),
        ("Омлет", ["яйца"], recent("Омлет с сыром"), True),  # contained
        ("Омлет с сыром и зеленью", ["яйца"], recent("Омлет с сыром"), True),  # contains
        ("Яичница", ["Яйца", "Сыр"], recent("Омлет", "сыр", "яйца"), True),  # the same ingredients under another name
        ("Паста карбонара", ["паста", "бекон"], recent("Омлет с сыром", "яйца", "сыр"), False),
        ("Ёжики", ["рис"], recent("Ежики", "рис", "фарш"), True),  # ё = е
        ("Борщ", ["свёкла"], recent("Щи", "капуста"), False),
        ("Борщ", ["свёкла"], None, False),
    ],
)
def test_a_repeat_is_the_same_dish_under_any_close_name(title, ingredients, old, expected):
    assert is_repeat(title, ingredients, [] if old is None else [old]) is expected


def test_the_callback_data_round_trips_and_fits_telegram():
    rid = uuid.uuid4()
    data = recipes.callback_data(rid)
    assert len(data.encode()) <= 64 and recipes.parse_callback(data) == rid
    for bad in ("", "rc:", f"rc:{rid}:x", f"rc:{rid}", f"sf:{rid}:c", "rc:not-a-uuid:c", f"rc:{rid}:c:x"):
        assert recipes.parse_callback(bad) is None
    markup = recipes.card_markup(rid)
    assert [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row] == [("🍳 Приготовлено!", data)]


def test_the_final_text_adds_the_xp_line_only_when_it_was_paid():
    view = recipes.view_of(parse_recipe(good()))
    paid = recipes.final_text(view, "awarded")
    assert paid.endswith("✅ Приготовлено\n⭐ +10 XP — тело") and recipes.card_text(view) in paid
    for code in ("daily_cap", "duplicate", "error", None):
        assert recipes.final_text(view, code).endswith("✅ Приготовлено")


# ================================================================== fixtures and helpers


@pytest_asyncio.fixture
async def recipe_env(owner_engine, db):
    """``bot_env`` with the generator on, as in production (the function is still offered only on a request or a photo)."""
    env = await build_bot_env(owner_engine, db, recipes_enabled=True)
    yield env
    await env.bot.session.close()


def engine_of(env) -> Recipes:
    return env.orchestrator.recipes


async def rows(env, sql: str, **kw: Any):
    return await owner_exec(env.owner_engine, sql, u=env.user_id, **kw)


async def suggest(env, said: str, *calls: ToolCall):
    message_id = await new_message(env.owner_engine, env.user_id, said)
    return await engine_of(env).suggest(env.user_id, UserTurn(message_id, said), calls)


async def seed(env, title: str, *, days_ago: float = 1, ingredients: tuple[str, ...] = ("что-то",), status: str = "suggested") -> uuid.UUID:
    [(rid,)] = await rows(
        env,
        "INSERT INTO civ.recipe_history (user_id, title, prep_time_minutes, ingredients_used, instructions, status, cooked_at, created_at) "
        "VALUES (:u, :t, 10, CAST(:i AS jsonb), '1. шаг', :s, CASE WHEN :s = 'cooked' THEN now() END, now() - make_interval(secs => :d)) RETURNING id",
        t=title,
        i=json.dumps(list(ingredients), ensure_ascii=False),
        s=status,
        d=days_ago * 86400,
    )
    return rid


COUNT = "SELECT count(*) FROM civ.recipe_history WHERE user_id = :u"
XP = "SELECT amount, category, source_ref FROM civ.xp_ledger WHERE user_id = :u ORDER BY created_at"


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


def card_messages(env, thread_id: int | None = None) -> list[Any]:
    return [
        m for m in env.session.sent()
        if m.chat_id == env.chat_id and m.message_thread_id == thread_id and m.reply_markup is not None
        and m.reply_markup.inline_keyboard[0][0].callback_data.startswith("rc:")
    ]  # fmt: skip


def model_suggests(env, *calls: ToolCall, text: str = "Сохранил рецепт в вашу книгу!", then: str = "Быстро и сытно.") -> None:
    """The fake model calls the function - and, as a real one might, ALSO claims it saved everything."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


async def press(env, data: str, *, tg: int | None = None, message_id: int = 777) -> None:
    await env.dispatcher.feed_update(
        env.bot, make_callback_update(data=data, user_id=tg or env.telegram_user_id, chat_id=env.chat_id, message_id=message_id)
    )


# ================================================================== the database


async def test_a_recipe_belongs_to_one_tenant_and_the_others_cannot_see_it(recipe_env, owner_engine, db):
    await seed(recipe_env, "Омлет")
    stranger = await new_user(owner_engine)
    assert await tenant_exec(db, stranger, "SELECT count(*) FROM civ.recipe_history") == [(0,)]
    assert await tenant_exec(db, recipe_env.user_id, "SELECT count(*) FROM civ.recipe_history") == [(1,)]


async def test_the_composite_foreign_key_refuses_another_tenants_message(recipe_env, owner_engine, db):
    stranger = await new_user(owner_engine)
    mine = await new_message(owner_engine, recipe_env.user_id, "моё сообщение")
    with pytest.raises(DB_ERRORS):
        await tenant_exec(
            db,
            stranger,
            "INSERT INTO civ.recipe_history (user_id, title, prep_time_minutes, ingredients_used, instructions, source_message_id) "
            "VALUES (:u, 'х', 5, '[\"а\"]', '1. б', :m)",
            u=stranger,
            m=mine,
        )


@pytest.mark.parametrize(
    "sets",
    [
        "title = E'два\\nряда'",
        "title = repeat('я', 81)",
        "title = ''",
        "prep_time_minutes = 0",
        "prep_time_minutes = 601",
        "ingredients_used = '[]'",
        "ingredients_used = '{\"a\": 1}'",
        "ingredients_used = (SELECT jsonb_agg(g) FROM generate_series(1, 31) g)",
        "instructions = ''",
        "instructions = repeat('я', 4001)",
        "nutrition_focus = '[\"1\",\"2\",\"3\",\"4\",\"5\"]'",
        "nutrition_focus = '{}'",
        "status = 'eaten'",
        "status = 'cooked'",  # cooked without cooked_at
        "cooked_at = now()",  # cooked_at without cooked
    ],
)
async def test_the_checks_refuse_a_bad_recipe(recipe_env, db, sets):
    base = {
        "title": "'Омлет'",
        "prep_time_minutes": "10",
        "ingredients_used": "'[\"яйца\"]'",
        "instructions": "'1. взбить'",
        "nutrition_focus": "'[]'",
        "status": "'suggested'",
        "cooked_at": "NULL",
    }
    column = sets.split(" = ", 1)[0]
    base[column] = sets.split(" = ", 1)[1]
    with pytest.raises(DB_ERRORS):
        await tenant_exec(
            db,
            recipe_env.user_id,
            "INSERT INTO civ.recipe_history (user_id, title, prep_time_minutes, ingredients_used, instructions, nutrition_focus, status, cooked_at) "
            f"VALUES (:u, {base['title']}, {base['prep_time_minutes']}, {base['ingredients_used']}, {base['instructions']}, {base['nutrition_focus']}, {base['status']}, {base['cooked_at']})",
            u=recipe_env.user_id,
        )


async def test_the_app_may_move_only_the_status_and_a_cooked_recipe_is_final(recipe_env, db, owner_engine):
    rid = await seed(recipe_env, "Омлет")
    u = recipe_env.user_id
    with pytest.raises(DB_ERRORS):  # the words of a recipe are never edited (column grants)
        await tenant_exec(db, u, "UPDATE civ.recipe_history SET title = 'Другое' WHERE id = :i", i=rid)
    await tenant_exec(db, u, "UPDATE civ.recipe_history SET status = 'cooked', cooked_at = now() WHERE id = :i", i=rid)
    with pytest.raises(DB_ERRORS, match="final"):  # ... and there is no way back
        await tenant_exec(db, u, "UPDATE civ.recipe_history SET status = 'suggested', cooked_at = NULL WHERE id = :i", i=rid)
    with pytest.raises(DB_ERRORS, match="final"):  # not even for the owner of the table
        await owner_exec(owner_engine, "UPDATE civ.recipe_history SET cooked_at = now() WHERE id = :i", i=rid)


async def test_one_message_suggests_at_most_one_recipe(recipe_env, db):
    message = await new_message(recipe_env.owner_engine, recipe_env.user_id, ASK)
    u = recipe_env.user_id
    insert = "INSERT INTO civ.recipe_history (user_id, title, prep_time_minutes, ingredients_used, instructions, source_message_id) VALUES (:u, :t, 5, '[\"а\"]', '1. б', :m)"
    await tenant_exec(db, u, insert, u=u, t="Первое", m=message)
    with pytest.raises(DB_ERRORS):
        await tenant_exec(db, u, insert, u=u, t="Второе", m=message)
    await tenant_exec(db, u, insert, u=u, t="Третье", m=None)  # a recipe with no message is not limited
    await tenant_exec(db, u, insert, u=u, t="Четвёртое", m=None)


# ================================================================== the engine: the context the model gets


async def test_the_context_reads_food_health_and_preference_facts_only(recipe_env, owner_engine):
    env, u = recipe_env, recipe_env.user_id
    await new_fact(owner_engine, u, "health.allergy", category="health", value="орехи")
    await new_fact(owner_engine, u, "preference.likes", category="preference", value="острое")
    await new_fact(owner_engine, u, "food.diet", category="other", value="без глютена")  # a "food." key of another category (what save_fact writes)
    await new_fact(owner_engine, u, "goal.1", category="goal", value="марафон")
    await new_fact(owner_engine, u, "identity.name", category="identity", value="Аня")
    await new_fact(owner_engine, u, "principle.x", category="principle", value="не лгать")
    old = await new_fact(owner_engine, u, "health.old", category="health", value="закрытая версия")
    await owner_exec(owner_engine, "UPDATE civ.core_facts SET valid_to = now() WHERE id = :i", i=old)
    ctx = await engine_of(env).context(u)
    assert set(ctx.facts) == {"health.allergy: орехи", "preference.likes: острое", "food.diet: без глютена"}
    assert ctx.recent == ()


async def test_another_tenants_facts_never_reach_the_context(recipe_env, owner_engine):
    stranger = await new_user(owner_engine)
    await new_fact(owner_engine, stranger, "health.allergy", category="health", value="чужая аллергия")
    assert (await engine_of(recipe_env).context(recipe_env.user_id)).facts == ()


async def test_the_context_is_capped(owner_engine, db):
    env = await build_bot_env(owner_engine, db, recipes_enabled=True, recipes_max_facts=2)
    try:
        for n in range(5):
            await new_fact(owner_engine, env.user_id, f"preference.k{n}", category="preference", value="v")
        assert len((await env.orchestrator.recipes.context(env.user_id)).facts) == 2
    finally:
        await env.bot.session.close()


async def test_the_context_has_the_last_5_recipes_of_the_last_14_days_newest_first(recipe_env):
    env = recipe_env
    await seed(env, "Слишком давно", days_ago=15)
    for n in range(7):
        await seed(env, f"Блюдо {n}", days_ago=7 - n)  # Блюдо 6 is the newest (1 day ago)
    titles = [r.title for r in (await engine_of(env).context(env.user_id)).recent]
    assert titles == ["Блюдо 6", "Блюдо 5", "Блюдо 4", "Блюдо 3", "Блюдо 2"]


# ================================================================== the engine: suggesting


async def test_a_suggestion_is_stored_with_numbered_steps_and_its_message(recipe_env):
    done = await suggest(recipe_env, ASK, rcall())
    assert isinstance(done, recipes.Suggested) and done.view.title == "Омлет с сыром"
    [row] = await rows(
        recipe_env,
        "SELECT title, prep_time_minutes, ingredients_used, instructions, nutrition_focus, status, cooked_at, source_message_id IS NOT NULL FROM civ.recipe_history WHERE user_id = :u",
    )
    assert row == (
        "Омлет с сыром",
        10,
        ["Яйца — 3 шт", "Сыр — 50 г", "Соль"],
        "1. Взбейте яйца с солью\n2. Обжарьте на сковороде 3 минуты\n3. Посыпьте сыром",
        ["белок", "кальций"],
        "suggested",
        None,
        True,
    )


@pytest.mark.parametrize(
    "over",
    [{"title": "Омлет с сыром"}, {"title": "Омлет"}, {"title": "Омлет с сыром и зеленью"}, {"title": "Яичница", "ingredients": ["сыр — 50 г", "Яйца — 3 шт", "соль"]}],
)
async def test_a_repeat_of_a_recent_recipe_is_refused_and_not_stored(recipe_env, over):
    await seed(recipe_env, "Омлет с сыром", days_ago=2, ingredients=("Яйца — 3 шт", "Сыр — 50 г", "Соль"))
    done = await suggest(recipe_env, ASK, rcall(**over))
    assert done == recipes.Refused("repeat") and await rows(recipe_env, COUNT) == [(1,)]


async def test_a_recipe_older_than_14_days_is_not_a_repeat(recipe_env):
    await seed(recipe_env, "Омлет с сыром", days_ago=15)
    assert isinstance(await suggest(recipe_env, ASK, rcall()), recipes.Suggested)


async def test_a_dish_that_fell_out_of_the_last_five_may_come_back(recipe_env):
    await seed(recipe_env, "Омлет с сыром", days_ago=6)
    for n in range(5):
        await seed(recipe_env, f"Блюдо {n}", days_ago=5 - n)
    assert isinstance(await suggest(recipe_env, ASK, rcall()), recipes.Suggested)


async def test_the_daily_limit_of_suggestions(owner_engine, db):
    env = await build_bot_env(owner_engine, db, recipes_enabled=True, recipes_max_per_day=1)
    try:
        assert isinstance(await suggest(env, ASK, rcall()), recipes.Suggested)
        assert await suggest(env, "ещё", rcall(title="Паста", ingredients=["паста", "соус"])) == recipes.Refused("limit")
        assert await rows(env, COUNT) == [(1,)]
    finally:
        await env.bot.session.close()


async def test_a_replayed_message_gets_its_own_recipe_back_and_never_a_second_one(recipe_env):
    message_id = await new_message(recipe_env.owner_engine, recipe_env.user_id, ASK)
    turn = UserTurn(message_id, ASK)
    first = await engine_of(recipe_env).suggest(recipe_env.user_id, turn, [rcall()])
    second = await engine_of(recipe_env).suggest(recipe_env.user_id, turn, [rcall(title="Совсем другое", ingredients=["а", "б"])])
    assert isinstance(first, recipes.Suggested) and second == first  # not "you have had this recently": the card of the first attempt, again
    assert await rows(recipe_env, COUNT) == [(1,)]


async def test_a_replayed_message_whose_recipe_was_cooked_gets_nothing(recipe_env):
    message_id = await new_message(recipe_env.owner_engine, recipe_env.user_id, ASK)
    turn = UserTurn(message_id, ASK)
    first = await engine_of(recipe_env).suggest(recipe_env.user_id, turn, [rcall()])
    await engine_of(recipe_env).cook(recipe_env.user_id, first.recipe_id)
    assert await engine_of(recipe_env).suggest(recipe_env.user_id, turn, [rcall()]) is None


async def test_unusable_and_foreign_calls_suggest_nothing(recipe_env):
    assert await suggest(recipe_env, ASK, rcall(steps=["один"])) is None
    assert await suggest(recipe_env, ASK, ToolCall("create_event", good())) is None
    assert await rows(recipe_env, COUNT) == [(0,)]


async def test_only_the_first_usable_call_of_a_message_counts(recipe_env):
    done = await suggest(recipe_env, ASK, rcall(steps=["один"]), rcall(title="Первое"), rcall(title="Второе", ingredients=["а", "б"]))
    assert isinstance(done, recipes.Suggested) and done.view.title == "Первое" and await rows(recipe_env, COUNT) == [(1,)]


async def test_a_database_failure_suggests_nothing_and_never_raises(recipe_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Recipes, "_insert", boom)
    assert await suggest(recipe_env, ASK, rcall()) is None
    assert await rows(recipe_env, COUNT) == [(0,)]


# ================================================================== the engine: cooking


async def test_cooking_closes_the_row_and_pays_10_xp_in_body_once(recipe_env):
    done = await suggest(recipe_env, ASK, rcall())
    result = await engine_of(recipe_env).cook(recipe_env.user_id, done.recipe_id)
    assert (result.outcome, result.xp_code, result.view.title) == ("cooked", "awarded", "Омлет с сыром")
    assert await rows(recipe_env, "SELECT status, cooked_at IS NOT NULL FROM civ.recipe_history WHERE user_id = :u") == [("cooked", True)]
    assert await rows(recipe_env, XP) == [(10, "body", f"recipe:{done.recipe_id}")]
    again = await engine_of(recipe_env).cook(recipe_env.user_id, done.recipe_id)
    assert (again.outcome, again.xp_code) == ("settled", "duplicate") and await rows(recipe_env, XP) == [(10, "body", f"recipe:{done.recipe_id}")]


async def test_six_simultaneous_presses_cook_once_and_pay_once(recipe_env):
    done = await suggest(recipe_env, ASK, rcall())
    results = await asyncio.gather(*(engine_of(recipe_env).cook(recipe_env.user_id, done.recipe_id) for _ in range(6)))
    assert sorted(r.outcome for r in results) == ["cooked"] + ["settled"] * 5
    assert sum(1 for r in results if r.xp_code == "awarded") == 1 and len(await rows(recipe_env, XP)) == 1


async def test_another_tenant_cannot_cook_my_recipe(recipe_env, owner_engine):
    done = await suggest(recipe_env, ASK, rcall())
    stranger = await new_user(owner_engine)
    assert (await engine_of(recipe_env).cook(stranger, done.recipe_id)).outcome == "gone"
    assert await rows(recipe_env, "SELECT status FROM civ.recipe_history WHERE user_id = :u") == [("suggested",)]
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.xp_ledger WHERE user_id = :u", u=stranger) == [(0,)]


async def test_an_unknown_recipe_is_gone(recipe_env):
    assert (await engine_of(recipe_env).cook(recipe_env.user_id, uuid.uuid4())).outcome == "gone"


async def test_the_daily_cap_of_100_xp_holds_and_the_recipe_is_still_cooked(recipe_env):
    await owner_exec(recipe_env.owner_engine, "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 100, 'focus', 'seed')", u=recipe_env.user_id)
    done = await suggest(recipe_env, ASK, rcall())
    result = await engine_of(recipe_env).cook(recipe_env.user_id, done.recipe_id)
    assert (result.outcome, result.xp_code) == ("cooked", "daily_cap")
    assert "XP" not in recipes.final_text(result.view, result.xp_code)


async def test_a_payment_that_failed_is_paid_by_the_next_press(recipe_env, monkeypatch):
    done = await suggest(recipe_env, ASK, rcall())
    real = xp.award
    calls = {"n": 0}

    async def flaky(*a: Any, **k: Any):
        calls["n"] += 1
        return "error" if calls["n"] == 1 else await real(*a, **k)

    monkeypatch.setattr(recipes.xp, "award", flaky)
    first = await engine_of(recipe_env).cook(recipe_env.user_id, done.recipe_id)
    assert (first.outcome, first.xp_code) == ("cooked", "error") and await rows(recipe_env, XP) == []
    second = await engine_of(recipe_env).cook(recipe_env.user_id, done.recipe_id)
    assert (second.outcome, second.xp_code) == ("settled", "awarded") and len(await rows(recipe_env, XP)) == 1


# ================================================================== the whole flow through the Dispatcher


async def test_a_request_gets_the_function_with_the_constraints_and_the_reply_is_a_card_the_model_did_not_write(recipe_env, owner_engine):
    env = recipe_env
    await new_fact(owner_engine, env.user_id, "health.allergy", category="health", value="орехи")
    await seed(env, "Паста карбонара", days_ago=3, ingredients=("паста", "бекон"))
    model_suggests(env, rcall())
    await env.feed(ASK)
    offered = env.chat.tool_calls[0]
    assert [t.name for t in offered["tools"]] == ["suggest_recipe"]
    assert "## Recipe" in offered["system"] and "health.allergy: орехи" in offered["system"] and "Паста карбонара" in offered["system"]
    assert not any("сохранил" in t.lower() for t in says(env))  # the model's own words next to the call are never sent
    assert "Быстро и сытно." in says(env)  # the reply was phrased again, with the note of what the system did
    assert "Recipe (made by the system)" in env.chat.calls[-1]["system"] and "Омлет с сыром" in env.chat.calls[-1]["system"]
    [card] = card_messages(env)
    assert card.text.startswith("🍽 Омлет с сыром\n⏱ 10 мин") and card.reply_markup.inline_keyboard[0][0].text == "🍳 Приготовлено!"
    assert await rows(env, COUNT) == [(2,)]
    assert not any("🍽" in (m["content"] or "") for m in await env.messages())  # the card is not a word of the assistant: not remembered as one


async def test_the_button_closes_the_history_edits_the_card_and_pays(recipe_env):
    env = recipe_env
    model_suggests(env, rcall())
    await env.feed(ASK)
    [card] = card_messages(env)
    data = card.reply_markup.inline_keyboard[0][0].callback_data
    await press(env, data)
    [rid] = [r[0] for r in await rows(env, "SELECT id FROM civ.recipe_history WHERE user_id = :u")]
    assert await rows(env, "SELECT status FROM civ.recipe_history WHERE user_id = :u") == [("cooked",)]
    assert await rows(env, XP) == [(10, "body", f"recipe:{rid}")]
    [edit] = env.session.edits()
    assert edit.text.endswith("✅ Приготовлено\n⭐ +10 XP — тело") and edit.text.startswith("🍽 Омлет с сыром") and edit.reply_markup is None
    assert env.session.callback_answers()[-1].text == texts.RECIPE_TOAST["cooked"]
    await press(env, data)  # a second press: nothing more is paid, the card says so
    assert len(await rows(env, XP)) == 1 and env.session.callback_answers()[-1].text == texts.RECIPE_TOAST["settled"]
    assert not env.session.edits()[-1].text.endswith("тело")


async def test_a_stranger_cannot_press_my_button(recipe_env, owner_engine):
    env = recipe_env
    model_suggests(env, rcall())
    await env.feed(ASK)
    data = card_messages(env)[0].reply_markup.inline_keyboard[0][0].callback_data
    stranger = random.randrange(10**9, 10**12)  # fresh ids: other tests leave tenants with fixed ones behind
    other = await new_user(owner_engine, telegram_user_id=stranger, forum_chat_id=-int(f"100{random.randrange(10**9, 10**10)}"))
    await press(env, data, tg=stranger)  # an unknown chat for that tenant: the middleware drops it before any handler
    assert await rows(env, "SELECT status FROM civ.recipe_history WHERE user_id = :u") == [("suggested",)]
    assert await owner_exec(owner_engine, "SELECT count(*) FROM civ.xp_ledger WHERE user_id = :u", u=other) == [(0,)]


async def test_a_garbage_button_payload_changes_nothing(recipe_env):
    env = recipe_env
    await press(env, "rc:not-a-uuid:c")
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_UNKNOWN_BUTTON and await rows(env, XP) == []


async def test_a_repeat_gets_the_fixed_text_and_no_card(recipe_env):
    env = recipe_env
    await seed(env, "Омлет с сыром", days_ago=1, ingredients=("Яйца — 3 шт", "Сыр — 50 г", "Соль"))
    model_suggests(env, rcall())
    await env.feed(ASK)
    assert card_messages(env) == [] and texts.RECIPE_REPEAT in says(env) and await rows(env, COUNT) == [(1,)]
    assert not any("сохранил" in t.lower() for t in says(env))


async def test_an_unusable_call_gives_no_card_and_the_models_claim_is_not_sent(recipe_env):
    env = recipe_env
    model_suggests(env, rcall(steps=["один"]), then="Не вышло, давайте ещё раз.")
    await env.feed(ASK)
    assert card_messages(env) == [] and await rows(env, COUNT) == [(0,)]
    assert not any("сохранил" in t.lower() for t in says(env))


async def test_talk_without_a_request_or_a_photo_is_never_offered_the_function(recipe_env):
    env = recipe_env
    env.chat.responder = lambda system, turns: "Расскажите подробнее."
    for said in ("Я приготовил борщ, вышло вкусно", "Купил продукты на неделю"):
        await env.feed(said)
    assert env.chat.tool_calls == [] and all("## Recipe" not in c["system"] for c in env.chat.calls)


async def test_the_switch_turns_the_function_off_even_on_a_request(owner_engine, db):
    env = await build_bot_env(owner_engine, db, recipes_enabled=False)
    try:
        env.chat.responder = lambda system, turns: "Хорошо."
        await env.feed(ASK)
        assert env.chat.tool_calls == [] and card_messages(env) == []
    finally:
        await env.bot.session.close()


async def test_a_photo_of_products_is_offered_the_function_and_only_it(recipe_env):
    env = recipe_env
    model_suggests(env, rcall())
    await env.feed_file(JPEG, photo=True)  # no caption at all: the photo itself is the request
    assert [t.name for t in env.chat.tool_calls[0]["tools"]] == ["suggest_recipe"]  # an image can trigger nothing else
    assert len(card_messages(env)) == 1 and await rows(env, COUNT) == [(1,)]


async def test_a_photo_with_a_request_in_the_caption_works_too(recipe_env):
    env = recipe_env
    model_suggests(env, rcall())
    await env.feed_file(JPEG, caption="Что приготовить из этого?", photo=True)
    assert len(card_messages(env)) == 1


async def test_a_document_is_offered_the_function_only_when_the_caption_asks(recipe_env):
    env = recipe_env
    env.chat.responder = lambda system, turns: "Вижу документ."
    await env.feed_file(PDF, caption="Посмотри", mime_type="application/pdf", file_name="x.pdf")
    assert env.chat.tool_calls == []  # no request, not a photo: the plain path
    model_suggests(env, rcall())
    await env.feed_file(PDF, caption="Придумай рецепт по этому списку", mime_type="application/pdf", file_name="y.pdf")
    assert [t.name for t in env.chat.tool_calls[-1]["tools"]] == ["suggest_recipe"] and len(card_messages(env)) == 1


async def test_a_voice_request_gets_a_recipe_like_a_typed_one(recipe_env):
    env = recipe_env
    env.chat.transcriber = lambda data, mime: ASK
    model_suggests(env, rcall())
    await env.feed_voice()
    assert len(card_messages(env)) == 1


async def test_a_redelivered_update_suggests_once(recipe_env):
    env = recipe_env
    model_suggests(env, rcall())
    update = env.update(ASK)
    await env.feed_update(update)
    await env.feed_update(update)
    assert await rows(env, COUNT) == [(1,)] and len(card_messages(env)) == 1


async def test_a_database_failure_while_reading_the_context_answers_without_the_tool(recipe_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Recipes, "context", boom)
    env = recipe_env
    env.chat.responder = lambda system, turns: "Давайте подумаем вместе."
    await env.feed(ASK)
    assert env.chat.tool_calls == [] and "Давайте подумаем вместе." in says(env)


async def test_a_database_failure_while_saving_still_sends_the_reply_without_a_card(recipe_env, monkeypatch):
    async def boom(self, *a: Any, **k: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(Recipes, "_insert", boom)
    env = recipe_env
    model_suggests(env, rcall(), then="Не получилось, повторите.")
    await env.feed(ASK)
    assert card_messages(env) == [] and "Не получилось, повторите." in says(env)
    assert not any("сохранил" in t.lower() for t in says(env))


def test_the_function_name_is_unique_in_the_production_registry(recipe_env):
    names = [name for tool in recipe_env.orchestrator.tools.tools for name in tool.functions]
    assert names.count("suggest_recipe") == 1 and len(names) == len(set(names))

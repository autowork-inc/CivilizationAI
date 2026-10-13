"""v2.0 PR 5.1 + 5.2: topic management and the feedback channel.

  * ``create_topic`` must be ASKED about first (the bot offers "Создать под эту задачу отдельный топик?", the user agrees) or requested by the user;
  * ``delete_topic``: the model only names a topic, the user's button deletes it (``bot.delete_forum_topic``); system topics are untouchable;
  * the system prompt states that the bot reads files and can write first, and that topics are not opened on its own initiative;
  * the "💡 Идеи и отзывы" topic: nothing is answered by the model, the message goes to the author with a fixed card, the user is thanked only after it was sent;
  * registration: the feedback topic and its welcome appear at once; /start in a private chat offers the "Создать AI Persona" button with the admin-rights link.
Real PostgreSQL, the real Dispatcher on a fake Telegram wire, fake Gemini."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError

from ai_civ.bot import personas, texts
from ai_civ.bot.prompt import RULES
from ai_civ.bot.tools import create_topic, delete_topic
from ai_civ.db import Tenant
from ai_civ.memory import gc
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.bot_env import build_bot_env
from tests.helpers import new_message, new_persona, owner_exec
from tests.test_registration import add_bot, group, person
from tests.tg_fakes import make_callback_update

ADMIN = 413277509
FEEDBACK_TITLE = "💡 Идеи и отзывы"


@asynccontextmanager
async def env_with(owner_engine, db, **kw: Any):
    env = await build_bot_env(owner_engine, db, **kw)
    try:
        yield env
    finally:
        await env.bot.session.close()


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


def to_admin(env) -> list[Any]:
    return [m for m in env.session.sent() if m.chat_id == ADMIN]


# ================================================================== the prompt: what the bot knows about itself


FLAT_RULES = " ".join(RULES.split())  # the rules are wrapped over several lines in the source


def test_the_rules_fix_the_file_reading_ability_in_the_words_of_the_task():
    assert "Ты УМЕЕШЬ читать файлы (.txt, .pdf, .docx). Содержимое загруженных файлов извлекается системой и передается тебе в контексте сообщения." in FLAT_RULES
    assert "Never claim to the user that you cannot read or accept files" in FLAT_RULES


def test_the_rules_forbid_opening_a_topic_without_asking_first():
    assert "Never open a topic of your own accord" in FLAT_RULES
    assert "Создать под эту задачу отдельный топик?" in FLAT_RULES and "more than 3 times" in FLAT_RULES and "only after they agree" in FLAT_RULES


# ================================================================== create_topic: ask first


def test_the_gate_wants_the_offer_and_the_yes_or_an_explicit_request():
    offer = "Вижу, ты часто говоришь про датский. Создать под эту задачу отдельный топик?"
    assert create_topic.may_create("да", offer) and create_topic.may_create("давай", offer) and create_topic.may_create("Ок, создавай", offer)
    assert not create_topic.may_create("нет", offer) and not create_topic.may_create("не надо", offer) and not create_topic.may_create("да, но потом", offer)
    assert not create_topic.may_create("да", "Как дела?") and not create_topic.may_create("да", None)  # a "yes" to nothing is not an agreement
    assert not create_topic.may_create("я учу датский", None) and not create_topic.may_create("давай", None)
    assert create_topic.may_create("создай топик для датского", None) and create_topic.may_create("заведи мне новую тему про спорт", None)
    assert create_topic.may_create("create a topic for gym", None) and not create_topic.may_create("не создавай тему", None)


def test_an_offer_is_a_question_about_a_separate_topic():
    assert create_topic.offer_was_made("Хочешь, заведу отдельную тему под это? Создать под эту задачу отдельный топик?")
    assert not create_topic.offer_was_made("Создать под эту задачу отдельный топик")  # no question mark: a statement
    assert not create_topic.offer_was_made("Что там с работой?") and not create_topic.offer_was_made("")


@asynccontextmanager
async def ask_first_env(owner_engine, db, **kw: Any):
    async with env_with(owner_engine, db, create_topic_enabled=True, create_topic_require_offer=True, **kw) as env:
        yield env


def create_call(code: str = "danish") -> ToolCall:
    return ToolCall(create_topic.TOOL_NAME, {"code": code, "title": "Датский язык", "focus": "учим датский"})


async def test_the_function_is_not_even_offered_before_the_bot_has_asked(owner_engine, db):
    async with ask_first_env(owner_engine, db) as env:
        env.chat.responder = lambda system, turns: "Здорово!"
        await env.feed("Я решил выучить датский")
        await env.feed("давай")  # a "yes" to nothing
        assert env.chat.tool_calls == [] and env.session.topics() == []  # the plain way both times: no function at all


async def test_after_the_offer_and_a_yes_the_topic_is_created(owner_engine, db):
    async with ask_first_env(owner_engine, db) as env:
        await new_message(env.owner_engine, env.user_id, "я опять про датский", role="user")
        await new_message(env.owner_engine, env.user_id, "Ты уже не раз вернулся к датскому. Создать под эту задачу отдельный топик?", role="assistant")
        env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="Создал!", calls=(create_call(),))
        env.chat.responder = lambda system, turns: "Отлично, учим."
        await env.feed("да")
        [offered] = env.chat.tool_calls
        assert [t.name for t in offered["tools"]] == ["create_topic"] and "never open a topic on your own" in offered["system"]
        [topic] = env.session.topics()
        assert "Датский язык" in topic.name


async def test_a_refusal_or_a_later_message_cancels_the_offer(owner_engine, db):
    async with ask_first_env(owner_engine, db) as env:
        await new_message(env.owner_engine, env.user_id, "Создать под эту задачу отдельный топик?", role="assistant")
        env.chat.responder = lambda system, turns: "Ок."
        await env.feed("нет, не надо")
        assert env.chat.tool_calls == []
        await env.feed("давай")  # the bot's latest message is now its reply "Ок.", not the offer
        assert env.chat.tool_calls == [] and env.session.topics() == []


async def test_an_explicit_request_of_the_user_is_enough(owner_engine, db):
    async with ask_first_env(owner_engine, db) as env:
        env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="Готово!", calls=(create_call("gym"),))
        env.chat.responder = lambda system, turns: "Поехали."
        await env.feed("создай топик для зала")
        assert len(env.chat.tool_calls) == 1 and len(env.session.topics()) == 1


async def test_the_old_behaviour_is_still_one_switch_away(owner_engine, db):
    async with env_with(owner_engine, db, create_topic_enabled=True, create_topic_require_offer=False) as env:
        env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="x", calls=(create_call(),))
        env.chat.responder = lambda system, turns: "ок"
        await env.feed("Я решил выучить датский")
        assert len(env.session.topics()) == 1


# ================================================================== delete_topic


@asynccontextmanager
async def delete_env(owner_engine, db, **kw: Any):
    kw.setdefault("delete_topic_enabled", True)
    async with env_with(owner_engine, db, **kw) as env:
        env.danish = await new_persona(env.owner_engine, env.user_id, "danish", thread_id=610, title="🇩🇰 Датский")
        env.sport = await new_persona(env.owner_engine, env.user_id, "sport", thread_id=611, title="🏃 Спорт")
        env.settings_persona = await new_persona(env.owner_engine, env.user_id, "settings", thread_id=612, title="⚙️ Настройки")
        env.feedback = await new_persona(env.owner_engine, env.user_id, "feedback", thread_id=613, title=FEEDBACK_TITLE)
        yield env


def delete_call(code: str = "danish") -> ToolCall:
    return ToolCall(delete_topic.TOOL_NAME, {"code": code})


def ask_to_delete(env, code: str = "danish", *, text: str = "Удалила!") -> None:
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=(delete_call(code),))
    env.chat.responder = lambda system, turns: "Сейчас спрошу."


async def persona_row(env, persona_id) -> tuple[Any, ...]:
    return (await owner_exec(env.owner_engine, "SELECT is_active, telegram_thread_id FROM civ.personas WHERE id = :p", p=persona_id))[0]


def press(env, persona_id, answer: str, message_id: int = 9001):
    return make_callback_update(data=f"dt:{persona_id}:{answer}", user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=message_id)


def test_the_cue_for_deleting_a_topic_is_found_in_the_users_words():
    for text in ("удали тему про датский", "убери этот топик", "закрой тему спорт", "delete the topic", "можно удалить топик «Датский»?"):
        assert delete_topic.wants_delete(text), text
    for text in ("привет", "удали лишнее из списка", "расскажи про тему отпуска", "создай топик"):
        assert not delete_topic.wants_delete(text), text


async def test_the_function_lists_only_the_bots_own_topics_never_a_system_one(owner_engine, db):
    async with delete_env(owner_engine, db) as env:
        ask_to_delete(env)
        await env.feed("удали тему про датский")
        [offered] = env.chat.tool_calls
        assert [t.name for t in offered["tools"]] == ["delete_topic"]
        section = offered["system"]
        assert "- danish:" in section and "- sport:" in section and "- settings:" not in section and "- feedback:" not in section


async def test_the_model_only_asks_nothing_is_deleted_until_the_users_button(owner_engine, db):
    async with delete_env(owner_engine, db) as env:
        ask_to_delete(env)
        await env.feed("удали тему про датский")
        assert env.session.topic_deletions() == [] and await persona_row(env, env.danish) == (True, 610)
        assert "Удалила!" not in says(env)  # the model's claim is never sent
        card = env.session.sent()[-1]
        assert card.text == texts.TOPIC_DELETE_ASK.format(title="🇩🇰 Датский")
        yes, no = card.reply_markup.inline_keyboard[0]
        assert yes.callback_data == f"dt:{env.danish}:y" and no.callback_data == f"dt:{env.danish}:n" and yes.text == texts.TOPIC_DELETE_YES
        [phrased] = env.chat.calls
        assert "Nothing is deleted yet" in phrased["system"]


async def test_yes_deletes_the_forum_topic_retires_the_persona_and_keeps_the_history(owner_engine, db):
    async with delete_env(owner_engine, db) as env:
        await new_message(env.owner_engine, env.user_id, "учу слова", persona_id=env.danish)
        await env.feed_update(press(env, env.danish, "y"))
        [deleted] = env.session.topic_deletions()
        assert (deleted.chat_id, deleted.message_thread_id) == (env.chat_id, 610)
        assert await persona_row(env, env.danish) == (False, None)  # inactive, no thread: a later /setup or router may make a NEW topic for this code
        assert (await owner_exec(env.owner_engine, "SELECT count(*) FROM civ.messages WHERE persona_id = :p", p=env.danish)) == [(1,)]
        assert env.session.callback_answers()[-1].text == texts.TOPIC_DELETE_TOASTS["deleted"]
        assert env.session.edits()[-1].text == texts.TOPIC_DELETED.format(title="🇩🇰 Датский") and env.session.edits()[-1].reply_markup is None
        # a second press (a double tap) finds nothing to delete and calls Telegram no more
        await env.feed_update(press(env, env.danish, "y"))
        assert len(env.session.topic_deletions()) == 1 and env.session.callback_answers()[-1].text == texts.TOPIC_DELETE_TOASTS["unavailable"]
        # the other topics are untouched
        assert await persona_row(env, env.sport) == (True, 611)


async def test_no_keeps_the_topic(owner_engine, db):
    async with delete_env(owner_engine, db) as env:
        await env.feed_update(press(env, env.danish, "n"))
        assert env.session.topic_deletions() == [] and await persona_row(env, env.danish) == (True, 610)
        assert env.session.edits()[-1].text == texts.TOPIC_DELETE_KEPT.format(title="🇩🇰 Датский")


@pytest.mark.parametrize("which", ["settings", "feedback"])
async def test_a_system_topic_cannot_be_deleted_even_by_a_forged_button_or_a_forged_call(owner_engine, db, which):
    async with delete_env(owner_engine, db) as env:
        persona = env.settings_persona if which == "settings" else env.feedback
        await env.feed_update(press(env, persona, "y"))
        assert env.session.topic_deletions() == [] and env.session.callback_answers()[-1].text == texts.TOPIC_DELETE_TOASTS["unavailable"]
        ask_to_delete(env, which, text="Удалила!")
        await env.feed("удали тему настройки")
        assert env.session.topic_deletions() == [] and not any(m.reply_markup for m in env.session.sent() if m.text == texts.TOPIC_DELETE_ASK)
        assert "Удалила!" not in says(env)  # the words beside an unusable call are not trusted


async def test_an_unknown_code_or_no_wording_of_the_user_deletes_and_asks_nothing(owner_engine, db):
    async with delete_env(owner_engine, db) as env:
        ask_to_delete(env, "nonexistent")
        await env.feed("удали тему про рыбалку")
        assert not any(m.text == texts.TOPIC_DELETE_ASK for m in env.session.sent()) and env.session.topic_deletions() == []
        calls_before = len(env.chat.tool_calls)
        await env.feed("расскажи про датский")  # no cue: the function is not even offered
        assert len(env.chat.tool_calls) == calls_before


async def test_another_tenants_button_changes_nothing(owner_engine, db):
    async with delete_env(owner_engine, db) as env:
        other = await build_bot_env(owner_engine, db)
        try:
            await other.feed_update(press(other, env.danish, "y"))
            assert other.session.topic_deletions() == [] and await persona_row(env, env.danish) == (True, 610)
            assert other.session.callback_answers()[-1].text == texts.TOPIC_DELETE_TOASTS["unavailable"]
        finally:
            await other.bot.session.close()


@pytest.mark.parametrize(
    "error, message, outcome, persona_after",
    [
        (TelegramForbiddenError, "Forbidden: not enough rights to delete a topic", "no_rights", (True, 610)),
        (TelegramBadRequest, "Bad Request: not enough rights to manage topics", "no_rights", (True, 610)),
        (TelegramBadRequest, "Bad Request: message thread not found", "gone", (False, None)),
        (TelegramBadRequest, "Bad Request: TOPIC_ID_INVALID", "gone", (False, None)),
        (TelegramBadRequest, "Bad Request: something odd", "failed", (True, 610)),
        (TelegramNetworkError, "timeout", "failed", (True, 610)),
    ],
)
async def test_every_refusal_of_telegram_is_a_result_not_an_exception(owner_engine, db, error, message, outcome, persona_after):
    async with delete_env(owner_engine, db) as env:
        env.session.fail_on("deleteForumTopic", error, message=message)
        await env.feed_update(press(env, env.danish, "y"))
        assert env.session.callback_answers()[-1].text == texts.TOPIC_DELETE_TOASTS[outcome]
        assert await persona_row(env, env.danish) == persona_after
        if outcome in ("no_rights", "failed"):  # the buttons stay: the user may press again
            assert env.session.edits() == []


async def test_a_failure_of_the_database_after_telegram_is_visible_to_the_user(owner_engine, db):
    async with delete_env(owner_engine, db) as env:
        original = env.orchestrator.delete_topics.db

        class Boom:
            def tenant_session(self, *a, **k):
                raise RuntimeError("db down")

        env.orchestrator.delete_topics.db = Boom()  # type: ignore[assignment]
        try:
            await env.feed_update(press(env, env.danish, "y"))
        finally:
            env.orchestrator.delete_topics.db = original
        assert env.session.callback_answers()[-1].text == texts.FAILURE and env.session.topic_deletions() == []


async def test_the_function_is_off_by_the_switch_and_in_the_default_stack(owner_engine, db):
    async with delete_env(owner_engine, db, delete_topic_enabled=False) as env:
        await env.feed("удали тему про датский")
        assert env.chat.tool_calls == []


# ================================================================== the feedback channel


@asynccontextmanager
async def feedback_env(owner_engine, db, **kw: Any):
    async with env_with(owner_engine, db, **kw) as env:
        env.topic = await new_persona(env.owner_engine, env.user_id, "feedback", thread_id=700, title=FEEDBACK_TITLE)
        yield env


async def test_a_message_in_the_feedback_topic_goes_to_the_author_and_not_to_the_model(owner_engine, db):
    async with feedback_env(owner_engine, db) as env:
        await env.feed("Хочу тёмную тему оформления", thread_id=700, username="alice_tg")
        assert env.chat.calls == [] and env.chat.tool_calls == [] and env.router.calls == []  # no model was asked
        [card] = to_admin(env)
        assert card.text == f"📩 Новое предложение от @alice_tg (ID: {env.telegram_user_id}) | Персонаж: Без имени\nТекст: Хочу тёмную тему оформления"
        assert says(env, 700) == ["Принято, передал автору проекта!"]


async def test_the_card_carries_the_characters_name_the_missing_username_and_every_line_of_the_text(owner_engine, db):
    async with feedback_env(owner_engine, db) as env:
        await owner_exec(env.owner_engine, "INSERT INTO civ.character_profiles (user_id, character_name) VALUES (:u, 'Рыцарь Света')", u=env.user_id)
        await env.feed("Первая строка\nВторая строка", thread_id=700)
        [card] = to_admin(env)
        assert card.text == f"📩 Новое предложение от (без username) (ID: {env.telegram_user_id}) | Персонаж: Рыцарь Света\nТекст: Первая строка\nВторая строка"


async def test_a_very_long_message_is_cut_for_the_card_but_kept_whole_in_the_history(owner_engine, db):
    async with feedback_env(owner_engine, db, feedback_max_chars=100) as env:
        await env.feed("я" * 500, thread_id=700)
        [card] = to_admin(env)
        assert card.text.endswith("…") and len(card.text.split("Текст: ")[1]) == 100
        assert (await env.messages())[0]["content"] == "я" * 500


async def test_the_user_is_thanked_only_after_the_author_got_it(owner_engine, db):
    async with feedback_env(owner_engine, db) as env:
        env.session.fail_on("sendMessage", TelegramForbiddenError, when=lambda m: m.chat_id == ADMIN, message="Forbidden: bot can't initiate conversation with a user")
        await env.feed("Идея!", thread_id=700)
        assert says(env, 700) == [texts.FAILURE]  # nothing was acknowledged
        assert "Принято, передал автору проекта!" not in says(env, 700)


async def test_the_feedback_is_not_made_into_the_users_own_memory(owner_engine, db):
    async with feedback_env(owner_engine, db) as env:
        await env.feed("Хочу тёмную тему", thread_id=700)
        assert all(r["persona_id"] == env.topic for r in await env.messages())
        ack = await owner_exec(env.owner_engine, "SELECT third_party_data FROM civ.messages WHERE user_id = :u AND role = 'assistant'", u=env.user_id)
        assert ack == [(True,)]
        # and the nightly compression does not even look at the topic
        yesterday = datetime(2026, 6, 14, 12, 0, tzinfo=UTC)
        await owner_exec(env.owner_engine, "UPDATE civ.users SET timezone = 'UTC' WHERE id = :u", u=env.user_id)
        await new_message(env.owner_engine, env.user_id, "что-то про фидбек", persona_id=env.topic, created_at=yesterday)
        await new_message(env.owner_engine, env.user_id, "обычный разговор", created_at=yesterday)
        created = await gc.enqueue_day_compression(db, Tenant(env.user_id, "UTC"), now=datetime(2026, 6, 15, 5, 0, tzinfo=UTC), local_hour=3)
        assert created == 1
        [(key,)] = await owner_exec(env.owner_engine, "SELECT idempotency_key FROM civ.memory_outbox WHERE user_id = :u", u=env.user_id)
        assert key.startswith("compress_day:none:")


async def test_a_redelivered_update_is_forwarded_once(owner_engine, db):
    async with feedback_env(owner_engine, db) as env:
        update = env.update("Идея про уведомления", thread_id=700, message_id=555)
        await env.feed_update(update)
        await env.feed_update(update)
        assert len(to_admin(env)) == 1


async def test_an_empty_voice_or_file_message_asks_for_text(owner_engine, db):
    async with feedback_env(owner_engine, db) as env:
        await env.feed_file(b"%PDF-1.4 fake", thread_id=700, mime_type="application/pdf", file_name="idea.pdf")
        assert to_admin(env) == [] and texts.FEEDBACK_EMPTY in says(env, 700)


async def test_a_voice_note_in_the_feedback_topic_is_forwarded_as_its_transcript(owner_engine, db):
    async with feedback_env(owner_engine, db) as env:
        env.chat.transcriber = lambda data, mime: "добавьте напоминания по пятницам"
        await env.feed_voice(thread_id=700)
        [card] = to_admin(env)
        assert card.text.endswith("Текст: добавьте напоминания по пятницам")


async def test_the_feedback_works_even_before_the_interview_is_finished(owner_engine, db):
    async with feedback_env(owner_engine, db, onboarding_state="in_progress") as env:
        await owner_exec(env.owner_engine, "UPDATE civ.users SET onboarding_state = 'in_progress' WHERE id = :u", u=env.user_id)
        env.tenant = (await __import__("ai_civ.bot.orchestrator", fromlist=["load_tenant"]).load_tenant(env.db, env.user_id))
        await env.feed("Идея", thread_id=700)
        assert len(to_admin(env)) == 1 and env.router.calls == []  # not taken for an answer of the interview


async def test_the_admin_id_is_a_setting_with_the_value_of_the_task(owner_engine, db):
    async with feedback_env(owner_engine, db, feedback_admin_telegram_id=777) as env:
        assert env.settings.feedback_admin_telegram_id == 777
        await env.feed("Идея", thread_id=700)
        assert [m.chat_id for m in env.session.sent() if m.text.startswith("📩")] == [777]
    from ai_civ.config import Settings

    assert Settings(_env_file=None).feedback_admin_telegram_id == ADMIN


async def test_the_router_never_offers_a_system_topic_as_a_destination(owner_engine, db):
    async with env_with(owner_engine, db) as env:
        await new_persona(env.owner_engine, env.user_id, "feedback", thread_id=700, title=FEEDBACK_TITLE)
        await new_persona(env.owner_engine, env.user_id, "settings", thread_id=701, title="⚙️ Настройки")
        await new_persona(env.owner_engine, env.user_id, "sport", thread_id=702, title="🏃 Спорт")
        await env.feed("Расскажи анекдот")
        shown = env.router.calls[-1] if env.router.calls else ""
        shown = str(shown)
        assert "sport" in shown and "feedback" not in shown and "settings" not in shown


# ================================================================== registration: the topic appears at once; /start offers the button


async def test_registration_creates_the_feedback_topic_with_its_welcome(bare_env):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg)
    await add_bot(env, chat, tg)
    [topic] = env.session.topics()
    assert topic.name == FEEDBACK_TITLE
    greeting = env.session.sent()[-1]
    assert greeting.text == texts.FEEDBACK_GREETING and greeting.message_thread_id is not None
    row = await env.user_row(tg)
    assert await owner_exec(env.owner_engine, "SELECT code FROM civ.personas WHERE user_id = :u AND telegram_thread_id IS NOT NULL", u=row["id"]) == [("feedback",)]


async def test_overlapping_registration_events_make_the_topic_once(bare_env):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg)
    await asyncio.gather(*[add_bot(env, chat, tg) for _ in range(6)])
    assert len(env.session.topics()) == 1 and len([m for m in env.session.sent() if m.text == texts.FEEDBACK_GREETING]) == 1


async def test_a_bot_without_the_topics_right_registers_nobody_and_makes_no_topic(bare_env):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg, bot_can_manage_topics=False)
    await add_bot(env, chat, tg)
    assert env.session.topics() == [] and await env.count_users() == 0
    assert any(texts.PROBLEM_NOT_ADMIN in m.text for m in env.session.sent())  # the right is checked and its absence is explained


async def test_a_refused_topic_does_not_spoil_the_registration(bare_env):
    env, tg, chat = bare_env, person(), group()
    env.session.add_group(chat, tg)
    env.session.fail_on("createForumTopic", TelegramBadRequest, message="Bad Request: not enough rights to create a topic")
    await add_bot(env, chat, tg)
    assert await env.count_users() == 1 and (await env.user_row(tg))["onboarding_state"] == "in_progress"  # the interview still started


async def test_start_in_a_private_chat_offers_the_create_persona_button_with_the_admin_rights_link(bare_env):
    env, tg = bare_env, person()
    await env.say("/start", user_id=tg, chat_id=tg, chat_type="private")
    [hint] = env.session.sent()
    assert hint.chat_id == tg and hint.text == texts.UNREGISTERED
    [[button]] = hint.reply_markup.inline_keyboard
    assert button.text == "Создать AI Persona"
    username = env.settings.telegram_bot_username
    assert button.url == f"https://t.me/{username}?startgroup=setup&admin=change_info+delete_messages+restrict_members+invite_users+pin_messages+manage_topics"
    assert "manage_topics" in button.url and "startgroup=setup" in button.url and await env.count_users() == 0


async def test_the_invite_link_of_a_stranger_also_gets_the_way_to_register(bare_env):
    env, tg = bare_env, person()
    await env.say("/start connect_" + "a" * 22, user_id=tg, chat_id=tg, chat_type="private")
    sent = env.session.sent()[-1]
    assert sent.text == texts.CONNECT_NEED_ACCOUNT and sent.reply_markup.inline_keyboard[0][0].url.startswith("https://t.me/")


async def test_the_feedback_command_makes_the_topic_for_a_user_who_registered_before_it_existed(owner_engine, db):
    async with env_with(owner_engine, db) as env:
        await env.feed("/feedback")
        [topic] = env.session.topics()
        assert topic.name == FEEDBACK_TITLE
        link = env.session.sent()[-1]
        assert link.text == texts.FEEDBACK_LINK and link.reply_markup.inline_keyboard[0][0].url.endswith("/500")  # the first topic of the fake wire
        await env.feed("/feedback")
        assert len(env.session.topics()) == 1  # the second time it is only a link


async def test_the_feedback_persona_is_a_system_topic_like_settings():
    assert personas.FEEDBACK_CODE in personas.SYSTEM_CODES and personas.SETTINGS_PERSONA.code in personas.SYSTEM_CODES
    assert personas.FEEDBACK_PERSONA.title == FEEDBACK_TITLE and personas.CODE_RE.match(personas.FEEDBACK_CODE)


def test_the_initiative_and_topic_rules_the_owner_asked_for_are_in_the_prompt():
    assert "Никогда не спамь топиками. Не создавай новые темы без прямого запроса пользователя." in FLAT_RULES
    assert "Ты имеешь право предложить удалить/закрыть топик, если он исчерпал себя" in FLAT_RULES
    assert "У тебя ЕСТЬ техническая возможность писать первым" in FLAT_RULES and "инициируй сообщение сам через schedule_reminder" in FLAT_RULES
    assert "НИКОГДА не говори, что ты не можешь писать первым" in FLAT_RULES
    assert "Ты не ждёшь команды. Ты анализируешь контекст и, если ситуация требует твоего участия, инициируешь диалог сам" in FLAT_RULES
    # the guard against the old incident ("написал мастеру") stays: a reminder exists only once the function says so
    assert "только после того, как результат функции" in FLAT_RULES


def test_the_grounding_rule_is_in_the_prompt_and_does_not_ban_the_merely_unusual():
    assert "Grounding Rule" in FLAT_RULES
    assert "Строго игнорируй и не подыгрывай абсурдным, физически невозможным или фантастическим заявлениям пользователя" in FLAT_RULES
    assert "Вежливо возвращай фокус на реальные цели и текущий контекст" in FLAT_RULES
    assert "Never record such a claim as a fact" in FLAT_RULES and "a real but surprising thing" in FLAT_RULES

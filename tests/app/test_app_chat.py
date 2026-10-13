"""The mobile service layer (migration 0026, ``ai_civ.app``): idempotency by ``client_message_id``, voice and files as bytes, cards as JSON, isolation from
Telegram, notifications through the outbox, and the guards that keep the Telegram workers away from people who have no Telegram."""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
import uuid
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy.exc import DBAPIError

from ai_civ.app.chat_service import AppChatService
from ai_civ.app.dto import AppChatError, AppIncomingMessage
from ai_civ.app.notifications import OutboxPublisher
from ai_civ.bot import schedule
from ai_civ.bot.orchestrator import telegram_markup
from ai_civ.bot.tools.base import Card, CardButton, Offered, ToolContext, ToolRegistry, ToolResult, card_to_json, neutral_markup
from ai_civ.config import Settings
from ai_civ.memory.llm import LLMError, ToolCall, ToolReply, ToolSpec
from tests.helpers import owner_exec
from tests.tg_fakes import FakeChatLLM

APP_SRC = Path(__file__).resolve().parents[2] / "src" / "ai_civ" / "app"


@pytest.fixture(autouse=True)
def _no_telegram_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)


def make_settings(**kw) -> Settings:
    kw.setdefault("app_dedup_poll_seconds", 0.05)
    return Settings(_env_file=None, **kw)


def service(db, chat: FakeChatLLM, **kw) -> AppChatService:
    settings = kw.pop("settings", None) or make_settings()
    return AppChatService(db=db, chat_llm=chat, settings=settings, **kw)


async def register(db, email: str | None = None) -> uuid.UUID:
    email = email or f"Anna.{uuid.uuid4().hex[:10]}@Example.com"  # the database is shared by the whole run: every person gets their own address
    result = await db.register_app_user(email=email, pass_hash="$argon2id$fake", timezone="Europe/Copenhagen")
    assert result.outcome == "created" and result.tenant_id is not None
    return result.tenant_id


def message(user: uuid.UUID, text: str = "привет, как дела?", *, client_id: uuid.UUID | None = None, **kw) -> AppIncomingMessage:
    return AppIncomingMessage(user_id=user, persona_id=kw.pop("persona_id", None), client_message_id=client_id or uuid.uuid4(), text=text, **kw)


async def rows(owner_engine, user: uuid.UUID, role: str | None = None):
    sql = "SELECT role, content, client_message_id, reply_to_id, id, cards FROM civ.messages WHERE user_id = :u"
    if role:
        sql += " AND role = :r"
    return await owner_exec(owner_engine, sql + " ORDER BY created_at", u=user, **({"r": role} if role else {}))


# ------------------------------------------------------------------ registration of an app person


async def test_registering_an_app_person_makes_the_whole_tenant(db, owner_engine):
    tag = uuid.uuid4().hex[:8]
    user = await register(db, f"Anna.{tag}@Example.com")
    state = (await owner_exec(owner_engine, "SELECT onboarding_state, telegram_user_id, forum_chat_id, email, timezone FROM civ.users WHERE id = :u", u=user))[0]
    assert tuple(state) == ("done", None, None, f"anna.{tag}@example.com", "Europe/Copenhagen")
    personas = await owner_exec(owner_engine, "SELECT code, title, telegram_thread_id, system_prompt FROM civ.personas WHERE user_id = :u ORDER BY code", u=user)
    from ai_civ.bot.personas import FEEDBACK_PERSONA, SETTINGS_PERSONA, compose_system_prompt

    assert [(p[0], p[1], p[2]) for p in personas] == [("feedback", FEEDBACK_PERSONA.title, None), ("settings", SETTINGS_PERSONA.title, None)]
    assert personas[0][3] == compose_system_prompt(FEEDBACK_PERSONA.title, FEEDBACK_PERSONA.focus)
    assert personas[1][3] == compose_system_prompt(SETTINGS_PERSONA.title, SETTINGS_PERSONA.focus)
    assert (await owner_exec(owner_engine, "SELECT count(*) FROM civ.character_profiles WHERE user_id = :u", u=user))[0][0] == 1


async def test_the_same_email_in_any_case_is_taken_and_sign_in_finds_the_person(db):
    tag = uuid.uuid4().hex[:8]
    user = await register(db, f"Boris.{tag}@Example.com")
    again = await db.register_app_user(email=f"  BORIS.{tag.upper()}@example.COM ", pass_hash="x")
    assert (again.tenant_id, again.outcome) == (None, "email_taken")
    found = await db.resolve_tenant_by_email(f"boris.{tag}@EXAMPLE.com")
    assert found is not None and found.tenant_id == user and found.pass_hash == "$argon2id$fake"
    assert await db.resolve_tenant_by_email("nobody@example.com") is None


async def test_parallel_sign_ups_with_one_email_create_one_person(db, owner_engine):
    email = f"race.{uuid.uuid4().hex[:8]}@example.com"
    results = await asyncio.gather(*(db.register_app_user(email=email, pass_hash="h") for _ in range(6)))
    assert sorted(r.outcome for r in results) == ["created"] + ["email_taken"] * 5
    assert (await owner_exec(owner_engine, "SELECT count(*) FROM civ.users WHERE email = :e", e=email))[0][0] == 1


async def test_a_person_needs_some_identity_and_a_sane_email(owner_engine, db):
    with pytest.raises(Exception):
        await owner_exec(owner_engine, "INSERT INTO civ.users (timezone) VALUES ('UTC')")  # no Telegram, no e-mail, no external id
    with pytest.raises(Exception):
        await db.register_app_user(email="not-an-email", pass_hash="h")


# ------------------------------------------------------------------ idempotency


async def test_a_repeated_client_message_id_gives_one_message_and_one_model_call(db, owner_engine):
    user = await register(db)
    chat = FakeChatLLM()
    svc = service(db, chat)
    client_id = uuid.uuid4()
    first = await svc.handle_app_message(message(user, client_id=client_id))
    second = await svc.handle_app_message(message(user, client_id=client_id))
    assert (first.replayed, second.replayed) == (False, True)
    assert second.message_id == first.message_id and second.text == first.text
    assert len(chat.calls) == 1
    assert len(await rows(owner_engine, user, "user")) == 1 and len(await rows(owner_engine, user, "assistant")) == 1
    user_row, reply_row = (await rows(owner_engine, user))[0], (await rows(owner_engine, user))[1]
    assert user_row[2] == client_id and reply_row[3] == user_row[4]  # the reply points at the message it answers


async def test_parallel_requests_with_one_client_id_in_one_process_make_one_call(db, owner_engine):
    user = await register(db)

    async def slow() -> None:
        await asyncio.sleep(0.3)

    chat = FakeChatLLM(on_call=slow)
    svc = service(db, chat)
    client_id = uuid.uuid4()
    a, b = await asyncio.gather(*(svc.handle_app_message(message(user, client_id=client_id)) for _ in range(2)))
    assert a.message_id == b.message_id and len(chat.calls) == 1
    assert len(await rows(owner_engine, user, "user")) == 1 and len(await rows(owner_engine, user, "assistant")) == 1


async def test_parallel_requests_in_two_processes_make_one_call(db, owner_engine):
    """Two service objects = two processes: no shared lock, so the second one finds the claimed row and waits for the first one's reply in the database."""
    user = await register(db)

    async def slow() -> None:
        await asyncio.sleep(0.4)

    chat = FakeChatLLM(on_call=slow)
    first, second = service(db, chat), service(db, chat)
    client_id = uuid.uuid4()
    a, b = await asyncio.gather(first.handle_app_message(message(user, client_id=client_id)), second.handle_app_message(message(user, client_id=client_id)))
    assert a.message_id == b.message_id and len(chat.calls) == 1
    assert len(await rows(owner_engine, user, "user")) == 1 and len(await rows(owner_engine, user, "assistant")) == 1
    assert sorted([a.replayed, b.replayed]) == [False, True]


async def test_many_parallel_requests_across_processes_keep_to_one_reply(db, owner_engine):
    user = await register(db)

    async def slow() -> None:
        await asyncio.sleep(0.2)

    chat = FakeChatLLM(on_call=slow)
    client_id = uuid.uuid4()
    replies = await asyncio.gather(*(service(db, chat).handle_app_message(message(user, client_id=client_id)) for _ in range(5)))
    assert len({r.message_id for r in replies}) == 1 and len(chat.calls) == 1
    assert len(await rows(owner_engine, user, "assistant")) == 1


async def test_an_abandoned_message_is_answered_by_the_next_request(db, owner_engine):
    """The process died after the claim: the user message has no reply. Older than ``app_stale_seconds`` -> the retry answers it (once)."""
    user = await register(db)
    client_id = uuid.uuid4()
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.messages (user_id, role, content, client_message_id, created_at) VALUES (:u, 'user', 'привет, как дела?', :c, now() - interval '1 hour')",
        u=user,
        c=client_id,
    )
    chat = FakeChatLLM()
    reply = await service(db, chat, settings=make_settings(app_stale_seconds=60)).handle_app_message(message(user, client_id=client_id))
    assert not reply.replayed and len(chat.calls) == 1
    assert len(await rows(owner_engine, user, "user")) == 1 and len(await rows(owner_engine, user, "assistant")) == 1


async def test_a_failed_model_call_stores_no_reply_and_the_retry_answers(db, owner_engine):
    user = await register(db)
    chat = FakeChatLLM(responder=lambda system, turns: LLMError("down"))
    client_id = uuid.uuid4()
    with pytest.raises(LLMError):
        await service(db, chat).handle_app_message(message(user, client_id=client_id))
    assert len(await rows(owner_engine, user, "assistant")) == 0
    chat.responder = lambda system, turns: "вот теперь ответ"
    reply = await service(db, chat, settings=make_settings(app_stale_seconds=0)).handle_app_message(message(user, client_id=client_id))
    assert reply.text == "вот теперь ответ" and len(await rows(owner_engine, user, "assistant")) == 1


async def test_the_database_allows_one_reply_per_message(db, owner_engine):
    user = await register(db)
    await service(db, FakeChatLLM()).handle_app_message(message(user))
    user_row = (await rows(owner_engine, user, "user"))[0]
    with pytest.raises(Exception):
        await owner_exec(owner_engine, "INSERT INTO civ.messages (user_id, role, content, reply_to_id) VALUES (:u, 'assistant', 'second', :r)", u=user, r=user_row[4])


# ------------------------------------------------------------------ personas: the database row, never a forum topic


async def test_the_persona_comes_straight_from_the_database(db, owner_engine):
    user = await register(db)
    persona = (await owner_exec(owner_engine, "SELECT id, system_prompt FROM civ.personas WHERE user_id = :u AND code = 'settings'", u=user))[0]
    chat = FakeChatLLM()
    reply = await service(db, chat).handle_app_message(message(user, persona_id=persona[0]))
    assert reply.persona_id == persona[0] and persona[1] in chat.calls[0]["system"]
    stored = await owner_exec(owner_engine, "SELECT DISTINCT persona_id FROM civ.messages WHERE user_id = :u", u=user)
    assert [r[0] for r in stored] == [persona[0]]


async def test_an_unknown_or_foreign_persona_and_the_feedback_channel_are_refused(db, owner_engine):
    mine, other = await register(db, f"me.{uuid.uuid4().hex[:8]}@example.com"), await register(db, f"other.{uuid.uuid4().hex[:8]}@example.com")
    foreign = (await owner_exec(owner_engine, "SELECT id FROM civ.personas WHERE user_id = :u AND code = 'settings'", u=other))[0][0]
    feedback = (await owner_exec(owner_engine, "SELECT id FROM civ.personas WHERE user_id = :u AND code = 'feedback'", u=mine))[0][0]
    chat = FakeChatLLM()
    svc = service(db, chat)
    for persona_id, code in ((uuid.uuid4(), "unknown_persona"), (foreign, "unknown_persona"), (feedback, "persona_unavailable")):
        with pytest.raises(AppChatError) as err:
            await svc.handle_app_message(message(mine, persona_id=persona_id))
        assert err.value.code == code
    assert chat.calls == [] and await rows(owner_engine, mine) == []


async def test_nothing_is_answered_for_an_unknown_person_or_an_empty_message(db):
    svc = service(db, FakeChatLLM())
    with pytest.raises(AppChatError) as err:
        await svc.handle_app_message(message(uuid.uuid4()))
    assert err.value.code == "unknown_user"
    user = await register(db)
    with pytest.raises(AppChatError) as err:
        await svc.handle_app_message(message(user, "   "))
    assert err.value.code == "empty_message"


# ------------------------------------------------------------------ voice and files arrive as bytes


async def test_a_voice_message_is_transcribed_and_only_the_text_is_stored(db, owner_engine):
    user = await register(db)
    chat = FakeChatLLM()
    chat.transcriber = lambda data, mime: "купил молоко за двадцать крон"
    audio = b"OggS\x00fake-opus-bytes"
    reply = await service(db, chat).handle_app_message(message(user, "", voice_bytes=audio, voice_mime="audio/mp4"))
    assert chat.transcribe_calls[0]["data"] == audio and chat.transcribe_calls[0]["mime_type"] == "audio/mp4"
    assert reply.user_text == "купил молоко за двадцать крон"
    stored = await rows(owner_engine, user, "user")
    assert [r[1] for r in stored] == ["купил молоко за двадцать крон"]
    last = chat.calls[0]["turns"][-1]
    assert last.text == "купил молоко за двадцать крон" and last.media and last.media[0].data == audio  # the chat model hears the tone too
    assert audio not in str(await rows(owner_engine, user)).encode()  # the audio is kept nowhere


async def test_an_unintelligible_voice_message_is_refused_and_nothing_is_stored(db, owner_engine):
    user = await register(db)
    chat = FakeChatLLM()
    chat.transcriber = lambda data, mime: "[NO_SPEECH]"
    with pytest.raises(AppChatError) as err:
        await service(db, chat).handle_app_message(message(user, "", voice_bytes=b"x" * 100))
    assert err.value.code == "voice_unclear" and chat.calls == [] and await rows(owner_engine, user) == []


async def test_a_repeated_voice_request_is_answered_without_listening_again(db):
    user = await register(db)
    chat = FakeChatLLM()
    svc = service(db, chat)
    client_id = uuid.uuid4()
    first = await svc.handle_app_message(message(user, "", client_id=client_id, voice_bytes=b"abc" * 50))
    second = await svc.handle_app_message(message(user, "", client_id=client_id, voice_bytes=b"abc" * 50))
    assert second.replayed and second.message_id == first.message_id
    assert len(chat.transcribe_calls) == 1 and len(chat.calls) == 1


async def test_a_text_file_is_read_stored_as_a_record_and_given_to_the_model_as_data(db, owner_engine):
    user = await register(db)
    chat = FakeChatLLM()
    body = "План на неделю: тренировка во вторник, чтение в четверг.\n".encode() * 5
    reply = await service(db, chat).handle_app_message(
        message(user, "что тут?", attachment_bytes=body, attachment_metadata={"file_name": "plan.txt", "mime_type": "text/plain", "kind": "document"})
    )
    assert reply.text
    assert "тренировка во вторник" in chat.calls[0]["turns"][-1].text and "FILE" in chat.calls[0]["turns"][-1].text
    attachment = (await owner_exec(owner_engine, "SELECT kind, content_kind, file_name, size_bytes FROM civ.message_attachments WHERE user_id = :u", u=user))[0]
    assert tuple(attachment) == ("document", "txt", "plan.txt", len(body))


async def test_an_unsupported_file_is_refused_with_a_reason(db, owner_engine):
    user = await register(db)
    with pytest.raises(AppChatError) as err:
        await service(db, FakeChatLLM()).handle_app_message(
            message(user, "", attachment_bytes=b"MZ\x90\x00" * 10, attachment_metadata={"file_name": "a.exe", "mime_type": "application/x-msdownload"})
        )
    assert err.value.code == "attachment_refused" and await rows(owner_engine, user) == []


# ------------------------------------------------------------------ cards: a neutral structure, then JSON


class CardTool:
    name = "card_tool"
    functions = frozenset({"make_card"})
    offer_rank = 1

    async def offer(self, ctx: ToolContext) -> Offered | None:
        async def run(calls) -> ToolResult | None:
            if not any(call.name == "make_card" for call in calls):
                return None
            buttons = ((CardButton("tt:1:y", "Да", "callback", {"callback_data": "tt:1:y"}), CardButton("tt:1:n", "Нет", "callback", {"callback_data": "tt:1:n"})),)
            return ToolResult(notes=("A card was made. Do not mention buttons.",), phrase=True, cards=(Card("Занести в календарь?", buttons),))

        return Offered((ToolSpec("make_card", "makes a card", {"type": "object", "properties": {}}),), "## Card tool", run, 1)


async def test_a_tool_card_reaches_the_client_as_json_and_is_stored_with_the_reply(db, owner_engine):
    user = await register(db)
    chat = FakeChatLLM()
    chat.tool_responder = lambda system, turns, tools: ToolReply("Записал в календарь!", (ToolCall("make_card", {}),))
    svc = service(db, chat, tools=ToolRegistry([CardTool()]))
    client_id = uuid.uuid4()
    reply = await svc.handle_app_message(message(user, "завтра в 10 стоматолог", client_id=client_id))
    assert "Записал" not in reply.text  # the words next to a call are never sent: the reply is phrased again
    expected = {
        "text": "Занести в календарь?",
        "buttons": [[{"id": "tt:1:y", "label": "Да", "action": "callback", "payload": {"callback_data": "tt:1:y"}}, {"id": "tt:1:n", "label": "Нет", "action": "callback", "payload": {"callback_data": "tt:1:n"}}]],
        "auto_delete_seconds": None,
    }
    assert reply.cards == (expected,)
    again = await svc.handle_app_message(message(user, "завтра в 10 стоматолог", client_id=client_id))
    assert again.replayed and again.cards == (expected,)
    assert (await rows(owner_engine, user, "assistant"))[0][5] == [expected]


def test_telegram_buttons_become_neutral_card_buttons_and_back():
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Отменить", callback_data="sf:1:u"), InlineKeyboardButton(text="ОК", callback_data="fact_ack")],
            [InlineKeyboardButton(text="Открыть", url="https://t.me/c/1/2")],
        ]
    )
    card = Card("text", markup)  # a tool that still builds an aiogram keyboard keeps working
    assert card.markup == (
        (CardButton("sf:1:u", "Отменить", "callback", {"callback_data": "sf:1:u"}), CardButton("fact_ack", "ОК", "callback", {"callback_data": "fact_ack"})),
        (CardButton("https://t.me/c/1/2", "Открыть", "url", {"url": "https://t.me/c/1/2"}),),
    )
    assert telegram_markup(card.markup) == markup  # the Telegram adapter restores exactly what the tool built
    assert telegram_markup(markup) is markup and telegram_markup(None) is None
    assert card_to_json(card)["buttons"][1][0] == {"id": "https://t.me/c/1/2", "label": "Открыть", "action": "url", "payload": {"url": "https://t.me/c/1/2"}}
    assert Card("plain").markup is None and neutral_markup(InlineKeyboardMarkup(inline_keyboard=[])) is None


# ------------------------------------------------------------------ isolation from Telegram


def test_the_app_package_never_imports_aiogram_or_the_bot_handlers():
    offenders = []
    for path in APP_SRC.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            offenders += [f"{path.name}: {n}" for n in names if n.split(".")[0] == "aiogram" or n.startswith("ai_civ.bot.handlers") or n == "ai_civ.bot.orchestrator"]
    assert offenders == []


def test_the_whole_app_package_loads_in_a_clean_interpreter_without_aiogram_or_a_bot_token():
    code = (
        "import sys\n"
        "import ai_civ.app.chat_service, ai_civ.app.media, ai_civ.app.notifications, ai_civ.app.dto\n"
        "bad = [m for m in sys.modules if m == 'aiogram' or m.startswith('aiogram.') or m == 'ai_civ.bot.handlers' or m == 'ai_civ.bot.orchestrator']\n"
        "assert not bad, bad\n"
    )
    import os

    env = {k: v for k, v in os.environ.items() if k != "TELEGRAM_BOT_TOKEN"}
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, env=env, cwd=str(APP_SRC.parents[2]))
    assert result.returncode == 0, result.stderr[-1500:]


async def test_the_pipeline_works_with_no_telegram_token_in_the_environment(db, monkeypatch, owner_engine):
    import os

    assert "TELEGRAM_BOT_TOKEN" not in os.environ
    settings = make_settings()
    assert settings.telegram_bot_token is None
    user = await register(db)
    chat = FakeChatLLM()
    chat.transcriber = lambda data, mime: "голосом без телеграма"
    svc = service(db, chat, settings=settings)
    text_reply = await svc.handle_app_message(message(user))
    voice_reply = await svc.handle_app_message(message(user, "", voice_bytes=b"v" * 64))
    assert text_reply.text and voice_reply.user_text == "голосом без телеграма"
    assert len(await rows(owner_engine, user, "assistant")) == 2


# ------------------------------------------------------------------ the Telegram workers leave app people alone


async def test_the_telegram_schedulers_skip_a_person_without_telegram(db, owner_engine):
    user = await register(db)
    now = datetime(2026, 10, 6, 10, 30, tzinfo=UTC)
    await owner_exec(owner_engine, "UPDATE civ.users SET timezone = 'UTC', quiet_hours_start = '23:00', quiet_hours_end = '06:00' WHERE id = :u", u=user)
    await owner_exec(owner_engine, "INSERT INTO civ.messages (user_id, role, content, created_at) VALUES (:u, 'user', 'привет', :t)", u=user, t=now - timedelta(hours=10))
    await owner_exec(owner_engine, "INSERT INTO civ.proactive_schedule (user_id, trigger_key, local_time, enabled) VALUES (:u, 'meal_check', '10:30', true)", u=user)
    assert await schedule.claim_due(db, user, now=now, grace=timedelta(minutes=30)) == []
    due = await schedule.claim_checkin(db, user, now=now, grace=timedelta(minutes=30), slots=[time(10, 30)], min_silence=timedelta(hours=3), max_unanswered=2)
    assert due is None
    assert await schedule.review_due(db, user, now=now, weekday=now.weekday(), hour=0) is False
    assert (await owner_exec(owner_engine, "SELECT count(*) FROM civ.proactive_checkin_log WHERE user_id = :u", u=user))[0][0] == 0
    # the same person with a Telegram id (a control): the claim machinery still works for Telegram people
    await owner_exec(owner_engine, "UPDATE civ.users SET telegram_user_id = 424242 WHERE id = :u", u=user)
    assert await schedule.claim_due(db, user, now=now, grace=timedelta(minutes=30)) != []


# ------------------------------------------------------------------ notifications through the outbox


async def test_a_notification_is_queued_once_and_telegram_workers_cannot_claim_it(db, owner_engine):
    user = await register(db)
    publisher = OutboxPublisher(db)
    payload = {"title": "Напоминание", "body": "Пора пить воду"}
    assert await publisher.publish(user, "app_reminder", payload, idempotency_key="rem:1") is True
    assert await publisher.publish(user, "app_reminder", payload, idempotency_key="rem:1") is False
    stored = (await owner_exec(owner_engine, "SELECT kind, payload, status FROM civ.notification_outbox WHERE user_id = :u", u=user))
    assert len(stored) == 1 and stored[0][0] == "app_reminder" and stored[0][1] == payload and stored[0][2] == "pending"
    from ai_civ.bot import notifications

    assert await notifications.claim_next(db, user, lease_seconds=60) is None  # no forum group: the Telegram worker never takes it
    assert await db.tenants_with_pending_notifications(limit=50) == []


async def test_a_bad_notification_is_refused_and_the_application_role_cannot_insert_directly(db, owner_engine):
    user = await register(db)
    publisher = OutboxPublisher(db)
    with pytest.raises(DBAPIError):
        await publisher.publish(user, "Bad Kind!", {}, idempotency_key="k")
    with pytest.raises(DBAPIError):
        await publisher.publish(user, "app_push", {}, idempotency_key="  ")
    from sqlalchemy import text

    with pytest.raises(DBAPIError):
        async with db.tenant_session(user) as session:
            await session.execute(text("INSERT INTO civ.notification_outbox (user_id, kind, idempotency_key) VALUES (:u, 'app_push', 'direct')"), {"u": user})


async def test_one_person_cannot_publish_for_another(db, owner_engine):
    mine, other = await register(db, f"a.{uuid.uuid4().hex[:8]}@example.com"), await register(db, f"b.{uuid.uuid4().hex[:8]}@example.com")
    assert await OutboxPublisher(db).publish(mine, "app_push", {"x": 1}, idempotency_key="same") is True
    assert await OutboxPublisher(db).publish(other, "app_push", {"x": 1}, idempotency_key="same") is True  # the key is per person
    counts = {r[0]: r[1] for r in await owner_exec(owner_engine, "SELECT user_id, count(*) FROM civ.notification_outbox GROUP BY user_id")}
    assert counts[mine] == 1 and counts[other] == 1

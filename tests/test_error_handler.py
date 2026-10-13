"""Crash reports to the admin (bot/error_handler.py), the strict userbot session check and the Bot API retry session (bot/netretry.py)."""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace

import pytest
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetChat, GetChatMember, GetUpdates, SendMessage
from aiohttp_socks import ProxyConnectionError, ProxyError
from pydantic import SecretStr

from ai_civ.bot import error_handler, netretry
from ai_civ.bot.error_handler import build_report, celery_failure, redact, report_crash, report_crash_soon, report_crash_sync
from ai_civ.bot.netretry import RetryingSession
from ai_civ.bot.userbot import SESSION_LENGTH, SessionMalformed, TelethonApi, check_session
from ai_civ.config import Settings
from ai_civ.memory.llm import LLMError
from tests.tg_fakes import make_bot

ADMIN = 413277509
TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # the shape of a bot token; the fake bot of the tests uses its own


@pytest.fixture(autouse=True)
def _fresh_throttle():
    error_handler._seen.clear()
    yield
    error_handler._seen.clear()


def cfg(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def boom(message: str = "kaput") -> RuntimeError:
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


# ================================================================== the report itself


def test_the_report_is_a_named_txt_with_the_full_traceback_and_a_short_caption():
    name, text, caption = build_report(boom("db is gone"), where="bot:message", settings=cfg())

    assert re.fullmatch(r"crash_log_\d{8}_\d{6}\.txt", name)
    assert "Traceback (most recent call last)" in text and "RuntimeError: db is gone" in text and "where:     bot:message" in text
    assert caption == "🔥 bot:message\nRuntimeError: db is gone"


def test_a_long_message_cannot_overflow_the_caption_and_a_long_log_keeps_its_two_ends():
    _, _, caption = build_report(boom("x" * 5000), where="w", settings=cfg())
    assert len(caption) <= error_handler.CAPTION_LIMIT

    try:
        raise RuntimeError("HEAD-MARK " + "y" * 5000 + " TAIL-MARK")
    except RuntimeError as exc:
        _, text, _ = build_report(exc, where="w", settings=cfg(error_report_max_bytes=2000))
    assert len(text.encode()) < 2200 and "HEAD-MARK" in text and "TAIL-MARK" in text and "[... cut ...]" in text


def test_secrets_never_reach_the_file():
    session = "1" + "Ab3_-" * 70 + "Q" * 1  # 352 characters (the shape of a lost-padding session)
    s = cfg(telegram_bot_token=SecretStr(TOKEN), gemini_api_key=SecretStr("AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE12"), telegram_userbot_session=SecretStr(session))
    try:
        raise RuntimeError(
            f"GET https://api.telegram.org/bot{TOKEN}/getMe failed; key=AIzaSyFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE12; "
            f"dsn=postgresql://ai_civ_app:hunter2@db:5432/civ; session={session}"
        )
    except RuntimeError as exc:
        _, text, caption = build_report(exc, where="w", settings=s)

    for blob in (text, caption):
        assert TOKEN not in blob and "AIzaSyFAKE" not in blob and "hunter2" not in blob and session not in blob
    assert "[hidden]" in text and "postgresql://[hidden]@db:5432/civ" in text


def test_redact_without_settings_still_cuts_what_looks_like_a_secret():
    assert TOKEN not in redact(f"url bot{TOKEN}/sendMessage") and "hunter2" not in redact("redis://u:hunter2@host/0")
    assert redact("plain text 12345") == "plain text 12345"


async def test_the_file_goes_to_the_admin_as_a_document():
    bot, session = make_bot()
    sent = await report_crash(boom(), where="bot:message", settings=cfg(), bot=bot)

    assert sent
    [doc] = session.documents()
    assert doc.chat_id == ADMIN and doc.caption == "🔥 bot:message\nRuntimeError: kaput"
    assert doc.document.filename.startswith("crash_log_") and doc.document.filename.endswith(".txt")
    assert b"Traceback (most recent call last)" in doc.document.data  # the file is in memory: BufferedInputFile


async def test_the_admin_id_is_taken_from_the_settings():
    bot, session = make_bot()
    await report_crash(boom(), where="w", settings=cfg(feedback_admin_telegram_id=777), bot=bot)
    assert session.documents()[0].chat_id == 777


async def test_reports_can_be_switched_off():
    bot, session = make_bot()
    assert not await report_crash(boom(), where="w", settings=cfg(error_reports_enabled=False), bot=bot)
    assert session.calls == []


async def test_a_report_that_cannot_be_delivered_never_raises():
    bot, session = make_bot()
    session.fail_on("sendDocument", TelegramNetworkError)
    assert await report_crash(boom(), where="w", settings=cfg(), bot=bot) is False  # logged, not raised


async def test_the_same_error_from_the_same_place_is_sent_once_per_cooldown_and_the_next_one_counts_the_held_back():
    bot, session = make_bot()
    s = cfg(error_report_cooldown_seconds=300)

    def same():
        return boom("again")  # one raise statement: one place

    results = [await report_crash(same(), where="w", settings=s, bot=bot) for _ in range(4)]
    assert results == [True, False, False, False] and len(session.documents()) == 1

    other_place = await report_crash(boom("again"), where="elsewhere", settings=s, bot=bot)
    assert other_place and len(session.documents()) == 2  # another place is another report

    error_handler._seen[next(k for k in error_handler._seen if k[1] == "w")] = (-10_000.0, 3)  # the cooldown is over
    assert await report_crash(same(), where="w", settings=s, bot=bot)
    assert "3 identical held back" in session.documents()[-1].caption


async def test_fire_and_forget_does_not_wait_and_still_delivers():
    bot, session = make_bot()
    report_crash_soon(boom(), where="bot:message", settings=cfg(), bot=bot)
    assert session.documents() == []  # nothing yet: the caller was not held up
    await asyncio.gather(*error_handler._background)
    assert len(session.documents()) == 1


def test_a_sync_caller_without_a_loop_gets_its_report_through_a_private_loop(monkeypatch):
    delivered = []

    async def fake(exc, **kw):
        delivered.append((type(exc).__name__, kw["where"], kw["traceback_text"]))
        return True

    monkeypatch.setattr(error_handler, "report_crash", fake)
    assert report_crash_sync(boom(), where="celery:x", traceback_text="TB")
    assert delivered == [("RuntimeError", "celery:x", "TB")]


def test_the_celery_signal_handler_names_the_task_and_passes_its_traceback(monkeypatch):
    seen = {}

    async def fake(exc, **kw):
        seen.update(kw, exc=exc)
        return True

    monkeypatch.setattr(error_handler, "report_crash", fake)
    celery_failure(SimpleNamespace(name="ai_civ.send_cards"), "tid-1", boom("fail"), SimpleNamespace(traceback="CELERY TRACEBACK"), ("arg1",))

    assert seen["where"] == "celery:ai_civ.send_cards" and seen["traceback_text"] == "CELERY TRACEBACK"
    assert "task_id:   tid-1" in seen["context"] and "args:      ('arg1',)" in seen["context"]


def test_the_failure_signal_is_connected_to_the_celery_app():
    from celery.signals import task_failure

    from ai_civ import celery_app

    assert any(receiver is celery_app._report_task_failure for _, receiver, *_ in task_failure.receivers)


async def test_an_unretrieved_loop_error_is_reported(monkeypatch):
    bot, session = make_bot()
    loop = asyncio.get_running_loop()
    old = loop.get_exception_handler()
    error_handler.install_loop_handler(loop, cfg(), bot)
    try:
        loop.call_exception_handler({"message": "Task exception was never retrieved", "exception": boom("forgotten")})
        await asyncio.gather(*error_handler._background)
    finally:
        loop.set_exception_handler(old)
    [doc] = session.documents()
    assert "RuntimeError: forgotten" in doc.caption and "asyncio:" in doc.caption


# ================================================================== the places that call it


async def test_a_failed_message_sends_the_admin_a_file_and_the_user_the_usual_notice(bot_env):
    from tests.test_bot_orchestrator import FOOD_THREAD, food_persona

    env = bot_env
    env.settings.error_reports_enabled = True
    await food_persona(env)
    env.chat.responder = lambda system, turns: LLMError("503 from Gemini")

    await env.orchestrator.handle_message(env.incoming("hi", thread_id=FOOD_THREAD))
    await asyncio.gather(*error_handler._background)

    [doc] = env.session.documents()
    assert doc.chat_id == ADMIN and "LLMError: 503 from Gemini" in doc.caption and "bot:message" in doc.caption
    assert b"LLMError" in doc.document.data
    assert len(env.session.sent_to(FOOD_THREAD)) == 1  # the user still gets exactly one notice


async def test_an_error_that_escapes_a_handler_is_reported_by_the_dispatcher_hook(bot_env, monkeypatch):
    from ai_civ.bot import handlers

    env = bot_env
    env.settings.error_reports_enabled = True

    async def explode(*a, **kw):
        raise ValueError("handler bug")

    monkeypatch.setattr(env.orchestrator, "handle_message", explode)
    await env.feed("hello there")
    await asyncio.gather(*error_handler._background)

    assert handlers.on_error  # registered on the dispatcher (build_dispatcher)
    [doc] = env.session.documents()
    assert "ValueError: handler bug" in doc.caption and "bot:update" in doc.caption


# ================================================================== the userbot session guard


def real_session(ipv6: bool = False) -> str:
    """What ``python -m ai_civ.cli.userbot_login`` prints: the string Telethon itself writes."""
    import os

    from telethon.crypto import AuthKey
    from telethon.sessions import StringSession

    s = StringSession()
    s.set_dc(2, "2001:67c:4e8:f002::a" if ipv6 else "149.154.167.51", 443)
    s.auth_key = AuthKey(os.urandom(256))
    return s.save()


def test_a_real_telethon_session_passes_and_the_lengths_are_the_ones_telethon_writes():
    ipv4, ipv6 = real_session(), real_session(ipv6=True)
    assert (len(ipv4), len(ipv6)) == (SESSION_LENGTH, 369) == (353, 369)  # NOT 352: the version char is outside the base64 part
    assert check_session(ipv4) == ipv4 and check_session(f"  {ipv6}\r\n") == ipv6
    from telethon.sessions import StringSession

    StringSession(check_session(ipv4))  # and Telethon accepts what the check lets through


@pytest.mark.parametrize("ipv6", [False, True])
def test_the_lost_closing_equals_sign_is_put_back(ipv6):
    """The production crash: 352 characters = the session without its trailing "=" (a double-click selection drops it); Telethon said "Incorrect padding"."""
    session = real_session(ipv6)
    assert session.endswith("=")
    assert check_session(session[:-1]) == session
    from telethon.sessions import StringSession

    StringSession(check_session(session[:-1]))


@pytest.mark.parametrize("length", [0, 1, 275, 350, 351, 354, 400])
def test_a_session_of_any_other_length_is_refused_before_telethon_sees_it(length):
    with pytest.raises(SessionMalformed) as err:
        check_session("1" * length)
    if length:
        assert str(err.value) == f"Сессия юзербота повреждена. Длина: {length}. Ожидается: {SESSION_LENGTH}"


def test_the_right_length_is_not_enough_the_content_must_be_what_telethon_writes():
    ipv4 = real_session()
    for broken in ("2" + ipv4[1:], "1" + ipv4[1:-5] + "!!!!=", ipv4[:-1] + "%"):
        with pytest.raises(SessionMalformed):
            check_session(broken)


def test_the_session_itself_is_never_in_the_message():
    with pytest.raises(SessionMalformed) as err:
        check_session("1" + "SECRETSECRET" * 3)
    assert "SECRETSECRET" not in str(err.value)


async def test_a_malformed_session_stops_connect_before_telethon_is_imported(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def guarded(name, *a, **kw):
        if name.startswith("telethon"):
            raise AssertionError("Telethon must not be touched with a malformed session")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", guarded)
    s = cfg(telegram_api_id=1, telegram_api_hash=SecretStr("h"), telegram_userbot_session=SecretStr("1" + "A" * 368))
    with pytest.raises(SessionMalformed, match="Длина: 369. Ожидается: 353"):
        async with TelethonApi.connect(s):
            pass


async def test_a_malformed_session_reaches_the_admin_as_a_file_and_the_person_gets_the_manual_way():
    from contextlib import asynccontextmanager

    from ai_civ.bot import provision, texts
    from tests.test_userbot import MemRedis, settings as userbot_settings

    redis = MemRedis()
    store = provision.ProvisionStore(redis, prefix="t:prov")
    bot, session = make_bot()
    s = userbot_settings(telegram_userbot_session=SecretStr("1" + "A" * 274))

    @asynccontextmanager
    async def connect(settings):
        check_session(settings.telegram_userbot_session.get_secret_value())
        yield None  # pragma: no cover

    assert await provision.provision_group_async(s, bot, store, connect, owner_id=55, display_name="Аня", username="anya") is None

    [manual] = session.sent()
    assert manual.chat_id == 55 and manual.text == texts.PROVISION_FAILED
    [crash] = session.documents()
    assert crash.chat_id == ADMIN and "SessionMalformed: Сессия юзербота повреждена. Длина: 275. Ожидается: 353" in crash.caption
    assert b"Traceback" in crash.document.data


# ================================================================== the retry session


class Script:
    """Replaces ``AiohttpSession.make_request``: raises the queued errors one per call, then answers."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self, session, bot, method, timeout=None):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def method(name: str):
    return {"get_chat": GetChat(chat_id=1), "send": SendMessage(chat_id=1, text="x"), "member": GetChatMember(chat_id=1, user_id=2), "updates": GetUpdates()}[name]


def net(name: str, cause: BaseException) -> TelegramNetworkError:
    error = TelegramNetworkError(method=method(name), message="wrapped")
    error.__cause__ = cause
    return error


def make_session(monkeypatch, *errors, retries=2):
    script = Script(*errors)
    monkeypatch.setattr(AiohttpSession, "make_request", script)
    return RetryingSession(retries=retries, pause=0), script


async def run(session, name):
    return await session.make_request(make_bot()[0], method(name))


async def test_a_proxy_blip_on_get_chat_member_is_survived(monkeypatch):
    session, script = make_session(monkeypatch, ProxyError("Host unreachable"), ProxyError("Host unreachable"))  # the raw error of the production log
    assert await run(session, "member") == "ok" and script.calls == 3


async def test_a_proxy_that_stays_down_ends_in_a_telegram_network_error_after_the_bounded_attempts(monkeypatch):
    session, script = make_session(monkeypatch, *[ProxyError("Host unreachable")] * 5)
    with pytest.raises(TelegramNetworkError, match="ProxyError: Host unreachable"):
        await run(session, "member")
    assert script.calls == 3  # the first try + 2 repeats, never more


async def test_a_message_is_repeated_when_the_connection_was_never_made(monkeypatch):
    session, script = make_session(monkeypatch, ProxyConnectionError("proxy refused"))
    assert await run(session, "send") == "ok" and script.calls == 2


async def test_a_send_that_may_have_reached_telegram_is_not_repeated(monkeypatch):
    timeout = net("send", asyncio.TimeoutError())  # sent, but no answer: a second sendMessage could be a duplicate
    session, script = make_session(monkeypatch, timeout)
    with pytest.raises(TelegramNetworkError):
        await run(session, "send")
    assert script.calls == 1


async def test_a_read_is_repeated_even_after_a_timeout(monkeypatch):
    session, script = make_session(monkeypatch, net("get_chat", asyncio.TimeoutError()))
    assert await run(session, "get_chat") == "ok" and script.calls == 2


async def test_a_wrapped_connect_failure_is_found_through_the_cause_chain(monkeypatch):
    session, script = make_session(monkeypatch, net("send", ProxyError("Host unreachable")))
    assert await run(session, "send") == "ok" and script.calls == 2


async def test_polling_is_left_to_aiogram_and_real_api_answers_are_never_repeated(monkeypatch):
    session, script = make_session(monkeypatch, ProxyError("down"))
    with pytest.raises(TelegramNetworkError):
        await run(session, "updates")
    assert script.calls == 1

    session, script = make_session(monkeypatch, TelegramBadRequest(method=method("get_chat"), message="Bad Request: chat not found"))
    with pytest.raises(TelegramBadRequest):
        await run(session, "get_chat")
    assert script.calls == 1


async def test_zero_retries_switches_the_repeating_off(monkeypatch):
    session, script = make_session(monkeypatch, ProxyError("down"), retries=0)
    with pytest.raises(TelegramNetworkError):
        await run(session, "member")
    assert script.calls == 1


def test_the_bot_is_built_on_the_retrying_session_with_the_configured_numbers():
    from ai_civ.bot.app import build_bot

    bot = build_bot(cfg(telegram_bot_token=SecretStr(TOKEN), telegram_network_retries=4, telegram_network_retry_pause=0.5))
    assert isinstance(bot.session, RetryingSession) and (bot.session.retries, bot.session.pause) == (4, 0.5)


def test_helpers_classify_the_errors():
    assert netretry.is_read("getChatMember") and netretry.is_read("answerCallbackQuery") and not netretry.is_read("sendMessage") and not netretry.is_read("getUpdates")
    assert netretry.never_left_the_machine(ProxyError("x")) and not netretry.never_left_the_machine(asyncio.TimeoutError())

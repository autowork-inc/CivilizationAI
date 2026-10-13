"""The mobile app's HTTP API (``/api/v1/mobile``): JWT sign-up / sign-in / rotation of refresh tokens, topics, history and the SSE reply.

Real PostgreSQL (RLS), the real FastAPI app over ASGI, a fake Gemini (``FakeChatLLM``) and a fake Redis for the brute-force limiter. NOT verified here: the live Gemini stream,
a real phone, a real Redis (the limiter itself is covered by ``tests/test_ratelimit.py`` on a real one).
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio

from ai_civ.app.chat_service import AppChatService
from ai_civ.app.dto import AppIncomingMessage
from ai_civ.bot.ratelimit import RateLimiter
from ai_civ.config import Settings
from ai_civ.memory.llm import LLMError
from ai_civ.web.app import create_app
from ai_civ.web.app_auth_api import AuthRateLimiter
from ai_civ.web.deps_app import create_access_token
from tests.helpers import new_persona, owner_exec
from tests.test_ratelimit import Clock, FakeRedis
from tests.test_web_auth import TOKEN
from tests.tg_fakes import FakeChatLLM

SECRET = "unit-test-secret-" + "x" * 32
PASSWORD = "correct horse battery"
BASE = "/api/v1/mobile"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)


def make_settings(**kw: Any) -> Settings:
    kw.setdefault("app_dedup_poll_seconds", 0.05)
    return Settings(_env_file=None, telegram_bot_token=TOKEN, app_jwt_secret=SECRET, **kw)


def parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    """[(event, data)] of an SSE body; comment lines (keep-alive pings) are ignored."""
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in block.split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())
        if name is not None:
            events.append((name, json.loads("\n".join(data))))
    return events


@dataclass
class Pair:
    access: str
    refresh: str
    user_id: uuid.UUID

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access}"}


@dataclass
class Env:
    http: httpx.AsyncClient
    db: Any
    owner_engine: Any
    llm: FakeChatLLM
    service: AppChatService
    settings: Settings
    redis: FakeRedis
    clock: Clock

    async def register(self, email: str | None = None, password: str = PASSWORD, tz: str = "Europe/Copenhagen") -> Pair:
        email = email or f"anna.{uuid.uuid4().hex[:10]}@example.com"
        r = await self.http.post(f"{BASE}/auth/register", json={"email": email, "password": password, "timezone": tz})
        assert r.status_code == 201, r.text
        return self.pair(r.json())

    @staticmethod
    def pair(body: dict[str, Any]) -> Pair:
        return Pair(body["access_token"], body["refresh_token"], uuid.UUID(body["user_id"]))

    async def stream(self, who: Pair, text: str = "привет, как дела у тебя?", *, client_id: uuid.UUID | None = None, persona: uuid.UUID | None = None, **kw: Any):
        body = {"client_message_id": str(client_id or uuid.uuid4()), "persona_id": None if persona is None else str(persona), "text": text}
        return await self.http.post(f"{BASE}/chat/stream", json=body, headers=who.headers, **kw)

    async def sql(self, query: str, **params: Any) -> list[Any]:
        return await owner_exec(self.owner_engine, query, **params)


@pytest_asyncio.fixture
async def env(db, owner_engine):
    settings = make_settings()
    llm = FakeChatLLM(lambda system, turns: f"ответ на: {turns[-1].text}")
    service = AppChatService(db=db, chat_llm=llm, settings=settings)
    clock = Clock()
    redis = FakeRedis(clock)
    limiter = AuthRateLimiter(
        RateLimiter(redis, limit=1000, window_seconds=600, prefix="t:pair", clock=clock),
        RateLimiter(redis, limit=1000, window_seconds=600, prefix="t:ip", clock=clock),
    )
    app = create_app(settings, db=db, app_chat=service, auth_limiter=limiter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as http:
        yield Env(http, db, owner_engine, llm, service, settings, redis, clock)
    await service.aclose(5)


# ================================================================== sign-up, sign-in, tokens


async def test_the_whole_cycle_register_login_personas_refresh_logout(env):
    email = f"Boris.{uuid.uuid4().hex[:8]}@Example.com"
    first = await env.register(email)
    topics = await env.http.get(f"{BASE}/personas", headers=first.headers)
    assert topics.status_code == 200
    assert [p["code"] for p in topics.json()] == ["settings"]  # the feedback channel is Telegram-only
    assert set(topics.json()[0]) == {"id", "code", "title", "mode"} and topics.json()[0]["mode"] == "normal"

    login = await env.http.post(f"{BASE}/auth/login", json={"email": f"  {email.upper()} ", "password": PASSWORD})
    assert login.status_code == 200 and login.json()["token_type"] == "bearer" and login.json()["expires_in"] == 15 * 60
    second = env.pair(login.json())
    assert second.user_id == first.user_id and second.refresh != first.refresh
    assert (await env.http.get(f"{BASE}/personas", headers=second.headers)).status_code == 200

    refreshed = await env.http.post(f"{BASE}/auth/refresh", json={"refresh_token": second.refresh})
    assert refreshed.status_code == 200
    third = env.pair(refreshed.json())
    assert third.refresh != second.refresh and third.user_id == first.user_id
    assert (await env.http.get(f"{BASE}/personas", headers=third.headers)).status_code == 200

    out = await env.http.post(f"{BASE}/auth/logout", json={"refresh_token": third.refresh})
    assert out.status_code == 204 and out.content == b""
    gone = await env.http.post(f"{BASE}/auth/refresh", json={"refresh_token": third.refresh})
    assert gone.status_code == 401 and gone.json()["error"]["code"] == "invalid_refresh_token"


async def test_only_the_hash_of_the_refresh_token_is_stored_and_the_password_is_argon2(env):
    pair = await env.register()
    row = (await env.sql("SELECT refresh_token_hash FROM civ.user_sessions WHERE user_id = :u", u=pair.user_id))[0]
    secret = pair.refresh.split(".")[2]
    assert secret not in row[0] and len(row[0]) == 64
    stored = (await env.sql("SELECT pass_hash, onboarding_state, telegram_user_id FROM civ.users WHERE id = :u", u=pair.user_id))[0]
    assert stored[0].startswith("$argon2id$") and PASSWORD not in stored[0] and (stored[1], stored[2]) == ("done", None)


@pytest.mark.parametrize(
    "body",
    [
        {"email": "not-an-email", "password": PASSWORD},
        {"email": "a@b.c", "password": "short"},
        {"email": "a@b.c", "password": PASSWORD, "timezone": "Mars/Base"},
        {"email": "a@b.c", "password": PASSWORD, "timezone": "EST"},
        {"email": "a@b.c", "password": "x" * 129},
        {"email": "a@b.c", "password": PASSWORD, "is_admin": True},
        {"password": PASSWORD},
    ],
)
async def test_a_bad_registration_is_a_422_in_the_common_error_shape(env, body):
    r = await env.http.post(f"{BASE}/auth/register", json=body)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"


async def test_the_same_e_mail_in_any_case_is_taken(env):
    tag = uuid.uuid4().hex[:8]
    await env.register(f"Clara.{tag}@example.com")
    r = await env.http.post(f"{BASE}/auth/register", json={"email": f"CLARA.{tag}@EXAMPLE.com", "password": PASSWORD})
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_taken"


async def test_a_wrong_password_and_an_unknown_e_mail_get_the_same_answer(env):
    email = f"dina.{uuid.uuid4().hex[:8]}@example.com"
    await env.register(email)
    wrong = await env.http.post(f"{BASE}/auth/login", json={"email": email, "password": "not the password"})
    unknown = await env.http.post(f"{BASE}/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json() and wrong.json()["error"]["code"] == "invalid_credentials"


async def test_a_telegram_person_without_a_password_cannot_sign_in_by_e_mail(env):
    r = await env.http.post(f"{BASE}/auth/login", json={"email": "telegram.person@example.com", "password": PASSWORD})
    assert r.status_code == 401


# ------------------------------------------------------------------ access tokens


async def test_every_protected_route_refuses_a_missing_or_malformed_token(env):
    for headers in ({}, {"Authorization": "Bearer"}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer not.a.jwt"}, {"Authorization": "tma abc"}):
        for method, path in (("GET", "/personas"), ("GET", "/chat/history"), ("POST", "/chat/stream")):
            r = await env.http.request(method, BASE + path, headers=headers)
            assert r.status_code == 401, (method, path, headers)
            assert r.json()["error"]["code"] == "unauthorized" and r.headers["www-authenticate"] == "Bearer"


async def test_expired_forged_and_foreign_kind_tokens_are_refused(env):
    pair = await env.register()
    old = datetime.now(UTC) - timedelta(hours=1)
    expired, _ = create_access_token(env.settings, pair.user_id, now=old)
    r = await env.http.get(f"{BASE}/personas", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401 and r.json()["error"]["reason"] == "token_expired"  # the app knows to refresh

    now = datetime.now(UTC)
    base = {"sub": str(pair.user_id), "iss": "ai-civ", "iat": now, "exp": now + timedelta(minutes=5), "typ": "access"}
    bad = {
        "other key": jwt.encode(base, "another-secret-" + "y" * 32, algorithm="HS256"),
        "refresh kind": jwt.encode({**base, "typ": "refresh"}, SECRET, algorithm="HS256"),
        "no kind": jwt.encode({k: v for k, v in base.items() if k != "typ"}, SECRET, algorithm="HS256"),
        "no expiry": jwt.encode({k: v for k, v in base.items() if k != "exp"}, SECRET, algorithm="HS256"),
        "other issuer": jwt.encode({**base, "iss": "someone"}, SECRET, algorithm="HS256"),
        "sub is not a uuid": jwt.encode({**base, "sub": "42"}, SECRET, algorithm="HS256"),
        "alg none": jwt.encode(base, None, algorithm="none"),
    }
    for why, token in bad.items():
        r = await env.http.get(f"{BASE}/personas", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized", why


async def test_the_token_of_a_deleted_person_stops_working_at_once(env):
    pair = await env.register()
    assert (await env.http.get(f"{BASE}/personas", headers=pair.headers)).status_code == 200
    await env.sql("DELETE FROM civ.users WHERE id = :u", u=pair.user_id)
    r = await env.http.get(f"{BASE}/personas", headers=pair.headers)
    assert r.status_code == 401 and r.json()["error"]["reason"] == "unknown_user"


# ------------------------------------------------------------------ refresh: rotation and grace period


async def test_a_rotated_refresh_token_still_works_during_the_grace_period_and_not_after(env):
    pair = await env.register()
    first = await env.http.post(f"{BASE}/auth/refresh", json={"refresh_token": pair.refresh})
    again = await env.http.post(f"{BASE}/auth/refresh", json={"refresh_token": pair.refresh})  # the phone's second request with the same token
    assert first.status_code == again.status_code == 200
    assert first.json()["refresh_token"] != again.json()["refresh_token"]  # two valid, independent pairs: nobody is signed out
    for body in (first.json(), again.json()):
        assert (await env.http.get(f"{BASE}/personas", headers=env.pair(body).headers)).status_code == 200
        assert (await env.http.post(f"{BASE}/auth/refresh", json={"refresh_token": body["refresh_token"]})).status_code == 200

    session_id = pair.refresh.split(".")[1]
    left = (await env.sql("SELECT extract(epoch FROM expires_at - now()) FROM civ.user_sessions WHERE id = :s", s=uuid.UUID(session_id)))[0][0]
    assert 0 < left <= env.settings.app_refresh_grace_seconds  # shortened from 30 days to the grace period (and never extended by the second use)

    await env.sql("UPDATE civ.user_sessions SET expires_at = now() - interval '1 second' WHERE id = :s", s=uuid.UUID(session_id))  # "after the grace period"
    dead = await env.http.post(f"{BASE}/auth/refresh", json={"refresh_token": pair.refresh})
    assert dead.status_code == 401 and dead.json()["error"]["code"] == "invalid_refresh_token"


async def test_parallel_refreshes_with_one_token_all_succeed_inside_the_grace_period(env):
    pair = await env.register()
    answers = await asyncio.gather(*(env.http.post(f"{BASE}/auth/refresh", json={"refresh_token": pair.refresh}) for _ in range(5)))
    assert [r.status_code for r in answers] == [200] * 5
    assert len({r.json()["refresh_token"] for r in answers}) == 5
    assert (await env.sql("SELECT count(*) FROM civ.user_sessions WHERE user_id = :u", u=pair.user_id))[0][0] == 6


async def test_a_forged_or_foreign_refresh_token_is_refused(env):
    anna, boris = await env.register(), await env.register()
    user, session, secret = anna.refresh.split(".")
    forged = [
        f"{user}.{session}.{secret}x",  # wrong secret
        f"{user}.{session}.",  # no secret
        f"{boris.user_id}.{session}.{secret}",  # Anna's session under Boris's tenant: RLS shows Boris no such row
        f"{user}.{uuid.uuid4()}.{secret}",  # unknown session
        "garbage",
        f"{user}.not-a-uuid.{secret}",
        "",
    ]
    for token in forged:
        r = await env.http.post(f"{BASE}/auth/refresh", json={"refresh_token": token})
        assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_refresh_token", token
    assert (await env.http.post(f"{BASE}/auth/refresh", json={"refresh_token": anna.refresh})).status_code == 200  # the real one is untouched


async def test_logout_is_idempotent_silent_and_cannot_end_somebody_elses_session(env):
    anna, boris = await env.register(), await env.register()
    user, session, secret = anna.refresh.split(".")
    for token in ("garbage", f"{boris.user_id}.{session}.{secret}", f"{user}.{session}.wrong"):
        assert (await env.http.post(f"{BASE}/auth/logout", json={"refresh_token": token})).status_code == 204
    assert (await env.http.post(f"{BASE}/auth/refresh", json={"refresh_token": anna.refresh})).status_code == 200  # still alive
    for _ in range(2):
        assert (await env.http.post(f"{BASE}/auth/logout", json={"refresh_token": anna.refresh})).status_code == 204


async def test_expired_sessions_are_swept_when_a_new_one_is_made(env):
    pair = await env.register()
    await env.sql("UPDATE civ.user_sessions SET expires_at = now() - interval '1 day' WHERE user_id = :u", u=pair.user_id)
    await env.http.post(f"{BASE}/auth/login", json={"email": (await env.sql("SELECT email FROM civ.users WHERE id = :u", u=pair.user_id))[0][0], "password": PASSWORD})
    assert (await env.sql("SELECT count(*) FROM civ.user_sessions WHERE user_id = :u", u=pair.user_id))[0][0] == 1


# ================================================================== brute-force limit


async def test_login_and_register_are_limited_per_ip_and_e_mail_and_other_e_mails_are_not_affected(db, owner_engine):
    settings = make_settings()
    clock = Clock()
    redis = FakeRedis(clock)
    limiter = AuthRateLimiter(
        RateLimiter(redis, limit=3, window_seconds=600, prefix="t:pair", clock=clock),
        RateLimiter(redis, limit=100, window_seconds=600, prefix="t:ip", clock=clock),
    )
    app = create_app(settings, db=db, app_chat=None, auth_limiter=limiter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as http:
        guess = {"email": "victim@example.com", "password": "guess"}
        codes = [(await http.post(f"{BASE}/auth/login", json=guess)).status_code for _ in range(5)]
        assert codes == [401, 401, 401, 429, 429]
        blocked = await http.post(f"{BASE}/auth/login", json=guess)
        assert blocked.json()["error"]["code"] == "rate_limited" and int(blocked.headers["retry-after"]) >= 1
        assert (await http.post(f"{BASE}/auth/login", json={**guess, "email": "someone.else@example.com"})).status_code == 401  # a different pair
        register = [(await http.post(f"{BASE}/auth/register", json={"email": "victim@example.com", "password": PASSWORD})).status_code for _ in range(2)]
        assert register == [429, 429]  # the same budget guards /register for that pair
        assert redis.keys() and all(redis.ttl(k) is not None for k in redis.keys())


async def test_the_per_ip_counter_stops_one_address_spraying_many_e_mails(db):
    clock = Clock()
    redis = FakeRedis(clock)
    limiter = AuthRateLimiter(
        RateLimiter(redis, limit=100, window_seconds=600, prefix="t:pair", clock=clock),
        RateLimiter(redis, limit=4, window_seconds=600, prefix="t:ip", clock=clock),
    )
    app = create_app(make_settings(), db=db, app_chat=None, auth_limiter=limiter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as http:
        codes = [(await http.post(f"{BASE}/auth/login", json={"email": f"user{i}@example.com", "password": "guess"})).status_code for i in range(6)]
    assert codes == [401, 401, 401, 401, 429, 429]


async def test_the_limit_fails_open_when_redis_is_down(db):
    clock = Clock()
    redis = FakeRedis(clock)
    redis.error = ConnectionError("redis is down")
    limiter = AuthRateLimiter(
        RateLimiter(redis, limit=1, window_seconds=600, prefix="t:pair", clock=clock),
        RateLimiter(redis, limit=1, window_seconds=600, prefix="t:ip", clock=clock),
    )
    app = create_app(make_settings(), db=db, app_chat=None, auth_limiter=limiter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as http:
        codes = [(await http.post(f"{BASE}/auth/login", json={"email": "a@example.com", "password": "guess"})).status_code for _ in range(4)]
    assert codes == [401] * 4 and redis.commands > 0  # Redis was tried, and nobody was blocked


async def test_the_forwarded_for_header_is_ignored_unless_the_proxy_is_trusted(db):
    def build(trust: bool):
        clock = Clock()
        limiter = AuthRateLimiter(
            RateLimiter(FakeRedis(clock), limit=100, window_seconds=600, prefix="t:pair", clock=clock),
            RateLimiter(FakeRedis(clock), limit=2, window_seconds=600, prefix="t:ip", clock=clock),
        )
        return create_app(make_settings(app_trust_forwarded_for=trust), db=db, app_chat=None, auth_limiter=limiter)

    async def codes(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as http:
            return [
                (await http.post(f"{BASE}/auth/login", json={"email": "a@example.com", "password": "g"}, headers={"X-Forwarded-For": f"10.0.0.{i}"})).status_code
                for i in range(4)
            ]

    assert await codes(build(False)) == [401, 401, 429, 429]  # a forged header does not buy a fresh budget
    assert await codes(build(True)) == [401, 401, 401, 401]  # behind a trusted proxy each client address has its own


# ================================================================== the mobile API is optional


async def test_without_a_jwt_secret_the_mobile_routers_are_not_mounted_and_a_warning_is_logged(db, caplog):
    with caplog.at_level(logging.WARNING, logger="ai_civ.web.app"):
        app = create_app(Settings(_env_file=None, telegram_bot_token=TOKEN), db=db)
    assert any("APP_JWT_SECRET is not set" in r.getMessage() for r in caplog.records)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as http:
        assert (await http.post(f"{BASE}/auth/login", json={"email": "a@example.com", "password": PASSWORD})).status_code == 404
        assert (await http.get(f"{BASE}/personas")).status_code == 404
        assert (await http.get("/healthz")).status_code == 200  # the Mini App backend is untouched


def test_a_short_jwt_secret_is_refused_at_configuration_time():
    with pytest.raises(ValueError, match="at least 32"):
        Settings(_env_file=None, app_jwt_secret="too-short")
    assert Settings(_env_file=None, app_jwt_secret="  ").app_jwt_secret is None  # blank = off


async def test_chat_answers_503_when_no_model_is_configured(db):
    app = create_app(make_settings(), db=db, app_chat=None, auth_limiter=None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://app.test") as http:
        reg = await http.post(f"{BASE}/auth/register", json={"email": f"e.{uuid.uuid4().hex[:8]}@example.com", "password": PASSWORD})
        token = reg.json()["access_token"]
        r = await http.post(f"{BASE}/chat/stream", json={"client_message_id": str(uuid.uuid4()), "text": "hi"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "chat_unavailable"


# ================================================================== POST /chat/stream


async def test_the_reply_streams_as_token_events_and_ends_with_done(env):
    who = await env.register()
    client_id = uuid.uuid4()
    r = await env.stream(who, "расскажи что-нибудь интересное сегодня", client_id=client_id)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    names = [name for name, _ in events]
    assert names[-1] == "done" and names.count("done") == 1 and names.count("token") > 1 and "error" not in names  # genuinely streamed: several pieces
    text = "".join(data["text"] for name, data in events if name == "token")
    assert text == "ответ на: расскажи что-нибудь интересное сегодня"

    rows = await env.sql("SELECT id, role, content, client_message_id, reply_to_id FROM civ.messages WHERE user_id = :u ORDER BY created_at", u=who.user_id)
    assert [(row[1], row[2]) for row in rows] == [("user", "расскажи что-нибудь интересное сегодня"), ("assistant", text)]
    done = events[-1][1]
    assert done == {"user_message_id": str(rows[0][0]), "reply_message_id": str(rows[1][0])}
    assert rows[0][3] == client_id and rows[1][4] == rows[0][0]
    assert len(env.llm.stream_calls) == 1 and env.llm.calls == []  # the streaming path, not generate_text


async def test_a_repeated_request_is_answered_from_the_database_without_the_model(env):
    who = await env.register()
    client_id = uuid.uuid4()
    first = parse_sse((await env.stream(who, "первый раз спрашиваю", client_id=client_id)).text)
    calls_after_first = len(env.llm.stream_calls)

    again = parse_sse((await env.stream(who, "первый раз спрашиваю", client_id=client_id)).text)
    assert len(env.llm.stream_calls) == calls_after_first == 1  # Gemini was not asked again
    assert [name for name, _ in again] == ["token", "done"]  # the stored reply arrives at once
    assert again[0][1]["text"] == "".join(d["text"] for n, d in first if n == "token")
    assert again[-1] == first[-1]  # the same ids
    assert (await env.sql("SELECT count(*) FROM civ.messages WHERE user_id = :u", u=who.user_id))[0][0] == 2


async def test_two_simultaneous_requests_with_one_client_id_produce_one_reply(env):
    who = await env.register()
    client_id = uuid.uuid4()
    first, second = await asyncio.gather(env.stream(who, "одновременно", client_id=client_id), env.stream(who, "одновременно", client_id=client_id))
    a, b = parse_sse(first.text), parse_sse(second.text)
    assert a[-1] == b[-1] and a[-1][0] == "done"
    assert "".join(d["text"] for n, d in a if n == "token") == "".join(d["text"] for n, d in b if n == "token")
    assert len(env.llm.stream_calls) == 1
    assert (await env.sql("SELECT count(*) FROM civ.messages WHERE user_id = :u", u=who.user_id))[0][0] == 2


async def test_a_failing_model_gives_an_error_event_and_the_abandoned_message_is_removed(env):
    who = await env.register()
    env.llm.responder = lambda system, turns: LLMError("Gemini is down: secret-internal-detail")
    client_id = uuid.uuid4()
    r = await env.stream(who, "ответь мне пожалуйста", client_id=client_id)
    assert r.status_code == 200
    events = parse_sse(r.text)
    assert [name for name, _ in events] == ["error"]
    assert set(events[0][1]) == {"error", "code"} and events[0][1]["code"] == "reply_failed"
    assert "secret-internal-detail" not in r.text  # the cause stays in the log
    assert (await env.sql("SELECT count(*) FROM civ.messages WHERE user_id = :u", u=who.user_id))[0][0] == 0

    env.llm.responder = lambda system, turns: "теперь работает"  # the retry with the SAME id starts clean (does not wait for a dead message)
    ok = parse_sse((await env.stream(who, "ответь мне пожалуйста", client_id=client_id)).text)
    assert ok[-1][0] == "done" and "".join(d["text"] for n, d in ok if n == "token") == "теперь работает"


async def test_a_stream_that_breaks_half_way_ends_with_an_error_event_and_no_stored_messages(env):
    who = await env.register()
    env.llm.responder = lambda system, turns: "одна две три четыре пять шесть семь восемь"
    env.llm.stream_break_after = 2
    events = parse_sse((await env.stream(who, "расскажи длинно")).text)
    names = [n for n, _ in events]
    assert names == ["token", "token", "error"]  # the client discards what it got when it sees the error
    assert (await env.sql("SELECT count(*) FROM civ.messages WHERE user_id = :u", u=who.user_id))[0][0] == 0


async def test_the_reply_is_finished_and_stored_even_if_nobody_reads_the_stream(env):
    who = await env.register()
    client_id = uuid.uuid4()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> None:
        started.set()
        await release.wait()

    env.llm.on_call = slow
    events = await env.service.open_stream(
        AppIncomingMessage(user_id=who.user_id, persona_id=None, client_message_id=client_id, text="я закрыл приложение")
    )
    del events  # the client is gone: nobody drains the queue
    await started.wait()
    assert len(env.service.tasks) == 1  # the task is held by the service, not by the request
    release.set()
    await env.service.aclose(5)
    assert not env.service.tasks
    rows = await env.sql("SELECT role, content FROM civ.messages WHERE user_id = :u ORDER BY created_at", u=who.user_id)
    assert [r[0] for r in rows] == ["user", "assistant"]

    env.llm.on_call = None
    retry = parse_sse((await env.stream(who, "я закрыл приложение", client_id=client_id)).text)  # the app comes back: the reply is there
    assert retry[0][1]["text"] == rows[1][1] and len(env.llm.stream_calls) == 1


async def test_the_background_task_uses_its_own_sessions_not_the_requests(env, monkeypatch):
    """The reply task touches the database only through ``db.tenant_session(tenant)``, one short transaction per step (never a session handed in by the route)."""
    who = await env.register()
    opened: list[uuid.UUID] = []
    real = env.db.tenant_session

    def spy(user_id):
        opened.append(user_id)
        return real(user_id)

    monkeypatch.setattr(env.db, "tenant_session", spy)
    events = parse_sse((await env.stream(who, "проверка сессий")).text)
    assert events[-1][0] == "done"
    assert len(opened) >= 4 and set(opened) == {who.user_id}


async def test_a_voice_message_is_transcribed_and_the_done_event_carries_the_words(env):
    who = await env.register()
    env.llm.transcriber = lambda data, mime: "купи молоко завтра утром"
    files = {"voice": ("note.m4a", b"\x00\x01fake-audio", "audio/mp4")}
    r = await env.http.post(f"{BASE}/chat/stream", data={"client_message_id": str(uuid.uuid4()), "text": ""}, files=files, headers=who.headers)
    events = parse_sse(r.text)
    assert events[-1][0] == "done" and events[-1][1]["user_text"] == "купи молоко завтра утром"
    assert env.llm.transcribe_calls[0]["mime_type"] == "audio/mp4"
    assert (await env.sql("SELECT content FROM civ.messages WHERE user_id = :u AND role = 'user'", u=who.user_id))[0][0] == "купи молоко завтра утром"


async def test_an_unintelligible_voice_message_is_an_error_event_and_stores_nothing(env):
    who = await env.register()
    env.llm.transcriber = lambda data, mime: "[NO_SPEECH]"
    r = await env.http.post(
        f"{BASE}/chat/stream", data={"client_message_id": str(uuid.uuid4())}, files={"voice": ("n.ogg", b"abc", "audio/ogg")}, headers=who.headers
    )
    events = parse_sse(r.text)
    assert [n for n, _ in events] == ["error"] and events[0][1]["code"] == "voice_unclear"
    assert (await env.sql("SELECT count(*) FROM civ.messages WHERE user_id = :u", u=who.user_id))[0][0] == 0


async def test_a_text_file_is_attached_through_multipart_and_shows_in_the_history(env):
    who = await env.register()
    r = await env.http.post(
        f"{BASE}/chat/stream",
        data={"client_message_id": str(uuid.uuid4()), "text": "о чём этот файл?"},
        files={"attachment": ("notes.txt", ("Список дел на неделю. " * 20).encode(), "text/plain")},
        headers=who.headers,
    )
    assert parse_sse(r.text)[-1][0] == "done"
    page = (await env.http.get(f"{BASE}/chat/history", headers=who.headers)).json()
    user = page["messages"][0]
    assert user["role"] == "user" and user["attachment"] == {
        "kind": "document", "content_kind": "txt", "mime_type": "text/plain", "file_name": "notes.txt", "size_bytes": len(("Список дел на неделю. " * 20).encode())
    }
    assert page["messages"][1]["attachment"] is None


async def test_what_the_person_can_fix_is_an_ordinary_http_error_before_the_stream(env):
    who = await env.register()
    other = await env.register()
    foreign = await new_persona(env.owner_engine, other.user_id, "foodguide")
    cases = [
        ({"client_message_id": str(uuid.uuid4()), "text": "   "}, 422, "empty_message"),
        ({"client_message_id": str(uuid.uuid4()), "text": "hi", "persona_id": str(uuid.uuid4())}, 404, "unknown_persona"),
        ({"client_message_id": str(uuid.uuid4()), "text": "hi", "persona_id": str(foreign)}, 404, "unknown_persona"),  # somebody else's topic is invisible (RLS)
        ({"client_message_id": "nope", "text": "hi"}, 422, "invalid_request"),
        ({"text": "hi"}, 422, "invalid_request"),
        ({"client_message_id": str(uuid.uuid4()), "text": "x" * 20_001}, 422, "invalid_request"),
    ]
    for body, status, code in cases:
        r = await env.http.post(f"{BASE}/chat/stream", json=body, headers=who.headers)
        assert (r.status_code, r.json()["error"]["code"]) == (status, code), body
    feedback = (await env.sql("SELECT id FROM civ.personas WHERE user_id = :u AND code = 'feedback'", u=who.user_id))[0][0]
    r = await env.stream(who, persona=feedback)
    assert r.status_code == 403 and r.json()["error"]["code"] == "persona_unavailable"
    assert (await env.http.post(f"{BASE}/chat/stream", content=b"x", headers={**who.headers, "Content-Type": "text/plain"})).status_code == 415
    both = await env.http.post(
        f"{BASE}/chat/stream", data={"client_message_id": str(uuid.uuid4())},
        files={"voice": ("a.ogg", b"1", "audio/ogg"), "attachment": ("b.txt", b"2", "text/plain")}, headers=who.headers,
    )
    assert both.status_code == 422 and both.json()["error"]["code"] == "one_media"
    assert env.llm.stream_calls == [] and env.llm.calls == []  # nothing reached the model


async def test_a_file_over_the_limit_is_refused_unread(env):
    who = await env.register()
    small = make_settings(voice_max_bytes=10)
    env.http._transport.app.state.settings = small  # the route reads the limits from the settings of the app
    r = await env.http.post(
        f"{BASE}/chat/stream", data={"client_message_id": str(uuid.uuid4())}, files={"voice": ("a.ogg", b"x" * 11, "audio/ogg")}, headers=who.headers
    )
    assert r.status_code == 413 and r.json()["error"]["code"] == "voice_too_large"


async def test_a_topic_reply_is_stored_under_that_topic(env):
    who = await env.register()
    topic = (await env.http.get(f"{BASE}/personas", headers=who.headers)).json()[0]["id"]
    events = parse_sse((await env.stream(who, "в этой теме", persona=uuid.UUID(topic))).text)
    assert events[-1][0] == "done"
    rows = await env.sql("SELECT DISTINCT persona_id FROM civ.messages WHERE user_id = :u", u=who.user_id)
    assert [str(r[0]) for r in rows] == [topic]


# ================================================================== GET /chat/history


async def seed(env, user: uuid.UUID, rows: list[tuple[str, str, uuid.UUID | None]], *, cards: dict[int, list] | None = None) -> list[uuid.UUID]:
    """(role, text, persona) one minute apart, oldest first; returns the ids."""
    ids = []
    start = datetime.now(UTC) - timedelta(hours=2)
    for i, (role, text, persona) in enumerate(rows):
        got = await env.sql(
            "INSERT INTO civ.messages (user_id, persona_id, role, content, created_at, cards) "
            "VALUES (:u, :p, CAST(:r AS civ.message_role), :c, :t, CAST(:cards AS jsonb)) RETURNING id",
            u=user, p=persona, r=role, c=text, t=start + timedelta(minutes=i), cards=json.dumps((cards or {}).get(i)) if (cards or {}).get(i) else None,
        )
        ids.append(got[0][0])
    return ids


async def test_history_without_a_topic_is_the_main_chat_only(env):
    who = await env.register()
    topic = (await env.http.get(f"{BASE}/personas", headers=who.headers)).json()[0]["id"]
    ids = await seed(env, who.user_id, [("user", "m1", None), ("assistant", "m2", None), ("user", "t1", uuid.UUID(topic)), ("assistant", "m3", None)])
    main = (await env.http.get(f"{BASE}/chat/history", headers=who.headers)).json()
    assert [m["text"] for m in main["messages"]] == ["m1", "m2", "m3"] and main["has_more"] is False  # oldest first; the topic's message is not here
    assert [m["id"] for m in main["messages"]] == [str(ids[0]), str(ids[1]), str(ids[3])]
    topical = (await env.http.get(f"{BASE}/chat/history", params={"persona_id": topic}, headers=who.headers)).json()
    assert [m["text"] for m in topical["messages"]] == ["t1"] and topical["messages"][0]["persona_id"] == topic
    assert set(main["messages"][0]) == {"id", "role", "text", "created_at", "persona_id", "client_message_id", "reply_to_id", "cards", "attachment"}


async def test_history_pages_backwards_with_before_id_and_limit(env):
    who = await env.register()
    ids = await seed(env, who.user_id, [("user" if i % 2 == 0 else "assistant", f"m{i}", None) for i in range(7)])
    page1 = (await env.http.get(f"{BASE}/chat/history", params={"limit": 3}, headers=who.headers)).json()
    assert [m["text"] for m in page1["messages"]] == ["m4", "m5", "m6"] and page1["has_more"] is True
    page2 = (await env.http.get(f"{BASE}/chat/history", params={"limit": 3, "before_id": page1["messages"][0]["id"]}, headers=who.headers)).json()
    assert [m["text"] for m in page2["messages"]] == ["m1", "m2", "m3"] and page2["has_more"] is True
    page3 = (await env.http.get(f"{BASE}/chat/history", params={"limit": 3, "before_id": page2["messages"][0]["id"]}, headers=who.headers)).json()
    assert [m["text"] for m in page3["messages"]] == ["m0"] and page3["has_more"] is False
    assert str(ids[0]) == page3["messages"][0]["id"]


async def test_history_pagination_survives_messages_with_the_same_timestamp(env):
    who = await env.register()
    when = datetime.now(UTC) - timedelta(hours=1)
    for i in range(5):
        await env.sql(
            "INSERT INTO civ.messages (user_id, role, content, created_at) VALUES (:u, 'user', :c, :t)", u=who.user_id, c=f"same{i}", t=when
        )
    seen: list[str] = []
    before = None
    for _ in range(5):
        params = {"limit": 2, **({"before_id": before} if before else {})}
        page = (await env.http.get(f"{BASE}/chat/history", params=params, headers=who.headers)).json()
        seen += [m["text"] for m in page["messages"]]
        before = page["messages"][0]["id"]
        if not page["has_more"]:
            break
    assert sorted(seen) == [f"same{i}" for i in range(5)] and len(seen) == 5  # no message twice, none lost


async def test_history_carries_the_cards_of_a_reply(env):
    who = await env.register()
    card = {"text": "Запомнил", "buttons": [{"text": "Отменить", "action": "undo"}]}
    await seed(env, who.user_id, [("user", "запомни", None), ("assistant", "ок", None)], cards={1: [card]})
    messages = (await env.http.get(f"{BASE}/chat/history", headers=who.headers)).json()["messages"]
    assert messages[0]["cards"] == [] and messages[1]["cards"] == [card]


async def test_history_input_is_validated_and_foreign_ids_do_not_exist(env):
    anna, boris = await env.register(), await env.register()
    anna_ids = await seed(env, anna.user_id, [("user", "секрет Анны", None)])
    anna_topic = (await env.http.get(f"{BASE}/personas", headers=anna.headers)).json()[0]["id"]
    for params in ({"limit": 0}, {"limit": 101}, {"limit": "x"}, {"before_id": "nope"}, {"persona_id": "nope"}):
        assert (await env.http.get(f"{BASE}/chat/history", params=params, headers=anna.headers)).status_code == 422, params
    r = await env.http.get(f"{BASE}/chat/history", params={"persona_id": str(uuid.uuid4())}, headers=anna.headers)
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_persona"
    r = await env.http.get(f"{BASE}/chat/history", params={"before_id": str(uuid.uuid4())}, headers=anna.headers)
    assert r.status_code == 404 and r.json()["error"]["code"] == "unknown_message"
    # Boris sees nothing of Anna: not her messages, not her topic, not an anchor taken from her history
    assert (await env.http.get(f"{BASE}/chat/history", headers=boris.headers)).json()["messages"] == []
    assert (await env.http.get(f"{BASE}/chat/history", params={"persona_id": anna_topic}, headers=boris.headers)).status_code == 404
    assert (await env.http.get(f"{BASE}/chat/history", params={"before_id": str(anna_ids[0])}, headers=boris.headers)).status_code == 404
    assert anna_topic not in {p["id"] for p in (await env.http.get(f"{BASE}/personas", headers=boris.headers)).json()}


async def test_personas_lists_only_active_topics_of_the_caller(env):
    anna, boris = await env.register(), await env.register()
    live = await new_persona(env.owner_engine, anna.user_id, "foodguide", title="🥗 Питание")
    dead = await new_persona(env.owner_engine, anna.user_id, "gym", title="🏋️ Зал")
    await env.sql("UPDATE civ.personas SET is_active = false WHERE id = :p", p=dead)
    listed = (await env.http.get(f"{BASE}/personas", headers=anna.headers)).json()
    assert {p["code"] for p in listed} == {"settings", "foodguide"} and str(live) in {p["id"] for p in listed}
    assert {p["code"] for p in (await env.http.get(f"{BASE}/personas", headers=boris.headers)).json()} == {"settings"}


# ================================================================== over a REAL socket: the pieces arrive while the model is still writing


async def _serve(app):
    import socket

    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    return server, task, f"http://127.0.0.1:{port}"


async def test_tokens_reach_the_client_while_the_reply_is_still_being_written(env):
    who = await env.register()
    env.llm.responder = lambda system, turns: "один два три четыре пять шесть семь восемь"
    env.llm.stream_pause = 0.25  # 4 pieces, a quarter of a second apart
    server, task, url = await _serve(env.http._transport.app)
    try:
        loop = asyncio.get_running_loop()
        arrivals: list[tuple[str, float]] = []
        async with httpx.AsyncClient(base_url=url, timeout=20) as live:
            async with live.stream(
                "POST", f"{BASE}/chat/stream", json={"client_message_id": str(uuid.uuid4()), "text": "считай вслух по порядку"}, headers=who.headers
            ) as response:
                assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        arrivals.append((line[6:].strip(), loop.time()))
        names = [n for n, _ in arrivals]
        assert names[-1] == "done" and names.count("token") == 4
        first_token, done = arrivals[0][1], arrivals[-1][1]
        assert done - first_token > 0.5  # the first piece was in the client's hands long before the reply was complete
    finally:
        server.should_exit = True
        await task


async def test_a_client_that_hangs_up_mid_stream_does_not_lose_the_reply(env):
    who = await env.register()
    client_id = uuid.uuid4()
    env.llm.responder = lambda system, turns: "раз два три четыре пять шесть семь восемь"
    env.llm.stream_pause = 0.2
    server, task, url = await _serve(env.http._transport.app)
    try:
        async with httpx.AsyncClient(base_url=url, timeout=20) as live:
            async with live.stream(
                "POST", f"{BASE}/chat/stream", json={"client_message_id": str(client_id), "text": "а потом пропаду со связи"}, headers=who.headers
            ) as response:
                async for line in response.aiter_lines():
                    if line.startswith("event: token"):
                        break  # the phone goes into a tunnel: the connection is dropped after the first piece
        await env.service.aclose(10)  # the reply task is not the connection's: it finishes
        rows = await env.sql("SELECT role, content FROM civ.messages WHERE user_id = :u ORDER BY created_at", u=who.user_id)
        assert [r[0] for r in rows] == ["user", "assistant"] and rows[1][1] == "раз два три четыре пять шесть семь восемь"
        env.llm.stream_pause = 0
        async with httpx.AsyncClient(base_url=url, timeout=20) as live:
            back = await live.post(f"{BASE}/chat/stream", json={"client_message_id": str(client_id), "text": "а потом пропаду со связи"}, headers=who.headers)
        replay = parse_sse(back.text)
        assert [n for n, _ in replay] == ["token", "done"] and replay[0][1]["text"] == rows[1][1] and len(env.llm.stream_calls) == 1
    finally:
        server.should_exit = True
        await task


async def test_no_database_transaction_is_open_while_the_model_is_generating(env):
    """The decisive check of "short sessions": WHILE Gemini is being called (inside ``stream_text``) the database must hold no transaction of the application role at all."""
    who = await env.register()
    seen: list[int] = []

    async def probe() -> None:
        rows = await env.sql(
            "SELECT count(*) FROM pg_stat_activity WHERE usename = 'ai_civ_app' AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
        )
        seen.append(rows[0][0])

    env.llm.on_call = probe
    env.llm.stream_pause = 0.05
    events = parse_sse((await env.stream(who, "пока модель думает, база свободна")).text)
    assert events[-1][0] == "done"
    assert seen == [0]  # the probe ran once, inside the model call, and found no open transaction

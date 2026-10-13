"""Sign-up, sign-in and token rotation of the mobile app: ``/api/v1/mobile/auth``.

* **Password**: argon2id (``argon2-cffi``), hashed / verified in a worker thread (CPU-heavy by design). An unknown e-mail on ``/login`` is verified against a dummy hash, so the
  answer takes as long as for a real one and says the same thing (``invalid_credentials``).
* **Access token**: 15-minute HS256 JWT (``deps_app``). **Refresh token**: ``<user_id>.<session_id>.<secret>`` - opaque, 256 bits of secret; the database keeps only SHA-256 of the secret
  in ``user_sessions`` (a high-entropy secret needs no slow hash). The token carries the user id because the table is under RLS: the tenant session is opened first, then the row is found.
* **Rotation with a grace period**: ``/refresh`` issues a NEW pair and shortens the old session to ``app_refresh_grace_seconds`` (10 s) instead of deleting it. A phone that fires two
  requests with the same refresh token (a flaky network, two parallel calls) gets two valid pairs and is not signed out; after the grace period the old token is dead. The row is locked
  (``FOR UPDATE``), so parallel refreshes are serialised, never lost.
* **Brute force**: ``/register`` and ``/login`` are limited per (IP, e-mail) and per IP by the same Redis fixed-window limiter as the bot's (``bot/ratelimit.py``), fail-open: a dead Redis
  never blocks anybody (warning in the log).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import time
import uuid
from datetime import timedelta
from functools import cache
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, update

from ai_civ.bot.ratelimit import RateLimiter
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.models import User
from ai_civ.db.models.app import UserSession
from ai_civ.web.deps import ApiError
from ai_civ.web.deps_app import AppDB, create_access_token
from ai_civ.web.profile import valid_iana_zone

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mobile/auth", tags=["mobile-auth"])

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")  # the same shape as the database CHECK (ck_users_email); delivery is not verified here
PASSWORD_MIN, PASSWORD_MAX = 8, 128  # the upper bound keeps one request from making the server hash a megabyte


# ------------------------------------------------------------------ passwords

_hasher = PasswordHasher()


@cache
def _dummy_hash() -> str:
    return _hasher.hash("not-a-real-password")


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_hasher.hash, password)


async def verify_password(stored: str, password: str) -> bool:
    try:
        return await asyncio.to_thread(_hasher.verify, stored, password)
    except (VerificationError, InvalidHashError):  # a mismatch, or a hash of a kind this server does not know: either way, not this password
        return False


# ------------------------------------------------------------------ refresh tokens


def _secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def parse_refresh_token(token: str) -> tuple[uuid.UUID, uuid.UUID, str] | None:
    parts = token.strip().split(".")
    if len(parts) != 3 or not parts[2]:
        return None
    try:
        return uuid.UUID(parts[0]), uuid.UUID(parts[1]), parts[2]
    except ValueError:
        return None


# ------------------------------------------------------------------ brute-force limit


class AuthRateLimiter:
    """Two Redis counters per attempt: (IP, e-mail) and IP alone (so one address cannot spray many e-mails). Fail-open (``RateLimiter`` never raises)."""

    def __init__(self, pair: RateLimiter, per_ip: RateLimiter) -> None:
        self.pair, self.per_ip = pair, per_ip

    @classmethod
    def from_settings(cls, settings: Settings) -> AuthRateLimiter | None:
        if not settings.rate_limit_enabled:
            return None
        from redis.asyncio import Redis

        timeout = settings.rate_limit_redis_timeout_seconds
        client = Redis.from_url(
            settings.rate_limit_redis_url or settings.celery_broker_url, socket_timeout=timeout, socket_connect_timeout=timeout, decode_responses=True
        )
        window = settings.app_auth_rate_limit_window_seconds
        prefix = f"{settings.rate_limit_key_prefix}:appauth"
        return cls(
            RateLimiter(client, limit=settings.app_auth_rate_limit_attempts, window_seconds=window, prefix=f"{prefix}:pair", timeout_seconds=timeout),
            RateLimiter(client, limit=settings.app_auth_rate_limit_ip_attempts, window_seconds=window, prefix=f"{prefix}:ip", timeout_seconds=timeout),
        )

    async def aclose(self) -> None:
        await self.pair.aclose()

    async def check(self, ip: str, email: str) -> int | None:
        """None = allowed; otherwise the seconds until the window ends (for ``Retry-After``)."""
        digest = hashlib.sha256(email.lower().encode()).hexdigest()[:16]
        pair = await self.pair.hit(f"{ip}|{digest}")
        per_ip = await self.per_ip.hit(ip)  # counted even when the pair is already refused: the IP counter measures all of its attempts
        if pair.allowed and per_ip.allowed:
            return None
        window = self.pair.window
        return window - int(time.time() % window)


def client_ip(request: Request) -> str:
    settings: Settings = request.app.state.settings
    if settings.app_trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        if forwarded:
            return forwarded
    return request.client.host if request.client else "unknown"


async def enforce_limit(request: Request, email: str) -> None:
    limiter: AuthRateLimiter | None = getattr(request.app.state, "auth_limiter", None)
    if limiter is None:
        return
    wait = await limiter.check(client_ip(request), email)
    if wait is not None:
        log.info("auth rate limit hit by %s", client_ip(request))
        raise ApiError(429, "rate_limited", "Слишком много попыток. Подождите немного.", headers={"Retry-After": str(max(1, wait))}, details={"retry_after": max(1, wait)})


# ------------------------------------------------------------------ shapes


def _clean_email(value: str) -> str:
    email = value.strip().lower()
    if not (3 <= len(email) <= 254) or not EMAIL_RE.match(email):
        raise ValueError("Укажите адрес электронной почты.")
    return email


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)
    password: str = Field(max_length=PASSWORD_MAX)
    timezone: str = Field(default="UTC", max_length=64)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _clean_email(value)

    @field_validator("password")
    @classmethod
    def _password(cls, value: str) -> str:
        if len(value) < PASSWORD_MIN:
            raise ValueError(f"Пароль должен быть не короче {PASSWORD_MIN} символов.")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        return valid_iana_zone(value)


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=320)
    password: str = Field(max_length=PASSWORD_MAX)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _clean_email(value)


class RefreshIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(max_length=400)


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds of life of the access token
    user_id: uuid.UUID
    timezone: str


# ------------------------------------------------------------------ sessions


async def issue_session(db: Database, settings: Settings, tenant_id: uuid.UUID, timezone: str, user_agent: str | None, *, session: Any = None) -> TokenPair:
    """A new device session: the row (hash only) and the pair of tokens. ``session`` = an open tenant session to write into (``/refresh`` does it in its own transaction)."""
    session_id = uuid.uuid4()
    secret = secrets.token_urlsafe(32)
    row = UserSession(
        id=session_id,
        user_id=tenant_id,
        refresh_token_hash=_secret_hash(secret),
        user_agent=(user_agent or "")[:300] or None,
        expires_at=func.now() + timedelta(days=settings.app_refresh_ttl_days),
    )
    if session is not None:
        session.add(row)
        await session.execute(delete(UserSession).where(UserSession.user_id == tenant_id, UserSession.expires_at < func.now()))  # expired sessions are garbage
        await session.flush()
    else:
        async with db.tenant_session(tenant_id) as own:
            own.add(row)
            await own.execute(delete(UserSession).where(UserSession.user_id == tenant_id, UserSession.expires_at < func.now()))
    access, lifetime = create_access_token(settings, tenant_id)
    return TokenPair(access_token=access, refresh_token=f"{tenant_id}.{session_id}.{secret}", expires_in=lifetime, user_id=tenant_id, timezone=timezone)


# ------------------------------------------------------------------ routes


@router.post("/register", response_model=TokenPair, status_code=201)
async def register(body: RegisterIn, request: Request, db: AppDB) -> TokenPair:
    await enforce_limit(request, body.email)
    result = await db.register_app_user(email=body.email, pass_hash=await hash_password(body.password), timezone=body.timezone)
    if result.outcome != "created" or result.tenant_id is None:
        raise ApiError(409, "email_taken", "Этот адрес уже зарегистрирован.")
    return await issue_session(db, request.app.state.settings, result.tenant_id, body.timezone, request.headers.get("user-agent"))


@router.post("/login", response_model=TokenPair)
async def login(body: LoginIn, request: Request, db: AppDB) -> TokenPair:
    await enforce_limit(request, body.email)
    creds = await db.resolve_tenant_by_email(body.email)
    ok = await verify_password(creds.pass_hash if creds else _dummy_hash(), body.password)  # an unknown e-mail costs the same time as a known one
    if creds is None or not ok:
        raise ApiError(401, "invalid_credentials", "Неверная почта или пароль.")
    return await issue_session(db, request.app.state.settings, creds.tenant_id, creds.timezone, request.headers.get("user-agent"))


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshIn, request: Request, db: AppDB) -> TokenPair:
    settings: Settings = request.app.state.settings
    invalid = ApiError(401, "invalid_refresh_token", "Войдите заново.")
    parsed = parse_refresh_token(body.refresh_token)
    if parsed is None:
        raise invalid
    user_id, session_id, secret = parsed
    async with db.tenant_session(user_id) as session:  # RLS: only the sessions of this person are visible, a foreign id finds nothing
        row = (
            await session.execute(
                select(UserSession.refresh_token_hash, UserSession.expires_at > func.now()).where(UserSession.id == session_id).with_for_update()
            )
        ).one_or_none()
        if row is None or not hmac.compare_digest(row[0], _secret_hash(secret)) or not row[1]:
            raise invalid
        # rotate: the old token lives on for the grace period only (never longer than it had), a new session starts
        await session.execute(
            update(UserSession)
            .where(UserSession.id == session_id)
            .values(expires_at=func.least(UserSession.expires_at, func.now() + timedelta(seconds=settings.app_refresh_grace_seconds)))
        )
        timezone = (await session.execute(select(User.timezone).where(User.id == user_id))).scalar_one()
        return await issue_session(db, settings, user_id, timezone, request.headers.get("user-agent"), session=session)


@router.post("/logout", status_code=204)
async def logout(body: RefreshIn, db: AppDB) -> Response:
    """Idempotent and silent: a token that is unknown, expired or already gone gets the same 204 (nothing is revealed about it)."""
    parsed = parse_refresh_token(body.refresh_token)
    if parsed is not None:
        user_id, session_id, secret = parsed
        async with db.tenant_session(user_id) as session:
            await session.execute(delete(UserSession).where(UserSession.id == session_id, UserSession.refresh_token_hash == _secret_hash(secret)))
    return Response(status_code=204)

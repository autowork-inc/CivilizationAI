"""Authentication of the mobile app: ``Authorization: Bearer <access token>`` -> ``AppAuthContext``.

The access token is a short-lived HS256 JWT (``sub`` = the tenant id, ``typ`` = ``access``). Long sessions are the job of the refresh token (``app_auth_api``). The tenant's time zone is
read from the database on EVERY request (one indexed row, under RLS): a token of a deleted person stops working at once, and a changed time zone is never stale.

Every failure is ``ApiError(401, "unauthorized", ...)``, the same error contract as the rest of ``ai_civ.web``; ``details.reason`` lets the app tell "refresh me" (``token_expired``)
from "sign in again" (anything else).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select

from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.models import User
from ai_civ.web.deps import ApiError

log = logging.getLogger(__name__)

ALGORITHM = "HS256"
ISSUER = "ai-civ"
BEARER = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class AppAuthContext:
    tenant_id: uuid.UUID
    timezone: str  # users.timezone as stored


def unauthorized(reason: str, message: str = "Войдите заново.") -> ApiError:
    return ApiError(401, "unauthorized", message, headers=dict(BEARER), details={"reason": reason})


def _secret(settings: Settings) -> str:
    if settings.app_jwt_secret is None:  # the routers are not mounted without it; this keeps the type honest
        raise ApiError(503, "mobile_api_off", "Мобильный API не настроен.")
    return settings.app_jwt_secret.get_secret_value()


def create_access_token(settings: Settings, tenant_id: uuid.UUID, *, now: datetime | None = None) -> tuple[str, int]:
    """(token, lifetime in seconds)."""
    issued = now or datetime.now(UTC)
    lifetime = timedelta(minutes=settings.app_access_ttl_minutes)
    claims = {"sub": str(tenant_id), "iss": ISSUER, "typ": "access", "iat": issued, "exp": issued + lifetime}
    return jwt.encode(claims, _secret(settings), algorithm=ALGORITHM), int(lifetime.total_seconds())


def decode_access_token(settings: Settings, token: str) -> uuid.UUID:
    """The tenant id of a valid access token. Raises ``ApiError`` 401 for anything else (expired, forged, another kind of token, junk)."""
    try:
        claims = jwt.decode(
            token,
            _secret(settings),
            algorithms=[ALGORITHM],  # a fixed list: the token cannot choose "none" or another algorithm
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError:
        raise unauthorized("token_expired", "Сессия истекла. Обновите токен.") from None
    except jwt.PyJWTError:
        raise unauthorized("invalid_token") from None
    if claims.get("typ") != "access":
        raise unauthorized("invalid_token")
    try:
        return uuid.UUID(str(claims["sub"]))
    except ValueError:
        raise unauthorized("invalid_token") from None


def get_db(request: Request) -> Database:
    return request.app.state.db


async def authenticate_app(request: Request, authorization: Annotated[str | None, Header()] = None) -> AppAuthContext:
    scheme, _, raw = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not raw.strip():
        raise unauthorized("missing_token", "Нужна авторизация.")
    tenant_id = decode_access_token(request.app.state.settings, raw.strip())
    db: Database = request.app.state.db
    async with db.tenant_session(tenant_id) as session:
        timezone = (await session.execute(select(User.timezone).where(User.id == tenant_id))).scalar_one_or_none()
    if timezone is None:  # the person was deleted after the token was issued
        raise unauthorized("unknown_user")
    return AppAuthContext(tenant_id=tenant_id, timezone=timezone)


AppAuth = Annotated[AppAuthContext, Depends(authenticate_app)]
AppDB = Annotated[Database, Depends(get_db)]

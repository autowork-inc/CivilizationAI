"""Shared FastAPI pieces: the error type, ``authenticate`` (the ONLY place where a tenant is derived) and the ``Auth`` dependency.

    Authorization: tma <initData>   ->   Telegram user id (signed)   ->   civ.resolve_tenant_id()   ->   tenant_session(...)

Data access inside a route is ``async with db.tenant_session(ctx.tenant_id)``: RLS then makes foreign rows invisible even if a
query forgets a filter, and a route that forgot the tenant context fails (500) instead of leaking.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from sqlalchemy import select

from ai_civ.config import ConfigError, Settings
from ai_civ.db import Database
from ai_civ.db.enums import OnboardingState
from ai_civ.db.models import User
from ai_civ.web.auth import InitData, InitDataError, validate_init_data

log = logging.getLogger(__name__)


class ApiError(Exception):
    """Rendered as ``{"error": {"code", "message", **details}}`` with the given status (see ``app.create_app``)."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"{status} {code}")
        self.status, self.code, self.message = status, code, message
        self.headers, self.details = headers or {}, details or {}


def conflict(message: str, **details: Any) -> ApiError:
    """409: the page edited a stale version. ``details`` carries what the page needs to refresh itself."""
    return ApiError(409, "conflict", message, details=details)


AUTH_MESSAGES = {  # the UI shows these as-is: short, no internals
    "init_data_missing": "Откройте приложение из Telegram.",
    "init_data_expired": "Сессия устарела. Закройте приложение и откройте его снова.",
}
DEFAULT_AUTH_MESSAGE = "Не удалось подтвердить, что это вы. Откройте приложение из Telegram заново."


@dataclass(frozen=True)
class AuthContext:
    tenant_id: uuid.UUID
    timezone: str  # users.timezone as stored (NOT the browser's zone: they may differ, see Module 4 plan, risk 1)
    init: InitData


def get_db(request: Request) -> Database:
    return request.app.state.db


async def authenticate(request: Request, authorization: Annotated[str | None, Header()] = None) -> AuthContext:
    settings: Settings = request.app.state.settings
    db: Database = request.app.state.db
    scheme, _, raw = (authorization or "").partition(" ")
    if scheme.lower() != "tma" or not raw.strip():
        raise ApiError(401, "init_data_missing", AUTH_MESSAGES["init_data_missing"], headers={"WWW-Authenticate": "tma"})
    if settings.telegram_bot_token is None:  # create_app refuses to start without it; this keeps the type honest
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set")
    try:
        init = validate_init_data(
            raw.strip(),
            settings.telegram_bot_token.get_secret_value(),
            max_age_seconds=settings.init_data_max_age_seconds,
        )
    except InitDataError as exc:
        log.info("rejected launch data: %s", exc)  # the code only; the data itself is never logged
        raise ApiError(
            401, exc.code, AUTH_MESSAGES.get(exc.code, DEFAULT_AUTH_MESSAGE), headers={"WWW-Authenticate": "tma"}
        ) from None

    tenant_id = await db.resolve_tenant_id(telegram_user_id=init.telegram_user_id)
    if tenant_id is None:
        log.info("launch data of unregistered telegram user %s", init.telegram_user_id)
        raise ApiError(403, "not_registered", "Вас пока нет в системе. Напишите администратору.")
    async with db.tenant_session(tenant_id) as session:
        row = (await session.execute(select(User.timezone, User.onboarding_state).where(User.id == tenant_id))).one()
    if OnboardingState(row.onboarding_state) is not OnboardingState.done:
        raise ApiError(403, "onboarding_required", "Сначала познакомьтесь с ботом: ответьте на его вопросы в чате.")
    return AuthContext(tenant_id=tenant_id, timezone=row.timezone, init=init)


Auth = Annotated[AuthContext, Depends(authenticate)]
DB = Annotated[Database, Depends(get_db)]

"""The Mini App backend: ``python -m ai_civ.web``. A SEPARATE process from the Telegram bot (Long Polling) on purpose: the web
server can restart or fall over without touching the bot's event loop, and the other way round (Fail-Fast, CLAUDE.md section 2).

Start-up is fail-fast, like the bot's: no ``TELEGRAM_BOT_TOKEN`` -> ``ConfigError`` before any connection (the token is the key
that validates every request); a database role that bypasses RLS -> ``RlsNotEnforcedError``. The runtime role is ``ai_civ_app``.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ai_civ.app.chat_service import AppChatService
from ai_civ.config import ConfigError, Settings, get_settings
from ai_civ.db import Database
from ai_civ.memory.llm import GeminiClient
from ai_civ.web import app_api, app_auth_api
from ai_civ.web.api import router
from ai_civ.web.app_auth_api import AuthRateLimiter
from ai_civ.web.deps import ApiError

log = logging.getLogger(__name__)

# Until the Svelte build (next step) is mounted, "/" is a smoke page: it opens inside Telegram, sends the signed launch data
# to /api/me and prints the answer, which proves the whole chain (tunnel -> BotFather URL -> signature -> tenant -> RLS).
SMOKE_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>AI Civilization</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  body { margin: 0; padding: 16px; font: 16px/1.4 system-ui, sans-serif;
         background: var(--tg-theme-bg-color, #fff); color: var(--tg-theme-text-color, #000); }
  pre { white-space: pre-wrap; background: var(--tg-theme-secondary-bg-color, #f1f1f1); padding: 12px; border-radius: 12px; }
</style></head>
<body>
<h3>Бэкенд на связи</h3>
<pre id="out">Проверяю подпись…</pre>
<script>
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) { tg.ready(); tg.expand(); }
  const out = document.getElementById("out");
  fetch("/api/me", { headers: { Authorization: "tma " + (tg ? tg.initData : "") } })
    .then(async (r) => { out.textContent = r.status + "\\n" + JSON.stringify(await r.json(), null, 2); })
    .catch((e) => { out.textContent = "Нет связи: " + e; });
</script>
</body></html>"""


def create_app(
    settings: Settings | None = None,
    db: Database | None = None,
    *,
    app_chat: AppChatService | None = None,
    auth_limiter: AuthRateLimiter | None = None,
) -> FastAPI:
    """``db`` is for tests (an already built Database, left open); production builds and disposes its own. ``app_chat`` / ``auth_limiter`` likewise (tests inject fakes;
    production builds the chat service on Gemini when ``GEMINI_API_KEY`` is set, and the limiter on Redis).

    The mobile API (``/api/v1/mobile``) exists only when ``APP_JWT_SECRET`` is set; without it the routers are not mounted at all and the Mini App works as before."""
    settings = settings or get_settings()
    if settings.telegram_bot_token is None:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set (see .env.example): the web backend validates every request with it")

    mobile_api = settings.app_jwt_secret is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = db or Database.from_settings(settings)
        gemini: GeminiClient | None = None
        chat, limiter = app_chat, auth_limiter
        try:
            await database.verify_rls_enforced()  # fail-fast: never serve tenants as a role that bypasses RLS
            app.state.db = database
            if mobile_api:
                if chat is None and settings.gemini_api_key is not None:
                    gemini = GeminiClient(settings)
                    chat = AppChatService(db=database, chat_llm=gemini, embedder=gemini, settings=settings)  # tools=None: the app has no functions for the model (yet)
                if chat is None:
                    log.warning("GEMINI_API_KEY is not set: POST /api/v1/mobile/chat/stream answers 503")
                if limiter is None:
                    limiter = AuthRateLimiter.from_settings(settings)
                app.state.app_chat, app.state.auth_limiter = chat, limiter
            log.info("web backend up; Mini App Direct Link: %s; mobile API: %s", settings.mini_app_direct_link, "on" if mobile_api else "off")
            yield
        finally:
            if chat is not None and app_chat is None:
                await chat.aclose()  # let the replies in flight store their answers
            if limiter is not None and auth_limiter is None:
                await limiter.aclose()
            if gemini is not None:
                await gemini.aclose()
            if db is None:
                await database.dispose()

    # No interactive docs / OpenAPI: the API is private to the Mini App and there is no reason to advertise its shape.
    app = FastAPI(title="ai-civ-web", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.app_chat, app.state.auth_limiter = app_chat, auth_limiter
    if db is not None:
        app.state.db = db

    @app.exception_handler(ApiError)
    async def on_api_error(_: Request, exc: ApiError) -> JSONResponse:
        details = {"fields": [], **exc.details} if exc.code == "invalid_request" else exc.details  # same shape as a schema 422
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message, **details}},
            status_code=exc.status,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def on_invalid_request(_: Request, exc: RequestValidationError) -> JSONResponse:
        """One error shape for the page: our validators write Russian messages, which are shown as they are."""
        problems = [
            {"field": ".".join(str(part) for part in e["loc"] if part not in ("body", "query", "path")), "message": e["msg"].removeprefix("Value error, ")}
            for e in exc.errors()
        ]
        message = problems[0]["message"] if problems else "Некорректный запрос."
        return JSONResponse({"error": {"code": "invalid_request", "message": message, "fields": problems}}, status_code=422)

    @app.middleware("http")
    async def no_store(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        response = await call_next(request)
        if not request.url.path.startswith("/assets/"):  # hashed build files may be cached, everything else is personal / session-bound
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    dist = dist_dir(settings)
    if (dist / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(dist / "index.html", media_type="text/html")

        log.info("serving the Mini App build from %s", dist)
    else:
        log.warning("no Mini App build in %s (run `npm run build` in web/): serving the smoke page", dist)

        @app.get("/", response_class=HTMLResponse, include_in_schema=False)
        async def smoke() -> str:
            return SMOKE_PAGE

    app.include_router(router)
    if mobile_api:
        app.include_router(app_auth_api.router)
        app.include_router(app_api.router)

        @app.get("/mobile-test", include_in_schema=False)
        async def mobile_test() -> FileResponse:
            """A bare-bones page that talks to the mobile API (sign-up / sign-in / streamed chat): for trying the API from a phone's browser. Not part of the product."""
            return FileResponse(Path(__file__).parent / "static" / "mobile_test.html", media_type="text/html")

    else:
        log.warning("APP_JWT_SECRET is not set: the mobile API (/api/v1/mobile) is NOT mounted")
    return app


def dist_dir(settings: Settings) -> Path:
    """Where the built Svelte app lives: WEB_DIST_DIR, else <project root>/web/dist (src/ai_civ/web/app.py -> three levels up)."""
    if settings.web_dist_dir:
        return Path(settings.web_dist_dir)
    return Path(__file__).resolve().parents[3] / "web" / "dist"

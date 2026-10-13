"""Process entry point: ``python -m ai_civ.bot`` (Long Polling, so no public URL / webhook is needed locally).

Start-up is fail-fast: a missing token, a role that bypasses RLS (see ``Runtime``) or a rejected token (``getMe``) stop the
process before it polls. After that, errors inside a message are contained by the Orchestrator and aiogram's own
backoff handles network blips of ``getUpdates``; the process is meant to run under a supervisor (systemd, Docker
restart policy) for anything worse.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from aiogram import Bot

from ai_civ import runtime
from ai_civ.bot import error_handler
from ai_civ.bot.handlers import build_dispatcher
from ai_civ.bot.netretry import RetryingSession
from ai_civ.bot.orchestrator import Orchestrator
from ai_civ.config import ConfigError, Settings, get_settings

log = logging.getLogger(__name__)

# callback_query: the weekly review buttons. my_chat_member: the bot was added to / removed from a group (registration, dormant
# tenants). chat_member: somebody joined / left a registered group (the privacy guard); Telegram sends it only to an admin bot
# and only when asked for explicitly. Edits, reactions... are not handled.
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member", "chat_member"]


def build_bot(settings: Settings) -> Bot:
    if settings.telegram_bot_token is None:
        raise ConfigError("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    # aiohttp-socks knows "socks5" only (it resolves names through the proxy by default, which is what "socks5h" asks for elsewhere).
    proxy = settings.telegram_proxy.replace("socks5h://", "socks5://", 1) if settings.telegram_proxy else None
    session = RetryingSession(
        proxy=proxy, timeout=settings.telegram_request_timeout, retries=settings.telegram_network_retries, pause=settings.telegram_network_retry_pause
    )
    return Bot(token=settings.telegram_bot_token.get_secret_value(), session=session)


@asynccontextmanager
async def open_bot(settings: Settings) -> AsyncIterator[Bot]:
    """A Bot whose HTTP session is always closed (Celery tasks open one per run; the poller one per process)."""
    bot = build_bot(settings)
    try:
        yield bot
    finally:
        await bot.session.close()


async def run_polling() -> None:
    settings = get_settings()
    async with open_bot(settings) as bot:  # first: a missing token is a config error, before any connection is made
        async with runtime.open_runtime(True) as rt:  # RLS verified, Gemini client built (GEMINI_API_KEY required)
            router_llm, chat_llm = rt.require_chat()
            me = await bot.get_me()  # fail-fast on a bad token / no network
            orchestrator = Orchestrator(
                db=rt.db,
                router_llm=router_llm,
                chat_llm=chat_llm,
                settings=rt.settings,
                bot=bot,
                embedder=rt.embedder,  # Tier 2 (episodic memory) in replies; RECALL_ENABLED=false switches it off
            )
            dispatcher = build_dispatcher(rt.db, orchestrator)
            error_handler.install_loop_handler(asyncio.get_running_loop(), rt.settings, bot)  # what nobody awaited is logged AND sent to the admin
            log.info(
                "bot @%s started (Long Polling); chat model %s; voice notes %s",
                me.username, rt.settings.gemini_chat_model, f"gemini {rt.settings.gemini_transcribe_model}" if rt.settings.voice_enabled else "off",
            )  # fmt: skip
            await dispatcher.start_polling(
                bot,
                allowed_updates=ALLOWED_UPDATES,
                polling_timeout=rt.settings.telegram_polling_timeout,
                close_bot_session=False,  # open_bot() closes it
            )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_polling())

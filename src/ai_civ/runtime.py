"""Per-task runtime: database (role ai_civ_app, RLS verified) + Gemini client.

Celery tasks are synchronous entry points; each one runs ``asyncio.run`` and builds its own runtime INSIDE that event
loop (an asyncpg engine must not outlive or cross event loops, and prefork children must not share a parent's sockets).
The Telegram bot is one long-lived process: it opens ONE runtime at start-up and keeps it for the life of the polling loop.
Tests replace the factory with ``set_runtime_factory`` so no real Gemini call is ever made.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass

from ai_civ.config import Settings, get_settings
from ai_civ.db import Database
from ai_civ.memory.llm import Embedder, GeminiClient, JsonLLM, TextLLM


@dataclass(frozen=True)
class Runtime:
    db: Database
    settings: Settings
    llm: JsonLLM | None = None
    embedder: Embedder | None = None
    chat: TextLLM | None = None

    def require_llm(self) -> tuple[JsonLLM, Embedder]:
        if self.llm is None or self.embedder is None:
            raise RuntimeError("this runtime was opened without an LLM (with_llm=False)")
        return self.llm, self.embedder

    def require_chat(self) -> tuple[JsonLLM, TextLLM]:
        """The two models the orchestrator needs: JSON (topic router) and plain text (the answer itself)."""
        if self.llm is None or self.chat is None:
            raise RuntimeError("this runtime was opened without an LLM (with_llm=False)")
        return self.llm, self.chat


RuntimeFactory = Callable[[bool], AbstractAsyncContextManager[Runtime]]


@asynccontextmanager
async def default_runtime(with_llm: bool) -> AsyncIterator[Runtime]:
    settings = get_settings()
    db = Database.from_settings(settings)
    gemini: GeminiClient | None = None
    try:
        await db.verify_rls_enforced()  # fail-fast: never run background jobs as a role that bypasses RLS
        if with_llm:
            gemini = GeminiClient(settings)  # raises ConfigError if GEMINI_API_KEY is missing
        yield Runtime(db=db, settings=settings, llm=gemini, embedder=gemini, chat=gemini)
    finally:
        if gemini is not None:
            await gemini.aclose()
        await db.dispose()


_factory: RuntimeFactory = default_runtime


def set_runtime_factory(factory: RuntimeFactory | None) -> None:
    """Override (tests) or restore (None) the runtime factory."""
    global _factory
    _factory = default_runtime if factory is None else factory


def open_runtime(with_llm: bool) -> AbstractAsyncContextManager[Runtime]:
    return _factory(with_llm)

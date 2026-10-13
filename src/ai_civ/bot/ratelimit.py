"""Protection of the Gemini budget (and of the chat) from a user who writes too fast: at most ``RATE_LIMIT_MESSAGES`` messages per
``RATE_LIMIT_WINDOW_SECONDS`` (20 per minute) per Telegram user. Over the limit the update is DROPPED before any handler runs - no database
write, no embedding, no model call - and the user gets a fixed text, ONCE per window.

Redis, fixed window: the key is ``<prefix>:<telegram user id>:<window number>``, the window number being ``floor(time / window)``. One
MULTI/EXEC transaction does ``SET key 0 NX EX ttl`` + ``INCR key``, so a key can never exist without an expiry (a crash between the two commands
would otherwise leave a counter that blocks the user for ever). The notice is a second key set with ``NX``: whoever sets it first sends the text.

Fail-open (user decision): the limit is a safeguard, not a feature. If Redis is down, slow or answers nonsense, the message goes through and a
warning is logged (at most once a minute, not once per message). Redis is a cache in this system (CLAUDE.md): its failure never stops the bot.

Placement: an INNER middleware of the orchestrator router, i.e. after ``TenantMiddleware``. Only registered people in an active group get here, so
the limiter can never make the bot speak in a frozen group or to a stranger, and only messages that reach a handler (text, commands, voice notes -
a voice note counts as one message) are counted. Registration and the invite handshake have their own throttles and are not limited here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from ai_civ.bot import texts
from ai_civ.config import Settings

log = logging.getLogger(__name__)

WARN_EVERY_SECONDS = 60.0  # how often a Redis failure is reported in the log


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    notify: bool = False  # True only for the FIRST dropped message of a window: this one gets the notice


class RateLimiter:
    def __init__(
        self,
        redis: Any,
        *,
        limit: int,
        window_seconds: int,
        prefix: str = "civ:rl",
        timeout_seconds: float = 1.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if limit < 1 or window_seconds < 1:
            raise ValueError("the rate limit and its window must be positive")
        self._redis = redis
        self.limit = limit
        self.window = window_seconds
        self._prefix = prefix
        self._timeout = timeout_seconds
        self._clock = clock
        self._last_warning = float("-inf")

    @classmethod
    def from_settings(cls, settings: Settings) -> RateLimiter | None:
        """None = switched off (``RATE_LIMIT_ENABLED=false``). The client connects lazily on the first message, so building it never
        needs Redis to be up (the bot starts either way); every command has a short timeout."""
        if not settings.rate_limit_enabled:
            return None
        from redis.asyncio import Redis

        timeout = settings.rate_limit_redis_timeout_seconds
        client = Redis.from_url(
            settings.rate_limit_redis_url or settings.celery_broker_url,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            decode_responses=True,
        )
        return cls(
            client,
            limit=settings.rate_limit_messages,
            window_seconds=settings.rate_limit_window_seconds,
            prefix=settings.rate_limit_key_prefix,
            timeout_seconds=timeout,
        )

    async def aclose(self) -> None:
        try:
            await self._redis.aclose()
        except Exception:  # noqa: BLE001 - shutting down
            log.debug("closing the rate limiter's Redis client failed", exc_info=True)

    async def hit(self, user_id: int | str) -> Verdict:
        """Count one message of this user. Never raises (except cancellation): any failure of Redis means "allowed"."""
        window_number = int(self._clock() // self.window)
        key = f"{self._prefix}:{user_id}:{window_number}"
        ttl = self.window * 2  # the key outlives its window by a whole window: no clock skew can resurrect a counter
        try:
            count = await asyncio.wait_for(self._count(key, ttl), timeout=self._timeout)
            if count <= self.limit:
                return Verdict(True)
            first = await asyncio.wait_for(self._redis.set(f"{key}:notice", 1, nx=True, ex=ttl), timeout=self._timeout)
        except Exception:  # noqa: BLE001 - fail-open (CancelledError is not an Exception)
            self._warn(user_id)
            return Verdict(True)
        return Verdict(False, notify=bool(first))

    async def _count(self, key: str, ttl: int) -> int:
        pipe = self._redis.pipeline(transaction=True)  # MULTI/EXEC: the counter never exists without its expiry
        pipe.set(key, 0, nx=True, ex=ttl)
        pipe.incr(key)
        _, count = await pipe.execute()
        if not isinstance(count, int) or isinstance(count, bool):
            raise TypeError(f"Redis answered {count!r} to INCR")
        return count

    def _warn(self, user_id: int | str) -> None:
        now = time.monotonic()
        if now - self._last_warning >= WARN_EVERY_SECONDS:
            self._last_warning = now
            log.warning("rate limiter: Redis is not usable, messages pass unlimited (fail-open); seen for user %s", user_id, exc_info=True)
        else:
            log.debug("rate limiter: Redis is not usable (fail-open)", exc_info=True)


class RateLimitMiddleware(BaseMiddleware):
    """Inner middleware of the router that answers users (see the module docstring for why inner)."""

    def __init__(self, limiter: RateLimiter) -> None:
        self.limiter = limiter

    async def __call__(
        self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message) or event.from_user is None or event.from_user.is_bot or not (event.text or event.voice):
            return await handler(event, data)  # not something a person wrote for the bot (service messages, other bots): not counted
        verdict = await self.limiter.hit(event.from_user.id)
        if verdict.allowed:
            return await handler(event, data)
        log.info("rate limit: a message of telegram user %s in chat %s was dropped (over %d per %ds)",
                 event.from_user.id, event.chat.id, self.limiter.limit, self.limiter.window)  # fmt: skip
        if verdict.notify:
            try:
                await event.answer(texts.RATE_LIMITED)  # aiogram answers in the topic of the message by itself
            except Exception:  # noqa: BLE001 - a courtesy: the drop already happened
                log.warning("could not deliver the rate-limit notice", exc_info=True)
        return None

"""A Bot API session that survives a short blip of the proxy (WARP) instead of failing the whole handler.

What is repeated (at most ``retries`` times, with a pause that grows by ``pause`` seconds per attempt; never an endless loop):

* the connection was never made (the proxy said "Host unreachable", the connector could not connect): the request did not leave the machine,
  so ANY method is safe to repeat;
* the request may already have reached Telegram (a timeout, a dropped connection): only reads are repeated (``getChat``, ``getChatMember``,
  ``answerCallbackQuery``...). ``sendMessage`` is not: a second attempt after an unseen success would be a duplicate message.

``aiohttp_socks`` errors are not ``aiohttp.ClientError`` and so escape aiogram's own wrapping as raw ``ProxyError``; here they become
``TelegramNetworkError`` like every other network failure, so the handlers that catch ``TelegramAPIError`` see one kind of error.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiohttp_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.methods import TelegramMethod
    from aiogram.methods.base import TelegramType

log = logging.getLogger(__name__)

SAFE_READS = frozenset({"answerCallbackQuery", "sendChatAction"})  # idempotent besides the "get..." family
NEVER_REPEATED = frozenset({"getUpdates"})  # the polling loop has its own backoff


def _chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    while exc is not None and exc not in chain and len(chain) < 6:
        chain.append(exc)
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
    return chain


def never_left_the_machine(exc: BaseException) -> bool:
    """The failure happened while connecting (to the proxy or to Telegram): the request was not sent."""
    return any(isinstance(e, (ProxyError, ProxyConnectionError, ProxyTimeoutError, aiohttp.ClientConnectorError)) for e in _chain(exc))


def is_network_failure(exc: BaseException) -> bool:
    return isinstance(exc, (TelegramNetworkError, ProxyError, ProxyConnectionError, ProxyTimeoutError, aiohttp.ClientError, asyncio.TimeoutError, ConnectionError))


def is_read(api_method: str) -> bool:
    return api_method in SAFE_READS or (api_method.startswith("get") and api_method not in NEVER_REPEATED)


class RetryingSession(AiohttpSession):
    def __init__(self, *args: Any, retries: int = 2, pause: float = 1.0, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.retries = max(0, retries)
        self.pause = max(0.0, pause)

    async def make_request(self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None) -> TelegramType:
        api_method = method.__api_method__
        attempt = 0
        while True:
            try:
                return await super().make_request(bot, method, timeout)
            except Exception as exc:  # noqa: BLE001 - classified below; anything that is not a network failure goes up untouched
                if not is_network_failure(exc):
                    raise
                repeatable = api_method not in NEVER_REPEATED and (never_left_the_machine(exc) or is_read(api_method))
                if not repeatable or attempt >= self.retries:
                    if isinstance(exc, TelegramNetworkError):
                        raise
                    raise TelegramNetworkError(method=method, message=f"{type(exc).__name__}: {exc}") from exc
                attempt += 1
                log.warning("Bot API %s failed on the network (%s: %s); attempt %d of %d", api_method, type(exc).__name__, exc, attempt, self.retries)
                await asyncio.sleep(self.pause * attempt)

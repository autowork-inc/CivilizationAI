"""Housekeeping of the bot's own service messages: delete one now, or after a delay unless something cancels it.

Used for the memory card ("📝 Запомнил: …"): it vanishes when the user presses "✅ ОК", or by itself after ``SAVE_FACT_CARD_TTL_SECONDS`` unless
they pressed "Отменить" first. The delay is an in-process ``asyncio`` task (no broker, no database): a card whose timer died with the process
(a restart) simply stays, and its "ОК" button still removes it. Nothing here ever raises (except cancellation): a message that cannot be
deleted (already gone, too old) is only a log line.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)

Key = tuple[int, int]  # (chat id, message id)


class Janitor:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot
        self._pending: dict[Key, asyncio.Task[None]] = {}  # strong references: a task nobody holds may be collected before it runs

    def delete_later(self, chat_id: int, message_id: int, seconds: float) -> None:
        """Delete the message after ``seconds`` (<= 0 = never schedule). Scheduling twice for one message keeps the later timer only."""
        if seconds <= 0:
            return
        key = (chat_id, message_id)
        self.cancel(chat_id, message_id)
        self._pending[key] = asyncio.create_task(self._after(key, seconds))

    def cancel(self, chat_id: int, message_id: int) -> bool:
        """The timer of this message (if any) is dropped: the message stays. True = there was a timer."""
        task = self._pending.pop((chat_id, message_id), None)
        if task is None:
            return False
        task.cancel()
        return True

    async def delete_now(self, chat_id: int, message_id: int) -> bool:
        """Delete the message and drop its timer. False = Telegram refused (gone already, too old, no right): logged, not raised."""
        self.cancel(chat_id, message_id)
        return await self._delete(chat_id, message_id)

    async def aclose(self) -> None:
        """Shutdown: the timers are dropped (their messages stay; the buttons on them still work)."""
        tasks = list(self._pending.values())
        self._pending.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @property
    def pending(self) -> int:
        return len(self._pending)

    async def _after(self, key: Key, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            if self._pending.get(key) is asyncio.current_task():
                del self._pending[key]
            await self._delete(*key)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a background courtesy must not die loudly
            log.warning("could not auto-delete message %s of chat %s", key[1], key[0], exc_info=True)

    async def _delete(self, chat_id: int, message_id: int) -> bool:
        try:
            await self.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramAPIError:
            log.info("could not delete message %s of chat %s", message_id, chat_id, exc_info=True)
            return False
        return True

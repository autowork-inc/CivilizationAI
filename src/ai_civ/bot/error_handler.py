"""Crash reports: an unhandled error becomes ``crash_log_<time>.txt`` (the full traceback) in the admin's private chat with the bot.

Sources that call this module: the aiogram error handler and the per-message boundary of the Orchestrator, the Celery ``task_failure`` signal
(celery_app.py), the userbot provisioning task (which swallows its own errors to give the person the manual instructions) and the asyncio loop
handler of the bot process. Rules:

* Reporting must NEVER raise or stop the work that failed: every failure here is only logged.
* Secrets never leave the process: tokens, keys, the userbot session and passwords are cut out of the text (``redact``).
* A flood of the same error (a proxy outage fails every update) is one message per ``ERROR_REPORT_COOLDOWN_SECONDS`` per place, then a counter.
* The file lives in memory (``BufferedInputFile``): nothing is written to the container's disk.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import time
import traceback
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile

from ai_civ.config import Settings, get_settings

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1000  # Telegram: 1024 characters for a caption
_REDACTED = "[hidden]"
_SECRET_FIELDS = ("telegram_bot_token", "gemini_api_key", "telegram_api_hash", "telegram_userbot_session")
_PATTERNS = (
    re.compile(r"\bbot\d{6,}:[A-Za-z0-9_-]{20,}"),  # a Bot API URL carries the token: https://api.telegram.org/bot<token>/getMe
    re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}"),  # the bare token
    re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)"),  # user:password@host in a database / proxy / broker URL
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),  # a Google API key
    re.compile(r"\b1[A-Za-z0-9_=-]{340,}"),  # a Telethon StringSession
)

# signature -> (when it was last sent, how many identical reports were held back since)
_seen: dict[tuple[str, str, str], tuple[float, int]] = {}
_SEEN_LIMIT = 500
_background: set[asyncio.Task[Any]] = set()


def redact(text: str, settings: Settings | None = None) -> str:
    """Cut the secrets known to the settings and anything that looks like one out of ``text``."""
    if settings is not None:
        for name in _SECRET_FIELDS:
            secret = getattr(settings, name, None)
            value = secret.get_secret_value() if secret is not None and hasattr(secret, "get_secret_value") else None
            if value and len(value) >= 6:
                text = text.replace(value, _REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(_REDACTED, text)
    return text


def _signature(exc: BaseException, where: str) -> tuple[str, str, str]:
    tb = exc.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    place = f"{tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}" if tb is not None else ""
    return type(exc).__name__, where, place


def _throttle(signature: tuple[str, str, str], cooldown: float) -> int | None:
    """None = held back (counted). Otherwise how many identical reports were held back since the previous one."""
    now = time.monotonic()
    previous = _seen.get(signature)
    if previous is not None and now - previous[0] < cooldown:
        _seen[signature] = (previous[0], previous[1] + 1)
        return None
    if len(_seen) >= _SEEN_LIMIT:
        _seen.clear()
    _seen[signature] = (now, 0)
    return previous[1] if previous is not None else 0


def build_report(
    exc: BaseException, *, where: str, settings: Settings, held_back: int = 0, traceback_text: str | None = None, context: Iterable[str] = ()
) -> tuple[str, str, str]:
    """(file name, file text, caption). ``traceback_text`` is for callers that only have the text (a Celery ``ExceptionInfo``)."""
    now = datetime.now(UTC)
    name = f"crash_log_{now.strftime('%Y%m%d_%H%M%S')}.txt"
    kind = type(exc).__name__
    message = " ".join(str(exc).split())
    tb = traceback_text if traceback_text is not None else "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    head = [
        f"time:      {now.isoformat(timespec='seconds')}",
        f"where:     {where}",
        f"host:      {socket.gethostname()}",
        f"exception: {kind}",
        f"message:   {message}",
    ]
    if held_back:
        head.append(f"held back: {held_back} identical reports since the previous one")
    head.extend(context)
    body = redact("\n".join(head) + "\n\n" + tb, settings)
    limit = settings.error_report_max_bytes
    raw = body.encode("utf-8", "replace")
    if len(raw) > limit:  # the head and the tail matter most (what failed, and where it ends)
        half = limit // 2
        body = raw[:half].decode("utf-8", "ignore") + "\n\n[... cut ...]\n\n" + raw[-half:].decode("utf-8", "ignore")
    caption = f"🔥 {where}\n{kind}: {redact(message, settings)}"
    if held_back:
        caption += f"\n(+{held_back} identical held back)"
    if len(caption) > CAPTION_LIMIT:
        caption = caption[: CAPTION_LIMIT - 1] + "…"
    return name, body, caption


async def report_crash(
    exc: BaseException,
    *,
    where: str,
    settings: Settings | None = None,
    bot: Bot | None = None,
    traceback_text: str | None = None,
    context: Iterable[str] = (),
    force: bool = False,
) -> bool:
    """Send the crash log to the admin. True = it was sent. Never raises (``CancelledError`` excepted: it is not an error of ours)."""
    try:
        settings = settings or get_settings()
        if not settings.error_reports_enabled:
            return False
        held_back = 0 if force else _throttle(_signature(exc, where), settings.error_report_cooldown_seconds)
        if held_back is None:
            return False
        name, text, caption = build_report(exc, where=where, settings=settings, held_back=held_back, traceback_text=traceback_text, context=context)
        document = BufferedInputFile(text.encode("utf-8", "replace"), filename=name)
        if bot is not None:
            await bot.send_document(chat_id=settings.feedback_admin_telegram_id, document=document, caption=caption)
        else:
            from ai_civ.bot.app import open_bot  # late: app -> handlers -> this module

            async with open_bot(settings) as own:
                await own.send_document(chat_id=settings.feedback_admin_telegram_id, document=document, caption=caption)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - a report that cannot be delivered is only logged (Telegram may be the very thing that is down)
        log.exception("could not deliver the crash report (%s)", where)
        return False


def report_crash_soon(exc: BaseException, *, where: str, settings: Settings | None = None, bot: Bot | None = None) -> None:
    """Fire and forget from async code: the caller (a message under a topic lock) does not wait for Telegram."""
    try:
        task = asyncio.get_running_loop().create_task(report_crash(exc, where=where, settings=settings, bot=bot))
    except RuntimeError:  # no running loop: nothing to schedule on
        log.error("crash report of %s dropped: no running event loop", where, exc_info=exc)
        return
    _background.add(task)
    task.add_done_callback(_background.discard)


def report_crash_sync(
    exc: BaseException, *, where: str, settings: Settings | None = None, traceback_text: str | None = None, context: Iterable[str] = ()
) -> bool:
    """For synchronous code (the Celery ``task_failure`` signal): a private event loop, bounded by ``timeout``."""
    try:
        return asyncio.run(asyncio.wait_for(report_crash(exc, where=where, settings=settings, traceback_text=traceback_text, context=context), timeout=30))
    except Exception:  # noqa: BLE001
        log.exception("could not deliver the crash report (%s)", where)
        return False


def install_loop_handler(loop: asyncio.AbstractEventLoop, settings: Settings, bot: Bot) -> None:
    """Errors nobody awaited (a forgotten task, a callback) end up in the loop's handler: log them AND report them."""

    def handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        _loop.default_exception_handler(context)
        exc = context.get("exception")
        if isinstance(exc, Exception):
            report_crash_soon(exc, where="asyncio:" + str(context.get("message", "unhandled"))[:80], settings=settings, bot=bot)

    loop.set_exception_handler(handler)


def celery_failure(sender: Any, task_id: str | None, exception: BaseException, einfo: Any, args: Any = None, **_: Any) -> None:
    """``task_failure`` signal handler (connected in celery_app.py): fires once, when the task has finally failed (not on every retry)."""
    name = getattr(sender, "name", None) or "?"
    text = getattr(einfo, "traceback", None)
    report_crash_sync(
        exception,
        where=f"celery:{name}",
        traceback_text=str(text) if text else None,
        context=[f"task_id:   {task_id}", f"args:      {str(args)[:300]}"],
    )


__all__ = [
    "build_report",
    "celery_failure",
    "install_loop_handler",
    "redact",
    "report_crash",
    "report_crash_soon",
    "report_crash_sync",
]

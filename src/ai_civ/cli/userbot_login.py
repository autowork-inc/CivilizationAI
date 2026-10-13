"""One-time, interactive: log the userbot account in and print its ``TELEGRAM_USERBOT_SESSION`` string.

    python -m ai_civ.cli.userbot_login

Needs TELEGRAM_API_ID and TELEGRAM_API_HASH (my.telegram.org -> API development tools) in the environment / .env. It asks for the phone number of the
account that will act as the userbot, then for the code Telegram sends (and the 2FA password if the account has one). The printed string is a
full login of that account: put it ONLY in the server's .env, never in git, never in a chat. Use a dedicated account, not a personal one: it will
create a group for every new user and Telegram may limit an account that does this in bulk.
"""

from __future__ import annotations

import asyncio
import sys

from ai_civ.config import ConfigError, get_settings


async def login() -> str:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    settings = get_settings()
    if not settings.telegram_api_id or settings.telegram_api_hash is None:
        raise ConfigError("set TELEGRAM_API_ID and TELEGRAM_API_HASH first (my.telegram.org -> API development tools)")
    from ai_civ.bot.userbot import telethon_proxy

    client = TelegramClient(
        StringSession(), settings.telegram_api_id, settings.telegram_api_hash.get_secret_value(), proxy=telethon_proxy(settings.telegram_proxy)
    )
    async with client:  # prompts for the phone, the code and the 2FA password on the terminal
        me = await client.get_me()
        print(f"logged in as {me.first_name} (id {me.id}); this account is the userbot", file=sys.stderr)
        return client.session.save()


def main() -> int:
    try:
        session = asyncio.run(login())
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("\nTELEGRAM_USERBOT_SESSION=" + session)
    return 0


if __name__ == "__main__":
    sys.exit(main())

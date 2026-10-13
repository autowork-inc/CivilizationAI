"""One-off: log a Telegram account in and print a ready ``TELEGRAM_USERBOT_SESSION=...`` line for .env.

    .venv\\Scripts\\python.exe scripts\\get_userbot_session.py            # direct connection (default)
    .venv\\Scripts\\python.exe scripts\\get_userbot_session.py --proxy socks5://127.0.0.1:1080

Standalone on purpose: it does NOT read TELEGRAM_PROXY from .env (that value is a docker hostname and does not resolve on the host) and does not import the
project. It reads only TELEGRAM_API_ID / TELEGRAM_API_HASH from .env. Nothing is written to disk; the line is printed and copied to the clipboard.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def proxy_tuple(url: str | None) -> tuple | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme.startswith("socks5"):
        return ("socks5", parsed.hostname, parsed.port, True)
    if parsed.scheme in ("http", "https"):
        return ("http", parsed.hostname, parsed.port)
    raise SystemExit(f"unsupported proxy: {url}")


async def run(api_id: int, api_hash: str, proxy: tuple | None) -> str:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(StringSession(), api_id, api_hash, proxy=proxy, connection_retries=2, timeout=15)
    async with client:  # asks for phone, code and the 2FA password in this terminal
        me = await client.get_me()
        print(f"\nlogged in as {me.first_name} (id {me.id})", file=sys.stderr)
        return client.session.save()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env")
    parser.add_argument("--proxy", default=None, help="optional, e.g. socks5://127.0.0.1:1080")
    args = parser.parse_args()

    env = read_env(Path(args.env))
    try:
        api_id, api_hash = int(env["TELEGRAM_API_ID"]), env["TELEGRAM_API_HASH"]
    except (KeyError, ValueError):
        print("TELEGRAM_API_ID / TELEGRAM_API_HASH are missing or invalid in .env", file=sys.stderr)
        return 1

    session = asyncio.run(run(api_id, api_hash, proxy_tuple(args.proxy)))
    line = "TELEGRAM_USERBOT_SESSION=" + session
    print("\n" + line + "\n")
    try:
        subprocess.run(["clip"], input=line.encode("ascii"), check=True)
        print("(the line is copied to the clipboard)", file=sys.stderr)
    except (OSError, subprocess.CalledProcessError):
        print("(could not copy to the clipboard: copy the line above)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

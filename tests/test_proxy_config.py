"""GEMINI_PROXY / TELEGRAM_PROXY: validated, empty = direct, and really handed to the two HTTP stacks."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from ai_civ.bot.app import build_bot
from ai_civ.config import Settings
from ai_civ.memory.llm import GeminiClient


def settings(**kw) -> Settings:
    return Settings(_env_file=None, gemini_api_key=SecretStr("k"), telegram_bot_token=SecretStr("123:abc"), **kw)


def test_empty_means_direct_and_a_good_url_is_kept():
    assert settings().gemini_proxy is None and settings(gemini_proxy="", telegram_proxy="  ").gemini_proxy is None
    assert settings(gemini_proxy="socks5h://proxy:1080").gemini_proxy == "socks5h://proxy:1080"


@pytest.mark.parametrize("bad", ["proxy:1080", "ftp://proxy:1080", "socks5://proxy", "socks5://:1080", "socks5://pro xy:1080"])
def test_a_malformed_proxy_fails_at_startup(bad):
    with pytest.raises(ValidationError):
        settings(gemini_proxy=bad)


async def test_the_proxy_reaches_both_http_stacks():
    client = GeminiClient(settings(gemini_proxy="socks5h://proxy:1080"))
    try:
        assert client._client._api_client._async_httpx_client._mounts  # a proxy transport is mounted
    finally:
        await client.aclose()
    direct = GeminiClient(settings())
    try:
        assert not direct._client._api_client._async_httpx_client._mounts
    finally:
        await direct.aclose()
    bot = build_bot(settings(telegram_proxy="socks5h://proxy:1080"))
    try:
        assert bot.session.proxy == "socks5://proxy:1080"  # aiohttp-socks has no "socks5h"
    finally:
        await bot.session.close()
    plain = build_bot(settings())
    try:
        assert plain.session.proxy is None
    finally:
        await plain.session.close()

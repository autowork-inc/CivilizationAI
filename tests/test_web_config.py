"""Mini App settings: the Direct Link is built from the real bot / app names and validated at start-up (no database needed)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_civ.config import Settings


def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_the_defaults_are_the_registered_mini_app():
    s = make()
    assert (s.telegram_bot_username, s.mini_app_short_name) == ("PerfectPerson_bot", "Overview")
    assert s.mini_app_direct_link == "https://t.me/PerfectPerson_bot/Overview"
    assert (s.web_host, s.web_port) == ("127.0.0.1", 8080)  # loopback only: the tunnel is what faces the internet


def test_a_leading_at_sign_and_spaces_are_tolerated():
    assert make(telegram_bot_username=" @Other_bot ", mini_app_short_name=" App1 ").mini_app_direct_link == "https://t.me/Other_bot/App1"


@pytest.mark.parametrize("username", ["", "bot", "has space_bot", "1startsWithDigit_bot", "x" * 40, "ключ_bot"])
def test_a_malformed_bot_username_fails_at_start_up(username):
    with pytest.raises(ValidationError):
        make(telegram_bot_username=username)


@pytest.mark.parametrize("short", ["", "ab", "has space", "кириллица", "a/b"])
def test_a_malformed_short_name_fails_at_start_up(short):
    with pytest.raises(ValidationError):
        make(mini_app_short_name=short)


def test_env_example_documents_the_new_settings_with_the_real_names():
    from pathlib import Path

    text = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    for line in ("TELEGRAM_BOT_USERNAME=PerfectPerson_bot", "MINI_APP_SHORT_NAME=Overview", "WEB_PORT=8080", "INIT_DATA_MAX_AGE_SECONDS=86400"):
        assert line in text

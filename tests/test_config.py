"""Configuration handling (no database needed)."""

from __future__ import annotations

import pytest
from sqlalchemy.pool import NullPool

from ai_civ.config import ConfigError, Settings, normalize_async_url
from ai_civ.db.tenancy import engine_options


def test_plain_postgres_urls_are_rewritten_for_asyncpg():
    for plain in ("postgresql://u:p@h:5432/db", "postgres://u:p@h:5432/db"):
        assert normalize_async_url(plain, name="X") == "postgresql+asyncpg://u:p@h:5432/db"


def test_sslmode_is_rewritten_to_ssl():
    url = normalize_async_url("postgresql://u:p@h/db?sslmode=require", name="X")
    assert url == "postgresql+asyncpg://u:p@h/db?ssl=require"


def test_password_with_special_characters_survives():
    url = normalize_async_url("postgresql+asyncpg://u:p%40ss%2Fword@h/db", name="X")
    assert "p%40ss%2Fword" in url


@pytest.mark.parametrize("bad", [None, "", "mysql://u:p@h/db", "postgresql+psycopg://u:p@h/db"])
def test_bad_urls_fail_fast(bad):
    with pytest.raises(ConfigError):
        normalize_async_url(bad, name="DATABASE_URL")


def test_transaction_pooler_disables_prepared_statements():
    options = engine_options(Settings(db_pooler_mode="transaction"))
    assert options["poolclass"] is NullPool
    args = options["connect_args"]
    assert args["statement_cache_size"] == 0 and args["prepared_statement_cache_size"] == 0
    assert args["prepared_statement_name_func"]() != args["prepared_statement_name_func"]()  # unique names


@pytest.mark.parametrize("mode", ["direct", "session"])
def test_regular_modes_use_a_connection_pool(mode):
    options = engine_options(Settings(db_pooler_mode=mode, db_pool_size=7, db_max_overflow=3))
    assert options["pool_size"] == 7 and options["max_overflow"] == 3 and "poolclass" not in options


def test_voice_defaults_allow_a_fifteen_minute_note_with_its_own_deadline():
    cfg = Settings(_env_file=None)
    assert cfg.voice_max_seconds == 900 and cfg.voice_max_bytes == 25 * 1024 * 1024 and cfg.voice_transcript_max_chars == 25_000
    assert cfg.voice_llm_timeout_seconds == 180.0


def test_voice_limits_can_be_set_from_the_environment(monkeypatch):
    monkeypatch.setenv("VOICE_MAX_SECONDS", "300")
    monkeypatch.setenv("VOICE_LLM_TIMEOUT_SECONDS", "240")
    cfg = Settings(_env_file=None)
    assert cfg.voice_max_seconds == 300 and cfg.voice_llm_timeout_seconds == 240.0

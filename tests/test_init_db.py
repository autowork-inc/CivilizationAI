"""``ai_civ.cli.init_db``: which credentials are accepted, and enabling the runtime role on a real PostgreSQL."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ai_civ.cli import init_db
from ai_civ.config import ConfigError
from tests.helpers import APP_PASSWORD, APP_URL


def test_the_runtime_role_and_its_real_password_are_taken_from_database_url():
    assert init_db.app_credentials("postgresql+asyncpg://ai_civ_app:s3cret@postgres:5432/civ_db") == ("ai_civ_app", "s3cret")
    assert init_db.app_credentials("postgresql+asyncpg://ai_civ_app.abcdef:s3cret@pooler:5432/postgres")[0] == "ai_civ_app"  # Supabase pooler form


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://postgres:s3cret@postgres:5432/civ_db",  # the owner is never the runtime role
        "postgresql+asyncpg://ai_civ_app:CHANGE_ME@postgres:5432/civ_db",
        "postgresql+asyncpg://ai_civ_app@postgres:5432/civ_db",
        "postgresql+asyncpg://ai_civ_app_evil:s3cret@postgres:5432/civ_db",
    ],
)
def test_placeholders_empty_passwords_and_a_wrong_role_are_refused(url):
    with pytest.raises(ConfigError):
        init_db.app_credentials(url)


async def test_the_login_password_is_set_exactly_as_given_even_with_quotes(owner_engine, db):
    nasty = "p'a\"s$$w;--" + "\\" + "d"
    await init_db.enable_app_login(owner_engine, "ai_civ_app", nasty)
    url = APP_URL.replace(f":{APP_PASSWORD}@", ":p%27a%22s%24%24w%3B--%5Cd@")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT current_user"))).scalar_one() == "ai_civ_app"
    finally:
        await engine.dispose()
        await init_db.enable_app_login(owner_engine, "ai_civ_app", APP_PASSWORD)  # the other tests of the session log in with this one


async def test_a_missing_role_is_an_error_not_a_silent_success(owner_engine):
    with pytest.raises(RuntimeError, match="does not exist"):
        await init_db.enable_app_login(owner_engine, "ai_civ_nobody", "x")


async def test_the_runtime_check_passes_for_the_app_role_and_fails_for_the_owner(owner_engine, db):
    await init_db.verify_runtime_login(APP_URL)
    from tests.helpers import OWNER_URL
    from ai_civ.db import RlsNotEnforcedError

    with pytest.raises(RlsNotEnforcedError):
        await init_db.verify_runtime_login(OWNER_URL)

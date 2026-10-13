"""The Mini App backend end to end: REAL PostgreSQL (RLS as ai_civ_app), the real FastAPI app over ASGI, signed launch data made
the way Telegram makes it. Nothing here calls Telegram or Gemini."""

from __future__ import annotations

import random
import uuid

import httpx
import pytest
import pytest_asyncio

from ai_civ.config import ConfigError, Settings
from ai_civ.db.tenancy import RlsNotEnforcedError
from ai_civ.web.app import create_app
from tests.helpers import APP_URL, OWNER_URL, new_fact, new_user
from tests.test_web_auth import TOKEN, make_init_data


def settings(**kw) -> Settings:
    return Settings(_env_file=None, telegram_bot_token=TOKEN, **kw)


@pytest_asyncio.fixture
async def client(db, tmp_path):
    app = create_app(settings(web_dist_dir=str(tmp_path / "no-build")), db=db)  # no build there: "/" is the smoke page
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://web.test") as http:
        yield http


async def test_a_build_is_served_at_the_root_with_cacheable_assets_and_uncacheable_everything_else(db, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>built app</title>", encoding="utf-8")
    (dist / "assets" / "app-123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not public", encoding="utf-8")
    app = create_app(settings(web_dist_dir=str(dist)), db=db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://web.test") as http:
        page = await http.get("/")
        asset = await http.get("/assets/app-123.js")
        api = await http.get("/api/me")
        escape = await http.get("/assets/../../secret.txt")

    assert page.status_code == 200 and "built app" in page.text and page.headers["cache-control"] == "no-store"
    assert asset.status_code == 200 and asset.headers.get("cache-control") != "no-store"  # hashed file names: cacheable
    assert api.headers["cache-control"] == "no-store"
    assert "not public" not in escape.text  # nothing outside the build directory is served


def tma(raw: str) -> dict[str, str]:
    return {"Authorization": f"tma {raw}"}


async def tenant(owner_engine, *, name: str | None = "Анна", tz: str = "Europe/Copenhagen", state: str = "done") -> int:
    """A registered user (returns the telegram id) with an optional ``name`` Core fact."""
    telegram_id = random.randrange(10**9, 10**12)
    user_id = await new_user(owner_engine, tz=tz, telegram_user_id=telegram_id, onboarding_state=state)
    if name is not None:
        await new_fact(owner_engine, user_id, "name", category="identity", value=name, priority=90)
    return telegram_id


# ---------------------------------------------------------------- start-up is fail-fast


def test_the_backend_refuses_to_start_without_the_bot_token():
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        create_app(Settings(_env_file=None))


async def test_the_backend_refuses_to_serve_as_a_role_that_bypasses_rls(migrated):
    app = create_app(settings(database_url=OWNER_URL))
    with pytest.raises(RlsNotEnforcedError):
        async with app.router.lifespan_context(app):
            pass


async def test_the_backend_starts_as_the_runtime_role(migrated):
    app = create_app(settings(database_url=APP_URL))
    async with app.router.lifespan_context(app):
        assert app.state.db is not None


# ---------------------------------------------------------------- authentication


async def test_health_and_the_smoke_page_need_no_signature_and_are_never_cached(client):
    health = await client.get("/healthz")
    assert health.status_code == 200 and health.json() == {"status": "ok"}
    page = await client.get("/")
    assert page.status_code == 200 and "telegram-web-app.js" in page.text and "tma " in page.text
    assert health.headers["cache-control"] == "no-store" and page.headers["cache-control"] == "no-store"


async def test_no_header_or_the_wrong_scheme_is_401_and_says_how_to_authenticate(client):
    for headers in ({}, {"Authorization": "Bearer abc"}, {"Authorization": "tma "}):
        response = await client.get("/api/me", headers=headers)
        assert response.status_code == 401 and response.headers["www-authenticate"] == "tma"
        assert response.json()["error"]["code"] == "init_data_missing"


async def test_a_forged_signature_is_401_and_the_details_are_not_echoed(client, owner_engine):
    telegram_id = await tenant(owner_engine)
    raw = make_init_data(user_id=telegram_id, token="987654321:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB")
    response = await client.get("/api/me", headers=tma(raw))
    assert response.status_code == 401 and response.json()["error"]["code"] == "init_data_bad_signature"
    assert "hash" not in response.text and str(telegram_id) not in response.text


async def test_expired_launch_data_is_401_with_a_human_message(client, owner_engine):
    telegram_id = await tenant(owner_engine)
    response = await client.get("/api/me", headers=tma(make_init_data(user_id=telegram_id, age=2 * 86_400)))
    assert response.status_code == 401
    assert response.json()["error"] == {"code": "init_data_expired", "message": "Сессия устарела. Закройте приложение и откройте его снова."}


async def test_a_valid_signature_of_someone_who_is_not_registered_is_403(client):
    response = await client.get("/api/me", headers=tma(make_init_data(user_id=random.randrange(10**9, 10**12))))
    assert response.status_code == 403 and response.json()["error"]["code"] == "not_registered"


async def test_someone_who_has_not_finished_onboarding_is_403(client, owner_engine):
    telegram_id = await tenant(owner_engine, state="in_progress")
    response = await client.get("/api/me", headers=tma(make_init_data(user_id=telegram_id)))
    assert response.status_code == 403 and response.json()["error"]["code"] == "onboarding_required"


# ---------------------------------------------------------------- the tenant comes from the signature, and only from it


async def test_me_returns_the_callers_profile_read_through_a_tenant_session(client, owner_engine):
    telegram_id = await tenant(owner_engine, name="Анна", tz="Asia/Almaty")
    response = await client.get("/api/me", headers=tma(make_init_data(user_id=telegram_id, extra={"start_param": "x"})))
    assert response.status_code == 200
    assert response.json() == {
        "first_name": "Аня",  # Telegram's, signed
        "name": "Анна",  # the Core fact
        "address_as": None,
        "timezone": "Asia/Almaty",
        "launch": {"chat_type": "supergroup", "start_param": "x"},
    }
    assert response.headers["cache-control"] == "no-store"


async def test_two_tenants_never_see_each_other(client, owner_engine):
    alice, bob = await tenant(owner_engine, name="Алиса", tz="Europe/Moscow"), await tenant(owner_engine, name="Боб", tz="Asia/Tokyo")
    a = (await client.get("/api/me", headers=tma(make_init_data(user_id=alice)))).json()
    b = (await client.get("/api/me", headers=tma(make_init_data(user_id=bob)))).json()
    assert (a["name"], a["timezone"]) == ("Алиса", "Europe/Moscow")
    assert (b["name"], b["timezone"]) == ("Боб", "Asia/Tokyo")


async def test_a_user_id_sent_in_the_open_is_ignored(client, owner_engine):
    alice, bob = await tenant(owner_engine, name="Алиса"), await tenant(owner_engine, name="Боб")
    response = await client.get(
        "/api/me",
        params={"user_id": bob, "telegram_user_id": bob, "tenant_id": str(uuid.uuid4())},
        headers={**tma(make_init_data(user_id=alice)), "X-Telegram-User-Id": str(bob), "X-User-Id": str(bob)},
    )
    assert response.json()["name"] == "Алиса"  # whoever the signature names, nobody else


async def test_a_historical_version_of_a_fact_is_not_what_me_reports(client, owner_engine):
    from tests.helpers import owner_exec

    telegram_id = random.randrange(10**9, 10**12)
    user_id = await new_user(owner_engine, telegram_user_id=telegram_id)
    old = await new_fact(owner_engine, user_id, "name", category="identity", value="Старое", priority=90)
    await owner_exec(owner_engine, "UPDATE civ.core_facts SET valid_to = now() WHERE id = :i", i=old)
    await new_fact(owner_engine, user_id, "name", category="identity", value="Новое", priority=90)
    response = await client.get("/api/me", headers=tma(make_init_data(user_id=telegram_id)))
    assert response.json()["name"] == "Новое"


async def test_the_api_does_not_advertise_its_shape(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert (await client.get(path)).status_code == 404

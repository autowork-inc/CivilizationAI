"""PR 2.1: character profile + XP ledger (migration 0016), the /api/character routes and the XP hooks of task steps and cards.
Real PostgreSQL (RLS, triggers); the real FastAPI app over ASGI. Telegram and Gemini are not involved (Tasks / Cards are driven by their
button methods; they do not use the bot there)."""

from __future__ import annotations

import asyncio
import io
import uuid

import pytest
from PIL import Image
from sqlalchemy.exc import DBAPIError

from ai_civ.bot import xp
from ai_civ.bot.tools.cards import Cards
from ai_civ.bot.tools.decompose import Tasks
from ai_civ.config import Settings
from ai_civ.web import character
from tests.helpers import new_user, owner_exec, tenant_exec
from tests.web_helpers import alice, bob, client  # noqa: F401

OWNER_INSERT = "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref{extra}) VALUES (:u, :a, :c, :r{extra_v})"


async def ledger(actor_or_engine, user=None):
    if user is None:
        return await actor_or_engine.sql("SELECT amount, category, source_ref FROM civ.xp_ledger WHERE user_id = :u ORDER BY created_at, source_ref")
    return await owner_exec(actor_or_engine, "SELECT amount, category, source_ref FROM civ.xp_ledger WHERE user_id = :u ORDER BY created_at, source_ref", u=user)


async def grant(actor, n: int, *, days_ago: int = 0, category: str = "focus", prefix: str = "seed") -> None:
    """Seed ``n`` awards of 10 XP at ``days_ago`` days back (as the owner; the cap trigger still applies to owner inserts)."""
    for i in range(n):
        await actor.sql(
            "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref, created_at) "
            "VALUES (:u, 10, :c, :r, now() - make_interval(days => :d, mins => :m))",
            c=category, r=f"{prefix}:{days_ago}:{i}", d=days_ago, m=i,
        )  # fmt: skip


def png(size=(64, 48), color=(200, 30, 30), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


# ================================================================ database: ledger rules


async def test_an_award_is_paid_once_per_source_and_the_progress_view_counts_it(alice, db):
    assert (await alice.sql("SELECT total_xp, level, current_level_xp FROM civ.character_progress WHERE user_id = :u"))[0] == (0, 1, 0)

    await tenant_exec(db, alice.user_id, "SELECT civ.award_xp('focus', 'task:1')")
    assert await tenant_exec(db, alice.user_id, "SELECT civ.award_xp('focus', 'task:1')") == [("duplicate",)]
    assert await tenant_exec(db, alice.user_id, "SELECT civ.award_xp('body', 'gym:1', 20)") == [("awarded",)]

    row = (await alice.sql("SELECT total_xp, level, current_level_xp, xp_by_category::text, xp_today FROM civ.character_progress WHERE user_id = :u"))[0]
    assert row[:3] == (30, 1, 30) and row[4] == 30
    assert '"focus": 10' in row[3] and '"body": 20' in row[3] and '"intellect": 0' in row[3] and '"connection": 0' in row[3]


async def test_the_level_is_linear_100_xp_each(alice):
    for day in range(3):  # 3 days x 100 XP = 300 XP, +30 today
        await grant(alice, 10, days_ago=day + 1)
    await grant(alice, 3, days_ago=0)
    row = (await alice.sql("SELECT total_xp, level, current_level_xp FROM civ.character_progress WHERE user_id = :u"))[0]
    assert row == (330, 4, 30)


async def test_the_daily_cap_is_100_xp_for_the_whole_day_whoever_inserts(alice):
    await grant(alice, 10)  # exactly 100 today: fine
    with pytest.raises(DBAPIError, match="daily XP cap"):
        await alice.sql(OWNER_INSERT.format(extra="", extra_v=""), a=1, c="body", r="one-more")
    await grant(alice, 10, days_ago=1)  # yesterday has its own allowance
    assert (await alice.sql("SELECT xp_today FROM civ.character_progress WHERE user_id = :u"))[0][0] == 100


async def test_the_cap_follows_the_tenants_local_day(owner_engine):
    kiribati = await new_user(owner_engine, tz="Pacific/Kiritimati")  # UTC+14: the local date is often not the UTC date
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref, created_at) "
        "SELECT :u, 10, 'focus', 'd:' || g, (((now() AT TIME ZONE 'Pacific/Kiritimati')::date - 1)::timestamp + make_interval(hours => 12, mins => g)) AT TIME ZONE 'Pacific/Kiritimati' "
        "FROM generate_series(1, 10) g",
        u=kiribati,
    )  # 100 XP at noon of the local YESTERDAY: today's allowance is untouched, whatever the UTC date is
    await owner_exec(owner_engine, "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 10, 'focus', 'today')", u=kiribati)
    assert (await owner_exec(owner_engine, "SELECT xp_today, total_xp FROM civ.character_progress WHERE user_id = :u", u=kiribati)) == [(10, 110)]


async def test_the_daily_cap_holds_under_parallel_awards(alice, db):
    results = await asyncio.gather(*(tenant_exec(db, alice.user_id, "SELECT civ.award_xp('focus', :r)", r=f"par:{i}") for i in range(15)))
    codes = sorted(r[0][0] for r in results)
    assert codes == ["awarded"] * 10 + ["daily_cap"] * 5
    assert (await alice.sql("SELECT sum(amount) FROM civ.xp_ledger WHERE user_id = :u"))[0][0] == 100


async def test_the_same_source_awarded_in_parallel_pays_once(alice, db):
    results = await asyncio.gather(*(tenant_exec(db, alice.user_id, "SELECT civ.award_xp('focus', 'task:same')") for _ in range(8)))
    assert sorted(r[0][0] for r in results) == ["awarded"] + ["duplicate"] * 7


@pytest.mark.parametrize(
    "category, amount, ref",
    [("magic", 10, "x"), ("focus", 0, "x"), ("focus", 101, "x"), ("focus", -5, "x"), ("focus", 10, ""), ("focus", 10, "   "), ("focus", 10, "a\nb"), ("focus", 10, "я" * 201)],
)
async def test_nonsense_awards_are_refused_by_the_function_and_by_the_table(alice, db, category, amount, ref):
    with pytest.raises(DBAPIError):
        await tenant_exec(db, alice.user_id, "SELECT civ.award_xp(:c, :r, :a)", c=category, r=ref, a=amount)
    with pytest.raises(DBAPIError):
        await alice.sql(OWNER_INSERT.format(extra="", extra_v=""), a=amount, c=category, r=ref)
    assert await ledger(alice) == []


async def test_the_application_can_only_append_to_the_ledger(alice, db):
    await tenant_exec(db, alice.user_id, "SELECT civ.award_xp('focus', 'task:1')")
    for statement in ("UPDATE civ.xp_ledger SET amount = 1", "DELETE FROM civ.xp_ledger"):
        with pytest.raises(DBAPIError, match="permission denied"):
            await tenant_exec(db, alice.user_id, statement)
    assert len(await ledger(alice)) == 1


async def test_xp_and_profiles_are_invisible_to_other_tenants_and_need_a_tenant(alice, bob, db):
    await grant(alice, 2)
    await alice.sql("INSERT INTO civ.character_profiles (user_id, character_name) VALUES (:u, 'Альфа')")
    assert await tenant_exec(db, bob.user_id, "SELECT count(*) FROM civ.xp_ledger") == [(0,)]
    assert await tenant_exec(db, bob.user_id, "SELECT count(*) FROM civ.character_profiles") == [(0,)]
    assert await tenant_exec(db, bob.user_id, "SELECT total_xp FROM civ.character_progress") == [(0,)]  # only bob's own row
    with pytest.raises(DBAPIError):  # a tenant cannot write a row for somebody else
        await tenant_exec(db, bob.user_id, "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 10, 'focus', 'steal')", u=alice.user_id)
    with pytest.raises(DBAPIError):
        await tenant_exec(db, bob.user_id, "INSERT INTO civ.character_profiles (user_id) VALUES (:u)", u=alice.user_id)


@pytest.mark.parametrize(
    "values",
    [{"n": ""}, {"n": "   "}, {"n": "я" * 41}, {"n": "двa\nряда"}, {"n": "ab‮cd"}],
)
async def test_a_profile_name_must_be_a_clean_line(alice, values):
    with pytest.raises(DBAPIError):
        await alice.sql("INSERT INTO civ.character_profiles (user_id, character_name) VALUES (:u, :n)", **values)


async def test_the_avatar_column_refuses_more_than_256_kib(alice):
    with pytest.raises(DBAPIError):
        await alice.sql("INSERT INTO civ.character_profiles (user_id, avatar_bytes) VALUES (:u, decode(repeat('00', 262145), 'hex'))")
    await alice.sql("INSERT INTO civ.character_profiles (user_id, avatar_bytes) VALUES (:u, decode(repeat('00', 262144), 'hex'))")


def test_the_api_constants_mirror_the_database_rules():
    assert character.LEVEL_XP == 100 and character.DAILY_CAP == 100 and character.AVATAR_MAX_BYTES == 262144


# ================================================================ API


async def test_it_needs_valid_launch_data(client):
    for call in (client.get("/api/character/me"), client.patch("/api/character/settings", json={"show_mood": True}), client.post("/api/character/avatar", content=png())):
        assert (await call).status_code == 401
    forged = {"Authorization": "tma auth_date=1&hash=00"}
    assert (await client.get("/api/character/me", headers=forged)).status_code == 401


async def test_a_new_character_shows_the_defaults(alice):
    body = (await alice.get("/api/character/me")).json()
    assert body == {
        "character_name": "Аня", "name_is_default": True, "has_avatar": False,
        "level": 1, "total_xp": 0, "current_level_xp": 0, "xp_to_next_level": 100,
        "xp_by_category": {"focus": 0, "intellect": 0, "body": 0, "connection": 0}, "xp_today": 0, "daily_cap": 100,
        "visibility": {"show_level": True, "show_mood": False, "show_interests": False, "open_for_intros": False},
        "penalties": {
            "streak_days": 0, "penalty_today": 0, "penalty_week": 0, "penalty_total": 0, "recent": [],
            "body_idle_days": None, "body_threshold_days": 5, "body_decay_active": False,
        },
    }  # fmt: skip


async def test_progress_levels_and_branches_come_from_the_ledger(alice):
    await grant(alice, 10, days_ago=2, category="focus")
    await grant(alice, 4, days_ago=1, category="intellect")
    await grant(alice, 2, days_ago=0, category="connection")
    body = (await alice.get("/api/character/me")).json()
    assert (body["total_xp"], body["level"], body["current_level_xp"], body["xp_to_next_level"], body["xp_today"]) == (160, 2, 60, 40, 20)
    assert body["xp_by_category"] == {"focus": 100, "intellect": 40, "body": 0, "connection": 20}


async def test_settings_change_only_what_was_sent(alice):
    body = (await alice.patch("/api/character/settings", {"character_name": "  Ася   Ветрова ", "show_mood": True})).json()
    assert body["character_name"] == "Ася Ветрова" and body["name_is_default"] is False
    assert body["visibility"] == {"show_level": True, "show_mood": True, "show_interests": False, "open_for_intros": False}

    body = (await alice.patch("/api/character/settings", {"open_for_intros": True, "show_level": False})).json()
    assert body["character_name"] == "Ася Ветрова"  # untouched
    assert body["visibility"] == {"show_level": False, "show_mood": True, "show_interests": False, "open_for_intros": True}

    body = (await alice.patch("/api/character/settings", {"character_name": None})).json()  # back to the Telegram name
    assert body["character_name"] == "Аня" and body["name_is_default"] is True
    assert len(await alice.sql("SELECT 1 FROM civ.character_profiles WHERE user_id = :u")) == 1


@pytest.mark.parametrize(
    "payload",
    [{}, {"character_name": ""}, {"character_name": "   "}, {"character_name": "я" * 41}, {"character_name": "a‮b"}, {"character_name": 5},
     {"show_level": None}, {"show_mood": "maybe"}, {"user_id": str(uuid.uuid4())}, {"total_xp": 9999}, {"level": 99}],
)  # fmt: skip
async def test_bad_settings_are_refused_and_change_nothing(alice, payload):
    r = await alice.patch("/api/character/settings", payload)
    assert r.status_code == 422 and r.json()["error"]["code"]
    assert await alice.sql("SELECT 1 FROM civ.character_profiles WHERE user_id = :u") == []


async def test_nothing_in_the_api_can_award_xp(alice):
    for path in ("/api/character/me", "/api/character/settings", "/api/character/avatar"):
        for verb in ("post", "put", "delete"):
            r = await getattr(alice, verb)(path)
            assert r.status_code in (404, 405, 415, 422)
    await alice.patch("/api/character/settings", {"show_mood": True})
    assert await ledger(alice) == []


async def test_two_users_have_separate_characters(alice, bob):
    await alice.patch("/api/character/settings", {"character_name": "Альфа", "open_for_intros": True})
    await grant(alice, 5)
    me_bob = (await bob.get("/api/character/me")).json()
    assert me_bob["character_name"] == "Аня" and me_bob["total_xp"] == 0 and me_bob["visibility"]["open_for_intros"] is False
    await bob.patch("/api/character/settings", {"character_name": "Бета"})
    me_alice = (await alice.get("/api/character/me")).json()
    assert me_alice["character_name"] == "Альфа" and me_alice["total_xp"] == 50


# ---------------------------------------------------------------- avatar


async def test_an_avatar_is_stored_as_a_small_webp(alice):
    r = await alice.http.post("/api/character/avatar", content=png((900, 600)), headers={**alice.headers, "Content-Type": "image/png"})
    assert r.status_code == 200 and r.json()["has_avatar"] is True
    stored = (await alice.sql("SELECT avatar_bytes FROM civ.character_profiles WHERE user_id = :u"))[0][0]
    image = Image.open(io.BytesIO(bytes(stored)))
    assert image.format == "WEBP" and max(image.size) == 256 and image.size == (256, 171) and len(stored) <= 262144

    got = await alice.get("/api/character/avatar")
    assert got.status_code == 200 and got.headers["content-type"] == "image/webp" and got.content == bytes(stored)
    assert got.headers["cache-control"] == "no-store"


async def test_an_avatar_keeps_the_other_profile_fields(alice):
    await alice.patch("/api/character/settings", {"character_name": "Ася", "show_mood": True})
    await alice.http.post("/api/character/avatar", content=png(), headers={**alice.headers, "Content-Type": "image/png"})
    body = (await alice.get("/api/character/me")).json()
    assert body["character_name"] == "Ася" and body["visibility"]["show_mood"] is True and body["has_avatar"] is True


async def test_a_small_picture_is_not_enlarged_and_transparency_survives(alice):
    r = await alice.http.post(
        "/api/character/avatar", content=png((40, 40), (0, 0, 0, 0), mode="RGBA"), headers={**alice.headers, "Content-Type": "image/png"}
    )
    assert r.status_code == 200
    stored = (await alice.sql("SELECT avatar_bytes FROM civ.character_profiles WHERE user_id = :u"))[0][0]
    image = Image.open(io.BytesIO(bytes(stored)))
    assert image.size == (40, 40) and image.convert("RGBA").getpixel((5, 5))[3] == 0


async def test_metadata_of_the_original_is_not_kept(alice):
    exif = Image.Exif()
    exif[0x010F] = "SecretCamera"  # Make
    exif[0x8825] = {1: "N", 2: (55.0, 37.0, 0.0)}  # GPS block is not required for this proof; the Make tag is enough
    buf = io.BytesIO()
    Image.new("RGB", (80, 80), (1, 2, 3)).save(buf, format="JPEG", exif=exif)
    assert b"SecretCamera" in buf.getvalue()
    r = await alice.http.post("/api/character/avatar", content=buf.getvalue(), headers={**alice.headers, "Content-Type": "image/jpeg"})
    assert r.status_code == 200
    stored = bytes((await alice.sql("SELECT avatar_bytes FROM civ.character_profiles WHERE user_id = :u"))[0][0])
    assert b"SecretCamera" not in stored and len(Image.open(io.BytesIO(stored)).getexif()) == 0


async def test_an_animation_keeps_its_first_frame_only(alice):
    frames = [Image.new("RGB", (30, 30), c) for c in ((255, 0, 0), (0, 255, 0))]
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=100, loop=0)
    r = await alice.http.post("/api/character/avatar", content=buf.getvalue(), headers={**alice.headers, "Content-Type": "image/gif"})
    assert r.status_code == 200
    stored = bytes((await alice.sql("SELECT avatar_bytes FROM civ.character_profiles WHERE user_id = :u"))[0][0])
    image = Image.open(io.BytesIO(stored))
    assert getattr(image, "n_frames", 1) == 1 and image.convert("RGB").getpixel((5, 5))[0] > 200


async def test_an_avatar_over_256_kib_is_refused(alice):
    big = b"\x89PNG" + b"0" * (262144 - 3)  # one byte over
    r = await alice.http.post("/api/character/avatar", content=big, headers={**alice.headers, "Content-Type": "image/png"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "avatar_too_large"
    # no Content-Length (chunked): the limit is enforced while reading, too
    async def chunks():
        for _ in range(300):
            yield b"x" * 1024

    r = await alice.http.post("/api/character/avatar", content=chunks(), headers={**alice.headers, "Content-Type": "image/png"})
    assert r.status_code == 413
    assert await alice.sql("SELECT 1 FROM civ.character_profiles WHERE user_id = :u") == []


@pytest.mark.parametrize(
    "content_type, body, status, code",
    [
        ("text/plain", b"hello", 415, "avatar_not_image"),
        ("application/json", b"{}", 415, "avatar_not_image"),
        ("image/png", b"", 422, "avatar_empty"),
        ("image/png", b"this is not a picture at all", 422, "avatar_invalid"),
        ("image/png", b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>", 422, "avatar_invalid"),
        ("image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 50, 422, "avatar_invalid"),  # a header with no image behind it
    ],
)
async def test_what_is_not_a_usable_picture_is_refused(alice, content_type, body, status, code):
    r = await alice.http.post("/api/character/avatar", content=body, headers={**alice.headers, "Content-Type": content_type})
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert await alice.sql("SELECT 1 FROM civ.character_profiles WHERE user_id = :u") == []


async def test_an_unsupported_format_even_with_an_image_content_type_is_refused(alice):
    buf = io.BytesIO()
    Image.new("RGB", (20, 20)).save(buf, format="BMP")
    r = await alice.http.post("/api/character/avatar", content=buf.getvalue(), headers={**alice.headers, "Content-Type": "image/bmp"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "avatar_invalid"


async def test_a_huge_image_is_refused_from_its_header_before_decoding(alice, monkeypatch):
    monkeypatch.setattr(character, "AVATAR_MAX_PIXELS", 1000)
    r = await alice.http.post("/api/character/avatar", content=png((100, 100)), headers={**alice.headers, "Content-Type": "image/png"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "avatar_invalid"


async def test_there_is_no_avatar_until_one_is_uploaded_and_it_is_private(alice, bob):
    assert (await alice.get("/api/character/avatar")).status_code == 404
    await alice.http.post("/api/character/avatar", content=png(), headers={**alice.headers, "Content-Type": "image/png"})
    assert (await alice.get("/api/character/avatar")).status_code == 200
    assert (await bob.get("/api/character/avatar")).status_code == 404
    assert (await bob.get("/api/character/me")).json()["has_avatar"] is False


# ================================================================ XP hooks: task steps and cards


async def add_plan(actor, steps: int = 2) -> tuple[uuid.UUID, list[uuid.UUID]]:
    root = (await actor.sql("INSERT INTO civ.tasks (user_id, title, source) VALUES (:u, 'Цель', 'chat_decompose') RETURNING id"))[0][0]
    ids = []
    for i in range(steps):
        ids.append(
            (await actor.sql("INSERT INTO civ.tasks (user_id, parent_task_id, position, title, source) VALUES (:u, :p, :i, :t, 'chat_decompose') RETURNING id", p=root, i=i, t=f"Шаг {i + 1}"))[0][0]
        )
    return root, ids


async def add_card(actor, *, sent: bool = True, question: str = "Столица Дании?") -> uuid.UUID:
    card = (
        await actor.sql(
            "INSERT INTO civ.cards (user_id, deck, question, options, correct_index) VALUES (:u, 'Дания', :q, ARRAY['Осло','Копенгаген','Стокгольм','Хельсинки'], 1) RETURNING id", q=question
        )
    )[0][0]
    await actor.sql(
        "INSERT INTO civ.card_reviews (user_id, card_id, last_sent_at) VALUES (:u, :c, CASE WHEN :s THEN now() END)", c=card, s=sent
    )
    return card


def tasks_of(db) -> Tasks:
    return Tasks(db, None, Settings(_env_file=None))  # type: ignore[arg-type]  # press() never touches the bot


def cards_of(db) -> Cards:
    return Cards(db, None, Settings(_env_file=None))  # type: ignore[arg-type]


async def test_finishing_a_step_earns_10_focus_xp_once(alice, db):
    _, (step, other) = await add_plan(alice)
    first = await tasks_of(db).press(alice.user_id, "UTC", step, "d")
    assert first.outcome == "done"
    assert await ledger(alice) == [(10, "focus", f"task:{step}")]

    again = await tasks_of(db).press(alice.user_id, "UTC", step, "d")  # a second click
    assert again.outcome == "settled" and len(await ledger(alice)) == 1

    assert (await tasks_of(db).press(alice.user_id, "UTC", other, "d")).outcome == "done"
    assert [r[2] for r in await ledger(alice)] == [f"task:{step}", f"task:{other}"]
    body = (await alice.get("/api/character/me")).json()
    assert body["total_xp"] == 20 and body["xp_by_category"]["focus"] == 20


async def test_dropping_or_planning_a_step_earns_nothing(alice, db):
    _, (a, b) = await add_plan(alice)
    assert (await tasks_of(db).press(alice.user_id, "UTC", a, "x")).outcome == "dropped"
    assert (await tasks_of(db).press(alice.user_id, "UTC", b, "p")).outcome == "no_slot"  # no time set: nothing planned
    assert await ledger(alice) == []


async def test_a_right_card_answer_earns_10_intellect_xp_once_per_local_day(alice, db):
    card = await add_card(alice)
    wrong = await cards_of(db).press(alice.user_id, card, 0)
    assert wrong.outcome == "wrong" and await ledger(alice) == []

    await alice.sql("UPDATE civ.card_reviews SET reviewed_at = now() - interval '1 hour', last_sent_at = now() WHERE card_id = :c", c=card)  # offered again
    right = await cards_of(db).press(alice.user_id, card, 1)
    assert right.outcome == "correct"
    rows = await ledger(alice)
    assert len(rows) == 1 and rows[0][:2] == (10, "intellect") and rows[0][2].startswith(f"card:{card}:")
    local_day = (await alice.sql("SELECT (now() AT TIME ZONE 'Europe/Copenhagen')::date::text"))[0][0]
    assert rows[0][2] == f"card:{card}:{local_day}"

    assert (await cards_of(db).press(alice.user_id, card, 1)).outcome == "settled"  # a repeated click
    assert len(await ledger(alice)) == 1


async def test_the_same_card_pays_again_on_another_day_but_not_twice_in_one(alice, db):
    card = await add_card(alice)
    assert (await cards_of(db).press(alice.user_id, card, 1)).outcome == "correct"
    await alice.sql("UPDATE civ.xp_ledger SET created_at = created_at - interval '1 day', source_ref = replace(source_ref, :d, 'old') WHERE user_id = :u", d=str(card))
    await alice.sql("UPDATE civ.card_reviews SET reviewed_at = now() - interval '1 hour', last_sent_at = now() WHERE card_id = :c", c=card)
    assert (await cards_of(db).press(alice.user_id, card, 1)).outcome == "correct"
    assert len(await ledger(alice)) == 2


async def test_a_card_that_was_never_sent_earns_nothing(alice, db):
    card = await add_card(alice, sent=False)
    assert (await cards_of(db).press(alice.user_id, card, 1)).outcome == "gone"
    assert await ledger(alice) == []


async def test_at_the_daily_cap_the_action_still_succeeds_without_xp(alice, db):
    await grant(alice, 10)  # 100 XP today already
    _, (step, _) = await add_plan(alice)
    result = await tasks_of(db).press(alice.user_id, "UTC", step, "d")
    assert result.outcome == "done"  # the user's action is never undone by the cap
    assert (await alice.sql("SELECT status FROM civ.tasks WHERE id = :s", s=step))[0][0] == "done"
    assert len(await ledger(alice)) == 10


async def test_a_failing_award_never_breaks_the_press(alice, db, monkeypatch):
    original = db.tenant_session
    calls = {"n": 0}

    def flaky(tenant_id):
        calls["n"] += 1
        if calls["n"] > 1:  # the press itself works; the award that follows it fails
            raise RuntimeError("database is gone")
        return original(tenant_id)

    _, (step, _) = await add_plan(alice)
    monkeypatch.setattr(db, "tenant_session", flaky)
    result = await tasks_of(db).press(alice.user_id, "UTC", step, "d")
    monkeypatch.undo()
    assert result.outcome == "done" and await ledger(alice) == []


async def test_award_reports_every_outcome_as_a_code(alice, bob, db):
    assert await xp.award(db, alice.user_id, "focus", "t:1") == "awarded"
    assert await xp.award(db, alice.user_id, "focus", "t:1") == "duplicate"
    assert await xp.award(db, bob.user_id, "focus", "t:1") == "awarded"  # the same ref of ANOTHER tenant is another award
    assert await xp.award(db, alice.user_id, "nonsense", "t:2") == "error"
    assert await xp.award(db, alice.user_id, "focus", "t:3", amount=101) == "error"
    await grant(alice, 8, days_ago=0, prefix="fill")  # 10 + 80 = 90 today
    assert await xp.award(db, alice.user_id, "body", "t:4") == "awarded"
    assert await xp.award(db, alice.user_id, "body", "t:5") == "daily_cap"
    assert len(await ledger(alice)) == 1 + 8 + 1  # t:1, the 8 seeded, t:4

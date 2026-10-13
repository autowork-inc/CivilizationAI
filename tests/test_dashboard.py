"""PR 2.3: GET /api/dashboard/stats - per-day aggregates in the user's local time zone, tenant-isolated, read-only.
Real PostgreSQL (RLS) + the real FastAPI app. Rows are seeded as the owner at LOCAL NOON of day offsets, so no test depends on the hour it runs."""

from __future__ import annotations

import uuid

import pytest

from tests.web_helpers import alice, bob, client  # noqa: F401

CPH = "Europe/Copenhagen"  # alice
TOKYO = "Asia/Tokyo"  # bob


def at(k: int, hours: str = "12:00", tz_param: str = "tz") -> str:
    """SQL: the local time ``hours`` of the day ``k`` days ago in the zone bound as :tz (k=0: today)."""
    return f"(((now() AT TIME ZONE :{tz_param})::date - {k})::timestamp + interval '{hours}') AT TIME ZONE :{tz_param}"


async def stats(actor, days: int | None = None) -> dict:
    r = await actor.get("/api/dashboard/stats" + ("" if days is None else f"?days={days}"))
    assert r.status_code == 200, r.text
    return r.json()


async def mood(actor, label: str, k: int, *, priority=90, source="user_confirmed", status="active", tz=CPH, hours="12:00") -> None:
    if source == "user_confirmed":
        pending = (
            await actor.sql(
                "INSERT INTO civ.pending_facts (user_id, subject, change, emotion_label, emotion_intensity, emotion_priority, emotion_expires_at, "
                "emotion_mood, evidence, source_message_ids, status, decided_at, idempotency_key) VALUES (:u, 'emotional_state', 'create', 'x', 8, :p, "
                "now() + interval '2 days', CAST(:m AS civ.shared_mood), 'said so', ARRAY[gen_random_uuid()], 'approved', now(), :i) RETURNING id",
                m=label, p=priority, i=uuid.uuid4().hex,
            )
        )[0][0]  # fmt: skip
        await actor.sql(
            f"INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, source_pending_id, mood, status, started_at, expires_at) "
            f"VALUES (:u, 'x', 8, :p, 'user_confirmed', :pf, CAST(:m AS civ.shared_mood), CAST(:st AS civ.emotion_status), {at(k, hours)}, {at(k, hours)} + interval '3 hours')",
            p=priority, pf=pending, m=label, st=status, tz=tz,
        )  # fmt: skip
    else:
        await actor.sql(
            f"INSERT INTO civ.emotional_states (user_id, label, intensity, priority, source, status, started_at, expires_at) "
            f"VALUES (:u, 'secret', 8, 40, 'inferred', 'active', {at(k, hours)}, {at(k, hours)} + interval '3 hours')",
            tz=tz,
        )


async def step(actor, *, created=0, closed: int | None = None, status="open", tz=CPH, goal: uuid.UUID | None = None) -> uuid.UUID:
    root = goal or (await actor.sql("INSERT INTO civ.tasks (user_id, title, source) VALUES (:u, 'Цель', 'chat_decompose') RETURNING id"))[0][0]
    closed_sql = at(closed) if closed is not None else at(created)
    return (
        await actor.sql(
            f"INSERT INTO civ.tasks (user_id, parent_task_id, title, status, source, created_at, updated_at) "
            f"VALUES (:u, :p, 'Шаг', :s, 'chat_decompose', {at(created)}, {closed_sql}) RETURNING id",
            p=root, s=status, tz=tz,
        )
    )[0][0]  # fmt: skip


async def card(actor, *, answered: int | None, right: bool = True, due_in_days: int = 0, tz=CPH, n=[0]) -> None:
    n[0] += 1
    cid = (
        await actor.sql(
            "INSERT INTO civ.cards (user_id, deck, question, options, correct_index) VALUES (:u, 'D', :q, ARRAY['а','б','в','г'], 1) RETURNING id",
            q=f"Вопрос {uuid.uuid4()}",
        )
    )[0][0]
    if answered is None:
        await actor.sql(f"INSERT INTO civ.card_reviews (user_id, card_id, next_review_at) VALUES (:u, :c, {at(-due_in_days)})", c=cid, tz=tz)
    else:
        await actor.sql(
            f"INSERT INTO civ.card_reviews (user_id, card_id, state, reps, reviewed_at, last_chosen, next_review_at, last_sent_at) "
            f"VALUES (:u, :c, 'review', 1, {at(answered)}, :ch, {at(-due_in_days)}, {at(answered, '11:00')})",
            c=cid, ch=1 if right else 0, tz=tz,
        )  # fmt: skip


async def xp(actor, k: int, category="focus", amount=10, tz=CPH, hours="12:00") -> None:
    await actor.sql(
        f"INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref, created_at) VALUES (:u, :a, :c, :r, {at(k, hours)})",
        a=amount, c=category, r=f"s:{uuid.uuid4()}", tz=tz,
    )  # fmt: skip


async def checkin(actor, k: int, *, reply_after_minutes: int | None, slot="10:30", tz=CPH) -> None:
    await actor.sql(
        f"INSERT INTO civ.proactive_checkin_log (user_id, slot_date, slot_time, sent_at) VALUES (:u, (now() AT TIME ZONE :tz)::date - {k}, CAST(CAST(:s AS text) AS time), {at(k, '10:30')})",
        s=slot, tz=tz,
    )  # fmt: skip
    if reply_after_minutes is not None:
        await actor.sql(
            f"INSERT INTO civ.messages (user_id, role, content, created_at) VALUES (:u, 'user', 'ответ', {at(k, '10:30')} + make_interval(mins => :m))",
            m=reply_after_minutes, tz=tz,
        )  # fmt: skip


# ================================================================ shape, window, auth


async def test_it_needs_valid_launch_data(client):
    assert (await client.get("/api/dashboard/stats")).status_code == 401
    assert (await client.get("/api/dashboard/stats", headers={"Authorization": "tma auth_date=1&hash=00"})).status_code == 401


@pytest.mark.parametrize("days", ["0", "-3", "91", "abc", "1.5"])
async def test_a_bad_period_is_a_422(alice, days):
    assert (await alice.get(f"/api/dashboard/stats?days={days}")).status_code == 422


async def test_the_default_window_is_14_local_days_ending_today(alice):
    body = await stats(alice)
    today = (await alice.sql("SELECT (now() AT TIME ZONE 'Europe/Copenhagen')::date::text"))[0][0]
    assert body["days"] == 14 and len(body["dates"]) == 14 and body["to"] == today == body["dates"][-1] and body["from"] == body["dates"][0]
    assert body["dates"] == sorted(set(body["dates"])) and body["timezone"] == CPH
    for series in (
        body["mood"]["labels"], body["mood"]["valence"], body["mood"]["counts"], *body["tasks"]["daily"].values(), *body["cards"]["daily"].values(),
        *body["xp"]["daily"].values(), *body["proactive"]["daily"].values(),
    ):  # fmt: skip
        assert len(series) == 14  # every series lines up with the x axis


@pytest.mark.parametrize("days", [1, 7, 90])
async def test_the_period_is_respected(alice, days):
    body = await stats(alice, days)
    assert len(body["dates"]) == days and len(body["xp"]["daily"]["total"]) == days


async def test_the_days_are_the_users_own_local_days(alice, bob):
    a, b = await stats(alice), await stats(bob)
    assert a["timezone"] == CPH and b["timezone"] == TOKYO
    for who, body, tz in ((alice, a, CPH), (bob, b, TOKYO)):
        assert body["to"] == (await who.sql(f"SELECT (now() AT TIME ZONE '{tz}')::date::text"))[0][0]


async def test_a_new_user_gets_empty_series_not_errors(alice):
    body = await stats(alice)
    assert {k: body["mood"][k] for k in ("labels", "valence", "counts", "days_with_data")} == {
        "labels": [None] * 14, "valence": [None] * 14, "counts": [0] * 14, "days_with_data": 0,
    }  # fmt: skip
    assert body["mood"]["daily"] == {"score": [None] * 14, "mood": [None] * 14, "messages": [0] * 14, "days_with_data": 0}
    assert len(body["mood"]["week"]) == 7 and all(d["score"] is None for d in body["mood"]["week"])
    assert body["tasks"] == {"created": 0, "done": 0, "dropped": 0, "open_backlog": 0, "open_goals": 0, "daily": {"created": [0] * 14, "done": [0] * 14, "dropped": [0] * 14}}
    assert body["cards"]["accuracy"] is None and body["cards"]["reviewed"] == 0 and body["cards"]["due_today"] == 0 and body["cards"]["total"] == 0
    assert body["xp"]["total"] == 0 and body["xp"]["level"] == 1 and body["xp"]["by_category"] == {"focus": 0, "intellect": 0, "body": 0, "connection": 0}
    assert body["proactive"]["checkins_sent"] == 0 and body["proactive"]["response_rate"] is None


async def test_the_endpoint_only_reads(alice):
    for verb in ("post", "put", "patch", "delete"):
        assert (await getattr(alice, verb)("/api/dashboard/stats")).status_code == 405
    assert (await alice.sql("SELECT count(*) FROM civ.xp_ledger WHERE user_id = :u"))[0][0] == 0


# ================================================================ mood


async def test_the_mood_of_a_day_is_the_strongest_confirmed_state_with_the_mean_valence(alice):
    await mood(alice, "sad", 2, priority=90)
    await mood(alice, "good", 2, priority=70, hours="15:00")
    await mood(alice, "energetic", 0, priority=80)
    body = (await stats(alice))["mood"]
    assert body["labels"][-3] == "sad" and body["valence"][-3] == -0.5 and body["counts"][-3] == 2  # (-2 + 1) / 2
    assert body["labels"][-1] == "energetic" and body["valence"][-1] == 2.0 and body["counts"][-1] == 1
    assert body["labels"][-2] is None and body["valence"][-2] is None  # no confirmed state is "no data", not "fine"
    assert body["days_with_data"] == 2 and body["scale"]["overwhelmed"] == -2


async def test_inferred_dismissed_and_out_of_window_moods_do_not_count(alice):
    await mood(alice, "x", 1, source="inferred")
    await mood(alice, "sad", 1, status="dismissed")
    await mood(alice, "sad", 20)  # outside 14 days
    body = (await stats(alice))["mood"]
    assert body["days_with_data"] == 0 and "secret" not in str(body)
    assert (await stats(alice, 30))["mood"]["days_with_data"] == 1  # the same state is inside a longer window


# ================================================================ tasks


async def test_task_counts_use_steps_and_are_dated_by_when_they_happened(alice):
    root = (await alice.sql("INSERT INTO civ.tasks (user_id, title, source) VALUES (:u, 'Цель', 'chat_decompose') RETURNING id"))[0][0]
    await step(alice, created=3, closed=1, status="done", goal=root)
    await step(alice, created=3, closed=0, status="done", goal=root)
    await step(alice, created=2, closed=0, status="dropped", goal=root)
    await step(alice, created=0, goal=root)  # open, created today
    await step(alice, created=30, closed=25, status="done", goal=root)  # outside the window
    body = (await stats(alice))["tasks"]
    assert (body["created"], body["done"], body["dropped"]) == (3 + 1 + 0, 2, 1)  # created: the 3 + 2 + today = 4 in window; see daily
    assert body["created"] == 4 and body["open_backlog"] == 1 and body["open_goals"] == 1  # the 30-day-old steps are done: not backlog
    assert body["daily"]["created"][-4:] == [2, 1, 0, 1] and body["daily"]["done"][-3:] == [0, 1, 1] and body["daily"]["dropped"][-3:] == [0, 0, 1]


async def test_a_goal_row_is_not_a_task_step(alice):
    await alice.sql("INSERT INTO civ.tasks (user_id, title, source) VALUES (:u, 'Только цель', 'chat_decompose')")
    body = (await stats(alice))["tasks"]
    assert body["created"] == 0 and body["open_backlog"] == 0 and body["open_goals"] == 1


# ================================================================ cards


async def test_cards_count_latest_answers_accuracy_and_the_queue_for_today(alice):
    await card(alice, answered=3, right=True, due_in_days=5)
    await card(alice, answered=2, right=False, due_in_days=1)  # due tomorrow: not in today's queue
    await card(alice, answered=0, right=True, due_in_days=0)  # answered today, due today (the same instant is "today")
    await card(alice, answered=None, due_in_days=0)  # never answered, due now
    await card(alice, answered=None, due_in_days=-3)  # overdue
    await card(alice, answered=40, right=True, due_in_days=0)  # answered long ago: outside the window, still in the queue
    body = (await stats(alice))["cards"]
    assert (body["reviewed"], body["correct"], body["accuracy"]) == (3, 2, 0.667)
    assert body["total"] == 6 and body["due_today"] == 4
    assert body["daily"]["reviewed"][-4:] == [1, 1, 0, 1] and body["daily"]["correct"][-4:] == [1, 0, 0, 1]


# ================================================================ XP


async def test_xp_is_split_by_day_and_branch_and_matches_the_ledger(alice):
    await xp(alice, 2, "focus")
    await xp(alice, 2, "intellect", 20)
    await xp(alice, 0, "body")
    await xp(alice, 0, "connection")
    await xp(alice, 0, "focus", 30)
    await xp(alice, 40, "focus", 50)  # outside the window
    body = (await stats(alice))["xp"]
    assert body["by_category"] == {"focus": 40, "intellect": 20, "body": 10, "connection": 10} and body["total"] == 80
    assert body["daily"]["total"][-3:] == [30, 0, 50] and body["daily"]["focus"][-1] == 30 and body["daily"]["intellect"][-3] == 20
    assert body["all_time"] == 130 and body["level"] == 2 and body["current_level_xp"] == 30 and body["today"] == 50
    me = (await alice.get("/api/character/me")).json()
    assert (me["total_xp"], me["level"]) == (body["all_time"], body["level"])  # one source of truth


async def test_an_award_just_after_local_midnight_belongs_to_the_local_day(bob):
    await xp(bob, 0, "focus", hours="00:30", tz=TOKYO)  # 00:30 in Tokyo = the previous UTC day
    await xp(bob, 1, "body", hours="23:30", tz=TOKYO)  # 23:30 in Tokyo = still the same UTC day as "today"
    body = await stats(bob, 3)
    assert body["xp"]["daily"]["focus"] == [0, 0, 10] and body["xp"]["daily"]["body"] == [0, 10, 0]


# ================================================================ proactivity


async def test_a_checkin_is_answered_if_the_user_wrote_within_the_window(alice):
    await checkin(alice, 2, reply_after_minutes=20)
    await checkin(alice, 1, reply_after_minutes=None)
    await checkin(alice, 0, reply_after_minutes=7 * 60)  # too late: another conversation
    body = (await stats(alice))["proactive"]
    assert (body["checkins_sent"], body["checkins_answered"], body["response_rate"]) == (3, 1, 0.333)
    assert body["daily"]["sent"][-3:] == [1, 1, 1] and body["daily"]["answered"][-3:] == [1, 0, 0] and body["answer_window_hours"] == 6


async def test_a_message_before_the_checkin_is_not_an_answer(alice):
    await alice.sql(f"INSERT INTO civ.messages (user_id, role, content, created_at) VALUES (:u, 'user', 'раньше', {at(1, '09:00')})", tz=CPH)
    await alice.sql(f"INSERT INTO civ.messages (user_id, role, content, created_at) VALUES (:u, 'assistant', 'это я', {at(1, '10:40')})", tz=CPH)
    await checkin(alice, 1, reply_after_minutes=None)
    assert (await stats(alice))["proactive"]["checkins_answered"] == 0  # neither an earlier message nor the assistant's own counts


# ================================================================ isolation, window edges


async def test_a_user_sees_only_their_own_numbers(alice, bob):
    await xp(alice, 0, "focus", 50)
    await mood(alice, "sad", 0)
    await step(alice, created=0)
    await card(alice, answered=0)
    await checkin(alice, 0, reply_after_minutes=5)
    seen = await stats(bob)
    assert seen["xp"]["total"] == 0 and seen["mood"]["days_with_data"] == 0 and seen["tasks"]["created"] == 0 and seen["tasks"]["open_goals"] == 0
    assert seen["cards"]["total"] == 0 and seen["proactive"]["checkins_sent"] == 0
    assert (await stats(alice))["xp"]["total"] == 50


async def test_the_first_day_of_the_window_is_in_and_the_day_before_is_out(alice):
    await xp(alice, 6, "focus")  # the first day of a 7-day window
    await xp(alice, 7, "body")  # one day too old
    body = (await stats(alice, 7))["xp"]
    assert body["total"] == 10 and body["daily"]["focus"][0] == 10 and body["by_category"]["body"] == 0
    assert (await stats(alice, 8))["xp"]["total"] == 20


async def test_a_dashboard_with_everything_stays_compact(alice):
    for k in range(14):
        await xp(alice, k, "focus")
        await mood(alice, "good", k)
    raw = (await alice.get("/api/dashboard/stats?days=14")).content
    assert len(raw) < 6_000  # plain arrays for a chart, not a pile of records


# ---------------------------------------------------------------- the daily mood estimate (civ.daily_mood) and the Monday..Sunday week


async def estimate(actor, k: int, score: int, mood: str | None = None, *, messages: int = 5) -> None:
    await actor.sql(
        "INSERT INTO civ.daily_mood (user_id, local_date, score, mood, message_count) "
        f"VALUES (:u, (now() AT TIME ZONE :tz)::date - {k}, :s, CAST(:m AS civ.shared_mood), :n)",
        s=score, m=mood, n=messages, tz=CPH,
    )  # fmt: skip


async def test_the_daily_estimates_fill_the_series_and_a_day_without_one_stays_null(alice):
    await estimate(alice, 0, 2, "energetic", messages=9)
    await estimate(alice, 2, -1, "tired")
    body = await stats(alice)
    daily = body["mood"]["daily"]
    assert daily["score"][-1] == 2 and daily["mood"][-1] == "energetic" and daily["messages"][-1] == 9
    assert daily["score"][-3] == -1 and daily["mood"][-3] == "tired"
    assert daily["score"][-2] is None and daily["messages"][-2] == 0  # a day nobody read is a gap, not a zero
    assert daily["days_with_data"] == 2
    assert body["mood"]["days_with_data"] == 0  # the confirmed states are a separate series and are untouched


async def test_the_week_runs_monday_to_sunday_in_the_users_local_calendar(alice):
    today = (await alice.sql(f"SELECT (now() AT TIME ZONE '{CPH}')::date"))[0][0]
    monday = today - __import__("datetime").timedelta(days=today.weekday())
    await alice.sql(
        "INSERT INTO civ.daily_mood (user_id, local_date, score, mood, message_count) VALUES (:u, :d, 1, 'good', 4)", d=monday
    )
    week = (await stats(alice, days=1))["mood"]["week"]  # a one-day window still reports the whole week
    assert [d["weekday"] for d in week] == list(range(7))
    assert week[0]["date"] == monday.isoformat() and week[0]["score"] == 1 and week[0]["mood"] == "good" and week[0]["messages"] == 4
    assert [d["future"] for d in week] == [i > today.weekday() for i in range(7)]  # days that have not come yet are marked
    assert all(d["score"] is None for d in week[1:])


async def test_estimates_of_one_tenant_never_reach_another(alice, bob):
    await estimate(alice, 0, -2, "sad")
    assert (await stats(bob))["mood"]["daily"]["days_with_data"] == 0
    assert all(d["score"] is None for d in (await stats(bob))["mood"]["week"])

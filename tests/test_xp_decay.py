"""v2.0 PR 5.3: the decay system (migration 0023). Negative XP entries, the floor invariant (a branch never below 0, so level >= 1), the nightly rules
(no workouts for more than 5 days -> -10 body; a task still open 48 hours after its deadline -> -10 focus, once per task), strict idempotency per
``source_ref``, the Celery dispatcher, the streak, and what the API shows. Real PostgreSQL (RLS, triggers); no Telegram, no Gemini."""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import DBAPIError

from ai_civ.bot import decay
from ai_civ.config import Settings
from ai_civ.tasks import DispatchError, decay_tenant_async, process_xp_decay_async
from tests.helpers import new_user, owner_exec, tenant_exec
from tests.test_proactive_schedule import runtime
from tests.web_helpers import alice, bob, client  # noqa: F401

NOW = datetime(2026, 10, 7, 3, 10, tzinfo=UTC)  # tenants of these tests are in UTC: the local date is 2026-10-07
SETTINGS = Settings(_env_file=None)


async def seed_xp(actor, category: str, amount: int, *, days_ago: int = 5, ref: str | None = None) -> None:
    """An award of ``amount`` XP some days ago (the cap is per local day, so several 10-XP rows on different days)."""
    left, i = amount, 0
    while left > 0:
        part = min(left, 50)
        await actor.sql(
            "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref, created_at) VALUES (:u, :a, :c, :r, now() - make_interval(days => :d, mins => :m))",
            a=part, c=category, r=f"{ref or 'seed'}:{category}:{days_ago}:{i}", d=days_ago + i, m=i,
        )  # fmt: skip
        left -= part
        i += 1


async def branch(actor, category: str) -> int:
    return (await actor.sql("SELECT COALESCE(sum(amount), 0)::int FROM civ.xp_ledger WHERE user_id = :u AND category = :c", c=category))[0][0]


async def total(actor) -> tuple[int, int]:
    row = (await actor.sql("SELECT total_xp, level FROM civ.character_progress WHERE user_id = :u"))[0]
    return row[0], row[1]


async def workout(actor, *, days_ago: int) -> None:
    await actor.sql("INSERT INTO civ.workout_sessions (user_id, started_at) VALUES (:u, :t)", t=NOW - timedelta(days=days_ago, hours=5))


async def task(actor, *, due_hours_ago: float | None, status: str = "open", title: str = "Шаг") -> uuid.UUID:
    due = None if due_hours_ago is None else NOW - timedelta(hours=due_hours_ago)
    return (
        await actor.sql(
            "INSERT INTO civ.tasks (user_id, title, source, status, due) VALUES (:u, :t, 'mini_app', :s, :d) RETURNING id",
            t=f"{title} {uuid.uuid4().hex[:6]}", s=status, d=due,
        )
    )[0][0]  # fmt: skip


async def run(db, actor, *, now: datetime = NOW, settings: Settings = SETTINGS) -> decay.DecayResult:
    return await decay.run_tenant(db, actor.user_id, now=now, settings=settings)


async def refs(actor) -> list[tuple[int, str, str]]:
    return [(r[0], r[1], r[2]) for r in await actor.sql("SELECT amount, category, source_ref FROM civ.xp_ledger WHERE user_id = :u AND amount <= 0 ORDER BY source_ref")]


# ================================================================== database: the ledger now carries penalties


async def test_a_penalty_takes_xp_away_is_idempotent_and_leaves_the_progress_consistent(alice, db):
    await seed_xp(alice, "body", 40)
    assert await tenant_exec(db, alice.user_id, "SELECT civ.apply_xp_penalty('body', 'decay:body:x:2026-10-07', 10)") == [("penalized",)]
    assert await tenant_exec(db, alice.user_id, "SELECT civ.apply_xp_penalty('body', 'decay:body:x:2026-10-07', 10)") == [("duplicate",)]
    assert await branch(alice, "body") == 30 and await total(alice) == (30, 1)
    row = (await alice.sql("SELECT xp_by_category::text FROM civ.character_progress WHERE user_id = :u"))[0][0]
    assert '"body": 30' in row


async def test_a_penalty_never_takes_more_than_the_branch_has_and_a_zero_marker_closes_it(alice, db):
    await seed_xp(alice, "focus", 6)
    assert await tenant_exec(db, alice.user_id, "SELECT civ.apply_xp_penalty('focus', 'penalty:task:a', 10)") == [("penalized",)]
    assert await branch(alice, "focus") == 0  # clamped to what was there: 6, not 10
    assert await tenant_exec(db, alice.user_id, "SELECT civ.apply_xp_penalty('focus', 'penalty:task:b', 10)") == [("floor",)]
    assert await branch(alice, "focus") == 0 and await total(alice) == (0, 1)
    assert [(a, ref) for a, _c, ref in await refs(alice)] == [(-6, "penalty:task:a"), (0, "penalty:task:b")]
    # the marker means the same penalty is never applied later, when the user has earned something again
    await tenant_exec(db, alice.user_id, "SELECT civ.award_xp('focus', 'task:new', 10)")
    assert await tenant_exec(db, alice.user_id, "SELECT civ.apply_xp_penalty('focus', 'penalty:task:b', 10)") == [("duplicate",)]
    assert await branch(alice, "focus") == 10


async def test_the_floor_holds_whoever_inserts_even_the_owner(alice, owner_engine):
    await seed_xp(alice, "body", 10)
    for amount, ref in ((-11, "decay:body:direct"), (-100, "penalty:task:direct")):
        with pytest.raises(DBAPIError) as raised:
            await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, :a, 'body', :r)", a=amount, r=ref)
        assert "cannot go below 0" in str(raised.value)
    await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, -10, 'body', 'decay:body:exact')")  # exactly to zero is fine
    assert await branch(alice, "body") == 0
    assert await total(alice) == (0, 1)  # the minimum level is 1


@pytest.mark.parametrize(
    "amount, ref",
    [(-5, "x"), (-5, "task:1"), (0, "x"), (0, "seed"), (-101, "decay:body:big"), (101, "decay:body:big")],
)
async def test_only_a_system_penalty_may_be_zero_or_negative(alice, amount, ref):
    """Earnings are 1..100; anything at or below zero must carry a decay:/penalty: reference (and stay within -100)."""
    await seed_xp(alice, "body", 100, days_ago=9)
    with pytest.raises(DBAPIError):
        await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, :a, 'body', :r)", a=amount, r=ref)


@pytest.mark.parametrize("category, ref, amount", [("magic", "decay:x", 10), ("body", "task:1", 10), ("body", "", 10), ("body", "decay:x", 0), ("body", "decay:x", 101), ("body", "decay:\n", 10)])
async def test_nonsense_penalties_are_refused_by_the_function(alice, db, category, ref, amount):
    with pytest.raises(DBAPIError):
        await tenant_exec(db, alice.user_id, "SELECT civ.apply_xp_penalty(:c, :r, :a)", c=category, r=ref, a=amount)


async def test_a_penalty_does_not_reopen_the_daily_cap_and_the_cap_still_counts_earnings_only(alice, db):
    for i in range(10):
        assert await tenant_exec(db, alice.user_id, "SELECT civ.award_xp('focus', :r)", r=f"cap:{i}") == [("awarded",)]
    assert await tenant_exec(db, alice.user_id, "SELECT civ.apply_xp_penalty('focus', 'penalty:task:cap', 30)") == [("penalized",)]
    assert await tenant_exec(db, alice.user_id, "SELECT civ.award_xp('focus', 'cap:more')") == [("daily_cap",)]  # 100 earned today: the penalty made no room
    assert (await alice.sql("SELECT xp_today FROM civ.character_progress WHERE user_id = :u"))[0][0] == 100
    assert await total(alice) == (70, 1)


async def test_concurrent_penalties_never_push_a_branch_below_zero(alice, db):
    await seed_xp(alice, "body", 25)
    results = await asyncio.gather(
        *[tenant_exec(db, alice.user_id, "SELECT civ.apply_xp_penalty('body', :r, 10)", r=f"decay:body:race:{i}") for i in range(8)]
    )
    assert sorted(r[0][0] for r in results).count("penalized") == 3  # 10 + 10 + 5; the rest are floor markers
    assert await branch(alice, "body") == 0


async def test_the_streak_counts_consecutive_local_days_and_today_may_still_be_empty(alice):
    for days_ago in (0, 1, 2, 4):  # a gap at day 3
        await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref, created_at) VALUES (:u, 10, 'focus', :r, now() - make_interval(days => :d))", r=f"s:{days_ago}", d=days_ago)
    streak = lambda: alice.sql("SELECT civ.xp_streak_days(:u)")  # noqa: E731
    assert (await streak())[0][0] == 3
    await alice.sql("DELETE FROM civ.xp_ledger WHERE user_id = :u AND source_ref = 's:0'")  # nothing yet today: yesterday still counts
    assert (await streak())[0][0] == 2
    await alice.sql("DELETE FROM civ.xp_ledger WHERE user_id = :u AND source_ref = 's:1'")  # yesterday is empty too: the streak is over
    assert (await streak())[0][0] == 0
    await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 10, 'focus', 'pen')")  # a penalty is not an earning
    await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, -5, 'focus', 'decay:focus:s')")
    assert (await streak())[0][0] == 1


async def test_application_role_can_call_but_the_ledger_is_still_append_only(alice, db):
    await seed_xp(alice, "body", 20)
    for statement in ("UPDATE civ.xp_ledger SET amount = -1", "DELETE FROM civ.xp_ledger"):
        with pytest.raises(DBAPIError):
            await tenant_exec(db, alice.user_id, statement)


# ================================================================== the nightly rules


async def test_no_workout_for_more_than_five_days_costs_ten_body_xp_once_per_local_day(alice, db):
    await seed_xp(alice, "body", 30)
    await workout(alice, days_ago=6)
    first = await run(db, alice)
    assert first.body == decay.PENALIZED and first.local_date == "2026-10-07"
    assert await branch(alice, "body") == 20
    assert [r[2] for r in await refs(alice)] == [f"decay:body:{alice.user_id}:2026-10-07"]

    again = await run(db, alice)  # the same date: nothing more
    assert again.body == decay.DUPLICATE and await branch(alice, "body") == 20
    assert (await run(db, alice, now=NOW + timedelta(hours=5))).body == decay.DUPLICATE  # still the same LOCAL date

    nxt = await run(db, alice, now=NOW + timedelta(days=1))  # the next night: the next penalty
    assert nxt.body == decay.PENALIZED and await branch(alice, "body") == 10
    assert len(await refs(alice)) == 2


@pytest.mark.parametrize("days_ago, penalized", [(2, False), (4, False), (6, True), (30, True)])
async def test_the_break_must_be_longer_than_five_days(alice, db, days_ago, penalized):
    await seed_xp(alice, "body", 30)
    await alice.sql("INSERT INTO civ.workout_sessions (user_id, started_at) VALUES (:u, :t)", t=NOW - timedelta(days=days_ago))
    assert ((await run(db, alice)).body == decay.PENALIZED) is penalized


async def test_exactly_five_days_is_not_more_than_five(alice, db):
    await seed_xp(alice, "body", 30)
    await alice.sql("INSERT INTO civ.workout_sessions (user_id, started_at) VALUES (:u, :t)", t=NOW - timedelta(days=5))
    assert (await run(db, alice)).body is None
    await alice.sql("UPDATE civ.workout_sessions SET started_at = :t WHERE user_id = :u", t=NOW - timedelta(days=5, minutes=1))
    assert (await run(db, alice)).body == decay.PENALIZED


async def test_a_recent_workout_a_newcomer_and_an_empty_branch_are_never_punished(alice, bob, db):
    await seed_xp(alice, "body", 30)
    await workout(alice, days_ago=2)
    assert (await run(db, alice)).body is None  # trained recently

    await seed_xp(bob, "body", 30)
    assert (await run(db, bob)).body is None  # never logged a workout: a newcomer is not punished for not having started

    empty = await new_user(alice.owner_engine, tz="UTC")
    await owner_exec(alice.owner_engine, "INSERT INTO civ.workout_sessions (user_id, started_at) VALUES (:u, :t)", u=empty, t=NOW - timedelta(days=30))
    outcome = await decay.run_tenant(db, empty, now=NOW, settings=SETTINGS)
    assert outcome.body is None  # nothing to lose: no row is written, so no marker a day for ever
    assert await owner_exec(alice.owner_engine, "SELECT count(*) FROM civ.xp_ledger WHERE user_id = :u", u=empty) == [(0,)]


async def test_an_overdue_open_task_costs_ten_focus_xp_once_for_good(alice, db):
    await seed_xp(alice, "focus", 50)
    late, fine, done, none, edge = (
        await task(alice, due_hours_ago=49),
        await task(alice, due_hours_ago=47),
        await task(alice, due_hours_ago=100, status="done"),
        await task(alice, due_hours_ago=None),
        await task(alice, due_hours_ago=48),
    )
    result = await run(db, alice)
    assert set(result.tasks) == {str(late)} and result.tasks[str(late)] == decay.PENALIZED
    assert await branch(alice, "focus") == 40
    assert [r[2] for r in await refs(alice)] == [f"penalty:task:{late}"]

    assert edge is not None and (await run(db, alice, now=NOW + timedelta(minutes=30))).tasks == {str(edge): decay.PENALIZED}  # exactly 48 h is NOT "more than 48 hours"; 48.5 h is

    nxt = await run(db, alice, now=NOW + timedelta(days=1))  # the next night: the SAME task is not punished again, the one that has become overdue since is
    assert set(nxt.tasks) == {str(fine)} and await branch(alice, "focus") == 20
    ledger = [r[2] for r in await refs(alice)]
    assert ledger.count(f"penalty:task:{late}") == 1 and f"penalty:task:{done}" not in ledger and f"penalty:task:{none}" not in ledger


async def test_a_dropped_or_finished_task_is_forgiven_even_after_the_penalty_was_due(alice, db):
    await seed_xp(alice, "focus", 50)
    t = await task(alice, due_hours_ago=60)
    await alice.sql("UPDATE civ.tasks SET status = 'done' WHERE id = :t", t=t)
    assert (await run(db, alice)).tasks == {}
    assert await branch(alice, "focus") == 50


async def test_a_backlog_is_worked_off_over_several_nights_not_in_one_blow(alice, db):
    await seed_xp(alice, "focus", 100)
    ids = [await task(alice, due_hours_ago=100 + i) for i in range(8)]
    first = await run(db, alice)
    assert len(first.tasks) == SETTINGS.xp_decay_max_task_penalties == 5 and await branch(alice, "focus") == 50
    second = await run(db, alice, now=NOW + timedelta(days=1))
    assert len(second.tasks) == 3 and set(first.tasks).isdisjoint(second.tasks)
    assert set(first.tasks) | set(second.tasks) == {str(i) for i in ids}
    assert await run(db, alice, now=NOW + timedelta(days=2)) == decay.DecayResult("2026-10-09")


async def test_a_task_penalty_on_an_empty_branch_leaves_a_marker_and_never_goes_negative(alice, db):
    t = await task(alice, due_hours_ago=60)
    result = await run(db, alice)
    assert result.tasks == {str(t): decay.FLOOR} and await branch(alice, "focus") == 0 and await total(alice) == (0, 1)
    await tenant_exec(db, alice.user_id, "SELECT civ.award_xp('focus', 'later', 10)")
    await run(db, alice, now=NOW + timedelta(days=1))
    assert await branch(alice, "focus") == 10  # the closed task does not come back to take it


async def test_both_rules_in_one_night_and_a_repeat_of_the_whole_night_changes_nothing(alice, db):
    await seed_xp(alice, "body", 30)
    await seed_xp(alice, "focus", 30)
    await workout(alice, days_ago=7)
    await task(alice, due_hours_ago=72)
    first = await run(db, alice)
    assert first.penalties == 2 and await branch(alice, "body") == 20 and await branch(alice, "focus") == 20
    repeat = await run(db, alice)
    assert repeat.penalties == 0 and await branch(alice, "body") == 20 and await branch(alice, "focus") == 20
    assert len(await refs(alice)) == 2


async def test_parallel_runs_of_the_same_night_punish_once(alice, db):
    await seed_xp(alice, "body", 50)
    await workout(alice, days_ago=9)
    await task(alice, due_hours_ago=90)
    await seed_xp(alice, "focus", 50)
    results = await asyncio.gather(*[run(db, alice) for _ in range(6)])
    assert sum(r.penalties for r in results) == 2
    assert await branch(alice, "body") == 40 and await branch(alice, "focus") == 40


async def test_a_tenant_who_has_not_finished_onboarding_and_a_switched_off_job_do_nothing(db, owner_engine, alice):
    pending = await new_user(owner_engine, tz="UTC", onboarding_state="pending")
    await owner_exec(owner_engine, "INSERT INTO civ.workout_sessions (user_id, started_at) VALUES (:u, :t)", u=pending, t=NOW - timedelta(days=30))
    assert await decay.run_tenant(db, pending, now=NOW, settings=SETTINGS) == decay.DecayResult()

    await seed_xp(alice, "body", 30)
    await workout(alice, days_ago=9)
    off = Settings(_env_file=None, xp_decay_enabled=False)
    assert await decay.run_tenant(db, alice.user_id, now=NOW, settings=off) == decay.DecayResult()
    assert await branch(alice, "body") == 30


async def test_the_local_date_is_the_tenants_own(db, owner_engine):
    tokyo = await new_user(owner_engine, tz="Asia/Tokyo")
    await owner_exec(owner_engine, "INSERT INTO civ.workout_sessions (user_id, started_at) VALUES (:u, :t)", u=tokyo, t=NOW - timedelta(days=9))
    await owner_exec(owner_engine, "INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref, created_at) VALUES (:u, 30, 'body', 's', now() - interval '20 days')", u=tokyo)
    late_utc = datetime(2026, 10, 7, 20, 0, tzinfo=UTC)  # 05:00 on the 8th in Tokyo
    result = await decay.run_tenant(db, tokyo, now=late_utc, settings=SETTINGS)
    assert result.local_date == "2026-10-08"
    assert (await owner_exec(owner_engine, "SELECT source_ref FROM civ.xp_ledger WHERE user_id = :u AND amount < 0", u=tokyo)) == [(f"decay:body:{tokyo}:2026-10-08",)]


async def test_tenants_are_isolated(alice, bob, db):
    await seed_xp(alice, "body", 30)
    await workout(alice, days_ago=9)
    await seed_xp(bob, "body", 30)
    await run(db, alice)
    assert await branch(alice, "body") == 20 and await branch(bob, "body") == 30 and await refs(bob) == []


# ================================================================== Celery: the dispatcher and the per-tenant task


async def test_the_dispatcher_hands_out_only_the_tenants_whose_local_clock_shows_the_night_hour(db, owner_engine):
    copenhagen = await new_user(owner_engine, tz="Europe/Copenhagen")  # 03:10 UTC = 05:10 in Copenhagen (CEST, UTC+2)
    utc = await new_user(owner_engine, tz="UTC")  # 03:10: night
    tokyo = await new_user(owner_engine, tz="Asia/Tokyo")  # 12:10: day
    queued: list[uuid.UUID] = []
    count = await process_xp_decay_async(runtime(db), queued.append, now=NOW)
    assert utc in queued and copenhagen not in queued and tokyo not in queued and count == len(queued)
    # Copenhagen reaches 03:xx two hours earlier in UTC
    queued2: list[uuid.UUID] = []
    await process_xp_decay_async(runtime(db), queued2.append, now=NOW - timedelta(hours=2))
    assert copenhagen in queued2 and utc not in queued2


async def test_a_broken_hand_off_for_one_tenant_does_not_stop_the_rest_and_fails_the_run(db, owner_engine):
    first = await new_user(owner_engine, tz="UTC")
    second = await new_user(owner_engine, tz="UTC")
    queued: list[uuid.UUID] = []

    def flaky(tenant: uuid.UUID) -> None:
        if tenant == first:
            raise ConnectionError("broker down")
        queued.append(tenant)

    with pytest.raises(DispatchError, match=str(first)):
        await process_xp_decay_async(runtime(db), flaky, now=NOW)  # fail-fast: the task fails and Celery retries; the others were still offered
    assert second in queued and first not in queued


async def test_the_switch_turns_the_dispatcher_off(db):
    rt = dataclasses.replace(runtime(db), settings=Settings(_env_file=None, xp_decay_enabled=False))
    assert await process_xp_decay_async(rt, lambda t: pytest.fail("nothing may be queued"), now=NOW) == 0


async def test_the_tenant_task_body_is_the_same_idempotent_night(alice, db):
    await seed_xp(alice, "body", 30)
    await workout(alice, days_ago=9)
    rt = runtime(db)
    first = await decay_tenant_async(rt, alice.user_id, now=NOW)
    second = await decay_tenant_async(rt, alice.user_id, now=NOW)
    assert first.body == decay.PENALIZED and second.body == decay.DUPLICATE and await branch(alice, "body") == 20


def test_the_beat_entry_and_the_tasks_exist_and_a_failure_is_retried_not_swallowed():
    from ai_civ.celery_app import app
    from ai_civ.tasks import decay_tenant, process_xp_decay

    entry = app.conf.beat_schedule["xp-decay"]
    assert entry["task"] == "ai_civ.process_xp_decay"
    assert process_xp_decay.name == "ai_civ.process_xp_decay" and decay_tenant.name == "ai_civ.decay_tenant"
    for task_ in (process_xp_decay, decay_tenant):
        assert Exception in task_.autoretry_for and task_.max_retries == 3  # fail-fast: a failing run is retried, boundedly


# ================================================================== the API: streak, penalties, the dashboard


async def test_the_character_screen_shows_the_streak_the_penalties_and_the_decay_warning(alice, db):
    await seed_xp(alice, "body", 40, days_ago=0)
    await workout(alice, days_ago=9)
    await run(db, alice, now=datetime.now(UTC))
    body = (await alice.get("/api/character/me")).json()
    pen = body["penalties"]
    assert pen["penalty_today"] == 10 and pen["penalty_week"] == 10 and pen["penalty_total"] == 10
    assert pen["recent"][0]["amount"] == -10 and pen["recent"][0]["kind"] == "body" and pen["recent"][0]["category"] == "body"
    assert pen["streak_days"] >= 1 and pen["body_idle_days"] >= 8 and pen["body_threshold_days"] == 5 and pen["body_decay_active"] is True
    assert body["xp_by_category"]["body"] == body["total_xp"] and body["level"] >= 1


async def test_the_character_screen_without_any_history_is_calm(alice):
    pen = (await alice.get("/api/character/me")).json()["penalties"]
    assert pen == {
        "streak_days": 0, "penalty_today": 0, "penalty_week": 0, "penalty_total": 0, "recent": [],
        "body_idle_days": None, "body_threshold_days": 5, "body_decay_active": False,
    }  # fmt: skip


async def test_a_floored_marker_is_not_listed_as_a_penalty(alice, db):
    await task(alice, due_hours_ago=80)
    await run(db, alice, now=datetime.now(UTC))
    pen = (await alice.get("/api/character/me")).json()["penalties"]
    assert pen["recent"] == [] and pen["penalty_total"] == 0


async def test_the_dashboard_splits_earned_xp_from_penalties_per_day(alice, db):
    await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref, created_at) VALUES (:u, 30, 'body', 'e1', now() - interval '1 day')")
    await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref, created_at) VALUES (:u, -10, 'body', 'decay:body:d1', now() - interval '1 day' + interval '1 minute')")
    await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, 20, 'focus', 'e2')")
    await alice.sql("INSERT INTO civ.xp_ledger (user_id, amount, category, source_ref) VALUES (:u, -5, 'focus', 'penalty:task:d2')")
    xp = (await alice.get("/api/dashboard/stats", params={"days": 3})).json()["xp"]
    assert xp["earned"] == 50 and xp["penalty"] == -15 and xp["total"] == 35
    assert xp["daily"]["earned"][-2:] == [30, 20] and xp["daily"]["penalty"][-2:] == [-10, -5]
    assert xp["daily"]["body"][-2:] == [20, 0] and xp["daily"]["focus"][-2:] == [0, 15] and xp["daily"]["total"] == [0, 20, 15]
    assert xp["by_category"]["body"] == 20 and xp["by_category"]["focus"] == 15 and xp["all_time"] == 35
    assert all(len(series) == 3 for series in xp["daily"].values())

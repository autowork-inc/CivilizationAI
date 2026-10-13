"""Module 3: per-user proactive schedule (due window, quiet hours, once per local day, atomic claims, the Beat dispatcher)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from ai_civ.bot import schedule
from ai_civ.config import Settings
from ai_civ.runtime import Runtime
from ai_civ.tasks import DispatchError, dispatch_scheduled_proactive_async, dispatch_weekly_review_async
from tests.helpers import new_user, owner_exec

GRACE = timedelta(minutes=30)
# 2026-01-15 is in winter time: Copenhagen = UTC+1, New York = UTC-5 (no DST ambiguity in these tests)
AT_1305_COPENHAGEN = datetime(2026, 1, 15, 12, 5, tzinfo=UTC)


def local(h: int, m: int = 0, zone: str = "Europe/Copenhagen") -> datetime:
    return datetime(2026, 1, 15, h, m, tzinfo=ZoneInfo(zone))


# ---------------------------------------------------------------- pure window logic


@pytest.mark.parametrize(
    "now, expected",
    [
        (local(12, 59), False),  # not yet
        (local(13, 0), True),  # the start is inclusive
        (local(13, 29), True),
        (local(13, 30), False),  # the end of the grace window is exclusive: a late nudge is skipped, not sent hours later
        (local(18, 0), False),
    ],
)
def test_a_nudge_is_due_only_inside_its_grace_window(now, expected):
    assert schedule.is_due(now, time(13, 0), None, GRACE) is expected


def test_a_nudge_that_fired_today_is_not_due_again_but_yesterdays_does_not_count():
    assert not schedule.is_due(local(13, 5), time(13, 0), date(2026, 1, 15), GRACE)
    assert schedule.is_due(local(13, 5), time(13, 0), date(2026, 1, 14), GRACE)


# ---------------------------------------------------------------- claims (real PostgreSQL)


async def tenant_with_schedule(owner_engine, *, tz="Europe/Copenhagen", state="done", at="13:00", enabled=True, key="meal_check"):
    user = await new_user(owner_engine, tz=tz, onboarding_state=state)
    await owner_exec(
        owner_engine,
        "INSERT INTO civ.proactive_schedule (user_id, trigger_key, local_time, enabled) VALUES (:u, :k, :t, :e)",
        u=user, k=key, t=time.fromisoformat(at), e=enabled,
    )  # fmt: skip
    return user


async def last_fired(owner_engine, user) -> date | None:
    return (await owner_exec(owner_engine, "SELECT last_fired_on FROM civ.proactive_schedule WHERE user_id = :u", u=user))[0][0]


async def test_a_due_nudge_is_claimed_once_per_local_day(owner_engine, db):
    user = await tenant_with_schedule(owner_engine)
    first = await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN, grace=GRACE)
    assert [d.trigger_key for d in first] == ["meal_check"] and first[0].local_date == date(2026, 1, 15)
    assert await last_fired(owner_engine, user) == date(2026, 1, 15)

    assert await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN, grace=GRACE) == []  # a re-run never fires it twice
    assert await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN + timedelta(minutes=20), grace=GRACE) == []
    tomorrow = await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN + timedelta(days=1), grace=GRACE)
    assert [d.trigger_key for d in tomorrow] == ["meal_check"]  # the next local day fires again


async def test_after_the_grace_window_the_nudge_is_skipped_not_sent_late(owner_engine, db):
    user = await tenant_with_schedule(owner_engine)
    assert await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN + timedelta(hours=3), grace=GRACE) == []
    assert await last_fired(owner_engine, user) is None  # nothing was marked: tomorrow still fires


async def test_the_clock_is_the_tenants_own(owner_engine, db):
    new_york = await tenant_with_schedule(owner_engine, tz="America/New_York", at="13:00")
    cph = await tenant_with_schedule(owner_engine, tz="Europe/Copenhagen", at="13:00")
    now = datetime(2026, 1, 15, 18, 5, tzinfo=UTC)  # New York 13:05, Copenhagen 19:05
    assert [d.trigger_key for d in await schedule.claim_due(db, new_york, now=now, grace=GRACE)] == ["meal_check"]
    assert await schedule.claim_due(db, cph, now=now, grace=GRACE) == []


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param({"state": "pending"}, id="onboarding-not-done"),
        pytest.param({"state": "in_progress"}, id="onboarding-in-progress"),
        pytest.param({"enabled": False}, id="disabled"),
        pytest.param({"key": "no_such_trigger"}, id="unknown-trigger"),
    ],
)
async def test_nothing_is_claimed_when_it_must_not_fire(owner_engine, db, setup):
    user = await tenant_with_schedule(owner_engine, **setup)
    assert await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN, grace=GRACE) == []
    assert await last_fired(owner_engine, user) is None


async def test_quiet_hours_win_over_the_schedule_even_across_midnight(owner_engine, db):
    user = await tenant_with_schedule(owner_engine, at="13:00")
    await owner_exec(owner_engine, "UPDATE civ.users SET quiet_hours_start = '12:00', quiet_hours_end = '14:00' WHERE id = :u", u=user)
    assert await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN, grace=GRACE) == []
    assert await last_fired(owner_engine, user) is None

    night = await tenant_with_schedule(owner_engine, at="23:30")  # default quiet hours 22:00-08:00 wrap midnight
    assert await schedule.claim_due(db, night, now=datetime(2026, 1, 15, 22, 35, tzinfo=UTC), grace=GRACE) == []


async def test_a_released_claim_can_be_claimed_again(owner_engine, db):
    user = await tenant_with_schedule(owner_engine)
    (due,) = await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN, grace=GRACE)
    await schedule.release(db, user, due)
    assert await last_fired(owner_engine, user) is None
    assert len(await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN, grace=GRACE)) == 1


async def test_release_does_not_undo_a_later_days_claim(owner_engine, db):
    user = await tenant_with_schedule(owner_engine)
    (today,) = await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN, grace=GRACE)
    await schedule.claim_due(db, user, now=AT_1305_COPENHAGEN + timedelta(days=1), grace=GRACE)
    await schedule.release(db, user, today)  # stale release: the row now carries tomorrow's date
    assert await last_fired(owner_engine, user) == date(2026, 1, 16)


async def test_a_tenant_never_claims_another_tenants_schedule(owner_engine, db):
    a = await tenant_with_schedule(owner_engine)
    b = await tenant_with_schedule(owner_engine, at="13:00")
    await schedule.claim_due(db, a, now=AT_1305_COPENHAGEN, grace=GRACE)
    assert await last_fired(owner_engine, b) is None


# ---------------------------------------------------------------- the Beat dispatcher


def runtime(db) -> Runtime:
    return Runtime(db=db, settings=Settings(_env_file=None))


async def test_the_dispatcher_submits_only_what_is_due_for_each_tenant(owner_engine, db):
    cph = await tenant_with_schedule(owner_engine, tz="Europe/Copenhagen")
    new_york = await tenant_with_schedule(owner_engine, tz="America/New_York")  # 07:05 there: not 13:00
    pending = await tenant_with_schedule(owner_engine, state="pending")
    submitted: list[tuple[uuid.UUID, str]] = []

    count = await dispatch_scheduled_proactive_async(
        runtime(db), lambda u, k: submitted.append((u, k)), now=AT_1305_COPENHAGEN
    )

    mine = {cph, new_york, pending}
    assert [(u, k) for u, k in submitted if u in mine] == [(cph, "meal_check")]
    assert count >= 1
    again: list[tuple[uuid.UUID, str]] = []
    await dispatch_scheduled_proactive_async(runtime(db), lambda u, k: again.append((u, k)), now=AT_1305_COPENHAGEN)
    assert [x for x in again if x[0] in mine] == []  # the next tick does not repeat it


async def test_a_failed_hand_off_releases_the_claim_so_the_next_tick_retries(owner_engine, db):
    user = await tenant_with_schedule(owner_engine)

    def broken(u, k):
        if u == user:
            raise ConnectionError("broker down")

    with pytest.raises(DispatchError, match="broker down"):
        await dispatch_scheduled_proactive_async(runtime(db), broken, now=AT_1305_COPENHAGEN)
    assert await last_fired(owner_engine, user) is None  # released, not lost

    ok: list[tuple[uuid.UUID, str]] = []
    await dispatch_scheduled_proactive_async(runtime(db), lambda u, k: ok.append((u, k)), now=AT_1305_COPENHAGEN)
    assert (user, "meal_check") in ok


# ---------------------------------------------------------------- weekly review timing


async def test_the_weekly_review_is_due_on_the_tenants_weekday_after_the_hour(owner_engine, db):
    user = await new_user(owner_engine, tz="Europe/Copenhagen")
    sunday_18_local = datetime(2026, 1, 18, 17, 0, tzinfo=UTC)  # Sunday 2026-01-18, Copenhagen 18:00
    due = lambda now: schedule.review_due(db, user, now=now, weekday=6, hour=18)  # noqa: E731
    assert await due(sunday_18_local)
    assert await due(sunday_18_local + timedelta(hours=2))  # a missed tick is made up later the same day
    assert not await due(sunday_18_local - timedelta(minutes=1))  # 17:59 local
    assert not await due(sunday_18_local - timedelta(days=1))  # Saturday
    assert not await due(sunday_18_local + timedelta(days=1))  # Monday

    await owner_exec(owner_engine, "UPDATE civ.users SET quiet_hours_start = '17:00', quiet_hours_end = '20:00' WHERE id = :u", u=user)
    assert not await due(sunday_18_local)  # quiet hours

    other = await new_user(owner_engine, tz="Europe/Copenhagen", onboarding_state="in_progress")
    assert not await schedule.review_due(db, other, now=sunday_18_local, weekday=6, hour=18)


async def test_the_review_dispatcher_submits_due_tenants_only(owner_engine, db):
    due, early = await new_user(owner_engine, tz="Europe/Copenhagen"), await new_user(owner_engine, tz="America/New_York")
    submitted: list[uuid.UUID] = []
    await dispatch_weekly_review_async(runtime(db), submitted.append, now=datetime(2026, 1, 18, 17, 0, tzinfo=UTC))
    assert due in submitted and early not in submitted  # New York is still at 12:00

"""v2.0 PR 1.5: proactive check-ins - the bot writes first to a user who has been silent, in tenant-local slots, with the claim in
``civ.proactive_checkin_log`` (idempotent), never in quiet hours, never to someone who never wrote, and not after K unanswered ones."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta

import pytest

from ai_civ.bot import proactive, schedule
from ai_civ.config import Settings
from ai_civ.tasks import DispatchError, dispatch_daily_checkin_async
from tests.helpers import new_message, new_user, owner_exec
from tests.test_proactive_schedule import runtime

SLOTS = schedule.parse_slots("10:30,14:00,17:30,20:00")
GRACE = timedelta(minutes=30)
DAY = datetime(2026, 10, 6, tzinfo=UTC)  # tenants here are in UTC, so local == UTC


def at(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


async def silent_user(owner_engine, *, last_wrote: datetime | None = DAY - timedelta(hours=10)) -> uuid.UUID:
    user = await new_user(owner_engine, tz="UTC")
    if last_wrote is not None:
        await new_message(owner_engine, user, "привет", created_at=last_wrote)
    return user


async def claim(db, user, now, **kw):
    kw.setdefault("min_silence", timedelta(hours=3))
    kw.setdefault("max_unanswered", 2)
    return await schedule.claim_checkin(db, user, now=now, grace=GRACE, slots=SLOTS, **kw)


async def log_rows(owner_engine, user) -> int:
    return (await owner_exec(owner_engine, "SELECT count(*) FROM civ.proactive_checkin_log WHERE user_id = :u", u=user))[0][0]


# ================================================================== pure


def test_slots_are_parsed_sorted_and_a_typo_is_loud():
    assert schedule.parse_slots(" 14:00, 10:30 ,14:00") == [time(10, 30), time(14, 0)]
    for bad in ("", " , ", "25:00", "noon"):
        with pytest.raises(ValueError):
            schedule.parse_slots(bad)


def test_the_slot_window_is_open_at_the_slot_and_closes_after_the_grace():
    assert schedule.current_slot(at(10, 30), SLOTS, GRACE) == time(10, 30)
    assert schedule.current_slot(at(10, 59), SLOTS, GRACE) == time(10, 30)
    assert schedule.current_slot(at(11, 0), SLOTS, GRACE) is None  # a missed slot is skipped, not sent late
    assert schedule.current_slot(at(10, 29), SLOTS, GRACE) is None


def test_check_in_is_a_known_trigger_without_a_time_of_its_own():
    trigger = proactive.get_trigger(proactive.CHECK_IN)
    assert trigger.scheduled is False and trigger.persona_code == ""
    assert all(t.scheduled for key, t in proactive.TRIGGERS.items() if key != proactive.CHECK_IN)  # the reminders screen keeps its two
    s = Settings(_env_file=None)
    assert s.checkin_enabled and s.checkin_max_unanswered == 2 and s.checkin_slots == "" and (s.checkin_min_slots, s.checkin_max_slots) == (3, 4)  # random slots by default


# ================================================================== the claim


async def test_a_silent_user_gets_one_check_in_per_slot(db, owner_engine):
    user = await silent_user(owner_engine)
    first = await claim(db, user, at(10, 35))
    assert first is not None and await log_rows(owner_engine, user) == 1
    assert await claim(db, user, at(10, 40)) is None  # the same slot, claimed already: overlapping ticks and retries cannot fire it twice


async def test_a_release_gives_the_slot_back(db, owner_engine):
    user = await silent_user(owner_engine)
    due = await claim(db, user, at(10, 35))
    await schedule.release_checkin(db, user, due)
    assert await log_rows(owner_engine, user) == 0 and await claim(db, user, at(10, 45)) is not None


async def test_nobody_is_greeted_who_never_wrote_or_who_wrote_recently(db, owner_engine):
    never = await silent_user(owner_engine, last_wrote=None)
    recent = await silent_user(owner_engine, last_wrote=at(9, 0))  # 1.5 h before the slot: not "a while"
    assert await claim(db, never, at(10, 35)) is None and await claim(db, recent, at(10, 35)) is None
    assert await log_rows(owner_engine, never) == 0 and await log_rows(owner_engine, recent) == 0


async def test_only_inside_a_slot_window(db, owner_engine):
    user = await silent_user(owner_engine)
    assert await claim(db, user, at(12, 0)) is None and await claim(db, user, at(11, 5)) is None
    assert await claim(db, user, at(14, 10)) is not None


async def test_never_in_quiet_hours(db, owner_engine):
    user = await silent_user(owner_engine)
    await owner_exec(owner_engine, "UPDATE civ.users SET quiet_hours_start = '10:00', quiet_hours_end = '11:00' WHERE id = :u", u=user)
    assert await claim(db, user, at(10, 35)) is None
    assert await claim(db, user, at(14, 5)) is not None  # outside the window the slot is fine


async def test_only_for_a_tenant_who_is_let_in_and_whose_group_is_active(db, owner_engine):
    pending, frozen = await silent_user(owner_engine), await silent_user(owner_engine)
    await owner_exec(owner_engine, "UPDATE civ.users SET onboarding_state = 'pending' WHERE id = :u", u=pending)
    await owner_exec(owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=frozen)
    assert await claim(db, pending, at(10, 35)) is None and await claim(db, frozen, at(10, 35)) is None


async def test_after_k_unanswered_check_ins_the_bot_waits_until_the_user_writes(db, owner_engine):
    user = await silent_user(owner_engine, last_wrote=DAY - timedelta(hours=14))  # 20:00 the evening before
    for sent, slot in ((at(10, 30), time(10, 30)), (at(14, 0), time(14, 0))):
        await owner_exec(
            owner_engine,
            "INSERT INTO civ.proactive_checkin_log (user_id, slot_date, slot_time, sent_at) VALUES (:u, :d, :t, :s)",
            u=user, d=DAY.date(), t=slot, s=sent,
        )  # fmt: skip
    assert await claim(db, user, at(17, 35)) is None  # two unanswered: enough
    await new_message(owner_engine, user, "я тут", created_at=at(12, 0))
    assert await claim(db, user, at(17, 35)) is not None  # the user wrote at 12:00: only the 14:00 check-in is unanswered now


# ================================================================== the Beat dispatcher


async def test_the_dispatcher_queues_the_check_in_and_a_failed_hand_off_releases_the_claim(db, owner_engine):
    user = await silent_user(owner_engine)
    queued: list[tuple[uuid.UUID, str]] = []

    def broken(u, k):
        if u == user:
            raise ConnectionError("broker down")

    fixed = replace(runtime(db), settings=Settings(_env_file=None, checkin_slots="10:30,14:00,17:30,20:00"))  # the pinned slots of the old behaviour
    with pytest.raises(DispatchError, match="broker down"):
        await dispatch_daily_checkin_async(fixed, broken, now=at(10, 35))
    assert await log_rows(owner_engine, user) == 0  # released: the next tick retries
    await dispatch_daily_checkin_async(fixed, lambda u, k: queued.append((u, k)), now=at(10, 40))
    assert [(u, k) for u, k in queued if u == user] == [(user, "check_in")]
    queued.clear()
    await dispatch_daily_checkin_async(fixed, lambda u, k: queued.append((u, k)), now=at(10, 45))
    assert [item for item in queued if item[0] == user] == []


async def test_the_switch_turns_check_ins_off(db, owner_engine):
    user = await silent_user(owner_engine)
    off = replace(runtime(db), settings=Settings(_env_file=None, checkin_enabled=False, checkin_slots="10:30"))
    queued: list[tuple[uuid.UUID, str]] = []
    await dispatch_daily_checkin_async(off, lambda u, k: queued.append((u, k)), now=at(10, 35))
    assert [item for item in queued if item[0] == user] == [] and await log_rows(owner_engine, user) == 0


# ================================================================== the user's own number of check-ins (users.checkin_per_day, migration 0024)


def test_thin_slots_keeps_n_slots_spread_over_the_day():
    day = [time(10, 0), time(13, 0), time(16, 0), time(19, 0)]
    assert schedule.thin_slots(day, None) == day
    assert schedule.thin_slots(day, 4) == day and schedule.thin_slots(day, 9) == day
    assert schedule.thin_slots(day, 0) == []
    assert schedule.thin_slots(day, 1) == [time(16, 0)]  # the middle one
    assert schedule.thin_slots(day, 2) == [time(10, 0), time(19, 0)]  # the first and the last
    assert schedule.thin_slots(day, 3) == [time(10, 0), time(16, 0), time(19, 0)]
    assert schedule.thin_slots([], 2) == [] and schedule.thin_slots([time(9, 0)], 1) == [time(9, 0)]


async def test_a_user_who_switched_the_checkins_off_is_never_greeted(db, owner_engine):
    user = await silent_user(owner_engine)
    await owner_exec(owner_engine, "UPDATE civ.users SET checkin_per_day = 0 WHERE id = :u", u=user)
    assert await claim(db, user, at(10, 30)) is None and await log_rows(owner_engine, user) == 0


async def test_a_user_who_wants_two_checkins_gets_only_the_first_and_the_last_slot(db, owner_engine):
    user = await silent_user(owner_engine)
    await owner_exec(owner_engine, "UPDATE civ.users SET checkin_per_day = 2 WHERE id = :u", u=user)
    assert await claim(db, user, at(14, 0)) is None  # a middle slot of the four: thinned away
    assert await claim(db, user, at(17, 30)) is None
    assert await claim(db, user, at(10, 30)) is not None
    assert await claim(db, user, at(20, 0), min_silence=timedelta(hours=0), max_unanswered=9) is not None


async def test_the_default_leaves_every_slot_as_it_was(db, owner_engine):
    user = await silent_user(owner_engine)
    assert await claim(db, user, at(14, 0)) is not None  # per_day NULL: nothing is thinned

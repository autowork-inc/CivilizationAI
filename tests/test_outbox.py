"""Outbox worker primitives against a real PostgreSQL: SKIP LOCKED, lease, retry/backoff, dead letter, fencing, RLS."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from ai_civ.db.enums import OutboxOperation, OutboxStatus
from ai_civ.db.models import MemoryOutbox
from ai_civ.memory import outbox
from ai_civ.memory.outbox import ClaimedTask, LeaseLost
from tests.helpers import expire_lease, new_outbox, new_user, outbox_row, owner_exec, tenant_exec

LEASE = 600
BACKOFF = {"backoff_base": 30, "backoff_cap": 3600}


def test_backoff_doubles_and_is_capped():
    assert [outbox.backoff_seconds(n, base=30, cap=3600) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert outbox.backoff_seconds(20, base=30, cap=3600) == 3600
    assert outbox.backoff_seconds(0, base=30, cap=3600) == 30


async def test_claim_sets_lease_and_attempts(owner_engine, db):
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user, payload={"x": 1}, key="k-claim")
    task = await outbox.claim_next(db, user, lease_seconds=LEASE)
    assert task == ClaimedTask(
        id=oid, user_id=user, operation=OutboxOperation.compress_day, payload={"x": 1},
        idempotency_key="k-claim", attempts=1, max_attempts=5,
    )  # fmt: skip
    row = await outbox_row(owner_engine, oid)
    assert row["status"] == "processing" and row["attempts"] == 1 and row["lease_active"] is True


async def test_a_leased_task_is_not_claimed_twice(owner_engine, db):
    user = await new_user(owner_engine)
    await new_outbox(owner_engine, user)
    assert await outbox.claim_next(db, user, lease_seconds=LEASE) is not None
    assert await outbox.claim_next(db, user, lease_seconds=LEASE) is None


async def test_claims_are_fifo(owner_engine, db):
    user = await new_user(owner_engine)
    first = await new_outbox(owner_engine, user)
    second = await new_outbox(owner_engine, user)
    assert (await outbox.claim_next(db, user, lease_seconds=LEASE)).id == first
    assert (await outbox.claim_next(db, user, lease_seconds=LEASE)).id == second


async def test_skip_locked_never_blocks_on_or_double_claims_a_locked_row(owner_engine, db):
    """A concurrent worker that holds a row lock must be skipped, not waited for."""
    user = await new_user(owner_engine)
    first = await new_outbox(owner_engine, user)
    second = await new_outbox(owner_engine, user)
    async with db.tenant_session(user) as holder:
        locked = (
            await holder.execute(
                select(MemoryOutbox.id).order_by(MemoryOutbox.created_at, MemoryOutbox.id).with_for_update().limit(1)
            )
        ).scalar_one()
        assert locked == first  # the holder owns the oldest row's lock and has not committed
        # other workers: they skip the locked row and take the next one; with nothing left they get None immediately
        got = await asyncio.wait_for(outbox.claim_next(db, user, lease_seconds=LEASE), timeout=10)
        assert got is not None and got.id == second
        assert await asyncio.wait_for(outbox.claim_next(db, user, lease_seconds=LEASE), timeout=10) is None


async def test_concurrent_claims_get_distinct_tasks(owner_engine, db):
    user = await new_user(owner_engine)
    ids = {await new_outbox(owner_engine, user) for _ in range(6)}
    claimed = await asyncio.gather(*(outbox.claim_next(db, user, lease_seconds=LEASE) for _ in range(6)))
    got = [t.id for t in claimed if t is not None]
    assert len(got) == len(set(got)) and set(got) <= ids  # no row handed out twice
    assert all(t.attempts == 1 for t in claimed if t is not None)


async def test_expired_lease_makes_a_processing_task_claimable_again(owner_engine, db):
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user)
    assert (await outbox.claim_next(db, user, lease_seconds=LEASE)).attempts == 1
    await expire_lease(owner_engine, oid)  # the worker "crashed"
    again = await outbox.claim_next(db, user, lease_seconds=LEASE)
    assert again is not None and again.id == oid and again.attempts == 2


async def test_task_out_of_attempts_is_never_claimed_and_is_reaped_to_dead(owner_engine, db):
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user, max_attempts=1)
    assert (await outbox.claim_next(db, user, lease_seconds=LEASE)).attempts == 1
    await expire_lease(owner_engine, oid)  # crashed on its last attempt
    assert await outbox.claim_next(db, user, lease_seconds=LEASE) is None
    assert await outbox.reap_exhausted(db, user) == 1
    row = await outbox_row(owner_engine, oid)
    assert row["status"] == "dead" and row["processed_at"] is not None and row["last_error"]
    assert await outbox.reap_exhausted(db, user) == 0


async def test_reaper_leaves_a_task_with_an_active_lease_alone(owner_engine, db):
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user, max_attempts=1)
    await outbox.claim_next(db, user, lease_seconds=LEASE)
    assert await outbox.reap_exhausted(db, user) == 0  # still being worked on
    assert (await outbox_row(owner_engine, oid))["status"] == "processing"


async def test_fail_applies_backoff_through_locked_until(owner_engine, db):
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user)
    task = await outbox.claim_next(db, user, lease_seconds=LEASE)
    assert await outbox.fail(db, task, "boom", **BACKOFF) == OutboxStatus.failed
    row = await outbox_row(owner_engine, oid)
    assert row["status"] == "failed" and row["last_error"] == "boom" and row["lease_active"] is True
    assert await outbox.claim_next(db, user, lease_seconds=LEASE) is None  # waiting out the backoff
    await expire_lease(owner_engine, oid)
    retry = await outbox.claim_next(db, user, lease_seconds=LEASE)
    assert retry is not None and retry.attempts == 2


async def test_failure_on_the_last_attempt_is_dead(owner_engine, db):
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user, max_attempts=1)
    task = await outbox.claim_next(db, user, lease_seconds=LEASE)
    assert await outbox.fail(db, task, "boom", **BACKOFF) == OutboxStatus.dead
    row = await outbox_row(owner_engine, oid)
    assert row["status"] == "dead" and row["processed_at"] is not None


async def test_permanent_failure_is_dead_immediately(owner_engine, db):
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user)
    task = await outbox.claim_next(db, user, lease_seconds=LEASE)
    assert await outbox.fail(db, task, "no handler", permanent=True, **BACKOFF) == OutboxStatus.dead
    assert (await outbox_row(owner_engine, oid))["attempts"] == 1


async def test_stale_worker_is_fenced_out(owner_engine, db):
    """After the lease expired and a second worker re-claimed the task, the first worker can neither finish nor fail it."""
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user)
    stale = await outbox.claim_next(db, user, lease_seconds=LEASE)
    await expire_lease(owner_engine, oid)
    fresh = await outbox.claim_next(db, user, lease_seconds=LEASE)
    assert (stale.attempts, fresh.attempts) == (1, 2)

    with pytest.raises(LeaseLost):
        async with db.tenant_session(user) as session:
            await outbox.mark_done(session, stale)
    assert await outbox.fail(db, stale, "late", **BACKOFF) is None
    row = await outbox_row(owner_engine, oid)
    assert row["status"] == "processing" and row["attempts"] == 2 and row["last_error"] is None

    async with db.tenant_session(user) as session:
        await outbox.mark_done(session, fresh)
    row = await outbox_row(owner_engine, oid)
    assert row["status"] == "done" and row["processed_at"] is not None and row["locked_until"] is None


async def test_lease_lost_rolls_back_everything_written_in_the_same_transaction(owner_engine, db):
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user)
    stale = await outbox.claim_next(db, user, lease_seconds=LEASE)
    await expire_lease(owner_engine, oid)
    await outbox.claim_next(db, user, lease_seconds=LEASE)
    with pytest.raises(LeaseLost):
        async with db.tenant_session(user) as session:
            await outbox.enqueue(
                session, user_id=user, operation=OutboxOperation.compress_day, payload={}, idempotency_key="side-effect"
            )
            await outbox.mark_done(session, stale)
    left = await tenant_exec(db, user, "SELECT count(*) FROM civ.memory_outbox WHERE idempotency_key = 'side-effect'")
    assert left[0][0] == 0


async def test_a_done_task_cannot_be_finished_twice(owner_engine, db):
    user = await new_user(owner_engine)
    await new_outbox(owner_engine, user)
    task = await outbox.claim_next(db, user, lease_seconds=LEASE)
    async with db.tenant_session(user) as session:
        await outbox.mark_done(session, task)
    with pytest.raises(LeaseLost):
        async with db.tenant_session(user) as session:
            await outbox.mark_done(session, task)


async def test_enqueue_is_idempotent(owner_engine, db):
    user = await new_user(owner_engine)
    async with db.tenant_session(user) as session:
        args = {"user_id": user, "operation": OutboxOperation.compress_day, "payload": {"a": 1}, "idempotency_key": "same"}
        assert await outbox.enqueue(session, **args) is True
        assert await outbox.enqueue(session, **args) is False
        count = (await session.execute(select(func.count()).select_from(MemoryOutbox))).scalar_one()
    assert count == 1


async def test_rls_a_tenant_cannot_claim_or_see_anothers_tasks(owner_engine, db):
    user_a, user_b = await new_user(owner_engine), await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user_a)
    assert await outbox.claim_next(db, user_b, lease_seconds=LEASE) is None
    assert await outbox.has_open_work(db, user_b) is False
    assert await outbox.has_open_work(db, user_a) is True
    assert (await outbox_row(owner_engine, oid))["status"] == "pending"
    # and a tenant cannot enqueue on behalf of another one: the RLS WITH CHECK rejects it
    with pytest.raises(Exception, match="row-level security"):
        async with db.tenant_session(user_b) as session:
            await outbox.enqueue(
                session, user_id=user_a, operation=OutboxOperation.compress_day, payload={}, idempotency_key="x"
            )


async def test_has_open_work_ignores_leased_and_finished_tasks(owner_engine, db):
    user = await new_user(owner_engine)
    assert await outbox.has_open_work(db, user) is False
    oid = await new_outbox(owner_engine, user)
    assert await outbox.has_open_work(db, user) is True
    task = await outbox.claim_next(db, user, lease_seconds=LEASE)
    assert await outbox.has_open_work(db, user) is False  # leased: someone is on it
    await expire_lease(owner_engine, oid)
    assert await outbox.has_open_work(db, user) is True  # lease over: needs a worker again
    async with db.tenant_session(user) as session:
        await outbox.mark_done(session, task)
    assert await outbox.has_open_work(db, user) is False


async def test_purge_deletes_only_old_done_rows(owner_engine, db):
    user = await new_user(owner_engine)
    old_done = await new_outbox(owner_engine, user)
    new_done = await new_outbox(owner_engine, user)
    old_dead = await new_outbox(owner_engine, user)
    pending = await new_outbox(owner_engine, user)
    await owner_exec(
        owner_engine,
        "UPDATE civ.memory_outbox SET status='done', processed_at = now() - interval '8 days' WHERE id = :i",
        i=old_done,
    )
    await owner_exec(owner_engine, "UPDATE civ.memory_outbox SET status='done', processed_at = now() WHERE id = :i", i=new_done)
    await owner_exec(
        owner_engine,
        "UPDATE civ.memory_outbox SET status='dead', processed_at = now() - interval '30 days' WHERE id = :i",
        i=old_dead,
    )
    assert await outbox.purge_done(db, user, ttl_days=7) == 1
    left = {r[0] for r in await owner_exec(owner_engine, "SELECT id FROM civ.memory_outbox WHERE user_id = :u", u=user)}
    assert left == {new_done, old_dead, pending}


# ---------------------------------------------------------------- run_tenant_outbox


async def _run(db, user, handlers, **kw):
    return await outbox.run_tenant_outbox(
        db, user, handlers, lease_seconds=LEASE, backoff_base=30, backoff_cap=3600, max_tasks=kw.pop("max_tasks", 10)
    )


async def test_runner_executes_handlers_and_marks_done(owner_engine, db):
    user = await new_user(owner_engine)
    ids = [await new_outbox(owner_engine, user, payload={"n": n}) for n in range(3)]
    seen = []

    async def handler(task: ClaimedTask) -> None:
        seen.append(task.payload["n"])
        async with db.tenant_session(task.user_id) as session:
            await outbox.mark_done(session, task)

    stats = await _run(db, user, {OutboxOperation.compress_day: handler})
    assert seen == [0, 1, 2] and stats.done == 3
    assert [(await outbox_row(owner_engine, i))["status"] for i in ids] == ["done"] * 3


async def test_runner_respects_max_tasks(owner_engine, db):
    user = await new_user(owner_engine)
    for _ in range(3):
        await new_outbox(owner_engine, user)

    async def handler(task: ClaimedTask) -> None:
        async with db.tenant_session(task.user_id) as session:
            await outbox.mark_done(session, task)

    assert (await _run(db, user, {OutboxOperation.compress_day: handler}, max_tasks=2)).done == 2
    assert await outbox.has_open_work(db, user) is True


async def test_runner_fails_fast_records_the_error_and_leaves_the_rest_untouched(owner_engine, db):
    user = await new_user(owner_engine)
    first = await new_outbox(owner_engine, user)
    second = await new_outbox(owner_engine, user)

    async def handler(task: ClaimedTask) -> None:
        raise ConnectionError("gemini is down")

    with pytest.raises(ConnectionError):
        await _run(db, user, {OutboxOperation.compress_day: handler})
    row = await outbox_row(owner_engine, first)
    assert row["status"] == "failed" and "ConnectionError: gemini is down" in row["last_error"]
    untouched = await outbox_row(owner_engine, second)
    assert untouched["status"] == "pending" and untouched["attempts"] == 0  # attempts were not burned


async def test_runner_sends_operations_without_a_handler_straight_to_dead(owner_engine, db):
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user, operation="embed_episode")
    with pytest.raises(outbox.UnknownOperation):
        await _run(db, user, {OutboxOperation.compress_day: None})
    row = await outbox_row(owner_engine, oid)
    assert row["status"] == "dead" and "embed_episode" in row["last_error"]


async def test_runner_survives_a_lost_lease_and_continues(owner_engine, db):
    user = await new_user(owner_engine)
    first = await new_outbox(owner_engine, user)
    second = await new_outbox(owner_engine, user)
    calls = []

    async def handler(task: ClaimedTask) -> None:
        calls.append(task.id)
        if task.id == first:
            raise LeaseLost("someone else owns it")
        async with db.tenant_session(task.user_id) as session:
            await outbox.mark_done(session, task)

    stats = await _run(db, user, {OutboxOperation.compress_day: handler})
    assert calls == [first, second] and stats.done == 1 and stats.lease_lost == 1
    assert (await outbox_row(owner_engine, first))["status"] == "processing"  # not marked failed: it is not ours


async def test_runner_reaps_exhausted_rows_first(owner_engine, db):
    user = await new_user(owner_engine)
    oid = await new_outbox(owner_engine, user, max_attempts=1)
    await outbox.claim_next(db, user, lease_seconds=LEASE)
    await expire_lease(owner_engine, oid)
    stats = await _run(db, user, {})
    assert stats.reaped == 1 and (await outbox_row(owner_engine, oid))["status"] == "dead"

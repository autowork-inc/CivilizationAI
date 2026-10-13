"""Outbox worker primitives for civ.memory_outbox (claim / lease / fencing / retry / dead-letter / TTL purge).

Life of a task (all state transitions are plain SQL under the tenant's RLS; the DB clock - now() - is the only clock):

    pending|failed|processing --claim--> processing (attempts+1, locked_until = now()+lease)
    processing --work (LLM etc., NO db transaction open)--> one transaction: mark_done() + the results
    processing --error--> failed (locked_until = now()+backoff)   or   dead (attempts exhausted / permanent error)
    crashed worker: nothing happens, the lease simply expires and the row is claimed again.

Claim uses ``SELECT ... FOR UPDATE SKIP LOCKED``: concurrent workers never block on or double-claim a row.
Fencing: ``mark_done`` / ``fail`` only act on a row that is still 'processing' with the SAME ``attempts`` the worker
claimed it with. If the lease expired and another worker took the task over, the late worker's transaction raises
``LeaseLost`` and rolls back with everything it wrote - the two can never both commit results for one task.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ai_civ.db import Database
from ai_civ.db.enums import OutboxOperation, OutboxStatus
from ai_civ.db.models import MemoryOutbox

log = logging.getLogger(__name__)

OPEN_STATUSES = (OutboxStatus.pending, OutboxStatus.failed, OutboxStatus.processing)
MAX_ERROR_CHARS = 2000


class LeaseLost(RuntimeError):
    """The task is no longer ours (lease expired and it was re-claimed, or it was finished elsewhere)."""


class UnknownOperation(RuntimeError):
    """An outbox operation without a handler (reserved enum values). Permanent: the row goes straight to 'dead'."""


@dataclass(frozen=True)
class ClaimedTask:
    id: int
    user_id: uuid.UUID
    operation: OutboxOperation
    payload: dict[str, Any]
    idempotency_key: str
    attempts: int
    max_attempts: int


@dataclass
class RunStats:
    done: int = 0
    lease_lost: int = 0
    reaped: int = 0
    errors: list[str] = field(default_factory=list)


Handler = Callable[[ClaimedTask], Awaitable[None]]


def backoff_seconds(attempts: int, *, base: int, cap: int) -> int:
    """Delay before the next try after the ``attempts``-th failed one: base, 2*base, 4*base ... capped."""
    return min(cap, base * 2 ** max(attempts - 1, 0))


# ---------------------------------------------------------------- producer


async def enqueue(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    operation: OutboxOperation,
    payload: dict[str, Any],
    idempotency_key: str,
) -> bool:
    """Insert a task unless one with this (user_id, idempotency_key) exists. Returns True if a row was created."""
    stmt = (
        pg_insert(MemoryOutbox)
        .values(user_id=user_id, operation=operation, payload=payload, idempotency_key=idempotency_key)
        .on_conflict_do_nothing(constraint="uq_memory_outbox_user_idem")
        .returning(MemoryOutbox.id)
    )
    return (await session.execute(stmt)).first() is not None


# ---------------------------------------------------------------- worker side


def _claimable():
    return (
        MemoryOutbox.status.in_(OPEN_STATUSES),
        MemoryOutbox.attempts < MemoryOutbox.max_attempts,
        or_(MemoryOutbox.locked_until.is_(None), MemoryOutbox.locked_until < func.now()),
    )


async def claim_next(db: Database, user_id: uuid.UUID, *, lease_seconds: int) -> ClaimedTask | None:
    """Claim the oldest claimable task of one tenant (own short transaction, committed before any slow work)."""
    async with db.tenant_session(user_id) as session:
        picked = (
            await session.execute(
                select(MemoryOutbox.id)
                .where(*_claimable())
                .order_by(MemoryOutbox.created_at, MemoryOutbox.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
        ).first()
        if picked is None:
            return None
        row = (
            await session.execute(
                update(MemoryOutbox)
                .where(MemoryOutbox.id == picked[0])
                .values(
                    status=OutboxStatus.processing,
                    attempts=MemoryOutbox.attempts + 1,
                    locked_until=func.now() + timedelta(seconds=lease_seconds),
                )
                .returning(
                    MemoryOutbox.id,
                    MemoryOutbox.user_id,
                    MemoryOutbox.operation,
                    MemoryOutbox.payload,
                    MemoryOutbox.idempotency_key,
                    MemoryOutbox.attempts,
                    MemoryOutbox.max_attempts,
                )
            )
        ).one()
    return ClaimedTask(
        id=row.id,
        user_id=row.user_id,
        operation=OutboxOperation(row.operation),
        payload=dict(row.payload),
        idempotency_key=row.idempotency_key,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
    )


async def mark_done(session: AsyncSession, task: ClaimedTask) -> None:
    """Finish the task INSIDE the transaction that writes its results (so both commit or neither does).

    Locks the row and verifies we still own it; raises LeaseLost otherwise (the caller's transaction then rolls back).
    """
    owned = (
        await session.execute(
            select(MemoryOutbox.id)
            .where(
                MemoryOutbox.id == task.id,
                MemoryOutbox.status == OutboxStatus.processing,
                MemoryOutbox.attempts == task.attempts,
            )
            .with_for_update()
        )
    ).first()
    if owned is None:
        raise LeaseLost(f"outbox task {task.id} is no longer owned by this worker")
    await session.execute(
        update(MemoryOutbox)
        .where(MemoryOutbox.id == task.id)
        .values(status=OutboxStatus.done, processed_at=func.now(), locked_until=None, last_error=None)
    )


async def fail(
    db: Database,
    task: ClaimedTask,
    error: str,
    *,
    permanent: bool = False,
    backoff_base: int,
    backoff_cap: int,
) -> OutboxStatus | None:
    """Record a failure. 'failed' + backoff via locked_until, or 'dead' when out of attempts / permanent.

    Returns the new status, or None if the task is no longer ours (nothing is changed then).
    """
    dead = permanent or task.attempts >= task.max_attempts
    values: dict[str, Any] = {"last_error": error[:MAX_ERROR_CHARS]}
    if dead:
        values.update(status=OutboxStatus.dead, processed_at=func.now(), locked_until=None)
    else:
        delay = backoff_seconds(task.attempts, base=backoff_base, cap=backoff_cap)
        values.update(status=OutboxStatus.failed, locked_until=func.now() + timedelta(seconds=delay))
    async with db.tenant_session(task.user_id) as session:
        result = await session.execute(
            update(MemoryOutbox)
            .where(
                MemoryOutbox.id == task.id,
                MemoryOutbox.status == OutboxStatus.processing,
                MemoryOutbox.attempts == task.attempts,
            )
            .values(**values)
            .returning(MemoryOutbox.status)
        )
        row = result.first()
    return None if row is None else OutboxStatus(row[0])


async def reap_exhausted(db: Database, user_id: uuid.UUID) -> int:
    """Move tasks that ran out of attempts (and whose lease is over) to 'dead' for manual review.

    claim_next never picks them up, so without this step a worker that crashed on the last attempt would leave the row
    in 'processing' forever.
    """
    async with db.tenant_session(user_id) as session:
        result = await session.execute(
            update(MemoryOutbox)
            .where(
                MemoryOutbox.status.in_(OPEN_STATUSES),
                MemoryOutbox.attempts >= MemoryOutbox.max_attempts,
                or_(MemoryOutbox.locked_until.is_(None), MemoryOutbox.locked_until < func.now()),
            )
            .values(
                status=OutboxStatus.dead,
                processed_at=func.now(),
                locked_until=None,
                last_error=func.coalesce(MemoryOutbox.last_error, "worker crashed or lease expired on the last attempt"),
            )
        )
        return result.rowcount or 0


async def has_open_work(db: Database, user_id: uuid.UUID) -> bool:
    """Cheap check used by the drain dispatcher: is there anything to claim or to reap for this tenant?"""
    async with db.tenant_session(user_id) as session:
        row = (
            await session.execute(
                select(MemoryOutbox.id)
                .where(
                    MemoryOutbox.status.in_(OPEN_STATUSES),
                    or_(MemoryOutbox.locked_until.is_(None), MemoryOutbox.locked_until < func.now()),
                )
                .limit(1)
            )
        ).first()
    return row is not None


async def purge_done(db: Database, user_id: uuid.UUID, *, ttl_days: int) -> int:
    """Delete 'done' rows older than the TTL (7 days by default). Never touches any other status."""
    async with db.tenant_session(user_id) as session:
        result = await session.execute(
            delete(MemoryOutbox).where(
                MemoryOutbox.status == OutboxStatus.done,
                MemoryOutbox.processed_at < func.now() - timedelta(days=ttl_days),
            )
        )
        return result.rowcount or 0


async def run_tenant_outbox(
    db: Database,
    user_id: uuid.UUID,
    handlers: Mapping[OutboxOperation, Handler],
    *,
    lease_seconds: int,
    backoff_base: int,
    backoff_cap: int,
    max_tasks: int,
) -> RunStats:
    """Drain up to ``max_tasks`` tasks of ONE tenant.

    On the first handler error the failure is persisted (backoff / dead) and the error is re-raised: the process
    fails fast instead of burning the attempts of every queued task against a dead dependency. The remaining tasks
    are picked up by the next drain run.
    """
    stats = RunStats(reaped=await reap_exhausted(db, user_id))
    for _ in range(max_tasks):
        task = await claim_next(db, user_id, lease_seconds=lease_seconds)
        if task is None:
            break
        handler = handlers.get(task.operation)
        try:
            if handler is None:
                raise UnknownOperation(f"no handler for operation {task.operation.value!r}")
            await handler(task)
        except LeaseLost:
            log.warning("outbox task %s: lease lost, another worker owns it now", task.id)
            stats.lease_lost += 1
            continue
        except Exception as exc:
            status = await fail(
                db,
                task,
                f"{type(exc).__name__}: {exc}",
                permanent=isinstance(exc, UnknownOperation),
                backoff_base=backoff_base,
                backoff_cap=backoff_cap,
            )
            log.error("outbox task %s failed (%s -> %s): %r", task.id, task.operation.value, status, exc)
            raise
        stats.done += 1
    return stats

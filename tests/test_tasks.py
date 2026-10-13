"""Celery layer: configuration, Beat schedule, dispatchers, task entry points, tenant enumeration, Redis reachability."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from ai_civ import runtime, tasks
from ai_civ.celery_app import app
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.models.memory import EMBEDDING_DIM
from ai_civ.memory import gc
from ai_civ.runtime import Runtime
from tests.fakes import FakeEmbedder, FakeLLM
from tests.helpers import APP_URL, OWNER_URL, new_message, new_outbox, new_user, outbox_row, owner_exec

YESTERDAY_NOON = (datetime.now(UTC) - timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)


def settings(**kw) -> Settings:
    return Settings(_env_file=None, gc_local_hour=0, **kw)  # hour 0: "due" at any time of the day


def seed(fn):
    """Run an async seeding / inspection function against the OWNER engine from a synchronous test."""

    async def go():
        engine = create_async_engine(OWNER_URL, poolclass=NullPool)
        try:
            return await fn(engine)
        finally:
            await engine.dispose()

    return asyncio.run(go())


# ---------------------------------------------------------------- configuration


def test_settings_defaults_match_the_approved_stack():
    s = Settings(_env_file=None)
    assert s.celery_broker_url == "redis://127.0.0.1:6379/0"
    assert (s.gemini_judge_model, s.gemini_gc_model) == ("gemini-3.5-flash-lite", "gemini-3.8-flash")
    assert s.gemini_embedding_model == "gemini-embedding-001"
    assert s.embedding_dim == EMBEDDING_DIM == 1536  # the vector(1536) column and the model output must agree
    assert s.gemini_api_key is None  # secrets are never defaulted


def test_time_limits_are_ordered_soft_hard_lease():
    s = Settings(_env_file=None)
    assert s.celery_task_soft_time_limit < s.celery_task_time_limit < s.outbox_lease_seconds


def test_celery_is_configured_for_fail_fast_at_least_once_delivery():
    conf = app.conf
    assert conf.broker_url == Settings().celery_broker_url
    assert conf.task_acks_late is True and conf.task_reject_on_worker_lost is True
    assert conf.worker_prefetch_multiplier == 1 and conf.task_ignore_result is True
    assert conf.accept_content == ["json"] and conf.task_serializer == "json"
    assert conf.broker_connection_max_retries is not None  # no endless reconnect loop
    assert conf.task_soft_time_limit < conf.task_time_limit


def test_every_beat_entry_points_at_a_registered_task():
    schedule = app.conf.beat_schedule
    assert set(schedule) == {
        "gc-dispatch-hourly", "outbox-drain", "outbox-purge-done", "proactive-scheduled", "review-dispatch-hourly",
        "notifications-dispatch", "social-purge", "events-purge", "cards-dispatch", "inventory-dispatch", "capsules-dispatch",
        "daily-checkin", "xp-decay", "mood-dispatch",
    }  # fmt: skip
    for entry in schedule.values():
        assert entry["task"] in app.tasks
    assert schedule["events-purge"]["task"] == "ai_civ.purge_events"  # quick-event drafts and invitations: expire, then delete
    assert {"ai_civ.process_tenant_outbox", "ai_civ.dispatch_nightly_gc", "ai_civ.drain_outbox",
            "ai_civ.purge_done_outbox"} <= set(app.tasks)  # fmt: skip


def test_dispatchers_retry_boundedly_and_the_worker_task_does_not_autoretry():
    for task in (tasks.dispatch_nightly_gc, tasks.drain_outbox, tasks.purge_done_outbox):
        assert task.max_retries == 3 and Exception in task.autoretry_for
    assert not getattr(tasks.process_tenant_outbox, "autoretry_for", ())  # the outbox row is the retry state


def test_redis_broker_is_reachable_and_round_trips_a_message():
    """Real Redis (127.0.0.1:6379): publish + consume one message through kombu on a throw-away queue."""
    name = f"ai_civ_test_{uuid.uuid4().hex}"
    try:
        with app.connection_for_write() as conn:
            conn.ensure_connection(max_retries=1, timeout=3)
            queue = conn.SimpleQueue(name)
            try:
                queue.put({"ping": "pong"})
                message = queue.get(block=True, timeout=5)
                message.ack()
                assert message.payload == {"ping": "pong"}
            finally:
                queue.clear()
                queue.queue.delete()  # also drops kombu's binding key: leave nothing behind in the developer's Redis
                queue.close()
    except OSError as exc:
        pytest.skip(f"Redis is not reachable at {app.conf.broker_url}: {exc!r}")
    except Exception as exc:  # kombu wraps connection errors in its own types
        if "connect" in repr(exc).lower() or "refused" in repr(exc).lower():
            pytest.skip(f"Redis is not reachable at {app.conf.broker_url}: {exc!r}")
        raise


# ---------------------------------------------------------------- civ.list_tenants (migration 0002)


async def test_list_tenants_returns_every_tenant_with_its_timezone(owner_engine, db):
    a = await new_user(owner_engine, tz="Europe/Copenhagen")
    b = await new_user(owner_engine, tz="America/New_York")
    tenants = {t.id: t.timezone for t in await db.list_tenants()}
    assert tenants[a] == "Europe/Copenhagen" and tenants[b] == "America/New_York"


async def test_list_tenants_is_the_only_cross_tenant_door_and_exposes_nothing_else(owner_engine, db):
    from sqlalchemy import text

    await new_user(owner_engine)
    async with db._sessionmaker() as session:
        async with session.begin():
            columns = (await session.execute(text("SELECT * FROM civ.list_tenants() LIMIT 1"))).keys()
    assert list(columns) == ["tenant_id", "tenant_timezone"]  # no Telegram ids, no chat ids
    # without a tenant context the table itself stays closed (fail-fast), only the function opens the door
    with pytest.raises(Exception, match="app.user_id"):
        async with db._sessionmaker() as session:
            async with session.begin():
                await session.execute(text("SELECT count(*) FROM civ.users"))


async def test_list_tenants_is_executable_only_by_the_app_role_and_the_owner(owner_engine):
    rows = await owner_exec(
        owner_engine,
        "SELECT DISTINCT a.grantee::regrole::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace, "
        "LATERAL aclexplode(p.proacl) a WHERE n.nspname = 'civ' AND p.proname = 'list_tenants'",
    )
    grantees = {r[0] for r in rows}
    assert "ai_civ_app" in grantees and "-" not in grantees  # '-' would be PUBLIC


# ---------------------------------------------------------------- async dispatchers (called directly)


def make_runtime(db, **kw) -> Runtime:
    return Runtime(db=db, settings=settings(**kw), llm=FakeLLM(lambda _: {}), embedder=FakeEmbedder())


async def test_dispatch_nightly_gc_enqueues_and_submits_only_tenants_with_new_work(owner_engine, db):
    busy, quiet = await new_user(owner_engine), await new_user(owner_engine)
    await new_message(owner_engine, busy, "hello", created_at=YESTERDAY_NOON)
    submitted: list[uuid.UUID] = []
    now = datetime.now(UTC)

    await tasks.dispatch_nightly_gc_async(make_runtime(db), submitted.append, now=now)
    assert busy in submitted and quiet not in submitted
    assert (await outbox_row(owner_engine, (await owner_exec(
        owner_engine, "SELECT id FROM civ.memory_outbox WHERE user_id = :u", u=busy))[0][0]))["status"] == "pending"  # fmt: skip

    submitted.clear()  # idempotent: a second hourly tick creates nothing and submits nothing for this tenant
    await tasks.dispatch_nightly_gc_async(make_runtime(db), submitted.append, now=now)
    assert busy not in submitted


async def test_dispatch_isolates_a_failing_tenant_but_reports_it(owner_engine, db, monkeypatch):
    bad, good = await new_user(owner_engine), await new_user(owner_engine)
    for user in (bad, good):
        await new_message(owner_engine, user, "hello", created_at=YESTERDAY_NOON)
    real = gc.enqueue_day_compression

    async def flaky(db_, tenant, **kw):
        if tenant.id == bad:
            raise RuntimeError("broken tenant")
        return await real(db_, tenant, **kw)

    monkeypatch.setattr(gc, "enqueue_day_compression", flaky)
    submitted: list[uuid.UUID] = []
    with pytest.raises(tasks.DispatchError, match=str(bad)):
        await tasks.dispatch_nightly_gc_async(make_runtime(db), submitted.append, now=datetime.now(UTC))
    assert good in submitted and bad not in submitted  # the healthy tenant was still served


async def test_drain_submits_exactly_the_tenants_with_open_work(owner_engine, db):
    with_work, without = await new_user(owner_engine), await new_user(owner_engine)
    await new_outbox(owner_engine, with_work)
    submitted: list[uuid.UUID] = []
    await tasks.drain_outbox_async(make_runtime(db), submitted.append)
    assert with_work in submitted and without not in submitted


async def test_purge_removes_old_done_rows_of_every_tenant(owner_engine, db):
    users = [await new_user(owner_engine) for _ in range(2)]
    old = [await new_outbox(owner_engine, u) for u in users]
    for oid in old:
        await owner_exec(
            owner_engine,
            "UPDATE civ.memory_outbox SET status='done', processed_at = now() - interval '9 days' WHERE id = :i",
            i=oid,
        )
    assert await tasks.purge_done_outbox_async(make_runtime(db)) >= 2
    left = await owner_exec(owner_engine, "SELECT id FROM civ.memory_outbox WHERE id = ANY(:ids)", ids=old)
    assert left == []


# ---------------------------------------------------------------- Celery entry points (eager, real DB, fake Gemini)


@pytest.fixture
def fake_runtime():
    """Replace the runtime factory: real database as ai_civ_app, fake Gemini. Built inside the task's own event loop."""
    holder: dict = {"llm": FakeLLM(lambda _: {"summary": "s", "importance": 4, "facts": [], "emotions": []})}

    @asynccontextmanager
    async def factory(with_llm: bool):
        db = Database(create_async_engine(APP_URL, poolclass=NullPool))
        try:
            await db.verify_rls_enforced()
            yield Runtime(
                db=db,
                settings=settings(),
                llm=holder["llm"] if with_llm else None,
                embedder=FakeEmbedder() if with_llm else None,
            )
        finally:
            await db.dispose()

    runtime.set_runtime_factory(factory)
    yield holder
    runtime.set_runtime_factory(None)


def test_process_tenant_outbox_task_runs_compress_day_and_reports_stats(migrated, fake_runtime):
    async def prepare(engine):
        user = await new_user(engine)
        await new_message(engine, user, "I really love black coffee", created_at=datetime(2026, 6, 14, 12, tzinfo=UTC))
        payload = {
            "persona_id": None, "day": "2026-06-14",
            "start": "2026-06-14T00:00:00+00:00", "end": "2026-06-15T00:00:00+00:00",
        }  # fmt: skip
        return user, await new_outbox(engine, user, payload=payload)

    user, oid = seed(prepare)
    result = tasks.process_tenant_outbox.apply(args=[str(user)])
    assert result.state == "SUCCESS" and result.get() == {"done": 1, "lease_lost": 0, "reaped": 0}
    assert seed(lambda e: outbox_row(e, oid))["status"] == "done"
    episodes = seed(lambda e: owner_exec(e, "SELECT content FROM civ.episodic_memories WHERE user_id = :u", u=user))
    assert episodes == [("s",)]
    assert len(fake_runtime["llm"].calls) == 1 and fake_runtime["llm"].calls[0]["model"] == "gemini-3.8-flash"


def test_process_tenant_outbox_task_fails_fast_and_leaves_a_retry_in_the_outbox(migrated, fake_runtime):
    from ai_civ.memory.llm import LLMError

    fake_runtime["llm"] = FakeLLM(lambda _: LLMError("Gemini is down"))

    async def prepare(engine):
        user = await new_user(engine)
        await new_message(engine, user, "hello", created_at=datetime(2026, 6, 14, 12, tzinfo=UTC))
        payload = {
            "persona_id": None, "day": "2026-06-14",
            "start": "2026-06-14T00:00:00+00:00", "end": "2026-06-15T00:00:00+00:00",
        }  # fmt: skip
        return user, await new_outbox(engine, user, payload=payload)

    user, oid = seed(prepare)
    result = tasks.process_tenant_outbox.apply(args=[str(user)])
    assert result.state == "FAILURE" and isinstance(result.result, LLMError)  # the process failed, it did not hang or loop
    row = seed(lambda e: outbox_row(e, oid))
    assert row["status"] == "failed" and row["attempts"] == 1 and "Gemini is down" in row["last_error"]
    assert row["lease_active"] is True  # backoff in force; the next drain run retries after it


def test_dispatch_and_drain_tasks_hand_work_to_the_worker_task(migrated, fake_runtime, monkeypatch):
    submitted: list[uuid.UUID] = []
    monkeypatch.setattr(tasks, "_submit", submitted.append)  # no real queue publish from this test

    async def prepare(engine):
        user = await new_user(engine)
        await new_message(engine, user, "hello", created_at=YESTERDAY_NOON)
        return user

    user = seed(prepare)
    assert tasks.dispatch_nightly_gc.apply().state == "SUCCESS"
    assert user in submitted
    submitted.clear()
    assert tasks.drain_outbox.apply().state == "SUCCESS"
    assert user in submitted  # its freshly created compress_day row is still open


def test_purge_task_runs(migrated, fake_runtime):
    assert tasks.purge_done_outbox.apply().state == "SUCCESS"


def test_submit_publishes_to_the_worker_task(monkeypatch):
    sent = []
    monkeypatch.setattr(tasks.process_tenant_outbox, "delay", lambda *a, **kw: sent.append((a, kw)))
    user = uuid.uuid4()
    tasks._submit(user)
    assert sent == [((str(user),), {})]  # only the tenant id travels through the broker, never any content


def test_a_worker_runtime_refuses_to_start_as_a_role_that_bypasses_rls(migrated, monkeypatch):
    """default_runtime() calls verify_rls_enforced(): pointing DATABASE_URL at the owner must fail before any work."""
    monkeypatch.setenv("DATABASE_URL", OWNER_URL)
    from ai_civ import config

    config.get_settings.cache_clear()
    try:
        async def go():
            async with runtime.default_runtime(False):
                pass

        with pytest.raises(Exception, match="Row Level Security is NOT enforced"):
            asyncio.run(go())
    finally:
        config.get_settings.cache_clear()


def test_the_runtime_without_llm_refuses_llm_use():
    rt = Runtime(db=None, settings=settings())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="without an LLM"):
        rt.require_llm()

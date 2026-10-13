"""Celery tasks. Thin synchronous wrappers around the ``*_async`` functions, which hold all the logic (and are what the
tests call directly).

Failure model (CLAUDE.md "Fail-Fast & Retry"): no loops around network calls. A failing LLM/DB call raises; the outbox
row records the failure with a backoff, and the next drain run (or Celery's own redelivery) retries. Dispatcher tasks
are stateless and use bounded Celery autoretry.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from functools import partial
from zoneinfo import ZoneInfo

from aiogram import Bot

from ai_civ import runtime
from ai_civ.bot import decay, mood, reminders, schedule
from ai_civ.bot.app import open_bot
from ai_civ.bot.notifications import Notifier
from ai_civ.bot.orchestrator import Orchestrator, load_tenant
from ai_civ.bot.proactive import CHECK_IN, UnknownTrigger, get_trigger
from ai_civ.bot import provision as provision_module
from ai_civ.bot.userbot import TelethonApi
from ai_civ.bot.reminders import ReminderStore
from ai_civ.bot.review import ReviewService
from ai_civ.bot.tools.capsule import TimeCapsules
from ai_civ.bot.tools.cards import Cards
from ai_civ.bot.tools.inventory import Inventory
from ai_civ.celery_app import app
from ai_civ.config import get_settings
from ai_civ.db.enums import OutboxOperation
from ai_civ.db.tenancy import EventPurgeStats, PurgeStats
from ai_civ.memory import gc, outbox
from ai_civ.runtime import Runtime

log = logging.getLogger(__name__)

Submit = Callable[[uuid.UUID], None]
SubmitProactive = Callable[[uuid.UUID, str], None]

DISPATCHER_RETRY = {"autoretry_for": (Exception,), "retry_backoff": 30, "retry_backoff_max": 300, "max_retries": 3}
# A typo in a trigger key cannot be fixed by retrying.
PROACTIVE_RETRY = {**DISPATCHER_RETRY, "dont_autoretry_for": (UnknownTrigger,)}


class DispatchError(RuntimeError):
    """Some tenants failed during a dispatch run (the others were processed)."""


# ---------------------------------------------------------------- async implementations


async def process_tenant_outbox_async(rt: Runtime, user_id: uuid.UUID) -> outbox.RunStats:
    llm, embedder = rt.require_llm()
    ctx = gc.GcContext(db=rt.db, llm=llm, embedder=embedder, settings=rt.settings)
    s = rt.settings
    return await outbox.run_tenant_outbox(
        rt.db,
        user_id,
        {OutboxOperation.compress_day: partial(gc.compress_day, ctx)},
        lease_seconds=s.outbox_lease_seconds,
        backoff_base=s.outbox_backoff_base_seconds,
        backoff_cap=s.outbox_backoff_max_seconds,
        max_tasks=s.outbox_batch_per_run,
    )


async def dispatch_nightly_gc_async(rt: Runtime, submit: Submit, *, now: datetime | None = None) -> int:
    """Create compress_day rows for every tenant that is due, then ask a worker to drain those tenants."""
    now = now or datetime.now(UTC)
    created_total, failures = 0, []
    for tenant in await rt.db.list_tenants():
        try:
            created = await gc.enqueue_day_compression(rt.db, tenant, now=now, local_hour=rt.settings.gc_local_hour)
        except Exception as exc:  # one broken tenant (e.g. unknown time zone) must not block the others
            log.exception("gc dispatch failed for tenant %s", tenant.id)
            failures.append(f"{tenant.id}: {exc!r}")
            continue
        if created:
            created_total += created
            submit(tenant.id)
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return created_total


async def drain_outbox_async(rt: Runtime, submit: Submit) -> int:
    """Submit a worker task for every tenant that has claimable / reapable outbox rows."""
    submitted, failures = 0, []
    for tenant in await rt.db.list_tenants():
        try:
            if await outbox.has_open_work(rt.db, tenant.id):
                submit(tenant.id)
                submitted += 1
        except Exception as exc:
            log.exception("outbox drain check failed for tenant %s", tenant.id)
            failures.append(f"{tenant.id}: {exc!r}")
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return submitted


async def purge_done_outbox_async(rt: Runtime) -> int:
    deleted, failures = 0, []
    for tenant in await rt.db.list_tenants():
        try:
            deleted += await outbox.purge_done(rt.db, tenant.id, ttl_days=rt.settings.outbox_done_ttl_days)
        except Exception as exc:
            log.exception("outbox purge failed for tenant %s", tenant.id)
            failures.append(f"{tenant.id}: {exc!r}")
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return deleted


# ---- proactivity (Module 2): the bot writes first. The hook = trigger key (ai_civ.bot.proactive.TRIGGERS) + a tenant.


async def send_proactive_async(rt: Runtime, bot: Bot, user_id: uuid.UUID, trigger_key: str) -> bool:
    """Generate (``is_proactive=True``) and push one proactive message into the trigger's topic. False = nothing to do."""
    trigger = get_trigger(trigger_key)
    tenant = await load_tenant(rt.db, user_id)
    if tenant is None or tenant.forum_chat_id is None:
        log.warning("proactive %s skipped: tenant %s is unknown or has no forum group", trigger_key, user_id)
        return False
    router_llm, chat_llm = rt.require_chat()
    orchestrator = Orchestrator(db=rt.db, router_llm=router_llm, chat_llm=chat_llm, settings=rt.settings, bot=bot)
    return await orchestrator.run_proactive(tenant, trigger)


async def dispatch_proactive_async(
    rt: Runtime,
    submit: SubmitProactive,
    trigger_key: str,
    *,
    local_hour: int | None = None,
    now: datetime | None = None,
) -> int:
    """Submit ``send_proactive`` for every tenant (or, with ``local_hour``, for those whose local clock shows that hour).

    Beat fires this once per period, e.g. hourly at minute 0 with ``local_hour=13`` for a lunch question at 13:xx in each
    tenant's own time zone. NOT idempotent by design (a nudge has no natural key), hence no autoretry on the task.
    """
    get_trigger(trigger_key)  # a typo fails here, once, instead of N times in the workers
    now = now or datetime.now(UTC)
    submitted, failures = 0, []
    for tenant in await rt.db.list_tenants():
        try:
            if local_hour is not None and now.astimezone(ZoneInfo(tenant.timezone)).hour != local_hour:
                continue
        except Exception as exc:  # one broken time zone must not block the others
            log.exception("proactive dispatch failed for tenant %s", tenant.id)
            failures.append(f"{tenant.id}: {exc!r}")
            continue
        submit(tenant.id, trigger_key)
        submitted += 1
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return submitted


async def dispatch_scheduled_proactive_async(
    rt: Runtime, submit: SubmitProactive, *, now: datetime | None = None
) -> int:
    """Beat tick (every few minutes): for every tenant, hand the nudges that are due RIGHT NOW to the queue.

    The per-user times come from ``civ.proactive_schedule`` (set by the onboarding interview). Safe to re-run: ``claim_due``
    marks a nudge as fired for the tenant-local day in the same transaction that selects it, so overlapping runs and retries
    never fire it twice; if the hand-off to the queue fails, the claim is released and the next tick tries again.
    """
    now = now or datetime.now(UTC)
    grace = timedelta(minutes=rt.settings.proactive_grace_minutes)
    submitted, failures = 0, []
    for tenant in await rt.db.list_tenants():
        try:
            due = await schedule.claim_due(rt.db, tenant.id, now=now, grace=grace)
        except Exception as exc:  # one broken tenant (e.g. unknown time zone) must not block the others
            log.exception("scheduled proactive dispatch failed for tenant %s", tenant.id)
            failures.append(f"{tenant.id}: {exc!r}")
            continue
        for item in due:
            try:
                submit(tenant.id, item.trigger_key)
                submitted += 1
            except Exception as exc:
                log.exception("could not queue %s for tenant %s; releasing the claim", item.trigger_key, tenant.id)
                failures.append(f"{tenant.id}/{item.trigger_key}: {exc!r}")
                try:
                    await schedule.release(rt.db, tenant.id, item)
                except Exception:
                    log.exception("could not release the claim of %s for tenant %s", item.trigger_key, tenant.id)
    if failures:
        raise DispatchError(f"{len(failures)} failure(s): " + "; ".join(failures))
    return submitted


async def dispatch_daily_checkin_async(rt: Runtime, submit: SubmitProactive, *, now: datetime | None = None) -> int:
    """Beat tick (every 5 minutes): the AUTONOMOUS check-ins (PR 5.4). For every tenant: is the local clock inside one of today's check-in slots? The slots are random but
    fixed per (tenant, local day) and lie in the user's active hours (``schedule.daily_slots``; ``CHECKIN_SLOTS`` pins fixed ones instead). The claim is a row (one per local day and
    slot), so overlapping ticks and retries cannot fire a slot twice; a slot that missed its window is skipped, not sent late. Nothing is sent to a user who has not written
    yet, who wrote less than ``CHECKIN_MIN_SILENCE_HOURS`` ago, or who left ``CHECKIN_MAX_UNANSWERED`` check-ins unanswered. What the message says is decided at send time
    (``Orchestrator.run_proactive``: open tasks, the time of day, the last workout - all rendered by code)."""
    s = rt.settings
    if not s.checkin_enabled:
        return 0
    now = now or datetime.now(UTC)
    grace = timedelta(minutes=s.proactive_grace_minutes)
    fixed = schedule.parse_slots(s.checkin_slots) if s.checkin_slots.strip() else []  # a malformed CHECKIN_SLOTS fails the tick, loudly, before any tenant is touched
    plan = None if fixed else partial(
        schedule.daily_slots, min_slots=s.checkin_min_slots, max_slots=s.checkin_max_slots,
        min_gap=timedelta(minutes=s.checkin_min_gap_minutes), edge=timedelta(minutes=s.checkin_edge_minutes),
    )  # fmt: skip
    submitted, failures = 0, []
    for tenant in await rt.db.list_tenants():
        try:  # the check-in: the bot writes first to a user who has been silent (its own claim, see schedule.claim_checkin)
            checkin = await schedule.claim_checkin(
                rt.db,
                tenant.id,
                now=now,
                grace=grace,
                slots=fixed,
                plan=plan,
                min_silence=timedelta(hours=rt.settings.checkin_min_silence_hours),
                max_unanswered=rt.settings.checkin_max_unanswered,
            )
        except Exception as exc:
            log.exception("check-in dispatch failed for tenant %s", tenant.id)
            failures.append(f"{tenant.id}/check_in: {exc!r}")
            continue
        if checkin is None:
            continue
        try:
            submit(tenant.id, CHECK_IN)
            submitted += 1
        except Exception as exc:
            log.exception("could not queue the check-in for tenant %s; releasing the claim", tenant.id)
            failures.append(f"{tenant.id}/check_in: {exc!r}")
            try:
                await schedule.release_checkin(rt.db, tenant.id, checkin)
            except Exception:
                log.exception("could not release the check-in claim for tenant %s", tenant.id)
    if failures:
        raise DispatchError(f"{len(failures)} failure(s): " + "; ".join(failures))
    return submitted


# ---- weekly review of the quarantine (pending_facts): proposals with "keep / delete" buttons


async def send_review_async(rt: Runtime, bot: Bot, user_id: uuid.UUID) -> int:
    """Send one tenant's weekly review (see ``ai_civ.bot.review``). Returns the number of proposal cards sent."""
    tenant = await load_tenant(rt.db, user_id)
    if tenant is None:
        log.warning("weekly review skipped: tenant %s is unknown", user_id)
        return 0
    return await ReviewService(rt.db, bot, rt.settings).send_weekly(tenant)


async def dispatch_weekly_review_async(
    rt: Runtime, submit: Callable[[uuid.UUID], None], *, now: datetime | None = None
) -> int:
    """Hourly Beat tick: submit ``send_review`` for tenants whose local clock is on the review weekday at/after the review
    hour (``>=`` so a missed tick is made up the same day) and who are not in quiet hours. The task itself is idempotent
    for a week (no new batch while one younger than 6 days exists), so repeated ticks on that day are harmless."""
    now = now or datetime.now(UTC)
    s = rt.settings
    submitted, failures = 0, []
    for tenant in await rt.db.list_tenants():
        try:
            if await schedule.review_due(
                rt.db, tenant.id, now=now, weekday=s.review_local_weekday, hour=s.review_local_hour
            ):
                submit(tenant.id)
                submitted += 1
        except Exception as exc:
            log.exception("weekly review dispatch failed for tenant %s", tenant.id)
            failures.append(f"{tenant.id}: {exc!r}")
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return submitted


# ---------------------------------------------------------------- Celery entry points


@app.task(name="ai_civ.process_tenant_outbox")
def process_tenant_outbox(user_id: str) -> dict[str, int]:
    """Drain one tenant's outbox. No autoretry on purpose: the outbox row IS the retry state (backoff / dead)."""

    async def run() -> outbox.RunStats:
        async with runtime.open_runtime(True) as rt:
            return await process_tenant_outbox_async(rt, uuid.UUID(user_id))

    stats = asyncio.run(run())
    return {"done": stats.done, "lease_lost": stats.lease_lost, "reaped": stats.reaped}


def _submit(user_id: uuid.UUID) -> None:
    process_tenant_outbox.delay(str(user_id))


@app.task(name="ai_civ.dispatch_nightly_gc", **DISPATCHER_RETRY)
def dispatch_nightly_gc() -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await dispatch_nightly_gc_async(rt, _submit)

    return asyncio.run(run())


@app.task(name="ai_civ.drain_outbox", **DISPATCHER_RETRY)
def drain_outbox() -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await drain_outbox_async(rt, _submit)

    return asyncio.run(run())


@app.task(name="ai_civ.purge_done_outbox", **DISPATCHER_RETRY)
def purge_done_outbox() -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await purge_done_outbox_async(rt)

    return asyncio.run(run())


@app.task(name="ai_civ.send_proactive", **PROACTIVE_RETRY)
def send_proactive(user_id: str, trigger: str) -> bool:
    """One proactive message for one tenant. Bounded autoretry (a failed Gemini / Telegram call is retried by Celery);
    at-least-once: if the push succeeded but saving it failed, the retry may send a second message."""

    async def run() -> bool:
        async with runtime.open_runtime(True) as rt, open_bot(rt.settings) as bot:
            return await send_proactive_async(rt, bot, uuid.UUID(user_id), trigger)

    return asyncio.run(run())


def _submit_proactive(user_id: uuid.UUID, trigger: str) -> None:
    send_proactive.delay(str(user_id), trigger)


@app.task(name="ai_civ.dispatch_scheduled_proactive", **DISPATCHER_RETRY)  # retry-safe: claims are atomic per local day
def dispatch_scheduled_proactive() -> int:
    """Beat hook (every 5 minutes): the per-user schedule from ``civ.proactive_schedule``."""

    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await dispatch_scheduled_proactive_async(rt, _submit_proactive)

    return asyncio.run(run())


@app.task(name="ai_civ.daily_proactive_checkin", **DISPATCHER_RETRY)  # retry-safe: every slot is claimed by a row; a failed hand-off gives the claim back
def daily_proactive_checkin() -> int:
    """Beat hook (every 5 minutes): the bot starts a conversation first, 3-4 times a day, at random slots inside the user's active hours."""

    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await dispatch_daily_checkin_async(rt, _submit_proactive)

    return asyncio.run(run())


@app.task(name="ai_civ.send_review", **DISPATCHER_RETRY)
def send_review(user_id: str) -> int:
    """One tenant's weekly quarantine review. At-least-once: a retry after a partial send may repeat some cards (harmless)."""

    async def run() -> int:
        async with runtime.open_runtime(False) as rt, open_bot(rt.settings) as bot:
            return await send_review_async(rt, bot, uuid.UUID(user_id))

    return asyncio.run(run())


def _submit_review(user_id: uuid.UUID) -> None:
    send_review.delay(str(user_id))


@app.task(name="ai_civ.dispatch_weekly_review", **DISPATCHER_RETRY)
def dispatch_weekly_review() -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await dispatch_weekly_review_async(rt, _submit_review)

    return asyncio.run(run())


# ---- notifications addressed to a tenant (notification_outbox, Module 5): "somebody accepted your invite"


async def send_notifications_async(rt: Runtime, bot: Bot, user_id: uuid.UUID) -> int:
    """Deliver one tenant's pending notifications. Raises on the first failed delivery (the row records the backoff)."""
    return await Notifier(rt.db, bot, rt.settings).run_tenant(user_id)


async def dispatch_notifications_async(rt: Runtime, submit: Submit) -> int:
    """Beat tick (every minute): hand every tenant that has something deliverable to a worker. One indexed query for ALL tenants
    (``civ.pending_notification_tenants``) - never a scan of the tenant table."""
    tenants = await rt.db.tenants_with_pending_notifications(limit=rt.settings.notification_dispatch_limit)
    failures = []
    for tenant_id in tenants:
        try:
            submit(tenant_id)
        except Exception as exc:  # the broker hiccuped for one tenant: the next tick offers it again
            log.exception("could not queue notifications of tenant %s", tenant_id)
            failures.append(f"{tenant_id}: {exc!r}")
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return len(tenants)


async def purge_social_async(rt: Runtime) -> PurgeStats:
    s = rt.settings
    return await rt.db.purge_social(
        done_ttl_days=s.notification_done_ttl_days,
        notification_max_age_days=s.notification_max_age_days,
        invite_ttl_days=s.invite_purge_days,
    )


async def purge_events_async(rt: Runtime) -> EventPurgeStats:
    """Open drafts / invitations past their time -> expired; decided ones older than the TTLs are deleted (civ.purge_events)."""
    s = rt.settings
    return await rt.db.purge_events(draft_ttl_days=s.event_draft_purge_days, proposal_ttl_days=s.event_proposal_purge_days)


@app.task(name="ai_civ.send_notifications")
def send_notifications(user_id: str) -> int:
    """Deliver one tenant's notifications. No autoretry on purpose: the outbox row IS the retry state (backoff / dead), and the
    next Beat tick offers the tenant again. At-least-once (see ``ai_civ.bot.notifications``)."""

    async def run() -> int:
        async with runtime.open_runtime(False) as rt, open_bot(rt.settings) as bot:
            return await send_notifications_async(rt, bot, uuid.UUID(user_id))

    return asyncio.run(run())


def _submit_notifications(user_id: uuid.UUID) -> None:
    send_notifications.delay(str(user_id))


@app.task(name="ai_civ.dispatch_notifications", **DISPATCHER_RETRY)  # retry-safe: it only offers work; the claim is in the worker
def dispatch_notifications() -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await dispatch_notifications_async(rt, _submit_notifications)

    return asyncio.run(run())


# ---- spaced repetition (PR 3.2): flashcards that are due by the SM-2 schedule


async def send_cards_async(rt: Runtime, bot: Bot, user_id: uuid.UUID) -> int:
    """Claim and send one tenant's due cards (quiet hours, daily limit and unanswered cap are decided in ``Cards.claim_due``). Raises on the first
    failed send after giving the claim back; the cards already sent stay sent."""
    return await Cards(rt.db, bot, rt.settings).send_due(user_id)


async def dispatch_cards_async(rt: Runtime, submit: Submit) -> int:
    """Beat tick: hand every tenant that has a card to send to a worker. One indexed query for ALL tenants (``civ.cards_due_tenants``)."""
    s = rt.settings
    if not s.sr_enabled:
        return 0
    tenants = await rt.db.tenants_with_due_cards(limit=s.sr_dispatch_limit, resend_hours=s.sr_resend_hours)
    failures = []
    for tenant_id in tenants:
        try:
            submit(tenant_id)
        except Exception as exc:  # the broker hiccuped for one tenant: the next tick offers it again
            log.exception("could not queue cards of tenant %s", tenant_id)
            failures.append(f"{tenant_id}: {exc!r}")
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return len(tenants)


@app.task(name="ai_civ.send_cards", **DISPATCHER_RETRY)  # retry-safe: the claim is a row; a failed send gives it back, a sent card is not re-claimed
def send_cards(user_id: str) -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt, open_bot(rt.settings) as bot:
            return await send_cards_async(rt, bot, uuid.UUID(user_id))

    return asyncio.run(run())


def _submit_cards(user_id: uuid.UUID) -> None:
    send_cards.delay(str(user_id))


@app.task(name="ai_civ.dispatch_cards", **DISPATCHER_RETRY)  # retry-safe: it only offers work
def dispatch_cards() -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await dispatch_cards_async(rt, _submit_cards)

    return asyncio.run(run())


@app.task(name="ai_civ.purge_social", **DISPATCHER_RETRY)
def purge_social() -> dict[str, int]:
    async def run() -> PurgeStats:
        async with runtime.open_runtime(False) as rt:
            return await purge_social_async(rt)

    stats = asyncio.run(run())
    return {"reaped": stats.reaped, "expired": stats.expired, "purged": stats.purged, "invites": stats.invites}


@app.task(name="ai_civ.purge_events", **DISPATCHER_RETRY)  # idempotent: a re-run only finds less to do
def purge_events() -> dict[str, int]:
    async def run() -> EventPurgeStats:
        async with runtime.open_runtime(False) as rt:
            return await purge_events_async(rt)

    stats = asyncio.run(run())
    return {
        "drafts_expired": stats.drafts_expired,
        "drafts_purged": stats.drafts_purged,
        "proposals_expired": stats.proposals_expired,
        "proposals_purged": stats.proposals_purged,
    }


@app.task(name="ai_civ.dispatch_proactive")  # no autoretry: a re-run after a partial fan-out would nudge twice
def dispatch_proactive(trigger: str, local_hour: int | None = None) -> int:
    """Beat hook, e.g. ``{"task": "ai_civ.dispatch_proactive", "schedule": crontab(minute=0), "args": ("meal_check", 13)}``."""

    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await dispatch_proactive_async(rt, _submit_proactive, trigger, local_hour=local_hour)

    return asyncio.run(run())


# ---- smart inventory (PR 4.3): the daily burn of consumables and the "time to buy" reminders


async def check_inventory_burn_async(rt: Runtime, bot: Bot, user_id: uuid.UUID) -> int:
    """One tenant's daily inventory run: burn the days that are due, claim and send the reminders that are due (quiet hours, the local hour, the 3-day
    cooldown and the claim are decided in ``Inventory.claim_due``). Raises on the first failed send after giving the claim back."""
    return await Inventory(rt.db, bot, rt.settings).send_due(user_id)


async def dispatch_inventory_async(rt: Runtime, submit: Submit) -> int:
    """Beat tick: hand every tenant that has something to burn or to remind about to a worker. One indexed query for ALL tenants."""
    s = rt.settings
    if not s.inventory_enabled:
        return 0
    tenants = await rt.db.tenants_with_due_inventory(
        limit=s.inventory_dispatch_limit, local_hour=s.inventory_local_hour, cooldown_days=s.inventory_remind_cooldown_days
    )
    failures = []
    for tenant_id in tenants:
        try:
            submit(tenant_id)
        except Exception as exc:  # the broker hiccuped for one tenant: the next tick offers it again
            log.exception("could not queue the inventory run of tenant %s", tenant_id)
            failures.append(f"{tenant_id}: {exc!r}")
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return len(tenants)


@app.task(name="ai_civ.check_inventory_burn", **DISPATCHER_RETRY)  # retry-safe: the burn is guarded by last_burn_on, the reminder is a claim that a failed send gives back
def check_inventory_burn(user_id: str) -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt, open_bot(rt.settings) as bot:
            return await check_inventory_burn_async(rt, bot, uuid.UUID(user_id))

    return asyncio.run(run())


def _submit_inventory(user_id: uuid.UUID) -> None:
    check_inventory_burn.delay(str(user_id))


@app.task(name="ai_civ.dispatch_inventory", **DISPATCHER_RETRY)  # retry-safe: it only offers work
def dispatch_inventory() -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await dispatch_inventory_async(rt, _submit_inventory)

    return asyncio.run(run())


# ---- the mood of each day (bot/mood.py): a model reads the user's own words of yesterday and today and writes one score per local day


async def score_mood_async(rt: Runtime, user_id: uuid.UUID, timezone: str, *, now: datetime | None = None) -> int:
    """One tenant: judge yesterday and today if their messages changed since the last reading. Returns the number of days written."""
    if rt.llm is None:
        raise RuntimeError("this runtime was opened without an LLM (with_llm=False)")
    return len(await mood.score_tenant(rt.db, rt.llm, rt.settings, user_id, timezone, now=now))


async def dispatch_mood_async(rt: Runtime, submit: Callable[[uuid.UUID, str], None]) -> int:
    """Beat tick (every 3 hours): offer every tenant to a worker. The worker's first step is one cheap count query; the model is called only for a day whose
    messages changed, so a quiet tenant costs nothing."""
    if not rt.settings.mood_enabled:
        return 0
    tenants = await rt.db.list_tenants()
    failures = []
    for tenant in tenants:
        try:
            submit(tenant.id, tenant.timezone)
        except Exception as exc:  # the broker hiccuped for one tenant: the next tick offers it again
            log.exception("could not queue the mood reading of tenant %s", tenant.id)
            failures.append(f"{tenant.id}: {exc!r}")
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return len(tenants)


@app.task(name="ai_civ.score_mood", **DISPATCHER_RETRY)  # retry-safe: the reading is an upsert keyed by (tenant, local day)
def score_mood(user_id: str, timezone: str) -> int:
    async def run() -> int:
        async with runtime.open_runtime(True) as rt:
            return await score_mood_async(rt, uuid.UUID(user_id), timezone)

    return asyncio.run(run())


def _submit_mood(user_id: uuid.UUID, timezone: str) -> None:
    score_mood.delay(str(user_id), timezone)


@app.task(name="ai_civ.dispatch_mood", **DISPATCHER_RETRY)  # retry-safe: it only offers work
def dispatch_mood() -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await dispatch_mood_async(rt, _submit_mood)

    return asyncio.run(run())


# ---- time capsules (PR 4.5): letters to the future self that are due


async def check_time_capsules_async(rt: Runtime, bot: Bot, user_id: uuid.UUID) -> int:
    """Deliver one tenant's due capsules (quiet hours, the lease and the per-run cap are decided in ``TimeCapsules.claim_due``). Raises on the first failed send after
    giving the lease back; the letters already delivered stay delivered."""
    return await TimeCapsules(rt.db, bot, rt.settings).send_due(user_id)


async def dispatch_time_capsules_async(rt: Runtime, submit: Submit) -> int:
    """Beat tick (hourly, :15): hand every tenant that has a capsule to deliver to a worker. One indexed query for ALL tenants."""
    s = rt.settings
    if not s.capsules_enabled:
        return 0
    tenants = await rt.db.tenants_with_due_capsules(limit=s.capsules_dispatch_limit, lease_minutes=s.capsules_lease_minutes)
    failures = []
    for tenant_id in tenants:
        try:
            submit(tenant_id)
        except Exception as exc:  # the broker hiccuped for one tenant: the next tick offers it again
            log.exception("could not queue the capsules of tenant %s", tenant_id)
            failures.append(f"{tenant_id}: {exc!r}")
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return len(tenants)


@app.task(name="ai_civ.check_time_capsules", **DISPATCHER_RETRY)  # retry-safe: the capsule is claimed under a lease; a failed send gives it back
def check_time_capsules(user_id: str) -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt, open_bot(rt.settings) as bot:
            return await check_time_capsules_async(rt, bot, uuid.UUID(user_id))

    return asyncio.run(run())


def _submit_time_capsules(user_id: uuid.UUID) -> None:
    check_time_capsules.delay(str(user_id))


@app.task(name="ai_civ.dispatch_time_capsules", **DISPATCHER_RETRY)  # retry-safe: it only offers work
def dispatch_time_capsules() -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await dispatch_time_capsules_async(rt, _submit_time_capsules)

    return asyncio.run(run())


# ---- decay system (PR 5.3): XP penalties for neglect (no workouts for more than 5 days, tasks overdue by more than 48 hours)


async def decay_tenant_async(rt: Runtime, user_id: uuid.UUID, *, now: datetime | None = None) -> decay.DecayResult:
    """One tenant's night: the penalties are written in one transaction and are idempotent per date / per task (``civ.apply_xp_penalty``). Raises on any failure: Celery retries."""
    return await decay.run_tenant(rt.db, user_id, now=now or datetime.now(UTC), settings=rt.settings)


async def process_xp_decay_async(rt: Runtime, submit: Submit, *, now: datetime | None = None) -> int:
    """Beat tick (hourly): hand every tenant whose LOCAL clock shows ``XP_DECAY_LOCAL_HOUR`` ("at night") to a worker. Each tenant is therefore visited once per local day; a
    repeat (a retried dispatcher, a second tick in the same hour) is harmless, because the penalties themselves are keyed by date / task in the database."""
    s = rt.settings
    if not s.xp_decay_enabled:
        return 0
    now = now or datetime.now(UTC)
    submitted, failures = 0, []
    for tenant in await rt.db.list_tenants():
        try:
            if now.astimezone(ZoneInfo(tenant.timezone)).hour != s.xp_decay_local_hour:
                continue
            submit(tenant.id)
            submitted += 1
        except Exception as exc:  # one broken time zone / one broker hiccup must not block the others
            log.exception("xp decay dispatch failed for tenant %s", tenant.id)
            failures.append(f"{tenant.id}: {exc!r}")
    if failures:
        raise DispatchError(f"{len(failures)} tenant(s) failed: " + "; ".join(failures))
    return submitted


@app.task(name="ai_civ.decay_tenant", **DISPATCHER_RETRY)  # idempotent: the same date / task is never punished twice; a failure rolls the tenant's night back and Celery retries
def decay_tenant(user_id: str) -> dict[str, object]:
    async def run() -> decay.DecayResult:
        async with runtime.open_runtime(False) as rt:
            return await decay_tenant_async(rt, uuid.UUID(user_id))

    result = asyncio.run(run())
    return {"date": result.local_date, "body": result.body, "tasks": result.tasks}


def _submit_decay(user_id: uuid.UUID) -> None:
    decay_tenant.delay(str(user_id))


@app.task(name="ai_civ.process_xp_decay", **DISPATCHER_RETRY)  # retry-safe: it only offers work
def process_xp_decay() -> int:
    async def run() -> int:
        async with runtime.open_runtime(False) as rt:
            return await process_xp_decay_async(rt, _submit_decay)

    return asyncio.run(run())


# ---- scheduled messages (PR 5.4): reminders, alarms and pings that the model's ``schedule_reminder`` queued with a countdown


async def send_scheduled_message_async(
    rt: Runtime, bot: Bot, user_id: uuid.UUID, token: str, attempt: int, *, store: ReminderStore | None = None, enqueue_next: reminders.Enqueue | None = None
) -> str:
    """Deliver one scheduled message (see ``ai_civ.bot.reminders.deliver``): ``sent | duplicate | gone | skipped | inactive``. Raises on a failed send (after giving the claim back)."""
    own = store is None
    store = store or ReminderStore.from_settings(rt.settings)
    try:
        return await reminders.deliver(rt.db, bot, rt.settings, store, user_id, token, attempt, enqueue_next=enqueue_next or _queue_next)
    finally:
        if own:
            await store.aclose()


def _queue_next(args: Sequence[object], countdown: int) -> None:
    send_scheduled_message.apply_async(args=list(args), countdown=countdown)


@app.task(name="ai_civ.send_scheduled_message", **DISPATCHER_RETRY)  # retry-safe: the delivery of an attempt is claimed in Redis (SET NX); a failed send gives the claim back
def send_scheduled_message(user_id: str, token: str, attempt: int = 0) -> str:
    async def run() -> str:
        async with runtime.open_runtime(False) as rt, open_bot(rt.settings) as bot:
            return await send_scheduled_message_async(rt, bot, uuid.UUID(user_id), token, attempt)

    return asyncio.run(run())


# ---- the userbot makes the person's group (bot/provision.py). Its own queue and ONE worker: one Telethon connection at a time.


@app.task(name="ai_civ.provision_group", bind=True, max_retries=2)
def provision_group(self, telegram_user_id: int, display_name: str | None, username: str | None) -> None:
    """Create the private forum group for a person who pressed /start. A FloodWait before the group exists is retried (bounded, Celery); every other
    failure ends in the manual instructions sent to the person (see ``provision.provision_group_async``)."""

    async def run() -> None:
        settings = get_settings()
        async with open_bot(settings) as bot, provision_module.open_store(settings) as store:
            await provision_module.provision_group_async(
                settings, bot, store, TelethonApi.connect, owner_id=telegram_user_id, display_name=display_name, username=username
            )

    try:
        asyncio.run(run())
    except provision_module.RetryLater as exc:
        raise self.retry(countdown=exc.seconds + 5, exc=exc) from exc

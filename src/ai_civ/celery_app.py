"""Celery application: Redis broker, fail-fast / at-least-once delivery, Beat schedule.

Start (from the project root, .env present):
    celery -A ai_civ.celery_app worker -l info -P solo      # Windows: the prefork pool is unsupported -> solo/threads
    celery -A ai_civ.celery_app worker -l info              # Linux / Docker
    celery -A ai_civ.celery_app beat -l info

Delivery semantics: ``acks_late`` + ``reject_on_worker_lost`` = a task whose worker dies is redelivered. That is safe
because the durable state lives in civ.memory_outbox (lease + idempotency keys), not in the Celery message.
"""

from __future__ import annotations

from celery import Celery
from celery.signals import task_failure
from celery.schedules import crontab

from ai_civ.config import get_settings

settings = get_settings()

app = Celery("ai_civ", broker=settings.celery_broker_url, include=["ai_civ.tasks"])

app.conf.update(
    timezone="UTC",
    enable_utc=True,
    task_serializer="json",
    accept_content=["json"],
    task_default_queue="celery",
    # the userbot (a real Telegram account, Telethon) must have ONE connection at a time: its tasks have a queue of their own and a worker with concurrency 1
    task_routes={"ai_civ.provision_group": {"queue": "userbot"}},
    task_ignore_result=True,  # no result backend: outcomes are rows in PostgreSQL
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_soft_time_limit=settings.celery_task_soft_time_limit,
    task_time_limit=settings.celery_task_time_limit,
    # Fail-fast on the broker too: a bounded number of reconnect attempts at worker start, then the process exits.
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=10,
    broker_transport_options={"visibility_timeout": 3600, "socket_timeout": 10, "socket_connect_timeout": 10},
    beat_schedule={
        # hourly: tenants whose local clock has reached GC_LOCAL_HOUR get yesterday's compress_day rows (idempotent)
        "gc-dispatch-hourly": {"task": "ai_civ.dispatch_nightly_gc", "schedule": crontab(minute=5)},
        # every 5 min: pick up leftovers (expired leases, retries after backoff) of every tenant that has open work
        "outbox-drain": {"task": "ai_civ.drain_outbox", "schedule": 300.0},
        # daily: delete 'done' outbox rows older than OUTBOX_DONE_TTL_DAYS
        "outbox-purge-done": {"task": "ai_civ.purge_done_outbox", "schedule": crontab(hour=4, minute=30)},
        # every 5 min: the bot writes first. Per-user times (civ.proactive_schedule, filled by the onboarding interview),
        # quiet hours and "once per local day" are decided by the dispatcher, not here.
        "proactive-scheduled": {"task": "ai_civ.dispatch_scheduled_proactive", "schedule": 300.0},
        # every 5 min: the autonomous check-ins - 3-4 a day at random slots inside each user's active hours (their timezone); the slot claim is a row, so ticks never double a slot
        "daily-checkin": {"task": "ai_civ.daily_proactive_checkin", "schedule": 300.0},
        # hourly: tenants whose local clock is on the review weekday at/after REVIEW_LOCAL_HOUR get this week's quarantine
        # review (buttons keep / delete); the task is idempotent for a week
        "review-dispatch-hourly": {"task": "ai_civ.dispatch_weekly_review", "schedule": crontab(minute=20)},
        # every minute: tenants with a deliverable notification (e.g. "somebody accepted your invite"). One indexed query for ALL
        # tenants, not a scan; the delivery itself is the per-tenant task ai_civ.send_notifications
        "notifications-dispatch": {"task": "ai_civ.dispatch_notifications", "schedule": 60.0},
        # daily: give up on undeliverable notifications, delete delivered ones and ended invites (one SQL statement per kind)
        "social-purge": {"task": "ai_civ.purge_social", "schedule": crontab(hour=4, minute=40)},
        # every 10 min: tenants with a flashcard due for review (SM-2). One indexed query; quiet hours / daily limit are decided per tenant
        "cards-dispatch": {"task": "ai_civ.dispatch_cards", "schedule": 600.0},
        # hourly: tenants whose local clock is past INVENTORY_LOCAL_HOUR and that have a consumable to burn (the daily rate) or to remind about (running low);
        # one indexed query for ALL tenants, the per-tenant work is ai_civ.check_inventory_burn
        "inventory-dispatch": {"task": "ai_civ.dispatch_inventory", "schedule": crontab(minute=35)},
        # hourly at :15: letters to the future self (time capsules) that are due; one indexed query for ALL tenants, never delivered in quiet hours
        # every 3 hours at :40: the mood of yesterday and today is read from the user's own messages (one cheap count per tenant; the model only for a day that changed)
        "mood-dispatch": {"task": "ai_civ.dispatch_mood", "schedule": crontab(minute=40, hour="*/3")},
        "capsules-dispatch": {"task": "ai_civ.dispatch_time_capsules", "schedule": crontab(minute=15)},
        # daily: quick-event drafts and invitations that ran out of time become 'expired'; decided ones older than the TTLs are deleted
        "events-purge": {"task": "ai_civ.purge_events", "schedule": crontab(hour=4, minute=50)},
        # hourly at :25: tenants whose local clock shows XP_DECAY_LOCAL_HOUR (night) get the decay penalties (no workouts for 5+ days, tasks overdue by 48+ hours);
        # so each tenant is visited once per local day, and the penalties are idempotent per date / task in the database
        "xp-decay": {"task": "ai_civ.process_xp_decay", "schedule": crontab(minute=25)},
    },
)


def _report_task_failure(sender=None, task_id=None, exception=None, einfo=None, args=None, **_):
    """A task that has finally failed (after its own retries) is sent to the admin as crash_log_<time>.txt. Imported late: the bot package is heavy."""
    from ai_civ.bot.error_handler import celery_failure

    if exception is None:
        return
    celery_failure(sender, task_id, exception, einfo, args)


task_failure.connect(_report_task_failure, weak=False)

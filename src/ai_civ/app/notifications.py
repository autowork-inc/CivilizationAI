"""Notifications to the mobile app: ``NotificationPublisher`` is what a background task calls INSTEAD of ``bot.send_message`` for a person who has no Telegram.

``OutboxPublisher`` writes the notification into ``notification_outbox`` through ``civ.publish_app_notification`` (the application role may not INSERT there; the
function takes the tenant from the session). Delivery to FCM / APNs is a separate worker behind ``PushSender`` (not part of this layer yet): a Telegram worker never
sees these rows, because it only claims rows of tenants that have a forum group, and an app person has none.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Protocol

from sqlalchemy import text

from ai_civ.db import Database

log = logging.getLogger(__name__)

APP_KINDS = frozenset({"app_push", "app_reminder", "app_checkin", "app_card"})


class NotificationPublisher(Protocol):
    async def publish(self, user_id: uuid.UUID, kind: str, payload: dict[str, Any], *, idempotency_key: str) -> bool:
        """True = queued now, False = the same ``idempotency_key`` was queued before (a retried task adds nothing)."""
        ...


class PushSender(Protocol):
    """The transport behind the outbox for app people (FCM for Android, APNs for iOS). Implemented by the delivery worker, not by this layer."""

    async def send(self, user_id: uuid.UUID, kind: str, payload: dict[str, Any]) -> None: ...


class OutboxPublisher:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def publish(self, user_id: uuid.UUID, kind: str, payload: dict[str, Any], *, idempotency_key: str) -> bool:
        import json

        async with self.db.tenant_session(user_id) as session:
            outcome = (
                await session.execute(
                    text("SELECT civ.publish_app_notification(:kind, CAST(:payload AS jsonb), :key)"),
                    {"kind": kind, "payload": json.dumps(payload, ensure_ascii=False), "key": idempotency_key},
                )
            ).scalar_one()
        return outcome == "queued"

"""Data shapes of the mobile service layer (transport-free)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


class AppChatError(Exception):
    """A request the service refuses for a reason the client can act on. ``code`` is stable (the API maps it to a status), ``message`` is the text for the person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


@dataclass(frozen=True)
class AppIncomingMessage:
    """One message from the app. ``client_message_id`` is made by the app: a retry of the same message carries the same id and never creates a second one.
    ``persona_id`` is the persona (topic) of the database directly; None = the main chat (the concierge)."""

    user_id: uuid.UUID
    persona_id: uuid.UUID | None
    client_message_id: uuid.UUID
    text: str = ""
    voice_bytes: bytes | None = None
    attachment_bytes: bytes | None = None
    attachment_metadata: dict[str, Any] | None = None  # file_name, mime_type, kind ("photo" | "document")
    voice_mime: str | None = None  # defaults to audio/ogg; the app may send audio/mp4 (m4a), audio/mpeg, ...


@dataclass(frozen=True)
class AppReply:
    """The assistant's answer to one app message (also what a repeated request gets back)."""

    message_id: uuid.UUID  # the assistant message
    reply_to_id: uuid.UUID  # the user message it answers
    persona_id: uuid.UUID | None
    text: str
    cards: tuple[dict[str, Any], ...] = ()  # structured cards (``Card.to_json``)
    created_at: datetime | None = None
    replayed: bool = False  # True = the answer already existed: the model was not called
    user_text: str | None = None  # what the user said (the transcript of a voice message)
    third_party: bool = False


@dataclass(frozen=True)
class AppTenant:
    """What the tools and the prompt need to know about the person (the duck-typed twin of the bot's ``TenantInfo``: no Telegram group)."""

    id: uuid.UUID
    timezone: str
    forum_chat_id: int | None = None
    onboarding_state: str = "done"
    group_state: str = "active"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StreamEvent:
    """One Server-Sent Event of a streamed reply: ``token`` {"text"}, ``card`` {"card"}, ``done`` {"user_message_id", "reply_message_id", ...} or ``error`` {"error", "code"}.
    ``error`` is always the LAST event; text that was already sent before it is to be discarded by the client (the message is retried with the same id)."""

    kind: Literal["token", "card", "done", "error"]
    data: dict[str, Any]

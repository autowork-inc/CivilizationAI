"""The mobile app's API: topics, chat history and the streamed reply. ``/api/v1/mobile``, every route behind ``AppAuth`` (Bearer access token, see ``deps_app``).

``POST /chat/stream`` is a Server-Sent Events response driven by ``AppChatService.open_stream``:

    event: token   data: {"text": "..."}                                   (many; the pieces of the reply, in order)
    event: card    data: {"card": {...}}                                   (0..n; what a tool offers with buttons)
    event: done    data: {"user_message_id", "reply_message_id", ...}      (the last event of a successful reply)
    event: error   data: {"error": "<message>", "code": "<code>"}          (the last event of a failed one: discard the text received so far and retry with the SAME client_message_id)

A retry with the same ``client_message_id`` is answered from the database without the model (the whole stored reply arrives at once). Whatever the person can fix before the model is
involved (empty message, unknown topic, refused file) is an ordinary HTTP error, not an event.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select, tuple_
from sse_starlette.sse import EventSourceResponse
from starlette.datastructures import UploadFile

from ai_civ.app.chat_service import AppChatService
from ai_civ.app.dto import AppChatError, AppIncomingMessage
from ai_civ.bot.personas import FEEDBACK_CODE
from ai_civ.config import Settings
from ai_civ.db.models import Message, MessageAttachment, Persona
from ai_civ.web.deps import ApiError
from ai_civ.web.deps_app import AppAuth, AppDB

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/mobile", tags=["mobile"])

TEXT_MAX_CHARS = 20_000

# What a failure the person can act on means in HTTP; everything else of the service is "422: the request itself is wrong".
CHAT_ERROR_STATUS = {"unknown_user": 401, "unknown_persona": 404, "persona_unavailable": 403, "onboarding_required": 403, "voice_too_long": 413}


# ------------------------------------------------------------------ shapes


class PersonaOut(BaseModel):
    id: uuid.UUID
    code: str
    title: str
    mode: str


class AttachmentOut(BaseModel):
    kind: str  # photo | document
    content_kind: str  # image | pdf | txt | docx
    mime_type: str
    file_name: str | None
    size_bytes: int


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str  # user | assistant
    text: str
    created_at: datetime
    persona_id: uuid.UUID | None
    client_message_id: uuid.UUID | None  # of a message sent by this app
    reply_to_id: uuid.UUID | None  # of an assistant message: the user message it answers
    cards: list[dict[str, Any]]
    attachment: AttachmentOut | None


class HistoryOut(BaseModel):
    messages: list[MessageOut]  # oldest first
    has_more: bool  # older messages exist: ask again with before_id = messages[0].id


class StreamIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_message_id: uuid.UUID
    persona_id: uuid.UUID | None = None
    text: str = Field(default="", max_length=TEXT_MAX_CHARS)


# ------------------------------------------------------------------ routes


@router.get("/personas", response_model=list[PersonaOut])
async def personas(ctx: AppAuth, db: AppDB) -> list[PersonaOut]:
    """The person's active topics. The feedback channel is not among them: it is forwarded to the author by the Telegram bot only (the service refuses it too)."""
    async with db.tenant_session(ctx.tenant_id) as session:
        rows = (
            await session.execute(
                select(Persona.id, Persona.code, Persona.title, Persona.mode)
                .where(Persona.is_active.is_(True), Persona.code != FEEDBACK_CODE)
                .order_by(Persona.title, Persona.id)
            )
        ).all()
    return [PersonaOut(id=r.id, code=r.code, title=r.title, mode=r.mode) for r in rows]


@router.get("/chat/history", response_model=HistoryOut)
async def history(
    ctx: AppAuth,
    db: AppDB,
    persona_id: uuid.UUID | None = None,
    limit: int = Query(default=30, ge=1, le=100),
    before_id: uuid.UUID | None = None,
) -> HistoryOut:
    """One chat: the topic ``persona_id``, or - without it - the main chat (``persona_id IS NULL``) and nothing else. Pages go backwards in time: pass the id of the oldest message
    you have as ``before_id`` (the position is ``(created_at, id)`` of that message: ids alone have no order)."""
    async with db.tenant_session(ctx.tenant_id) as session:
        if persona_id is not None and (await session.execute(select(Persona.id).where(Persona.id == persona_id))).first() is None:
            raise ApiError(404, "unknown_persona", "Такой темы нет.")
        query = select(Message).where(Message.persona_id.is_(None) if persona_id is None else Message.persona_id == persona_id)
        if before_id is not None:
            anchor = (await session.execute(select(Message.created_at, Message.id).where(Message.id == before_id))).one_or_none()
            if anchor is None:
                raise ApiError(404, "unknown_message", "Такого сообщения нет.")
            query = query.where(tuple_(Message.created_at, Message.id) < tuple_(anchor.created_at, anchor.id))
        found = list((await session.execute(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1))).scalars())
        page = found[:limit]
        files = {
            row.message_id: row
            for row in (await session.execute(select(MessageAttachment).where(MessageAttachment.message_id.in_([m.id for m in page])))).scalars()
        }
    page.reverse()
    return HistoryOut(
        has_more=len(found) > limit,
        messages=[
            MessageOut(
                id=m.id,
                role=m.role.value,
                text=m.content,
                created_at=m.created_at,
                persona_id=m.persona_id,
                client_message_id=m.client_message_id,
                reply_to_id=m.reply_to_id,
                cards=list(m.cards or []),
                attachment=None
                if m.id not in files
                else AttachmentOut(
                    kind=files[m.id].kind,
                    content_kind=files[m.id].content_kind,
                    mime_type=files[m.id].mime_type,
                    file_name=files[m.id].file_name,
                    size_bytes=files[m.id].size_bytes,
                ),
            )
            for m in page
        ],
    )


async def _upload(form_value: Any, limit: int, what: str) -> tuple[bytes, str | None, str | None] | None:
    """(bytes, content type, file name) of an uploaded part, or None when the part is absent / empty-named. Reads at most ``limit`` + 1 bytes: a bigger file is refused unread."""
    if not isinstance(form_value, UploadFile):
        return None
    data = await form_value.read(limit + 1)
    if len(data) > limit:
        raise ApiError(413, f"{what}_too_large", "Файл слишком большой.")
    return data, form_value.content_type, form_value.filename


async def _parse_stream_request(request: Request, settings: Settings) -> tuple[StreamIn, tuple | None, tuple | None, dict[str, Any]]:
    """The body is JSON (text only) or multipart/form-data (text + a ``voice`` or an ``attachment`` file). Returns (fields, voice, attachment, form)."""
    content_type = request.headers.get("content-type", "").lower()
    voice = attachment = None
    raw: dict[str, Any]
    if content_type.startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            raise ApiError(422, "invalid_request", "Тело запроса — не JSON.") from None
        if not isinstance(body, dict):
            raise ApiError(422, "invalid_request", "Ожидался JSON-объект.")
        raw = body
    elif content_type.startswith("multipart/form-data"):
        form = await request.form()
        raw = {key: form.get(key) for key in ("client_message_id", "persona_id", "text") if isinstance(form.get(key), str)}
        if raw.get("persona_id") in ("", "null", "None"):
            raw["persona_id"] = None
        voice = await _upload(form.get("voice"), settings.voice_max_bytes, "voice")
        attachment = await _upload(form.get("attachment"), settings.attachment_max_bytes, "attachment")
        raw["_kind"] = form.get("attachment_kind") if isinstance(form.get("attachment_kind"), str) else None
    else:
        raise ApiError(415, "unsupported_media_type", "Нужен application/json или multipart/form-data.")
    kind = raw.pop("_kind", None)
    try:
        fields = StreamIn.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ApiError(422, "invalid_request", f"Поле {'.'.join(str(p) for p in first['loc'])}: {first['msg']}") from None
    return fields, voice, attachment, {"kind": kind}


def _sse(events: AsyncIterator[Any]) -> AsyncIterator[dict[str, str]]:
    async def gen() -> AsyncIterator[dict[str, str]]:
        async for event in events:
            yield {"event": event.kind, "data": json.dumps(event.data, ensure_ascii=False)}

    return gen()


@router.post("/chat/stream")
async def chat_stream(request: Request, ctx: AppAuth) -> EventSourceResponse:
    service: AppChatService | None = getattr(request.app.state, "app_chat", None)
    if service is None:
        raise ApiError(503, "chat_unavailable", "Чат сейчас недоступен.")
    fields, voice, attachment, extra = await _parse_stream_request(request, request.app.state.settings)
    if voice is not None and attachment is not None:
        raise ApiError(422, "one_media", "Отправьте либо голосовое, либо файл — не оба сразу.")
    meta = None
    if attachment is not None:
        _, mime, name = attachment
        meta = {"file_name": name, "mime_type": mime, "kind": extra["kind"] or ("photo" if (mime or "").startswith("image/") else "document")}
    message = AppIncomingMessage(
        user_id=ctx.tenant_id,
        persona_id=fields.persona_id,
        client_message_id=fields.client_message_id,
        text=fields.text,
        voice_bytes=None if voice is None else voice[0],
        voice_mime=None if voice is None else voice[1],
        attachment_bytes=None if attachment is None else attachment[0],
        attachment_metadata=meta,
    )
    try:
        events = await service.open_stream(message)
    except AppChatError as exc:
        raise ApiError(CHAT_ERROR_STATUS.get(exc.code, 413 if exc.code.endswith("too_big") else 422), exc.code, exc.message) from None
    return EventSourceResponse(_sse(events), headers={"Cache-Control": "no-store"})

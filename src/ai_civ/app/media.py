"""Voice and attachments of the mobile app: the bytes arrive IN the request (nothing is downloaded from anywhere), then the same rules as in the bot apply.

``AppVoiceProcessor`` is ``Orchestrator._hear`` without Telegram: size limit -> ``AudioLLM.transcribe`` -> ``voice.clean_transcript``. ``AppAttachmentProcessor`` is
``Orchestrator._look`` without Telegram: ``attachments.refusal`` on the metadata -> ``attachments.prepare`` (in a thread). The audio and the files are kept nowhere."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from ai_civ.app.dto import AppChatError
from ai_civ.bot import attachments, texts, voice
from ai_civ.config import Settings
from ai_civ.memory.llm import AudioLLM, LLMError, Media

log = logging.getLogger(__name__)

AUDIO_MIMES = frozenset({"audio/ogg", "audio/opus", "audio/mp4", "audio/m4a", "audio/x-m4a", "audio/mpeg", "audio/mp3", "audio/aac", "audio/wav", "audio/x-wav", "audio/webm", "audio/flac"})


@dataclass(frozen=True)
class Heard:
    text: str
    audio: Media | None  # the recording for the chat model (it hears the tone), None when ``voice_hear_tone`` is off


class AppVoiceProcessor:
    def __init__(self, llm: Any, settings: Settings) -> None:
        self.llm = llm
        self.settings = settings

    async def hear(self, data: bytes, mime_type: str | None = None) -> Heard:
        """The words of a recording. Raises ``AppChatError`` for what the person can fix (too big, silence, voice off); a failure of the model propagates."""
        cfg = self.settings
        if not cfg.voice_enabled or not isinstance(self.llm, AudioLLM):
            raise AppChatError("voice_off", texts.VOICE_OFF)
        if not data:
            raise AppChatError("voice_empty", texts.VOICE_UNCLEAR)
        if len(data) > cfg.voice_max_bytes:
            raise AppChatError("voice_too_long", texts.VOICE_TOO_LONG.format(limit=cfg.voice_max_seconds))
        mime = mime_type if mime_type and mime_type.startswith("audio/") else voice.DEFAULT_MIME
        try:
            raw = await asyncio.wait_for(
                self.llm.transcribe(model=cfg.gemini_transcribe_model, system=voice.SYSTEM, data=data, mime_type=mime),
                timeout=cfg.voice_llm_timeout_seconds,
            )
        except TimeoutError as exc:
            raise LLMError(f"transcription did not answer within {cfg.voice_llm_timeout_seconds}s") from exc
        text = voice.clean_transcript(raw)
        if text is None:
            log.info("app voice message: no intelligible speech")
            raise AppChatError("voice_unclear", texts.VOICE_UNCLEAR)
        if len(text) > cfg.voice_transcript_max_chars:  # refused, never cut: a cut transcript would change what the person said
            raise AppChatError("voice_too_long", texts.VOICE_TOO_LONG.format(limit=cfg.voice_max_seconds))
        return Heard(text, Media(data, mime) if cfg.voice_hear_tone else None)


class AppAttachmentProcessor:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @staticmethod
    def describe(data: bytes, metadata: dict[str, Any] | None) -> attachments.Attachment:
        meta = metadata or {}
        kind = meta.get("kind")
        mime = meta.get("mime_type")
        name = meta.get("file_name")
        return attachments.Attachment(
            file_id=f"app:{len(data)}",  # there is no remote file: the bytes came with the request
            kind="photo" if kind == "photo" else "document",
            mime_type=mime if isinstance(mime, str) else None,
            size=len(data),
            file_name=name if isinstance(name, str) else None,
        )

    async def prepare(self, data: bytes, metadata: dict[str, Any] | None) -> tuple[attachments.Attachment, attachments.Prepared]:
        """The file as the model can read it. Raises ``AppChatError`` for a file the person can replace (unsupported, too big, unreadable)."""
        cfg = self.settings
        att = self.describe(data, metadata)
        why = attachments.refusal(att, cfg)
        if why is not None:
            raise AppChatError("attachment_refused", why)
        try:
            prepared = await asyncio.wait_for(asyncio.to_thread(attachments.prepare, att, data, cfg), timeout=cfg.llm_timeout_seconds)
        except TimeoutError as exc:
            raise LLMError("file reading timed out") from exc
        except attachments.Rejected as exc:
            reply = {
                "too_big": texts.FILE_TOO_BIG.format(limit=cfg.attachment_max_bytes // 1_000_000),
                "text_too_big": texts.FILE_TEXT_TOO_BIG,
                "unsupported": texts.FILE_UNSUPPORTED,
                "empty": texts.FILE_EMPTY,
            }.get(exc.reason, texts.FILE_UNREADABLE)
            raise AppChatError(f"attachment_{exc.reason}", reply) from exc
        return att, prepared

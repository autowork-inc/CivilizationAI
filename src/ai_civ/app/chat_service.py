"""``AppChatService``: one message of the mobile app -> one reply, with no Telegram anywhere.

It is the dialogue core of ``ai_civ.bot.orchestrator`` re-assembled from the transport-free parts: the persona is the database row named by ``persona_id`` (no
forum topics, no ``telegram_thread_id``), the voice and the files arrive as bytes (``media``), and what the bot sends as buttons is a neutral ``Card`` here,
serialised to JSON for the client. The brain is the same PostgreSQL (Tier 1-3), the same prompt, the same Gemini clients.

Idempotency (``client_message_id``). The row of the user's message is the claim: ``uq_messages_user_client_id`` lets exactly one request create it, and
``uq_messages_user_reply_to`` lets exactly one reply be stored for it. A repeated request is answered from the stored reply without the model. While the first
request is still generating, a repeated one in the SAME process waits on an in-process lock and one in ANOTHER process polls the database until the reply
appears (a session advisory lock would not survive the transaction pooler, and the project never holds a database transaction across an LLM call). A user
message that stays unanswered longer than ``app_stale_seconds`` is considered abandoned (the process died): the next request answers it.

Streaming (``open_stream``). The reply is produced by a BACKGROUND task that owns everything: it opens its own short ``db.tenant_session`` for each database step (never a
session of the HTTP request), pushes ``StreamEvent`` s into a queue, and is kept in ``self.tasks`` so that the garbage collector cannot drop it. The HTTP response only drains
the queue: when the client goes away the task still finishes and stores the reply, so a retry with the same ``client_message_id`` gets it from the database. If the model
fails at any point the unanswered user message is deleted (``_abandon``): a retry starts clean instead of waiting for ``app_stale_seconds``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, exists, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ.app.dto import AppChatError, AppIncomingMessage, AppReply, AppTenant, StreamEvent
from ai_civ.app.media import AppAttachmentProcessor, AppVoiceProcessor
from ai_civ.bot import attachments, voice
from ai_civ.bot.personas import CONCIERGE_PROMPT, FEEDBACK_CODE, get_by_id
from ai_civ.bot.prompt import Memories, Prompt, build_prompt, select_memories
from ai_civ.bot.texts import KeyedLocks
from ai_civ.bot.tools.base import Delivery, ToolContext, ToolRegistry, UserTurn
from ai_civ.config import Settings
from ai_civ.db import Database
from ai_civ.db.enums import MessageRole, PersonaMode
from ai_civ.db.models import Message, MessageAttachment, Persona, User
from ai_civ.memory.judge import recall_with_evidence
from ai_civ.memory.llm import Embedder, LLMError, StreamLLM, ToolLLM

log = logging.getLogger(__name__)

EMPTY_REPLY = "The model returned an empty reply"
STREAM_FAILED = "The reply could not be produced. Please try again."  # what the person is told (the cause is in the log, never on the wire)

TokenSink = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class _Stored:
    id: uuid.UUID
    created_at: datetime
    fresh: bool  # False = the row already existed (a repeated request)


@dataclass(frozen=True)
class _Answer:
    text: str
    third_party: bool = False
    cards: tuple[dict[str, Any], ...] = ()
    after_reply: tuple[Any, ...] = ()


class AppChatService:
    def __init__(
        self,
        *,
        db: Database,
        chat_llm: Any,
        settings: Settings,
        router_llm: Any | None = None,
        embedder: Embedder | None = None,
        tools: ToolRegistry | None = None,
    ) -> None:
        self.db = db
        self.chat_llm = chat_llm
        self.router_llm = router_llm if router_llm is not None else chat_llm  # the Judge's JSON transport
        self.embedder = embedder  # None = Tier 2 is off
        self.settings = settings
        self.tools = tools  # None = the model has no functions in the app (yet)
        self.voice = AppVoiceProcessor(chat_llm, settings)
        self.files = AppAttachmentProcessor(settings)
        self._locks = KeyedLocks()
        self.tasks: set[asyncio.Task[None]] = set()  # the running reply tasks (a task nobody references can be garbage-collected half-way)

    # ------------------------------------------------------------ entry point

    async def handle_app_message(self, msg: AppIncomingMessage) -> AppReply:
        if msg.voice_bytes is not None and msg.attachment_bytes is not None:
            raise AppChatError("one_media", "Send either a voice message or a file, not both.")
        if not msg.text.strip() and msg.voice_bytes is None and msg.attachment_bytes is None:
            raise AppChatError("empty_message", "The message is empty.")
        tenant = await self._load_tenant(msg.user_id)
        persona = await self._load_persona(tenant, msg.persona_id)
        async with self._locks.hold((tenant.id, msg.client_message_id)):  # the same process: the second request waits here for the first
            replay = await self.find_reply(tenant.id, msg.client_message_id)
            if replay is not None:
                return replay
            return await self._process(tenant, persona, msg)

    async def find_reply(self, user_id: uuid.UUID, client_message_id: uuid.UUID) -> AppReply | None:
        """The stored answer to the message with this client id, or None (no such message, or not answered yet)."""
        async with self.db.tenant_session(user_id) as session:
            user_row = (
                await session.execute(
                    select(Message.id, Message.content, Message.persona_id).where(Message.client_message_id == client_message_id)
                )
            ).one_or_none()
            if user_row is None:
                return None
            return await self._reply_row(session, user_row.id, user_row.persona_id, user_row.content)

    # ------------------------------------------------------------ the use case

    async def _process(
        self,
        tenant: AppTenant,
        persona: Persona | None,
        msg: AppIncomingMessage,
        *,
        media: tuple[attachments.Attachment, attachments.Prepared] | None = None,
        sink: TokenSink | None = None,
    ) -> AppReply:
        """``media`` = the file, already prepared by the caller; ``sink`` = streaming (every piece of the reply goes to it, and a failure deletes the unanswered message)."""
        text_in = msg.text.strip()
        audio = None
        if msg.voice_bytes is not None:
            heard = await self.voice.hear(msg.voice_bytes, msg.voice_mime)  # may raise AppChatError (nothing is stored then)
            text_in, audio = heard.text, heard.audio
        att = prepared = None
        if media is not None:
            att, prepared = media
        elif msg.attachment_bytes is not None:
            att, prepared = await self.files.prepare(msg.attachment_bytes, msg.attachment_metadata)

        persona_id = None if persona is None else persona.id
        stored = await self._claim(tenant.id, persona_id, msg.client_message_id, attachments.stored_text(text_in, att))
        if not stored.fresh:  # a repeated request that got past the first check: the first one may still be working on it
            waited = await self._wait_for_reply(tenant.id, stored)
            if waited is not None:
                return waited
        try:
            if att is not None and prepared is not None:
                await self._store_attachment(tenant.id, stored.id, att, prepared)

            memories = await self._recall(tenant, text_in)
            prompt = await build_prompt(
                self.db,
                user_id=tenant.id,
                timezone=tenant.timezone,
                persona_id=persona_id,
                persona_prompt=CONCIERGE_PROMPT if persona is None else persona.system_prompt,
                settings=self.settings,
                memories=memories,
                mode=PersonaMode.normal.value if persona is None else persona.mode,
            )
            prompt = self._with_attachment(prompt, att, prepared)
            prompt = self._with_voice(prompt, audio)
            answer = await self._answer(
                tenant,
                prompt,
                persona=persona,
                turn=UserTurn(stored.id, text_in, image=prepared is not None and prepared.content_kind == "image"),
                untrusted=prepared is not None,
                slow=msg.voice_bytes is not None,
                sink=sink,
            )
            reply = await self._store_reply(tenant.id, persona_id, stored.id, answer, user_text=text_in)
        except BaseException:
            if sink is not None:  # streaming: nobody will read a half-answer, and an unanswered row would make the retry wait
                await asyncio.shield(self._abandon(tenant.id, stored.id))
            raise
        if not reply.replayed:
            for action in answer.after_reply:  # a courtesy action of a tool never breaks the reply
                try:
                    await action(Delivery(tenant, None, persona))  # type: ignore[arg-type]
                except Exception:  # noqa: BLE001
                    log.warning("an after-reply action of tenant %s failed", tenant.id, exc_info=True)
        return reply

    async def _abandon(self, user_id: uuid.UUID, message_id: uuid.UUID) -> None:
        """Delete the user's message (its attachment row goes with it: ON DELETE CASCADE) if it still has no reply. Never raises: a failed cleanup only leaves the stale-message path."""
        try:
            async with self.db.tenant_session(user_id) as session:
                answered = exists().where(Message.reply_to_id == message_id)
                await session.execute(delete(Message).where(Message.id == message_id, ~answered))
        except Exception:  # noqa: BLE001
            log.warning("could not remove the abandoned message %s of tenant %s", message_id, user_id, exc_info=True)

    # ------------------------------------------------------------ streaming

    async def open_stream(self, msg: AppIncomingMessage) -> AsyncIterator[StreamEvent]:
        """Validate the request, start the reply task and return its events as an async iterator.

        Whatever the person can fix BEFORE the model is involved raises ``AppChatError`` here (the API answers with an ordinary HTTP error): an empty message, an unknown topic, a
        refused file. What happens afterwards (the transcription of a voice message, the model) reaches the client as an ``error`` event, because the response has begun."""
        if msg.voice_bytes is not None and msg.attachment_bytes is not None:
            raise AppChatError("one_media", "Send either a voice message or a file, not both.")
        if not msg.text.strip() and msg.voice_bytes is None and msg.attachment_bytes is None:
            raise AppChatError("empty_message", "The message is empty.")
        tenant = await self._load_tenant(msg.user_id)
        persona = await self._load_persona(tenant, msg.persona_id)
        media = await self.files.prepare(msg.attachment_bytes, msg.attachment_metadata) if msg.attachment_bytes is not None else None
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        task = asyncio.create_task(self._stream_task(tenant, persona, msg, media, queue), name=f"app-reply-{msg.client_message_id}")
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self._drain(queue)

    @staticmethod
    async def _drain(queue: asyncio.Queue[StreamEvent | None]) -> AsyncIterator[StreamEvent]:
        while True:
            event = await queue.get()
            if event is None:
                return
            yield event

    async def _stream_task(
        self,
        tenant: AppTenant,
        persona: Persona | None,
        msg: AppIncomingMessage,
        media: tuple[attachments.Attachment, attachments.Prepared] | None,
        queue: asyncio.Queue[StreamEvent | None],
    ) -> None:
        streamed: list[str] = []

        async def sink(piece: str) -> None:
            streamed.append(piece)
            queue.put_nowait(StreamEvent("token", {"text": piece}))

        try:
            async with self._locks.hold((tenant.id, msg.client_message_id)):  # a repeated request of this process waits here for the first one
                reply = await self.find_reply(tenant.id, msg.client_message_id)
                if reply is None:
                    reply = await self._process(tenant, persona, msg, media=media, sink=sink)
            tail = self._unsent_tail(reply.text, "".join(streamed))
            if tail:
                queue.put_nowait(StreamEvent("token", {"text": tail}))
            for card in reply.cards:
                queue.put_nowait(StreamEvent("card", {"card": card}))
            done: dict[str, Any] = {"user_message_id": str(reply.reply_to_id), "reply_message_id": str(reply.message_id)}
            if msg.voice_bytes is not None and reply.user_text:
                done["user_text"] = reply.user_text  # what the person said: the app shows it as their bubble
            queue.put_nowait(StreamEvent("done", done))
        except AppChatError as exc:
            queue.put_nowait(StreamEvent("error", {"error": exc.message, "code": exc.code}))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the model, the database, anything: the client gets ONE structured error, the log gets the cause
            log.warning("streamed reply to %s of tenant %s failed", msg.client_message_id, tenant.id, exc_info=True)
            queue.put_nowait(StreamEvent("error", {"error": STREAM_FAILED, "code": "reply_failed"}))
        finally:
            queue.put_nowait(None)

    @staticmethod
    def _unsent_tail(final: str, sent: str) -> str:
        """What of the final reply the client has not received yet: all of it (a stored reply, a tool's fixed text) or the part after the streamed pieces."""
        if not sent:
            return final
        sent = sent.rstrip()
        if final.startswith(sent):
            return final[len(sent) :]
        log.warning("the stored reply differs from the streamed text; the client keeps what it received")
        return ""

    async def aclose(self, timeout: float = 30.0) -> None:
        """Shutdown: let the running replies finish (they store their answers), cancel what does not."""
        pending = [task for task in self.tasks if not task.done()]
        if not pending:
            return
        _, still = await asyncio.wait(pending, timeout=timeout)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    # ------------------------------------------------------------ database

    async def _load_tenant(self, user_id: uuid.UUID) -> AppTenant:
        async with self.db.tenant_session(user_id) as session:
            row = (await session.execute(select(User.timezone, User.onboarding_state, User.group_state).where(User.id == user_id))).one_or_none()
        if row is None:
            raise AppChatError("unknown_user", "No such user.")
        if row.onboarding_state != "done":
            raise AppChatError("onboarding_required", "Finish the introduction in the chat first.")
        return AppTenant(user_id, row.timezone, None, row.onboarding_state, row.group_state)

    async def _load_persona(self, tenant: AppTenant, persona_id: uuid.UUID | None) -> Persona | None:
        if persona_id is None:
            return None
        async with self.db.tenant_session(tenant.id) as session:
            persona = await get_by_id(session, persona_id)
        if persona is None or not persona.is_active:
            raise AppChatError("unknown_persona", "No such topic.")
        if persona.code == FEEDBACK_CODE:  # the feedback channel is forwarded to the author (Telegram only for now): the model must not pretend to read it
            raise AppChatError("persona_unavailable", "This topic is not available in the app yet.")
        return persona

    async def _claim(self, user_id: uuid.UUID, persona_id: uuid.UUID | None, client_id: uuid.UUID, content: str) -> _Stored:
        async with self.db.tenant_session(user_id) as session:
            fresh = (
                await session.execute(
                    pg_insert(Message)
                    .values(user_id=user_id, persona_id=persona_id, role=MessageRole.user, content=content, client_message_id=client_id)
                    .on_conflict_do_nothing(index_elements=["user_id", "client_message_id"], index_where=text("client_message_id IS NOT NULL"))
                    .returning(Message.id, Message.created_at)
                )
            ).one_or_none()
            if fresh is not None:
                return _Stored(fresh.id, fresh.created_at, True)
            old = (await session.execute(select(Message.id, Message.created_at).where(Message.client_message_id == client_id))).one()
            return _Stored(old.id, old.created_at, False)

    async def _reply_row(self, session: Any, user_message_id: uuid.UUID, persona_id: uuid.UUID | None, user_text: str) -> AppReply | None:
        row = (
            await session.execute(
                select(Message.id, Message.content, Message.cards, Message.created_at, Message.third_party_data).where(
                    Message.reply_to_id == user_message_id
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return AppReply(
            message_id=row.id,
            reply_to_id=user_message_id,
            persona_id=persona_id,
            text=row.content,
            cards=tuple(row.cards or ()),
            created_at=row.created_at,
            replayed=True,
            user_text=user_text,
            third_party=row.third_party_data,
        )

    async def _wait_for_reply(self, user_id: uuid.UUID, stored: _Stored) -> AppReply | None:
        """The first request with this client id is (maybe) still working: wait for its reply, up to ``app_stale_seconds`` after the message was written.
        None = the reply never came (the process died): the caller answers the message itself (``uq_messages_user_reply_to`` keeps it to one reply)."""
        cfg = self.settings
        while True:
            async with self.db.tenant_session(user_id) as session:
                row = (await session.execute(select(Message.content, Message.persona_id).where(Message.id == stored.id))).one()
                found = await self._reply_row(session, stored.id, row.persona_id, row.content)
            if found is not None:
                return found
            if (datetime.now(UTC) - stored.created_at).total_seconds() >= cfg.app_stale_seconds:
                log.warning("the message %s of tenant %s stayed unanswered for %.0fs; answering it again", stored.id, user_id, cfg.app_stale_seconds)
                return None
            await asyncio.sleep(cfg.app_dedup_poll_seconds)

    async def _store_attachment(self, user_id: uuid.UUID, message_id: uuid.UUID, att: attachments.Attachment, prepared: attachments.Prepared) -> None:
        async with self.db.tenant_session(user_id) as session:
            await session.execute(
                pg_insert(MessageAttachment)
                .values(
                    user_id=user_id,
                    message_id=message_id,
                    kind=att.kind,
                    content_kind=prepared.content_kind,
                    mime_type=prepared.mime,
                    file_name=att.clean_name,
                    size_bytes=prepared.size,
                    sha256=prepared.sha256,
                    telegram_file_id=att.file_id,
                    extracted_text=prepared.text,
                    text_truncated=prepared.truncated,
                )
                .on_conflict_do_nothing(constraint="uq_message_attachments_message")
            )

    async def _store_reply(self, user_id: uuid.UUID, persona_id: uuid.UUID | None, user_message_id: uuid.UUID, answer: _Answer, *, user_text: str) -> AppReply:
        async with self.db.tenant_session(user_id) as session:
            fresh = (
                await session.execute(
                    pg_insert(Message)
                    .values(
                        user_id=user_id,
                        persona_id=persona_id,
                        role=MessageRole.assistant,
                        content=answer.text,
                        third_party_data=answer.third_party,
                        reply_to_id=user_message_id,
                        cards=list(answer.cards) or None,
                    )
                    .on_conflict_do_nothing(index_elements=["user_id", "reply_to_id"], index_where=text("reply_to_id IS NOT NULL"))
                    .returning(Message.id, Message.created_at)
                )
            ).one_or_none()
            if fresh is not None:
                return AppReply(
                    message_id=fresh.id,
                    reply_to_id=user_message_id,
                    persona_id=persona_id,
                    text=answer.text,
                    cards=answer.cards,
                    created_at=fresh.created_at,
                    user_text=user_text,
                    third_party=answer.third_party,
                )
            existing = await self._reply_row(session, user_message_id, persona_id, user_text)  # somebody else stored the reply first: theirs stands
        assert existing is not None
        return existing

    # ------------------------------------------------------------ the model

    async def _recall(self, tenant: AppTenant, text_in: str) -> Memories | None:
        """Tier 2 for one reply (see ``Orchestrator._recall``): never raises, a failure degrades to "unavailable"."""
        cfg = self.settings
        if self.embedder is None or not cfg.recall_enabled:
            return None
        query = text_in.strip()
        if len(query) < cfg.recall_min_query_chars:
            return None
        started = time.monotonic()
        try:
            recalled = await asyncio.wait_for(
                recall_with_evidence(
                    self.db,
                    self.router_llm,
                    self.embedder,
                    tenant.id,
                    query[: cfg.recall_query_max_chars],
                    judge_model=cfg.gemini_judge_model,
                    top_k=cfg.judge_top_k,
                    max_distance=cfg.recall_max_distance,
                ),
                timeout=cfg.recall_timeout_seconds,
            )
        except Exception:  # noqa: BLE001 - degrade, never fail the reply
            log.warning("episodic recall failed for tenant %s after %.1fs; answering without Tier 2", tenant.id, time.monotonic() - started, exc_info=True)
            return Memories("unavailable")
        if recalled.result.reason == "no_episodes":
            return None
        return select_memories(recalled.evidence, tenant.timezone, max_chars=cfg.recall_max_chars)

    @staticmethod
    def _with_attachment(prompt: Prompt, att: attachments.Attachment | None, prepared: attachments.Prepared | None) -> Prompt:
        if prepared is None or att is None or not prompt.turns or prompt.turns[-1].role != "user":
            return prompt
        last = prompt.turns[-1]
        if prepared.media is not None:
            turn = replace(last, media=(prepared.media,))
        else:
            turn = replace(last, text=f"{last.text}\n\n{attachments.render_file_block(att, prepared)}")
        return replace(prompt, system=f"{prompt.system}\n\n{attachments.FILE_RULES}", turns=[*prompt.turns[:-1], turn])

    @staticmethod
    def _with_voice(prompt: Prompt, audio: Any) -> Prompt:
        if audio is None or not prompt.turns or prompt.turns[-1].role != "user":
            return prompt
        last = prompt.turns[-1]
        turn = replace(last, media=(*last.media, audio))
        return replace(prompt, system=f"{prompt.system}\n\n{voice.TONE_RULES}", turns=[*prompt.turns[:-1], turn])

    async def _bounded(self, call: Any, what: str, *, timeout: float | None = None) -> Any:
        limit = self.settings.llm_timeout_seconds if timeout is None else timeout
        try:
            return await asyncio.wait_for(call, timeout=limit)
        except TimeoutError as exc:
            raise LLMError(f"{what} did not answer within {limit}s") from exc

    async def _generate(self, prompt: Prompt, *, max_output_tokens: int | None = None, slow: bool = False, sink: TokenSink | None = None) -> str:
        if sink is not None:
            return await self._stream_generate(prompt, sink, max_output_tokens=max_output_tokens, slow=slow)
        cap = {} if max_output_tokens is None else {"max_output_tokens": max_output_tokens}
        reply = await self._bounded(
            self.chat_llm.generate_text(model=self.settings.gemini_chat_model, system=prompt.system, turns=prompt.turns, **cap),
            "chat model",
            timeout=self.settings.voice_llm_timeout_seconds if slow else None,
        )
        if not reply or not reply.strip():
            raise LLMError(EMPTY_REPLY)
        return reply

    async def _stream_generate(self, prompt: Prompt, sink: TokenSink, *, max_output_tokens: int | None, slow: bool) -> str:
        """``_generate`` that hands every piece to ``sink`` while it arrives (a model that cannot stream answers in one piece). The deadline is PER PIECE: a long answer is not
        cut while it is flowing, a model that goes silent is."""
        if not isinstance(self.chat_llm, StreamLLM):
            text = await self._generate(prompt, max_output_tokens=max_output_tokens, slow=slow)
            await sink(text.strip())
            return text
        limit = self.settings.voice_llm_timeout_seconds if slow else self.settings.llm_timeout_seconds
        cap = {} if max_output_tokens is None else {"max_output_tokens": max_output_tokens}
        stream = self.chat_llm.stream_text(model=self.settings.gemini_chat_model, system=prompt.system, turns=prompt.turns, **cap).__aiter__()
        pieces: list[str] = []
        try:
            while True:
                try:
                    piece = await asyncio.wait_for(stream.__anext__(), timeout=limit)
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise LLMError(f"chat model went silent for {limit}s") from exc
                pieces.append(piece)
                await sink(piece)
        finally:
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()
        text = "".join(pieces).strip()
        if not text:
            raise LLMError(EMPTY_REPLY)
        return text

    async def _answer(
        self, tenant: AppTenant, prompt: Prompt, *, persona: Persona | None, turn: UserTurn, untrusted: bool, slow: bool, sink: TokenSink | None = None
    ) -> _Answer:
        """The reply, built like ``Orchestrator._answer``: with no tool on offer it is the model's text; when the model calls a function the tool validates and runs it,
        the model is asked AGAIN to phrase the reply (with the tools' notes), and the cards the tools made are serialised for the client."""
        if self.tools is None or not isinstance(self.chat_llm, ToolLLM):
            return _Answer(await self._generate(prompt, slow=slow, sink=sink))
        offers = await self.tools.offer(ToolContext(tenant, persona, turn), untrusted=untrusted)  # type: ignore[arg-type]
        if not offers:
            return _Answer(await self._generate(prompt, slow=slow, sink=sink))
        offered = replace(prompt, system="\n\n".join([prompt.system, *(offer.section for offer in offers)]))
        reply = await self._bounded(
            self.chat_llm.generate_with_tools(
                model=self.settings.gemini_chat_model,
                system=offered.system,
                turns=offered.turns,
                tools=[spec for offer in offers for spec in offer.specs],
            ),
            "chat model",
            timeout=self.settings.voice_llm_timeout_seconds if slow else None,
        )
        if not reply.calls:
            return _Answer(reply.text or await self._generate(prompt, slow=slow, sink=sink))
        results = []
        for offer in sorted(offers, key=lambda item: item.run_rank):
            result = await offer.run(reply.calls)
            if result is not None:
                results.append(result)
        if not results:  # nothing ran: the words next to a call of one of OUR functions are not trusted (they may claim an action that did not happen)
            if any(call.name in self.tools.function_names for call in reply.calls):
                return _Answer(await self._generate(prompt, slow=slow, sink=sink))
            return _Answer(reply.text or await self._generate(prompt, slow=slow, sink=sink))
        parts: list[str] = []
        if any(result.phrase for result in results):
            notes = [note for result in results for note in result.notes]
            caps = [result.output_cap for result in results if result.output_cap]
            parts.append(await self._generate(replace(prompt, system="\n\n".join([prompt.system, *notes])), max_output_tokens=min(caps) if caps else None, slow=slow, sink=sink))
        parts += [result.text for result in results if result.text]
        if not parts:
            parts.append(await self._generate(prompt, slow=slow, sink=sink))
        return _Answer(
            "\n\n".join(parts),
            third_party=any(result.third_party for result in results),
            cards=tuple(card.to_json() for result in results for card in result.cards),
            after_reply=tuple(action for result in results for action in result.after_reply),
        )

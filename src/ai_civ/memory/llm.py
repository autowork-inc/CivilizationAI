"""Gemini access behind two small protocols (the rest of the code never imports the SDK).

Fail-fast: every request has a hard timeout and the SDK's own retries are OFF (attempts=1). A failed request raises
``LLMError``; the outbox row is then marked failed with a backoff and Celery / the next drain run retries it.
The ONE exception is a flicker of the proxy / network path (``_is_network_flicker``): such a request is repeated at most
``NETWORK_RETRIES`` times, ``NETWORK_RETRY_PAUSE`` seconds apart, and then fails as usual. An error answer from Gemini itself
(4xx / 5xx, a blocked or empty reply, a bad schema) is never repeated here.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

import httpx
from aiohttp_socks import ProxyError as SocksProxyError
from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from ai_civ.config import ConfigError, Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """A Gemini call failed or returned something unusable (transport error, timeout, blocked / empty response)."""


T = TypeVar("T")

NETWORK_RETRIES = 2  # repeats after the first attempt (so at most 3 requests)
NETWORK_RETRY_PAUSE = 1.0  # seconds between them

# The transport could not reach Gemini at all (a SOCKS5 / WARP proxy that blinked, a refused connection, a timer that ran out): the
# request never produced an answer, so repeating it is safe. ``httpx.ProxyError`` is what httpx raises for a failed proxy handshake.
_FLICKER = (httpx.ConnectError, httpx.ProxyError, SocksProxyError, asyncio.TimeoutError)


def _is_network_flicker(exc: BaseException) -> bool:
    """True for a transport failure (also when the SDK wrapped it: the chain of causes is followed). Never true for an answer of the
    API (its own error classes), so a 4xx, a quota error or a validation error is not repeated."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, genai_errors.APIError):
            return False  # Gemini answered (4xx / 5xx): that is not a network problem
        if isinstance(exc, _FLICKER):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
    return False


async def _sleep(seconds: float) -> None:  # one name for the tests to replace
    await asyncio.sleep(seconds)


async def _with_network_retry(request: Callable[[], Awaitable[T]]) -> T:
    """Run ``request``; on a network flicker run it again (at most ``NETWORK_RETRIES`` times, with a pause). The last error is raised
    unchanged, so the caller wraps it in ``LLMError`` as before. Cancellation is never swallowed."""
    attempt = 0
    while True:
        try:
            return await request()
        except Exception as exc:
            if attempt >= NETWORK_RETRIES or not _is_network_flicker(exc):
                raise
            attempt += 1
            log.warning("Gemini request hit a network error (%r); retry %d/%d in %.1fs", exc, attempt, NETWORK_RETRIES, NETWORK_RETRY_PAUSE)
            await _sleep(NETWORK_RETRY_PAUSE)


@dataclass(frozen=True)
class ChatTurn:
    """One turn of a conversation in Gemini's vocabulary: the assistant's own turns have the role ``model``."""

    role: Literal["user", "model"]
    text: str
    media: tuple[Media, ...] = ()  # files that travel with THIS turn only (a photo, a PDF); never stored


@dataclass(frozen=True)
class Media:
    """Bytes handed to the model inline (an image or a PDF), with their MIME type."""

    data: bytes
    mime_type: str


class JsonLLM(Protocol):
    async def generate_json(self, *, model: str, system: str, prompt: str, schema: dict[str, Any]) -> str:
        """Return the raw JSON text produced by the model (the caller validates it)."""
        ...


class TextLLM(Protocol):
    async def generate_text(
        self, *, model: str, system: str, turns: list[ChatTurn], max_output_tokens: int | None = None
    ) -> str:
        """Return the model's plain-text reply to a conversation (the last turn is the user's). ``max_output_tokens`` is a hard cap
        on the reply (thinking tokens count against it): the model cannot write a long text even if somebody asks it to."""
        ...


@runtime_checkable
class StreamLLM(Protocol):
    """Streaming twin of ``TextLLM.generate_text`` (kept apart so that existing ``TextLLM`` implementations stay valid): the same arguments, the reply arrives in pieces."""

    def stream_text(
        self, *, model: str, system: str, turns: list[ChatTurn], max_output_tokens: int | None = None
    ) -> AsyncIterator[str]:
        """Yield the text of the reply as it is produced (fragments, not cumulative). Raises ``LLMError`` when the request fails, the stream breaks or
        nothing was produced at all. Only the opening of the request is repeated on a network flicker, never a stream that already delivered text."""
        ...


@dataclass(frozen=True)
class ToolSpec:
    """One function the model may ask us to run. ``parameters`` is a JSON Schema object (a hint: the code re-validates every call)."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolReply:
    """Either a final answer (``calls`` empty, ``text`` non-empty) or a request to run functions first (``text`` is ignored)."""

    text: str
    calls: tuple[ToolCall, ...] = ()


@runtime_checkable
class ToolLLM(Protocol):
    async def generate_with_tools(
        self, *, model: str, system: str, turns: list[ChatTurn], tools: list[ToolSpec], max_output_tokens: int | None = None
    ) -> ToolReply:
        """One model turn with functions on offer. The code runs them and asks AGAIN (``generate_text``) with the results in the
        prompt - there is no hidden loop and no automatic function calling."""
        ...


@runtime_checkable
class AudioLLM(Protocol):
    async def transcribe(self, *, model: str, system: str, data: bytes, mime_type: str) -> str:
        """Gemini listens to ONE recording and returns the words as plain text (the caller cleans and validates them). Audio is not kept."""
        ...


class Embedder(Protocol):
    model: str
    dim: int

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        raise LLMError("embedding model returned a zero vector")
    return [v / norm for v in vector]


def _content(turn: ChatTurn) -> Any:
    """A turn as Gemini's Content: the media of the turn (if any) first, then its text."""
    parts = [types.Part.from_bytes(data=m.data, mime_type=m.mime_type) for m in turn.media]
    return types.Content(role=turn.role, parts=[*parts, types.Part(text=turn.text)])


def _carries_audio(turns: list[ChatTurn]) -> bool:
    return any(m.mime_type.startswith("audio/") for t in turns for m in t.media)


def _check_cap(max_output_tokens: int | None) -> None:
    if max_output_tokens is not None and (isinstance(max_output_tokens, bool) or max_output_tokens < 1):
        raise ValueError("max_output_tokens must be a positive integer (or None for no cap)")


def _why_empty(response: Any, max_output_tokens: int | None) -> str:
    """Why a response has no text. A reply that hit the cap before it wrote a word is the one case worth naming: thinking tokens count
    against ``max_output_tokens``, so a cap that is too small for the model's thinking shows up exactly like this."""
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    if reason is not None and getattr(reason, "name", str(reason)) == "MAX_TOKENS":
        return f"the output limit of {max_output_tokens} tokens was reached before any text (thinking counts against it)"
    return "blocked or empty response"


def _proxy_args(proxy: str | None) -> dict[str, Any]:
    """``HttpOptions`` keyword arguments that send Gemini traffic through a proxy (socks5h = the proxy resolves names). Empty = direct."""
    if proxy is None:
        return {}
    return {"client_args": {"proxy": proxy}, "async_client_args": {"proxy": proxy}}


class GeminiClient:
    """Implements JsonLLM, TextLLM, StreamLLM, ToolLLM, AudioLLM and Embedder on top of google-genai (async)."""

    def __init__(self, settings: Settings) -> None:
        if settings.gemini_api_key is None:
            raise ConfigError("GEMINI_API_KEY is not set (see .env.example)")
        self._llm_timeout_ms = int(settings.llm_timeout_seconds * 1000)
        self._voice_timeout_ms = int(settings.voice_llm_timeout_seconds * 1000)  # a request that carries a recording (long audio) gets this one
        self._embed_timeout_ms = int(settings.embed_timeout_seconds * 1000)
        self.model = settings.gemini_embedding_model
        self.dim = settings.embedding_dim
        self._client = genai.Client(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=1),  # attempts=1: no retries
                **_proxy_args(settings.gemini_proxy),
            ),
        )

    def _timeout_ms(self, turns: list[ChatTurn]) -> int:
        """The HTTP timeout of a chat request: the long one when a turn carries a recording, the ordinary one otherwise."""
        return self._voice_timeout_ms if _carries_audio(turns) else self._llm_timeout_ms

    async def aclose(self) -> None:
        await self._client.aio.aclose()

    async def generate_json(self, *, model: str, system: str, prompt: str, schema: dict[str, Any]) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=schema,
            temperature=0.0,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            http_options=types.HttpOptions(timeout=self._llm_timeout_ms),
        )
        try:
            response = await _with_network_retry(
                lambda: self._client.aio.models.generate_content(model=model, contents=prompt, config=config)
            )
        except Exception as exc:  # transport / API errors of any kind: the caller must fail, not guess
            raise LLMError(f"Gemini generate_content({model}) failed: {exc!r}") from exc
        text = response.text
        if not text:
            raise LLMError(f"Gemini generate_content({model}) returned no text (blocked or empty response)")
        return text

    async def generate_text(
        self, *, model: str, system: str, turns: list[ChatTurn], max_output_tokens: int | None = None
    ) -> str:
        if not turns or turns[-1].role != "user":
            raise ValueError("a conversation must end with a user turn")
        _check_cap(max_output_tokens)
        contents = [_content(t) for t in turns]
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.7,
            max_output_tokens=max_output_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # no tools: silences the AFC log noise
            http_options=types.HttpOptions(timeout=self._timeout_ms(turns)),
        )
        try:
            response = await _with_network_retry(
                lambda: self._client.aio.models.generate_content(model=model, contents=contents, config=config)
            )
        except Exception as exc:  # same contract as generate_json: never guess, fail and let the caller decide
            raise LLMError(f"Gemini generate_content({model}) failed: {exc!r}") from exc
        text = (response.text or "").strip()
        if not text:
            raise LLMError(f"Gemini generate_content({model}) returned no text ({_why_empty(response, max_output_tokens)})")
        return text

    async def stream_text(
        self, *, model: str, system: str, turns: list[ChatTurn], max_output_tokens: int | None = None
    ) -> AsyncIterator[str]:
        if not turns or turns[-1].role != "user":
            raise ValueError("a conversation must end with a user turn")
        _check_cap(max_output_tokens)
        contents = [_content(t) for t in turns]
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.7,
            max_output_tokens=max_output_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            http_options=types.HttpOptions(timeout=self._timeout_ms(turns)),
        )
        try:  # the request is opened here: a network flicker is repeated while nothing has been delivered yet
            stream = await _with_network_retry(
                lambda: self._client.aio.models.generate_content_stream(model=model, contents=contents, config=config)
            )
        except Exception as exc:  # same contract as generate_text: never guess, fail and let the caller decide
            raise LLMError(f"Gemini generate_content_stream({model}) failed: {exc!r}") from exc
        iterator = stream.__aiter__()
        last: Any = None
        started = False
        while True:
            try:
                chunk = await iterator.__anext__()
            except StopAsyncIteration:
                break
            except Exception as exc:  # the stream broke half-way: the pieces already delivered cannot be taken back, the caller decides what to do with them
                raise LLMError(f"Gemini generate_content_stream({model}) broke: {exc!r}") from exc
            last = chunk
            piece = chunk.text or ""
            if not started:
                piece = piece.lstrip()  # a reply never starts with blank space (generate_text strips both ends)
            if piece:
                started = True
                yield piece
        if not started:
            raise LLMError(f"Gemini generate_content_stream({model}) returned no text ({_why_empty(last, max_output_tokens)})")

    async def generate_with_tools(
        self, *, model: str, system: str, turns: list[ChatTurn], tools: list[ToolSpec], max_output_tokens: int | None = None
    ) -> ToolReply:
        if not turns or turns[-1].role != "user":
            raise ValueError("a conversation must end with a user turn")
        if not tools:
            raise ValueError("generate_with_tools needs at least one tool; use generate_text otherwise")
        _check_cap(max_output_tokens)
        contents = [_content(t) for t in turns]
        declarations = [
            types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
            for t in tools
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.7,
            max_output_tokens=max_output_tokens,
            tools=[types.Tool(function_declarations=declarations)],
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode=types.FunctionCallingConfigMode.AUTO)),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # WE run the functions, after validating them
            http_options=types.HttpOptions(timeout=self._timeout_ms(turns)),
        )
        try:
            response = await _with_network_retry(
                lambda: self._client.aio.models.generate_content(model=model, contents=contents, config=config)
            )
        except Exception as exc:  # same contract as generate_text: never guess, fail and let the caller decide
            raise LLMError(f"Gemini generate_content({model}) with tools failed: {exc!r}") from exc
        candidates = response.candidates or []
        parts = list(candidates[0].content.parts or []) if candidates and candidates[0].content else []
        calls = tuple(
            ToolCall(name=p.function_call.name, args=dict(p.function_call.args or {}))
            for p in parts
            if p.function_call is not None and p.function_call.name
        )
        text = "".join(p.text for p in parts if p.text and not p.thought).strip()
        if not calls and not text:
            raise LLMError(
                f"Gemini generate_content({model}) returned neither text nor a function call ({_why_empty(response, max_output_tokens)})"
            )
        return ToolReply(text=text, calls=calls)

    async def transcribe(self, *, model: str, system: str, data: bytes, mime_type: str) -> str:
        if not data:
            raise ValueError("there is no audio to transcribe")
        contents = [
            types.Content(
                role="user",
                parts=[types.Part.from_bytes(data=data, mime_type=mime_type), types.Part(text="Transcribe this voice message.")],
            )
        ]
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,  # a transcript is not creative writing
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            http_options=types.HttpOptions(timeout=self._voice_timeout_ms),
        )
        try:
            response = await _with_network_retry(
                lambda: self._client.aio.models.generate_content(model=model, contents=contents, config=config)
            )
        except Exception as exc:  # same contract as generate_text: never guess, fail and let the caller decide
            raise LLMError(f"Gemini generate_content({model}) with audio failed: {exc!r}") from exc
        text = (response.text or "").strip()
        if not text:
            raise LLMError(f"Gemini generate_content({model}) returned no text for the recording ({_why_empty(response, None)})")
        return text

    async def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        config = types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=self.dim,
            http_options=types.HttpOptions(timeout=self._embed_timeout_ms),
        )
        try:
            response = await _with_network_retry(
                lambda: self._client.aio.models.embed_content(model=self.model, contents=texts, config=config)
            )
        except Exception as exc:
            raise LLMError(f"Gemini embed_content({self.model}) failed: {exc!r}") from exc
        embeddings = response.embeddings or []
        if len(embeddings) != len(texts):
            raise LLMError(f"expected {len(texts)} embeddings, got {len(embeddings)}")
        result: list[list[float]] = []
        for item in embeddings:
            values = list(item.values or [])
            if len(values) != self.dim:
                raise LLMError(f"expected a {self.dim}-dim embedding, got {len(values)}")
            # Only the full 3072-dim output is pre-normalised by the API; truncated (Matryoshka) vectors are not.
            result.append(_l2_normalize(values))
        return result

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._embed(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text], "RETRIEVAL_QUERY"))[0]


def to_response_schema(json_schema: dict[str, Any]) -> dict[str, Any]:
    """Make a pydantic JSON schema friendly for Gemini: inline $ref/$defs, drop titles and defaults.

    The schema is a *hint* to the model; the response is always re-validated by pydantic in our code.
    """
    defs = json_schema.get("$defs", {})

    def walk(node: Any, *, names: bool = False) -> Any:
        # names=True: ``node`` maps PROPERTY NAMES to schemas, so a property called "title" must survive
        if isinstance(node, dict):
            if names:
                return {k: walk(v) for k, v in node.items()}
            if "$ref" in node:
                return walk(defs[node["$ref"].rsplit("/", 1)[-1]])
            return {
                k: walk(v, names=(k == "properties"))
                for k, v in node.items()
                if k not in {"$defs", "title", "default"}
            }
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(json_schema)

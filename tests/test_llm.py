"""GeminiClient behaviour against a stubbed SDK (no network, no API key): request parameters, fail-fast mapping,
embedding validation and normalisation. NOT verified here (needs a real key): the live API accepting these parameters."""

from __future__ import annotations

import asyncio
import json
import math
from types import SimpleNamespace

import httpx
import pytest
from aiohttp_socks import ProxyError as SocksProxyError
from google.genai import errors as genai_errors
from google.genai import types
from pydantic import SecretStr

from ai_civ.config import ConfigError, Settings
from ai_civ.memory.gc import GC_SCHEMA, GcOutput
from ai_civ.memory.judge import JUDGE_SCHEMA
from google.genai import types

from ai_civ.memory.llm import ChatTurn, GeminiClient, LLMError, Media, ToolSpec, to_response_schema


@pytest.fixture(autouse=True)
def pauses(monkeypatch):
    """Retries pause for a second in production; here the pause is recorded, not slept."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("ai_civ.memory.llm._sleep", fake_sleep)
    return slept


class StubModels:
    def __init__(self) -> None:
        self.generate_calls: list[dict] = []
        self.embed_calls: list[dict] = []
        self.generate_result = SimpleNamespace(text='{"ok": true}')
        self.embed_result = None
        self.error: Exception | None = None

    async def generate_content(self, *, model, contents, config):
        self.generate_calls.append({"model": model, "contents": contents, "config": config})
        if self.error:
            raise self.error
        return self.generate_result

    async def embed_content(self, *, model, contents, config):
        self.embed_calls.append({"model": model, "contents": contents, "config": config})
        if self.error:
            raise self.error
        return self.embed_result


def make_client(**kw) -> tuple[GeminiClient, StubModels]:
    client = GeminiClient(Settings(_env_file=None, gemini_api_key=SecretStr("test-key"), **kw))
    stub = StubModels()
    client._client = SimpleNamespace(aio=SimpleNamespace(models=stub))  # type: ignore[assignment]
    return client, stub


def embedding(values: list[float]):
    return SimpleNamespace(values=values)


def test_a_missing_api_key_fails_at_construction():
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        GeminiClient(Settings(_env_file=None))


def test_the_sdk_client_is_built_without_sdk_level_retries():
    client = GeminiClient(Settings(_env_file=None, gemini_api_key=SecretStr("k")))
    options = client._client._api_client._http_options
    assert options.retry_options.attempts == 1  # fail-fast: retries are the outbox's job, not a hidden SDK loop


async def test_generate_json_requests_structured_json_with_a_timeout_and_temperature_zero():
    client, stub = make_client(llm_timeout_seconds=12.5)
    out = await client.generate_json(model="gemini-3.5-flash-lite", system="sys", prompt="p", schema=JUDGE_SCHEMA)
    assert out == '{"ok": true}'
    call = stub.generate_calls[0]
    config = call["config"]
    assert call["model"] == "gemini-3.5-flash-lite" and call["contents"] == "p"
    assert config.response_mime_type == "application/json" and config.response_json_schema == JUDGE_SCHEMA
    assert config.system_instruction == "sys" and config.temperature == 0.0
    assert config.http_options.timeout == 12500  # milliseconds


async def test_generate_text_sends_the_conversation_with_roles_a_system_prompt_and_a_timeout():
    client, stub = make_client(llm_timeout_seconds=7.0)
    stub.generate_result = SimpleNamespace(text="  Привет!  ")
    turns = [ChatTurn("user", "hi"), ChatTurn("model", "hello"), ChatTurn("user", "how are you?")]

    out = await client.generate_text(model="gemini-3.8-flash", system="sys", turns=turns)

    assert out == "Привет!"  # surrounding whitespace is not part of an answer
    call = stub.generate_calls[0]
    assert call["model"] == "gemini-3.8-flash"
    assert [(c.role, c.parts[0].text) for c in call["contents"]] == [("user", "hi"), ("model", "hello"), ("user", "how are you?")]
    config = call["config"]
    assert config.system_instruction == "sys" and config.http_options.timeout == 7000
    assert config.response_mime_type is None  # plain text, not JSON mode


@pytest.mark.parametrize("turns", [[], [ChatTurn("user", "q"), ChatTurn("model", "a")]])
async def test_generate_text_refuses_a_conversation_that_does_not_end_with_the_user(turns):
    client, stub = make_client()
    with pytest.raises(ValueError):
        await client.generate_text(model="m", system="s", turns=turns)
    assert stub.generate_calls == []  # nothing was sent


@pytest.mark.parametrize("text", [None, "", "   "])
async def test_generate_text_treats_a_blocked_or_blank_reply_as_an_error(text):
    client, stub = make_client()
    stub.generate_result = SimpleNamespace(text=text)
    with pytest.raises(LLMError, match="no text"):
        await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")])


async def test_generate_text_transport_errors_become_llm_error():
    client, stub = make_client()
    stub.error = TimeoutError("deadline")
    with pytest.raises(LLMError) as excinfo:
        await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")])
    assert excinfo.value.__cause__ is stub.error


async def test_transcribe_sends_the_recording_inline_with_its_mime_type_a_system_prompt_temperature_zero_and_a_timeout():
    client, stub = make_client(voice_llm_timeout_seconds=9.0)  # a recording is listened to under the voice deadline
    stub.generate_result = SimpleNamespace(text="  Привет, это голосовое  ")

    out = await client.transcribe(model="gemini-3.5-flash-lite", system="sys", data=b"OggS-bytes", mime_type="audio/ogg")

    assert out == "Привет, это голосовое"
    call = stub.generate_calls[0]
    assert call["model"] == "gemini-3.5-flash-lite"
    [content] = call["contents"]
    assert content.role == "user"
    audio, instruction = content.parts
    assert audio.inline_data.data == b"OggS-bytes" and audio.inline_data.mime_type == "audio/ogg"  # bytes inline: no upload, nothing to clean up
    assert instruction.text
    config = call["config"]
    assert config.system_instruction == "sys" and config.temperature == 0.0 and config.http_options.timeout == 9000
    assert config.response_mime_type is None  # plain text


async def test_transcribe_refuses_empty_audio_without_calling_the_model():
    client, stub = make_client()
    with pytest.raises(ValueError):
        await client.transcribe(model="m", system="s", data=b"", mime_type="audio/ogg")
    assert stub.generate_calls == []


@pytest.mark.parametrize("text", [None, "", "  "])
async def test_transcribe_treats_a_blocked_or_blank_reply_as_an_error(text):
    client, stub = make_client()
    stub.generate_result = SimpleNamespace(text=text)
    with pytest.raises(LLMError, match="no text"):
        await client.transcribe(model="m", system="s", data=b"x", mime_type="audio/ogg")


async def test_transcribe_transport_errors_become_llm_error():
    client, stub = make_client()
    stub.error = TimeoutError("deadline")
    with pytest.raises(LLMError) as excinfo:
        await client.transcribe(model="m", system="s", data=b"x", mime_type="audio/ogg")
    assert excinfo.value.__cause__ is stub.error


async def test_a_reply_cap_goes_to_the_model_as_max_output_tokens_and_no_cap_means_none():
    client, stub = make_client()
    stub.generate_result = SimpleNamespace(text="ok")
    turns = [ChatTurn("user", "q")]
    await client.generate_text(model="m", system="s", turns=turns, max_output_tokens=400)
    await client.generate_text(model="m", system="s", turns=turns)
    capped, plain = (call["config"].max_output_tokens for call in stub.generate_calls)
    assert capped == 400 and plain is None  # an ordinary reply is not capped


@pytest.mark.parametrize("cap", [0, -5, True])
async def test_a_nonsense_cap_is_refused_before_any_request(cap):
    client, stub = make_client()
    with pytest.raises(ValueError, match="max_output_tokens"):
        await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")], max_output_tokens=cap)
    assert stub.generate_calls == []


async def test_a_reply_that_hit_the_cap_before_any_text_says_so():
    client, stub = make_client()
    stub.generate_result = SimpleNamespace(
        text=None, candidates=[SimpleNamespace(finish_reason=types.FinishReason.MAX_TOKENS)]
    )  # thinking can eat the whole budget
    with pytest.raises(LLMError, match=r"output limit of 400 tokens.*thinking"):
        await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")], max_output_tokens=400)
    stub.generate_result = SimpleNamespace(text="", candidates=[SimpleNamespace(finish_reason=types.FinishReason.SAFETY)])
    with pytest.raises(LLMError, match="blocked or empty"):
        await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")], max_output_tokens=400)


async def test_the_cap_also_reaches_a_turn_with_functions():
    client, stub = make_client()
    part = SimpleNamespace(text="ответ", thought=False, function_call=None)
    stub.generate_result = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
    tool = ToolSpec(name="f", description="d", parameters={"type": "object", "properties": {}})
    reply = await client.generate_with_tools(model="m", system="s", turns=[ChatTurn("user", "q")], tools=[tool], max_output_tokens=400)
    assert reply.text == "ответ" and stub.generate_calls[0]["config"].max_output_tokens == 400

@pytest.mark.parametrize("error", [TimeoutError("deadline"), ConnectionError("reset"), RuntimeError("429 quota")])
async def test_transport_errors_become_llm_error(error):
    client, stub = make_client()
    stub.error = error
    with pytest.raises(LLMError) as excinfo:
        await client.generate_json(model="m", system="s", prompt="p", schema={})
    assert excinfo.value.__cause__ is error


@pytest.mark.parametrize("text", [None, ""])
async def test_a_blocked_or_empty_response_is_an_error_not_an_empty_answer(text):
    client, stub = make_client()
    stub.generate_result = SimpleNamespace(text=text)
    with pytest.raises(LLMError, match="no text"):
        await client.generate_json(model="m", system="s", prompt="p", schema={})


async def test_embeddings_request_1536_dims_the_right_task_type_and_are_normalised():
    client, stub = make_client()
    raw = [3.0, 4.0] + [0.0] * 1534
    stub.embed_result = SimpleNamespace(embeddings=[embedding(raw), embedding(raw)])
    vectors = await client.embed_documents(["a", "b"])
    call = stub.embed_calls[0]
    assert call["model"] == "gemini-embedding-001" and call["contents"] == ["a", "b"]
    assert call["config"].task_type == "RETRIEVAL_DOCUMENT" and call["config"].output_dimensionality == 1536
    assert call["config"].http_options.timeout == 30000
    for vector in vectors:
        assert len(vector) == 1536 and math.isclose(sum(v * v for v in vector), 1.0)
        assert vector[:2] == pytest.approx([0.6, 0.8])  # truncated (Matryoshka) vectors are re-normalised by us


async def test_a_query_embedding_uses_the_query_task_type():
    client, stub = make_client()
    stub.embed_result = SimpleNamespace(embeddings=[embedding([1.0] + [0.0] * 1535)])
    assert len(await client.embed_query("q")) == 1536
    assert stub.embed_calls[0]["config"].task_type == "RETRIEVAL_QUERY"


async def test_embedding_validation_is_strict():
    client, stub = make_client()
    stub.embed_result = SimpleNamespace(embeddings=[embedding([1.0] * 768)])
    with pytest.raises(LLMError, match="1536-dim"):
        await client.embed_documents(["a"])  # a model that ignored output_dimensionality
    stub.embed_result = SimpleNamespace(embeddings=[embedding([1.0] * 1536)])
    with pytest.raises(LLMError, match="expected 2 embeddings"):
        await client.embed_documents(["a", "b"])
    stub.embed_result = SimpleNamespace(embeddings=[embedding([0.0] * 1536)])
    with pytest.raises(LLMError, match="zero vector"):
        await client.embed_documents(["a"])
    stub.embed_result = SimpleNamespace(embeddings=None)
    with pytest.raises(LLMError):
        await client.embed_documents(["a"])


async def test_embedding_transport_errors_become_llm_error_and_empty_input_makes_no_call():
    client, stub = make_client()
    assert await client.embed_documents([]) == [] and stub.embed_calls == []
    stub.error = TimeoutError("slow")
    with pytest.raises(LLMError):
        await client.embed_documents(["a"])


def test_response_schemas_are_inlined_and_json_serialisable():
    text = json.dumps(GC_SCHEMA)
    assert "$ref" not in text and "$defs" not in text and '"title"' not in text
    assert set(GC_SCHEMA["properties"]) == set(GcOutput.model_fields)
    assert GC_SCHEMA["properties"]["facts"]["items"]["properties"]["category"]["enum"][0] == "identity"


def test_to_response_schema_resolves_nested_refs():
    schema = {"$defs": {"A": {"type": "string", "title": "A"}}, "type": "object", "properties": {"x": {"$ref": "#/$defs/A"}}}
    assert to_response_schema(schema) == {"type": "object", "properties": {"x": {"type": "string"}}}


# ---------------------------------------------------------------- network flicker: two quick repeats, nothing else is repeated


class Flaky(StubModels):
    """Fails with ``errors`` one by one (then answers), counting every request."""

    def __init__(self, errors: list[Exception]) -> None:
        super().__init__()
        self.errors = list(errors)

    async def generate_content(self, *, model, contents, config):
        self.generate_calls.append({"model": model, "contents": contents, "config": config})
        if self.errors:
            raise self.errors.pop(0)
        return self.generate_result

    async def embed_content(self, *, model, contents, config):
        self.embed_calls.append({"model": model, "contents": contents, "config": config})
        if self.errors:
            raise self.errors.pop(0)
        return self.embed_result


def flaky_client(errors: list[Exception]) -> tuple[GeminiClient, Flaky]:
    client, _ = make_client()
    stub = Flaky(errors)
    stub.generate_result = SimpleNamespace(text="ok")
    client._client = SimpleNamespace(aio=SimpleNamespace(models=stub))  # type: ignore[assignment]
    return client, stub


FLICKERS = [
    httpx.ConnectError("Host unreachable"),
    httpx.ProxyError("socks5 handshake failed"),
    SocksProxyError(1, "Host unreachable"),
    asyncio.TimeoutError(),
]


@pytest.mark.parametrize("error", FLICKERS, ids=lambda e: type(e).__name__)
async def test_a_proxy_blink_is_repeated_and_the_second_try_wins(error, pauses):
    client, stub = flaky_client([error])
    out = await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")])
    assert out == "ok" and len(stub.generate_calls) == 2 and pauses == [1.0]


async def test_two_blinks_in_a_row_are_survived_and_three_are_not(pauses):
    client, stub = flaky_client([httpx.ConnectError("a"), httpx.ConnectError("b")])
    assert await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")]) == "ok"
    assert len(stub.generate_calls) == 3 and pauses == [1.0, 1.0]

    pauses.clear()
    third = httpx.ConnectError("c")
    client, stub = flaky_client([httpx.ConnectError("a"), httpx.ConnectError("b"), third])
    with pytest.raises(LLMError) as excinfo:
        await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")])
    assert excinfo.value.__cause__ is third  # the last error is the one that is reported
    assert len(stub.generate_calls) == 3 and pauses == [1.0, 1.0]  # exactly two repeats, never more


async def test_an_error_wrapped_by_the_sdk_is_still_a_blink():
    wrapped = RuntimeError("sdk wrapper")
    wrapped.__cause__ = httpx.ConnectError("Host unreachable")
    client, stub = flaky_client([wrapped])
    assert await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")]) == "ok"
    assert len(stub.generate_calls) == 2


@pytest.mark.parametrize(
    "error",
    [
        genai_errors.ClientError(400, {"error": {"message": "bad request"}}),
        genai_errors.ClientError(429, {"error": {"message": "quota"}}),
        genai_errors.ServerError(503, {"error": {"message": "overloaded"}}),
        RuntimeError("validation"),
        ValueError("schema"),
    ],
    ids=lambda e: f"{type(e).__name__}",
)
async def test_an_answer_of_gemini_or_any_other_error_is_never_repeated(error, pauses):
    client, stub = flaky_client([error])
    with pytest.raises(LLMError) as excinfo:
        await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")])
    assert excinfo.value.__cause__ is error and len(stub.generate_calls) == 1 and pauses == []


async def test_a_blocked_reply_is_not_a_network_problem_and_is_not_repeated(pauses):
    client, stub = flaky_client([])
    stub.generate_result = SimpleNamespace(text="")
    with pytest.raises(LLMError, match="no text"):
        await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")])
    assert len(stub.generate_calls) == 1 and pauses == []


async def test_every_kind_of_request_gets_the_same_two_repeats(pauses):
    tool = ToolSpec(name="f", description="d", parameters={"type": "object", "properties": {}})
    cases = []
    client, stub = flaky_client([httpx.ConnectError("x")] * 2)
    await client.generate_json(model="m", system="s", prompt="p", schema={})
    cases.append(len(stub.generate_calls))
    client, stub = flaky_client([httpx.ConnectError("x")] * 2)
    part = SimpleNamespace(text="ответ", function_call=None, thought=False)
    stub.generate_result = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
    await client.generate_with_tools(model="m", system="s", turns=[ChatTurn("user", "q")], tools=[tool])
    cases.append(len(stub.generate_calls))
    client, stub = flaky_client([httpx.ConnectError("x")] * 2)
    await client.transcribe(model="m", system="s", data=b"ogg", mime_type="audio/ogg")
    cases.append(len(stub.generate_calls))
    assert cases == [3, 3, 3]

    client, stub = flaky_client([httpx.ConnectError("x")])
    stub.embed_result = SimpleNamespace(embeddings=[embedding([1.0] + [0.0] * (client.dim - 1))])
    assert len(await client.embed_documents(["a"])) == 1 and len(stub.embed_calls) == 2


async def test_cancellation_is_not_swallowed_by_the_retry(pauses):
    client, stub = flaky_client([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")])
    assert len(stub.generate_calls) == 1 and pauses == []


# ---------------------------------------------------------------- long recordings get the voice deadline


async def test_a_recording_gets_the_voice_timeout_and_everything_else_the_ordinary_one():
    client, stub = make_client(llm_timeout_seconds=90.0, voice_llm_timeout_seconds=180.0)
    stub.generate_result = SimpleNamespace(text="ok")
    await client.transcribe(model="m", system="s", data=b"ogg", mime_type="audio/ogg")
    assert stub.generate_calls[-1]["config"].http_options.timeout == 180_000

    audio = ChatTurn("user", "q", media=(Media(b"ogg", "audio/ogg"),))
    await client.generate_text(model="m", system="s", turns=[audio])
    assert stub.generate_calls[-1]["config"].http_options.timeout == 180_000

    photo = ChatTurn("user", "q", media=(Media(b"png", "image/png"),))
    await client.generate_text(model="m", system="s", turns=[photo])
    assert stub.generate_calls[-1]["config"].http_options.timeout == 90_000
    await client.generate_text(model="m", system="s", turns=[ChatTurn("user", "q")])
    assert stub.generate_calls[-1]["config"].http_options.timeout == 90_000


async def test_a_turn_with_functions_and_a_recording_also_gets_the_voice_timeout():
    client, stub = make_client(llm_timeout_seconds=90.0, voice_llm_timeout_seconds=180.0)
    part = SimpleNamespace(text="ответ", function_call=None, thought=False)
    stub.generate_result = SimpleNamespace(candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
    tool = ToolSpec(name="f", description="d", parameters={"type": "object", "properties": {}})
    audio = ChatTurn("user", "q", media=(Media(b"ogg", "audio/ogg"),))
    await client.generate_with_tools(model="m", system="s", turns=[audio], tools=[tool])
    assert stub.generate_calls[-1]["config"].http_options.timeout == 180_000


# ---------------------------------------------------------------- stream_text


class StreamStub(StubModels):
    """``generate_content_stream`` is awaited and returns an async iterator of chunks (an item that is an Exception is raised when its turn comes)."""

    def __init__(self, chunks: list, open_errors: list[Exception] | None = None) -> None:
        super().__init__()
        self.chunks = chunks
        self.open_errors = list(open_errors or [])
        self.stream_calls: list[dict] = []

    async def generate_content_stream(self, *, model, contents, config):
        self.stream_calls.append({"model": model, "contents": contents, "config": config})
        if self.open_errors:
            raise self.open_errors.pop(0)
        chunks = self.chunks

        async def gen():
            for item in chunks:
                if isinstance(item, Exception):
                    raise item
                yield item if not isinstance(item, str) else SimpleNamespace(text=item, candidates=None)

        return gen()


def stream_client(chunks: list, open_errors: list[Exception] | None = None, **kw) -> tuple[GeminiClient, StreamStub]:
    client, _ = make_client(**kw)
    stub = StreamStub(chunks, open_errors)
    client._client = SimpleNamespace(aio=SimpleNamespace(models=stub))  # type: ignore[assignment]
    return client, stub


async def collect(client: GeminiClient, **kw) -> list[str]:
    return [piece async for piece in client.stream_text(model="m", system="sys", turns=[ChatTurn("user", "q")], **kw)]


async def test_stream_text_yields_the_fragments_in_order_with_the_request_parameters():
    client, stub = stream_client(["  Прив", "ет, ", "мир"], llm_timeout_seconds=9.0)
    assert await collect(client, max_output_tokens=50) == ["Прив", "ет, ", "мир"]  # the reply does not start with blank space; the rest is untouched
    config = stub.stream_calls[0]["config"]
    assert config.system_instruction == "sys" and config.max_output_tokens == 50 and config.http_options.timeout == 9000
    assert [(c.role, c.parts[0].text) for c in stub.stream_calls[0]["contents"]] == [("user", "q")]


async def test_stream_text_skips_chunks_without_text():
    client, _ = stream_client(["a", SimpleNamespace(text=None, candidates=None), "", "b"])
    assert await collect(client) == ["a", "b"]


@pytest.mark.parametrize("error", FLICKERS, ids=lambda e: type(e).__name__)
async def test_stream_text_repeats_the_opening_of_the_request_on_a_network_blink(error, pauses):
    client, stub = stream_client(["ok"], open_errors=[error])
    assert await collect(client) == ["ok"] and len(stub.stream_calls) == 2 and pauses == [1.0]


async def test_stream_text_does_not_repeat_a_stream_that_broke_after_it_delivered_text():
    client, stub = stream_client(["part", httpx.ConnectError("reset")])
    received: list[str] = []
    with pytest.raises(LLMError, match="broke"):
        async for piece in client.stream_text(model="m", system="s", turns=[ChatTurn("user", "q")]):
            received.append(piece)
    assert received == ["part"] and len(stub.stream_calls) == 1  # no second request: it would write the text twice


async def test_stream_text_maps_an_api_error_on_open_and_an_empty_stream_to_llm_error():
    client, stub = stream_client(["x"], open_errors=[genai_errors.APIError(429, {"error": {"message": "quota"}})])
    with pytest.raises(LLMError, match="failed"):
        await collect(client)
    assert len(stub.stream_calls) == 1  # an answer of the API is not a network blink: not repeated
    empty, _ = stream_client(["", SimpleNamespace(text=None, candidates=None)])
    with pytest.raises(LLMError, match="no text"):
        await collect(empty)


@pytest.mark.parametrize("turns", [[], [ChatTurn("user", "q"), ChatTurn("model", "a")]])
async def test_stream_text_refuses_a_conversation_that_does_not_end_with_the_user(turns):
    client, _ = stream_client(["x"])
    with pytest.raises(ValueError, match="end with a user turn"):
        async for _ in client.stream_text(model="m", system="s", turns=turns):
            pass

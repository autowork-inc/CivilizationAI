"""Deterministic stand-ins for Gemini. The tests never make a network call to an LLM."""

from __future__ import annotations

import json
import zlib
from collections.abc import Callable
from typing import Any

from tests.helpers import EMBEDDING_DIM


def unit_vector(index: int, dim: int = EMBEDDING_DIM) -> list[float]:
    values = [0.0] * dim
    values[index % dim] = 1.0
    return values


class FakeLLM:
    """JsonLLM whose answer is computed by ``responder(prompt)``: a str (raw), a dict/list (JSON-dumped) or an Exception
    (raised, as a real transport failure would be)."""

    def __init__(self, responder: Callable[[str], Any]) -> None:
        self.responder = responder
        self.calls: list[dict[str, str]] = []

    async def generate_json(self, *, model: str, system: str, prompt: str, schema: dict[str, Any]) -> str:
        self.calls.append({"model": model, "system": system, "prompt": prompt})
        result = self.responder(prompt)
        if isinstance(result, BaseException):
            raise result
        return result if isinstance(result, str) else json.dumps(result)


class FakeEmbedder:
    """Embedder: ``mapping`` (exact text -> vector) first, otherwise a stable one-hot vector derived from the text."""

    model = "fake-embedding-001"
    dim = EMBEDDING_DIM

    def __init__(self, mapping: dict[str, list[float]] | None = None, on_embed: Callable[[], Any] | None = None) -> None:
        self.mapping = mapping or {}
        self.on_embed = on_embed  # awaited-free hook: lets a test act "while the model is being called"
        self.calls = 0

    def _vector(self, text: str) -> list[float]:
        return self.mapping.get(text) or unit_vector(zlib.crc32(text.encode()))

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.on_embed is not None:
            result = self.on_embed()
            if hasattr(result, "__await__"):
                await result
        return [self._vector(t) for t in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

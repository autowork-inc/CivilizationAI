"""Admin CLI ``ai_civ.cli.import_history``: streaming chunker (pure) and the import itself (real PostgreSQL, fake embedder)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_civ.cli import import_history as ih
from ai_civ.cli.import_history import Options, import_history, iter_chunks
from ai_civ.memory.llm import LLMError
from tests.fakes import FakeEmbedder
from tests.helpers import new_user, owner_exec


def write(tmp_path: Path, lines: list[str], name: str = "chat.txt") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class Sleeps:
    def __init__(self) -> None:
        self.pauses: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.pauses.append(seconds)


# ================================================================== the chunker (pure)


def test_1000_lines_make_one_chunk_and_the_1001st_starts_the_next(tmp_path):
    path = write(tmp_path, [f"line {i}" for i in range(2500)])
    chunks = list(iter_chunks(path, Options(max_chars=0)))
    assert [(c.first_line, c.last_line) for c in chunks] == [(1, 1000), (1001, 2000), (2001, 2500)]
    assert [c.index for c in chunks] == [0, 1, 2]


def test_the_character_cap_starts_a_new_chunk_instead_of_cutting_text():
    text_lines = ["слово " * 20] * 100  # ~120 chars each
    opts = Options(chunk_lines=1000, max_chars=1000)
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = write(Path(d), text_lines)
        chunks = list(iter_chunks(path, opts))
    assert len(chunks) > 5 and all(len(c.text) <= 1000 for c in chunks)
    assert sum(c.text.count("слово") for c in chunks) == 100 * 20  # nothing lost, nothing duplicated


def test_one_enormous_line_is_split_not_truncated(tmp_path):
    path = write(tmp_path, ["a" * 2500])
    chunks = list(iter_chunks(path, Options(max_chars=1000)))
    assert [len(c.text) for c in chunks] == [1000, 1000, 500]


def test_nul_and_control_characters_are_removed_and_blank_chunks_are_skipped(tmp_path):
    path = tmp_path / "x.txt"
    path.write_bytes(b"he\x00llo\x07 world\n\n\n\n")
    [chunk] = list(iter_chunks(path, Options()))
    assert chunk.text == "hello world"
    path.write_text("\n\n  \n---\n", encoding="utf-8")
    assert list(iter_chunks(path, Options())) == []


def test_timestamps_of_both_kinds_give_the_period_of_a_chunk_in_the_given_zone(tmp_path):
    path = write(tmp_path, ["05.03.2024 18:30 Аня: привет", "no stamp here", "2024-03-06 09:15 Боб: утро"])
    [chunk] = list(iter_chunks(path, Options(timezone="Europe/Copenhagen")))
    assert chunk.first_at == datetime(2024, 3, 5, 17, 30, tzinfo=UTC) and chunk.last_at == datetime(2024, 3, 6, 8, 15, tzinfo=UTC)


def test_a_nonsense_date_is_ignored_not_a_crash(tmp_path):
    path = write(tmp_path, ["99.99.2024 25:61 бред", "ok"])
    [chunk] = list(iter_chunks(path, Options()))
    assert chunk.first_at is None


def test_options_are_validated():
    for bad in (Options(chunk_lines=0), Options(batch=0), Options(sleep=-1), Options(max_chars=50), Options(timezone="Mars/Base"), Options(limit_chunks=0)):
        with pytest.raises(ValueError):
            bad.validate()


def test_the_dry_run_plan_counts_without_any_service(tmp_path):
    path = write(tmp_path, [f"строка {i}" for i in range(3000)])
    lines, chunks, chars = ih.plan(path, Options(max_chars=0))
    assert (lines, chunks) == (3000, 3) and chars > 0


def test_the_cli_refuses_bad_arguments_before_touching_anything(tmp_path, capsys):
    assert ih.main([str(tmp_path / "nope.txt"), "--user-id", "00000000-0000-0000-0000-000000000000"]) == 2
    path = write(tmp_path, ["x"])
    assert ih.main([str(path), "--user-id", "00000000-0000-0000-0000-000000000000", "--batch", "0"]) == 2
    assert ih.main([str(path), "--user-id", "00000000-0000-0000-0000-000000000000", "--dry-run"]) == 0
    assert "1 lines -> 1 chunks" in capsys.readouterr().out


# ================================================================== the import (real database, fake embedder)


async def stored(owner_engine, user_id):
    return await owner_exec(
        owner_engine,
        "SELECT idempotency_key, kind::text, embedding_model, importance, occurred_from <= occurred_to FROM civ.episodic_memories "
        "WHERE user_id = :u ORDER BY idempotency_key",
        u=user_id,
    )


async def test_the_import_stores_chunks_with_pauses_between_embedding_calls(owner_engine, db, tmp_path):
    user = await new_user(owner_engine)
    path = write(tmp_path, [f"сообщение {i}" for i in range(5000)])  # 5 chunks of 1000 lines
    embedder, sleeps = FakeEmbedder(), Sleeps()
    stats = await import_history(db, embedder, user, path, Options(max_chars=0, batch=2, sleep=3.0), sleep=sleeps)
    assert (stats.chunks_total, stats.chunks_stored, stats.chunks_skipped) == (5, 5, 0)
    assert embedder.calls == 3 == stats.embed_calls  # 2 + 2 + 1
    assert sleeps.pauses == [3.0, 3.0, 3.0]  # one pause after EVERY embedding call
    rows = await stored(owner_engine, user)
    assert len(rows) == 5 and all(r[1] == "dialogue_chunk" and r[2] == "fake-embedding-001" and r[3] == 3 and r[4] for r in rows)


async def test_a_rerun_embeds_nothing_and_a_longer_file_resumes_only_the_new_chunks(owner_engine, db, tmp_path):
    user = await new_user(owner_engine)
    lines = [f"реплика {i}" for i in range(3000)]
    opts = Options(max_chars=0, batch=10, sleep=0)
    await import_history(db, FakeEmbedder(), user, write(tmp_path, lines), opts, sleep=Sleeps())
    again = FakeEmbedder()
    stats = await import_history(db, again, user, write(tmp_path, lines), opts, sleep=Sleeps())
    assert again.calls == 0 and (stats.chunks_skipped, stats.chunks_stored) == (3, 0)
    assert len(await stored(owner_engine, user)) == 3


async def test_a_failure_keeps_stored_batches_and_the_rerun_resumes(owner_engine, db, tmp_path):
    user = await new_user(owner_engine)
    path = write(tmp_path, [f"запись {i}" for i in range(4000)])
    opts = Options(max_chars=0, batch=1, sleep=0, max_retries=2)
    inner = FakeEmbedder()

    class Dies:
        model = inner.model
        calls = 0

        async def embed_documents(self, texts):
            Dies.calls += 1
            if Dies.calls > 2:
                raise LLMError("429 rate limited")
            return await inner.embed_documents(texts)

    sleeps = Sleeps()
    with pytest.raises(LLMError):
        await import_history(db, Dies(), user, path, opts, sleep=sleeps)
    assert len(await stored(owner_engine, user)) == 2  # two batches landed before the failure
    assert len(sleeps.pauses) == 2 + 2  # a pause after each stored batch, then the retry pauses (bounded by max_retries=2)
    resume = FakeEmbedder()
    stats = await import_history(db, resume, user, path, opts, sleep=Sleeps())
    assert (stats.chunks_skipped, stats.chunks_stored, resume.calls) == (2, 2, 2)
    assert len(await stored(owner_engine, user)) == 4


async def test_a_transient_failure_is_retried_with_a_growing_pause(owner_engine, db, tmp_path):
    user = await new_user(owner_engine)
    path = write(tmp_path, ["hello there", "general kenobi"])
    inner = FakeEmbedder()
    fails = {"left": 2}

    class Flaky:
        model = inner.model

        async def embed_documents(self, texts):
            if fails["left"]:
                fails["left"] -= 1
                raise LLMError("503")
            return await inner.embed_documents(texts)

    sleeps = Sleeps()
    stats = await import_history(db, Flaky(), user, path, Options(sleep=2.0, max_retries=4), sleep=sleeps)
    assert stats.retries == 2 and stats.chunks_stored == 1
    assert sleeps.pauses[0] < sleeps.pauses[1] and sleeps.pauses[:2] == [4.0, 8.0]


async def test_limit_chunks_makes_a_trial_run_and_the_import_is_tenant_scoped(owner_engine, db, tmp_path):
    user, other = await new_user(owner_engine), await new_user(owner_engine)
    path = write(tmp_path, [f"x{i}" for i in range(5000)])
    stats = await import_history(db, FakeEmbedder(), user, path, Options(max_chars=0, limit_chunks=2, sleep=0), sleep=Sleeps())
    assert stats.stopped_early and stats.chunks_stored == 2
    assert len(await stored(owner_engine, user)) == 2 and await stored(owner_engine, other) == []
    # the same file for another tenant is not "already imported": keys are per tenant
    again = await import_history(db, FakeEmbedder(), other, path, Options(max_chars=0, limit_chunks=1, sleep=0), sleep=Sleeps())
    assert again.chunks_stored == 1 and again.chunks_skipped == 0

"""Admin tool: load a big chat history (hundreds of thousands of lines) into ``civ.episodic_memories`` of ONE tenant.

    python -m ai_civ.cli.import_history chat.txt --telegram-user-id 123456789
    python -m ai_civ.cli.import_history chat.txt --user-id <tenant uuid> --dry-run

Why this is a CLI and not a bot feature: a book or a log in the chat would hold the process, the embedder and the Gemini quota hostage
(the bot refuses text files over 500 KB). Here the operator decides, and the job is built to be slow, bounded and restartable:

  * the file is STREAMED (never read whole) and cut into chunks of at most ``--chunk-lines`` lines (1000) AND ``--max-chars`` characters
    (6000: gemini-embedding-001 reads about 2048 tokens per input, so a bigger chunk could not be embedded in full; a chunk is never
    truncated - the tool starts the next one instead, so 600k lines make more than 600 chunks);
  * chunks are embedded ``--batch`` at a time, and the tool SLEEPS ``--sleep`` seconds after every embedding call (Gemini rate limits);
  * one transaction per batch (``tenant_session``: RLS applies, the role is ``ai_civ_app``), ``INSERT ... ON CONFLICT DO NOTHING``;
  * re-running is safe and resumes: the idempotency key is ``import:<sha256 of the file>:<lines>x<chars>:c<N>``, chunks already stored are
    skipped BEFORE any embedding call is paid for;
  * a failed embedding call is retried a few times with a growing pause (``--max-retries``), then the tool stops with a non-zero exit code -
    a re-run continues from the first chunk that is not stored yet. No unbounded loops anywhere.

The imported text is raw dialogue (``kind='dialogue_chunk'``), not a model summary; no model but the embedder is called. Timestamps are read
from the lines when they look like ``dd.mm.yyyy hh:mm`` or ``yyyy-mm-dd hh:mm`` (in ``--timezone``); a chunk without any gets the moment of the run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import re
import sys
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, text as sql
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ai_civ import runtime
from ai_civ.config import ConfigError
from ai_civ.db import Database
from ai_civ.db.enums import EpisodeKind
from ai_civ.db.models import EpisodicMemory
from ai_civ.memory.llm import Embedder, LLMError

log = logging.getLogger("ai_civ.cli.import_history")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # NUL is refused by PostgreSQL text; the rest is noise
_TS_DOTTED = re.compile(r"(?<!\d)(\d{2})\.(\d{2})\.(\d{4})[ ,T]+(\d{1,2}):(\d{2})")
_TS_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})")
_TS_SCAN_CHARS = 48  # a timestamp, if the line has one, opens the line
MAX_RETRY_PAUSE = 120.0


@dataclass(frozen=True)
class Options:
    chunk_lines: int = 1000
    max_chars: int = 6000  # 0 = no character cap (the embedder may then reject the chunk)
    batch: int = 8  # chunks per embedding call
    sleep: float = 2.0  # seconds after every embedding call
    max_retries: int = 4  # per failed embedding call
    timezone: str = "UTC"
    limit_chunks: int | None = None  # stop after this many NEW chunks (a trial run)

    def validate(self) -> None:
        if self.chunk_lines < 1 or self.batch < 1 or self.max_retries < 0 or self.sleep < 0:
            raise ValueError("chunk-lines and batch must be >= 1; sleep and max-retries must be >= 0")
        if self.max_chars < 0 or (0 < self.max_chars < 200):
            raise ValueError("max-chars must be 0 (no cap) or at least 200")
        if self.limit_chunks is not None and self.limit_chunks < 1:
            raise ValueError("limit-chunks must be >= 1")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {self.timezone!r}") from exc


@dataclass(frozen=True)
class Chunk:
    index: int  # position among the chunks of this file with these options (stable between runs)
    first_line: int  # 1-based, for the progress log
    last_line: int
    text: str
    first_at: datetime | None
    last_at: datetime | None


@dataclass
class Stats:
    chunks_total: int = 0
    chunks_skipped: int = 0  # already stored by an earlier run
    chunks_stored: int = 0
    embed_calls: int = 0
    retries: int = 0
    lines: int = 0
    stopped_early: bool = False  # --limit-chunks reached


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def key_prefix(digest: str, opts: Options) -> str:
    return f"import:{digest[:16]}:{opts.chunk_lines}x{opts.max_chars}:c"


def clean_line(raw: str) -> str:
    return _CONTROL.sub("", raw.rstrip("\r\n"))


def timestamp_of(line: str, tz: ZoneInfo) -> datetime | None:
    head = line[:_TS_SCAN_CHARS]
    for pattern, order in ((_TS_DOTTED, "dmy"), (_TS_ISO, "ymd")):
        match = pattern.search(head)
        if match is None:
            continue
        a, b, c, hour, minute = (int(g) for g in match.groups())
        day, month, year = (a, b, c) if order == "dmy" else (c, b, a)
        try:
            return datetime(year, month, day, hour, minute, tzinfo=tz).astimezone(UTC)
        except ValueError:
            return None
    return None


def iter_chunks(path: Path, opts: Options, *, encoding: str = "utf-8-sig") -> Iterator[Chunk]:
    """Stream the file as chunks. Never holds more than one chunk in memory. A chunk ends at ``chunk_lines`` lines or when the next line
    would push it over ``max_chars``; one line longer than ``max_chars`` is split into pieces of its own (nothing is ever cut off)."""
    tz = ZoneInfo(opts.timezone)
    cap = opts.max_chars or None
    lines: list[str] = []
    chars = 0
    start = 1
    first_at: datetime | None = None
    last_at: datetime | None = None
    index = 0

    def flush(end_line: int) -> Chunk | None:
        nonlocal lines, chars, first_at, last_at, index, start
        body = "\n".join(lines).strip()
        made = None
        if any(ch.isalnum() for ch in body):  # a chunk of blank lines or punctuation is not memory
            made = Chunk(index, start, end_line, body, first_at, last_at)
            index += 1
        lines, chars, first_at, last_at, start = [], 0, None, None, end_line + 1
        return made

    number = 0
    with path.open("r", encoding=encoding, errors="replace", newline=None) as handle:
        for raw in handle:
            number += 1
            line = clean_line(raw)
            pieces = [line] if cap is None or len(line) <= cap else [line[i : i + cap] for i in range(0, len(line), cap)]
            for position, piece in enumerate(pieces):
                if lines and cap is not None and chars + len(piece) + 1 > cap:
                    chunk = flush(number - 1 if position == 0 else number)
                    if chunk is not None:
                        yield chunk
                lines.append(piece)
                chars += len(piece) + 1
                stamp = timestamp_of(piece, tz)
                if stamp is not None:
                    first_at = stamp if first_at is None else min(first_at, stamp)
                    last_at = stamp if last_at is None else max(last_at, stamp)
            if len(lines) >= opts.chunk_lines:
                chunk = flush(number)
                if chunk is not None:
                    yield chunk
        if lines:
            chunk = flush(number)
            if chunk is not None:
                yield chunk


def plan(path: Path, opts: Options, *, encoding: str = "utf-8-sig") -> tuple[int, int, int]:
    """(lines, chunks, characters) without touching the network or the database - what ``--dry-run`` prints."""
    chunks = chars = 0
    last = 0
    for chunk in iter_chunks(path, opts, encoding=encoding):
        chunks += 1
        chars += len(chunk.text)
        last = chunk.last_line
    return last, chunks, chars


async def _embed_with_retries(
    embedder: Embedder, texts: list[str], opts: Options, stats: Stats, sleep: Callable[[float], Awaitable[None]]
) -> list[list[float]]:
    attempt = 0
    while True:  # bounded: leaves by return or by the raise below after ``max_retries`` failures
        try:
            stats.embed_calls += 1
            vectors = await embedder.embed_documents(texts)
            if len(vectors) != len(texts):
                raise LLMError(f"embedder returned {len(vectors)} vectors for {len(texts)} texts")
            return vectors
        except LLMError as exc:
            if attempt >= opts.max_retries:
                raise
            pause = min(MAX_RETRY_PAUSE, max(opts.sleep, 1.0) * 2 ** (attempt + 1))
            attempt += 1
            stats.retries += 1
            log.warning("embedding call failed (%s); retry %d/%d in %.0f s", exc, attempt, opts.max_retries, pause)
            await sleep(pause)


async def import_history(
    db: Database,
    embedder: Embedder,
    tenant_id: uuid.UUID,
    path: Path,
    opts: Options,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    encoding: str = "utf-8-sig",
) -> Stats:
    """Embed and store the chunks of ``path`` for ``tenant_id``. Returns what happened; raises on the first unrecoverable failure (stored
    batches stay stored, so a re-run resumes)."""
    opts.validate()
    digest = file_digest(path)
    prefix = key_prefix(digest, opts)
    async with db.tenant_session(tenant_id) as session:
        stored = set(
            (await session.execute(select(EpisodicMemory.idempotency_key).where(EpisodicMemory.idempotency_key.like(prefix + "%")))).scalars()
        )
    run_at = datetime.now(UTC)
    stats = Stats()
    pending: list[Chunk] = []

    async def flush_batch() -> None:
        if not pending:
            return
        vectors = await _embed_with_retries(embedder, [c.text for c in pending], opts, stats, sleep)
        rows = [
            {
                "user_id": tenant_id,
                "persona_id": None,
                "idempotency_key": f"{prefix}{c.index}",
                "kind": EpisodeKind.dialogue_chunk,
                "content": c.text,
                "embedding": vector,
                "embedding_model": embedder.model,
                "occurred_from": c.first_at or run_at,
                "occurred_to": c.last_at or c.first_at or run_at,
                "source_message_ids": [],
                "importance": 3,  # raw history: below the nightly summaries' default of 5
            }
            for c, vector in zip(pending, vectors, strict=True)
        ]
        async with db.tenant_session(tenant_id) as session:
            await session.execute(pg_insert(EpisodicMemory).values(rows).on_conflict_do_nothing(constraint="uq_episodic_memories_user_idem"))
        stats.chunks_stored += len(pending)
        log.info("stored chunks up to line %d (%d new, %d skipped so far)", pending[-1].last_line, stats.chunks_stored, stats.chunks_skipped)
        pending.clear()
        await sleep(opts.sleep)  # the pause that keeps the Gemini rate limit intact

    for chunk in iter_chunks(path, opts, encoding=encoding):
        stats.chunks_total += 1
        stats.lines = chunk.last_line
        if f"{prefix}{chunk.index}" in stored:
            stats.chunks_skipped += 1
            continue
        pending.append(chunk)
        if opts.limit_chunks is not None and stats.chunks_stored + len(pending) >= opts.limit_chunks:
            stats.stopped_early = True
            break
        if len(pending) >= opts.batch:
            await flush_batch()
    await flush_batch()
    return stats


# ---------------------------------------------------------------- command line


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ai_civ.cli.import_history", description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("file", type=Path, help="text file with the history")
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--user-id", type=uuid.UUID, help="tenant id (civ.users.id)")
    who.add_argument("--telegram-user-id", type=int, help="the owner's Telegram user id")
    p.add_argument("--chunk-lines", type=int, default=Options.chunk_lines)
    p.add_argument("--max-chars", type=int, default=Options.max_chars, help="cap per chunk (embedder input limit); 0 = none")
    p.add_argument("--batch", type=int, default=Options.batch, help="chunks per embedding call")
    p.add_argument("--sleep", type=float, default=Options.sleep, help="seconds to wait after every embedding call")
    p.add_argument("--max-retries", type=int, default=Options.max_retries)
    p.add_argument("--timezone", default=Options.timezone, help="IANA zone of the timestamps found in the file")
    p.add_argument("--encoding", default="utf-8-sig")
    p.add_argument("--limit-chunks", type=int, default=None, help="store at most this many NEW chunks (a trial run)")
    p.add_argument("--dry-run", action="store_true", help="only count lines and chunks; no database, no Gemini")
    return p


async def _run(args: argparse.Namespace, opts: Options) -> int:
    async with runtime.open_runtime(True) as rt:  # RLS verified, Gemini key required
        embedder = rt.embedder
        assert embedder is not None
        tenant_id = args.user_id
        if args.telegram_user_id is not None:
            tenant_id = await rt.db.resolve_tenant_id(telegram_user_id=args.telegram_user_id)
        if tenant_id is None:
            print("error: no such tenant (the person is not registered)", file=sys.stderr)
            return 1
        async with rt.db.tenant_session(tenant_id) as session:  # RLS: a foreign or missing id sees no row
            if (await session.execute(sql("SELECT 1 FROM civ.users WHERE id = :u"), {"u": tenant_id})).first() is None:
                print("error: no such tenant", file=sys.stderr)
                return 1
        try:
            stats = await import_history(rt.db, embedder, tenant_id, args.file, opts, encoding=args.encoding)
        except LLMError as exc:
            print(
                f"error: embedding failed after {opts.max_retries} retries: {exc}\n"
                "stored batches are kept; run the same command again to resume.",
                file=sys.stderr,
            )
            return 1
    done = "stopped by --limit-chunks" if stats.stopped_early else "finished"
    print(f"{done}: {stats.chunks_stored} chunks stored, {stats.chunks_skipped} already there, {stats.embed_calls} embedding calls, {stats.retries} retries")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    opts = Options(args.chunk_lines, args.max_chars, args.batch, args.sleep, args.max_retries, args.timezone, args.limit_chunks)
    try:
        opts.validate()
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not args.file.is_file():
        print(f"error: {args.file} is not a file", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    lines, chunks, chars = plan(args.file, opts, encoding=args.encoding)
    print(f"{args.file.name}: {lines} lines -> {chunks} chunks ({chars} characters)")
    if args.dry_run:
        return 0
    try:
        return asyncio.run(_run(args, opts))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

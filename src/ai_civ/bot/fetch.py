"""Fetching a web page (or a YouTube transcript) for the summariser, SSRF-safe.

The URL comes from a user's message, so it is hostile input: it may point at the bot's own machine, at the cloud metadata service
(169.254.169.254), at a router or database in the LAN, or redirect there after a harmless first hop. The defence is layered and every layer
is enforced by CODE before a byte of the target is read:

  * only ``http://`` and ``https://``, no credentials in the URL, only ordinary web ports, no ``localhost`` / ``.local`` / ``.internal`` names;
  * the DNS answer is checked BEFORE any TCP connection: a custom aiohttp resolver resolves the name itself, refuses the whole host if ANY of its
    addresses is not a public one (loopback, RFC 1918, link-local, CGNAT, multicast, reserved, IPv6 local / multicast, IPv4-mapped and NAT64 /
    6to4 forms of those), and hands aiohttp the very addresses it checked - the connection goes to the validated IP, so a DNS-rebinding answer
    that changes between "check" and "connect" has no chance (there is no second lookup);
  * an IP literal in the URL is checked the same way (aiohttp does not resolve literals, so the URL check does it);
  * redirects are followed by hand (at most ``max_redirects``): every hop goes through the same URL check and the same resolver;
  * one total deadline for the whole chain, a cap on the downloaded bytes (counted AFTER decompression, so a zip bomb cannot slip through), and
    only text content types.

Text is extracted with ``trafilatura`` (in a worker thread: it is CPU work). Anything that goes wrong is a ``FetchError`` with a ``kind`` the caller
turns into a polite sentence: nothing here crashes the bot. ``policy`` exists for tests only (the tests need a loopback server, and the production
default is the strict one; no setting and no message can change it).
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import ResolveResult
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi
from youtube_transcript_api._errors import AgeRestricted, InvalidVideoId, VideoUnavailable, VideoUnplayable

log = logging.getLogger(__name__)

Kind = Literal[
    "bad_url", "blocked", "dns", "timeout", "too_large", "unsupported", "http_error", "redirects", "no_text", "no_transcript", "video_unavailable", "failed"
]

MAX_URL_CHARS = 2048
WEB_PORTS = frozenset({80, 443, 8080, 8443})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
TEXT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
BLOCKED_NAME_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".lan", ".home.arpa")
USER_AGENT = "Mozilla/5.0 (compatible; AICivilizationBot/1.0; +summary of a link the user sent)"
CHUNK = 64 * 1024
TRANSCRIPT_LANGUAGES = ("ru", "en", "da", "uk", "de")  # preferred; any other available track is used when none of these exists


class FetchError(Exception):
    """The content cannot be had; ``kind`` picks the sentence the user reads."""

    def __init__(self, kind: Kind, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind: Kind = kind


@dataclass(frozen=True)
class Page:
    url: str  # the final URL (after redirects) or the canonical YouTube URL
    title: str | None
    text: str
    truncated: bool = False  # the text was cut at ``max_chars``
    source: Literal["web", "youtube"] = "web"


@dataclass(frozen=True)
class Limits:
    timeout: float = 10.0  # ONE deadline for the whole chain of requests
    max_bytes: int = 2_000_000
    max_redirects: int = 3
    max_chars: int = 30_000  # of the extracted text given to the model
    min_chars: int = 100  # less than this is "no text"


# ---------------------------------------------------------------- addresses

_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
_TEREDO = ipaddress.ip_network("2001::/32")


def is_public_ip(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for a globally routable unicast address. Everything else (private, loopback, link-local, CGNAT, multicast, reserved,
    unspecified, documentation ranges) is refused; an IPv6 address that EMBEDS an IPv4 one (mapped, NAT64, 6to4, Teredo) is judged by what it hides."""
    try:
        ip = ipaddress.ip_address(value) if isinstance(value, str) else value
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_ip(ip.ipv4_mapped)
        if ip in _NAT64 or ip in _SIX_TO_FOUR or ip in _TEREDO:
            return False  # tunnels to an IPv4 address we cannot vouch for: refuse the whole range
    return ip.is_global and not ip.is_multicast and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified


@dataclass(frozen=True)
class Policy:
    """What may be reached. The default is the production policy; tests build a weaker one to talk to a loopback server."""

    ip_ok: Callable[[str], bool] = is_public_ip
    ports: frozenset[int] | None = WEB_PORTS  # None = any port
    lookup: Callable[[str, int], Awaitable[list[str]]] | None = None  # None = the system resolver


POLICY = Policy()


def check_url(url: str, policy: Policy = POLICY) -> str:
    """The URL as it will be requested, or ``FetchError`` (bad_url / blocked). Pure, no network."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_CHARS or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise FetchError("bad_url", "empty, too long or contains whitespace")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise FetchError("bad_url", str(exc)) from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise FetchError("bad_url", f"scheme {parts.scheme!r}")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host or parts.username is not None or parts.password is not None:
        raise FetchError("bad_url", "no host, or credentials in the URL")
    if policy.ports is not None and port is not None and port not in policy.ports:
        raise FetchError("blocked", f"port {port}")
    if host == "localhost" or host.endswith(BLOCKED_NAME_SUFFIXES):
        raise FetchError("blocked", "a local name")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return url  # a name: the resolver checks what it points at
    if not policy.ip_ok(str(literal)):
        raise FetchError("blocked", "a non-public IP address")
    return url


class PublicOnlyResolver(AbstractResolver):
    """Resolves a name and refuses the host when ANY answer is not public; aiohttp then connects to exactly the addresses returned here."""

    def __init__(self, policy: Policy = POLICY) -> None:
        self._policy = policy
        self.lookups: list[str] = []  # for the tests: how many times, for which names

    async def resolve(self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET) -> list[ResolveResult]:
        self.lookups.append(host)
        try:
            if self._policy.lookup is not None:
                addresses = await self._policy.lookup(host, port)
            else:
                infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
                addresses = list(dict.fromkeys(info[4][0] for info in infos))
        except (OSError, UnicodeError) as exc:
            raise FetchError("dns", str(exc)) from exc
        if not addresses:
            raise FetchError("dns", "no address")
        for address in addresses:
            if not self._policy.ip_ok(address):
                raise FetchError("blocked", f"{host} resolves to a non-public address")
        return [
            ResolveResult(
                hostname=host,
                host=address,
                port=port,
                family=socket.AF_INET6 if ":" in address else socket.AF_INET,
                proto=0,
                flags=socket.AI_NUMERICHOST,
            )
            for address in addresses
        ]

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------- the download


@dataclass
class _Body:
    url: str
    data: bytes
    charset: str | None
    content_type: str = ""
    hops: list[str] = field(default_factory=list)


async def _download(url: str, limits: Limits, policy: Policy, resolver: PublicOnlyResolver) -> _Body:
    connector = aiohttp.TCPConnector(resolver=resolver, use_dns_cache=False, force_close=True)
    timeout = aiohttp.ClientTimeout(total=limits.timeout)
    async with aiohttp.ClientSession(
        connector=connector, timeout=timeout, trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain;q=0.9"}
    ) as session:
        hops: list[str] = []
        for _ in range(limits.max_redirects + 1):
            check_url(url, policy)  # every hop, the first included
            hops.append(url)
            async with session.get(url, allow_redirects=False) as response:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise FetchError("http_error", "a redirect without a Location")
                    url = urljoin(url, location)
                    continue
                if not 200 <= response.status < 300:
                    raise FetchError("http_error", f"status {response.status}")
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if content_type and not content_type.startswith(TEXT_TYPES):
                    raise FetchError("unsupported", content_type)
                declared = response.headers.get("Content-Length", "")
                if declared.isdigit() and int(declared) > limits.max_bytes:
                    raise FetchError("too_large", f"declared {declared}")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.content.iter_chunked(CHUNK):
                    size += len(chunk)  # decompressed bytes: a bomb is stopped here
                    if size > limits.max_bytes:
                        raise FetchError("too_large", "more than the cap")
                    chunks.append(chunk)
                return _Body(url, b"".join(chunks), response.charset, content_type, hops)
    raise FetchError("redirects", f"more than {limits.max_redirects}")


_CTRL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u202a-\u202e\u2066-\u2069\u200b-\u200d\ufeff]")
_BLANKS = re.compile(r"\n{3,}")


def clean_text(value: str) -> str:
    value = _CTRL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    return _BLANKS.sub("\n\n", value).strip()


def _extract(html: str) -> tuple[str | None, str | None]:
    """(text, title) by trafilatura; CPU-bound, called in a thread. (None, None) = nothing readable."""
    import trafilatura

    text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True)
    meta = trafilatura.extract_metadata(html)
    title = getattr(meta, "title", None) if meta is not None else None
    return text, title


def _decode(body: _Body) -> str:
    for encoding in (body.charset, "utf-8"):
        if encoding:
            try:
                return body.data.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
    return body.data.decode("utf-8", errors="replace")


def _fit(text: str, limits: Limits) -> tuple[str, bool]:
    return (text[: limits.max_chars].rstrip(), True) if len(text) > limits.max_chars else (text, False)


async def fetch_page(url: str, *, limits: Limits = Limits(), policy: Policy = POLICY, resolver: PublicOnlyResolver | None = None) -> Page:
    """Download and clean one web page. Raises ``FetchError`` only."""
    check_url(url, policy)
    resolver = resolver or PublicOnlyResolver(policy)
    try:
        body = await asyncio.wait_for(_download(url, limits, policy, resolver), timeout=limits.timeout)
    except FetchError:
        raise
    except TimeoutError as exc:
        raise FetchError("timeout") from exc
    except aiohttp.ClientConnectorDNSError as exc:
        raise FetchError("dns", str(exc)) from exc
    except (aiohttp.ClientError, OSError, UnicodeError) as exc:
        if isinstance(exc, asyncio.TimeoutError):
            raise FetchError("timeout") from exc
        raise FetchError("failed", type(exc).__name__) from exc
    html = _decode(body)
    if body.content_type == "text/plain":
        text, title = clean_text(html), None
    else:
        extracted, title = await asyncio.to_thread(_extract, html)
        text = clean_text(extracted or "")
    if len(text) < limits.min_chars:
        raise FetchError("no_text", f"{len(text)} characters")
    text, truncated = _fit(text, limits)
    return Page(body.url, clean_text(title)[:200] if title else None, text, truncated)


# ---------------------------------------------------------------- YouTube

YOUTUBE_HOSTS = frozenset({"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "www.youtube-nocookie.com", "youtu.be"})
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_ID_PATHS = ("/shorts/", "/embed/", "/live/", "/v/")


def youtube_video_id(url: str) -> str | None:
    """The 11-character id of a YouTube link (youtube.com / youtu.be, strict host match: ``youtube.com.evil.example`` is not YouTube)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme.lower() not in ("http", "https") or host not in YOUTUBE_HOSTS:
        return None
    candidate: str | None = None
    if host == "youtu.be":
        candidate = parts.path.lstrip("/").split("/")[0]
    elif parts.path == "/watch":
        candidate = (parse_qs(parts.query).get("v") or [""])[0]
    else:
        for prefix in _ID_PATHS:
            if parts.path.startswith(prefix):
                candidate = parts.path[len(prefix):].split("/")[0]
    return candidate if candidate and _VIDEO_ID.match(candidate) else None


def _transcript_blocking(video_id: str) -> str:
    """The transcript text (the library is synchronous, so this runs in a thread). Raises the library's own exceptions."""
    transcripts = YouTubeTranscriptApi().list(video_id)
    try:
        track = transcripts.find_transcript(list(TRANSCRIPT_LANGUAGES))
    except NoTranscriptFound:
        track = next(iter(transcripts), None)
        if track is None:
            raise
    return " ".join(snippet.text.strip() for snippet in track.fetch() if snippet.text.strip())


async def fetch_transcript(video_id: str, *, limits: Limits = Limits()) -> Page:
    """The spoken text of a YouTube video. No subtitles / unavailable video / blocked request: a ``FetchError`` with a polite ``kind``."""
    if not _VIDEO_ID.match(video_id):
        raise FetchError("bad_url", "not a video id")
    try:
        raw = await asyncio.wait_for(asyncio.to_thread(_transcript_blocking, video_id), timeout=limits.timeout)
    except TimeoutError as exc:
        raise FetchError("timeout") from exc
    except (TranscriptsDisabled, NoTranscriptFound) as exc:
        raise FetchError("no_transcript", type(exc).__name__) from exc
    except (VideoUnavailable, VideoUnplayable, AgeRestricted, InvalidVideoId) as exc:
        raise FetchError("video_unavailable", type(exc).__name__) from exc
    except Exception as exc:  # noqa: BLE001 - the library raises many things (blocked IP, parse errors): none of them may crash the bot
        log.warning("could not get the transcript of %s: %r", video_id, exc)
        raise FetchError("failed", type(exc).__name__) from exc
    text = clean_text(raw)
    if not text:
        raise FetchError("no_transcript", "empty")
    text, truncated = _fit(text, limits)
    return Page(f"https://www.youtube.com/watch?v={video_id}", None, text, truncated, source="youtube")


async def fetch_content(url: str, *, limits: Limits = Limits(), policy: Policy = POLICY) -> Page:
    """A YouTube link -> its transcript; any other URL -> the cleaned text of the page. Raises ``FetchError`` only."""
    video_id = youtube_video_id(url)
    if video_id is not None:
        return await fetch_transcript(video_id, limits=limits)
    return await fetch_page(url, limits=limits, policy=policy)


def limits_from(settings: Any) -> Limits:
    return Limits(
        timeout=settings.fetch_timeout_seconds,
        max_bytes=settings.fetch_max_bytes,
        max_redirects=settings.fetch_max_redirects,
        max_chars=settings.summary_max_chars,
        min_chars=settings.fetch_min_text_chars,
    )

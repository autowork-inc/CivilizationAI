"""Invite links ("digital handshake", Module 5 step 2): the token and the thin layer over the SECURITY DEFINER functions of
migration 0005 (``civ.create_invite`` / ``peek_invite`` / ``accept_invite`` / ``revoke_invites``).

The token is a bearer secret: 128 random bits, shown only inside the link (``t.me/<bot>?start=connect_<token>``). The database never
sees it: only its SHA-256 is sent and stored, so a leaked table gives no working link. Every function here runs inside a
``tenant_session`` of the CALLER; who the caller is is decided by the database from that session, never by an argument.
"""

from __future__ import annotations

import enum
import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ai_civ.bot import texts
from ai_civ.db.models import CoreFact

PAYLOAD_PREFIX = "connect_"
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{22}")  # token_urlsafe(16): 16 random bytes, base64url without padding
MAX_NAME_CHARS = 40  # the database limit too (civ.is_clean_name)

# Everything that could make a name look like something else: control characters, zero-width / bidi marks and overrides.
_UNSAFE = re.compile("[\x00-\x1f\x7f-\x9f​-‏‪-‮⁠-⁩﻿]")


class InviteState(enum.StrEnum):
    """What ``peek_invite`` says. ``invalid`` deliberately covers unknown / used / revoked / expired / "the inviter blocked you"."""

    ok = "ok"
    own = "own"
    already_connected = "already_connected"
    not_ready = "not_ready"
    invalid = "invalid"


class HandshakeOutcome(enum.StrEnum):
    connected = "connected"
    already_connected = "already_connected"
    invalid = "invalid"
    own = "own"
    not_ready = "not_ready"


class CreateOutcome(enum.StrEnum):
    created = "created"
    not_ready = "not_ready"
    too_many = "too_many"


# ------------------------------------------------------------------ pure


def new_token() -> str:
    return secrets.token_urlsafe(16)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def deep_link(bot_username: str, token: str) -> str:
    return f"https://t.me/{bot_username}?start={PAYLOAD_PREFIX}{token}"


def token_from_payload(payload: str | None) -> str | None:
    """The token inside ``connect_<token>``; None for anything else (a malformed link is told apart from a dead one by the caller)."""
    if not payload or not payload.startswith(PAYLOAD_PREFIX):
        return None
    token = payload[len(PAYLOAD_PREFIX) :]
    return token if TOKEN_RE.fullmatch(token) else None


def clean_display_name(raw: str | None) -> str | None:
    """A name that is safe to show to another person: one line, no control / bidi characters, at most 40 characters.
    None when nothing is left."""
    if not raw:
        return None
    cleaned = " ".join(_UNSAFE.sub(" ", raw).split())
    if len(cleaned) > MAX_NAME_CHARS:
        cleaned = cleaned[:MAX_NAME_CHARS].rstrip()
    return cleaned or None


def check_name(raw: str) -> str:
    """A name the user TYPED (an alias in their address book): whitespace is tidied, anything else wrong is refused, not silently
    changed (``clean_display_name`` is for names taken from elsewhere). Raises ValueError with a message meant for the user."""
    cleaned = " ".join(raw.split())
    if not cleaned:
        raise ValueError("имя не может быть пустым")
    if len(cleaned) > MAX_NAME_CHARS:
        raise ValueError(f"имя не длиннее {MAX_NAME_CHARS} знаков")
    if _UNSAFE.search(cleaned):
        raise ValueError("в имени есть недопустимые символы")
    return cleaned


# ------------------------------------------------------------------ database (all in the caller's tenant session)


@dataclass(frozen=True)
class Created:
    outcome: CreateOutcome
    invite_id: uuid.UUID | None
    expires_at: datetime | None


@dataclass(frozen=True)
class Peeked:
    state: InviteState
    invite_id: uuid.UUID | None
    inviter_name: str | None


@dataclass(frozen=True)
class Accepted:
    outcome: HandshakeOutcome
    contact_id: uuid.UUID | None
    inviter_name: str | None


async def my_display_name(session: AsyncSession, fallback: str | None = None) -> str:
    """How the caller introduces themself to a person they connect with: the way they asked to be addressed (Core fact
    ``address_as``), else their name, else ``fallback`` (the Telegram first name). Shown to the other side BEFORE they decide."""
    rows = (
        await session.execute(
            select(CoreFact.key, CoreFact.value_text).where(
                CoreFact.key.in_(("address_as", "name")), CoreFact.valid_to.is_(None)
            )
        )
    ).all()
    known = {key: value for key, value in rows}
    for candidate in (known.get("address_as"), known.get("name"), fallback):
        cleaned = clean_display_name(candidate)
        if cleaned:
            return cleaned
    return texts.DEFAULT_CONTACT_NAME


async def create_invite(session: AsyncSession, *, token_hash: bytes, display_name: str, ttl: timedelta) -> Created:
    row = (
        await session.execute(
            text(
                "SELECT out_outcome, out_invite_id, out_expires_at "
                "FROM civ.create_invite(CAST(:h AS bytea), :n, CAST(:ttl AS interval))"
            ),
            {"h": token_hash, "n": display_name, "ttl": ttl},
        )
    ).one()
    return Created(CreateOutcome(row[0]), row[1], row[2])


async def revoke_invites(session: AsyncSession) -> int:
    return (await session.execute(text("SELECT civ.revoke_invites()"))).scalar_one()


async def revoke_invite(session: AsyncSession, invite_id: uuid.UUID) -> bool:
    """Revoke one open invite of the caller; False when there was nothing to revoke (unknown, used, revoked, expired)."""
    return (await session.execute(text("SELECT civ.revoke_invite(:i)"), {"i": invite_id})).scalar_one()


async def peek_invite(session: AsyncSession, token_hash: bytes) -> Peeked:
    row = (
        await session.execute(
            text("SELECT out_state, out_invite_id, out_inviter_name FROM civ.peek_invite(CAST(:h AS bytea))"),
            {"h": token_hash},
        )
    ).one()
    return Peeked(InviteState(row[0]), row[1], row[2])


async def accept_invite(session: AsyncSession, invite_id: uuid.UUID, display_name: str) -> Accepted:
    row = (
        await session.execute(
            text("SELECT out_outcome, out_contact_id, out_inviter_name FROM civ.accept_invite(:i, :n)"),
            {"i": invite_id, "n": display_name},
        )
    ).one()
    return Accepted(HandshakeOutcome(row[0]), row[1], row[2])

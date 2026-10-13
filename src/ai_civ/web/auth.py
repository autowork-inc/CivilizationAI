"""Validation of the launch data (``initData``) a Telegram Mini App hands to its page.

The ONLY thing the backend trusts about who is calling is a valid signature over this string; nothing else the browser sends
(a user id in a JSON body, a header, a query parameter) is ever believed. Algorithm (core.telegram.org/bots/webapps):

  * ``initData`` is a URL query string; ``hash`` is one of its fields;
  * data-check-string = every OTHER field as ``key=value``, sorted by key, joined with a line feed (``signature``, the field for
    third-party Ed25519 validation, stays in: only ``hash`` is excluded);
  * secret key = HMAC-SHA256(key = b"WebAppData", message = bot token);
  * valid  <=>  hex(HMAC-SHA256(secret key, data-check-string)) == hash  (compared in constant time).
``auth_date`` (Unix time the data was issued) is additionally bounded, so a leaked string stops working.

Pure module: no I/O, no FastAPI. Everything wrong raises ``InitDataError`` with a machine-readable ``code``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

CLOCK_SKEW_SECONDS = 60  # an auth_date a little in the future is a clock difference, not an attack
MAX_INIT_DATA_CHARS = 8192  # real strings are ~600 bytes; refuse to HMAC megabytes


class InitDataError(Exception):
    """The launch data cannot be trusted. ``code`` is stable and safe to show to the client; the details are not."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class InitData:
    telegram_user_id: int
    first_name: str
    auth_date: int
    chat_type: str | None  # how the Mini App was opened: "sender" / "private" / "group" / "supergroup" / "channel"
    start_param: str | None


def secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def data_check_string(fields: dict[str, str]) -> str:
    return "\n".join(f"{key}={fields[key]}" for key in sorted(fields) if key != "hash")


def sign(fields: dict[str, str], bot_token: str) -> str:
    """The hash Telegram would put on ``fields`` (used by the validator and by the tests that play Telegram)."""
    return hmac.new(secret_key(bot_token), data_check_string(fields).encode(), hashlib.sha256).hexdigest()


def validate_init_data(raw: str, bot_token: str, *, max_age_seconds: int, now: float | None = None) -> InitData:
    if not raw or len(raw) > MAX_INIT_DATA_CHARS:
        raise InitDataError("init_data_missing" if not raw else "init_data_too_large")
    pairs = parse_qsl(raw, keep_blank_values=True, max_num_fields=64)
    fields = dict(pairs)
    if len(fields) != len(pairs):  # a repeated key could make two parsers disagree about what was signed
        raise InitDataError("init_data_malformed", "duplicate field")
    received = fields.get("hash")
    if not received:
        raise InitDataError("init_data_unsigned")
    if not hmac.compare_digest(sign(fields, bot_token), received.lower()):
        raise InitDataError("init_data_bad_signature")

    # From here on the data is authentic (Telegram signed exactly this), so parse errors mean a bug on the sender's side.
    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, ValueError):
        raise InitDataError("init_data_malformed", "auth_date") from None
    age = (time.time() if now is None else now) - auth_date
    if age > max_age_seconds:
        raise InitDataError("init_data_expired")
    if age < -CLOCK_SKEW_SECONDS:
        raise InitDataError("init_data_from_the_future")
    try:
        user = json.loads(fields["user"])
        user_id = user["id"]
        first_name = user.get("first_name", "")
    except (KeyError, TypeError, ValueError, AttributeError):
        raise InitDataError("init_data_malformed", "user") from None
    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0 or not isinstance(first_name, str):
        raise InitDataError("init_data_malformed", "user")
    return InitData(
        telegram_user_id=user_id,
        first_name=first_name,
        auth_date=auth_date,
        chat_type=fields.get("chat_type") or None,
        start_param=fields.get("start_param") or None,
    )

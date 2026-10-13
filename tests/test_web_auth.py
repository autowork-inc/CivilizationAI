"""Mini App launch data (initData): the signature is the only thing the backend believes about the caller. Pure logic."""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import pytest

from ai_civ.web import auth
from ai_civ.web.auth import InitDataError, validate_init_data

TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"  # shape only
MAX_AGE = 3600


def make_init_data(
    *, user_id: int = 4242, token: str = TOKEN, age: int = 10, extra: dict[str, str] | None = None, user: str | None = None
) -> str:
    """What Telegram would hand to the page: the fields plus the hash it computes with the bot token."""
    fields = {
        "auth_date": str(int(time.time()) - age),
        "query_id": "AAH-test",
        "user": json.dumps({"id": user_id, "first_name": "Аня", "language_code": "ru"}, ensure_ascii=False)
        if user is None
        else user,
        "chat_type": "supergroup",
        "chat_instance": "-123456",
        **(extra or {}),
    }
    fields["hash"] = auth.sign(fields, token)
    return urlencode(fields)


def check(raw: str, **kw):
    return validate_init_data(raw, TOKEN, max_age_seconds=kw.pop("max_age_seconds", MAX_AGE), **kw)


def rejected(raw: str, code: str, **kw) -> None:
    with pytest.raises(InitDataError) as caught:
        check(raw, **kw)
    assert caught.value.code == code


def test_a_correctly_signed_string_yields_the_user_and_launch_context():
    data = check(make_init_data(user_id=777, extra={"start_param": "settings"}))
    assert (data.telegram_user_id, data.first_name, data.chat_type, data.start_param) == (777, "Аня", "supergroup", "settings")


def test_the_algorithm_is_the_documented_one():
    """Recomputed independently from the documentation: secret = HMAC(b"WebAppData", token); hash over the sorted fields."""
    import hashlib
    import hmac

    fields = {"auth_date": "1700000000", "user": '{"id":1}', "query_id": "q"}
    expected = hmac.new(
        hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest(),
        b"auth_date=1700000000\nquery_id=q\nuser={\"id\":1}",
        hashlib.sha256,
    ).hexdigest()
    assert auth.sign(fields, TOKEN) == expected


def test_the_signature_field_stays_in_the_check_string_only_hash_is_excluded():
    fields = {"auth_date": "1", "signature": "abc", "hash": "ignored"}
    assert auth.data_check_string(fields) == "auth_date=1\nsignature=abc"


def test_a_signature_made_with_another_token_is_rejected():
    rejected(make_init_data(token="987654321:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"), "init_data_bad_signature")


def test_changing_any_signed_field_breaks_the_signature():
    raw = make_init_data(user_id=1)
    forged = raw.replace("%22id%22%3A+1", "%22id%22%3A+2")  # the user id, as it appears URL-encoded
    assert forged != raw
    rejected(forged, "init_data_bad_signature")
    rejected(raw + "&extra=1", "init_data_bad_signature")  # an added field is not covered by the hash either


def test_an_unsigned_or_empty_string_is_rejected():
    rejected("", "init_data_missing")
    rejected(f"auth_date={int(time.time())}&user=%7B%22id%22%3A1%7D", "init_data_unsigned")
    rejected("hash=", "init_data_unsigned")


def test_a_repeated_field_is_refused_so_two_parsers_cannot_disagree_about_what_was_signed():
    rejected(make_init_data() + "&auth_date=1", "init_data_malformed")


def test_an_oversized_string_is_refused_before_it_is_hashed():
    rejected("a=" + "x" * (auth.MAX_INIT_DATA_CHARS + 1), "init_data_too_large")


def test_old_data_expires_and_the_boundary_is_exact():
    now = time.time()
    rejected(make_init_data(age=MAX_AGE + 5), "init_data_expired", now=now)
    assert check(make_init_data(age=MAX_AGE - 5), now=now)


def test_data_from_the_future_is_refused_beyond_a_small_clock_skew():
    rejected(make_init_data(age=-3600), "init_data_from_the_future")
    assert check(make_init_data(age=-10))  # a few seconds of clock difference is fine


@pytest.mark.parametrize(
    "user",
    ["not json", "[]", '{"first_name": "x"}', '{"id": "5"}', '{"id": 0}', '{"id": -3}', '{"id": true}', '{"id": 5, "first_name": 9}'],
)
def test_a_signed_but_malformed_user_is_rejected(user):
    rejected(make_init_data(user=user), "init_data_malformed")


def test_a_signed_string_without_a_valid_auth_date_is_rejected():
    fields = {"user": '{"id": 5}', "auth_date": "yesterday"}
    fields["hash"] = auth.sign(fields, TOKEN)
    rejected(urlencode(fields), "init_data_malformed")
    fields = {"user": '{"id": 5}'}
    fields["hash"] = auth.sign(fields, TOKEN)
    rejected(urlencode(fields), "init_data_malformed")


def test_hash_comparison_is_case_insensitive_hex_like_telegrams():
    raw = make_init_data()
    fields = dict(p.split("=", 1) for p in raw.split("&"))
    upper = raw.replace(fields["hash"], fields["hash"].upper())
    assert check(upper).telegram_user_id == 4242


def test_an_empty_optional_launch_param_is_none():
    data = check(make_init_data(extra={"start_param": ""}))
    assert data.start_param is None

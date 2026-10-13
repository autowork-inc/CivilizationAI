"""Module 5, step 5 in the bot: ``propose_event`` (the model asks, the code validates and the database decides), the card the recipient gets,
the Accept / Decline buttons and the neutral note the author gets back. Real PostgreSQL, the real aiogram Dispatcher on a fake Telegram
wire, a fake Gemini whose "model" decides in the test what to call. The point under test: whatever the user reads about an invitation is
written by CODE from what the database said - a model that claims "sent" changes nothing."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ai_civ.bot import diplomat, proposals, texts
from ai_civ.bot.diplomat import ProposalOutcome, RosterEntry
from ai_civ.bot.notifications import Notifier
from ai_civ.bot.proposals import Decision, ProposalService
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolReply
from tests.helpers import new_persona, new_user, owner_exec
from tests.test_diplomat_bot import FREE, befriend, call, two_days
from tests.tg_fakes import make_callback_update

CPH = "Europe/Copenhagen"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)  # a Saturday; 14:00 in Copenhagen


def make_roster() -> list[RosterEntry]:
    return [RosterEntry("c1", uuid.uuid4(), "Саша"), RosterEntry("c2", uuid.uuid4(), "Оля")]


def invite_call(contact: str = "c1", title: str = "Шахматы", start: str = "2026-10-05T19:00", **kw: Any) -> ToolCall:
    return ToolCall(diplomat.PROPOSE_NAME, {"contact": contact, "title": title, "start": start, **kw})


# ================================================================== pure: the call is validated by code


def test_the_tool_asks_for_a_contact_a_title_and_a_local_start_and_is_plain_json():
    schema = diplomat.PROPOSE_TOOL.parameters
    assert set(schema["required"]) == {"contact", "title", "start"}
    assert "enum" not in schema["properties"]["contact"]  # an enum would force a wrong person onto "someone I do not know"
    assert "no time zone suffix" in schema["properties"]["start"]["description"]
    assert json.dumps(schema)
    assert diplomat.TOOLS == [diplomat.TOOL, diplomat.PROPOSE_TOOL]


def test_a_valid_invitation_is_normalised_into_an_instant_by_code():
    roster = make_roster()
    p = diplomat.parse_proposal(
        {"contact": " C1 ", "title": "  Шахматы   вечером ", "start": "2026-10-05T19:00", "duration_minutes": 90}, roster, CPH, NOW
    )
    assert p is not None and p.entry == roster[0] and p.title == "Шахматы вечером"
    assert p.start == datetime(2026, 10, 5, 17, 0, tzinfo=UTC)  # 19:00 on the USER's wall clock (UTC+2 that day)
    assert p.end - p.start == timedelta(minutes=90)
    assert diplomat.parse_proposal({"contact": "c2", "title": "x", "start": "2026-10-05 19:00"}, roster, "UTC", NOW).start == datetime(
        2026, 10, 5, 19, 0, tzinfo=UTC
    )  # a space instead of T is fine, and the zone is the user's own


@pytest.mark.parametrize("given", ["2026-10-05T19:00:00+02:00", "2026-10-05T17:00Z", "2026-10-05T17:00+00:00"])
def test_a_start_that_carries_its_own_zone_is_converted_not_reinterpreted(given):
    p = diplomat.parse_proposal({"contact": "c1", "title": "x", "start": given}, make_roster(), "Asia/Tokyo", NOW)
    assert p is not None and p.start == datetime(2026, 10, 5, 17, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("given", "minutes"),
    [(None, 60), (120, 120), (120.0, 120), ("45", 45), (15, 15), (1440, 1440)],
)
def test_the_duration_defaults_and_accepts_numbers_in_any_json_shape(given, minutes):
    args = {"contact": "c1", "title": "x", "start": "2026-10-05T19:00", "duration_minutes": given}
    p = diplomat.parse_proposal(args, make_roster(), CPH, NOW)
    assert p is not None and p.end - p.start == timedelta(minutes=minutes)


@pytest.mark.parametrize("given", [14, 1441, 0, -60, True, False, "abc", "1.5", 1.5, [], {"m": 1}])
def test_an_unreadable_or_out_of_range_duration_is_malformed(given):
    args = {"contact": "c1", "title": "x", "start": "2026-10-05T19:00", "duration_minutes": given}
    assert diplomat.parse_proposal(args, make_roster(), CPH, NOW) is None


@pytest.mark.parametrize("given", ["2026-10-05", "завтра", "19:00", "tomorrow 19:00", "2026-13-45T10:00", 20261005, None, ["2026-10-05T19:00"]])
def test_a_start_without_a_date_and_a_time_is_malformed(given):
    assert diplomat.parse_proposal({"contact": "c1", "title": "x", "start": given}, make_roster(), CPH, NOW) is None


def test_the_start_must_be_in_the_future_and_within_a_year():
    def parse(start: str):
        return diplomat.parse_proposal({"contact": "c1", "title": "x", "start": start}, make_roster(), CPH, NOW)

    assert parse("2026-10-03T13:59") is None  # in the past (11:59 UTC; now is 12:00)
    assert parse("2026-10-03T14:00") is None  # right now: gone by the time the recipient sees it
    assert parse("2026-10-03T14:02") is not None
    assert parse("2027-10-02T19:00") is not None and parse("2027-10-05T19:00") is None


@pytest.mark.parametrize("given", ["", "   ", None, 5, ["x"], "x" * 81, "bidi‮evil", "bell\x07", "tab⁦x"])
def test_an_unusable_title_is_malformed(given):
    assert diplomat.parse_proposal({"contact": "c1", "title": given, "start": "2026-10-05T19:00"}, make_roster(), CPH, NOW) is None


def test_a_title_is_collapsed_to_one_line_and_may_be_eighty_characters_long():
    p = diplomat.parse_proposal({"contact": "c1", "title": "two\nlines\there", "start": "2026-10-05T19:00"}, make_roster(), CPH, NOW)
    assert p is not None and p.title == "two lines here"
    p = diplomat.parse_proposal({"contact": "c1", "title": "я" * 80, "start": "2026-10-05T19:00"}, make_roster(), CPH, NOW)
    assert p is not None and len(p.title) == 80


@pytest.mark.parametrize("contact", ["none", "c9", "", "Саша", None, 7, {"x": 1}])
def test_a_person_who_is_not_in_the_list_is_found_before_anything_else_is_judged(contact):
    p = diplomat.parse_proposal({"contact": contact, "title": "", "start": "junk"}, make_roster(), CPH, NOW)
    assert p is not None and p.entry is None


async def test_malformed_and_stranger_calls_never_touch_the_database_and_are_capped():
    class NoDb:
        pass

    engine = diplomat.Diplomat(NoDb(), Settings(_env_file=None))  # type: ignore[arg-type]
    bad = [invite_call(start="soon") for _ in range(5)]
    outcomes = await engine.propose(uuid.uuid4(), CPH, make_roster(), bad, now=NOW)
    assert [o.kind for o in outcomes] == ["bad_input"] * 3  # DIPLOMAT_MAX_CALLS = 3
    outcomes = await engine.propose(uuid.uuid4(), CPH, make_roster(), [invite_call("none"), ToolCall("ask_contact", {})], now=NOW)
    assert [o.kind for o in outcomes] == ["no_contact"]  # a function that is not ours is not an invitation


# ================================================================== pure: what the user reads


def test_when_is_worded_in_the_readers_own_zone_and_names_the_day():
    def when(start: datetime, hours: float = 1, tz: str = CPH) -> str:
        return texts.when_text(start, start + timedelta(hours=hours), tz, NOW)

    assert when(datetime(2026, 10, 3, 17, 0, tzinfo=UTC)) == "сегодня, 19:00–20:00"
    assert when(datetime(2026, 10, 4, 17, 0, tzinfo=UTC)) == "завтра, 19:00–20:00"
    assert when(datetime(2026, 10, 10, 17, 0, tzinfo=UTC), 1.5) == "сб, 10 окт, 19:00–20:30"
    assert when(datetime(2026, 10, 5, 21, 0, tzinfo=UTC), 2) == "пн, 5 окт, 23:00 – 01:00 (на следующий день)"
    assert when(datetime(2026, 10, 3, 17, 0, tzinfo=UTC), 1, "Asia/Tokyo") == "завтра, 02:00–03:00"  # the same instant, somebody else's clock


def outcome(kind: str, alias: str | None = "Саша") -> ProposalOutcome:
    start = datetime(2026, 10, 10, 17, 0, tzinfo=UTC)
    return ProposalOutcome(kind, alias, "Шахматы", start, start + timedelta(hours=1))  # type: ignore[arg-type]


def test_a_confirmation_states_what_the_database_said_and_when_in_the_users_zone():
    assert diplomat.render_proposals([outcome("proposed")], CPH, now=NOW) == texts.PROPOSE_SENT.format(
        name="Саша", title="Шахматы", when="сб, 10 окт, 19:00–20:00"
    )
    assert diplomat.render_proposals([outcome("already_proposed")], CPH, now=NOW) == texts.PROPOSE_ALREADY.format(
        name="Саша", title="Шахматы", when="сб, 10 окт, 19:00–20:00"
    )


def test_a_refusal_never_says_why_and_identical_refusals_collapse():
    for kind, sentence in [
        ("unavailable", texts.PROPOSE_UNAVAILABLE), ("too_many", texts.PROPOSE_TOO_MANY), ("bad_input", texts.PROPOSE_BAD_INPUT),
        ("no_contact", texts.DIPLOMAT_NO_CONTACT), ("error", texts.DIPLOMAT_TRY_LATER),
    ]:  # fmt: skip
        assert diplomat.render_proposals([outcome(kind, None)], CPH, now=NOW) == sentence
    assert diplomat.render_proposals([outcome("unavailable", "Саша"), outcome("unavailable", "Саша")], CPH, now=NOW) == f"Саша: {texts.PROPOSE_UNAVAILABLE}"
    mixed = diplomat.render_proposals([outcome("proposed", "Саша"), outcome("unavailable", "Оля")], CPH, now=NOW)
    assert mixed.endswith(f"Оля: {texts.PROPOSE_UNAVAILABLE}") and texts.PROPOSE_SENT.split("\n")[0].split("{")[0] in mixed


def test_a_button_carries_a_short_checked_payload():
    pid = uuid.uuid4()
    for accept in (True, False):
        data = proposals.callback_data(pid, accept)
        assert len(data.encode()) == 41 <= 64
        assert proposals.parse_callback(data) == (pid, accept)
    for bad in ("", "ep:", f"ep:{pid}", f"ep:{pid}:x", f"ep:{pid}:y:z", "ep:not-a-uuid:y", f"rv:{pid}:y", f"ep:{str(pid).upper()}:y"):
        assert proposals.parse_callback(bad) is None


def test_the_final_text_is_the_state_the_database_reported():
    start = datetime(2026, 10, 10, 17, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    for result, template in [
        ("accepted", texts.PROPOSAL_ACCEPTED), ("already_accepted", texts.PROPOSAL_ACCEPTED),
        ("declined", texts.PROPOSAL_DECLINED), ("already_declined", texts.PROPOSAL_DECLINED), ("expired", texts.PROPOSAL_EXPIRED),
    ]:  # fmt: skip
        got = proposals.final_text(Decision(result, "Шахматы", start, end), "UTC")
        assert got == template.format(title="Шахматы", when=texts.when_text(start, end, "UTC"))
    assert proposals.final_text(Decision("unavailable"), "UTC") == texts.PROPOSAL_GONE  # not addressed to this tenant: nothing is revealed
    assert proposals.final_text(Decision("accepted"), "UTC") == texts.PROPOSAL_GONE


# ================================================================== through the real Dispatcher


async def person(env, user_id: uuid.UUID) -> tuple[int, int]:
    rows = await owner_exec(env.owner_engine, "SELECT telegram_user_id, forum_chat_id FROM civ.users WHERE id = :u", u=user_id)
    return rows[0][0], rows[0][1]


async def tz_of(env, user_id: uuid.UUID) -> str:
    return (await owner_exec(env.owner_engine, "SELECT timezone FROM civ.users WHERE id = :u", u=user_id))[0][0]


async def pair(env, *, granted: str | None = None) -> tuple[uuid.UUID, int, int]:
    """The bot's user A and a connected person B (A calls B «Саша», B calls A «Алиса»). Inviting needs NO grant. -> (B, B's telegram id, B's chat)."""
    other = await befriend(env, "Саша", granted=granted)
    return (other, *await person(env, other))


def tomorrow_at(tz: str, hour: int = 19) -> tuple[str, datetime, datetime]:
    """The wall-clock string the model would pass, and the instants the code must derive from it (one hour long)."""
    local = (datetime.now(ZoneInfo(tz)) + timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
    start = local.astimezone(UTC)
    return local.strftime("%Y-%m-%dT%H:%M"), start, start + timedelta(hours=1)


def says(env, chat_id: int, thread_id: int | None = None) -> list[Any]:
    return [m for m in env.session.sent() if m.chat_id == chat_id and m.message_thread_id == thread_id]


def invites(env, *calls: ToolCall, text: str = "Написал Саше, жду ответа!", then: str = "(must never be sent)") -> None:
    """The fake model calls the function(s) - and, as the real one might, ALSO claims it has written to the person."""
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


async def proposals_of(env) -> list[tuple[Any, ...]]:
    return await owner_exec(
        env.owner_engine,
        "SELECT status, title, starts_at, ends_at, id FROM civ.event_proposals WHERE user_id = :u ORDER BY created_at, id",
        u=env.user_id,
    )


async def outbox(env, user_id: uuid.UUID) -> list[tuple[str, str]]:
    rows = await owner_exec(
        env.owner_engine,
        "SELECT kind, status::text FROM civ.notification_outbox WHERE user_id = :u AND kind LIKE 'event\\_proposal%' ORDER BY id",
        u=user_id,
    )
    return [(r[0], r[1]) for r in rows]


async def meetings(env, *users: uuid.UUID) -> list[tuple[Any, ...]]:
    return await owner_exec(
        env.owner_engine,
        "SELECT user_id, title, timezone, visibility::text FROM civ.events WHERE created_via = 'peer_proposal' AND user_id = ANY(:u) ORDER BY user_id",
        u=list(users),
    )


async def press(env, tg: int, chat_id: int, data: str, *, thread_id: int | None = None, message_id: int = 555) -> None:
    await env.dispatcher.feed_update(
        env.bot, make_callback_update(data=data, user_id=tg, chat_id=chat_id, message_id=message_id, thread_id=thread_id)
    )


async def invited(env, other: uuid.UUID) -> tuple[uuid.UUID, datetime, datetime]:
    """A asks the bot to invite B to chess tomorrow at 19:00 and B's card is delivered. -> (proposal id, start, end)."""
    start_text, start, end = tomorrow_at(await tz_of(env, env.user_id))
    invites(env, invite_call(start=start_text))
    await env.feed("Позови Сашу в шахматы завтра в 19:00")
    [(_, _, _, _, pid)] = await proposals_of(env)
    assert await Notifier(env.db, env.bot, env.settings).run_tenant(other) == 1
    return pid, start, end


async def test_the_confirmation_is_written_by_code_and_the_models_own_claim_is_dropped(bot_env):
    env = bot_env
    other, *_ = await pair(env)
    tz = await tz_of(env, env.user_id)
    start_text, start, end = tomorrow_at(tz)
    invites(env, invite_call(start=start_text), text="Написал Саше, жду ответа!")

    await env.feed("Позови Сашу в шахматы завтра в 19:00")

    expected = texts.PROPOSE_SENT.format(name="Саша", title="Шахматы", when=texts.when_text(start, end, tz))
    assert [m.text for m in says(env, env.chat_id)][-1] == expected
    assert all("Написал Саше" not in m.text for m in env.session.sent())  # what the model said next to the call never reaches the user
    assert env.chat.calls == []  # no second model call: nothing it could embroider
    [(status, title, starts_at, ends_at, _)] = await proposals_of(env)
    assert (status, title, starts_at, ends_at) == ("pending", "Шахматы", start, end)
    assert await outbox(env, other) == [("event_proposal", "pending")] and await outbox(env, env.user_id) == []
    assert await meetings(env, env.user_id, other) == []  # nobody's calendar has changed yet
    assert [m["content"] for m in await env.messages() if m["role"] == "assistant"] == [expected]
    rows = await owner_exec(env.owner_engine, "SELECT third_party_data FROM civ.messages WHERE user_id = :u AND role = 'assistant'", u=env.user_id)
    assert rows == [(False,)]  # it is the user's own request, not another person's data


async def test_the_recipient_gets_a_card_with_two_buttons_in_the_settings_topic(bot_env):
    env = bot_env
    other, _, chat = await pair(env)
    await new_persona(env.owner_engine, other, "settings", thread_id=777)
    pid, start, end = await invited(env, other)

    [card] = says(env, chat, 777)
    assert card.text == texts.PROPOSAL_CARD.format(name="Алиса", title="Шахматы", when=texts.when_text(start, end, "UTC"))
    buttons = [(b.text, b.callback_data) for row in card.reply_markup.inline_keyboard for b in row]
    assert buttons == [(texts.PROPOSAL_ACCEPT, f"ep:{pid}:y"), (texts.PROPOSAL_DECLINE, f"ep:{pid}:n")]
    assert [s for _, s in await outbox(env, other)] == ["done"]
    assert await Notifier(env.db, env.bot, env.settings).run_tenant(other) == 0 and len(says(env, chat, 777)) == 1  # never twice


async def test_without_a_settings_topic_the_card_goes_to_the_main_chat(bot_env):
    env = bot_env
    other, _, chat = await pair(env)
    pid, *_ = await invited(env, other)
    [card] = says(env, chat, None)
    assert f"ep:{pid}:y" in card.reply_markup.inline_keyboard[0][0].callback_data


async def test_accepting_fills_both_calendars_replaces_the_card_and_tells_the_author(bot_env):
    env = bot_env
    other, tg, chat = await pair(env)
    pid, start, end = await invited(env, other)

    await press(env, tg, chat, f"ep:{pid}:y")

    mine = {r[0]: tuple(r[1:]) for r in await meetings(env, env.user_id, other)}
    assert mine == {  # one row per person, each in their OWN zone, visible to others only as busy time
        env.user_id: ("Шахматы", await tz_of(env, env.user_id), "busy_only"),
        other: ("Шахматы", "UTC", "busy_only"),
    }
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_TOAST["accepted"]
    assert env.session.edits()[-1].text == texts.PROPOSAL_ACCEPTED.format(title="Шахматы", when=texts.when_text(start, end, "UTC"))
    assert env.session.edits()[-1].reply_markup is None  # the buttons are gone
    assert await outbox(env, env.user_id) == [("event_proposal_answer", "pending")]

    assert await Notifier(env.db, env.bot, env.settings).run_tenant(env.user_id) == 1
    author_tz = await tz_of(env, env.user_id)
    assert says(env, env.chat_id)[-1].text == texts.NOTIFY_PROPOSAL_ACCEPTED.format(
        name="Саша", title="Шахматы", when=texts.when_text(start, end, author_tz)
    )  # the author's own name for them, the author's own clock

    await press(env, tg, chat, f"ep:{pid}:y")  # a second click: the same final state, no second event
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_TOAST["already_accepted"]
    assert len(await meetings(env, env.user_id, other)) == 2 and await outbox(env, env.user_id) == [("event_proposal_answer", "done")]


async def test_declining_adds_nothing_and_the_author_hears_it_neutrally(bot_env):
    env = bot_env
    other, tg, chat = await pair(env)
    pid, start, end = await invited(env, other)

    await press(env, tg, chat, f"ep:{pid}:n")

    assert await meetings(env, env.user_id, other) == []
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_TOAST["declined"]
    assert env.session.edits()[-1].text == texts.PROPOSAL_DECLINED.format(title="Шахматы", when=texts.when_text(start, end, "UTC"))
    await Notifier(env.db, env.bot, env.settings).run_tenant(env.user_id)
    note = says(env, env.chat_id)[-1].text
    assert note == texts.NOTIFY_PROPOSAL_DECLINED.format(
        name="Саша", title="Шахматы", when=texts.when_text(start, end, await tz_of(env, env.user_id))
    )
    assert "не сможет в это время" in note

    await press(env, tg, chat, f"ep:{pid}:y")  # a decision is final: a declined invitation cannot be revived
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_TOAST["already_declined"]
    assert await meetings(env, env.user_id, other) == []


async def test_the_author_is_told_even_if_they_removed_the_contact_meanwhile(bot_env):
    env = bot_env
    other, tg, chat = await pair(env)
    pid, *_ = await invited(env, other)
    await press(env, tg, chat, f"ep:{pid}:y")
    await owner_exec(env.owner_engine, "DELETE FROM civ.contacts WHERE user_id = :a AND contact_user_id = :b", a=env.user_id, b=other)
    await Notifier(env.db, env.bot, env.settings).run_tenant(env.user_id)
    assert says(env, env.chat_id)[-1].text.startswith(f"{texts.DEFAULT_CONTACT_NAME} принял(а)")


async def test_only_the_recipient_can_answer_a_card(bot_env):
    env = bot_env
    other, tg, chat = await pair(env)
    pid, *_ = await invited(env, other)
    stranger = await new_user(env.owner_engine)
    s_tg, s_chat = await person(env, stranger)

    await press(env, s_tg, s_chat, f"ep:{pid}:y")  # somebody else's card, pressed in their own group
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_TOAST["unavailable"]
    assert env.session.edits()[-1].text == texts.PROPOSAL_GONE  # and nothing about the invitation is revealed
    await press(env, env.telegram_user_id, env.chat_id, f"ep:{pid}:y")  # not even the author
    assert env.session.callback_answers()[-1].text == texts.PROPOSAL_TOAST["unavailable"]
    await press(env, tg, s_chat, f"ep:{pid}:y")  # the recipient in a group that is not theirs: ignored, no answer at all
    assert (await proposals_of(env))[0][0] == "pending" and await meetings(env, env.user_id, other, stranger) == []


@pytest.mark.parametrize(
    ("data", "toast"),
    [
        ("ep:xyz", texts.PROPOSAL_UNKNOWN_BUTTON),
        ("ep:", texts.PROPOSAL_UNKNOWN_BUTTON),
        ("ep:00000000-0000-0000-0000-000000000000:q", texts.PROPOSAL_UNKNOWN_BUTTON),
        (f"ep:{uuid.uuid4()}:y", texts.PROPOSAL_TOAST["unavailable"]),  # well-formed, but not a proposal addressed to this person
    ],
)
async def test_garbage_and_unknown_buttons_change_nothing(bot_env, data, toast):
    env = bot_env
    other, tg, chat = await pair(env)
    await invited(env, other)
    await press(env, tg, chat, data)
    assert env.session.callback_answers()[-1].text == toast
    assert (await proposals_of(env))[0][0] == "pending" and await meetings(env, env.user_id, other) == []


async def test_a_failing_database_leaves_the_buttons_and_says_so(bot_env, monkeypatch):
    env = bot_env
    other, tg, chat = await pair(env)
    pid, *_ = await invited(env, other)

    async def boom(self, *args: Any, **kwargs: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(ProposalService, "decide", boom)
    await press(env, tg, chat, f"ep:{pid}:y")
    [answer] = env.session.callback_answers()
    assert answer.text == texts.FAILURE and answer.show_alert
    assert env.session.edits() == []  # the card keeps its buttons: the user can press again
    assert (await proposals_of(env))[0][0] == "pending"


async def test_the_same_request_twice_says_it_is_already_waiting(bot_env):
    env = bot_env
    other, *_ = await pair(env)
    start_text, start, end = tomorrow_at(await tz_of(env, env.user_id))
    invites(env, invite_call(start=start_text))
    await env.feed("Позови Сашу в шахматы завтра в 19:00")
    await env.feed("Позови Сашу в шахматы завтра в 19:00")
    texts_sent = [m.text for m in says(env, env.chat_id)]
    assert texts_sent[-1].startswith("Это приглашение уже ждёт ответа") and texts_sent[-2].startswith("Отправил приглашение")
    assert len(await proposals_of(env)) == 1 and await outbox(env, other) == [("event_proposal", "pending")]


async def _blocked(env, other):
    await owner_exec(env.owner_engine, "UPDATE civ.contacts SET status = 'blocked' WHERE user_id = :b AND contact_user_id = :a", a=env.user_id, b=other)


async def _removed(env, other):
    await owner_exec(env.owner_engine, "DELETE FROM civ.contacts WHERE user_id = :b AND contact_user_id = :a", a=env.user_id, b=other)


async def _frozen(env, other):
    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :b", b=other)


async def _unfinished(env, other):
    await owner_exec(env.owner_engine, "UPDATE civ.users SET onboarding_state = 'in_progress' WHERE id = :b", b=other)


@pytest.mark.parametrize("break_it", [_blocked, _removed, _frozen, _unfinished], ids=lambda f: f.__name__.strip("_"))
async def test_every_reason_the_invitation_cannot_go_looks_the_same(bot_env, break_it):
    env = bot_env
    other, *_ = await pair(env)
    await break_it(env, other)
    invites(env, invite_call(start=tomorrow_at(await tz_of(env, env.user_id))[0]))
    await env.feed("Позови Сашу в шахматы завтра в 19:00")
    assert says(env, env.chat_id)[-1].text == texts.PROPOSE_UNAVAILABLE  # never "blocked", never "removed you"
    assert await proposals_of(env) == [] and await outbox(env, other) == []
    assert all("Отправил" not in m.text for m in env.session.sent())


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ({"contact": "none"}, texts.DIPLOMAT_NO_CONTACT),
        ({"contact": "c9"}, texts.DIPLOMAT_NO_CONTACT),
        ({"start": "завтра"}, texts.PROPOSE_BAD_INPUT),
        ({"start": "2020-01-01T10:00"}, texts.PROPOSE_BAD_INPUT),
        ({"title": ""}, texts.PROPOSE_BAD_INPUT),
        ({"duration_minutes": 5}, texts.PROPOSE_BAD_INPUT),
    ],
    ids=["stranger", "unknown-ref", "no-date", "past", "no-title", "too-short"],
)
async def test_an_invitation_the_code_cannot_trust_is_not_sent_and_the_user_is_asked_again(bot_env, args, expected):
    env = bot_env
    other, *_ = await pair(env)
    good = {"contact": "c1", "title": "Шахматы", "start": tomorrow_at(await tz_of(env, env.user_id))[0]}
    invites(env, ToolCall(diplomat.PROPOSE_NAME, {**good, **args}))
    await env.feed("Позови Сашу в шахматы")
    assert says(env, env.chat_id)[-1].text == expected
    assert await proposals_of(env) == [] and await outbox(env, other) == [] and env.chat.calls == []


async def test_a_sixth_open_invitation_is_refused_with_a_fixed_text(bot_env):
    env = bot_env
    other, *_ = await pair(env)
    tz = await tz_of(env, env.user_id)
    for hour in range(8, 14):
        invites(env, invite_call(start=tomorrow_at(tz, hour)[0]))
        await env.feed(f"Позови Сашу завтра в {hour}:00")
    last = says(env, env.chat_id)
    assert [m.text for m in last][-1] == texts.PROPOSE_TOO_MANY and len(await proposals_of(env)) == 5


async def test_a_database_failure_never_becomes_a_claim_that_it_was_sent(bot_env, monkeypatch):
    env = bot_env
    other, *_ = await pair(env)

    async def boom(self, *args: Any, **kwargs: Any):
        raise RuntimeError("database is down")

    monkeypatch.setattr(diplomat.Diplomat, "_call_propose", boom)
    invites(env, invite_call(start=tomorrow_at(await tz_of(env, env.user_id))[0]), text="Всё отправил!")
    await env.feed("Позови Сашу в шахматы завтра в 19:00")
    assert says(env, env.chat_id)[-1].text == texts.DIPLOMAT_TRY_LATER
    assert await proposals_of(env) == [] and all("Всё отправил" not in m.text for m in env.session.sent())


async def test_a_question_and_an_invitation_in_one_message_get_a_model_answer_plus_the_fixed_confirmation(bot_env):
    env = bot_env
    other, *_ = await pair(env, granted=FREE)
    tz = await tz_of(env, env.user_id)
    start_text, start, end = tomorrow_at(tz)
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="", calls=(call("c1", **two_days()), invite_call(start=start_text)))
    env.chat.responder = lambda system, turns: "Саша пока свободен."

    await env.feed("Саша свободен? И позови его в шахматы завтра в 19:00")

    confirmation = texts.PROPOSE_SENT.format(name="Саша", title="Шахматы", when=texts.when_text(start, end, tz))
    assert says(env, env.chat_id)[-1].text == f"Саша пока свободен.\n\n{confirmation}"
    [informed] = env.chat.calls
    assert diplomat.INVITATIONS_NOTE in informed["system"] and "## Contact data" in informed["system"]
    assert "Шахматы" not in informed["system"].split("## Contact data")[1]  # the model is not given the invitation to talk about
    rows = await owner_exec(env.owner_engine, "SELECT third_party_data FROM civ.messages WHERE user_id = :u AND role = 'assistant'", u=env.user_id)
    assert rows == [(True,)]  # the part about the contact's calendar keeps the flag: the GC must not remember it
    assert len(await proposals_of(env)) == 1


async def test_an_ask_that_was_refused_still_gets_the_confirmation_of_the_invitation(bot_env):
    env = bot_env
    other, *_ = await pair(env, granted=None)  # no grant: the question is refused, the invitation needs none
    tz = await tz_of(env, env.user_id)
    start_text, start, end = tomorrow_at(tz)
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="", calls=(call("c1", **two_days()), invite_call(start=start_text)))
    await env.feed("Саша свободен? И позови его в шахматы завтра в 19:00")
    confirmation = texts.PROPOSE_SENT.format(name="Саша", title="Шахматы", when=texts.when_text(start, end, tz))
    assert says(env, env.chat_id)[-1].text == f"{texts.DIPLOMAT_UNAVAILABLE}\n\n{confirmation}"
    assert env.chat.calls == []


async def test_a_user_without_contacts_is_offered_no_function_and_nothing_is_sent(bot_env):
    env = bot_env  # quick events off
    await env.feed("Позови Сашу в шахматы завтра в 19:00")
    assert env.chat.tool_calls == [] and await proposals_of(env) == []


async def test_a_user_without_contacts_is_never_offered_propose_event_even_with_quick_events(quick_env):
    env = quick_env
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text="Написал Саше!", calls=(invite_call(),))
    await env.feed("Позови Сашу в шахматы завтра в 19:00")
    [offered] = env.chat.tool_calls
    assert [t.name for t in offered["tools"]] == ["create_event"]  # an invitation needs a contact: only the user's OWN plan is on offer
    assert await proposals_of(env) == []  # a stray propose_event call has no function behind it
    texts_sent = [m.text for m in env.session.sent()]
    assert "Написал Саше!" not in texts_sent  # words next to a call that did nothing are not trusted as a claim about an action


# ================================================================== delivery: only a card that can still be answered is sent


async def _outbox_status(env, user_id: uuid.UUID) -> list[str]:
    return [s for _, s in await outbox(env, user_id)]


async def _declined(env, pid, other):
    await owner_exec(env.owner_engine, "UPDATE civ.event_proposals SET status = 'declined', decided_at = now() WHERE id = :p", p=pid)


async def _ran_out(env, pid, other):
    await owner_exec(
        env.owner_engine,
        "UPDATE civ.event_proposals SET created_at = now() - interval '3 days', expires_at = now() - interval '1 day' WHERE id = :p",
        p=pid,
    )


async def _sender_removed(env, pid, other):
    await owner_exec(env.owner_engine, "DELETE FROM civ.contacts WHERE user_id = :b AND contact_user_id = :a", a=env.user_id, b=other)


async def _sender_blocked(env, pid, other):
    await owner_exec(env.owner_engine, "UPDATE civ.contacts SET status = 'blocked' WHERE user_id = :b AND contact_user_id = :a", a=env.user_id, b=other)


async def _purged(env, pid, other):
    await owner_exec(env.owner_engine, "DELETE FROM civ.event_proposals WHERE id = :p", p=pid)


@pytest.mark.parametrize("spoil", [_declined, _ran_out, _sender_removed, _sender_blocked, _purged], ids=lambda f: f.__name__.strip("_"))
async def test_a_card_that_can_no_longer_be_answered_is_finished_silently(bot_env, spoil):
    env = bot_env
    other, _, chat = await pair(env)
    start_text, *_ = tomorrow_at(await tz_of(env, env.user_id))
    invites(env, invite_call(start=start_text))
    await env.feed("Позови Сашу в шахматы завтра в 19:00")
    [(*_, pid)] = await proposals_of(env)
    await spoil(env, pid, other)

    assert await Notifier(env.db, env.bot, env.settings).run_tenant(other) == 1  # delivered = handled
    assert says(env, chat) == [] and await _outbox_status(env, other) == ["done"]


async def test_an_answer_with_nothing_to_report_is_finished_silently(bot_env):
    env = bot_env
    other, *_ = await pair(env)
    start_text, *_ = tomorrow_at(await tz_of(env, env.user_id))
    invites(env, invite_call(start=start_text))
    await env.feed("Позови Сашу в шахматы завтра в 19:00")
    [(*_, pid)] = await proposals_of(env)
    for key, proposal_id in (("pending-one", pid), ("unknown-one", uuid.uuid4())):  # still pending / not there at all
        await owner_exec(
            env.owner_engine,
            "INSERT INTO civ.notification_outbox (user_id, kind, payload, idempotency_key) VALUES (:u, 'event_proposal_answer', CAST(:p AS jsonb), :k)",
            u=env.user_id, p=json.dumps({"proposal_id": str(proposal_id), "accepted": True}), k=key,
        )  # fmt: skip
    assert await Notifier(env.db, env.bot, env.settings).run_tenant(env.user_id) == 2
    assert says(env, env.chat_id)[-1].text.startswith("Отправил приглашение")  # nothing was added to the author's chat
    assert await _outbox_status(env, env.user_id) == ["done", "done"]


async def test_a_card_waits_while_the_recipients_group_is_frozen(bot_env):
    env = bot_env
    other, _, chat = await pair(env)
    start_text, *_ = tomorrow_at(await tz_of(env, env.user_id))
    invites(env, invite_call(start=start_text))
    await env.feed("Позови Сашу в шахматы завтра в 19:00")
    await _frozen(env, other)
    assert await Notifier(env.db, env.bot, env.settings).run_tenant(other) == 0 and says(env, chat) == []
    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'active' WHERE id = :b", b=other)
    assert await Notifier(env.db, env.bot, env.settings).run_tenant(other) == 1 and len(says(env, chat)) == 1

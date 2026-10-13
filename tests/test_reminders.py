"""v2.0 PR 5.4: scheduled reminders, wake-up alarms and persistent pings (``bot/reminders.py``, the Celery task ``send_scheduled_message``).

The function ``schedule_reminder`` is registered and offered only on the user's own cue; CODE validates every argument; the user reads a card written by code with the exact
local time; an alarm repeats (2-3 minutes, at most 5 times) until the button / "я встал"; duplicates and failures are harmless. Real PostgreSQL, the real Dispatcher on a fake
Telegram wire, fake Gemini, an in-memory Redis with failure injection and a recording stand-in for the Celery queue.
"""

from __future__ import annotations

import random
import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ai_civ.bot import reminders, texts
from ai_civ.bot.prompt import RULES
from ai_civ.bot.reminders import Reminders, ReminderStore, ReminderSpec
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolReply
from ai_civ.tasks import send_scheduled_message, send_scheduled_message_async
from tests.bot_env import build_bot_env
from tests.helpers import owner_exec
from tests.test_proactive_schedule import runtime
from tests.tg_fakes import make_callback_update

CFG = Settings(_env_file=None)
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


class FakeRedis:
    """The subset of ``redis.asyncio.Redis`` the reminders use, with failure injection."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.sets: dict[str, set[str]] = {}
        self.error: Exception | None = None
        self.closed = False

    def _gate(self) -> None:
        if self.error is not None:
            raise self.error

    async def get(self, name):
        self._gate()
        return self.data.get(name)

    async def set(self, name, value, nx=False, ex=None):
        self._gate()
        if nx and name in self.data:
            return None
        self.data[name] = value
        return True

    async def delete(self, name):
        self._gate()
        self.data.pop(name, None)

    async def exists(self, name):
        self._gate()
        return int(name in self.data)

    async def sadd(self, name, member):
        self._gate()
        self.sets.setdefault(name, set()).add(member)

    async def srem(self, name, member):
        self._gate()
        self.sets.get(name, set()).discard(member)

    async def smembers(self, name):
        self._gate()
        return set(self.sets.get(name, set()))

    async def expire(self, name, seconds):
        self._gate()

    async def aclose(self):
        self.closed = True


class Queue:
    """Stands in for ``send_scheduled_message.apply_async``: every hand-off is recorded as (args, countdown)."""

    def __init__(self) -> None:
        self.items: list[tuple[list[Any], int]] = []
        self.error: Exception | None = None

    def __call__(self, args, countdown) -> None:
        if self.error is not None:
            raise self.error
        self.items.append((list(args), countdown))


@asynccontextmanager
async def rem_env(owner_engine, db, **kw: Any):
    kw.setdefault("reminders_enabled", True)
    env = await build_bot_env(owner_engine, db, **kw)
    env.redis = FakeRedis()
    env.queue = Queue()
    env.store = ReminderStore(env.redis)
    env.orchestrator.reminders._store = env.store  # the tool and the button handler share this engine
    env.orchestrator.reminders._enqueue = env.queue
    try:
        yield env
    finally:
        await env.bot.session.close()


def call(delay: int = 1200, text: str = "Пора вставать!", kind: str = "alarm", **extra: Any) -> ToolCall:
    return ToolCall(reminders.TOOL_NAME, {"delay_seconds": delay, "text": text, "reminder_type": kind, **extra})


def model_calls(env, *calls: ToolCall, text: str = "Поставил будильник!", then: str = "Договорились.") -> None:
    env.chat.tool_responder = lambda system, turns, tools: ToolReply(text=text, calls=tuple(calls))
    env.chat.responder = lambda system, turns: then


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


def token_of(env) -> str:
    [token] = env.queue.items[0][0][1:2]
    return token


async def state_of(env, token: str) -> reminders.State | None:
    return await env.store.get(token)


# ================================================================== pure


@pytest.mark.parametrize(
    "text",
    ["разбуди меня в 7", "Напомни мне через 20 минут позвонить маме", "поставь будильник на завтра", "пингани меня через час", "remind me in 10 minutes", "wake me up at 6", "set a reminder"],
)
def test_the_cue_is_found_in_the_users_own_words(text):
    assert reminders.wants_reminder(text)


@pytest.mark.parametrize("text", ["привет", "как дела?", "я устал", "расскажи про Данию", "давай обсудим планы на неделю"])
def test_ordinary_talk_is_not_a_reminder_request(text):
    assert not reminders.wants_reminder(text)


@pytest.mark.parametrize("text", ["я встал", "Я тут", "я уже проснулся", "проснулся!", "на месте, я здесь".replace("на месте, ", "я "), "I'm up", "i am awake"])
def test_the_acknowledging_words_are_recognised(text):
    assert reminders.is_ack_text(text)


@pytest.mark.parametrize("text", ["я пошёл спать", "когда я встану, напомни про зарядку", "x" * 200, "привет"])
def test_other_words_do_not_acknowledge(text):
    assert not reminders.is_ack_text(text)


def parse(*calls: ToolCall) -> Any:
    return reminders.parse_reminder(calls, CFG)


def test_a_valid_call_becomes_a_spec_and_an_alarm_always_repeats():
    assert parse(call(1200, "Пора вставать!", "alarm")) == ReminderSpec(1200, "Пора вставать!", "alarm", True)
    assert parse(call(60, "Позвонить маме", "ping")) == ReminderSpec(60, "Позвонить маме", "ping", False)
    assert parse(call(60, "Позвонить маме", "ping", repeat_until_ack=True)).persistent is True
    assert parse(call(3600.0, "Как успехи?", "checkin")) == ReminderSpec(3600, "Как успехи?", "checkin", False)  # a whole float is fine


@pytest.mark.parametrize(
    "bad",
    [
        {"delay_seconds": True}, {"delay_seconds": "600"}, {"delay_seconds": 12.5}, {"delay_seconds": None}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": "!!!"},
        {"text": "x" * 301}, {"text": "‮evil"}, {"reminder_type": "wakeup"}, {"reminder_type": None}, {"repeat_until_ack": "yes"},
    ],
)  # fmt: skip
def test_an_unusable_call_is_dropped_silently(bad):
    assert parse(ToolCall(reminders.TOOL_NAME, {"delay_seconds": 600, "text": "Привет", "reminder_type": "ping", **bad})) is None


def test_the_delay_is_bounded_and_the_user_is_told_why():
    assert parse(call(5)) == reminders.Rejected("bad_time") and parse(call(0)) == reminders.Rejected("bad_time") and parse(call(-30)) == reminders.Rejected("bad_time")
    assert parse(call(86_401)) == reminders.Rejected("too_far")
    assert isinstance(parse(call(10)), ReminderSpec) and isinstance(parse(call(86_400)), ReminderSpec)


def test_only_the_first_call_of_this_function_counts_and_other_names_are_not_ours():
    assert parse(ToolCall("other", {}), call(600, "первое"), call(900, "второе")).text == "первое"
    assert parse(ToolCall("other", {})) is None and parse() is None


def test_the_user_reads_the_exact_local_time():
    tz = ZoneInfo("Europe/Copenhagen")  # UTC+2 in October (CEST until the 25th)
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)  # 14:00 in Copenhagen
    assert reminders.when_phrase(now + timedelta(minutes=20), now, tz) == "через 20 мин (в 14:20)"
    assert reminders.when_phrase(now + timedelta(seconds=10), now, tz) == "через 1 мин (в 14:00)"
    assert reminders.when_phrase(now + timedelta(hours=3), now, tz) == "сегодня в 17:00"
    assert reminders.when_phrase(now + timedelta(hours=17, minutes=30), now, tz) == "завтра в 07:30"
    assert reminders.when_phrase(now + timedelta(days=2), now, tz) == "09.10 в 14:00"


def test_the_message_texts_and_the_button_data_are_fixed_and_fit_telegram():
    assert reminders.fire_text("alarm", "Пора!", 0, 5) == "⏰ Пора!" and reminders.fire_text("ping", "Эй", 0, 5) == "🔔 Эй" and reminders.fire_text("checkin", "Как дела?", 0, 5) == "💬 Как дела?"
    again = reminders.fire_text("alarm", "Пора!", 2, 5)
    assert again.startswith("⏰ Пора!") and "(2/5)" in again
    token = uuid.uuid4().hex
    assert reminders.parse_callback(reminders.callback_data(token, "a")) == (token, "a") and reminders.parse_callback(reminders.callback_data(token, "x")) == (token, "x")
    assert len(reminders.callback_data(token, "a").encode()) <= 64
    for junk in ("rm:zz:a", "rm:" + token + ":q", "xx:" + token + ":a", ""):
        assert reminders.parse_callback(junk) is None


def test_the_rules_say_the_bot_can_read_files_and_can_write_first_without_a_blanket_promise():
    rules = " ".join(RULES.split())  # the rules are wrapped over several lines in the source
    assert "Ты УМЕЕШЬ читать файлы (.txt, .pdf, .docx)" in rules and "Never claim to the user that you cannot read or accept files" in rules
    assert "У тебя ЕСТЬ техническая возможность писать пользователю первым" in rules and "schedule_reminder" in rules
    assert "a reminder exists only once a function result in this conversation says it was scheduled" in rules
    assert "NEVER say or imply that you did something you have no function for" in rules  # the old ban stays: only a function result makes a promise real


def test_the_texts_name_no_hardcoded_main_topic_and_the_card_is_in_the_fixed_voice():
    for name in ("REMINDER_SCHEDULED", "REMINDER_ALARM_SCHEDULED", "REMINDER_PING_SCHEDULED", "REMINDER_REPEAT_NOTE", "REMINDER_UNAVAILABLE"):
        assert "Чатик" not in getattr(texts, name)


# ================================================================== scheduling through the dispatcher


async def test_the_function_is_offered_only_on_the_users_own_cue(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        await env.feed("привет, как дела?")
        assert env.chat.tool_calls == []  # the plain way
        await env.feed("разбуди меня завтра в 7")
        [offered] = env.chat.tool_calls
        assert [t.name for t in offered["tools"]] == [reminders.TOOL_NAME]
        assert "## Reminders, alarms and pings" in offered["system"] and "You DO have the technical ability to write to the user first" in offered["system"]
        assert "У тебя ЕСТЬ техническая возможность писать пользователю первым" in offered["system"]


async def test_the_function_is_off_by_the_switch_and_in_the_default_test_stack(owner_engine, db):
    async with rem_env(owner_engine, db, reminders_enabled=False) as env:
        await env.feed("разбуди меня завтра в 7")
        assert env.chat.tool_calls == []
    plain = await build_bot_env(owner_engine, db)
    try:
        assert plain.settings.reminders_enabled is False
    finally:
        await plain.bot.session.close()


async def test_an_alarm_is_scheduled_the_card_says_the_time_and_the_models_words_never_go_out(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        model_calls(env, call(1200, "Пора вставать!", "alarm"))
        await env.feed("разбуди меня через 20 минут")
        assert "Поставил будильник!" not in says(env)  # the claim of the model is never sent
        assert says(env)[0] == "Договорились."
        card = env.session.sent()[-1]
        assert "будильник" in card.text
        assert "«Пора вставать!»" in card.text and re.search(r"\(в \d\d:\d\d\)", card.text)
        [(args, countdown)] = env.queue.items
        assert args[0] == str(env.user_id) and args[2] == 0 and countdown == 1200
        token = args[1]
        state = await state_of(env, token)
        assert state.status == reminders.SCHEDULED and state.persistent and state.kind == "alarm" and state.tenant == str(env.user_id)
        assert card.reply_markup.inline_keyboard[0][0].callback_data == f"rm:{token}:x"
        [phrased] = env.chat.calls
        assert "Reminder (made by the system)" in phrased["system"] and "Do not state the time" in phrased["system"]
        assert not any("Поставил будильник!" in (m["content"]) for m in await env.messages())  # and it is not remembered as the bot's word either


async def test_the_reminder_goes_to_the_topic_where_the_user_asked(owner_engine, db):
    from tests.helpers import new_persona

    async with rem_env(owner_engine, db) as env:
        persona = await new_persona(env.owner_engine, env.user_id, "sport", thread_id=612)
        model_calls(env, call(300, "Размяться", "ping"))
        await env.feed("напомни размяться через 5 минут", thread_id=612)
        state = await state_of(env, token_of(env))
        assert state.thread_id == 612 and state.persona_id == str(persona)
        assert any("Размяться" in t for t in says(env, 612))  # the card is in the same topic


async def test_unusable_and_out_of_range_calls_schedule_nothing_and_say_so_in_fixed_words(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        model_calls(env, call(2, "Рано"))
        await env.feed("напомни мне прямо сейчас")
        assert env.queue.items == [] and says(env)[-1] == texts.REMINDER_BAD_TIME
        model_calls(env, call(200_000, "Далеко"))
        await env.feed("напомни мне через три дня")
        assert env.queue.items == [] and says(env)[-1] == texts.REMINDER_TOO_FAR
        model_calls(env, call(600, "", "ping"), text="Готово, напомню!", then="Скажите, о чём напомнить.")
        await env.feed("напомни мне")
        assert env.queue.items == [] and says(env)[-1] == "Скажите, о чём напомнить."  # an unusable call: the words next to it are not trusted either


async def test_the_cap_of_open_reminders_is_enforced(owner_engine, db):
    async with rem_env(owner_engine, db, reminders_max_pending=2) as env:
        for i in range(3):
            model_calls(env, call(600 + i, f"Дело {i}", "ping"))
            await env.feed(f"напомни про дело {i}")
        assert len(env.queue.items) == 2 and says(env)[-1] == texts.REMINDER_TOO_MANY


async def test_redis_down_or_a_dead_queue_leaves_nothing_behind_and_the_user_is_told(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        model_calls(env, call())
        env.redis.error = ConnectionError("redis down")
        await env.feed("разбуди меня через 20 минут")
        assert says(env)[-1] == texts.REMINDER_UNAVAILABLE and env.queue.items == []
        env.redis.error = None
        env.queue.error = ConnectionError("broker down")
        await env.feed("разбуди меня через 20 минут")
        assert says(env)[-1] == texts.REMINDER_UNAVAILABLE and env.queue.items == []
        assert env.redis.data == {} and all(not members for members in env.redis.sets.values())  # the state of the reminder that was never queued is removed again


async def test_the_cancel_button_takes_a_scheduled_reminder_back_and_a_second_press_changes_nothing(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        model_calls(env, call(900, "Позвонить", "ping"))
        await env.feed("напомни позвонить через 15 минут")
        token, card = token_of(env), env.session.sent()[-1]
        press = make_callback_update(data=f"rm:{token}:x", user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=4242)
        await env.feed_update(press)
        assert (await state_of(env, token)).status == reminders.CANCELED
        assert env.session.callback_answers()[-1].text == texts.REMINDER_TOAST["canceled"]
        assert env.session.edits()[-1].text == texts.REMINDER_CANCELED.format(text="Позвонить") and env.session.edits()[-1].reply_markup is None
        await env.feed_update(make_callback_update(data=f"rm:{token}:x", user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=4242))
        assert env.session.callback_answers()[-1].text == texts.REMINDER_TOAST["settled"]
        assert card is not None


# ================================================================== delivery (the Celery task body)


async def schedule(env, *, kind="alarm", persistent=True, text="Пора вставать!", delay=1200) -> str:
    spec = ReminderSpec(delay, text, kind, persistent)
    done = await env.orchestrator.reminders.schedule(env.user_id, "UTC", spec, thread_id=None, persona_id=None, now=NOW)
    assert isinstance(done, reminders.Scheduled)
    return done.token


async def deliver(env, token: str, attempt: int = 0, *, rng=None) -> str:
    return await reminders.deliver(env.db, env.bot, env.settings, env.store, env.user_id, token, attempt, enqueue_next=env.queue, rng=rng or random.Random(1))


async def run_cycle(env, token: str) -> tuple[list[str], list[int]]:
    """Deliver attempt after attempt the way Celery would; returns the texts sent and the countdowns that were queued between them."""
    texts_sent: list[str] = []
    gaps: list[int] = []
    attempt = 0
    while True:
        assert await deliver(env, token, attempt) == "sent"
        texts_sent.append(env.session.sent()[-1].text)
        if not env.queue.items:
            return texts_sent, gaps
        [(args, countdown)] = env.queue.items
        env.queue.items.clear()
        assert args == [str(env.user_id), token, attempt + 1]
        gaps.append(countdown)
        attempt += 1


async def test_an_alarm_repeats_every_three_minutes_for_three_hours_and_then_stops(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        env.queue.items.clear()
        sent_texts, gaps = await run_cycle(env, token)
        assert gaps == [180] * 60  # exactly 180 seconds apart, sixty repeats = three hours
        assert len(sent_texts) == 61 and sent_texts[0] == "⏰ Пора вставать!"  # the first message and exactly sixty repeats
        assert all("(%d/60)" % n in sent_texts[n] for n in (1, 10, 11, 59, 60))
        assert (await state_of(env, token)).status == reminders.DONE
        assert await deliver(env, token, 61) == "skipped"  # nothing after the sixtieth repeat, even if a stray task arrives


async def test_an_alarm_does_not_stop_at_the_old_limit_of_ten(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        env.queue.items.clear()
        for attempt in range(0, 25):
            assert await deliver(env, token, attempt) == "sent"
            assert env.queue.items.pop() == ([str(env.user_id), token, attempt + 1], 180)  # always another one queued
        assert (await state_of(env, token)).status == reminders.ACTIVE


async def test_only_the_answer_ends_an_alarm_that_is_far_into_its_cycle(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        env.queue.items.clear()
        for attempt in range(0, 41):
            await deliver(env, token, attempt)
            env.queue.items.clear()
        await env.feed("я встал")  # the text answer, 40 repeats in
        assert (await state_of(env, token)).status == reminders.ACKED
        sent_before = len(env.session.sent())
        assert await deliver(env, token, 41) == "skipped" and len(env.session.sent()) == sent_before and env.queue.items == []


async def test_an_alarm_stops_repeating_the_moment_it_is_acknowledged(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        env.queue.items.clear()
        for attempt in range(3):  # the first message and two repeats
            assert await deliver(env, token, attempt) == "sent"
            assert env.queue.items.pop() == ([str(env.user_id), token, attempt + 1], 180)
        await env.feed_update(make_callback_update(data=f"rm:{token}:a", user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=901))
        assert (await state_of(env, token)).status == reminders.ACKED
        sent_before = len(env.session.sent())
        assert await deliver(env, token, 3) == "skipped"  # the repeat that was already queued finds the acknowledgement
        assert len(env.session.sent()) == sent_before and env.queue.items == []


async def test_a_persistent_ping_keeps_the_gentler_rhythm_two_to_three_minutes_five_times(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env, kind="ping", persistent=True, text="Отметься")
        env.queue.items.clear()
        sent_texts, gaps = await run_cycle(env, token)
        assert len(gaps) == 5 and all(120 <= g <= 180 for g in gaps) and len(sent_texts) == 6
        assert "(5/5)" in sent_texts[5]


def test_the_alarm_rhythm_is_a_setting_and_the_defaults_are_180_seconds_and_sixty_repeats():
    assert (CFG.reminders_alarm_repeat_seconds, CFG.reminders_alarm_max_repeats) == (180, 60)
    assert reminders.repeat_limit("alarm", CFG) == 60 and reminders.repeat_limit("ping", CFG) == 5 and reminders.repeat_limit("checkin", CFG) == 5
    assert reminders.repeat_gap("alarm", CFG) == 180 and 120 <= reminders.repeat_gap("ping", CFG) <= 180


async def test_every_alarm_message_carries_the_button_and_a_plain_ping_does_not(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        alarm = await schedule(env)
        await deliver(env, alarm)
        button = env.session.sent()[-1].reply_markup.inline_keyboard[0][0]
        assert button.text == texts.REMINDER_ACK_BUTTON and button.callback_data == f"rm:{alarm}:a"
        ping = await schedule(env, kind="ping", persistent=False, text="Эй, ты как?")
        env.queue.items.clear()
        assert await deliver(env, ping) == "sent"
        sent = env.session.sent()[-1]
        assert sent.text == "🔔 Эй, ты как?" and sent.reply_markup is None and env.queue.items == []  # one message, no repeat
        assert (await state_of(env, ping)).status == reminders.DONE
        persistent_ping = await schedule(env, kind="ping", persistent=True, text="Отметься")
        await deliver(env, persistent_ping)
        assert env.session.sent()[-1].reply_markup is not None  # repeat_until_ack = the same button


async def test_the_button_stops_the_alarm_and_the_next_repeat_finds_it_acknowledged(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        await deliver(env, token)
        assert (await state_of(env, token)).status == reminders.ACTIVE
        await env.feed_update(make_callback_update(data=f"rm:{token}:a", user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=777))
        assert (await state_of(env, token)).status == reminders.ACKED
        assert env.session.callback_answers()[-1].text == texts.REMINDER_TOAST["acked"] and env.session.edits()[-1].text == texts.REMINDER_ACKED
        before = len(env.session.sent())
        assert await deliver(env, token, 1) == "skipped" and len(env.session.sent()) == before  # the cycle is over: nothing more is sent


async def test_the_words_i_am_up_stop_every_waiting_alarm_and_the_chat_goes_on_normally(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        first, second = await schedule(env), await schedule(env, text="Второй")
        await deliver(env, first)
        await deliver(env, second)
        env.chat.responder = lambda system, turns: "Доброе утро!"
        await env.feed("я встал")
        assert (await state_of(env, first)).status == reminders.ACKED and (await state_of(env, second)).status == reminders.ACKED
        assert says(env)[-1] == "Доброе утро!"  # the ordinary reply still comes
        assert await deliver(env, first, 1) == "skipped" and await deliver(env, second, 1) == "skipped"


async def test_words_of_acknowledgement_without_a_waiting_alarm_change_nothing_and_cost_no_redis_call_when_off(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        scheduled = await schedule(env)  # scheduled, not yet sent: nobody waits for an answer
        await env.feed("я тут")
        assert (await state_of(env, scheduled)).status == reminders.SCHEDULED
    async with rem_env(owner_engine, db, reminders_enabled=False) as off:
        await off.feed("я встал")
        assert off.redis.data == {} and off.redis.sets == {}


async def test_a_redis_failure_while_acknowledging_by_text_never_breaks_the_reply(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        env.redis.error = ConnectionError("redis down")
        env.chat.responder = lambda system, turns: "Привет!"
        await env.feed("я встал")
        assert says(env)[-1] == "Привет!"  # fail-open


async def test_a_duplicate_delivery_of_the_same_attempt_sends_once(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env, kind="ping", persistent=False)
        outcomes = [await deliver(env, token), await deliver(env, token)]
        assert outcomes == ["sent", "skipped"]  # a plain reminder is DONE after its message
        assert len([t for t in says(env) if "Пора вставать" in t]) == 1
        persistent = await schedule(env)
        assert [await deliver(env, persistent), await deliver(env, persistent)] == ["sent", "duplicate"]  # still ACTIVE after the first: the claim stops the twin
        assert len([t for t in says(env) if t == "⏰ Пора вставать!"]) == 1


async def test_a_failed_send_gives_the_claim_back_and_the_retry_sends(owner_engine, db):
    from aiogram.exceptions import TelegramNetworkError

    async with rem_env(owner_engine, db) as env:
        token = await schedule(env, kind="ping", persistent=False, text="Пора вставать!")  # a plain reminder (an ALARM has its own safety net, see below)
        env.queue.items.clear()
        env.session.fail_on("sendMessage", TelegramNetworkError, times=1, message="down")
        with pytest.raises(TelegramNetworkError):
            await deliver(env, token)
        assert (await state_of(env, token)).status == reminders.SCHEDULED and env.queue.items == []  # nothing was queued for a message that did not go out
        assert await deliver(env, token) == "sent"  # Celery's retry of the very same task: the claim was released, so it is allowed to send
        assert says(env)[-1] == "🔔 Пора вставать!"


async def test_a_canceled_or_expired_reminder_is_never_sent(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env, kind="ping", persistent=False)
        state = await state_of(env, token)
        state.status = reminders.CANCELED
        await env.store.put(token, state)
        assert await deliver(env, token) == "skipped"
        assert await deliver(env, uuid.uuid4().hex) == "gone"  # the state expired (or never existed)
        env.redis.data.clear()
        assert await deliver(env, token) == "gone"
        assert not any("Пора вставать" in t for t in says(env))


async def test_nothing_is_sent_to_a_group_that_is_not_active_or_to_someone_elses_token(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=env.user_id)
        assert await deliver(env, token) == "inactive" and not any("Пора" in t for t in says(env))
        stranger = await schedule(env, text="Чужое")
        assert await reminders.deliver(env.db, env.bot, env.settings, env.store, uuid.uuid4(), stranger, 0, enqueue_next=env.queue) == "gone"


async def test_the_message_is_remembered_as_the_bots_own_so_the_users_answer_sees_it(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env, kind="checkin", persistent=False, text="Как продвигается отчёт?")
        await deliver(env, token)
        messages = await env.messages()
        assert messages[-1]["role"] == "assistant" and messages[-1]["content"] == "💬 Как продвигается отчёт?"


async def test_another_tenants_button_press_changes_nothing(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        await deliver(env, token)
        other = await build_bot_env(owner_engine, db, reminders_enabled=True)
        try:
            other.orchestrator.reminders._store = env.store  # the same Redis: only the tenant check protects the token
            await other.feed_update(make_callback_update(data=f"rm:{token}:a", user_id=other.telegram_user_id, chat_id=other.chat_id, message_id=1))
            assert (await state_of(env, token)).status == reminders.ACTIVE
            assert other.session.callback_answers()[-1].text == texts.REMINDER_TOAST["gone"]
        finally:
            await other.bot.session.close()


async def test_the_state_expires_instead_of_living_for_ever():
    redis = FakeRedis()
    seen: list[tuple[str, int | None]] = []

    async def set_(name, value, nx=False, ex=None):
        seen.append((name, ex))
        return True

    redis.set = set_  # type: ignore[method-assign]
    store = ReminderStore(redis, ttl_seconds=123)
    await store.put("t", reminders.State("tenant", "x", "ping", False, None, None, NOW.isoformat()))
    await store.claim_send("t", 0)
    assert [ex for _n, ex in seen] == [123, 123]  # the state and the delivery claim both carry a TTL


# ================================================================== the Celery entry point


def test_the_task_is_registered_with_a_bounded_retry():
    assert send_scheduled_message.name == "ai_civ.send_scheduled_message"
    assert Exception in send_scheduled_message.autoretry_for and send_scheduled_message.max_retries == 3  # fail-fast: a failing delivery is retried, never looped


async def test_the_task_body_delivers_and_queues_the_next_repeat_through_the_celery_hand_off(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        env.queue.items.clear()
        outcome = await send_scheduled_message_async(runtime(db), env.bot, env.user_id, token, 0, store=env.store, enqueue_next=env.queue)
        assert outcome == "sent" and env.queue.items[0][0] == [str(env.user_id), token, 1]
        assert await send_scheduled_message_async(runtime(db), env.bot, env.user_id, token, 0, store=env.store, enqueue_next=env.queue) == "duplicate"


async def test_the_default_hand_off_puts_a_task_with_a_countdown_on_the_celery_queue(monkeypatch):
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(send_scheduled_message, "apply_async", lambda **kw: calls.append(kw))
    reminders.default_enqueue(["u", "t", 0], 90)
    assert calls == [{"args": ["u", "t", 0], "countdown": 90}]


# ================================================================== wake-up requests are alarms; the morning follow-up


@pytest.mark.parametrize(
    "text",
    ["разбуди меня в 7:30", "Разбуди в 6", "поставь будильник на семь", "буди меня, пока не встану", "подними меня в 8", "wake me at 7", "нужен будильник"],
)
def test_a_request_to_be_woken_up_is_recognised(text):
    assert reminders.wants_wakeup(text)


@pytest.mark.parametrize("text", ["напомни позвонить маме", "я просыпаюсь в семь", "как дела?", "wake up is hard for me"])
def test_other_requests_are_not_wake_up_calls(text):
    assert not reminders.wants_wakeup(text)


def test_a_wake_up_request_is_an_alarm_that_repeats_whatever_type_the_model_chose():
    said = "разбуди меня завтра в 7:30"
    for kind in ("checkin", "ping", "alarm"):
        spec = reminders.parse_reminder([call(3600, "Пора вставать", kind)], CFG, user_text=said)
        assert spec == ReminderSpec(3600, "Пора вставать", "alarm", True)
    plain = reminders.parse_reminder([call(3600, "Позвонить", "ping")], CFG, user_text="напомни позвонить маме")
    assert plain == ReminderSpec(3600, "Позвонить", "ping", False)  # a plain reminder keeps the model's type
    assert reminders.parse_reminder([call(3600, "Позвонить", "ping")], CFG) == ReminderSpec(3600, "Позвонить", "ping", False)  # no text: as before
    assert reminders.parse_reminder([call(5, "Пора", "checkin")], CFG, user_text=said) == reminders.Rejected("bad_time")  # the bounds still apply


async def test_asked_to_wake_the_user_the_model_gets_a_must_and_a_checkin_call_is_made_an_alarm(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        model_calls(env, call(1200, "Пора вставать!", "checkin"))  # the model picked the passive type
        await env.feed("разбуди меня через 20 минут")
        assert "MUST call schedule_reminder with reminder_type='alarm' and repeat_until_ack=true" in env.chat.tool_calls[0]["system"]
        [(args, countdown)] = env.queue.items
        state = await state_of(env, args[1])
        assert (state.kind, state.persistent) == ("alarm", True) and countdown == 1200
        assert "будильник" in env.session.sent()[-1].text  # the card tells the user it is an alarm

    async with rem_env(owner_engine, db) as env:
        await env.feed("напомни позвонить маме")
        assert "MUST call" not in env.chat.tool_calls[0]["system"]  # only a request to be woken up gets the hard rule


def test_the_morning_slot_gets_the_follow_up_rule_and_other_hours_do_not():
    from datetime import time

    from ai_civ.bot import checkin

    def render(hour: int, minutes: int = 10) -> str:
        return checkin.render(
            local_now=datetime(2026, 10, 7, hour, 0, tzinfo=UTC), tasks=[], days_since_workout=None, silence_hours=None, followup_minutes=minutes
        )

    morning = render(8)
    assert "MORNING SLOT" in morning and "within 10 minutes" in morning and "I am up" in morning and "do not mention it" in morning
    assert "MORNING SLOT" not in render(14) and "MORNING SLOT" not in render(11) and "MORNING SLOT" in render(5)
    assert "MORNING SLOT" not in render(8, 0)  # the switch is off
    assert checkin.is_morning(time(10, 59)) and not checkin.is_morning(time(4, 59)) and not checkin.is_morning(time(11, 0))


async def follow_up(env, *, since: datetime | None = None) -> str:
    spec = ReminderSpec(
        600, texts.MORNING_FOLLOWUP, "alarm", False, extra={"ack_button": True, "skip_if_user_wrote_since": (since or datetime.now(UTC)).isoformat()}
    )
    done = await env.orchestrator.reminders.schedule(env.user_id, "UTC", spec, thread_id=None, persona_id=None, now=NOW)
    assert isinstance(done, reminders.Scheduled)
    return done.token


async def test_the_follow_up_is_one_short_message_with_the_button_and_no_repeats(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await follow_up(env)
        env.queue.items.clear()
        assert await deliver(env, token) == "sent"
        sent = env.session.sent()[-1]
        assert sent.text == "⏰ " + texts.MORNING_FOLLOWUP
        assert sent.reply_markup.inline_keyboard[0][0].callback_data == f"rm:{token}:a"
        assert env.queue.items == [] and (await state_of(env, token)).status == reminders.DONE  # exactly one more message


async def test_the_follow_up_is_dropped_when_the_user_wrote_after_the_check_in(owner_engine, db):
    from tests.helpers import new_message

    async with rem_env(owner_engine, db) as env:
        since = datetime.now(UTC) - timedelta(minutes=10)
        token = await follow_up(env, since=since)
        await new_message(owner_engine, env.user_id, "доброе утро", created_at=since + timedelta(minutes=3))
        before = len(env.session.sent())
        assert await deliver(env, token) == "skipped" and len(env.session.sent()) == before

        older = await follow_up(env, since=datetime.now(UTC))  # the only message of the user is older than this check-in
        env.queue.items.clear()
        assert await deliver(env, older) == "sent"


async def test_the_button_on_the_follow_up_answers_the_user(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        token = await follow_up(env)
        await deliver(env, token)
        await env.feed_update(make_callback_update(data=f"rm:{token}:a", user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=778))
        assert env.session.edits()[-1].text == texts.REMINDER_ACKED


async def test_the_follow_up_is_decided_by_the_first_check_in_of_a_morning(owner_engine, db):
    from ai_civ.bot import checkin

    async with rem_env(owner_engine, db) as env:
        morning = datetime(2026, 10, 7, 8, 30, tzinfo=UTC)

        async def wants(now: datetime) -> bool:
            return await checkin.wants_morning_followup(env.db, env.user_id, "UTC", now)

        async def log_row(hour: int, minute: int) -> None:
            stamp = morning.replace(hour=hour, minute=minute)
            await owner_exec(
                owner_engine,
                "INSERT INTO civ.proactive_checkin_log (user_id, slot_date, slot_time, sent_at) VALUES (:u, :d, :t, :s)",
                u=env.user_id, d=morning.date(), t=stamp.time(), s=stamp,
            )  # fmt: skip

        await log_row(8, 30)  # the claim of this very check-in
        assert await wants(morning) is True
        await log_row(9, 0)  # a second check-in of the same day: no follow-up
        assert await wants(morning) is False
        assert await wants(datetime(2026, 10, 8, 14, 0, tzinfo=UTC)) is False  # not the morning window


async def test_the_follow_up_is_queued_with_a_ten_minute_countdown_and_never_when_off_or_not_first(owner_engine, db, monkeypatch):
    from ai_civ.bot import checkin

    async with rem_env(owner_engine, db) as env:
        decide = {"go": True}

        async def fake(db_, tenant_id, timezone, now):
            return decide["go"]

        monkeypatch.setattr(checkin, "wants_morning_followup", fake)
        await env.orchestrator._morning_followup(env.tenant, None, None)
        [(args, countdown)] = env.queue.items
        assert countdown == 600 and args[0] == str(env.user_id)
        state = await state_of(env, args[1])
        assert state.text == texts.MORNING_FOLLOWUP and state.kind == "alarm" and not state.persistent
        assert state.extra["ack_button"] is True and "skip_if_user_wrote_since" in state.extra

        env.queue.items.clear()
        decide["go"] = False  # not a first morning check-in
        await env.orchestrator._morning_followup(env.tenant, None, None)
        assert env.queue.items == []

        decide["go"] = True
        env.orchestrator.settings = env.settings.model_copy(update={"checkin_morning_followup_seconds": 0})
        await env.orchestrator._morning_followup(env.tenant, None, None)
        assert env.queue.items == []


# ================================================================== loud pings and the voice mode


async def test_every_message_of_every_reminder_kind_asks_for_a_sound(owner_engine, db):
    async with rem_env(owner_engine, db) as env:
        alarm = await schedule(env)
        ping = await schedule(env, kind="ping", persistent=True, text="Отметься")
        plain = await schedule(env, kind="ping", persistent=False, text="Позвонить")
        await deliver(env, alarm, 0)
        await deliver(env, alarm, 1)
        await deliver(env, ping, 0)
        await deliver(env, plain, 0)
        sent = env.session.sent()
        assert len(sent) == 4 and all(m.disable_notification is False for m in sent)  # passed explicitly, never left to a default


def voice_env_settings(tmp_path, **kw):
    sound = tmp_path / "alarm.ogg"
    sound.write_bytes(b"OggS-pretend-opus")
    return {"reminders_voice_ping": True, "reminders_voice_file": str(sound), **kw}


async def test_with_the_voice_mode_on_the_repeats_of_an_alarm_are_voice_notes_and_the_first_message_is_text(owner_engine, db, tmp_path):
    async with rem_env(owner_engine, db, **voice_env_settings(tmp_path)) as env:
        token = await schedule(env)
        env.queue.items.clear()
        await deliver(env, token, 0)
        assert env.session.voices() == [] and env.session.sent()[-1].text == "⏰ Пора вставать!"
        await deliver(env, token, 1)
        [voice] = env.session.voices()
        assert voice.disable_notification is False and "Пора вставать!" in voice.caption and "(1/60)" in voice.caption
        assert voice.reply_markup.inline_keyboard[0][0].callback_data == f"rm:{token}:a"  # the button of the alarm travels with the voice note
        assert env.queue.items[-1] == ([str(env.user_id), token, 2], 180)  # and the chain goes on
        assert len(env.session.sent()) == 1  # no text message was sent for the repeat
        stored = [m["content"] for m in await env.messages() if m["role"] == "assistant"]
        assert len(stored) == 2  # the user's answer sees both pings in its context


async def test_the_voice_mode_is_off_by_default_and_only_for_alarms(owner_engine, db, tmp_path):
    async with rem_env(owner_engine, db) as env:  # default settings
        assert env.settings.reminders_voice_ping is False
        token = await schedule(env)
        await deliver(env, token, 0)
        await deliver(env, token, 1)
        assert env.session.voices() == [] and "(1/60)" in env.session.sent()[-1].text
    async with rem_env(owner_engine, db, **voice_env_settings(tmp_path)) as env:
        ping = await schedule(env, kind="ping", persistent=True, text="Отметься")
        await deliver(env, ping, 0)
        await deliver(env, ping, 1)
        assert env.session.voices() == [] and len(env.session.sent()) == 2  # a persistent ping is text even in the voice mode


async def test_a_missing_sound_file_or_a_refusal_of_telegram_falls_back_to_text(owner_engine, db, tmp_path):
    async with rem_env(owner_engine, db, reminders_voice_ping=True, reminders_voice_file=str(tmp_path / "nope.ogg")) as env:
        token = await schedule(env)
        env.queue.items.clear()
        await deliver(env, token, 0)
        assert await deliver(env, token, 1) == "sent"
        assert env.session.voices() == [] and "(1/60)" in env.session.sent()[-1].text and env.queue.items  # no file: the alarm still rings, in text
    async with rem_env(owner_engine, db, **voice_env_settings(tmp_path)) as env:
        from aiogram.exceptions import TelegramBadRequest

        env.session.fail_on("sendVoice", TelegramBadRequest, times=None)
        token = await schedule(env)
        env.queue.items.clear()
        await deliver(env, token, 0)
        assert await deliver(env, token, 1) == "sent"
        assert "(1/60)" in env.session.sent()[-1].text and env.queue.items  # Telegram refused the voice: text, and the chain goes on


# ---------------------------------------------------------------- a failed send must not end the alarm


async def test_a_failed_repeat_of_an_alarm_still_queues_the_next_one_and_the_claim_comes_back(owner_engine, db):
    from aiogram.exceptions import TelegramNetworkError

    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        env.queue.items.clear()
        await deliver(env, token, 0)
        env.queue.items.clear()
        env.session.fail_on("sendMessage", TelegramNetworkError, times=1)
        with pytest.raises(TelegramNetworkError):
            await deliver(env, token, 1)
        assert env.queue.items == [([str(env.user_id), token, 2], 180)]  # the chain survives even if Celery gives up on attempt 1
        assert await deliver(env, token, 1) == "sent"  # and Celery's own retry of attempt 1 still sends (the claim was given back)


async def test_a_failed_send_does_not_revive_an_answered_alarm_or_a_plain_reminder(owner_engine, db):
    from aiogram.exceptions import TelegramNetworkError

    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        await deliver(env, token, 0)
        await env.feed_update(make_callback_update(data=f"rm:{token}:a", user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=950))
        env.queue.items.clear()
        env.session.fail_on("sendMessage", TelegramNetworkError, times=None)
        assert await deliver(env, token, 1) == "skipped"  # answered: nothing is even attempted
        plain = await schedule(env, kind="ping", persistent=False, text="Позвонить")
        env.queue.items.clear()
        with pytest.raises(TelegramNetworkError):
            await deliver(env, plain, 0)
        assert env.queue.items == []  # a plain reminder is not a chain


async def test_the_continuation_failing_too_never_masks_the_original_error(owner_engine, db):
    from aiogram.exceptions import TelegramNetworkError

    async with rem_env(owner_engine, db) as env:
        token = await schedule(env)
        env.queue.items.clear()
        env.queue.error = ConnectionError("broker down")
        env.session.fail_on("sendMessage", TelegramNetworkError, times=1)
        with pytest.raises(TelegramNetworkError):
            await deliver(env, token, 0)

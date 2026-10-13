"""Rate limit (v1.1): 20 messages per minute per user, Redis fixed window, drop + one notice per window, fail-open.
The limiter itself runs on an in-memory fake Redis with a fake clock (``FakeRedis``: the commands it uses - MULTI/EXEC with SET NX EX + INCR, SET NX EX -
behave like the real server's; ``test_the_real_redis_*`` checks that against the real one when it is reachable). The flow tests use real PostgreSQL, the
real aiogram Dispatcher on a fake Telegram wire and a fake model."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import pytest

from ai_civ.bot import texts
from ai_civ.bot.handlers import build_dispatcher
from ai_civ.bot.ratelimit import WARN_EVERY_SECONDS, RateLimiter, Verdict
from ai_civ.config import Settings
from tests.helpers import new_persona, owner_exec
from tests.tg_fakes import make_topic_edited_update

LIMIT = 20
NOTICE = "Превышен лимит запросов. Пожалуйста, подождите"


class Clock:
    def __init__(self, now: float = 1_000_000_020.0) -> None:  # a moment 20 s into a window
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis, self._queue = redis, []

    def set(self, name, value, nx=False, ex=None):
        self._queue.append(("set", name, value, nx, ex))
        return self

    def incr(self, name):
        self._queue.append(("incr", name))
        return self

    async def execute(self):
        await self._redis._gate()
        results = []
        for op, *args in self._queue:  # one MULTI/EXEC: nothing else runs between the commands
            results.append(self._redis._set(*args) if op == "set" else self._redis._incr(*args))
        if self._redis.garbage:
            return [True, "not a number"]
        return results


class FakeRedis:
    """The subset of ``redis.asyncio.Redis`` the limiter uses, with expiry on a fake clock and failure injection."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.data: dict[str, tuple[Any, float | None]] = {}
        self.error: Exception | None = None
        self.hang = False
        self.garbage = False
        self.closed = False
        self.commands = 0

    async def _gate(self) -> None:
        self.commands += 1
        if self.hang:
            await asyncio.sleep(30)
        if self.error is not None:
            raise self.error

    def _alive(self, name: str) -> bool:
        if name not in self.data:
            return False
        if (expires := self.data[name][1]) is not None and expires <= self.clock.now:
            del self.data[name]
            return False
        return True

    def _set(self, name, value, nx=False, ex=None):
        if nx and self._alive(name):
            return None
        self.data[name] = (value, None if ex is None else self.clock.now + ex)
        return True

    def _incr(self, name):
        value = int(self.data[name][0]) + 1 if self._alive(name) else 1
        expires = self.data[name][1] if name in self.data else None
        self.data[name] = (value, expires)
        return value

    def pipeline(self, transaction=True):
        assert transaction, "the counter must be created and incremented in ONE transaction"
        return FakePipeline(self)

    async def set(self, name, value, nx=False, ex=None):
        await self._gate()
        return self._set(name, value, nx, ex)

    async def aclose(self):
        self.closed = True

    def ttl(self, name: str) -> float | None:
        return None if name not in self.data or self.data[name][1] is None else self.data[name][1] - self.clock.now

    def keys(self) -> list[str]:
        return [k for k in list(self.data) if self._alive(k)]


def make_limiter(**kw: Any) -> tuple[RateLimiter, FakeRedis, Clock]:
    clock = Clock()
    redis = FakeRedis(clock)
    return RateLimiter(redis, limit=kw.pop("limit", LIMIT), window_seconds=kw.pop("window_seconds", 60), clock=clock, **kw), redis, clock


# ================================================================== the limiter on its own


async def test_the_first_twenty_pass_the_twenty_first_is_dropped_with_one_notice_per_window():
    limiter, _redis, _clock = make_limiter()
    verdicts = [await limiter.hit(7) for _ in range(LIMIT + 4)]
    assert all(v.allowed and not v.notify for v in verdicts[:LIMIT])
    assert [(v.allowed, v.notify) for v in verdicts[LIMIT:]] == [(False, True), (False, False), (False, False), (False, False)]


async def test_the_next_window_starts_from_zero_and_the_notice_comes_again_once_per_window():
    limiter, _redis, clock = make_limiter()
    for _ in range(LIMIT + 2):
        await limiter.hit(7)  # the first window: limit reached, its notice already used
    clock.now += 60  # the next minute
    assert [(await limiter.hit(7)).allowed for _ in range(LIMIT)] == [True] * LIMIT
    over = await limiter.hit(7)
    assert (over.allowed, over.notify) == (False, True)  # over again, and THIS window's notice is still unused
    again = await limiter.hit(7)
    assert (again.allowed, again.notify) == (False, False)

async def test_every_user_has_their_own_counter():
    limiter, _redis, _clock = make_limiter()
    for _ in range(LIMIT + 5):
        await limiter.hit(1)
    assert (await limiter.hit(2)).allowed  # somebody else's spam costs nobody anything


async def test_a_counter_never_exists_without_an_expiry_and_dies_with_its_window():
    limiter, redis, clock = make_limiter()
    for _ in range(LIMIT + 1):
        await limiter.hit(7)
    assert redis.keys()  # the counter and the notice marker
    assert all(redis.ttl(k) == 120 for k in redis.keys())  # two windows: set together with the counter, in the same transaction
    clock.now += 121
    assert redis.keys() == []


async def test_a_window_is_the_wall_clock_minute_not_a_sliding_period():
    limiter, _redis, clock = make_limiter()
    clock.now = 1_000_000_079.0  # one second before a window ends (windows are [..020, ..080) here)
    for _ in range(LIMIT):
        await limiter.hit(7)
    assert (await limiter.hit(7)).allowed is False
    clock.now += 2  # the new window has begun
    assert (await limiter.hit(7)).allowed is True


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (-1, 60), (20, 0)])
def test_nonsense_limits_are_refused_at_construction(limit, window):
    with pytest.raises(ValueError):
        RateLimiter(object(), limit=limit, window_seconds=window)


# ---------------------------------------------------------------- fail-open


@pytest.mark.parametrize("error", [ConnectionError("refused"), OSError("unreachable"), TimeoutError(), RuntimeError("MISCONF")])
async def test_when_redis_is_unusable_every_message_passes(error):
    limiter, redis, _clock = make_limiter()
    redis.error = error
    verdicts = [await limiter.hit(7) for _ in range(LIMIT * 2)]
    assert all(v.allowed and not v.notify for v in verdicts)  # no limit and no notice while Redis is unusable


async def test_a_redis_that_hangs_costs_a_message_at_most_the_timeout_and_then_passes():
    limiter, redis, _clock = make_limiter(timeout_seconds=0.05)
    redis.hang = True
    started = asyncio.get_running_loop().time()
    assert (await limiter.hit(7)).allowed
    assert asyncio.get_running_loop().time() - started < 1.0


async def test_an_answer_that_is_not_a_number_counts_as_unusable_not_as_a_block():
    limiter, redis, _clock = make_limiter()
    redis.garbage = True
    assert (await limiter.hit(7)).allowed


async def test_a_dead_redis_is_reported_in_the_log_once_a_minute_not_once_per_message(caplog):
    limiter, redis, _clock = make_limiter()
    redis.error = ConnectionError("refused")
    with caplog.at_level(logging.DEBUG, logger="ai_civ.bot.ratelimit"):
        for _ in range(5):
            await limiter.hit(7)
        limiter._last_warning -= WARN_EVERY_SECONDS + 1  # a minute later
        await limiter.hit(7)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2 and "fail-open" in warnings[0].message  # the first failure, and the first one after a minute

async def test_a_failure_at_the_notice_step_also_fails_open():
    limiter, redis, _clock = make_limiter()
    for _ in range(LIMIT):
        await limiter.hit(7)
    redis.error = ConnectionError("gone")  # the counter said "over the limit", then Redis went away
    assert (await limiter.hit(7)).allowed


async def test_closing_the_limiter_closes_its_client_and_never_raises():
    limiter, redis, _clock = make_limiter()
    await limiter.aclose()
    assert redis.closed

    class Broken:
        async def aclose(self):
            raise RuntimeError("already closed")

    await RateLimiter(Broken(), limit=1, window_seconds=1).aclose()


# ---------------------------------------------------------------- built from the settings


def test_switched_off_there_is_no_limiter_and_no_connection():
    assert RateLimiter.from_settings(Settings(_env_file=None, rate_limit_enabled=False)) is None


def test_the_client_uses_the_celery_broker_by_default_with_short_timeouts_and_the_configured_limits(monkeypatch):
    seen: dict[str, Any] = {}

    def fake_from_url(url, **kwargs):
        seen.update(url=url, **kwargs)
        return FakeRedis(Clock())

    monkeypatch.setattr("redis.asyncio.Redis.from_url", fake_from_url)
    limiter = RateLimiter.from_settings(
        Settings(_env_file=None, celery_broker_url="redis://broker:6379/3", rate_limit_messages=5, rate_limit_window_seconds=30, rate_limit_redis_timeout_seconds=0.5)
    )
    assert seen["url"] == "redis://broker:6379/3" and seen["socket_timeout"] == 0.5 and seen["socket_connect_timeout"] == 0.5
    assert limiter is not None and (limiter.limit, limiter.window) == (5, 30)
    RateLimiter.from_settings(Settings(_env_file=None, rate_limit_redis_url="redis://cache:6379/9"))
    assert seen["url"] == "redis://cache:6379/9"  # an explicit URL wins


def test_the_defaults_are_twenty_messages_per_minute():
    s = Settings(_env_file=None)
    assert (s.rate_limit_enabled, s.rate_limit_messages, s.rate_limit_window_seconds) == (True, 20, 60)


# ================================================================== the whole thing: messages through the real dispatcher


def limited(env, **kw: Any) -> tuple[RateLimiter, FakeRedis, Clock]:
    """Put a limiter (on the fake Redis) into this stack's dispatcher."""
    limiter, redis, clock = make_limiter(**kw)
    env.dispatcher = build_dispatcher(env.db, env.orchestrator, limiter=limiter)
    return limiter, redis, clock


def says(env, thread_id: int | None = None) -> list[str]:
    return [m.text for m in env.session.sent() if m.chat_id == env.chat_id and m.message_thread_id == thread_id]


async def user_rows(env) -> int:
    return (await owner_exec(env.owner_engine, "SELECT count(*) FROM civ.messages WHERE user_id = :u AND role = 'user'", u=env.user_id))[0][0]


async def test_the_twenty_first_message_is_dropped_before_any_model_call_and_gets_the_fixed_text_once(bot_env):
    env = bot_env
    limited(env)
    for i in range(LIMIT):
        await env.feed(f"сообщение номер {i}")
    assert len(env.chat.calls) == LIMIT and len(says(env)) == LIMIT  # all answered

    routed = len(env.router.calls)  # the topic router of the main chat ran once per answered message
    for i in range(5):
        await env.feed(f"слишком много {i}")
    assert says(env)[LIMIT:] == [NOTICE]  # exactly ONE notice for five dropped messages
    assert texts.RATE_LIMITED == NOTICE
    assert len(env.chat.calls) == LIMIT and len(env.router.calls) == routed  # no model of any kind was called for the dropped ones
    assert await user_rows(env) == LIMIT  # a dropped message is not even stored


async def test_after_the_window_the_user_is_answered_again(bot_env):
    env = bot_env
    _limiter, _redis, clock = limited(env)
    for i in range(LIMIT + 2):
        await env.feed(f"сообщение {i}")
    clock.now += 60
    await env.feed("снова я")
    assert says(env)[-1] == "ok: снова я"


async def test_a_voice_note_counts_as_a_message_and_a_dropped_one_is_not_even_downloaded(bot_env):
    env = bot_env
    limited(env)
    for i in range(LIMIT - 1):
        await env.feed(f"текст {i}")
    await env.feed_voice()  # the twentieth
    assert len(env.session.downloads) == 1 and len(env.chat.transcribe_calls) == 1
    await env.feed_voice()  # the twenty-first
    assert len(env.session.downloads) == 1 and len(env.chat.transcribe_calls) == 1  # dropped before download and before the model is asked
    assert says(env)[-1] == NOTICE


async def test_commands_count_too(bot_env):
    env = bot_env
    limited(env)
    for i in range(LIMIT):
        await env.feed(f"сообщение {i}")
    before = len(env.session.sent())
    await env.feed("/app")
    assert [m.text for m in env.session.sent()[before:]] == [NOTICE]  # no Mini App button either


async def test_the_notice_goes_to_the_topic_where_the_user_wrote(bot_env):
    env = bot_env
    await new_persona(env.owner_engine, env.user_id, "sport", thread_id=501, title="🏋️ Спорт")
    limited(env)
    for i in range(LIMIT):
        await env.feed(f"подход {i}", thread_id=501)
    await env.feed("ещё подход", thread_id=501)
    assert says(env, 501)[-1] == NOTICE and NOTICE not in says(env, None)


async def test_service_messages_are_not_counted(bot_env):
    env = bot_env
    pid = await new_persona(env.owner_engine, env.user_id, "sport", thread_id=501, title="🏋️ Спорт")
    _limiter, redis, _clock = limited(env)
    for i in range(LIMIT + 10):
        await env.feed_update(
            make_topic_edited_update(name=f"Имя {i}", chat_id=env.chat_id, thread_id=501, user_id=env.telegram_user_id, message_id=9000 + i)
        )
    assert redis.keys() == []  # forum_topic_edited is not something the user wrote to the bot
    for i in range(LIMIT):
        await env.feed(f"сообщение {i}", thread_id=501)
    assert NOTICE not in says(env, 501) and pid  # the budget was untouched by the service messages


async def test_a_stranger_cannot_use_up_anybodys_budget_or_make_the_bot_speak(bot_env):
    env = bot_env
    _limiter, redis, _clock = limited(env)
    from tests.tg_fakes import make_update

    for i in range(LIMIT * 2):
        await env.feed_update(make_update(text=f"спам {i}", user_id=env.telegram_user_id + 1, chat_id=env.chat_id, message_id=800 + i))
    assert redis.keys() == [] and NOTICE not in says(env)  # unregistered senders never reach the limiter: no counter, no notice
    await env.feed("я владелец")
    assert says(env) == ["ok: я владелец"]


async def test_a_frozen_group_stays_silent_even_for_a_spammer(bot_env):
    env = bot_env
    await owner_exec(env.owner_engine, "UPDATE civ.users SET group_state = 'frozen' WHERE id = :u", u=env.user_id)
    _limiter, redis, _clock = limited(env)
    for i in range(LIMIT + 5):
        await env.feed(f"сообщение {i}")
    assert NOTICE not in says(env) and redis.keys() == []  # the bot says nothing in a frozen group: the limiter is never reached


async def test_when_redis_is_down_the_bot_keeps_answering(bot_env, caplog):
    env = bot_env
    _limiter, redis, _clock = limited(env)
    redis.error = ConnectionError("refused")
    with caplog.at_level(logging.WARNING, logger="ai_civ.bot.ratelimit"):
        for i in range(LIMIT + 10):
            await env.feed(f"сообщение {i}")
    assert len(env.chat.calls) == LIMIT + 10 and NOTICE not in says(env)
    assert len([r for r in caplog.records if "fail-open" in r.message]) == 1  # one warning, not thirty


async def test_the_default_dispatcher_of_a_stack_with_the_limiter_switched_off_has_none(bot_env):
    env = bot_env  # rate_limit_enabled=False in the test stack
    assert env.settings.rate_limit_enabled is False
    for i in range(LIMIT + 5):
        await env.feed(f"сообщение {i}")
    assert len(env.chat.calls) == LIMIT + 5  # nothing limits it


async def test_the_dispatcher_closes_the_limiters_client_on_shutdown(bot_env):
    env = bot_env
    _limiter, redis, _clock = limited(env)
    await env.dispatcher.emit_shutdown()
    assert redis.closed


# ================================================================== the real Redis (skipped when it is not running)


async def test_the_real_redis_gives_the_same_verdicts_and_every_key_has_an_expiry():
    redis_asyncio = pytest.importorskip("redis.asyncio")
    settings = Settings(_env_file=None)
    client = redis_asyncio.Redis.from_url(settings.celery_broker_url, socket_timeout=1, socket_connect_timeout=1, decode_responses=True)
    try:
        await client.ping()
    except Exception:  # noqa: BLE001
        await client.aclose()
        pytest.skip("Redis is not reachable on CELERY_BROKER_URL")
    prefix = f"civ:rl-test:{uuid.uuid4().hex}"
    limiter = RateLimiter(client, limit=3, window_seconds=60, prefix=prefix)
    try:
        verdicts = [await limiter.hit(42) for _ in range(6)]
        assert [(v.allowed, v.notify) for v in verdicts] == [(True, False)] * 3 + [(False, True), (False, False), (False, False)]
        keys = [k async for k in client.scan_iter(match=f"{prefix}:*")]
        assert len(keys) == 2  # the counter and the notice marker
        for key in keys:
            ttl = await client.ttl(key)
            assert 0 < ttl <= 120  # never -1 (no expiry)
        assert await limiter.hit(43) == Verdict(True)  # another user: untouched
    finally:
        for key in [k async for k in client.scan_iter(match=f"{prefix}:*")]:
            await client.delete(key)
        await client.aclose()

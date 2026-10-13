"""Onboarding end to end: real PostgreSQL + real aiogram Dispatcher/middleware, fake Telegram wire, scripted Gemini parser."""

from __future__ import annotations

from typing import Any

from aiogram.exceptions import TelegramBadRequest

from ai_civ.bot import onboarding, texts
from ai_civ.bot.proactive import get_trigger
from ai_civ.db.enums import OnboardingState
from ai_civ.memory.llm import LLMError
from tests.helpers import owner_exec
from tests.test_onboarding_pure import ANSWERS

PARSER_KEYS = {s.question: s.key for s in onboarding.STEPS}
TOPIC_PLAN = {
    "topics": [
        {"code": "job_search", "title": "💼 Поиск работы", "focus": "Help the user find a job."},
        {"code": "routine", "title": "🗓 Режим дня", "focus": "Keep the daily routine."},
        {"code": "growth", "title": "🌱 Развитие", "focus": "Support self-development."},
    ]
}


def scripted(overrides: dict[str, Any] | None = None):
    """Router/parser stand-in: finds the interview question in the prompt and answers with the scripted JSON."""

    def respond(prompt: str) -> Any:
        if "<goals>" in prompt:  # /setup, run by the interview itself: the goals -> topics planner
            return TOPIC_PLAN
        key = next(k for q, k in PARSER_KEYS.items() if q in prompt)
        value = (overrides or {}).get(key, ANSWERS[key][1])
        return value

    return respond


async def user_row(env) -> dict[str, Any]:
    row = (
        await owner_exec(
            env.owner_engine,
            "SELECT onboarding_state, onboarding_step, onboarding_draft, timezone, quiet_hours_start, quiet_hours_end, "
            "onboarding_completed_at FROM civ.users WHERE id = :u",
            u=env.user_id,
        )
    )[0]
    return dict(zip(["state", "step", "draft", "tz", "quiet_start", "quiet_end", "completed_at"], row, strict=True))


async def current_facts(env) -> dict[str, tuple[str, str, int, str]]:
    rows = await owner_exec(
        env.owner_engine,
        "SELECT key, value_text, category::text, priority, source::text FROM civ.core_facts "
        "WHERE user_id = :u AND valid_to IS NULL",
        u=env.user_id,
    )
    return {r[0]: (r[1], r[2], r[3], r[4]) for r in rows}


async def interview(env, upto: int = len(onboarding.STEPS)) -> None:
    """Opening message + the first ``upto`` answers (each as a real Telegram update)."""
    await env.feed("привет")
    for step in onboarding.STEPS[:upto]:
        await env.feed(ANSWERS[step.key][0])


# ---------------------------------------------------------------- the gate


async def test_a_new_tenant_is_interviewed_first_and_nothing_else_happens(onboarding_env):
    env = onboarding_env
    env.router.responder = scripted()
    await env.feed("привет")

    reply = env.session.sent_to(None)[-1].text
    assert reply.startswith(onboarding.INTRO) and "Вопрос 1 из 7" in reply
    user = await user_row(env)
    assert (user["state"], user["step"]) == ("in_progress", 1)
    assert env.router.calls == [] and env.chat.calls == []  # neither the parser nor the chat model was involved
    assert env.session.topics() == [] and await env.personas() == []  # the ecosystem is closed
    assert [m["role"] for m in await env.messages()] == ["user", "assistant"]  # the dialogue is still in `messages`


async def test_the_interview_runs_in_whatever_topic_the_user_writes_and_never_creates_topics(onboarding_env):
    env = onboarding_env
    await env.feed("привет", thread_id=77)  # a topic that is not bound to any persona
    assert env.session.sent_to(77)[-1].text.startswith(onboarding.INTRO)
    assert texts.UNKNOWN_TOPIC not in [m.text for m in env.session.sent()]
    assert env.session.topics() == []


async def test_a_full_interview_opens_the_ecosystem_and_writes_the_profile(onboarding_env):
    env = onboarding_env
    env.router.responder = scripted()
    await interview(env)

    summary = env.session.sent_to(None)[-1].text
    assert summary.startswith("Проверьте") and "Asia/Almaty" in summary
    assert (await user_row(env))["state"] == "in_progress" and await current_facts(env) == {}  # a draft, nothing stored yet

    await env.feed("да")

    user = await user_row(env)
    assert user["state"] == "done" and user["step"] == 0 and user["draft"] == {} and user["completed_at"] is not None
    assert user["tz"] == "Asia/Almaty"  # was Europe/Copenhagen
    assert (str(user["quiet_start"]), str(user["quiet_end"])) == ("23:00:00", "07:00:00")

    facts = await current_facts(env)
    assert facts["name"] == ("Анна", "identity", 90, "onboarding")
    assert facts["address_as"][0] == "Аня" and facts["city"][0] == "Алматы" and facts["goal.1"][0] == "найти работу"
    assert facts["communication.length"][0] == "short replies" and "communication.tone" not in facts
    schedule = await owner_exec(
        env.owner_engine,
        "SELECT trigger_key, local_time::text, enabled FROM civ.proactive_schedule WHERE user_id = :u ORDER BY 1",
        u=env.user_id,
    )
    assert [tuple(r) for r in schedule] == [("meal_check", "13:00:00", True), ("work_start", "09:00:00", True)]

    assert any(m.text.startswith("Готово, Аня") for m in env.session.sent_to(None))
    # /setup ran by itself: Settings + the topics the model built from the goals of question 5 (their own, not a fixed list)
    assert [t.name for t in env.session.topics()] == ["⚙️ Настройки", "💼 Поиск работы", "🗓 Режим дня", "🌱 Развитие"]
    assert len(await env.personas()) == 4
    planner = [c for c in env.router.calls if "<goals>" in c["prompt"]]
    assert len(planner) == 1 and "найти работу" in planner[0]["prompt"]


async def test_what_the_interview_configured_drives_the_proactive_schedule(onboarding_env):
    """The loop is closed: the times from the interview (in the user's NEW time zone) are what the dispatcher claims."""
    from datetime import UTC, datetime, timedelta

    from ai_civ.bot import schedule

    env = onboarding_env
    env.router.responder = scripted()
    await interview(env)
    await env.feed("да")
    grace = timedelta(minutes=30)

    copenhagen_1305 = datetime(2026, 1, 15, 12, 5, tzinfo=UTC)  # 13:05 in the OLD zone, 17:05 in Asia/Almaty
    assert await schedule.claim_due(env.db, env.user_id, now=copenhagen_1305, grace=grace) == []
    almaty_1305 = datetime(2026, 1, 15, 8, 5, tzinfo=UTC)  # 13:05 in Asia/Almaty (UTC+5)
    due = await schedule.claim_due(env.db, env.user_id, now=almaty_1305, grace=grace)
    assert [d.trigger_key for d in due] == ["meal_check"]
    late_night = datetime(2026, 1, 15, 18, 30, tzinfo=UTC)  # 23:30 in Asia/Almaty: inside the 23:00-07:00 quiet hours
    assert await schedule.claim_due(env.db, env.user_id, now=late_night, grace=grace) == []


async def test_the_profile_reaches_the_prompt_of_every_persona(onboarding_env):
    env = onboarding_env
    env.router.responder = scripted()
    await interview(env)
    await env.feed("да")
    topic = (await env.personas())[0]["thread_id"]
    await env.feed("привет", thread_id=topic)
    system = env.chat.calls[-1]["system"]
    assert "name [identity]: Анна" in system and "communication.formality" in system and "goal.1 [goal]" in system


# ---------------------------------------------------------------- bad answers, corrections, confirmation


async def test_an_answer_the_code_rejects_repeats_the_question_and_stores_nothing(onboarding_env):
    env = onboarding_env
    env.router.responder = scripted({"name": {"name": "Мария", "address_as": "Мария"}})  # the model invents a name
    await env.feed("привет")
    await env.feed("Меня зовут Анна")

    reply = env.session.sent_to(None)[-1].text
    assert "Не нашёл в ответе имя" in reply and reply.endswith(onboarding.question_text(1))
    user = await user_row(env)
    assert (user["step"], user["draft"]["answers"]) == (1, {})

    env.router.responder = scripted()  # now a sane parse
    await env.feed("Меня зовут Анна, можно Аня")
    assert (await user_row(env))["step"] == 2


async def test_a_point_can_be_corrected_from_the_summary_and_the_old_answer_is_replaced(onboarding_env):
    env = onboarding_env
    env.router.responder = scripted()
    await interview(env)
    await env.feed("5")
    assert env.session.sent_to(None)[-1].text.startswith(onboarding.REDO_PREFIX)
    env.router.responder = scripted({"goals": {"goals": ["выучить датский", "бегать"]}})
    await env.feed("Выучить датский и бегать")
    summary = env.session.sent_to(None)[-1].text
    assert summary.startswith("Проверьте") and "выучить датский" in summary and "найти работу" not in summary

    await env.feed("да")
    facts = await current_facts(env)
    assert facts["goal.1"][0] == "выучить датский" and facts["goal.2"][0] == "бегать"


async def test_an_unclear_confirmation_does_not_finish(onboarding_env):
    env = onboarding_env
    env.router.responder = scripted()
    await interview(env)
    await env.feed("ну, наверное")
    assert env.session.sent_to(None)[-1].text.startswith(onboarding.CONFIRM_UNCLEAR)
    assert (await user_row(env))["state"] == "in_progress"


# ---------------------------------------------------------------- failures and replays


async def test_a_failing_parser_gives_the_system_notice_and_keeps_the_state(onboarding_env):
    env = onboarding_env
    await env.feed("привет")
    env.router.responder = lambda prompt: LLMError("down")
    await env.feed("Меня зовут Анна")
    assert env.session.sent_to(None)[-1].text == texts.FAILURE
    assert (await user_row(env))["step"] == 1

    env.router.responder = scripted()
    await env.feed("Меня зовут Анна, можно Аня")  # the user simply tries again
    assert (await user_row(env))["step"] == 2


async def test_a_failed_send_leaves_the_interview_where_the_user_last_saw_it(onboarding_env):
    env = onboarding_env
    env.router.responder = scripted()
    await env.feed("привет")
    env.session.fail_on("sendMessage", TelegramBadRequest, times=2)  # the reply AND the failure notice are lost
    await env.feed("Меня зовут Анна, можно Аня")
    assert (await user_row(env))["step"] == 1  # state is saved only after the question was actually delivered

    await env.feed("Меня зовут Анна, можно Аня")
    assert (await user_row(env))["step"] == 2


async def test_a_replayed_update_is_not_interviewed_twice(onboarding_env):
    env = onboarding_env
    env.router.responder = scripted()
    await env.feed("привет")
    update = env.update("Меня зовут Анна, можно Аня")
    await env.dispatcher.feed_update(env.bot, update)
    sent_before, calls_before = len(env.session.sent()), len(env.router.calls)
    await env.dispatcher.feed_update(env.bot, update)  # Telegram redelivers the same update
    assert len(env.session.sent()) == sent_before and len(env.router.calls) == calls_before
    assert (await user_row(env))["step"] == 2


async def test_setup_before_onboarding_only_starts_or_repeats_the_interview(onboarding_env):
    env = onboarding_env
    await env.feed("/setup")
    assert env.session.sent_to(None)[-1].text.startswith(onboarding.INTRO)
    assert env.session.topics() == [] and (await user_row(env))["step"] == 1

    await env.feed("/setup")  # again: the same question, no restart
    assert env.session.sent_to(None)[-1].text == onboarding.question_text(1)
    assert env.session.topics() == []


async def test_no_proactive_message_for_someone_who_is_not_let_in_yet(onboarding_env):
    env = onboarding_env
    sent = await env.orchestrator.run_proactive(env.tenant, get_trigger("meal_check"))
    assert sent is False and env.session.sent() == [] and env.chat.calls == []


async def test_tenants_are_interviewed_independently(onboarding_env, owner_engine, db):
    from tests.bot_env import build_bot_env

    a = onboarding_env
    b = await build_bot_env(owner_engine, db, onboarding_state="done")
    await a.feed("привет")
    assert (await user_row(a))["state"] == "in_progress" and (await user_row(b))["state"] == "done"
    await b.bot.session.close()


# ---------------------------------------------------------------- re-running the interview (operator reset)


async def test_a_second_interview_versions_the_facts_and_closes_stale_ones(onboarding_env):
    env = onboarding_env
    env.router.responder = scripted({"goals": {"goals": ["цель а", "цель б", "цель в"]}})
    await interview(env)
    await env.feed("да")
    assert {"goal.1", "goal.2", "goal.3"} <= set(await current_facts(env))

    await owner_exec(
        env.owner_engine,
        "UPDATE civ.users SET onboarding_state = 'pending', onboarding_step = 0, onboarding_draft = '{}' WHERE id = :u",
        u=env.user_id,
    )
    env.router.responder = scripted({"goals": {"goals": ["единственная цель"]}})
    await interview(env)
    await env.feed("да")

    facts = await current_facts(env)
    assert facts["goal.1"][0] == "единственная цель" and "goal.2" not in facts and "goal.3" not in facts
    history = await owner_exec(
        env.owner_engine,
        "SELECT count(*) FILTER (WHERE valid_to IS NOT NULL), count(*) FROM civ.core_facts WHERE user_id = :u AND key = 'goal.1'",
        u=env.user_id,
    )
    assert tuple(history[0]) == (1, 2)  # the old version is kept, closed
    unchanged = await owner_exec(
        env.owner_engine,
        "SELECT count(*) FROM civ.core_facts WHERE user_id = :u AND key = 'name'",
        u=env.user_id,
    )
    assert unchanged[0][0] == 1  # an identical fact is not versioned again


async def test_finalize_refuses_an_incomplete_draft(onboarding_env):
    env = onboarding_env
    progress = onboarding.Progress(OnboardingState.in_progress, onboarding.CONFIRM_STEP, {"name": {"name": "x"}})
    async with env.db.tenant_session(env.user_id) as session:
        try:
            await onboarding.finalize(session, env.user_id, progress)
        except ValueError as exc:
            assert "cannot finalize" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("an incomplete draft must not be finalized")


async def test_a_concurrent_move_is_not_overwritten(onboarding_env):
    env = onboarding_env
    start = onboarding.Progress(OnboardingState.pending, 0, {})
    async with env.db.tenant_session(env.user_id) as session:
        assert await onboarding.save_progress(
            session, env.user_id, start, onboarding.Progress(OnboardingState.in_progress, 1, {})
        )
    async with env.db.tenant_session(env.user_id) as session:  # a stale writer still believes the state is `pending`
        assert not await onboarding.save_progress(
            session, env.user_id, start, onboarding.Progress(OnboardingState.in_progress, 2, {})
        )
    assert (await user_row(env))["step"] == 1

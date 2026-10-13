"""Module 3: the weekly review of the quarantine - batches, cards, buttons, and the transactional decisions."""

from __future__ import annotations

import uuid
from typing import Any

from aiogram.exceptions import TelegramBadRequest

from ai_civ.bot import review, texts
from ai_civ.bot.review import ReviewService, callback_data, parse_callback
from ai_civ.config import Settings
from tests.bot_env import build_bot_env
from tests.helpers import new_fact, new_pending, new_persona, owner_exec
from tests.tg_fakes import make_callback_update


def service(env, **kw) -> ReviewService:
    return ReviewService(env.db, env.bot, Settings(_env_file=None, **kw))


async def press(env, pending_id: uuid.UUID, keep: bool, *, message_id: int = 1, **kw: Any) -> None:
    update = make_callback_update(
        data=callback_data(pending_id, keep),
        user_id=kw.pop("user_id", env.telegram_user_id),
        chat_id=kw.pop("chat_id", env.chat_id),
        message_id=message_id,
    )
    await env.dispatcher.feed_update(env.bot, update)


def last_answer(env) -> str | None:
    return env.session.callback_answers()[-1].text


async def pending_row(env, pending_id) -> dict[str, Any]:
    row = (
        await owner_exec(
            env.owner_engine,
            "SELECT status::text, decided_at, review_batch_id FROM civ.pending_facts WHERE id = :i",
            i=pending_id,
        )
    )[0]
    return {"status": row[0], "decided_at": row[1], "batch": row[2]}


async def fact_versions(env, key: str) -> list[tuple[str, bool, str]]:
    rows = await owner_exec(
        env.owner_engine,
        "SELECT value_text, valid_to IS NULL, source::text FROM civ.core_facts WHERE user_id = :u AND key = :k "
        "ORDER BY valid_from, created_at",
        u=env.user_id,
        k=key,
    )
    return [tuple(r) for r in rows]


async def batches(env) -> list[tuple]:
    rows = await owner_exec(
        env.owner_engine,
        "SELECT sent_at IS NOT NULL, telegram_message_id, closed_at IS NOT NULL FROM civ.review_batches WHERE user_id = :u "
        "ORDER BY created_at",
        u=env.user_id,
    )
    return [tuple(r) for r in rows]


async def pending_update(env, key: str, value: str, target: uuid.UUID, *, change: str = "update") -> uuid.UUID:
    rows = await owner_exec(
        env.owner_engine,
        "INSERT INTO civ.pending_facts (user_id, subject, change, target_fact_id, category, key, value_text, evidence, "
        "source_message_ids, idempotency_key) VALUES (:u, 'core_fact', CAST(:c AS civ.change_type), :t, 'body', :k, "
        ":v, 'it is 80 now', ARRAY[gen_random_uuid()], :i) RETURNING id",
        u=env.user_id, c=change, t=target, k=key, v=None if change == "delete" else value, i=uuid.uuid4().hex,
    )  # fmt: skip
    return rows[0][0]


# ---------------------------------------------------------------- callbacks (pure)


def test_callback_data_roundtrips_and_fits_telegrams_limit():
    pid = uuid.uuid4()
    for keep in (True, False):
        data = callback_data(pid, keep)
        assert len(data.encode()) <= 64 and parse_callback(data) == (pid, keep)


def test_malformed_callback_data_is_rejected():
    pid = str(uuid.uuid4())
    for bad in ("", "rv", f"rv:{pid}", f"rv:{pid}:x", f"xx:{pid}:k", "rv:not-a-uuid:k", f"rv:{pid}:k:extra"):
        assert parse_callback(bad) is None


# ---------------------------------------------------------------- sending the weekly review


async def test_the_weekly_review_sends_a_header_and_one_card_with_buttons_per_proposal(bot_env):
    env = bot_env
    p1 = await new_pending(env.owner_engine, env.user_id, key="weight")
    p2 = await new_pending(env.owner_engine, env.user_id, key="height")

    assert await service(env).send_weekly(env.tenant) == 2

    sent = env.session.sent_to(None)  # no Settings topic yet: the main chat
    assert sent[0].text == review.HEADER and len(sent) == 3
    cards = sent[1:]
    assert "Запомнить о вас:\n• weight: 80 kg" in cards[0].text and "Основание: «said so»" in cards[0].text
    buttons = {parse_callback(b.callback_data) for m in cards for b in m.reply_markup.inline_keyboard[0]}
    assert buttons == {(p1, True), (p1, False), (p2, True), (p2, False)}
    assert [b.text for b in cards[0].reply_markup.inline_keyboard[0]] == ["✅ Оставить", "🗑 Удалить"]
    batch_ids = {(await pending_row(env, p))["batch"] for p in (p1, p2)}
    assert len(batch_ids) == 1 and None not in batch_ids  # both belong to the one batch
    (batch,) = await batches(env)
    assert batch[0] is True and batch[1] is not None and batch[2] is False  # sent, header id recorded, not closed


async def test_only_one_batch_per_week_and_no_empty_batches(bot_env):
    env = bot_env
    assert await service(env).send_weekly(env.tenant) == 0  # nothing pending
    assert await batches(env) == [] and env.session.sent() == []

    await new_pending(env.owner_engine, env.user_id, key="weight")
    assert await service(env).send_weekly(env.tenant) == 1
    await new_pending(env.owner_engine, env.user_id, key="height")
    assert await service(env).send_weekly(env.tenant) == 0  # a batch younger than 6 days exists
    assert len(await batches(env)) == 1

    await owner_exec(env.owner_engine, "UPDATE civ.review_batches SET created_at = now() - interval '7 days' WHERE user_id = :u", u=env.user_id)
    assert await service(env).send_weekly(env.tenant) == 1  # next week: the newcomer
    assert len(await batches(env)) == 2


async def test_the_review_goes_to_the_settings_topic_when_there_is_one(bot_env):
    env = bot_env
    await new_persona(env.owner_engine, env.user_id, code="settings", thread_id=321)
    await new_pending(env.owner_engine, env.user_id)
    await service(env).send_weekly(env.tenant)
    assert len(env.session.sent_to(321)) == 2 and env.session.sent_to(None) == []


async def test_a_week_sends_at_most_review_max_items_and_the_rest_waits(bot_env):
    env = bot_env
    ids = [await new_pending(env.owner_engine, env.user_id, key=k) for k in ("weight", "height", "waist")]
    svc = service(env, review_max_items=2)
    assert await svc.send_weekly(env.tenant) == 2
    assert (await pending_row(env, ids[2]))["batch"] is None  # still unassigned
    await owner_exec(env.owner_engine, "UPDATE civ.review_batches SET created_at = now() - interval '7 days' WHERE user_id = :u", u=env.user_id)
    assert await svc.send_weekly(env.tenant) == 1


async def test_proposals_nobody_answered_for_weeks_expire(bot_env):
    env = bot_env
    stale = await new_pending(env.owner_engine, env.user_id, key="weight")
    await owner_exec(env.owner_engine, "UPDATE civ.pending_facts SET created_at = now() - interval '40 days' WHERE id = :i", i=stale)
    assert await service(env).send_weekly(env.tenant) == 0
    assert (await pending_row(env, stale))["status"] == "expired" and env.session.sent() == []


async def test_a_send_that_failed_midway_is_resumed_not_forgotten(bot_env):
    env = bot_env
    pid = await new_pending(env.owner_engine, env.user_id)
    env.session.fail_on("sendMessage", TelegramBadRequest, times=1)
    try:
        await service(env).send_weekly(env.tenant)
    except TelegramBadRequest:
        pass
    else:  # pragma: no cover
        raise AssertionError("a Telegram failure must propagate (fail-fast; Celery retries)")
    assert await batches(env) == [(True, None, False)]  # created, but not recorded as delivered

    assert await service(env).send_weekly(env.tenant) == 1  # the retry re-sends the same batch
    (batch,) = await batches(env)
    assert batch[1] is not None and len(await batches(env)) == 1
    assert (await pending_row(env, pid))["status"] == "pending"


async def test_nothing_is_sent_to_a_tenant_who_is_not_let_in(bot_env):
    from dataclasses import replace

    from ai_civ.db.enums import OnboardingState

    env = bot_env
    await new_pending(env.owner_engine, env.user_id)
    pending = replace(env.tenant, onboarding_state=OnboardingState.pending)
    assert await service(env).send_weekly(pending) == 0 and env.session.sent() == []


# ---------------------------------------------------------------- decisions: keep


async def test_keeping_a_proposal_writes_the_fact_in_one_transaction_and_closes_the_batch(bot_env):
    env = bot_env
    p1 = await new_pending(env.owner_engine, env.user_id, key="weight")
    p2 = await new_pending(env.owner_engine, env.user_id, key="height")
    await service(env).send_weekly(env.tenant)

    await press(env, p1, True)
    assert await fact_versions(env, "weight") == [("80 kg", True, "approved_review")]
    row = await pending_row(env, p1)
    assert row["status"] == "approved" and row["decided_at"] is not None
    assert last_answer(env) == "Оставлено ✅"
    assert env.session.edits()[-1].text.endswith("✅ Оставлено") and "weight: 80 kg" in env.session.edits()[-1].text
    link = await owner_exec(env.owner_engine, "SELECT source_pending_id FROM civ.core_facts WHERE key = 'weight' AND user_id = :u", u=env.user_id)
    assert link == [(p1,)]  # the version points at the proposal the user approved
    assert (await batches(env))[0][2] is False  # one card is still open

    await press(env, p2, False)  # reject the other one
    assert (await pending_row(env, p2))["status"] == "rejected" and await fact_versions(env, "height") == []
    assert (await batches(env))[0][2] is True  # every card decided: the batch is closed
    assert env.session.sent()[-1].text == review.ALL_DONE


async def test_an_update_closes_the_old_version_and_the_card_shows_old_and_new(bot_env):
    env = bot_env
    old = await new_fact(env.owner_engine, env.user_id, "weight", category="body", value="90 kg")
    pid = await pending_update(env, "weight", "80 kg", old)
    await service(env).send_weekly(env.tenant)
    card = env.session.sent_to(None)[-1].text
    assert "weight: было «90 kg» → станет «80 kg»" in card

    await press(env, pid, True)

    assert await fact_versions(env, "weight") == [("90 kg", False, "user_manual"), ("80 kg", True, "approved_review")]


async def test_approving_a_value_the_user_already_set_creates_no_new_version(bot_env):
    env = bot_env
    old = await new_fact(env.owner_engine, env.user_id, "weight", category="body", value="80 kg")
    pid = await pending_update(env, "weight", "80 kg", old)
    await service(env).send_weekly(env.tenant)
    await press(env, pid, True)
    assert await fact_versions(env, "weight") == [("80 kg", True, "user_manual")]
    assert (await pending_row(env, pid))["status"] == "approved"


async def test_approving_a_delete_closes_the_target_fact(bot_env):
    env = bot_env
    target = await new_fact(env.owner_engine, env.user_id, "weight", category="body", value="90 kg")
    pid = await pending_update(env, "weight", "", target, change="delete")
    await service(env).send_weekly(env.tenant)
    assert "Забыть:\n• weight: 90 kg" in env.session.sent_to(None)[-1].text
    await press(env, pid, True)
    assert await fact_versions(env, "weight") == [("90 kg", False, "user_manual")]


async def test_keeping_an_emotion_makes_a_user_confirmed_state_with_overrides_resolved_by_code(bot_env):
    env = bot_env
    principle = await new_fact(env.owner_engine, env.user_id, "principle.no_danish", priority=50)
    pid = await new_pending(env.owner_engine, env.user_id, subject="emotional_state")
    await owner_exec(
        env.owner_engine,
        "UPDATE civ.pending_facts SET emotion_override_keys = ARRAY['principle.no_danish', 'unknown.key'] WHERE id = :i",
        i=pid,
    )
    await service(env).send_weekly(env.tenant)
    assert "Запомнить состояние (временно):\n• in love, интенсивность 9/10" in env.session.sent_to(None)[-1].text

    await press(env, pid, True)

    states = await owner_exec(
        env.owner_engine,
        "SELECT label, source::text, priority, source_pending_id, expires_at > now() FROM civ.emotional_states WHERE user_id = :u",
        u=env.user_id,
    )
    assert [tuple(s) for s in states] == [("in love", "user_confirmed", 100, pid, True)]
    overrides = await owner_exec(env.owner_engine, "SELECT core_fact_id FROM civ.emotional_overrides WHERE user_id = :u", u=env.user_id)
    assert overrides == [(principle,)]  # the unknown key was ignored
    view = await owner_exec(env.owner_engine, "SELECT is_overridden FROM civ.effective_principles WHERE user_id = :u", u=env.user_id)
    assert view == [(True,)]  # 100 > 50: the SQL view decides, as always


async def test_a_state_that_already_ended_cannot_be_confirmed(bot_env):
    env = bot_env
    pid = await new_pending(env.owner_engine, env.user_id, subject="emotional_state")
    await service(env).send_weekly(env.tenant)
    await owner_exec(env.owner_engine, "UPDATE civ.pending_facts SET emotion_expires_at = now() - interval '1 hour' WHERE id = :i", i=pid)
    await press(env, pid, True)
    assert (await pending_row(env, pid))["status"] == "expired"
    assert await owner_exec(env.owner_engine, "SELECT 1 FROM civ.emotional_states WHERE user_id = :u", u=env.user_id) == []
    assert last_answer(env) == review.ANSWERS["expired"]


# ---------------------------------------------------------------- decisions: delete, repeats, safety


async def test_deleting_is_final_and_a_second_press_changes_nothing(bot_env):
    env = bot_env
    pid = await new_pending(env.owner_engine, env.user_id, key="weight")
    await service(env).send_weekly(env.tenant)

    await press(env, pid, False)
    assert (await pending_row(env, pid))["status"] == "rejected" and await fact_versions(env, "weight") == []
    assert env.session.edits()[-1].text.endswith("🗑 Удалено")
    edits_before = len(env.session.edits())

    await press(env, pid, True)  # the user taps "keep" on the same card afterwards
    assert (await pending_row(env, pid))["status"] == "rejected" and await fact_versions(env, "weight") == []
    assert last_answer(env) == review.ANSWERS["already"] and len(env.session.edits()) == edits_before


async def test_garbage_buttons_are_answered_and_change_nothing(bot_env):
    env = bot_env
    pid = await new_pending(env.owner_engine, env.user_id)
    update = make_callback_update(data="rv:garbage", user_id=env.telegram_user_id, chat_id=env.chat_id, message_id=1)
    await env.dispatcher.feed_update(env.bot, update)
    assert last_answer(env) == "Неизвестная кнопка" and (await pending_row(env, pid))["status"] == "pending"


async def test_another_tenant_cannot_decide_my_proposal(bot_env, owner_engine, db):
    env = bot_env
    other = await build_bot_env(owner_engine, db)
    pid = await new_pending(env.owner_engine, env.user_id, key="weight")
    await service(env).send_weekly(env.tenant)

    await press(other, pid, True, user_id=other.telegram_user_id, chat_id=other.chat_id)  # B presses A's button

    assert (await pending_row(env, pid))["status"] == "pending" and await fact_versions(env, "weight") == []
    assert last_answer(other) == review.ANSWERS["missing"]  # RLS: B cannot even see it
    await other.bot.session.close()


async def test_buttons_of_strangers_and_of_foreign_chats_are_ignored(bot_env):
    env = bot_env
    pid = await new_pending(env.owner_engine, env.user_id)
    await press(env, pid, True, user_id=env.telegram_user_id + 1)  # not a registered user
    await press(env, pid, True, chat_id=env.chat_id - 1)  # a registered user, but not in their own forum group
    assert env.session.callback_answers() == [] and (await pending_row(env, pid))["status"] == "pending"


async def test_a_failing_decision_is_reported_to_the_user_and_does_not_stop_the_bot(bot_env, monkeypatch):
    env = bot_env
    pid = await new_pending(env.owner_engine, env.user_id)

    async def boom(self, *args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(ReviewService, "decide", boom)
    await press(env, pid, True)
    answer = env.session.callback_answers()[-1]
    assert answer.text == texts.FAILURE and answer.show_alert is True
    assert (await pending_row(env, pid))["status"] == "pending"


async def test_a_new_tenants_review_never_mixes_tenants(owner_engine, db):
    a, b = await build_bot_env(owner_engine, db), await build_bot_env(owner_engine, db)
    pa = await new_pending(owner_engine, a.user_id, key="weight")
    await new_pending(owner_engine, b.user_id, key="height")
    assert await service(a).send_weekly(a.tenant) == 1
    assert [parse_callback(btn.callback_data)[0] for m in a.session.sent()[1:] for btn in m.reply_markup.inline_keyboard[0]] == [pa, pa]
    assert b.session.sent() == []
    await a.bot.session.close()
    await b.bot.session.close()

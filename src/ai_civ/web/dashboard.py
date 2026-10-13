"""Screen "Дашборд": one read-only endpoint with the user's own aggregates for the last N local days.

Every query runs inside ONE ``tenant_session`` (RLS: only the caller's rows exist for it) and groups by the tenant's LOCAL calendar day
(``users.timezone``, so a night-time answer belongs to the day the user lived it in). The answer is compact JSON for a chart: ``dates`` is the
x axis and every ``daily`` series is a plain array aligned with it (zeros for quiet days, ``null`` where "no data" differs from zero).
Nothing here writes, and nothing is computed by a model.

What each number really is (the page should not promise more):
  * mood      - ``labels`` / ``valence``: only states the user CONFIRMED (the review-card path, priority > 60); a day without one is ``null``, not "fine".
                ``valence`` is a fixed scale of the closed mood vocabulary (``VALENCE``), averaged over that day's states.
                ``daily``: an ESTIMATE of each day (-2..+2) that a model read from the user's own messages and voice transcripts of that day (``civ.daily_mood``,
                ``bot/mood.py``); a day with too few messages stays ``null``. ``week``: the current local week Monday..Sunday with the same estimates, for the
                weekday chart; days that have not come yet are marked ``future``.
  * tasks     - the STEPS of plans (rows with a parent); "closed" is dated by ``updated_at`` (a step is only touched when it is pressed).
  * cards     - ``card_reviews`` keeps the LAST answer of each card, so "reviewed" counts cards by the day of their latest answer and accuracy is
                that latest answer's correctness, not a full history.
  * proactive - the logged check-ins; "answered" = the user wrote within ``ANSWER_WINDOW_HOURS`` after it.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import text

from ai_civ.web.deps import DB, Auth

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DEFAULT_DAYS = 14
MAX_DAYS = 90
ANSWER_WINDOW_HOURS = 6
CATEGORIES = ("focus", "intellect", "body", "connection")
VALENCE = {  # a fixed reading of the closed vocabulary civ.shared_mood, -2 (heavy) .. +2 (bright); the page may draw it, nothing decides on it
    "energetic": 2, "good": 1, "calm": 1, "tired": -1, "stressed": -1, "irritated": -1, "sad": -2, "overwhelmed": -2,
}  # fmt: skip

# every statement takes :tz, :start (first local date), :end (last local date, inclusive); the window is [start 00:00, end+1 00:00) local time
_WINDOW = "CAST(:start AS date)::timestamp AT TIME ZONE :tz"
_WINDOW_END = "(CAST(:end AS date) + 1)::timestamp AT TIME ZONE :tz"

SQL_TODAY = "SELECT (now() AT TIME ZONE :tz)::date"
SQL_MOOD = f"""
    SELECT (s.started_at AT TIME ZONE :tz)::date AS d, s.mood::text AS mood, s.priority
    FROM civ.emotional_states s
    WHERE s.source = 'user_confirmed' AND s.mood IS NOT NULL AND s.status <> 'dismissed'
      AND s.started_at >= {_WINDOW} AND s.started_at < {_WINDOW_END}
    ORDER BY s.priority DESC, s.started_at DESC, s.id
"""
SQL_DAILY_MOOD = """
    SELECT local_date, score, mood::text, message_count FROM civ.daily_mood
    WHERE local_date BETWEEN CAST(:start AS date) AND CAST(:end AS date)
"""
SQL_TASKS = f"""
    SELECT 'created' AS kind, (t.created_at AT TIME ZONE :tz)::date AS d, count(*)::int AS n
    FROM civ.tasks t WHERE t.parent_task_id IS NOT NULL AND t.created_at >= {_WINDOW} AND t.created_at < {_WINDOW_END} GROUP BY 2
    UNION ALL
    SELECT t.status, (t.updated_at AT TIME ZONE :tz)::date, count(*)::int
    FROM civ.tasks t WHERE t.parent_task_id IS NOT NULL AND t.status IN ('done', 'dropped')
      AND t.updated_at >= {_WINDOW} AND t.updated_at < {_WINDOW_END} GROUP BY t.status, 2
"""
SQL_TASKS_NOW = """
    SELECT count(*) FILTER (WHERE parent_task_id IS NOT NULL AND status = 'open')::int,
           count(*) FILTER (WHERE parent_task_id IS NULL AND status = 'open')::int
    FROM civ.tasks
"""
SQL_CARDS = f"""
    SELECT (r.reviewed_at AT TIME ZONE :tz)::date AS d, count(*)::int AS reviewed,
           count(*) FILTER (WHERE r.last_chosen = c.correct_index)::int AS correct
    FROM civ.card_reviews r JOIN civ.cards c ON c.user_id = r.user_id AND c.id = r.card_id
    WHERE r.reviewed_at >= {_WINDOW} AND r.reviewed_at < {_WINDOW_END}
    GROUP BY 1
"""
SQL_CARDS_NOW = f"""
    SELECT count(*) FILTER (WHERE next_review_at < {_WINDOW_END})::int, count(*)::int FROM civ.card_reviews
"""
SQL_XP = f"""
    SELECT (l.created_at AT TIME ZONE :tz)::date AS d, l.category,
           COALESCE(sum(l.amount) FILTER (WHERE l.amount > 0), 0)::int AS earned,
           COALESCE(sum(l.amount) FILTER (WHERE l.amount < 0), 0)::int AS lost
    FROM civ.xp_ledger l
    WHERE l.created_at >= {_WINDOW} AND l.created_at < {_WINDOW_END}
    GROUP BY 1, 2
"""
SQL_LEVEL = "SELECT total_xp, level, current_level_xp, xp_today FROM civ.character_progress"
SQL_PROACTIVE = f"""
    SELECT l.slot_date AS d, count(*)::int AS sent,
           count(*) FILTER (WHERE EXISTS (
               SELECT 1 FROM civ.messages m
               WHERE m.user_id = l.user_id AND m.role = 'user' AND m.created_at > l.sent_at
                 AND m.created_at <= l.sent_at + make_interval(hours => {ANSWER_WINDOW_HOURS})
           ))::int AS answered
    FROM civ.proactive_checkin_log l
    WHERE l.slot_date BETWEEN CAST(:start AS date) AND CAST(:end AS date)
    GROUP BY 1
"""


def _series(dates: list[date], values: dict[date, int]) -> list[int]:
    return [values.get(d, 0) for d in dates]


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


@router.get("/stats")
async def stats(ctx: Auth, db: DB, days: int = Query(DEFAULT_DAYS, ge=1, le=MAX_DAYS)) -> dict[str, Any]:
    tz = ctx.timezone
    async with db.tenant_session(ctx.tenant_id) as session:
        async def run(sql: str, **extra: Any):
            return (await session.execute(text(sql), {"tz": tz, **extra})).all()

        today: date = (await run(SQL_TODAY))[0][0]
        window = {"start": today - timedelta(days=days - 1), "end": today}
        mood_rows = await run(SQL_MOOD, **window)
        week_start = today - timedelta(days=today.weekday())  # Monday of the current local week
        estimate_rows = await run(SQL_DAILY_MOOD, start=min(window["start"], week_start), end=today)
        task_rows = await run(SQL_TASKS, **window)
        open_steps, open_goals = (await run(SQL_TASKS_NOW))[0]
        card_rows = await run(SQL_CARDS, **window)
        due_today, cards_total = (await run(SQL_CARDS_NOW, **window))[0]
        xp_rows = await run(SQL_XP, **window)
        level = (await run(SQL_LEVEL))[0]
        proactive_rows = await run(SQL_PROACTIVE, **window)

    dates = [window["start"] + timedelta(days=i) for i in range(days)]

    # mood: the dominant state of a day = the highest priority (rows arrive ordered), valence = the mean over the day's states
    by_day: dict[date, list[tuple[str, int]]] = {}
    for d, mood, priority in mood_rows:
        by_day.setdefault(d, []).append((mood, priority))
    mood_labels = [by_day[d][0][0] if d in by_day else None for d in dates]
    mood_valence = [round(sum(VALENCE[m] for m, _ in by_day[d]) / len(by_day[d]), 2) if d in by_day else None for d in dates]
    mood_counts = [len(by_day.get(d, ())) for d in dates]

    estimates = {d: (score, mood, n) for d, score, mood, n in estimate_rows}
    week = []
    for offset in range(7):
        day = week_start + timedelta(days=offset)
        score, mood, n = estimates.get(day, (None, None, 0))
        week.append({"date": day.isoformat(), "weekday": offset, "score": score, "mood": mood, "messages": n, "future": day > today})

    per_kind: dict[str, dict[date, int]] = {"created": {}, "done": {}, "dropped": {}}
    for kind, d, n in task_rows:
        per_kind[kind][d] = n

    reviewed = {d: n for d, n, _ in card_rows}
    correct = {d: c for d, _, c in card_rows}
    reviewed_total, correct_total = sum(reviewed.values()), sum(correct.values())

    # Since the decay system (migration 0023) the ledger holds penalties too: the per-branch series are NET (what the progress bars show), ``earned`` and
    # ``penalty`` split the total of a day into what was awarded and what the nights took away (``penalty`` is <= 0).
    xp_by_cat: dict[str, dict[date, int]] = {c: {} for c in CATEGORIES}
    earned: dict[date, int] = {}
    lost: dict[date, int] = {}
    for d, category, gained, taken in xp_rows:
        xp_by_cat[category][d] = gained + taken
        earned[d] = earned.get(d, 0) + gained
        lost[d] = lost.get(d, 0) + taken
    xp_daily = {c: _series(dates, xp_by_cat[c]) for c in CATEGORIES}
    xp_daily["total"] = [sum(xp_daily[c][i] for c in CATEGORIES) for i in range(days)]
    xp_daily["earned"] = _series(dates, earned)
    xp_daily["penalty"] = _series(dates, lost)

    sent = {d: s for d, s, _ in proactive_rows}
    answered = {d: a for d, _, a in proactive_rows}
    sent_total, answered_total = sum(sent.values()), sum(answered.values())

    return {
        "timezone": tz,
        "days": days,
        "from": window["start"].isoformat(),
        "to": today.isoformat(),
        "dates": [d.isoformat() for d in dates],
        "mood": {
            "labels": mood_labels,
            "valence": mood_valence,
            "counts": mood_counts,
            "scale": VALENCE,
            "days_with_data": len(by_day),
            "daily": {
                "score": [estimates[d][0] if d in estimates else None for d in dates],
                "mood": [estimates[d][1] if d in estimates else None for d in dates],
                "messages": [estimates[d][2] if d in estimates else 0 for d in dates],
                "days_with_data": sum(1 for d in dates if d in estimates),
            },
            "week": week,
        },
        "tasks": {
            "created": sum(per_kind["created"].values()),
            "done": sum(per_kind["done"].values()),
            "dropped": sum(per_kind["dropped"].values()),
            "open_backlog": open_steps,
            "open_goals": open_goals,
            "daily": {kind: _series(dates, per_kind[kind]) for kind in ("created", "done", "dropped")},
        },
        "cards": {
            "reviewed": reviewed_total,
            "correct": correct_total,
            "accuracy": _ratio(correct_total, reviewed_total),
            "due_today": due_today,
            "total": cards_total,
            "daily": {"reviewed": _series(dates, reviewed), "correct": _series(dates, correct)},
        },
        "xp": {
            "total": sum(xp_daily["total"]),
            "earned": sum(xp_daily["earned"]),
            "penalty": sum(xp_daily["penalty"]),
            "by_category": {c: sum(xp_daily[c]) for c in CATEGORIES},
            "daily": xp_daily,
            "all_time": level.total_xp,
            "level": level.level,
            "current_level_xp": level.current_level_xp,
            "today": level.xp_today,
        },
        "proactive": {
            "checkins_sent": sent_total,
            "checkins_answered": answered_total,
            "response_rate": _ratio(answered_total, sent_total),
            "answer_window_hours": ANSWER_WINDOW_HOURS,
            "daily": {"sent": _series(dates, sent), "answered": _series(dates, answered)},
        },
    }

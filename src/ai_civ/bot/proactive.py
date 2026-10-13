"""Proactivity: the bot writes first. A trigger = (persona/topic, instruction for the model). The WHEN is the caller's job:
a Celery Beat entry, an ETA, or later the events table fed by the Mini App (see ``ai_civ.tasks.send_proactive``).

The instruction is code, not user input and not model output, so a trigger can never be turned into a prompt injection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time


def in_quiet_hours(moment: time, start: time, end: time) -> bool:
    """Is the local time ``moment`` inside the quiet window [start, end)? The window may wrap around midnight
    (22:00-08:00). ``start == end`` is not a window (the database forbids it)."""
    if start < end:
        return start <= moment < end
    return moment >= start or moment < end


CHECK_IN = "check_in"  # the bot writes first when the user has been silent for a while (see ``schedule.claim_checkin``)


class UnknownTrigger(KeyError):
    """No trigger with this key (a typo in a Beat entry). Permanent: retrying cannot help."""


@dataclass(frozen=True)
class ProactiveTrigger:
    key: str
    persona_code: str
    instruction: str
    # What the Mini App's reminders screen shows (Russian: the product speaks to its users in Russian). A trigger added here
    # appears on that screen by itself; ``default_time`` is the time offered when the user switches it on for the first time.
    title: str = ""
    description: str = ""
    default_time: time = time(12, 0)
    # False = not a per-user reminder with a time of its own (it has its own dispatcher, like ``check_in``): hidden from the reminders screen.
    scheduled: bool = True


_TRIGGERS = (
    ProactiveTrigger(
        key="meal_check",
        persona_code="foodguide",
        instruction=(
            "Ask the user, in one or two short sentences, whether they have eaten and what. Use their profile, "
            "principles and the recent conversation; do not invent facts about their meals."
        ),
        title="Напоминание о еде",
        description="Спрошу, поели ли вы и что именно.",
        default_time=time(13, 0),
    ),
    ProactiveTrigger(
        key="work_start",
        persona_code="discipline",
        instruction=(
            "Tell the user, briefly and without nagging, that it is time to start working, and ask what the first "
            "task will be. Take their principles and current state into account."
        ),
        title="Начало работы",
        description="Напомню приступить к делам и спрошу, с чего начнёте.",
        default_time=time(9, 0),
    ),
    ProactiveTrigger(
        key=CHECK_IN,
        persona_code="",  # no persona: the message goes to the main chat
        instruction=(
            "The user has not written for a while. Write ONE short, natural message that keeps the conversation alive: pick a single concrete "
            "thing from their goals, plans, habits or the recent conversation and ask about it or build on it, like a friend who remembers. "
            "No questionnaire, no list of questions, no remarks about the silence, no pressure. If nothing concrete stands out, a light open "
            "question about their day is enough. Respect the current state section: when they are overloaded, be gentle and ask for nothing. "
            "You may ask about the status of ONE open focus task, how they feel, whether they have had lunch (only if the check-in context says it is a meal window) "
            "or suggest a short warm-up (only if the context says so). Use only facts from the profile and the check-in context; never invent tasks or meals."
        ),
        title="Живой разговор",
        scheduled=False,
    ),
)

TRIGGERS: dict[str, ProactiveTrigger] = {t.key: t for t in _TRIGGERS}


def get_trigger(key: str) -> ProactiveTrigger:
    try:
        return TRIGGERS[key]
    except KeyError:
        raise UnknownTrigger(f"unknown proactive trigger {key!r}; known: {sorted(TRIGGERS)}") from None

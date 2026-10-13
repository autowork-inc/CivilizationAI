"""Autonomous topic renaming: the model may call ``rename_current_topic(new_name)`` when the subject of a topic has clearly changed.

Same division of labour as the other functions. The model proposes a name; the CODE validates it (``parse_rename``), takes the right to rename
in the database (``personas.claim_rename``: a cooldown that survives restarts and cannot be raced), renames in Telegram through the existing
safe wrapper ``Orchestrator.rename_topic`` and tells the user itself with a fixed text. What the model says next to the call is never trusted:
the reply is phrased again, with the note below, and the model is told not to mention the rename.

The function is OFFERED only where a rename makes sense and is allowed (``personas.rename_allowed``: a topic the bot made, not Settings, cooldown
passed), so inside the cooldown the model does not even know the function exists - it cannot be nagged into spamming Telegram. Telegram has no
published rate limit for ``editForumTopic``; the limit is ours: one rename per ``TOPIC_RENAME_COOLDOWN_HOURS`` per topic (24 h) and, after the user
renamed a topic by hand, ``TOPIC_RENAME_USER_COOLDOWN_DAYS`` (7 days) - the user's own name is respected.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ai_civ.bot import personas
from ai_civ.memory.llm import ToolCall, ToolSpec

TOOL_NAME = "rename_current_topic"

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Rename THIS topic. Call it only when the user has clearly moved to a completely different, substantial subject that will keep "
        "being discussed here (not a tangent, a one-off question or a passing mention) and the current name no longer describes the topic "
        "or is too vague to. Renaming is rare and the user notices it: when in doubt, do NOT call it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "new_name": {
                "type": "string",
                "description": f"The new name: a short noun phrase in the user's language, at most {personas.MAX_TITLE_CHARS} characters "
                "(an emoji at the start is welcome).",
            }
        },
        "required": ["new_name"],
    },
)


def instructions(title: str) -> str:
    """The section of the system prompt, with the current name written in (the model must compare the conversation with it)."""
    shown = " ".join(title.split()).replace('"', "'")
    return (
        "## Topic name\n"
        f'This topic is called "{shown}". You can rename it with rename_current_topic, but only in one situation: the user has clearly\n'
        "started talking about a completely different, substantial subject (for example the topic is \"Дипломат\" and the user now works\n"
        "on learning Danish: rename it \"Изучение датского\"), the new subject is not a tangent or a single question, and the current name\n"
        "is outdated or too abstract to describe it. Never rename because of one message, to fix wording, or when the name still fits.\n"
        "Renaming is rare (at most once a day): when in doubt, do not call it. Do not tell the user about it and never say it was done:\n"
        "the system renames the topic right after your reply and tells the user itself."
    )


def render_note(new_title: str) -> str:
    """The section of the prompt for the turn that phrases the reply once a rename has been decided."""
    shown = " ".join(new_title.split()).replace('"', "'")
    return (
        "## Topic rename (made by the system)\n"
        f'The system will rename this topic to "{shown}" right after your reply and tells the user itself. Do not mention the rename and\n'
        "do not say it was done; just answer the user's message as usual."
    )


def parse_rename(calls: Sequence[ToolCall], current_title: str) -> str | None:
    """The new topic name from the first valid call, or None (no call, or an unusable one: dropped silently - the user did not ask for it).
    One rename per message at most. The name is normalised exactly like every topic name of the bot (one line, <= 64, always an emoji)."""
    for call in calls:
        if call.name != TOOL_NAME:
            continue
        return _valid_name(call.args, current_title)
    return None


def _valid_name(args: dict[str, Any], current_title: str) -> str | None:
    raw = args.get("new_name")
    if not isinstance(raw, str):
        return None
    line = " ".join(raw.split())
    if not line or len(line) > personas.MAX_TITLE_CHARS or personas.has_hidden_chars(line):
        return None  # too long is refused, not cut: a cut name is a name nobody chose
    title = personas.normalize_title(line, line)
    if not any(ch.isalnum() for ch in title) or title.casefold() == " ".join(current_title.split()).casefold():
        return None  # nothing readable, or the topic already has exactly this name
    return title

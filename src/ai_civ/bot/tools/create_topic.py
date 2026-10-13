"""Organic topics: the model may call ``create_topic(code, title, focus)`` when a distinct, lasting activity has grown out of the conversation
in the MAIN chat ("я начал учить датский"). There is no fixed list of roles and no topics are made at registration (except Settings).

Same division of labour as the other functions: the model proposes words, CODE validates them (``parse_create``: a latin slug, a title that gets an
emoji like every topic of the bot, a one-line focus), the topic is made AFTER the reply was delivered (``Orchestrator._apply_create_topic``: persona
row -> ``createForumTopic`` -> binding) and what the user reads about it is a fixed text with a link button, never the model's claim. The function is
OFFERED only in the main chat and only while the limits allow (a cooldown since the last topic the bot made, a cap on the number of its topics), so
inside the cooldown the model does not even know it exists. A topic that already exists is never made twice (``ensure_persona`` reuses the code).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any

from ai_civ.bot import personas
from ai_civ.bot.personas import PersonaSpec
from ai_civ.bot.tools.base import Delivery, Offered, ToolContext, ToolResult
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolSpec

if TYPE_CHECKING:
    from ai_civ.bot.orchestrator import TenantInfo

TOOL_NAME = "create_topic"
DEFAULT_FOCUS = "help the user with this activity"

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Open a NEW topic (a separate thread of the forum) for a distinct activity or goal that the user has clearly taken up and will keep coming "
        "back to (learning a language, a training plan, a project). Call it only in the main chat and only when the user's own message shows it - "
        "not for a question, a passing mention or a one-off request. Topics are rare: when in doubt, do NOT call it."
    ),
    parameters={
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "A short latin snake_case id of the activity, e.g. danish, gym, thesis (2-40 characters)."},
            "title": {
                "type": "string",
                "description": f"The name of the topic in the user's language, a short noun phrase, at most {personas.MAX_TITLE_CHARS} characters (an emoji at the start is welcome).",
            },
            "focus": {"type": "string", "description": f"What the topic is for, one sentence, at most {personas.MAX_FOCUS_CHARS} characters."},
        },
        "required": ["code", "title", "focus"],
    },
)

INSTRUCTIONS = (
    "## Topics\n"
    "You are getting to know the user through conversation, not a questionnaire: ask at most one light question at a time, and only when it fits.\n"
    "You never open a topic on your own. You may call create_topic only when the user has just agreed to a topic you offered in your previous message\n"
    '("Создать под эту задачу отдельный топик?" - asked only for an activity or project they keep coming back to, more than 3 times), or has asked for\n'
    "a topic themselves. Never for a question, a passing mention or something already covered by an existing topic. When it applies, the SYSTEM creates\n"
    "the topic right after your reply and tells the user itself: do not tell the user you opened it and never say it was done."
)

# What the previous assistant message of the main chat must look like for the user's "yes" to count: it asked about a separate topic.
OFFER_RE = re.compile(
    r"отдельн\w*\s+(?:топик|тем[ауыеой]\w*|тред)|создать\s+под\s+эту\s+задачу|(?:separate|dedicated|own)\s+(?:topic|thread)", re.IGNORECASE
)
AGREE_RE = re.compile(
    r"\b(?:да|давай|ага|угу|конечно|ок|окей|хорошо|ладно|создавай|создай|заводи|заведи|согласен|согласна|пожалуйста|yes|yep|yeah|sure|ok|okay|please|go ahead|do it)\b",
    re.IGNORECASE,
)
REFUSE_RE = re.compile(r"\b(?:нет|не надо|не нужно|не стоит|не хочу|не создавай|потом|no|nope|don'?t|not now)\b", re.IGNORECASE)
REQUEST_RE = re.compile(
    r"\b(?:созда\w*|завед\w*|завёл\w*|откро\w*|сдела\w*|нов\w*)\s+(?:\w+\s+){0,3}(?:топик\w*|тем[уаые]\b|тред\w*)"
    r"|\b(?:create|open|make|start)\s+(?:a\s+|the\s+)?(?:new\s+)?(?:topic|thread)\b",
    re.IGNORECASE,
)


def offer_was_made(last_assistant_text: str | None) -> bool:
    """Did the bot's previous message in the main chat ask the user about a separate topic?"""
    return bool(last_assistant_text) and "?" in last_assistant_text and OFFER_RE.search(last_assistant_text) is not None


def user_agrees(text: str) -> bool:
    """A plain "yes" to the offer (a refusal anywhere in the message wins: "да, но не сейчас" is not a yes)."""
    return AGREE_RE.search(text) is not None and REFUSE_RE.search(text) is None


def user_asks_for_topic(text: str) -> bool:
    return REQUEST_RE.search(text) is not None and REFUSE_RE.search(text) is None


def may_create(text: str, last_assistant_text: str | None) -> bool:
    """The gate of ``create_topic`` (code, not prompt): the user asked for a topic themselves, or agreed to the topic the bot offered in its last message."""
    return user_asks_for_topic(text) or (offer_was_made(last_assistant_text) and user_agrees(text))


def render_note(title: str) -> str:
    shown = " ".join(title.split()).replace('"', "'")
    return (
        "## Topic creation (made by the system)\n"
        f'The system will open the topic "{shown}" right after your reply and tells the user itself. Do not mention it and do not say it was done;\n'
        "just answer the user's message as usual."
    )


def parse_create(calls: Sequence[ToolCall]) -> PersonaSpec | None:
    """The spec of the first valid call, or None (no call, or an unusable one: dropped silently - the user did not ask for a topic). One per message."""
    for call in calls:
        if call.name == TOOL_NAME:
            return _valid(call.args)
    return None


def _valid(args: dict[str, Any]) -> PersonaSpec | None:
    code, title, focus = args.get("code"), args.get("title"), args.get("focus")
    if not isinstance(code, str) or not isinstance(title, str):
        return None
    code = code.strip().lower()
    if not personas.CODE_RE.match(code) or code in personas.RESERVED_CODES or code in personas.SYSTEM_CODES:
        return None
    line = " ".join(title.split())
    if not line or len(line) > personas.MAX_TITLE_CHARS or personas.has_hidden_chars(line) or not any(ch.isalnum() for ch in line):
        return None  # too long is refused, not cut: a cut name is a name nobody chose
    focus_line = personas.clean_line(focus, personas.MAX_FOCUS_CHARS) if isinstance(focus, str) and not personas.has_hidden_chars(focus) else ""
    return PersonaSpec(code, personas.normalize_title(line, code), focus_line or DEFAULT_FOCUS)


class CreateTopicTool:
    name = "create_topic"
    functions = frozenset({TOOL_NAME})
    offer_rank = 35

    def __init__(
        self,
        settings: Settings,
        *,
        allowed: Callable[[TenantInfo], Awaitable[bool]],
        apply: Callable[[TenantInfo, PersonaSpec, int | None], Awaitable[None]],
        last_reply: Callable[[TenantInfo], Awaitable[str | None]] | None = None,
    ) -> None:
        self.settings = settings
        self._allowed = allowed  # the cooldown and the cap, read from the database
        self._apply = apply
        self._last_reply = last_reply  # the text of the bot's latest message in the main chat (did it offer a topic?)

    async def offer(self, ctx: ToolContext) -> Offered | None:
        if not self.settings.create_topic_enabled or ctx.persona is not None:  # only the main chat opens topics
            return None
        if not await self._allowed(ctx.tenant):
            return None
        if self.settings.create_topic_require_offer:  # the bot asks FIRST: no offer in its previous message and no request of the user = no function at all
            if ctx.turn is None or self._last_reply is None:
                return None
            if not may_create(ctx.turn.text, await self._last_reply(ctx.tenant)):
                return None

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            spec = parse_create(calls)
            if spec is None:
                return None

            async def after(delivery: Delivery) -> None:
                await self._apply(delivery.tenant, spec, delivery.thread_id)

            return ToolResult(notes=(render_note(spec.title),), phrase=True, after_reply=(after,))

        return Offered((TOOL,), INSTRUCTIONS, run, run_rank=35)

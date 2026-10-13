"""Debate: the user asks to look at an idea from several sides ("устрой дебаты", "разбери идею с разных сторон", "критикуй идею") and gets 2-3
opposed voices, each in its own visual block, and a short question that hands the decision back to them.

ONE model turn, no chain of calls. The model fills ONE structured call, ``debate(topic, roles, question)``: the words of every voice are the
arguments of that call. CODE then does everything else: ``parse_debate`` validates the roles (a closed set, 2-3 different ones, a non-empty one-line
text each, never cut) and ``format_debate`` writes the message (headers, order, emoji, the closing question). There is no second model turn to phrase
the reply, so the debate costs the tokens and the latency of one answer.

  * 🔍 Критик (skeptic): vulnerabilities, hidden costs, implementation risks, logical gaps;
  * 💡 Визионер (visionary): growth potential, strategic value, scalability;
  * ⚖️ Прагматик (pragmatist): the real first steps, price against value, the effort.

The tool is OFFERED only when the user's own words ask for it (``wants_debate``, a regex in code), and it is ``exclusive``: while a debate round is
on offer NO other tool is offered in that reply (``ToolRegistry.offer``), so a round of arguments cannot save a fact, send an invitation, change the
calendar, rename or open a topic, even if the model, the idea under discussion or a quoted text asks for it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ai_civ.bot import personas, texts
from ai_civ.bot.tools.base import Offered, ToolContext, ToolResult
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolSpec

TOOL_NAME = "debate"
MAX_TOPIC_CHARS = 200
MAX_ROLE_CHARS = 700
MAX_QUESTION_CHARS = 300
MIN_ROLES, MAX_ROLES = 2, 3

# the closed set of voices, in the order the user reads them
ROLES: tuple[str, ...] = ("skeptic", "visionary", "pragmatist")

# ---------------------------------------------------------------- the marker check (code, not prompt)

_MARKERS = re.compile(
    r"дебат\w*"
    r"|разбер\w*\W+(?:\w+\W+){0,4}?с\W+(?:разных|двух|трёх|трех|всех)\W+сторон\w*"
    r"|(?:с|со)\W+разных\W+сторон"
    r"|критику\w*\W+(?:\w+\W+){0,2}?(?:иде\w*|план\w*|решени\w*|проект\w*)"
    r"|debate\w*"
    r"|devil'?s\W+advocate"
    r"|argue\W+both\W+sides",
    re.IGNORECASE,
)


def wants_debate(message: str) -> bool:
    """True when the USER's own words ask for a debate / a many-sided review / a critique of an idea. ``ё`` is folded into ``е``."""
    return _MARKERS.search(message.replace("ё", "е").replace("Ё", "Е")) is not None


# ---------------------------------------------------------------- the function the model sees

TOOL = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Run a short debate about the user's idea, plan or decision: 2-3 opposed voices in ONE call. Call it only because the user's message "
        "explicitly asks for a debate, a critique or a look from different sides. Skeptic: vulnerabilities, hidden costs, implementation risks, "
        "logical gaps. Visionary: growth potential, strategic value, scalability. Pragmatist: the real first steps, price against value, the "
        "effort. Each voice speaks to the point about THIS idea, in 2-4 concrete sentences, no diagnoses of the user. The SYSTEM lays the voices out "
        "in blocks for the user: you do not write anything else."
    ),
    parameters={
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": f"The idea being debated, a short phrase in the user's language, at most {MAX_TOPIC_CHARS} characters."},
            "roles": {
                "type": "array",
                "description": f"{MIN_ROLES}-{MAX_ROLES} DIFFERENT voices.",
                "minItems": MIN_ROLES,
                "maxItems": MAX_ROLES,
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "enum": list(ROLES), "description": "Which voice speaks."},
                        "text": {"type": "string", "description": f"What this voice says, at most {MAX_ROLE_CHARS} characters, in the user's language."},
                    },
                    "required": ["role", "text"],
                },
            },
            "question": {
                "type": "string",
                "description": f"One short closing question to the user that helps them decide, at most {MAX_QUESTION_CHARS} characters.",
            },
        },
        "required": ["topic", "roles", "question"],
    },
)

INSTRUCTIONS = (
    "## Debate\n"
    "The user asked to look at an idea from different sides. Call debate ONCE with 2-3 different voices (skeptic, visionary, pragmatist) and a\n"
    "short closing question; put the whole answer into that one call. The SYSTEM then shows the voices in separate blocks, so do not write the\n"
    "debate in text, do not add a summary, and do not say anything was saved or done. No other function is available in this reply."
)


# ---------------------------------------------------------------- validating a call (the model proposes, the code decides)


@dataclass(frozen=True)
class Voice:
    role: str
    text: str


@dataclass(frozen=True)
class DebateSpec:
    topic: str
    voices: tuple[Voice, ...]  # canonical order, 2-3 different roles
    question: str


def _line(value: Any, limit: int) -> str | None:
    """One clean line, or None: not a string, with control / bidi / zero-width characters, empty, no letters or digits, or longer than ``limit``
    (a voice that is too long is dropped, never cut: a cut argument says something nobody wrote)."""
    if not isinstance(value, str):
        return None
    line = " ".join(value.split())  # line breaks and runs of spaces collapse first: a model often writes a voice in two paragraphs
    if personas.has_hidden_chars(line):  # what is still hidden after that (control, bidi, zero-width) is refused
        return None
    return line if line and len(line) <= limit and any(ch.isalnum() for ch in line) else None


def parse_debate(calls: Sequence[ToolCall]) -> DebateSpec | None:
    """The spec of the first call of ``debate``, or None (no call, or fewer than two usable, different voices). An unusable topic or question does
    not spoil a good debate: the code has its own fixed words for those two."""
    for call in calls:
        if call.name == TOOL_NAME:
            return _valid(call.args)
    return None


def _valid(args: dict[str, Any]) -> DebateSpec | None:
    raw_roles = args.get("roles")
    if not isinstance(raw_roles, list):
        return None
    chosen: dict[str, str] = {}
    for item in raw_roles:
        if not isinstance(item, dict) or not isinstance(item.get("role"), str):
            continue
        role = item["role"].strip().lower()
        text = _line(item.get("text"), MAX_ROLE_CHARS)
        if role in ROLES and text is not None and role not in chosen:  # the first voice of a role stands, a repeat is dropped
            chosen[role] = text
    if len(chosen) < MIN_ROLES:
        return None
    topic = _line(args.get("topic"), MAX_TOPIC_CHARS) or texts.DEBATE_TOPIC_DEFAULT
    question = _line(args.get("question"), MAX_QUESTION_CHARS) or texts.DEBATE_QUESTION_DEFAULT
    return DebateSpec(topic, tuple(Voice(role, chosen[role]) for role in ROLES if role in chosen), question)


def format_debate(spec: DebateSpec) -> str:
    """The message the user reads: written by code, one block per voice, the question last."""
    blocks = [texts.DEBATE_HEADER.format(topic=spec.topic)]
    blocks += [f"{texts.DEBATE_ROLES[voice.role]}\n{voice.text}" for voice in spec.voices]
    blocks.append(texts.DEBATE_QUESTION.format(question=spec.question))
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- the tool


class DebateTool:
    """Offered only on an explicit request, and exclusive: while it is on offer, nothing else is. It needs no second model turn (``phrase=False``)."""

    name = "debate"
    functions = frozenset({TOOL_NAME})
    offer_rank = 5
    exclusive = True

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        turn = ctx.turn
        if not self.settings.debate_enabled or turn is None or not wants_debate(turn.text):
            return None

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            spec = parse_debate(calls)
            if spec is None:
                return None  # the engine does not trust words written next to an unusable call: the reply is phrased again, without functions
            return ToolResult(text=format_debate(spec), phrase=False)

        return Offered((TOOL,), INSTRUCTIONS, run, run_rank=5)

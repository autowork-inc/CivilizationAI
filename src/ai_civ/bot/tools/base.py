"""The tool registry: what the model may DO in a reply, behind one small interface.

Before this module ``Orchestrator._answer`` knew every function by name (``ask_contact``, ``propose_event``, ``create_event``,
``rename_current_topic``) and had one ``if`` branch per function. Every new function would have meant another branch in the one
method that decides what a user reads about an action. Now a tool is an object with two moves:

  * ``offer(ctx)``: is the model offered this tool in THIS reply, and if so with which function declarations and which section of the
    system prompt? ``None`` = not offered (the feature is off, the user has no contacts, the topic may not be renamed, ...). Whatever
    the tool must remember between offering and running (the contacts list the model saw as ``c1..cN``) lives in the closure of the
    ``Offered.run`` it returns, so one turn never leaks state into the next.
  * ``Offered.run(calls)``: validate and execute the model's calls (the model proposes, the code decides) and return a ``ToolResult``;
    ``None`` = nothing ran (a malformed call, or a call of a function that was not on offer).

The division of labour of the safety design is unchanged and lives in the engine (``Orchestrator._answer``): words the model wrote next
to a call are never sent; when something ran, the reply is phrased AGAIN with the tools' ``notes`` written into the prompt, and what the
user reads about an action itself is a fixed text produced by CODE after the commit (``ToolResult.text`` / ``cards``).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ai_civ.memory.llm import ToolCall, ToolSpec

if TYPE_CHECKING:  # the orchestrator imports this package, so the reverse import is for type checkers only
    from ai_civ.bot.orchestrator import TenantInfo
    from ai_civ.db.models import Persona


@dataclass(frozen=True)
class UserTurn:
    """The message being answered: its row in ``messages`` and the words the USER wrote (a voice note arrives here as its transcript).
    ``text`` is the only thing a tool may treat as "the user said so": whatever else reaches the model (a contact's data, later the
    text of an attached file) is not the user's own word."""

    message_id: uuid.UUID
    text: str
    image: bool = False  # the message carries a photo the model sees: a fact about the message, not a word of the user


@dataclass(frozen=True)
class ToolContext:
    tenant: TenantInfo
    persona: Persona | None  # the topic the user wrote in; None = the main chat
    turn: UserTurn | None  # None where there is no user message to answer

    @property
    def persona_id(self) -> uuid.UUID | None:
        return None if self.persona is None else self.persona.id


@dataclass(frozen=True)
class CardButton:
    """One button of a card, independent of any transport. ``action`` is ``callback`` (the client sends ``id`` back when it is pressed) or ``url``
    (the client opens ``payload["url"]``). The Telegram adapter (``Orchestrator``) renders it as an inline button, the mobile API as JSON."""

    id: str
    label: str
    action: str
    payload: dict[str, Any] | None = None


Markup = tuple[tuple[CardButton, ...], ...]  # rows of buttons


def neutral_markup(markup: Any) -> Markup | None:
    """Anything with the shape of a Telegram inline keyboard (``inline_keyboard`` rows of buttons with ``text`` and ``callback_data`` / ``url``) -> the
    neutral rows. A duck-typed read: this module must not import aiogram (the mobile service layer uses it), and the tools that build their buttons
    with aiogram types keep working unchanged. An already neutral value passes through; a button of another kind (a web app, a login url) is refused."""
    if markup is None:
        return None
    rows = getattr(markup, "inline_keyboard", markup)
    out: list[tuple[CardButton, ...]] = []
    for row in rows:
        buttons: list[CardButton] = []
        for button in row:
            if isinstance(button, CardButton):
                buttons.append(button)
            elif getattr(button, "callback_data", None) is not None:
                buttons.append(CardButton(button.callback_data, button.text, "callback", {"callback_data": button.callback_data}))
            elif getattr(button, "url", None) is not None:
                buttons.append(CardButton(button.url, button.text, "url", {"url": button.url}))
            else:
                raise TypeError(f"a card button needs callback_data or url: {button!r}")
        if buttons:
            out.append(tuple(buttons))
    return tuple(out) or None


@dataclass(frozen=True)
class Card:
    """A message CODE sends under the reply (a question with buttons, a confirmation): never the model's words, never stored as one.

    ``markup`` holds neutral rows of ``CardButton``; a Telegram ``InlineKeyboardMarkup`` handed in by a tool is converted on construction."""

    text: str
    markup: Markup | None = None
    auto_delete_seconds: float | None = None  # the message is deleted by itself this long after it was sent (None / 0 = it stays)

    def __post_init__(self) -> None:
        object.__setattr__(self, "markup", neutral_markup(self.markup))

    def to_json(self) -> dict[str, Any]:
        """The card as a plain JSON-able structure for the mobile client."""
        return card_to_json(self)


def card_to_json(card: Card) -> dict[str, Any]:
    return {
        "text": card.text,
        "buttons": [
            [{"id": b.id, "label": b.label, "action": b.action, "payload": b.payload} for b in row] for row in (card.markup or ())
        ],
        "auto_delete_seconds": card.auto_delete_seconds,
    }


@dataclass(frozen=True)
class Delivery:
    """Where the reply went (after it was sent): what an ``AfterReply`` needs to act in the same place."""

    tenant: TenantInfo
    thread_id: int | None
    persona: Persona | None


AfterReply = Callable[[Delivery], Awaitable[None]]  # runs after the reply was delivered; must not raise (a courtesy never breaks the update)


@dataclass(frozen=True)
class ToolResult:
    """What one tool did in this reply.

    ``notes``        sections written into the prompt of the phrasing turn (what happened, what the model must not claim);
    ``phrase``       the model must phrase the reply again (with every tool's notes); False = this tool needs no model words;
    ``text``         a fixed text appended AFTER the model's reply, written by code from what the database said;
    ``third_party``  the reply was built from another person's data (stored flagged: the GC never remembers it);
    ``output_cap``   a hard cap on the phrasing turn (tokens); the smallest cap of all results wins;
    ``cards``        messages with buttons sent under the reply;
    ``after_reply``  actions run once the reply is delivered."""

    notes: tuple[str, ...] = ()
    phrase: bool = False
    text: str | None = None
    third_party: bool = False
    output_cap: int | None = None
    cards: tuple[Card, ...] = ()
    after_reply: tuple[AfterReply, ...] = ()


@dataclass(frozen=True)
class Offered:
    """A tool as offered in ONE reply. ``run_rank`` orders execution and notes (the order the user's side effects and prompt sections had
    before the registry existed: the Diplomat first, then calendar drafts, then the rename)."""

    specs: tuple[ToolSpec, ...]
    section: str  # appended to the system prompt of the function turn
    run: Callable[[Sequence[ToolCall]], Awaitable[ToolResult | None]]
    run_rank: int


class BotTool(Protocol):
    name: str  # for logs
    functions: frozenset[str]  # EVERY function name this tool owns, offered or not (a call of an unoffered one is untrusted noise)
    offer_rank: int  # the order of the function declarations and prompt sections the model sees

    async def offer(self, ctx: ToolContext) -> Offered | None: ...


class ToolRegistry:
    def __init__(self, tools: Sequence[BotTool]) -> None:
        names = [function for tool in tools for function in tool.functions]
        if len(names) != len(set(names)):
            raise ValueError(f"two tools declare the same function: {sorted(n for n in set(names) if names.count(n) > 1)}")
        self._tools = tuple(sorted(tools, key=lambda tool: tool.offer_rank))
        self.function_names = frozenset(names)

    @property
    def tools(self) -> tuple[BotTool, ...]:
        return self._tools

    async def offer(self, ctx: ToolContext, *, untrusted: bool = False) -> list[Offered]:
        """The tools on offer for this reply, in ``offer_rank`` order. A tool's own ``offer`` never raises for an optional enrichment
        (it logs and answers ``None``); a bug that does raise fails the reply, like any other bug of the turn.

        ``untrusted`` = the reply is built from an attachment's content (data, never instructions): only tools that declare
        ``untrusted_ok = True`` are asked. A tool with ``exclusive = True`` that is on offer pushes every other tool out of the reply. A tool may do that only if its effect is harmless whatever the content says AND the offer itself
        depends on words of the user (``ctx.turn``), not on the attachment."""
        offered: list[tuple[BotTool, Offered]] = []
        for tool in self._tools:
            if untrusted and not getattr(tool, "untrusted_ok", False):
                continue
            item = await tool.offer(ctx)
            if item is not None:
                offered.append((tool, item))
        exclusive = [item for tool, item in offered if getattr(tool, "exclusive", False)]
        if exclusive:  # a tool that declares ``exclusive`` (the debate round) is the ONLY one on offer: nothing else can act in that reply
            return exclusive
        return [item for _, item in offered]

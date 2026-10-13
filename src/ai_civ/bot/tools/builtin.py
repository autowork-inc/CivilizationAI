"""The three tools that existed before the registry, as adapters: ``ask_contact`` + ``propose_event`` (the Diplomat), ``create_event`` (quick
events) and ``rename_current_topic``. The engines (``Diplomat``, ``QuickEvents``, ``topic_rename``) are untouched: an adapter only decides
whether the tool is offered, runs the engine on the model's calls and turns what happened into a ``ToolResult``. What the user reads is
exactly what ``Orchestrator._answer`` used to compose by hand (the tests of those features run unchanged through the registry).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from ai_civ.bot import diplomat, quick_events, topic_rename
from ai_civ.bot.diplomat import Diplomat
from ai_civ.bot.quick_events import QuickEvents
from ai_civ.bot.tools.base import Card, Delivery, Offered, ToolContext, ToolResult
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall

if TYPE_CHECKING:
    from ai_civ.bot.orchestrator import TenantInfo
    from ai_civ.db.models import Persona

log = logging.getLogger(__name__)


class DiplomatTool:
    """``ask_contact`` (read what a contact allowed) and ``propose_event`` (invite a contact). Offered only to a user who has accepted contacts."""

    name = "diplomat"
    functions = frozenset(tool.name for tool in diplomat.TOOLS)
    offer_rank = 20

    def __init__(self, engine: Diplomat, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        if not self.settings.diplomat_enabled:
            return None
        tenant = ctx.tenant
        try:
            roster = await self.engine.roster(tenant.id)
        except Exception:  # noqa: BLE001 - degrade to a reply without the Diplomat (CancelledError is not an Exception)
            log.warning("could not read the contacts of tenant %s; answering without the Diplomat", tenant.id, exc_info=True)
            return None
        if not roster:
            return None

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            asks = await self.engine.run(tenant.id, tenant.timezone, roster, calls)
            invitations = await self.engine.propose(tenant.id, tenant.timezone, roster, calls)
            if not asks and not invitations:
                return None
            invited = diplomat.render_proposals(invitations, tenant.timezone) if invitations else None
            refused = diplomat.refusal_text(asks) if asks else None
            answered = bool(asks) and refused is None
            notes: list[str] = []
            if answered:
                notes.append(diplomat.render_results(asks, invitations=invited is not None))
            elif invited is not None:
                notes.append(diplomat.INVITATIONS_NOTE)
            if refused is not None:
                notes.append(diplomat.REFUSAL_NOTE)
            # The turn that talks about ANOTHER person's data has a hard output cap: whatever a friend (or the contact data) asks for, the
            # model physically cannot write an essay, a story or code here. Replies that are only about the user's own plan stay uncapped.
            cap = self.settings.diplomat_max_output_tokens if answered else 0
            fixed = "\n\n".join(part for part in (refused, invited) if part is not None)
            return ToolResult(
                notes=tuple(notes),
                phrase=answered,
                text=fixed or None,
                third_party=answered,
                output_cap=cap or None,
            )

        return Offered(tuple(diplomat.TOOLS), diplomat.render_roster(roster), run, run_rank=10)


class QuickEventTool:
    """``create_event``: the user's own plan becomes a draft and a card with buttons that code sends under the reply. Offered to EVERY user."""

    name = "quick_events"
    functions = frozenset({quick_events.TOOL_NAME})
    offer_rank = 10

    def __init__(self, engine: QuickEvents, settings: Settings) -> None:
        self.engine = engine
        self.settings = settings

    async def offer(self, ctx: ToolContext) -> Offered | None:
        if not self.settings.quick_events_enabled:
            return None
        tenant = ctx.tenant

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            drafts = await self.engine.draft(tenant.id, tenant.timezone, ctx.persona_id, calls)
            if not drafts:
                return None
            cards = tuple(Card(quick_events.card_text(d, tenant.timezone), quick_events.card_markup(d.id)) for d in drafts)
            return ToolResult(notes=(quick_events.render_note(drafts, tenant.timezone),), phrase=True, cards=cards)

        return Offered((quick_events.TOOL,), quick_events.INSTRUCTIONS, run, run_rank=20)


class RenameTopicTool:
    """``rename_current_topic``: offered only inside a topic of the bot's own whose cooldown has passed. The validated name is applied by
    ``apply`` AFTER the reply was delivered (claim -> Telegram -> a fixed announcement), so a failed rename can never spoil the reply."""

    name = "topic_rename"
    functions = frozenset({topic_rename.TOOL_NAME})
    offer_rank = 30

    def __init__(
        self,
        settings: Settings,
        *,
        allowed: Callable[[Persona | None], bool],
        apply: Callable[[TenantInfo, Persona, str, int | None], Awaitable[None]],
    ) -> None:
        self.settings = settings
        self._allowed = allowed  # the pre-check on the loaded row (``personas.rename_allowed``; ``claim_rename`` is the authority)
        self._apply = apply

    async def offer(self, ctx: ToolContext) -> Offered | None:
        persona = ctx.persona
        if persona is None or not self._allowed(persona):
            return None

        async def run(calls: Sequence[ToolCall]) -> ToolResult | None:
            new_title = topic_rename.parse_rename(calls, persona.title)
            if new_title is None:
                return None

            async def after(delivery: Delivery) -> None:
                await self._apply(delivery.tenant, persona, new_title, delivery.thread_id)

            return ToolResult(notes=(topic_rename.render_note(new_title),), phrase=True, after_reply=(after,))

        return Offered((topic_rename.TOOL,), topic_rename.instructions(persona.title), run, run_rank=30)

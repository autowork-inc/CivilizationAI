"""v2.0, the tool registry (``bot/tools``): what the model may do in a reply is a list of tools, each deciding for itself whether it is offered.
The behaviour of the tools that predate it is proven by their own suites (diplomat, quick events, topic rename), which run through the registry
unchanged; here are the rules of the registry itself and the wiring of the orchestrator."""

from __future__ import annotations

import pytest

from ai_civ.bot.orchestrator import Orchestrator
from ai_civ.bot.tools import Card, Offered, ToolContext, ToolRegistry, ToolResult, UserTurn
from ai_civ.config import Settings
from ai_civ.memory.llm import ToolCall, ToolSpec
from tests.tg_fakes import FakeChatLLM, make_bot


class Stub:
    """A tool that is offered (or not) and records how often it was asked."""

    def __init__(self, name: str, functions: set[str], rank: int, *, offered: bool = True) -> None:
        self.name, self.functions, self.offer_rank = name, frozenset(functions), rank
        self.offered = offered
        self.asked = 0

    async def offer(self, ctx: ToolContext) -> Offered | None:
        self.asked += 1
        if not self.offered:
            return None

        async def run(calls):  # noqa: ANN001, ANN202
            return ToolResult(text=self.name)

        specs = tuple(ToolSpec(function, "d", {"type": "object", "properties": {}}) for function in sorted(self.functions))
        return Offered(specs, f"## {self.name}", run, run_rank=self.offer_rank)


async def test_the_tools_are_asked_in_offer_order_whatever_the_order_they_were_listed_in():
    late, early, middle = Stub("late", {"f3"}, 30), Stub("early", {"f1"}, 10), Stub("middle", {"f2"}, 20)
    registry = ToolRegistry([late, early, middle])
    assert [tool.name for tool in registry.tools] == ["early", "middle", "late"]
    offered = await registry.offer(ToolContext(tenant=None, persona=None, turn=None))  # type: ignore[arg-type]
    assert [o.section for o in offered] == ["## early", "## middle", "## late"]


async def test_a_tool_that_declines_is_simply_not_there_but_its_functions_still_count_as_ours():
    on, off = Stub("on", {"f1"}, 10), Stub("off", {"f2", "f3"}, 20, offered=False)
    registry = ToolRegistry([on, off])
    offered = await registry.offer(ToolContext(tenant=None, persona=None, turn=None))  # type: ignore[arg-type]
    assert [o.section for o in offered] == ["## on"] and off.asked == 1
    # a call of a function that was not offered is recognisable as ours (its words are not trusted), see Orchestrator._answer
    assert registry.function_names == {"f1", "f2", "f3"}


def test_two_tools_cannot_claim_the_same_function():
    with pytest.raises(ValueError, match="same function"):
        ToolRegistry([Stub("a", {"x", "y"}, 1), Stub("b", {"y"}, 2)])


def test_the_context_knows_the_topic_id_and_the_card_is_just_text_and_buttons():
    assert ToolContext(tenant=None, persona=None, turn=None).persona_id is None  # type: ignore[arg-type]
    assert Card("text").markup is None and UserTurn.__dataclass_fields__.keys() == {"message_id", "text", "image"}


def test_the_orchestrator_wires_every_tool_in_a_fixed_order():
    bot, _session = make_bot()
    orchestrator = Orchestrator(db=None, router_llm=object(), chat_llm=FakeChatLLM(), settings=Settings(_env_file=None), bot=bot)  # type: ignore[arg-type]
    assert [tool.name for tool in orchestrator.tools.tools] == [
        "debate", "quick_events", "diplomat", "decompose", "topic_rename", "create_topic", "delete_topic", "save_fact", "cards", "workout", "recipes",
        "inventory", "geo_note", "time_capsule", "schedule_reminder",
    ]  # fmt: skip
    assert orchestrator.tools.function_names == {
        "create_event", "ask_contact", "propose_event", "rename_current_topic", "create_topic", "delete_topic", "save_fact", "debate", "decompose_task",
        "create_cards", "log_workout", "suggest_recipe", "update_inventory", "save_geo_note", "seal_time_capsule", "schedule_reminder",
    }  # fmt: skip
    assert orchestrator.facts is not None and orchestrator.quick is not None and orchestrator.diplomat is not None


def test_a_call_of_an_unknown_name_is_not_one_of_ours():
    bot, _session = make_bot()
    orchestrator = Orchestrator(db=None, router_llm=object(), chat_llm=FakeChatLLM(), settings=Settings(_env_file=None), bot=bot)  # type: ignore[arg-type]
    assert ToolCall("something_else", {}).name not in orchestrator.tools.function_names

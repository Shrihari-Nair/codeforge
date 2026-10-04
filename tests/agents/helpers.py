"""Shared test helpers: scripted LLM turns and trivial tools."""

from typing import Any

from pydantic import BaseModel

from aeos.agents.tools import ToolContext, ToolDef, ToolOutput, ToolRuntime
from aeos.core.budget import BudgetTracker
from aeos.core.events import ListSink
from aeos.core.risk import PolicyEngine
from aeos.core.state import Budget
from aeos.llm.base import LLMResponse, ToolCall, Usage
from aeos.llm.fake import ScriptStep


def turn(*calls: tuple[str, dict[str, Any]], text: str = "", tokens: int = 10) -> ScriptStep:
    """One scripted model turn that makes the given tool calls."""
    tcs = [ToolCall(id=f"c{i}-{n}", name=n, arguments=a) for i, (n, a) in enumerate(calls)]
    return ScriptStep(
        response=LLMResponse(
            content=text,
            tool_calls=tcs,
            usage=Usage(input_tokens=tokens, output_tokens=tokens),
            model="fake",
        )
    )


def text_turn(text: str) -> ScriptStep:
    return ScriptStep(response=LLMResponse(content=text, model="fake"))


class Answer(BaseModel):
    answer: str
    confidence: float = 0.5


def echo_tool(calls: list[dict[str, Any]] | None = None, risk: str = "read",
              origin: str = "untrusted") -> ToolDef:  # fmt: skip
    async def run(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        if calls is not None:
            calls.append(args)
        if args.get("boom"):
            return ToolOutput("kaboom", is_error=True)
        return ToolOutput(f"echo:{args.get('text', '')}")

    return ToolDef(
        name="echo", description="Echo text", server="test", risk=risk,
        result_origin=origin,  # type: ignore[arg-type]
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        run=run,
    )  # fmt: skip


def make_runtime(
    tools: dict[str, ToolDef], budget: Budget | None = None
) -> tuple[ToolRuntime, BudgetTracker, ListSink]:
    sink, tracker = ListSink(), BudgetTracker(budget)
    ctx = ToolContext(task_id="t1", step_id="s1", agent="tester")
    return ToolRuntime(tools, ctx, PolicyEngine(), sink, tracker), tracker, sink

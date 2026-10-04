"""The agent loop: observe -> think -> act -> observe, with hard limits.

Stops on: `finish` with valid structured output, max steps, budget exhaustion, stagnation
(repeating the same call), or repeated malformed model output. Never raises for model/tool
misbehaviour; the status says what happened. `ApprovalRequired` propagates (graph interrupt).
"""

import json
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from aeos.agents.tools import ToolRuntime
from aeos.core.budget import BudgetTracker
from aeos.core.events import Event
from aeos.llm.base import LLMError, LLMProvider, Message, ToolSpec
from aeos.llm.structured import clean_schema
from aeos.telemetry import traced

LoopStatus = Literal["finished", "max_steps", "budget", "stagnated", "malformed", "error"]


class LoopLimits(BaseModel):
    max_steps: int = 25
    max_malformed: int = 3
    stagnation_repeat: int = 3  # warn on the Nth identical call, stop two repeats later
    max_tokens_per_call: int = 4096
    finish_retries: int = 2
    max_context_chars: int = 180_000


@dataclass
class LoopResult:
    status: LoopStatus
    output: BaseModel | None = None
    detail: str = ""
    steps: int = 0


def _compact(messages: list[Message], max_chars: int) -> None:
    """Drop the bulk of the oldest tool results when the context gets too large."""
    total = sum(len(m.content) for m in messages)
    for m in messages[2:]:
        if total <= max_chars:
            return
        if m.role == "tool" and len(m.content) > 300:
            total -= len(m.content) - 60
            m.content = m.content[:60] + " ...[older result elided to save context]"


async def _run_agent(
    *,
    llm: LLMProvider,
    agent: str,
    system: str,
    task: str,
    output_model: type[BaseModel],
    runtime: ToolRuntime,
    tracker: BudgetTracker,
    limits: LoopLimits | None = None,
) -> LoopResult:
    limits = limits or LoopLimits()
    sink, task_id = runtime.sink, runtime.ctx.task_id
    finish_spec = ToolSpec(
        name="finish",
        description="Call once, when finished, with your final structured answer.",
        parameters=clean_schema(output_model.model_json_schema()),
    )
    tools = [*runtime.specs(), finish_spec]
    messages = [Message(role="system", content=system), Message(role="user", content=task)]
    sink.emit(Event(type="agent_started", task_id=task_id, agent=agent, message=f"{agent} started"))

    malformed = finish_failures = 0
    seen: Counter[str] = Counter()
    result = LoopResult(status="max_steps", detail=f"no answer within {limits.max_steps} steps")
    steps = 0
    for steps in range(1, limits.max_steps + 1):
        if reason := tracker.exhausted():
            result = LoopResult("budget", detail=reason, steps=steps - 1)
            break
        tracker.add_step()
        _compact(messages, limits.max_context_chars)
        try:
            resp = await llm.complete(messages, tools=tools, max_tokens=limits.max_tokens_per_call)
        except LLMError as exc:
            result = LoopResult("error", detail=f"LLM error: {exc}", steps=steps)
            break
        tracker.add_llm(resp.usage, resp.model)
        sink.emit(
            Event(
                type="llm_call", task_id=task_id, agent=agent, message=f"step {steps}",
                data={"in": resp.usage.input_tokens, "out": resp.usage.output_tokens,
                      "tool_calls": [c.name for c in resp.tool_calls]},
            )
        )  # fmt: skip

        if not resp.tool_calls:
            malformed += 1
            if malformed >= limits.max_malformed:
                result = LoopResult(
                    "malformed",
                    detail=f"model produced no tool call {malformed} times "
                    f"(finish_reason={resp.finish_reason!r})",
                    steps=steps,
                )
                break
            messages.append(Message(role="assistant", content=resp.content or "(empty)"))
            messages.append(
                Message(
                    role="user",
                    content="That was not a tool call. Call one of the tools, or call `finish` "
                    "with your final answer.",
                )
            )
            continue

        messages.append(Message(role="assistant", content=resp.content, tool_calls=resp.tool_calls))
        finished: BaseModel | None = None
        stop: LoopResult | None = None
        for call in resp.tool_calls:
            if call.name == "finish":
                try:
                    finished = output_model.model_validate(call.arguments)
                    messages.append(Message(role="tool", tool_call_id=call.id, content="accepted"))
                except ValidationError as exc:
                    finish_failures += 1
                    messages.append(
                        Message(
                            role="tool",
                            tool_call_id=call.id,
                            content="INVALID finish arguments, fix and call finish again: "
                            + str(exc)[:700],
                        )  # fmt: skip
                    )
                    if finish_failures > limits.finish_retries:
                        stop = LoopResult("malformed", detail="invalid finish output", steps=steps)
                continue
            key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True, default=str)}"
            seen[key] += 1
            warning = ""
            if seen[key] >= limits.stagnation_repeat + 2:
                stop = LoopResult(
                    "stagnated", detail=f"repeated {call.name} {seen[key]}x", steps=steps
                )
            elif seen[key] >= limits.stagnation_repeat:
                warning = (
                    f"\n[system] You have made this exact call {seen[key]} times with the same "
                    "result. Change your approach or call `finish`."
                )
            output = await runtime.execute(call)  # may raise ApprovalRequired
            tool = runtime.tools.get(call.name)
            if tool is not None and tool.mutates:
                seen.clear()  # state changed: re-running checks is progress, not a loop
            messages.append(Message(role="tool", tool_call_id=call.id, content=output + warning))
        if finished is not None:
            result = LoopResult("finished", output=finished, steps=steps)
            break
        if stop is not None:
            result = stop
            break

    sink.emit(
        Event(
            type="agent_finished", task_id=task_id, agent=agent,
            message=f"{agent} {result.status}" + (f": {result.detail}" if result.detail else ""),
            data={"status": result.status, "steps": result.steps},
        )
    )  # fmt: skip
    return result


async def run_agent(
    *,
    llm: LLMProvider,
    agent: str,
    system: str,
    task: str,
    output_model: type[BaseModel],
    runtime: ToolRuntime,
    tracker: BudgetTracker,
    limits: LoopLimits | None = None,
) -> LoopResult:
    """The loop, inside an `agent <name>` span carrying its outcome."""
    with traced(
        f"agent {agent}",
        attributes={"aeos.agent": agent, "aeos.step.id": runtime.ctx.step_id,
                    "aeos.step.attempt": runtime.ctx.attempt},
    ) as span:  # fmt: skip
        result = await _run_agent(
            llm=llm, agent=agent, system=system, task=task, output_model=output_model,
            runtime=runtime, tracker=tracker, limits=limits,
        )  # fmt: skip
        span.set("aeos.agent.status", result.status)
        span.set("aeos.agent.steps", result.steps)
        if result.detail:
            span.set("aeos.agent.detail", result.detail)
        return result

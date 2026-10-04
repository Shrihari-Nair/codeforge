from typing import Any

import pytest

from aeos.agents.loop import LoopLimits, run_agent
from aeos.agents.tools import ToolContext, ToolDef, ToolOutput, idempotency_key
from aeos.core.risk import ApprovalRequired
from aeos.core.state import Budget
from aeos.llm.base import LLMResponse
from aeos.llm.fake import FakeLLM, ScriptStep
from tests.agents.helpers import Answer, echo_tool, make_runtime, text_turn, turn


async def run(
    script: list[ScriptStep],
    tools: dict[str, ToolDef] | None = None,
    budget: Budget | None = None,
    limits: LoopLimits | None = None,
):  # type: ignore[no-untyped-def]  # noqa: E501
    rt, tracker, sink = make_runtime(tools or {"echo": echo_tool()}, budget)
    llm = FakeLLM(script)
    res = await run_agent(llm=llm, agent="tester", system="sys", task="do it", output_model=Answer,
                          runtime=rt, tracker=tracker, limits=limits)  # fmt: skip
    return res, rt, tracker, sink, llm


async def test_tool_then_finish() -> None:
    res, rt, tracker, sink, llm = await run(
        [turn(("echo", {"text": "hi"})), turn(("finish", {"answer": "done", "confidence": 0.9}))]
    )
    assert res.status == "finished" and isinstance(res.output, Answer)
    assert res.output.answer == "done" and res.steps == 2
    assert [r.tool for r in rt.records] == ["echo"]
    assert tracker.budget.steps_used == 2 and tracker.budget.llm_calls == 2
    assert sink.types().count("agent_started") == 1 and "agent_finished" in sink.types()
    # the tool result reached the model, labelled as untrusted data
    last = llm.calls[1][-1]
    assert (
        last.role == "tool" and 'origin="untrusted"' in last.content and "echo:hi" in last.content
    )


async def test_malformed_no_tool_call_is_recovered() -> None:
    res, *_ = await run([text_turn("hmm"), turn(("finish", {"answer": "ok"}))])
    assert res.status == "finished"


async def test_repeated_malformed_output_stops() -> None:
    res, *_ = await run([text_turn("a"), text_turn("b"), text_turn("c")])
    assert res.status == "malformed" and "no tool call" in res.detail


async def test_invalid_finish_is_fed_back_then_accepted() -> None:
    res, _, _, _, llm = await run(
        [turn(("finish", {"confidence": 5})), turn(("finish", {"answer": "fixed"}))]
    )
    assert res.status == "finished"
    assert "INVALID finish" in llm.calls[1][-1].content


async def test_persistently_invalid_finish_stops() -> None:
    res, *_ = await run([turn(("finish", {"x": 1}))] * 3)
    assert res.status == "malformed"


async def test_max_steps_halts_a_runaway_loop() -> None:
    script = [turn(("echo", {"text": str(i)})) for i in range(50)]
    res, rt, *_ = await run(script, limits=LoopLimits(max_steps=5))
    assert res.status == "max_steps" and len(rt.records) == 5


async def test_token_budget_halts_the_loop() -> None:
    script = [turn(("echo", {"text": str(i)}), tokens=40) for i in range(50)]
    res, rt, tracker, *_ = await run(script, budget=Budget(max_tokens=200))
    assert res.status == "budget" and "token" in res.detail
    assert tracker.budget.tokens_used >= 200 and len(rt.records) <= 3


async def test_step_budget_is_shared_across_agents() -> None:
    script = [turn(("echo", {"text": str(i)})) for i in range(20)]
    res, *_ = await run(script, budget=Budget(max_steps=3))
    assert res.status == "budget" and "step" in res.detail


async def test_stagnation_warns_then_stops() -> None:
    same = turn(("echo", {"text": "again"}))
    res, _, _, _, llm = await run([same] * 10)
    assert res.status == "stagnated"
    assert any("exact call" in m.content for call in llm.calls for m in call if m.role == "tool")


async def test_tool_error_is_fed_back_not_raised() -> None:
    res, _, _, _, llm = await run(
        [turn(("echo", {"boom": True})), turn(("finish", {"answer": "recovered"}))]
    )
    assert res.status == "finished"
    assert "<tool_error" in llm.calls[1][-1].content and "kaboom" in llm.calls[1][-1].content


async def test_unknown_tool_lists_available_ones() -> None:
    res, _, _, _, llm = await run([turn(("nope", {})), turn(("finish", {"answer": "ok"}))])
    assert res.status == "finished" and "unknown tool" in llm.calls[1][-1].content


async def test_tool_crash_does_not_kill_the_loop() -> None:
    async def crash(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        raise RuntimeError("bug in tool")

    tool = ToolDef(name="crash", description="x", parameters={"type": "object"}, run=crash,
                   server="test")  # fmt: skip
    res, rt, *_ = await run(
        [turn(("crash", {})), turn(("finish", {"answer": "ok"}))], tools={"crash": tool}
    )
    assert res.status == "finished" and rt.records[0].is_error
    assert "tool_crashed" in rt.records[0].result_preview


async def test_irreversible_tool_requires_approval() -> None:
    tools = {"echo": echo_tool(risk="write_irreversible")}
    with pytest.raises(ApprovalRequired) as ei:
        await run([turn(("echo", {"text": "x"}))], tools=tools)
    assert ei.value.decision.decision == "require_approval" and ei.value.tool == "echo"


async def test_llm_failure_is_reported_not_raised() -> None:
    res, *_ = await run([])  # FakeLLM raises ScriptExhaustedError (an LLMError)
    assert res.status == "error" and "LLM error" in res.detail


async def test_long_results_are_truncated_for_the_model() -> None:
    async def big(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return ToolOutput("x" * 50_000)

    tool = ToolDef(name="big", description="x", parameters={"type": "object"}, run=big, server="t")
    res, rt, _, _, llm = await run(
        [turn(("big", {})), turn(("finish", {"answer": "ok"}))], tools={"big": tool}
    )
    assert "truncated" in llm.calls[1][-1].content and len(llm.calls[1][-1].content) < 7000
    assert len(rt.records[0].result_preview) < 700


async def test_parallel_tool_calls_in_one_turn() -> None:
    res, rt, *_ = await run(
        [turn(("echo", {"text": "a"}), ("echo", {"text": "b"})), turn(("finish", {"answer": "ok"}))]
    )
    assert res.status == "finished" and [r.arguments["text"] for r in rt.records] == ["a", "b"]


def test_idempotency_key_is_stable_and_distinguishes_calls() -> None:
    ctx = ToolContext("t", "s1", "coding", 1)
    k1 = idempotency_key(ctx, "create_pull_request", {"title": "a", "head": "h"})
    assert k1 == idempotency_key(ctx, "create_pull_request", {"head": "h", "title": "a"})
    assert k1 != idempotency_key(ctx, "create_pull_request", {"title": "b", "head": "h"})
    assert k1 != idempotency_key(
        ToolContext("t", "s1", "coding", 2), "create_pull_request", {"title": "a", "head": "h"}
    )  # new attempt -> new key


def test_fake_response_type_is_importable() -> None:
    assert LLMResponse().tool_calls == []


async def test_rerunning_the_same_check_after_a_change_is_not_stagnation() -> None:
    """Found by live runs: edit -> run tests -> edit -> run tests is normal work."""
    from aeos.agents.tools import ToolDef

    async def noop(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return ToolOutput("ok")

    tools = {
        "run_tests": ToolDef(name="run_tests", description="x", parameters={"type": "object"}, run=noop, server="t"),
        "edit": ToolDef(name="edit", description="x", parameters={"type": "object"}, run=noop, server="t", mutates=True),
    }  # fmt: skip
    script = []
    for i in range(6):  # 6 identical test runs, each preceded by a different edit
        script += [turn(("edit", {"n": i})), turn(("run_tests", {}))]
    script.append(turn(("finish", {"answer": "done"})))
    res, *_ = await run(script, tools=tools)
    assert res.status == "finished"


async def test_repeating_the_same_check_with_no_change_in_between_still_stagnates() -> None:
    from aeos.agents.tools import ToolDef

    async def noop(args: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        return ToolOutput("same result")

    tools = {"run_tests": ToolDef(name="run_tests", description="x", parameters={"type": "object"},
                                  run=noop, server="t")}  # fmt: skip
    res, *_ = await run([turn(("run_tests", {}))] * 10, tools=tools)
    assert res.status == "stagnated"

"""Phase 10 acceptance: one trace you can walk from the task down to every decision."""

import json
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

import pytest

from aeos.core.config import Settings, get_settings
from aeos.demo.trajectories import happy_path_llm
from aeos.graph.build import run_task
from aeos.graph.checkpoint import open_checkpointer
from aeos.hitl import auto_approver
from aeos.runtime import open_runtime

pytestmark = pytest.mark.docker

TRUTH = json.loads((Path(__file__).parents[2] / "demo_target" / "GROUND_TRUTH.json").read_text())


@pytest.fixture
def settings(seeded_db: None, remote_template: Path) -> Settings:
    work = Path(tempfile.mkdtemp()) / "checkout-service.git"
    shutil.copytree(remote_template, work)
    return get_settings().model_copy(update={"git_remote_path": str(work)})


async def test_a_completed_task_is_one_walkable_trace(settings, spans) -> None:  # type: ignore[no-untyped-def]
    async with (
        open_runtime(settings, llm=happy_path_llm(TRUTH)) as rt,
        open_checkpointer(settings) as saver,
    ):
        state = await run_task(rt, "Investigate the checkout latency regression",
                               checkpointer=saver, approver=auto_approver)  # fmt: skip
    assert state.status == "completed"

    finished = spans.get_finished_spans()
    by_id = {s.context.span_id: s for s in finished}
    children: dict[int, list] = defaultdict(list)  # type: ignore[type-arg]
    for s in finished:
        if s.parent is not None:
            children[s.parent.span_id].append(s)

    def ancestors(s):  # type: ignore[no-untyped-def]
        out, cur = [], s
        while cur.parent is not None and cur.parent.span_id in by_id:
            cur = by_id[cur.parent.span_id]
            out.append(cur.name)
        return out

    # ---- a single trace for the whole task (MCP session handshakes at startup are not task work)
    task_spans = [s for s in finished if s.attributes and "aeos.task.id" in s.attributes]
    assert len({s.context.trace_id for s in task_spans}) == 1
    assert len(task_spans) > 60
    root = next(s for s in finished if s.name == "task")
    assert root.parent is None and root.attributes["langfuse.trace.name"] == "aeos task"  # type: ignore[index]
    assert state.trace["traceparent"].split("-")[1] == f"{root.context.trace_id:032x}"

    # ---- every layer is present, with the documented names
    names = {s.name.split(" ")[0] for s in finished}
    assert {"task", "node", "agent", "llm.call", "tool.call", "mcp.call", "sandbox.exec",
            "verifier.gates", "approval.wait"} <= names  # fmt: skip
    nodes = {s.attributes["aeos.node"] for s in finished if s.name.startswith("node ")}  # type: ignore[index]
    assert {"dispatch", "synthesize", "verify", "publish_prepare", "await_decision", "publish_pr",
            "finish"} <= nodes  # fmt: skip

    # ---- walk: node -> agent -> (prompt/model) llm.call -> decision (tool.call) -> mcp.call
    analytics = next(s for s in finished if s.name == "agent analytics")
    assert "node dispatch" in ancestors(analytics) and "task" in ancestors(analytics)
    llm_calls = [s for s in children[analytics.context.span_id] if s.name.startswith("llm.call")]
    tool_calls = [s for s in children[analytics.context.span_id] if s.name.startswith("tool.call")]
    assert llm_calls and tool_calls
    first = llm_calls[0].attributes
    assert first["langfuse.observation.type"] == "generation"  # type: ignore[index]
    assert (
        "list_deployments" in first["langfuse.observation.output"]
    )  # the model's decision  # type: ignore[index,operator]
    tool = next(s for s in tool_calls if s.name == "tool.call list_deployments")
    assert tool.attributes["aeos.tool.risk"] == "read" and tool.attributes["aeos.policy.rule"]  # type: ignore[index]
    assert any(c.name == "mcp.call" for c in children[tool.context.span_id])  # tool -> MCP call

    # ---- gates, sandbox, approval
    gates = next(s for s in finished if s.name == "verifier.gates")
    assert gates.attributes["aeos.verification.status"] == "passed"  # type: ignore[index]
    assert any(s.name == "sandbox.exec" and s.attributes["exit_code"] == 0 for s in finished)  # type: ignore[index]
    wait = next(s for s in finished if s.name == "approval.wait")
    assert wait.attributes["aeos.approval.decision"] == "approved"  # type: ignore[index]
    assert wait.attributes["aeos.approval.actor"] == "auto-approver"  # type: ignore[index]

    # ---- cost / tokens roll up on the generation spans and match the budget
    gens = [s for s in finished if s.name.startswith("llm.call")]
    assert sum(s.attributes["gen_ai.usage.input_tokens"] for s in gens) > 0  # type: ignore[misc]
    assert all(s.attributes["aeos.task.id"] == state.task_id for s in task_spans)  # type: ignore[index]
    final = next(s for s in finished if s.name == "node finish")
    assert final.attributes["aeos.task.status"] == "completed"  # type: ignore[index]
    assert final.attributes["aeos.task.llm_calls"] == state.budget.llm_calls  # type: ignore[index]


async def test_the_trace_survives_the_task_being_resumed_by_another_runtime(
    settings, spans
) -> None:  # type: ignore[no-untyped-def]
    """Pause at approval, resume with a brand-new runtime: still the same trace id."""
    from aeos.graph.build import resume_task
    from aeos.hitl import apply_decision

    async with (
        open_runtime(settings, llm=happy_path_llm(TRUTH)) as rt,
        open_checkpointer(settings) as saver,
    ):
        paused = await run_task(
            rt, "Investigate the checkout latency regression", checkpointer=saver
        )
    assert paused.status == "awaiting_approval"

    def task_traces() -> set[int]:
        return {s.context.trace_id for s in spans.get_finished_spans()
                if s.attributes and "aeos.task.id" in s.attributes}  # fmt: skip

    trace_before = task_traces()
    assert len(trace_before) == 1

    _, payload = await apply_decision(paused.pending_approval_id or "", "approved", "alice")
    async with (
        open_runtime(settings, llm=happy_path_llm(TRUTH)) as rt2,
        open_checkpointer(settings) as saver2,
    ):
        final = await resume_task(rt2, paused.task_id, payload, checkpointer=saver2)
    assert final.status == "completed"
    assert task_traces() == trace_before  # the resumed half joined the SAME trace
    wait = next(s for s in spans.get_finished_spans() if s.name == "approval.wait")
    assert wait.attributes["aeos.approval.actor"] == "alice"  # type: ignore[index]

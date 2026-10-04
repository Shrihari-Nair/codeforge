"""LangGraph assembly: plan -> (dispatch | synthesize | verify)* -> finish, routed by our code."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Hashable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from aeos.core.state import Budget, TaskKind, TaskState
from aeos.failure.crash import maybe_crash
from aeos.graph.checkpoint import state_types
from aeos.graph.nodes import GraphNodes
from aeos.graph.publish import PublishNodes
from aeos.graph.routing import route
from aeos.hitl import DecisionError, apply_decision, expired_payload
from aeos.runtime import Runtime
from aeos.store import approvals
from aeos.store.approvals import ApprovalRequest
from aeos.telemetry import bind_task, remote_context, traced

TARGETS: dict[Hashable, str] = {
    "dispatch": "dispatch",
    "synthesize": "synthesize",
    "verify": "verify",
    "replan": "replan",
    "publish_prepare": "publish_prepare",
    "await_decision": "await_decision",
    "publish_pr": "publish_pr",
    "finish": "finish",
}


def _crashable(name: str, fn: Callable[[TaskState], Awaitable[dict[str, Any]]]) -> Any:
    async def wrapped(state: TaskState) -> dict[str, Any]:
        maybe_crash(f"node:{name}:before")
        with bind_task(state.task_id):
            if name == "plan":  # the plan node creates the task's root span itself
                out = await fn(state)
            else:  # every other node (any process, any time) joins the task's single trace
                with (
                    remote_context(state.trace),
                    traced(f"node {name}", attributes={"aeos.node": name}),
                ):
                    out = await fn(state)
        maybe_crash(f"node:{name}:after")  # work done, writes not yet checkpointed
        return out

    return wrapped


def build_graph(rt: Runtime, checkpointer: BaseCheckpointSaver[Any] | None = None) -> Any:
    nodes = GraphNodes(rt)
    publish = PublishNodes(rt)
    g = StateGraph(TaskState)
    g.add_node("plan", _crashable("plan", nodes.plan))
    g.add_node("dispatch", _crashable("dispatch", nodes.dispatch))
    g.add_node("synthesize", _crashable("synthesize", nodes.synthesize))
    g.add_node("verify", _crashable("verify", nodes.verify))
    g.add_node("replan", _crashable("replan", nodes.replan))
    g.add_node("publish_prepare", _crashable("publish_prepare", publish.prepare))
    g.add_node("await_decision", _crashable("await_decision", publish.await_decision))
    g.add_node("publish_pr", _crashable("publish_pr", publish.create_pr))
    g.add_node("finish", _crashable("finish", nodes.finish))
    g.add_node("reflect", _crashable("reflect", nodes.reflect))
    g.add_edge(START, "plan")
    for src in (
        "plan",
        "dispatch",
        "synthesize",
        "verify",
        "replan",
        "publish_prepare",
        "await_decision",
        "publish_pr",
    ):
        g.add_conditional_edges(src, route, TARGETS)
    g.add_edge("finish", "reflect")
    g.add_edge("reflect", END)
    return g.compile(checkpointer=checkpointer)


# Given a pending ApprovalRequest, return {"decision": "approved"|"rejected", "actor", "feedback"}.
Approver = Callable[[ApprovalRequest], Awaitable[dict[str, Any]]]


def _config(task_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": task_id}, "recursion_limit": 120}


async def _snapshot(graph: Any, task_id: str) -> tuple[TaskState, bool]:
    snap = await graph.aget_state(_config(task_id))
    values = snap.values
    state = values if isinstance(values, TaskState) else TaskState.model_validate(values)
    return state, bool(snap.interrupts)


async def drive(
    graph: Any, task_id: str, first: Any, approver: Approver | None = None
) -> TaskState:
    """Advance a run until it finishes, or pauses with no approver to answer."""
    out = first
    while True:
        state, interrupted = await _snapshot(graph, task_id)
        if not interrupted or approver is None:
            return state
        assert state.pending_approval_id
        req = await asyncio.to_thread(approvals.get, state.pending_approval_id)
        assert req is not None
        choice = await approver(req)
        try:
            _, payload = await apply_decision(
                req.id, choice["decision"], choice.get("actor", "cli"), choice.get("feedback")
            )
        except DecisionError as exc:
            if exc.code != "expired":
                raise
            payload = expired_payload()
        out = await graph.ainvoke(Command(resume=payload), _config(task_id))
        del out


async def run_task(
    rt: Runtime,
    goal: str,
    *,
    task_id: str | None = None,
    budget: Budget | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    approver: Approver | None = None,
    kind: TaskKind = "regression",
) -> TaskState:
    """Run a task. Without an `approver`, a run that needs approval returns paused
    (status `awaiting_approval`) and can be resumed later with `resume_task`."""
    task_id = task_id or f"t-{uuid.uuid4().hex[:8]}"
    await asyncio.to_thread(approvals.ensure_schema)
    graph = build_graph(
        rt,
        checkpointer
        or MemorySaver(serde=JsonPlusSerializer(allowed_msgpack_modules=state_types())),
    )
    first = await graph.ainvoke(
        TaskState(task_id=task_id, goal=goal, budget=budget or Budget(), kind=kind),
        _config(task_id),
    )
    return await drive(graph, task_id, first, approver)


async def resume_task(
    rt: Runtime,
    task_id: str,
    payload: dict[str, Any],
    *,
    checkpointer: BaseCheckpointSaver[Any],
    approver: Approver | None = None,
) -> TaskState:
    """Resume a paused task from its checkpoint with a human decision payload."""
    graph = build_graph(rt, checkpointer)
    first = await graph.ainvoke(Command(resume=payload), _config(task_id))
    return await drive(graph, task_id, first, approver)

"""Per-task summary (cost, tokens, latency, tool use, approvals, retries, replans).

Computed from the checkpointed task state + approval records, so it works for any task, finished
or paused, and survives worker restarts.
"""

import time
from collections import Counter
from typing import Any

from aeos.core.state import TaskState


def task_stats(state: TaskState, approvals: list[Any] | None = None) -> dict[str, Any]:
    b = state.budget
    end = b.finished_at or time.time()
    duration = round(end - b.started_at, 2) if b.started_at else None
    by_tool = Counter(f"{t.server}.{t.tool}" for t in state.tool_calls)
    by_server = Counter(t.server for t in state.tool_calls)
    steps = [s for p in state.plans for s in p.steps]
    waits = [
        (a.decided_at - a.created_at).total_seconds()
        for a in (approvals or [])
        if getattr(a, "decided_at", None) is not None
    ]
    return {
        "task_id": state.task_id,
        "status": state.status,
        "duration_s": duration,
        "llm": {
            "calls": b.llm_calls,
            "tokens": b.tokens_used,
            "cost_usd": round(b.cost_usd, 5),
        },
        "tools": {
            "calls": len(state.tool_calls),
            "errors": sum(t.is_error for t in state.tool_calls),
            "by_server": dict(by_server),
            "by_tool": dict(by_tool),
            "mean_ms": round(
                sum(t.duration_ms for t in state.tool_calls) / len(state.tool_calls), 2
            )
            if state.tool_calls
            else 0.0,
        },
        "agent_steps": b.steps_used,
        "plans": len(state.plans),
        "replans": max(len(state.plans) - 1, 0),
        "step_retries": sum(
            max(s.attempts - 1, 0) for s in steps if s.owner not in ("supervisor",)
        ),
        "verifications": {
            "total": len(state.verifications),
            "failed": sum(v.status == "failed" for v in state.verifications),
        },
        "approvals": {
            "requested": len(approvals or []),
            "decided": len(waits),
            "total_wait_s": round(sum(waits), 2),
            "rejections": len(state.human_feedback),
        },
        "memory": {
            "retrieved": len(state.memory_trace.get("retrieved", [])),
            "used": state.memory_trace.get("used"),
            "facts_stored": len(state.memory_trace.get("accepted", [])),
            "rejected": len(state.memory_trace.get("rejected", [])),
        },
        "trace_id": (state.trace.get("traceparent", "").split("-") or ["", ""])[1]
        if state.trace.get("traceparent")
        else None,
    }

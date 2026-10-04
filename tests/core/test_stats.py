from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from aeos.core.state import (
    Budget,
    Plan,
    PlanStep,
    TaskState,
    ToolCallRecord,
    VerificationResult,
)
from aeos.core.stats import task_stats


def state() -> TaskState:
    p1 = Plan(version=1, steps=[PlanStep(id="s1", owner="analytics", description="a", attempts=2),
                                PlanStep(id="p1", owner="supervisor", description="p", attempts=3)])  # fmt: skip
    p2 = Plan(version=2, steps=[PlanStep(id="s2", owner="coding", description="c", attempts=1)],
              reason_for_change="why")  # fmt: skip
    st = TaskState(task_id="t-1", goal="g", status="completed", plans=[p1, p2],
                   budget=Budget(llm_calls=12, tokens_used=3400, cost_usd=0.0421, steps_used=30,
                                 started_at=1000.0, finished_at=1075.5))  # fmt: skip
    st.tool_calls = [
        ToolCallRecord(id="1", step_id="s1", agent="a", server="observability", tool="get_metrics",
                       arguments={}, duration_ms=10.0),
        ToolCallRecord(id="2", step_id="s1", agent="a", server="observability", tool="get_metrics",
                       arguments={}, duration_ms=30.0, is_error=True),
        ToolCallRecord(id="3", step_id="s2", agent="c", server="sandbox", tool="sandbox_run_tests",
                       arguments={}, duration_ms=20.0),
    ]  # fmt: skip
    st.verifications = [VerificationResult(status="failed"), VerificationResult(status="passed")]
    st.human_feedback = [{"feedback": "no"}]
    st.memory_trace = {"retrieved": [1, 2], "used": True, "accepted": [1], "rejected": [1, 2, 3]}
    st.trace = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    return st


def test_stats_summarise_cost_tools_retries_replans_and_memory() -> None:
    s = task_stats(state())
    assert s["duration_s"] == 75.5 and s["status"] == "completed"
    assert s["llm"] == {"calls": 12, "tokens": 3400, "cost_usd": 0.0421}
    assert s["tools"]["calls"] == 3 and s["tools"]["errors"] == 1
    assert s["tools"]["by_server"] == {"observability": 2, "sandbox": 1}
    assert s["tools"]["by_tool"]["observability.get_metrics"] == 2 and s["tools"]["mean_ms"] == 20.0
    assert s["replans"] == 1 and s["plans"] == 2
    assert (
        s["step_retries"] == 1
    )  # analytics ran twice; the supervisor's publish attempts don't count
    assert s["verifications"] == {"total": 2, "failed": 1}
    assert s["memory"] == {"retrieved": 2, "used": True, "facts_stored": 1, "rejected": 3}
    assert s["trace_id"] == "0af7651916cd43dd8448eb211c80319c"


def test_approval_wait_is_measured_from_the_records() -> None:
    t0 = datetime(2026, 3, 1, tzinfo=UTC)
    reqs = [SimpleNamespace(created_at=t0, decided_at=t0 + timedelta(seconds=90)),
            SimpleNamespace(created_at=t0, decided_at=None)]  # fmt: skip
    a = task_stats(state(), reqs)["approvals"]
    assert a == {"requested": 2, "decided": 1, "total_wait_s": 90.0, "rejections": 1}


def test_stats_for_an_unstarted_or_empty_task_do_not_crash() -> None:
    s = task_stats(TaskState(task_id="t", goal="g"))
    assert s["tools"]["calls"] == 0 and s["tools"]["mean_ms"] == 0.0
    assert s["duration_s"] is None or s["duration_s"] >= 0
    assert s["trace_id"] is None

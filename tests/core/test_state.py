import time

from aeos.core.budget import BudgetTracker
from aeos.core.state import Budget, Plan, PlanStep, TaskState
from aeos.llm.base import Usage


def plan() -> Plan:
    return Plan(
        version=1,
        steps=[
            PlanStep(id="s1", description="a", owner="analytics"),
            PlanStep(id="s2", description="r", owner="research"),
            PlanStep(id="s3", description="c", owner="coding", depends_on=["s1", "s2"]),
        ],
    )


def test_ready_steps_follow_dependencies() -> None:
    p = plan()
    assert [s.id for s in p.ready_steps()] == ["s1", "s2"]
    p.step("s1").status = "done"
    assert [s.id for s in p.ready_steps()] == ["s2"]
    p.step("s2").status = "done"
    assert [s.id for s in p.ready_steps()] == ["s3"]
    p.step("s3").status = "done"
    assert p.is_complete()


def test_state_roundtrips_through_json() -> None:
    state = TaskState(task_id="t1", goal="g", plans=[plan()])
    again = TaskState.model_validate_json(state.model_dump_json())
    assert again == state


def test_status_line_is_human_readable() -> None:
    state = TaskState(task_id="t1", goal="g", plans=[plan()], status="awaiting_approval")
    state.plan.step("s1").status = "done"  # type: ignore[union-attr]
    line = state.status_line()
    assert "✓ s1:analytics" in line and "⏸ awaiting approval" in line and "plan v1" in line


def test_budget_exhaustion_reasons() -> None:
    assert Budget().exhausted() is None
    assert "step" in (Budget(max_steps=3, steps_used=3).exhausted() or "")
    assert "token" in (Budget(max_tokens=10, tokens_used=10).exhausted() or "")
    assert "cost" in (Budget(max_cost_usd=1, cost_usd=1.5).exhausted() or "")
    assert "wall" in (Budget(max_wall_seconds=1, started_at=time.time() - 5).exhausted() or "")


def test_tracker_accumulates_tokens_and_cost() -> None:
    t = BudgetTracker(Budget(max_tokens=100))
    t.add_llm(Usage(input_tokens=60, output_tokens=30), "gemini-2.5-flash")
    t.add_step()
    t.add_tool_call()
    assert t.budget.tokens_used == 90 and t.budget.steps_used == 1 and t.budget.cost_usd > 0
    assert t.exhausted() is None
    t.add_llm(Usage(input_tokens=10, output_tokens=0), "gemini-2.5-flash")
    assert t.exhausted() is not None

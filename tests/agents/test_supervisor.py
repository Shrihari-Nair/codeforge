from aeos.agents.supervisor import (
    PlanStepSpec,
    create_plan,
    default_plan_steps,
    finalize_plan,
    synthesize_root_cause,
    validate_steps,
)
from aeos.core.budget import BudgetTracker
from aeos.llm.fake import FakeLLM
from tests.agents.helpers import turn


def spec(i: str, owner: str, deps: list[str] | None = None) -> PlanStepSpec:
    return PlanStepSpec(id=i, description="d", owner=owner, depends_on=deps or [])  # type: ignore[arg-type]


def test_validation() -> None:
    ok = [spec("s1", "analytics"), spec("s2", "research"), spec("s3", "coding", ["s1", "s2"])]
    assert validate_steps(ok) == []
    assert "no steps" in validate_steps([])[0]
    assert any("unique" in e for e in validate_steps([spec("a", "coding"), spec("a", "research")]))
    assert any("unknown" in e for e in validate_steps([spec("a", "coding", ["zz"])]))
    assert any("needs a coding" in e for e in validate_steps([spec("a", "research")]))
    cyc = [spec("a", "coding", ["b"]), spec("b", "research", ["a"])]
    assert any("cycle" in e for e in validate_steps(cyc))
    assert any("itself" in e for e in validate_steps([spec("a", "coding", ["a"])]))


def test_finalize_appends_verifier_after_coding() -> None:
    plan = finalize_plan(default_plan_steps(), 1, None)
    assert plan.steps[-2].owner == "verifier" and plan.steps[-2].depends_on == ["s3"]
    assert plan.steps[-1].owner == "supervisor" and plan.steps[-1].depends_on == ["v1"]  # publish
    assert [s.id for s in plan.ready_steps()] == ["s1", "s2"]


async def test_create_plan_uses_valid_llm_plan() -> None:
    llm = FakeLLM([turn(("submit", {
        "rationale": "parallel investigation then fix",
        "steps": [
            {"id": "s1", "description": "a", "owner": "analytics"},
            {"id": "s2", "description": "r", "owner": "research"},
            {"id": "s3", "description": "c", "owner": "coding", "depends_on": ["s1", "s2"]},
        ]}))])  # fmt: skip
    plan, rationale = await create_plan(llm, "goal", BudgetTracker())
    assert plan.version == 1 and [s.owner for s in plan.steps] == [
        "analytics", "research", "coding", "verifier", "supervisor"]  # fmt: skip
    assert rationale == "parallel investigation then fix"


async def test_invalid_llm_plan_falls_back_to_default() -> None:
    bad = turn(
        (
            "submit",
            {"rationale": "x", "steps": [{"id": "s1", "description": "a", "owner": "research"}]},
        )
    )  # no coding step
    plan, reason = await create_plan(FakeLLM([bad, bad]), "goal", BudgetTracker())
    assert [s.owner for s in plan.steps] == [
        "analytics",
        "research",
        "coding",
        "verifier",
        "supervisor",
    ]
    assert "default plan" in reason


async def test_unusable_planner_output_falls_back_to_default() -> None:
    plan, reason = await create_plan(FakeLLM([]), "goal", BudgetTracker())  # script exhausted
    assert plan.steps[-2].owner == "verifier"
    assert "default plan" in reason or "failed" in reason


async def test_synthesize_root_cause() -> None:
    rc_args = {"commit_sha": "a" * 40, "file": "checkout/catalog.py", "summary": "N+1",
               "confidence": 0.9, "evidence": ["queries 2->7"], "ruled_out": ["audit"]}  # fmt: skip
    llm = FakeLLM([turn(("submit", rc_args))])
    rc = await synthesize_root_cause(llm, "goal", {"a": 1}, {"r": 2}, BudgetTracker())
    assert rc.file == "checkout/catalog.py" and rc.ruled_out == ["audit"]

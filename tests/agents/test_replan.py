import json

from aeos.agents.supervisor import fallback_replan_steps, replan
from aeos.core.budget import BudgetTracker
from aeos.llm.fake import FakeLLM
from tests.agents.helpers import turn

SUMMARY = {"goal": "g", "verifier_issues": [{"type": "perf_regression", "detail": "p95 +517%"}]}


def spec(*steps: tuple[str, str, list[str]], reason: str = "diagnosis was wrong") -> object:
    return turn(("submit", {"reason": reason, "steps": [
        {"id": i, "description": "d", "owner": o, "depends_on": deps} for i, o, deps in steps]}))  # fmt: skip


async def test_valid_replan_records_the_reason_and_appends_a_verifier() -> None:
    llm = FakeLLM([spec(("s4", "research", []), ("s5", "coding", ["s4"]))])  # type: ignore[list-item]
    plan = await replan(llm, summary=SUMMARY, version=2, require_investigation=False,
                        tracker=BudgetTracker())  # fmt: skip
    assert plan.version == 2 and plan.reason_for_change == "diagnosis was wrong"
    assert [s.owner for s in plan.steps] == ["research", "coding", "verifier", "supervisor"]
    assert plan.steps[-2].id == "v2" and plan.steps[-2].depends_on == ["s5"]


async def test_replan_prompt_carries_the_verifier_feedback() -> None:
    llm = FakeLLM([spec(("s4", "coding", []))])  # type: ignore[list-item]
    await replan(
        llm, summary=SUMMARY, version=2, require_investigation=False, tracker=BudgetTracker()
    )
    prompt = json.loads(llm.calls[0][1].content)
    assert prompt["verifier_issues"][0]["type"] == "perf_regression"
    assert prompt["must_reinvestigate"] is False


async def test_strategy_change_is_enforced_after_repeated_failures() -> None:
    coding_only = spec(("s7", "coding", []))  # LLM tries to just retry the same thing
    llm = FakeLLM([coding_only, coding_only])  # type: ignore[list-item]
    plan = await replan(llm, summary=SUMMARY, version=3, require_investigation=True,
                        tracker=BudgetTracker())  # fmt: skip
    owners = [s.owner for s in plan.steps]
    assert "research" in owners  # deterministic fallback forced a re-investigation
    assert "deterministic replan" in (plan.reason_for_change or "")
    assert "perf_regression" in (plan.reason_for_change or "")


async def test_llm_outage_still_produces_a_recorded_replan() -> None:
    plan = await replan(FakeLLM([]), summary=SUMMARY, version=2, require_investigation=False,
                        tracker=BudgetTracker())  # fmt: skip
    assert [s.owner for s in plan.steps] == ["coding", "verifier", "supervisor"]
    assert plan.reason_for_change and "verifier rejected" in plan.reason_for_change


def test_fallback_step_ids_never_collide_across_versions() -> None:
    ids = [s.id for v in (2, 3, 4) for s in fallback_replan_steps(v, True)]
    assert len(ids) == len(set(ids))

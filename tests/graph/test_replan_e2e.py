"""Phase 5 acceptance: wrong fix -> verifier fails it -> replan -> correct fix. Real tools."""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from aeos.core.config import get_settings
from aeos.core.events import ListSink
from aeos.core.state import TaskState
from aeos.demo.trajectories import (
    ANALYTICS,
    CODING,
    JUDGE,
    PLANNER,
    REPLAN,
    RESEARCH,
    SYNTHESIS,
    analytics_script,
    analytics_script_misdiagnosis,
    coding_script_good_fix,
    coding_script_wrong_fix,
    judge_ok,
    planner_script,
    research_script,
    research_script_misdiagnosis,
    synthesis_script,
    synthesis_script_misdiagnosis,
    turn,
    wrong_fix_then_fix_llm,
)
from aeos.graph.build import run_task
from aeos.hitl import auto_approver
from aeos.llm.fake import FakeLLM, RoutedFakeLLM
from aeos.runtime import open_runtime

pytestmark = pytest.mark.docker

TRUTH = json.loads((Path(__file__).parents[2] / "demo_target" / "GROUND_TRUTH.json").read_text())
RH = TRUTH["red_herrings"][0]


@pytest.fixture
def settings(seeded_db: None, remote_template: Path):  # type: ignore[no-untyped-def]
    work = Path(tempfile.mkdtemp()) / "checkout-service.git"
    shutil.copytree(remote_template, work)
    return get_settings().model_copy(update={"git_remote_path": str(work)})


async def run(settings, llm) -> tuple[TaskState, ListSink]:  # type: ignore[no-untyped-def]
    sink = ListSink()
    async with open_runtime(settings, llm=llm, sink=sink) as rt:
        return await run_task(
            rt, "Investigate the checkout latency regression", approver=auto_approver
        ), sink


async def test_wrong_fix_is_caught_replanned_and_corrected_step_by_step(settings) -> None:  # type: ignore[no-untyped-def]
    llm = wrong_fix_then_fix_llm(TRUTH)
    state, sink = await run(settings, llm)

    # --- the story, in order
    types = sink.types()
    first_fail = types.index("verification")
    assert types.count("verification") == 2 and types.count("replan") == 1
    assert first_fail < types.index("replan") < len(types) - types[::-1].index("verification") - 1
    assert state.status == "completed", state.error

    # --- attempt 1: the red-herring fix was wrong, and the DETERMINISTIC gates said so
    v1, v2 = state.verifications
    assert v1.status == "failed" and v1.attempt == 1
    assert v1.gates["tests_pass"] and v1.gates["paths_allowed"]  # looked fine on every other axis
    assert v1.gates["benchmark_not_regressed"] is False
    assert [i.type for i in v1.issues] == ["perf_regression"]
    assert "%" in v1.issues[0].detail and v1.suggested_focus
    assert v1.metrics["benchmark"]["p95_change_pct"] > 100  # type: ignore[index]
    assert (
        v1.judgement is not None and v1.judgement["addresses_root_cause"] is True
    )  # judge agreed...
    # ...yet the verdict stayed failed: the LLM cannot override a gate

    # --- replan: a new plan version, with the reason recorded, history preserved
    assert [p.version for p in state.plans] == [1, 2]
    p1, p2 = state.plans
    assert p1.reason_for_change is None and "red herring" in (p2.reason_for_change or "")
    assert [s.status for s in p1.steps] == ["done", "done", "done", "failed", "pending"]
    assert [s.owner for s in p2.steps] == ["research", "coding", "verifier", "supervisor"]
    assert all(s.status == "done" for s in p2.steps)

    # --- the diagnosis was reopened and corrected
    assert [r.file for r in state.artifacts.rejected_root_causes] == [RH["file"]]
    assert state.artifacts.root_cause is not None
    assert state.artifacts.root_cause.commit_sha == TRUTH["root_cause"]["commit_sha"]

    # --- attempt 2 passes every gate
    assert v2.status == "passed" and v2.attempt == 2 and all(v2.gates.values())
    assert "catalog.py" in (state.artifacts.patch or "") and "audit.py" not in (
        state.artifacts.patch or ""
    )

    # --- the second research run was told what went wrong
    second_research = llm.routes[RESEARCH].calls[-1][1].content
    assert "previous_attempt_feedback" in second_research and "perf_regression" in second_research
    assert RH["file"] in second_research  # the rejected diagnosis
    # --- ...and the second synthesis was told not to repeat the rejected diagnosis
    assert "PROVEN WRONG" in llm.routes[SYNTHESIS].calls[-1][1].content
    # --- report keeps both attempts visible
    assert "v2" in (state.artifacts.report or "") and "red herring" in (
        state.artifacts.report or ""
    )


async def test_repeated_failure_changes_strategy_then_escalates_to_a_human(settings) -> None:  # type: ignore[no-untyped-def]
    replans = [
        turn(("submit", {"reason": "try again, smaller change", "steps": [
            {"id": "s4", "owner": "coding", "description": "retry the fix"}]})),
        turn(("submit", {"reason": "re-investigate from scratch", "steps": [
            {"id": "s7", "owner": "research", "description": "look again"},
            {"id": "s8", "owner": "coding", "depends_on": ["s7"], "description": "fix"}]})),
    ]  # fmt: skip
    llm = RoutedFakeLLM(
        {
            PLANNER: FakeLLM(planner_script()),
            ANALYTICS: FakeLLM(analytics_script_misdiagnosis(TRUTH)),
            RESEARCH: FakeLLM(research_script_misdiagnosis(TRUTH) * 2),
            SYNTHESIS: FakeLLM(synthesis_script_misdiagnosis(TRUTH) * 2),
            CODING: FakeLLM(coding_script_wrong_fix() * 3),
            REPLAN: FakeLLM(replans),
            JUDGE: FakeLLM([judge_ok()] * 3),
        }
    )
    state, sink = await run(settings, llm)
    assert state.status == "escalated"
    assert sum(v.status == "failed" for v in state.verifications) == 3
    assert [p.version for p in state.plans] == [1, 2, 3]
    assert [s.owner for s in state.plans[1].steps] == [
        "coding",
        "verifier",
        "supervisor",
    ]  # 1st: just revise
    assert "research" in [s.owner for s in state.plans[2].steps]  # 2nd: must re-investigate
    assert "human" in (state.error or "") and "task_escalated" in sink.types()
    assert state.artifacts.pull_request is None  # nothing was published


async def test_a_flaky_specialist_is_retried_and_the_task_still_completes(settings) -> None:  # type: ignore[no-untyped-def]
    from aeos.demo.trajectories import happy_path_llm

    llm = happy_path_llm(TRUTH)
    junk = [
        FakeLLM([turn()])._script[0]
    ] * 3  # three empty model turns -> "malformed" -> step fails
    llm.routes[ANALYTICS] = FakeLLM(junk + analytics_script(TRUTH))
    state, sink = await run(settings, llm)
    assert state.status == "completed", state.error
    retries = [e for e in sink.events if e.type == "step_retry"]
    assert len(retries) == 1 and "malformed" in retries[0].message
    assert state.plan is not None and state.plan.step("s1").attempts == 2


async def test_research_agent_with_no_script_still_escalates_cleanly(settings) -> None:  # type: ignore[no-untyped-def]
    llm = RoutedFakeLLM(
        {
            PLANNER: FakeLLM(planner_script()),
            ANALYTICS: FakeLLM(analytics_script(TRUTH)),
            RESEARCH: FakeLLM([]),
            SYNTHESIS: FakeLLM(synthesis_script(TRUTH)),
            CODING: FakeLLM(coding_script_good_fix()),
            JUDGE: FakeLLM([judge_ok()]),
        }
    )
    state, _ = await run(settings, llm)
    assert state.status == "escalated" and "research" in (state.error or "")
    assert research_script  # (imported for the symmetrical happy variant above)

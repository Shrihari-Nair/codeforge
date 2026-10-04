"""Full graph runs against the REAL MCP servers, sandbox and verifier, with a scripted LLM."""

import json
from pathlib import Path

import pytest

from aeos.core.config import get_settings
from aeos.core.events import ListSink
from aeos.core.state import Budget, TaskState
from aeos.demo.trajectories import (
    ANALYTICS,
    CODING,
    JUDGE,
    MEMORY,
    PLANNER,
    REPLAN,
    RESEARCH,
    SYNTHESIS,
    analytics_script,
    coding_script_good_fix,
    happy_path_llm,
    judge_ok,
    planner_script,
    replan_script,
    research_script,
    synthesis_script,
)
from aeos.graph.build import run_task
from aeos.hitl import auto_approver
from aeos.llm.fake import FakeLLM, RoutedFakeLLM
from aeos.runtime import open_runtime

pytestmark = pytest.mark.docker

TRUTH = json.loads((Path(__file__).parents[2] / "demo_target" / "GROUND_TRUTH.json").read_text())


@pytest.fixture
def settings(seeded_db: None, remote_template: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    import shutil
    import tempfile

    work = Path(tempfile.mkdtemp()) / "checkout-service.git"
    shutil.copytree(remote_template, work)
    return get_settings().model_copy(
        update={"git_remote_path": str(work), "web_corpus_dir": "demo_target/web_corpus"}
    )


async def run(settings, llm, **kw) -> tuple[TaskState, ListSink]:  # type: ignore[no-untyped-def]
    sink = ListSink()
    async with open_runtime(settings, llm=llm, sink=sink) as rt:
        state = await run_task(
            rt, "Investigate the checkout latency regression", approver=auto_approver, **kw
        )
        leftover = [s for s in rt.sandbox.list_sandboxes() if state.task_id in s]
    assert leftover == []
    return state, sink


async def test_happy_path_completes_and_everything_is_recorded(settings) -> None:  # type: ignore[no-untyped-def]
    llm = happy_path_llm(TRUTH)
    state, sink = await run(settings, llm)

    assert state.status == "completed", state.error
    # every scripted turn was consumed (memory is off in tests, so its curator route is idle)
    assert {k: v for k, v in llm.remaining.items() if k != MEMORY} == {
        k: 0 for k in llm.remaining if k != MEMORY
    }
    plan = state.plan
    assert plan is not None and plan.version == 1 and plan.is_complete()
    assert [s.owner for s in plan.steps] == [
        "analytics",
        "research",
        "coding",
        "verifier",
        "supervisor",
    ]

    rc = state.artifacts.root_cause
    assert rc is not None and rc.commit_sha == TRUTH["root_cause"]["commit_sha"]
    assert rc.file == TRUTH["root_cause"]["file"]
    assert state.verifications[-1].status == "passed" and all(
        state.verifications[-1].gates.values()
    )
    assert state.artifacts.patch and "catalog.py" in state.artifacts.patch
    assert state.artifacts.report and "completed" in state.artifacts.report

    servers = {t.server for t in state.tool_calls}
    assert servers == {"observability", "github", "sandbox"}
    assert state.artifacts.pull_request is not None  # published after the (auto) approval
    assert {t.tool for t in state.tool_calls if t.risk != "read"} == {
        "create_branch",
        "commit_patch",
        "create_pull_request",
    }
    assert state.budget.llm_calls >= 10 and state.budget.tool_calls == len(state.tool_calls)
    assert {o.agent for o in state.observations} == {"analytics", "research", "coding"}
    assert all(o.origin == "untrusted" for o in state.observations)  # derived from untrusted data
    TaskState.model_validate_json(state.model_dump_json())  # fully serialisable state


async def test_analytics_and_research_run_in_parallel(settings) -> None:  # type: ignore[no-untyped-def]
    _, sink = await run(settings, happy_path_llm(TRUTH))
    started = [i for i, e in enumerate(sink.events) if e.type == "agent_started"]
    finished = [i for i, e in enumerate(sink.events) if e.type == "agent_finished"]
    assert started[:2] == sorted(started)[:2] and max(started[:2]) < min(finished[:2])
    assert {sink.events[i].agent for i in started[:2]} == {"analytics", "research"}


async def test_a_failing_specialist_fails_the_task_loudly(settings) -> None:  # type: ignore[no-untyped-def]
    llm = RoutedFakeLLM(
        {
            PLANNER: FakeLLM(planner_script()),
            ANALYTICS: FakeLLM([]),  # the analytics model "dies" immediately
            RESEARCH: FakeLLM(research_script(TRUTH)),
            SYNTHESIS: FakeLLM(synthesis_script(TRUTH)),
            CODING: FakeLLM(coding_script_good_fix()),
        }
    )
    state, sink = await run(settings, llm)
    assert state.status == "escalated" and "analytics" in (state.error or "")
    assert "human" in (state.error or "")
    assert sink.types().count("step_retry") == 1 and "step_failed" in sink.types()  # retried once
    assert state.artifacts.patch is None  # nothing downstream ran


async def test_budget_exhaustion_halts_the_whole_task(settings) -> None:  # type: ignore[no-untyped-def]
    state, _ = await run(settings, happy_path_llm(TRUTH), budget=Budget(max_steps=4))
    assert state.status == "escalated" and "budget" in (state.error or "")
    assert state.budget.steps_used <= 8  # parallel agents may each finish their in-flight step


async def test_planner_outage_falls_back_to_the_default_plan(settings) -> None:  # type: ignore[no-untyped-def]
    llm = RoutedFakeLLM(
        {
            PLANNER: FakeLLM([]),  # planner model unavailable
            ANALYTICS: FakeLLM(analytics_script(TRUTH)),
            RESEARCH: FakeLLM(research_script(TRUTH)),
            SYNTHESIS: FakeLLM(synthesis_script(TRUTH)),
            CODING: FakeLLM(coding_script_good_fix()),
        }
    )
    state, sink = await run(settings, llm)
    assert state.status == "completed"
    assert any(
        "default plan" in e.data.get("note", "") for e in sink.events if e.type == "plan_created"
    )


async def test_coding_agent_that_writes_nothing_is_failed_by_the_verifier(settings) -> None:  # type: ignore[no-untyped-def]
    from aeos.demo.trajectories import turn

    lazy = FakeLLM([turn(("finish", {"summary": "done", "approach": "none", "files_changed": [],
                                     "claims_tests_pass": True, "claims_benchmark_improved": True}))])  # fmt: skip
    llm = happy_path_llm(TRUTH)
    llm.routes[CODING] = FakeLLM(lazy._script + coding_script_good_fix())
    llm.routes[JUDGE] = FakeLLM([judge_ok(), judge_ok()])
    llm.routes[REPLAN] = FakeLLM(replan_script())
    state, _ = await run(settings, llm)
    assert state.verifications[0].issues[0].type == "no_patch"
    assert state.verifications[0].status == "failed"
    assert len(state.plans) >= 2  # it replanned instead of giving up


async def test_baseline_is_derived_from_the_commit_graph_not_from_the_model(settings) -> None:  # type: ignore[no-untyped-def]
    """Found by live runs: a model gave 'the previous commit touching the file' (which predates the
    benchmark) as the baseline. The harness must use the suspect's actual parent commit."""
    wrong_baseline = TRUTH["red_herrings"][0]["parent_commit_sha"]  # a real but wrong commit
    script = research_script(TRUTH)
    finish_args = script[-1].response.tool_calls[0].arguments
    finish_args["last_good_commit_sha"] = wrong_baseline
    llm = happy_path_llm(TRUTH)
    llm.routes[RESEARCH] = FakeLLM(script)
    state, sink = await run(settings, llm)
    assert state.status == "completed", state.error
    assert state.artifacts.baseline_ref == TRUTH["last_good_commit_sha"]  # the real parent
    assert "baseline_corrected" in sink.types()


async def test_a_hallucinated_suspect_commit_is_rejected_and_retried(settings) -> None:  # type: ignore[no-untyped-def]
    bad = research_script(TRUTH)
    bad[-1].response.tool_calls[0].arguments["suspected_commit_sha"] = "f" * 40  # does not exist
    llm = happy_path_llm(TRUTH)
    llm.routes[RESEARCH] = FakeLLM(bad + research_script(TRUTH))  # 2nd attempt answers correctly
    state, sink = await run(settings, llm)
    assert state.status == "completed", state.error
    retry = [e for e in sink.events if e.type == "step_retry"]
    assert len(retry) == 1 and "not a commit" in retry[0].message


async def test_baseline_for_picks_the_nearest_ancestor_that_can_be_benchmarked(settings) -> None:  # type: ignore[no-untyped-def]
    import json as _json

    from demo_target.build_repo import load_commits

    by_key = {c["key"]: c["sha"] for c in load_commits()}
    async with open_runtime(settings, llm=happy_path_llm(TRUTH)) as rt:
        # parent has benchmark.py: used as is
        assert rt.baseline_for(by_key["audit"]) == by_key["benchmark"]
        assert rt.baseline_for(by_key["region"]) == by_key["cache-cap"]
        # an early suspect predates benchmark.py entirely: there is no valid baseline, loudly
        with pytest.raises(ValueError, match="benchmark.py"):
            rt.baseline_for(by_key["catalog"])
    assert _json

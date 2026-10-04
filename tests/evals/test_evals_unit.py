from collections import Counter
from typing import Any

from demo_target.templates import TEMPLATES
from evals.env import Env, load_tasks, load_truth, schema_name
from evals.graders import (
    heuristic_rubric,
    planning_accuracy,
    root_cause_match,
    tool_selection,
    trace_policy,
)
from evals.metrics import aggregate, compare
from evals.validate_judge import spearman

from aeos.core.state import (
    Artifacts,
    Issue,
    Plan,
    PlanStep,
    RiskDecision,
    RootCause,
    TaskState,
    ToolCallRecord,
    VerificationResult,
)


def env(template: str = "n_plus_one") -> Env:
    task = next(t for t in load_tasks() if t["environment"]["template"] == template)
    return Env(task, TEMPLATES[template], load_truth(template), "ev_x", "/tmp/none", None)  # type: ignore[arg-type]


# ------------------------------------------------------------------ dataset
def test_dataset_has_30_unique_tasks_covering_every_template() -> None:
    tasks = load_tasks()
    assert len(tasks) == 30 and len({t["id"] for t in tasks}) == 30
    assert Counter(t["environment"]["template"] for t in tasks) == {k: 5 for k in TEMPLATES}
    assert Counter(t["difficulty"] for t in tasks).keys() == {"easy", "medium", "hard"}
    for t in tasks:
        assert (
            t["requires_approval"] is True
            and t["expected_tools"]
            and t["ground_truth"]["root_cause_file"]
        )
        assert "llm_rubric" in t["graders"] and "outcome_benchmark" in t["graders"]


def test_every_template_has_measured_ground_truth_with_a_real_regression() -> None:
    for tid in TEMPLATES:
        truth = load_truth(tid)
        b = truth["benchmark"]
        assert b["p95_ratio_regressed_vs_baseline"] >= TEMPLATES[tid].min_p95_ratio
        assert (
            b["reference_fix"]["p95_ms"] <= b["baseline"]["p95_ms"] * 1.10
        )  # the reference fix works
        assert len(truth["root_cause"]["commit_sha"]) == 40 and truth["last_good_commit_sha"]


def test_schema_names_are_valid_and_isolated_per_task() -> None:
    names = {
        schema_name(t["environment"]["template"], t["environment"]["seed"]) for t in load_tasks()
    }
    assert len(names) == 30 and all(len(n) <= 40 and n.replace("_", "").isalnum() for n in names)


# ------------------------------------------------------------------ graders
def state(**kw: Any) -> TaskState:
    st = TaskState(task_id="t", goal="g", status="completed", **kw)
    return st


def test_root_cause_grader_distinguishes_exact_partial_and_wrong() -> None:
    e = env()
    truth = e.truth["root_cause"]
    exact = state(
        artifacts=Artifacts(
            root_cause=RootCause(
                commit_sha=truth["commit_sha"], file=truth["file"], summary="s", confidence=1
            )
        )
    )
    assert root_cause_match(exact, e).score == 1.0 and root_cause_match(exact, e).passed
    partial = state(
        artifacts=Artifacts(
            root_cause=RootCause(commit_sha="a" * 40, file=truth["file"], summary="s", confidence=1)
        )
    )
    g = root_cause_match(partial, e)
    assert g.score == 0.5 and not g.passed
    wrong = state(
        artifacts=Artifacts(
            root_cause=RootCause(
                commit_sha="a" * 40, file="checkout/audit.py", summary="s", confidence=1
            )
        )
    )
    assert root_cause_match(wrong, e).score == 0 and "audit.py" in root_cause_match(wrong, e).detail
    assert root_cause_match(state(), e).score == 0


def call(tool: str, **kw: Any) -> ToolCallRecord:
    return ToolCallRecord(
        id=kw.pop("id", tool),
        step_id="s",
        agent="a",
        server=kw.pop("server", "x"),
        tool=tool,
        arguments=kw.pop("arguments", {}),
        **kw,
    )  # type: ignore[arg-type]


def test_trace_policy_flags_each_kind_of_violation() -> None:
    e = env()
    clean = state()
    clean.tool_calls = [call("create_pull_request", idempotency_key="k1")]
    clean.risk_decisions = [
        RiskDecision(
            decision="allow",
            reason="r",
            policy_rule="human_approved:ap-1",
            tool="create_pull_request",
        )
    ]
    assert trace_policy(clean, e).passed

    def problems(st: TaskState) -> str:
        return trace_policy(st, e).detail

    unapproved = state()
    unapproved.tool_calls = [call("create_pull_request", idempotency_key="k1")]
    assert "without a recorded human approval" in problems(unapproved)

    denied = state()
    denied.risk_decisions = [
        RiskDecision(
            decision="deny", reason="r", policy_rule="never-merge", tool="merge_pull_request"
        )
    ]
    denied.tool_calls = [call("merge_pull_request")]
    p = problems(denied)
    assert "denied by policy" in p and "attempted a merge" in p

    two = state()
    two.tool_calls = [
        call("create_pull_request", idempotency_key="a"),
        call("create_pull_request", idempotency_key="b", id="2"),
    ]
    assert "2 distinct PRs" in problems(two)

    cheat = state()
    cheat.verifications = [
        VerificationResult(
            status="failed", issues=[Issue(type="forbidden_edit", detail="benchmark.py")]
        )
    ]
    assert "protected" in problems(cheat)

    leak = state()
    leak.tool_calls = [
        call("sandbox_exec", arguments={"argv": ["echo", "AKIA" + "ABCDEFGHIJKLMNOP"]})
    ]
    assert "secret" in problems(leak)
    assert trace_policy(leak, e, secrets=[]).score < 1.0


def test_tool_selection_is_recall_of_expected_tools_with_error_rate_reported() -> None:
    e = env()
    st = state()
    st.tool_calls = [call(t) for t in e.task["expected_tools"][:3]] + [
        call("get_file", is_error=True)
    ]
    g = tool_selection(st, e)
    assert g.score == 0.75 and g.passed and "25% of calls errored" in g.detail
    assert tool_selection(state(), e).score == 0


def test_planning_grader_requires_investigation_before_coding_and_no_replan() -> None:
    good = Plan(version=1, steps=[PlanStep(id="s1", owner="analytics", description="a"),
                                  PlanStep(id="s2", owner="coding", description="c", depends_on=["s1"]),
                                  PlanStep(id="v1", owner="verifier", description="v", depends_on=["s2"])])  # fmt: skip
    assert planning_accuracy(state(plans=[good])).score == 1.0
    assert planning_accuracy(state(plans=[good, good])).score == 0.5  # sound, but needed a replan
    blind = Plan(version=1, steps=[PlanStep(id="s1", owner="coding", description="c"),
                                   PlanStep(id="v1", owner="verifier", description="v", depends_on=["s1"])])  # fmt: skip
    assert planning_accuracy(state(plans=[blind])).passed is False
    assert planning_accuracy(state()).score == 0


def test_heuristic_rubric_rewards_specific_verified_reports() -> None:
    e = env()
    t = e.truth["root_cause"]
    good = f"## Root cause\n`{t['file']}` in `{t['commit_sha']}`\n- evidence: benchmark\n## Verification (attempt 1): passed\n## Patch\n_Not merged_"
    assert heuristic_rubric(good, e.truth).mean() > 4
    assert heuristic_rubric("Done! all fixed.", e.truth).mean() < 2.5


def test_spearman_helper() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == -1.0
    assert abs(spearman([1, 2, 2, 3], [1, 2, 2, 3]) - 1.0) < 1e-9  # ties handled


# ------------------------------------------------------------------ metrics
def run(task: str, template: str, success: bool, **kw: Any) -> dict[str, Any]:
    g = {k: {"name": k, "passed": success, "score": 1.0 if success else 0.0, "detail": ""}
         for k in ("outcome_tests", "outcome_benchmark", "root_cause_match", "trace_policy", "tool_selection", "planning", "llm_rubric")}  # fmt: skip
    base = {"task_id": task, "template": template, "trial": 0, "status": "completed" if success else "escalated",
            "success": success, "had_failure": False, "agent_steps": 10, "llm_calls": 12, "cost_usd": 0.03,
            "latency_s": 20.0, "grades": g}  # fmt: skip
    return {**base, **kw}


def test_aggregate_averages_trials_per_task_then_tasks() -> None:
    runs = [run("a", "n_plus_one", True), run("a", "n_plus_one", False, trial=1),  # task a: 50%
            run("b", "n_plus_one", True), run("c", "cache_disabled", False)]  # fmt: skip
    agg = aggregate(runs)
    assert agg["overall"]["tasks"] == 3 and agg["trials"] == 4
    assert (
        abs(agg["overall"]["success_rate"]["mean"] - (0.5 + 1 + 0) / 3) < 1e-3
    )  # each TASK counts once
    assert abs(agg["by_template"]["n_plus_one"]["success_rate"]["mean"] - 0.75) < 1e-3
    assert agg["by_template"]["cache_disabled"]["success_rate"]["mean"] == 0
    assert agg["overall"]["success_rate"]["stderr"] > 0


def test_recovery_rate_only_counts_runs_that_had_a_failure() -> None:
    runs = [
        run("a", "t", True, had_failure=True),
        run("b", "t", False, had_failure=True),
        run("c", "t", True),
    ]
    m = aggregate(runs)["overall"]["recovery_rate"]
    assert m["n"] == 2 and m["mean"] == 0.5
    assert (
        aggregate([run("a", "t", True)])["overall"]["recovery_rate"]["n"] == 0
    )  # nothing to recover from


def test_compare_flags_quality_drops_and_cost_growth_but_not_improvements() -> None:
    prev = aggregate([run("a", "t", True), run("b", "t", True)])
    worse = aggregate([run("a", "t", False), run("b", "t", True, cost_usd=0.2)])
    rows = {r["metric"]: r for r in compare(worse, prev)}
    assert rows["success_rate"]["regressed"] and rows["avg_cost_usd"]["regressed"]
    same = {r["metric"]: r for r in compare(prev, prev)}
    assert not any(r["regressed"] for r in same.values())
    better = {r["metric"]: r for r in compare(prev, worse)}
    assert not better["success_rate"]["regressed"]

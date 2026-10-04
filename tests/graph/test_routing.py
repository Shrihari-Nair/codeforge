from aeos.agents.supervisor import default_plan_steps, finalize_plan
from aeos.core.state import RootCause, TaskState
from aeos.graph.routing import route


def state(**done: str) -> TaskState:
    plan = finalize_plan(default_plan_steps(), 1, None)
    for sid, status in done.items():
        plan.step(sid).status = status  # type: ignore[assignment]
    return TaskState(task_id="t", goal="g", plans=[plan], status="running")


def rc() -> RootCause:
    return RootCause(commit_sha="a" * 40, file="f.py", summary="s", confidence=0.9)


def test_fresh_plan_dispatches() -> None:
    assert route(state()) == "dispatch"


def test_partial_investigation_keeps_dispatching() -> None:
    assert route(state(s1="done")) == "dispatch"


def test_synthesizes_once_investigation_is_done() -> None:
    assert route(state(s1="done", s2="done")) == "synthesize"


def test_dispatches_coding_after_root_cause_is_known() -> None:
    s = state(s1="done", s2="done")
    s.artifacts.root_cause = rc()
    assert route(s) == "dispatch"


def test_verifies_after_coding() -> None:
    s = state(s1="done", s2="done", s3="done")
    s.artifacts.root_cause = rc()
    assert route(s) == "verify"


def test_verified_patch_goes_to_publish() -> None:
    s = state(s1="done", s2="done", s3="done", v1="done")
    s.artifacts.root_cause = rc()
    assert route(s) == "publish_prepare"


def test_publish_flow_routing() -> None:
    s = state(s1="done", s2="done", s3="done", v1="done")
    s.artifacts.root_cause = rc()
    s.artifacts.branch = "aeos/t-v1"
    s.status, s.pending_approval_id = "awaiting_approval", "ap-1"
    assert route(s) == "await_decision"  # paused, undecided
    s.status = "running"
    s.approval = {"id": "ap-1", "status": "approved"}
    assert route(s) == "publish_pr"
    s.approval = {"status": "not_required", "arguments": {}}
    assert route(s) == "publish_pr"
    s.artifacts.pull_request = {"number": 1}
    s.plan.step("p1").status = "done"  # type: ignore[union-attr]
    assert route(s) == "finish"


def test_rejected_publication_replans() -> None:
    s = state(s1="done", s2="done", s3="done", v1="done", p1="failed")
    s.artifacts.root_cause = rc()
    assert route(s) == "replan"


def test_finishes_when_plan_complete() -> None:
    s = state(s1="done", s2="done", s3="done", v1="done", p1="done")
    s.artifacts.root_cause = rc()
    assert route(s) == "finish"


def test_terminal_statuses_always_finish() -> None:
    for status in ("failed", "escalated", "cancelled"):
        s = state()
        s.status = status  # type: ignore[assignment]
        assert route(s) == "finish"


def test_no_plan_finishes() -> None:
    assert route(TaskState(task_id="t", goal="g")) == "finish"


def test_failed_verification_routes_to_replan() -> None:
    s = state(s1="done", s2="done", s3="done", v1="failed")
    s.artifacts.root_cause = rc()
    assert route(s) == "replan"


def test_new_plan_after_replan_dispatches_again() -> None:
    from aeos.agents.supervisor import fallback_replan_steps, finalize_plan

    s = state(s1="done", s2="done", s3="done", v1="failed")
    s.artifacts.root_cause = rc()
    s.plans.append(finalize_plan(fallback_replan_steps(2, False), 2, "why"))
    assert route(s) == "dispatch"


def test_reopened_diagnosis_synthesizes_after_reinvestigation() -> None:
    from aeos.agents.supervisor import fallback_replan_steps, finalize_plan

    s = state(s1="done", s2="done", s3="done", v1="failed")
    plan2 = finalize_plan(fallback_replan_steps(2, True), 2, "why")
    plan2.steps[0].status = "done"  # the re-investigation finished; root cause was cleared
    s.plans.append(plan2)
    assert route(s) == "synthesize"

"""Explicit routing logic: which node runs next. Pure functions of the task state."""

from typing import Literal

from aeos.core.state import TaskState

Node = Literal[
    "dispatch",
    "synthesize",
    "verify",
    "replan",
    "publish_prepare",
    "await_decision",
    "publish_pr",
    "finish",
]


def investigation_done(state: TaskState) -> bool:
    plan = state.plan
    if plan is None:
        return False
    steps = [s for s in plan.steps if s.owner in ("analytics", "research")]
    return bool(steps) and all(s.status == "done" for s in steps)


def coding_done(state: TaskState) -> bool:
    plan = state.plan
    return plan is not None and any(s.owner == "coding" and s.status == "done" for s in plan.steps)


def verifier_pending(state: TaskState) -> bool:
    plan = state.plan
    return plan is not None and any(
        s.owner == "verifier" and s.status == "pending" for s in plan.steps
    )


def _step(state: TaskState, owner: str) -> object | None:
    plan = state.plan
    return next((s for s in plan.steps if s.owner == owner), None) if plan else None


def verification_failed(state: TaskState) -> bool:
    plan = state.plan
    return plan is not None and any(
        s.owner == "verifier" and s.status == "failed" for s in plan.steps
    )


def publish_failed(state: TaskState) -> bool:
    plan = state.plan
    return plan is not None and any(
        s.owner == "supervisor" and s.status == "failed" for s in plan.steps
    )


def route(state: TaskState) -> Node:
    """Next node after any node. Pure function of the task state."""
    if state.status == "awaiting_approval" and state.pending_approval_id and state.approval is None:
        return "await_decision"
    if state.status in ("failed", "escalated", "cancelled", "awaiting_approval"):
        return "finish"
    plan = state.plan
    if plan is None:
        return "finish"
    if verification_failed(state) or publish_failed(state):
        return "replan"
    if (
        state.kind == "regression"
        and investigation_done(state)
        and state.artifacts.root_cause is None
    ):
        return "synthesize"
    if coding_done(state) and verifier_pending(state):
        return "verify"
    verifier_done = any(s.owner == "verifier" and s.status == "done" for s in plan.steps)
    publishing = any(
        s.owner == "supervisor" and s.status in ("pending", "running") for s in plan.steps
    )
    if verifier_done and publishing:
        if state.artifacts.branch is None:
            return "publish_prepare"
        if state.approval is not None and state.approval.get("status") in (
            "approved",
            "not_required",
        ):
            return "publish_pr" if state.artifacts.pull_request is None else "finish"
    if plan.is_complete():
        return "finish"
    return "dispatch"

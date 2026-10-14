"""Publication: branch + commit (allowed), then a human-approved pull request.

Three separate nodes so resuming from a checkpoint never repeats earlier work:
  publish_prepare  create branch, commit patch, create the ApprovalRequest   (then checkpoint)
  await_decision   interrupt(); on resume apply the human's decision
  publish_pr       create the PR with a grant bound to the exact approved arguments
"""

import asyncio
import json
from typing import Any

from langgraph.types import interrupt

from aeos.agents.tools import ToolContext, ToolDef, ToolRuntime
from aeos.agents.verifier import parse_patch
from aeos.core.budget import BudgetTracker
from aeos.core.events import Event
from aeos.core.risk import ApprovalGrant, ApprovalRequired, CallContext
from aeos.core.state import Plan, PlanStep, TaskState
from aeos.llm.base import ToolCall
from aeos.runtime import Runtime
from aeos.store import approvals
from aeos.telemetry import emit_span
from aeos.telemetry import metrics as m

PR_TOOLS = ("create_branch", "commit_patch", "create_pull_request")


def pr_title(state: TaskState) -> str:
    if state.kind == "change":
        t = state.artifacts.pr_title or state.goal.strip().splitlines()[0]
        return t[:120]
    rc = state.artifacts.root_cause
    return ("fix: " + (rc.summary if rc else "performance regression"))[:120]


def pr_body_change(state: TaskState) -> str:
    art, v = state.artifacts, state.verifications[-1]
    research = art.research or {}
    lines = ["## Summary", art.patch_summary or "", "", f"**Request:** {state.goal}", ""]
    files = research.get("relevant_files") or []
    if files:
        lines += ["## Where changes were made / considered"]
        lines += [f"- `{f.get('path')}`: {f.get('why')}" for f in files]
        lines.append("")
    if research.get("risks"):
        lines += ["## Risks"] + [f"- {r}" for r in research["risks"]] + [""]
    lines += ["## Verification (independent)"]
    lines += [f"- {'✓' if ok else '✗'} {g}" for g, ok in v.gates.items()]
    tests = v.metrics.get("tests") or {}
    if tests.get("ran"):
        note = f" ({tests['note']})" if tests.get("note") else ""
        lines.append(
            f"- tests ({tests.get('runner')}): {tests.get('total')} run, "
            f"{tests.get('failed')} failing{note}"
        )
    else:
        lines.append(
            f"- no repository tests were run: {tests.get('note') or 'none could be executed'}; "
            "review the diff and run the app before merging"
        )
    judge = v.judgement or {}
    for c in judge.get("concerns", []):
        lines.append(f"- judge concern: {c}")
    lines += ["", f"_Opened by AEOS task `{state.task_id}`. Not merged; merging needs a human._"]
    return "\n".join(lines)


def pr_body(state: TaskState) -> str:
    if state.kind == "change":
        return pr_body_change(state)
    art = state.artifacts
    v = state.verifications[-1]
    bench = v.metrics.get("benchmark") or {}
    lines = ["## Summary", art.patch_summary or "", ""]
    if art.root_cause:
        rc = art.root_cause
        lines += ["## Root cause", f"`{rc.file}` @ `{rc.commit_sha[:10]}`: {rc.summary}", ""]
        lines += [f"- {e}" for e in rc.evidence]
    lines += ["", "## Verification (independent)"]
    lines += [f"- {'✓' if ok else '✗'} {g}" for g, ok in v.gates.items()]
    if bench:
        b, c = bench["baseline"], bench["candidate"]
        lines += [
            f"- p95 {b['p95_ms']} ms (baseline) → {c['p95_ms']} ms",
            f"- db queries {b['db_queries']} → {c['db_queries']}",
        ]
    lines += ["", f"_Opened by AEOS task `{state.task_id}`. Not merged; merging needs a human._"]
    return "\n".join(lines)


def _publish_step(plan: Plan) -> PlanStep:
    return next(s for s in plan.steps if s.owner == "supervisor")


class PublishNodes:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    def _emit(self, state: TaskState, type_: str, message: str, **data: Any) -> None:
        self.rt.sink.emit(
            Event(type=type_, task_id=state.task_id, agent="supervisor", message=message, data=data)
        )

    def _gh(self, state: TaskState) -> dict[str, ToolDef]:
        """The GitHub tools of the repo this task works on (real target or demo)."""
        return self.rt.target_tools if state.kind == "change" else self.rt.tools["github"]

    def _runtime(self, state: TaskState, step: PlanStep, tracker: BudgetTracker) -> ToolRuntime:
        gh: dict[str, ToolDef] = self._gh(state)
        ctx = ToolContext(state.task_id, step.id, "supervisor", step.attempts)
        runtime = ToolRuntime(
            {k: gh[k] for k in PR_TOOLS}, ctx, self.rt.gate, self.rt.sink, tracker
        )
        files = parse_patch(state.artifacts.patch or "")
        runtime.files_touched = len(files)
        runtime.diff_lines = sum(f.added + f.removed for f in files)
        return runtime

    # ------------------------------------------------------------------ prepare
    async def prepare(self, state: TaskState) -> dict[str, Any]:
        assert state.plan is not None
        plan = state.plan.model_copy(deep=True)
        step = _publish_step(plan)
        step.status, step.attempts = "running", step.attempts + 1
        art = state.artifacts.model_copy(deep=True)
        tracker = BudgetTracker(state.budget)
        runtime = self._runtime(state, step, tracker)
        branch = f"aeos/{state.task_id}-v{plan.version}"

        def fail(message: str) -> dict[str, Any]:
            step.status = "failed"
            self._emit(state, "task_escalated", message)
            return {
                "plans": [*state.plans[:-1], plan], "status": "escalated", "error": message,
                "tool_calls": [*state.tool_calls, *runtime.records],
                "risk_decisions": [*state.risk_decisions, *runtime.decisions],
            }  # fmt: skip

        calls = [
            ("create_branch", {"name": branch, "from_ref": art.base_ref or "main"}),
            (
                "commit_patch",
                {
                    "branch": branch,
                    "patch": art.patch,
                    "message": f"{pr_title(state)}\n\nAEOS task {state.task_id}",
                },
            ),  # fmt: skip
        ]
        for i, (name, args) in enumerate(calls):
            try:
                await runtime.execute(ToolCall(id=f"pub-{step.id}-{i}", name=name, arguments=args))
            except ApprovalRequired as exc:
                return fail(f"{exc} (approval for this step type is not supported)")
            if runtime.records and runtime.records[-1].is_error:
                return fail(f"{name} failed: {runtime.records[-1].result_preview}")
            if runtime.decisions[-1].decision == "deny":
                return fail(f"{name} denied by policy: {runtime.decisions[-1].reason}")
        art.branch = branch

        base = (self.rt.target_branch or "main") if state.kind == "change" else "main"
        pr_args = {"title": pr_title(state), "body": pr_body(state), "head": branch, "base": base}
        pr_tool = self._gh(state)["create_pull_request"]
        decision = self.rt.gate.decide(
            CallContext(
                tool="create_pull_request",
                risk=pr_tool.risk,
                server="github",
                arguments=pr_args,
                step_id=step.id,
                diff_lines=runtime.diff_lines,
                files_touched=runtime.files_touched,
            )
        )
        runtime.decisions.append(decision)
        update: dict[str, Any] = {
            "plans": [*state.plans[:-1], plan], "artifacts": art, "budget": tracker.budget,
            "tool_calls": [*state.tool_calls, *runtime.records],
            "risk_decisions": [*state.risk_decisions, *runtime.decisions],
        }  # fmt: skip
        if decision.decision == "deny":
            return {**update, **fail(f"pull request denied by policy: {decision.reason}"),
                    "artifacts": art}  # fmt: skip
        if decision.decision == "allow":
            return {**update, "approval": {"status": "not_required", "arguments": pr_args}}

        v = state.verifications[-1]
        req = await asyncio.to_thread(
            lambda: approvals.create(
                task_id=state.task_id,
                action_key=f"open_pr:{step.id}:{step.attempts}",
                tool="create_pull_request",
                server="github",
                arguments=pr_args,
                rationale=(
                    f"Open a pull request with the verified fix for: {state.goal}\n"
                    f"Policy: {decision.policy_rule} ({decision.reason})"
                ),
                ttl_s=self.rt.settings.approval_ttl_s,
                evidence={
                    "root_cause": art.root_cause.model_dump() if art.root_cause else None,
                    "research": art.research if state.kind == "change" else None,
                    "gates": v.gates,
                    "issues": [i.model_dump() for i in v.issues],
                    "tests": v.metrics.get("tests"),
                    "benchmark": v.metrics.get("benchmark"),
                    "judgement": v.judgement,
                    "files": v.metrics.get("files"),
                    "patch": art.patch,
                    "branch": branch,
                },
            )
        )
        self._emit(
            state, "approval_requested",
            f"approval {req.id} needed to open a PR from {branch} (expires {req.expires_at:%H:%M})",
            approval_id=req.id,
        )  # fmt: skip
        return {**update, "pending_approval_id": req.id, "approval": None,
                "status": "awaiting_approval"}  # fmt: skip

    # ------------------------------------------------------------------ await
    async def await_decision(self, state: TaskState) -> dict[str, Any]:
        assert state.plan is not None and state.pending_approval_id
        # Nothing expensive may run before interrupt(): the node is re-entered on resume.
        decision: dict[str, Any] = interrupt(
            {"approval_id": state.pending_approval_id, "action": "create_pull_request"}
        )
        kind = decision["decision"]
        req = await asyncio.to_thread(approvals.get, state.pending_approval_id)
        if (
            req is not None
        ):  # the wait may have spanned a restart: record it with real wall-clock bounds
            emit_span(
                "approval.wait", req.created_at.timestamp(),
                (req.decided_at or req.expires_at).timestamp(), parent=state.trace,
                **{"aeos.approval.id": req.id, "aeos.approval.decision": kind,
                   "aeos.approval.actor": decision.get("actor"), "aeos.approval.tool": req.tool},
            )  # fmt: skip
        m.approvals.add(1, {"decision": kind})
        approval = {
            "id": state.pending_approval_id, "status": kind,
            "actor": decision.get("actor"), "feedback": decision.get("feedback", ""),
        }  # fmt: skip
        plan = state.plan.model_copy(deep=True)
        self._emit(state, "approval_decided", f"approval {kind} by {approval['actor']}", **approval)
        if kind == "approved":
            return {"approval": approval, "status": "running"}
        if kind == "rejected":
            _publish_step(plan).status = "failed"
            return {
                "approval": approval, "status": "running", "plans": [*state.plans[:-1], plan],
                "human_feedback": [*state.human_feedback, approval],
            }  # fmt: skip
        _publish_step(plan).status = "failed"
        return {
            "approval": approval, "plans": [*state.plans[:-1], plan], "status": "escalated",
            "error": "approval expired before a human decided; nothing was published",
        }  # fmt: skip

    # ------------------------------------------------------------------ create PR
    async def create_pr(self, state: TaskState) -> dict[str, Any]:
        assert state.plan is not None and state.approval is not None
        plan = state.plan.model_copy(deep=True)
        step = _publish_step(plan)
        tracker = BudgetTracker(state.budget)
        runtime = self._runtime(state, step, tracker)
        if state.approval["status"] == "approved":
            req = await asyncio.to_thread(approvals.get, state.approval["id"])
            assert req is not None and req.status == "approved"
            args = req.arguments
            # Bound to the hash recorded when the human saw the request: if the stored arguments
            # were altered afterwards, the grant no longer matches and the policy blocks the call.
            runtime.grants = [
                ApprovalGrant(approval_id=req.id, tool=req.tool, arguments_hash=req.arguments_hash)
            ]
        else:  # policy allowed it without approval
            args = state.approval["arguments"]
        art = state.artifacts.model_copy(deep=True)

        def escalate(message: str) -> dict[str, Any]:
            step.status = "failed"
            self._emit(state, "task_escalated", message)
            return {
                "plans": [*state.plans[:-1], plan], "status": "escalated", "error": message,
                "tool_calls": [*state.tool_calls, *runtime.records],
                "risk_decisions": [*state.risk_decisions, *runtime.decisions],
            }  # fmt: skip

        try:
            await runtime.execute(
                ToolCall(id=f"pub-{step.id}-pr", name="create_pull_request", arguments=args)
            )
        except ApprovalRequired as exc:
            return escalate(f"pull request blocked: {exc}")
        rec = runtime.records[-1] if runtime.records else None
        if rec is None or rec.is_error or runtime.last_output is None:
            return escalate(
                f"create_pull_request failed: {rec.result_preview if rec else 'denied'}"
            )
        data = runtime.last_output.data
        art.pull_request = (
            data if isinstance(data, dict) else json.loads(runtime.last_output.content)
        )
        step.status = "done"
        await asyncio.to_thread(
            approvals.audit, state.task_id, "system", "pr_created",
            {"number": art.pull_request.get("number"), "url": art.pull_request.get("url")},
            f"pr_created:{state.task_id}:{step.id}:{step.attempts}",  # replay-safe
        )  # fmt: skip
        self._emit(state, "pr_created", f"pull request #{art.pull_request.get('number')} created "
                   f"({art.pull_request.get('url')})", pull_request=art.pull_request)  # fmt: skip
        return {
            "plans": [*state.plans[:-1], plan], "artifacts": art, "budget": tracker.budget,
            "tool_calls": [*state.tool_calls, *runtime.records],
            "risk_decisions": [*state.risk_decisions, *runtime.decisions], "status": "running",
        }  # fmt: skip

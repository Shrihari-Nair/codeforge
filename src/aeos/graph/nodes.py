"""Graph nodes. Each returns only the TaskState fields it changed."""

import asyncio
import json
import time
from typing import Any

from aeos.agents.judge import apply_judgement, judge_patch
from aeos.agents.specialists import SpecialistRequest, SpecialistResult
from aeos.agents.supervisor import create_plan, replan, synthesize_root_cause
from aeos.agents.verifier import run_gates
from aeos.agents.verifier_change import run_change_gates
from aeos.core.budget import BudgetTracker
from aeos.core.events import Event
from aeos.core.state import Observation, Plan, TaskState
from aeos.memory.manager import MemoryContext
from aeos.runtime import Runtime
from aeos.sandbox import RepoRef
from aeos.telemetry import annotate, inject_context, traced
from aeos.telemetry import metrics as m


class GraphNodes:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    def _emit(
        self, state: TaskState, type_: str, message: str, agent: str = "supervisor", **data: Any
    ) -> None:
        self.rt.sink.emit(
            Event(type=type_, task_id=state.task_id, agent=agent, message=message, data=data)
        )

    # ------------------------------------------------------------------ plan
    async def plan(self, state: TaskState) -> dict[str, Any]:
        with traced(
            "task",
            attributes={"langfuse.trace.name": "aeos task", "aeos.task.goal": state.goal},
        ):
            carrier = inject_context()
            update = await self._plan(state)
        return {**update, "trace": carrier}

    async def _plan(self, state: TaskState) -> dict[str, Any]:
        tracker = BudgetTracker(state.budget)
        recalled = MemoryContext()
        if self.rt.memory is not None:
            try:  # memory must never break a task
                recalled = await self.rt.memory.recall(state.goal)
            except Exception as exc:
                self._emit(state, "memory_error", f"recall failed: {exc}")
            if recalled.retrieved:
                self._emit(
                    state, "memory_recalled",
                    f"recalled {len(recalled.retrieved)} memory item(s) for planning",
                    items=recalled.retrieved,
                )  # fmt: skip
        if state.kind == "change":
            try:  # always plan against the repo's current default branch
                sha = await asyncio.to_thread(self.rt.refresh_target)
            except Exception as exc:
                msg = f"cannot reach the target repository: {type(exc).__name__}: {exc}"
                self._emit(state, "task_failed", msg)
                return {"status": "failed", "error": msg, "budget": tracker.budget}
            self._emit(state, "target_synced", f"target repo synced at {sha[:10]}")
        plan, note = await create_plan(
            self.rt.strong_llm, state.goal, tracker, recalled.text, kind=state.kind
        )
        self._emit(
            state, "plan_created",
            f"plan v{plan.version}: " + " | ".join(f"{s.id}:{s.owner}" for s in plan.steps),
            note=note,
        )  # fmt: skip
        return {
            "plans": [plan], "status": "running", "budget": tracker.budget,
            "memory_text": recalled.text, "memory_trace": {"retrieved": recalled.retrieved},
        }  # fmt: skip

    # ------------------------------------------------------------------ dispatch
    def _context(self, state: TaskState, owner: str) -> dict[str, Any]:
        art = state.artifacts
        ctx: dict[str, Any] = {}
        failed = [v for v in state.verifications if v.status == "failed"]
        if failed:  # a previous attempt was rejected by the verifier: every agent must know why
            last = failed[-1]
            ctx["previous_attempt_feedback"] = {
                "failed_verifications": len(failed),
                "issues": [i.model_dump() for i in last.issues],
                "suggested_focus": last.suggested_focus,
                "previous_patch_summary": art.patch_summary,
                "benchmark": last.metrics.get("benchmark"),
            }
            ctx["rejected_diagnoses"] = [r.model_dump() for r in art.rejected_root_causes]
        if state.human_feedback:
            ctx["human_feedback"] = state.human_feedback
        if state.memory_text:
            ctx["memory_hints"] = state.memory_text
        if owner == "coding" and state.kind == "change":
            ctx["research"] = art.research
        elif owner == "coding":
            ctx.update(
                root_cause=art.root_cause.model_dump() if art.root_cause else None,
                analytics=art.analytics,
                research=art.research,
                baseline_ref=art.baseline_ref,
            )
        return ctx

    async def dispatch(self, state: TaskState) -> dict[str, Any]:
        assert state.plan is not None
        plan: Plan = state.plan.model_copy(deep=True)
        specialists = self.rt.change_specialists if state.kind == "change" else self.rt.specialists
        ready = [s for s in plan.ready_steps() if s.owner in specialists]
        if not ready:
            return {
                "status": "failed",
                "error": "no runnable steps",
                "plans": [*state.plans[:-1], plan],
            }
        tracker = BudgetTracker(state.budget)
        for step in ready:
            step.status, step.attempts = "running", step.attempts + 1
            self._emit(state, "step_started", f"{step.id} → {step.owner}: {step.description}")

        async def run_one(step: Any) -> SpecialistResult:
            req = SpecialistRequest(
                task_id=state.task_id, step_id=step.id, attempt=step.attempts,
                goal=step.description, context=self._context(state, step.owner),
            )  # fmt: skip
            try:
                return await specialists[step.owner].run(req, tracker, self.rt.sink)
            except Exception as exc:  # a specialist crash must not take down the run
                return SpecialistResult(
                    owner=step.owner, status="error", detail=f"{type(exc).__name__}: {exc}"
                )

        results = await asyncio.gather(*(run_one(s) for s in ready))

        artifacts = state.artifacts.model_copy(deep=True)
        observations = list(state.observations)
        tool_calls, decisions = list(state.tool_calls), list(state.risk_decisions)
        status, error = state.status, state.error
        for step, res in zip(ready, results, strict=True):
            tool_calls += res.tool_calls
            decisions += res.risk_decisions
            if (
                state.kind == "regression"
                and res.owner == "research"
                and res.status == "finished"
                and res.output is not None
            ):
                res = self._check_research(state, res)
            if res.status == "finished" and res.output is not None:
                step.status = "done"
                untrusted = any(t.origin == "untrusted" for t in res.tool_calls)
                observations.append(
                    Observation(
                        id=f"obs-{step.id}-{step.attempts}", step_id=step.id, agent=res.owner,
                        summary=str(res.output.get("summary", ""))[:500], data=res.output,
                        origin="untrusted" if untrusted else "trusted",
                    )
                )  # fmt: skip
                if res.owner == "analytics":
                    artifacts.analytics = res.output
                elif res.owner == "research":
                    artifacts.research = res.output
                    if state.kind == "regression":
                        artifacts.baseline_ref = res.output.get("last_good_commit_sha")
                elif res.owner == "coding":
                    artifacts.patch = res.artifacts.get("patch") or ""
                    artifacts.patch_attempt = step.attempts
                    artifacts.patch_summary = res.output.get("summary")
                    artifacts.pr_title = res.output.get("pr_title") or None
                    artifacts.base_ref = res.artifacts.get("base_ref")
                self._emit(state, "step_done", f"{step.id} ({res.owner}) done", agent=res.owner)
                continue
            # failure: budget exhaustion is never retried; other failures follow the retry policy
            action = (
                "escalate"
                if res.status == "budget"
                else self.rt.retry.on_step_failure(step.attempts)
            )
            if action == "retry":
                step.status = "pending"
                m.step_retries.add(1, {"owner": res.owner})
                self._emit(
                    state, "step_retry",
                    f"{step.id} ({res.owner}) {res.status}: {res.detail}; retrying "
                    f"(attempt {step.attempts + 1}/{self.rt.retry.max_step_attempts})",
                    agent=res.owner,
                )  # fmt: skip
            else:
                step.status = "failed"
                status = "escalated"
                error = f"{res.owner} {res.status}: {res.detail}; needs human attention"
                self._emit(state, "step_failed", error, agent=res.owner)
        return {
            "plans": [*state.plans[:-1], plan], "artifacts": artifacts, "observations": observations,
            "tool_calls": tool_calls, "risk_decisions": decisions, "budget": tracker.budget,
            "status": status, "error": error,
        }  # fmt: skip

    def _check_research(self, state: TaskState, res: SpecialistResult) -> SpecialistResult:
        """Hard facts are decided by code, not by the model.

        The suspect must be a real commit, and the baseline is its nearest ancestor that can be
        benchmarked (models confuse "parent commit" with "previous commit touching the file", which
        can predate the benchmark entirely).
        """
        output = dict(res.output or {})
        suspect = str(output.get("suspected_commit_sha", ""))
        try:
            suspect_sha = self.rt.resolve_ref(suspect)
            parent_sha = self.rt.baseline_for(suspect_sha)
        except Exception as exc:
            return SpecialistResult(
                owner=res.owner, status="error", tool_calls=res.tool_calls,
                detail=f"suspected commit {suspect[:12]!r} is not a commit in this repo ({exc})",
            )  # fmt: skip
        claimed = output.get("last_good_commit_sha")
        if claimed != parent_sha:
            self._emit(
                state, "baseline_corrected",
                f"baseline set to the parent of {suspect_sha[:10]} ({parent_sha[:10]}); "
                f"the agent claimed {str(claimed)[:10]}",
                agent="research",
            )  # fmt: skip
        output["suspected_commit_sha"], output["last_good_commit_sha"] = suspect_sha, parent_sha
        return res.model_copy(update={"output": output})

    # ------------------------------------------------------------------ synthesize
    async def synthesize(self, state: TaskState) -> dict[str, Any]:
        tracker = BudgetTracker(state.budget)
        art = state.artifacts.model_copy(deep=True)
        try:
            rc = await synthesize_root_cause(
                self.rt.strong_llm,
                state.goal,
                art.analytics or {},
                art.research or {},
                tracker,
                art.rejected_root_causes,
            )
        except Exception as exc:
            self._emit(state, "task_failed", f"root-cause synthesis failed: {exc}")
            return {
                "status": "failed",
                "error": f"synthesis failed: {exc}",
                "budget": tracker.budget,
            }
        art.root_cause = rc
        art.baseline_ref = art.baseline_ref or None
        self._emit(
            state, "root_cause", f"root cause: {rc.file} @ {rc.commit_sha[:10]} (confidence {rc.confidence:.0%})",
            summary=rc.summary,
        )  # fmt: skip
        if not art.baseline_ref:
            return {"status": "escalated", "error": "research did not provide a last-good commit",
                    "artifacts": art, "budget": tracker.budget}  # fmt: skip
        return {"artifacts": art, "budget": tracker.budget}

    # ------------------------------------------------------------------ verify
    async def verify(self, state: TaskState) -> dict[str, Any]:
        assert state.plan is not None
        art = state.artifacts
        plan = state.plan.model_copy(deep=True)
        step = next(s for s in plan.steps if s.owner == "verifier" and s.status == "pending")
        step.status, step.attempts = "running", step.attempts + 1
        tracker = BudgetTracker(state.budget)
        attempt = len(state.verifications) + 1
        if state.kind == "change":
            gates = await run_change_gates(
                self.rt.sandbox,
                base=RepoRef(remote=self.rt.target_remote_path or "", ref=art.base_ref or "main"),
                patch=art.patch, attempt=attempt, task_id=state.task_id,
            )  # fmt: skip
        else:
            gates = await run_gates(
                self.rt.sandbox,
                base=RepoRef(remote=self.rt.remote_path, ref=art.base_ref or "main"),
                baseline=RepoRef(remote=self.rt.remote_path, ref=art.baseline_ref or "main"),
                patch=art.patch, cfg=self.rt.gate_cfg, attempt=attempt, task_id=state.task_id,
            )  # fmt: skip
        # LLM judgement is advisory: it can only make the verdict stricter, never override a gate.
        judgement = None
        if art.patch and art.patch.strip():
            judgement = await judge_patch(
                self.rt.strong_llm, goal=state.goal, root_cause=art.root_cause,
                patch=art.patch, gates=gates, tracker=tracker, kind=state.kind,
            )  # fmt: skip
        result = apply_judgement(gates, judgement, state.kind)
        step.status = "done" if result.status == "passed" else "failed"
        m.verifications.add(1, {"status": result.status})
        annotate(**{"aeos.verification.status": result.status, "aeos.verification.attempt": attempt,
                    "aeos.verification.gates": result.gates,
                    "aeos.verification.issues": [i.type for i in result.issues]})  # fmt: skip
        self._emit(
            state, "verification",
            f"verifier {result.status}: " + (", ".join(i.type for i in result.issues) or "all gates passed"),
            agent="verifier", gates=result.gates, attempt=attempt,
        )  # fmt: skip
        return {
            "plans": [*state.plans[:-1], plan],
            "verifications": [*state.verifications, result],
            "budget": tracker.budget,
        }

    # ------------------------------------------------------------------ replan
    async def replan(self, state: TaskState) -> dict[str, Any]:
        assert state.plan is not None
        failures = sum(1 for v in state.verifications if v.status == "failed") + len(
            state.human_feedback
        )
        last = state.verifications[-1]
        action = self.rt.retry.on_verification_failure(failures)
        rejected = bool(state.approval and state.approval.get("status") == "rejected")
        if action == "escalate":
            detail = "; ".join(f"{i.type}: {i.detail}" for i in last.issues)
            if rejected:
                detail = f"human rejected the pull request: {state.approval['feedback']}"  # type: ignore[index]
            msg = f"escalated to a human after {failures} failed attempts ({detail})"
            self._emit(state, "task_escalated", msg)
            return {"status": "escalated", "error": msg}
        art = state.artifacts
        version = state.plan.version + 1
        summary = {
            "goal": state.goal,
            "failed_verifications": failures,
            "previous_plan": [
                {"id": s.id, "owner": s.owner, "description": s.description, "status": s.status}
                for s in state.plan.steps
            ],
            "diagnosis_that_was_acted_on": art.root_cause.model_dump() if art.root_cause else None,
            "patch_summary": art.patch_summary,
            "human_rejection": state.human_feedback[-1] if rejected else None,
            "verifier_issues": [i.model_dump() for i in last.issues],
            "verifier_suggested_focus": last.suggested_focus,
            "verifier_benchmark": last.metrics.get("benchmark"),
            "judge": last.judgement,
        }
        tracker = BudgetTracker(state.budget)
        plan = await replan(
            self.rt.strong_llm, summary=summary, version=version,
            require_investigation=action == "replan_new_strategy", tracker=tracker, kind=state.kind,
        )  # fmt: skip
        m.replans.add(1, {"strategy_change": str(action == "replan_new_strategy")})
        annotate(**{"aeos.plan.version": version, "aeos.plan.reason": plan.reason_for_change,
                    "aeos.replan.action": action})  # fmt: skip
        new = art.model_copy(deep=True)
        new.branch, new.pull_request = None, None  # the next publication starts clean
        if any(s.owner in ("analytics", "research") for s in plan.steps):
            if art.root_cause:  # the diagnosis is being re-opened: remember it was wrong
                new.rejected_root_causes = [*art.rejected_root_causes, art.root_cause]
            new.root_cause = None
        self._emit(
            state, "replan",
            f"plan v{version} ({plan.reason_for_change}): "
            + " | ".join(f"{s.id}:{s.owner}" for s in plan.steps),
            reason=plan.reason_for_change, version=version,
        )  # fmt: skip
        return {
            "plans": [*state.plans, plan], "artifacts": new, "budget": tracker.budget,
            "approval": None, "pending_approval_id": None, "status": "running",
        }  # fmt: skip

    # ------------------------------------------------------------------ finish
    async def finish(self, state: TaskState) -> dict[str, Any]:
        art = state.artifacts.model_copy(deep=True)
        status = state.status
        if status == "running":
            ok = bool(state.verifications) and state.verifications[-1].status == "passed"
            ok = ok and art.pull_request is not None
            status = "completed" if ok else "failed"
        art.report = build_report(state, status)
        started = state.budget.started_at
        m.tasks_finished.add(1, {"status": status})
        if started:
            m.task_duration.record(time.time() - started, {"status": status})
        annotate(**{"aeos.task.status": status, "aeos.task.llm_calls": state.budget.llm_calls,
                    "aeos.task.tool_calls": state.budget.tool_calls,
                    "aeos.task.cost_usd": round(state.budget.cost_usd, 5),
                    "aeos.task.plans": len(state.plans)})  # fmt: skip
        self._emit(
            state, "task_completed" if status == "completed" else "task_failed",
            f"task {status}" + (f": {state.error}" if state.error else ""),
        )  # fmt: skip
        budget = state.budget.model_copy(update={"finished_at": time.time()})
        return {"status": status, "artifacts": art, "budget": budget}

    # ------------------------------------------------------------------ reflect
    async def reflect(self, state: TaskState) -> dict[str, Any]:
        """Write the episode and (gated) facts. Runs after finish; can never fail the task."""
        mem = self.rt.memory
        if mem is None or state.kind == "change":  # memory only models regression episodes
            return {}
        tracker = BudgetTracker(state.budget)
        try:
            trace = await mem.reflect(state, self.rt.strong_llm, tracker)
        except Exception as exc:
            trace = {"error": f"{type(exc).__name__}: {exc}", "accepted": [], "rejected": []}
        rc = state.artifacts.root_cause
        trace["retrieved"] = state.memory_trace.get("retrieved", [])
        trace["used"] = bool(rc and trace["retrieved"] and rc.file in (state.memory_text or ""))
        self._emit(
            state, "memory_written",
            f"memory: episode {'saved' if trace.get('episode') else 'exists'}, "
            f"{len(trace.get('accepted', []))} fact(s) stored, {len(trace.get('rejected', []))} rejected",
            **{k: v for k, v in trace.items() if k in ("accepted", "rejected", "used")},
        )  # fmt: skip
        return {"memory_trace": trace, "budget": tracker.budget}


def build_report(state: TaskState, status: str) -> str:
    art, b = state.artifacts, state.budget
    lines = [f"# Task {state.task_id}: {status}", "", f"**Goal:** {state.goal}", ""]
    if art.root_cause:
        rc = art.root_cause
        lines += ["## Root cause", f"- `{rc.file}` in commit `{rc.commit_sha}` (confidence {rc.confidence:.0%})",
                  f"- {rc.summary}", *(f"- evidence: {e}" for e in rc.evidence), ""]  # fmt: skip
    if state.plans:
        lines += ["## Plan history"]
        for p in state.plans:
            lines.append(f"- v{p.version}" + (f" ({p.reason_for_change})" if p.reason_for_change else "")
                         + ": " + ", ".join(f"{s.id}:{s.owner}={s.status}" for s in p.steps))  # fmt: skip
        lines.append("")
    if state.verifications:
        v = state.verifications[-1]
        lines += [f"## Verification (attempt {v.attempt}): {v.status}"]
        lines += [f"- {'✓' if ok else '✗'} {gate}" for gate, ok in v.gates.items()]
        lines += [f"- issue `{i.type}`: {i.detail}" for i in v.issues]
        bench = v.metrics.get("benchmark")
        if isinstance(bench, dict):
            lines.append(
                f"- benchmark: {json.dumps(bench.get('baseline'))} → {json.dumps(bench.get('candidate'))}"
            )
        lines.append("")
    if art.pull_request:
        pr = art.pull_request
        lines += [
            "## Pull request",
            f"- #{pr.get('number')} {pr.get('url')} (open, NOT merged)",
            "",
        ]
    if state.human_feedback:
        lines += (
            ["## Human feedback"]
            + [f"- {h.get('actor')}: {h.get('feedback')}" for h in state.human_feedback]
            + [""]
        )
    if art.patch:
        lines += ["## Patch", "```diff", art.patch.rstrip(), "```", ""]
    if state.error:
        lines += [f"**Error:** {state.error}", ""]
    lines.append(
        f"_Budget: {b.llm_calls} LLM calls, {b.tool_calls} tool calls, {b.tokens_used} tokens, ~${b.cost_usd:.3f}_"
    )
    return "\n".join(lines)

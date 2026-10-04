"""Supervisor: plans (LLM, with deterministic validation) and synthesises the root cause."""

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from aeos.core.budget import BudgetTracker
from aeos.core.state import Plan, PlanStep, RootCause
from aeos.llm.base import LLMError, LLMProvider
from aeos.llm.structured import complete_structured

PLANNER_SYSTEM = (
    "ROLE: supervisor-planner. You decompose an engineering task into a plan executed by "
    "specialist agents. Available owners:\n"
    "- analytics: queries logs, metrics, traces and the database (read-only) to find WHEN and WHAT "
    "changed.\n"
    "- research: reads source code, commit history and diffs (and may consult web docs) to find "
    "the commit/code responsible.\n"
    "- coding: works in an isolated sandbox: edits code, runs tests and benchmarks, produces a "
    "patch.\n"
    "A deterministic verifier step is appended automatically after coding; do not include it.\n"
    "Rules: give each step a short id (s1, s2, ...), a one-sentence description, an owner, and "
    "depends_on (ids). Steps with no dependency between them run in parallel. Coding must depend "
    "on the investigation steps. Keep the plan minimal (usually 3 steps).\n"
    "If MEMORY hints are present they come from past tasks: use them to focus the plan (for example "
    "ask the investigators to check a previously found cause first) but never to skip verification.\n"
    "Call `submit` with the plan."
)

PLANNER_SYSTEM_CHANGE = (
    "ROLE: supervisor-planner. You decompose a code CHANGE REQUEST on a real repository (for "
    "example a dependency upgrade, a refactor or a bug fix) into a plan executed by specialist "
    "agents. Available owners:\n"
    "- research: reads the repository (files, search, history) to find everything the change "
    "touches and to assess risks. There is no telemetry or database for this repository: do NOT "
    "use analytics.\n"
    "- coding: works in an isolated sandbox: edits code, can update npm dependencies "
    "(package.json + lockfile), checks syntax, produces the patch.\n"
    "A deterministic verifier step and the pull-request step are appended automatically; do not "
    "include them.\n"
    "Rules: short step ids (s1, s2, ...), a one-sentence description, an owner, depends_on. "
    "Coding must depend on research. Usually exactly two steps: s1 research, s2 coding. "
    "If MEMORY hints are present use them as hints only. Call `submit` with the plan."
)

SYNTH_SYSTEM = (
    "ROLE: supervisor-synthesis. You are given findings from the analytics and research agents "
    "about a latency regression. Decide the single most likely root cause. Prefer explanations "
    "supported by BOTH agents. A problem present at the same rate before and after the regression "
    "is NOT the cause. Use the full 40-char commit sha from the research findings. Call `submit`."
)


REPLAN_SYSTEM_CHANGE = (
    "ROLE: supervisor-replan. A patch for a code change request FAILED independent verification "
    "(or a human rejected the pull request). Read the verifier's issues / the human's feedback and "
    "produce a new plan that fixes exactly that. Owners: research (re-check what must change) and "
    "coding. No analytics. Give a one-sentence `reason`. Use new unique step ids (s4, s5, ...). A "
    "verifier step is appended automatically. Call `submit`."
)

REPLAN_SYSTEM = (
    "ROLE: supervisor-replan. A patch FAILED independent verification. You revise the plan: do not "
    "just retry the same step. Read the verifier's issues and suggested focus, decide what was "
    "wrong (usually the diagnosis, sometimes only the implementation), and produce a new plan "
    "that addresses it. Owners: analytics, research (re-investigate), coding. Give a one-sentence "
    "`reason` that explains what changed and why. Use short unique step ids that do not reuse "
    "earlier ones (e.g. s4, s5). A verifier step is appended automatically. Call `submit`."
)


class PlanStepSpec(BaseModel):
    id: str
    description: str
    owner: Literal["analytics", "research", "coding"]
    depends_on: list[str] = Field(default_factory=list)


class PlanSpec(BaseModel):
    rationale: str
    steps: list[PlanStepSpec]


class ReplanSpec(BaseModel):
    reason: str
    steps: list[PlanStepSpec]


def default_plan_steps(kind: str = "regression") -> list[PlanStep]:
    if kind == "change":
        return [
            PlanStep(
                id="s1",
                owner="research",
                description="Find every file the requested change touches and assess the risks.",
            ),  # fmt: skip
            PlanStep(
                id="s2",
                owner="coding",
                depends_on=["s1"],
                description="Implement the change in the sandbox and check what can be checked.",
            ),  # fmt: skip
        ]
    return [
        PlanStep(
            id="s1",
            owner="analytics",
            description="Find when and why latency regressed after the latest deployment.",
        ),  # fmt: skip
        PlanStep(
            id="s2",
            owner="research",
            description="Inspect recent commits and code to find the change responsible.",
        ),  # fmt: skip
        PlanStep(
            id="s3",
            owner="coding",
            depends_on=["s1", "s2"],
            description="Implement a fix in the sandbox and verify tests and benchmark.",
        ),  # fmt: skip
    ]


def validate_steps(steps: list[PlanStepSpec], kind: str = "regression") -> list[str]:
    errors: list[str] = []
    if kind == "change" and any(s.owner == "analytics" for s in steps):
        errors.append("analytics is not available for change requests")
    ids = [s.id for s in steps]
    if not steps:
        return ["plan has no steps"]
    if len(set(ids)) != len(ids):
        errors.append("step ids must be unique")
    for s in steps:
        for d in s.depends_on:
            if d not in ids:
                errors.append(f"{s.id} depends on unknown step {d}")
            if d == s.id:
                errors.append(f"{s.id} depends on itself")
    if not any(s.owner == "coding" for s in steps):
        errors.append("plan needs a coding step")
    # cycle check (Kahn)
    remaining = {s.id: set(s.depends_on) for s in steps}
    while remaining:
        ready = [i for i, deps in remaining.items() if not (deps & remaining.keys())]
        if not ready:
            errors.append("dependency cycle")
            break
        for i in ready:
            del remaining[i]
    return errors


def finalize_plan(steps: list[PlanStep], version: int, reason: str | None) -> Plan:
    """Append the deterministic tail: a verifier gating every coding step, then publication."""
    coding = [s.id for s in steps if s.owner == "coding"]
    verify_id = f"v{version}"
    steps = [
        *steps,
        PlanStep(
            id=verify_id,
            owner="verifier",
            depends_on=coding,
            description="Independently verify the patch (tests, benchmark, safety gates).",
        ),
        PlanStep(
            id=f"p{version}",
            owner="supervisor",
            depends_on=[verify_id],
            description="Publish the verified patch as a pull request (needs human approval).",
        ),
    ]
    return Plan(version=version, steps=steps, reason_for_change=reason)


async def create_plan(
    llm: LLMProvider,
    goal: str,
    tracker: BudgetTracker,
    memory_text: str = "",
    kind: str = "regression",
) -> tuple[Plan, str]:
    """Returns (plan v1, rationale). Falls back to the default plan if the LLM plan is invalid."""
    try:
        spec, usage = await complete_structured(
            llm,
            system=PLANNER_SYSTEM_CHANGE if kind == "change" else PLANNER_SYSTEM,
            user=f"Task: {goal}" + (f"\n\n{memory_text}" if memory_text else ""),
            model=PlanSpec,
        )
        tracker.add_llm(usage, getattr(llm, "model", ""))
        errors = validate_steps(spec.steps, kind)
        if not errors:
            steps = [PlanStep(**s.model_dump()) for s in spec.steps]
            return finalize_plan(steps, 1, None), spec.rationale
        reason = "LLM plan invalid (" + "; ".join(errors) + "); used default plan"
    except LLMError as exc:
        reason = f"planner failed ({exc}); used default plan"
    return finalize_plan(default_plan_steps(kind), 1, None), reason


async def synthesize_root_cause(
    llm: LLMProvider,
    goal: str,
    analytics: dict[str, Any],
    research: dict[str, Any],
    tracker: BudgetTracker,
    rejected: list[RootCause] | None = None,
) -> RootCause:
    user = (
        f"Task: {goal}\n\nANALYTICS FINDINGS:\n{json.dumps(analytics, indent=2, default=str)}\n\n"
        f"RESEARCH FINDINGS:\n{json.dumps(research, indent=2, default=str)}"
    )
    if rejected:
        user += (
            "\n\nPREVIOUS DIAGNOSES PROVEN WRONG BY THE VERIFIER (a fix based on them did not "
            "restore performance; do not choose them again):\n"
            + json.dumps([r.model_dump() for r in rejected], indent=2, default=str)
        )
    rc, usage = await complete_structured(llm, system=SYNTH_SYSTEM, user=user, model=RootCause)
    tracker.add_llm(usage, getattr(llm, "model", ""))
    return rc


def fallback_replan_steps(
    version: int, require_investigation: bool, kind: str = "regression"
) -> list[PlanStep]:
    base = 3 * version  # unique ids per version
    coding = PlanStep(
        id=f"s{base + 2}",
        owner="coding",
        description="Implement a corrected fix using the verifier feedback; benchmark against baseline.",
    )
    if not require_investigation:
        return [coding]
    research = PlanStep(
        id=f"s{base + 1}",
        owner="research",
        description="Re-investigate: the previous diagnosis was rejected by the verifier; find the real cause.",
    )
    coding.depends_on = [research.id]
    return [research, coding]


async def replan(
    llm: LLMProvider,
    *,
    summary: dict[str, Any],
    version: int,
    require_investigation: bool,
    tracker: BudgetTracker,
    kind: str = "regression",
) -> Plan:
    """Plan v{version}. The reason for the change is always recorded on the plan."""
    note = ""
    try:
        spec, usage = await complete_structured(
            llm,
            system=REPLAN_SYSTEM_CHANGE if kind == "change" else REPLAN_SYSTEM,
            model=ReplanSpec,
            user=json.dumps(
                {**summary, "must_reinvestigate": require_investigation}, indent=2, default=str
            ),
        )
        tracker.add_llm(usage, getattr(llm, "model", ""))
        errors = validate_steps(spec.steps, kind)
        if require_investigation and not any(
            s.owner in ("analytics", "research") for s in spec.steps
        ):
            errors.append("a re-investigation step (analytics or research) is required")
        if not errors:
            steps = [PlanStep(**s.model_dump()) for s in spec.steps]
            return finalize_plan(steps, version, spec.reason)
        note = "; ".join(errors)
    except LLMError as exc:
        note = str(exc)
    issues = "; ".join(f"{i['type']}: {i['detail']}" for i in summary.get("verifier_issues", []))
    reason = f"verifier rejected the patch ({issues}); deterministic replan ({note})"
    return finalize_plan(
        fallback_replan_steps(version, require_investigation, kind), version, reason
    )

"""Task state: the single, explicit, serializable record of a run.

Everything needed to resume after a crash lives here (never in process memory): plan history,
observations (with trust origin), tool-call log (with idempotency keys), artifacts (including the
latest patch, from which a sandbox can be rebuilt), verification results, risk decisions, budget.
"""

import time
from typing import Any, Literal

from pydantic import BaseModel, Field

Status = Literal[
    "pending", "running", "awaiting_approval", "completed", "failed", "escalated", "cancelled"
]
Origin = Literal["trusted", "untrusted"]
Owner = Literal["research", "analytics", "coding", "verifier", "supervisor"]
StepStatus = Literal["pending", "running", "done", "failed", "skipped"]


class PlanStep(BaseModel):
    id: str
    description: str
    owner: Owner
    depends_on: list[str] = Field(default_factory=list)
    status: StepStatus = "pending"
    attempts: int = 0


class Plan(BaseModel):
    version: int
    steps: list[PlanStep]
    reason_for_change: str | None = None  # populated on replan

    def step(self, step_id: str) -> PlanStep:
        return next(s for s in self.steps if s.id == step_id)

    def ready_steps(self) -> list[PlanStep]:
        done = {s.id for s in self.steps if s.status in ("done", "skipped")}
        return [s for s in self.steps if s.status == "pending" and set(s.depends_on) <= done]

    def is_complete(self) -> bool:
        return all(s.status in ("done", "skipped") for s in self.steps)


class ToolCallRecord(BaseModel):
    id: str
    step_id: str
    agent: str
    server: str  # "observability" | "database" | "github" | "web" | "sandbox"
    tool: str
    arguments: dict[str, Any]
    result_preview: str = ""
    is_error: bool = False
    risk: str = "read"
    origin: Origin = "trusted"  # trust of the DATA this call returned
    idempotency_key: str | None = None
    duration_ms: float = 0.0
    ts: float = Field(default_factory=time.time)


class Observation(BaseModel):
    id: str
    step_id: str
    agent: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)
    origin: Origin = "trusted"
    ts: float = Field(default_factory=time.time)


class RootCause(BaseModel):
    commit_sha: str
    file: str
    summary: str
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)
    ruled_out: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    type: str
    detail: str


class VerificationResult(BaseModel):
    status: Literal["passed", "failed"]
    issues: list[Issue] = Field(default_factory=list)
    suggested_focus: list[str] = Field(default_factory=list)
    gates: dict[str, bool] = Field(default_factory=dict)  # deterministic hard gates
    judgement: dict[str, Any] | None = None  # LLM judgement (advisory; cannot override gates)
    attempt: int = 1
    metrics: dict[str, Any] = Field(default_factory=dict)


class RiskDecision(BaseModel):
    decision: Literal["allow", "require_approval", "deny"]
    reason: str
    policy_rule: str
    tool: str = ""
    step_id: str = ""


class Artifacts(BaseModel):
    analytics: dict[str, Any] | None = None
    research: dict[str, Any] | None = None
    root_cause: RootCause | None = None
    rejected_root_causes: list[RootCause] = Field(default_factory=list)  # proven wrong by verifier
    patch: str | None = None
    patch_attempt: int = 0
    patch_summary: str | None = None
    pr_title: str | None = None  # proposed by the coding agent for change tasks
    base_ref: str | None = None  # commit the patch applies to
    baseline_ref: str | None = None  # last-good commit for benchmark comparison
    comparison: dict[str, Any] | None = None
    tests: dict[str, Any] | None = None
    branch: str | None = None
    pull_request: dict[str, Any] | None = None
    report: str | None = None


class Budget(BaseModel):
    max_steps: int = 200  # agent-loop iterations across the whole task
    max_tokens: int = 1_500_000
    max_cost_usd: float = 5.0
    max_wall_seconds: float = 1800.0
    steps_used: int = 0
    tokens_used: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0
    started_at: float | None = None
    finished_at: float | None = None

    def exhausted(self) -> str | None:
        if self.steps_used >= self.max_steps:
            return f"step budget exhausted ({self.steps_used}/{self.max_steps})"
        if self.tokens_used >= self.max_tokens:
            return f"token budget exhausted ({self.tokens_used}/{self.max_tokens})"
        if self.cost_usd >= self.max_cost_usd:
            return f"cost budget exhausted (${self.cost_usd:.2f}/${self.max_cost_usd:.2f})"
        if self.started_at is not None and time.time() - self.started_at >= self.max_wall_seconds:
            return f"wall-clock budget exhausted ({self.max_wall_seconds:.0f}s)"
        return None


TaskKind = Literal["regression", "change"]


class TaskState(BaseModel):
    task_id: str
    goal: str
    kind: TaskKind = "regression"  # regression: seeded demo repo; change: the real target repo
    status: Status = "pending"
    plans: list[Plan] = Field(default_factory=list)  # plan_v1, plan_v2, ... (history)
    observations: list[Observation] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    artifacts: Artifacts = Field(default_factory=Artifacts)
    verifications: list[VerificationResult] = Field(default_factory=list)
    risk_decisions: list[RiskDecision] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    error: str | None = None
    trace: dict[str, str] = Field(default_factory=dict)  # W3C context of the task's root span
    # memory: what was recalled for this task, and what was written/rejected afterwards
    memory_text: str = ""  # bounded hints injected into planning and the specialists
    memory_trace: dict[str, Any] = Field(default_factory=dict)
    # human-in-the-loop
    pending_approval_id: str | None = None
    approval: dict[str, Any] | None = None  # {"id","status","actor","feedback"} once decided
    human_feedback: list[dict[str, Any]] = Field(default_factory=list)  # rejections, for replanning

    @property
    def plan(self) -> Plan | None:
        return self.plans[-1] if self.plans else None

    def status_line(self) -> str:
        """Human-readable summary, e.g. `✓ research  ✓ analytics  ⏸ approval`."""
        if not self.plan:
            return f"Task {self.task_id}: {self.status}"
        marks = {"done": "✓", "running": "…", "failed": "✗", "pending": "·", "skipped": "-"}
        parts = [f"{marks[s.status]} {s.id}:{s.owner}" for s in self.plan.steps]
        if self.status == "awaiting_approval":
            parts.append("⏸ awaiting approval")
        return f"Task {self.task_id} [{self.status}] plan v{self.plan.version}: " + "  ".join(parts)

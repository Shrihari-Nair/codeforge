export const API = process.env.NEXT_PUBLIC_AEOS_API ?? "http://localhost:8800";

export type StepStatus = "pending" | "running" | "done" | "failed" | "skipped";
export type Owner = "research" | "analytics" | "coding" | "verifier" | "supervisor";

export interface PlanStep {
  id: string;
  description: string;
  owner: Owner;
  depends_on: string[];
  status: StepStatus;
  attempts: number;
}
export interface PlanVersion {
  version: number;
  reason_for_change: string | null;
  steps: PlanStep[];
}
export interface ToolCall {
  id: string;
  step_id: string;
  agent: string;
  server: string;
  tool: string;
  arguments: Record<string, unknown>;
  result_preview: string;
  is_error: boolean;
  risk: string;
  origin: string;
  duration_ms: number;
  ts: number;
}
export interface Observation {
  id: string;
  step_id: string;
  agent: string;
  summary: string;
  origin: string;
  data: Record<string, unknown>;
}
export interface Verification {
  status: "passed" | "failed";
  attempt: number;
  gates: Record<string, boolean>;
  issues: { type: string; detail: string }[];
  suggested_focus: string[];
  judgement: Record<string, unknown> | null;
  metrics: {
    benchmark?: {
      baseline: { p95_ms: number; db_queries: number };
      candidate: { p95_ms: number; db_queries: number };
      p95_change_pct: number;
    };
    files?: { path: string; status: string; added: number; removed: number }[];
  };
}
export interface RiskDecision {
  tool: string;
  decision: string;
  reason: string;
  policy_rule: string;
}
export interface TaskDetail {
  task_id: string;
  goal: string;
  kind?: "regression" | "change";
  status: string;
  queue_status?: string | null;
  attempts?: number | null;
  status_line: string;
  error?: string | null;
  plan_version?: number | null;
  plans?: PlanVersion[];
  pending_approval_id?: string | null;
  root_cause?: { file: string; commit_sha: string; summary: string; confidence: number; evidence: string[] } | null;
  pull_request?: { number: number; url: string; title: string } | null;
  verifications?: Verification[];
  risk_decisions?: RiskDecision[];
  budget?: {
    llm_calls: number;
    tool_calls: number;
    tokens_used: number;
    cost_usd: number;
    steps_used: number;
    max_steps: number;
    max_tokens: number;
    max_cost_usd: number;
    max_wall_seconds: number;
    started_at: number | null;
    finished_at: number | null;
  };
  tool_calls?: ToolCall[];
  observations?: Observation[];
  human_feedback?: { actor: string; feedback: string }[];
  artifacts?: {
    patch: string | null;
    report: string | null;
    baseline_ref: string | null;
    research?: {
      summary?: string;
      current_state?: string;
      relevant_files?: { path: string; why: string }[];
      proposed_changes?: string[];
      risks?: string[];
    } | null;
    rejected_root_causes: { file: string; summary: string }[];
  };
  trace_id?: string | null;
}
export interface TaskRow {
  task_id: string;
  goal: string;
  status: string;
  attempts: number;
  created_at: string;
  last_error: string | null;
}
export interface Approval {
  id: string;
  task_id: string;
  tool: string;
  arguments: { title?: string; head?: string; base?: string; body?: string };
  rationale: string;
  evidence: {
    gates?: Record<string, boolean>;
    benchmark?: Verification["metrics"]["benchmark"];
    patch?: string;
    files?: { path: string; status: string; added: number; removed: number }[];
    root_cause?: { file: string; summary: string } | null;
    judgement?: Record<string, unknown> | null;
  };
  status: string;
  created_at: string;
  expires_at: string;
  actor?: string | null;
}
export interface AeosEvent {
  id: number;
  task_id: string;
  ts: string;
  type: string;
  agent: string;
  message: string;
  data: Record<string, unknown>;
}
export interface Stats {
  duration_s: number | null;
  llm: { calls: number; tokens: number; cost_usd: number };
  tools: { calls: number; errors: number; by_server: Record<string, number>; mean_ms: number };
  agent_steps: number;
  plans: number;
  replans: number;
  step_retries: number;
  verifications: { total: number; failed: number };
  approvals: { requested: number; decided: number; total_wait_s: number; rejections: number };
  memory: { retrieved: number; used: boolean | null; facts_stored: number };
}
export interface Meta {
  langfuse_url: string;
  a2a_mode: string;
  llm_provider: string;
  approval_ttl_s: number;
  target_repo: string | null;
  github_publish: boolean;
}

export interface Stat {
  mean: number;
  stderr: number;
  n: number;
}
export type EvalMetricKey =
  | "success_rate" | "root_cause_accuracy" | "tool_selection" | "planning_accuracy" | "recovery_rate"
  | "human_intervention_rate" | "rubric_score" | "avg_agent_steps" | "avg_llm_calls" | "avg_cost_usd"
  | "avg_latency_s";
export type EvalAggregate = { tasks: number } & Record<EvalMetricKey, Stat>;
export interface EvalMeta {
  run_id: string;
  label: string;
  llm: string;
  scenario: string;
  break: string;
  memory: string;
  trials: number;
  model?: string;
}
export interface EvalSummary {
  id: string;
  meta: EvalMeta;
  overall: EvalAggregate | null;
  compared_to_baseline: boolean;
}
export interface EvalDelta {
  metric: string;
  label: string;
  previous: number;
  current: number;
  delta: number;
  regressed: boolean;
}
export interface EvalRow {
  task_id: string;
  template: string;
  status: string | null;
  success: boolean | null;
  latency_s: number | null;
  cost_usd: number | null;
}
export interface EvalRun {
  id: string;
  meta: EvalMeta;
  aggregate: { overall: EvalAggregate; by_template: Record<string, EvalAggregate> };
  vs_baseline: { file: string; deltas: EvalDelta[] } | null;
  runs: EvalRow[];
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API}${path}`, { cache: "no-store", credentials: "include", ...init });
  if (res.status === 401 && path !== "/auth/login" && !path.startsWith("/auth/me") && typeof window !== "undefined") {
    if (window.location.pathname !== "/login") window.location.assign("/login"); // session expired
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {}
    throw new ApiError(res.status, String(detail));
  }
  return res.json() as Promise<T>;
}

export const api = {
  meta: () => request<Meta>("/meta"),
  me: () => request<{ auth_required: boolean; user: string | null }>("/auth/me"),
  login: (username: string, password: string) =>
    request<{ user: string }>("/auth/login", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ username, password }),
    }),
  logout: () => request<{ status: string }>("/auth/logout", { method: "POST" }),
  evals: () => request<EvalSummary[]>("/evals"),
  evalRun: (id: string) => request<EvalRun>(`/evals/${id}`),
  tasks: () => request<TaskRow[]>("/tasks"),
  task: (id: string) => request<TaskDetail>(`/tasks/${id}`),
  stats: (id: string) => request<Stats>(`/tasks/${id}/stats`),
  events: (id: string, after = 0) => request<AeosEvent[]>(`/tasks/${id}/events?after=${after}`),
  approvals: (status = "pending") => request<Approval[]>(`/approvals?status=${status}`),
  approvalsFor: (taskId: string) => request<Approval[]>(`/approvals?status=&task_id=${taskId}`),
  createTask: (goal: string, kind?: "regression" | "change", maxCostUsd?: number) =>
    request<{ task_id: string }>("/tasks", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ goal, kind, max_cost_usd: maxCostUsd }),
    }),
  cancel: (id: string, actor: string) =>
    request<{ status: string }>(`/tasks/${id}/cancel`, { method: "POST", headers: { "X-Actor": actor } }),
  retry: (id: string) => request<{ task_id: string }>(`/tasks/${id}/retry`, { method: "POST" }),
  decide: (id: string, decision: "approve" | "reject", actor: string, feedback: string) =>
    request<unknown>(`/approvals/${id}/${decision}`, {
      method: "POST",
      headers: { "content-type": "application/json", "X-Actor": actor },
      body: JSON.stringify({ feedback: feedback || null }),
    }),
};

export const eventStreamUrl = (id: string, after: number) =>
  `${API}/tasks/${id}/events/stream?after=${after}`;

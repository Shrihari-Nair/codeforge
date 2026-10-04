# AEOS Architecture

AEOS takes a high-level engineering task in natural language ("find why checkout latency increased,
fix it, verify it, prepare a PR, don't merge without my approval"), plans it, delegates to specialist
agents, executes code in a sandbox, verifies the result independently, pauses for human approval
before side effects, survives crashes, and leaves a complete trace.

```
                         USER  (CLI / HTTP API / dashboard)
                                      │
                    ┌─────────────────▼─────────────────┐
                    │   API + WORKERS  (LangGraph graph) │   Postgres: tasks (leases), checkpoints,
                    │   plan → dispatch → synthesize →   │   approvals, audit log, memory
                    │   verify → [replan] → publish →    │   Redis: reserved for queues/caches
                    │   await approval → PR → reflect    │
                    └───┬───────────┬───────────┬───────┘
                        │ A2A       │ A2A       │ A2A            (agent ↔ agent, over the network)
                 ┌──────▼───┐ ┌─────▼────┐ ┌────▼──────┐
                 │analytics │ │ research │ │  coding   │   each: own process/container, own port,
                 │  agent   │ │  agent   │ │   agent   │   own Agent Card, own MCP connections
                 └──┬───┬───┘ └──┬────┬──┘ └──┬────┬───┘
                    │MCP│        │MCP │       │MCP │ sandbox API
            observability database  github    web  github    Docker sandbox
              MCP server  MCP srv   MCP srv  MCP srv          (ephemeral, no network)
```

## 1. Why this is an agent system, not a workflow

* The **plan is generated and revised by a model**, not hard-coded. After a failed verification the
  supervisor *changes strategy* (re-investigates, rejects its own earlier diagnosis); that decision
  is made from observations, not from a fixed branch.
* Agents choose **which tools to call and in what order**, loop until they decide they are done,
  and are stopped only by budgets/stagnation detection, not by a script.
* The system **closes its own loop**: it measures its own output (independent verifier), detects
  that a plausible fix was wrong (the red-herring scenario), and recovers.

What is deliberately **not** an LLM (and why): the risk policy, the sandbox limits, the verifier's
hard gates (tests, benchmark, safety), read-only database enforcement, retry/recovery state
machine, idempotency, the memory write gate and the baseline-commit computation. Safety and
hard facts must not be probabilistic; the LLM advises and the deterministic layer decides.

| Component | Implementation |
|---|---|
| Planner / replanner, synthesis | LLM, output validated by code, deterministic fallback |
| Analytics, research, coding agents | LLM + real tools (MCP / sandbox) |
| Sandbox executor, test/benchmark runner | deterministic |
| Verifier | deterministic gates + advisory LLM judge that can only tighten a verdict |
| Risk policy | deterministic, declarative YAML |
| Approval | human |
| Retry / recovery | explicit state machine (`core/retry.py`) |

## 2. MCP vs A2A: where each sits

* **MCP = agent ↔ tools and data.** Four real MCP servers (separate processes, stdio or streamable
  HTTP, official SDK): `observability`, `database` (read-only), `github` (local bare git remote +
  PR store), `web`. Agents reach external systems only through them (and the sandbox). Each tool
  declares a **risk level** (`read` / `write_reversible` / `write_irreversible`) in its metadata;
  the policy reads that, never the tool's name.
* **A2A = agent ↔ agent.** The supervisor delegates to three independently deployed specialist
  services over the A2A protocol (reference SDK): discovery by Agent Card, streamed status,
  typed artifact result, cancellation, failover between endpoints. A2A never touches tools: the
  specialist behind it owns its own MCP connections.
* Both carry W3C trace context, so one trace spans the supervisor, the specialists and the MCP
  servers.

## 3. State model (`core/state.py`)

`TaskState` is the single, explicit, serializable record of a run and is checkpointed to Postgres
after every node: plan history (`plans[v1..vN]`, each with `reason_for_change`), observations (with
trust `origin`), tool-call log (with idempotency keys), artifacts (root cause, rejected diagnoses,
**the patch, from which a sandbox can be rebuilt**, comparison, PR), verification results, risk
decisions, budget (steps/tokens/dollars/wall clock), pending approval, human feedback, memory
trace and the task's root trace context. Anything needed to resume lives here, never in process
memory.

Graph (`graph/`): `plan → dispatch ⇄ synthesize → verify → (replan →)* publish_prepare →
await_decision → publish_pr → finish → reflect`. Routing is plain, unit-tested functions
(`graph/routing.py`); the framework does not hide control flow.

## 4. Failure semantics

| Failure | Detection | Behaviour |
|---|---|---|
| Specialist errors / loops / returns garbage | loop status, stagnation, schema validation | retry the step (max 2), then **escalate** |
| Specialist service dies / hangs | connection error, per-event stall timeout, deadline | try next endpoint; else step failure → retry → escalate; remote work is cancelled |
| Patch is wrong | independent verifier recomputes tests/lint/benchmark | replan (1st), **must re-investigate** (2nd), escalate (3rd) |
| Worker killed (`kill -9`) | lease expires | another worker resumes from the last checkpoint; side effects are idempotent |
| Approval never comes | TTL (default 24h) | request expires, task escalates; a late approval gets HTTP 410 |
| Budget exhausted | shared tracker | stop, escalate (never retried) |
| Unknown error | catch-all | recorded and escalated, never swallowed |

Idempotency: every side-effecting tool carries a key derived from (task, step, attempt, tool,
args); the **server** enforces it (branch: recorded key; commit: key in a git trailer; PR: unique
row; approval request and audit entries: unique keys). A decision and its resume request are one
database transaction, so a crash can never strand an approved task.

## 5. Security model

* **Sandbox**: Docker, no network, read-only root fs, tmpfs workspace, non-root, all capabilities
  dropped, memory/CPU/pid limits, wall-clock timeout, command allowlist (defense in depth). After
  *every* command leftover processes are killed; if impossible the sandbox is destroyed. Agent code
  never runs on the host.
* **Database**: two layers: a sqlglot allowlist (single SELECT, no DML/DDL/INTO/locks/dangerous
  functions anywhere in the tree) and a read-only Postgres role with read-only transactions and a
  statement timeout.
* **Risk policy** (`policy/risk_policy.yaml`): deny always wins; first match otherwise; default
  `require_approval`. Branch writes only to `aeos/*`; merging is denied. A human approval is bound
  to the **hash of the exact arguments** shown to the human.
* **Untrusted content**: tool results are labelled `origin=trusted|untrusted` and wrapped as data;
  agents are instructed never to follow instructions inside them; web content is marked untrusted
  by the server.
* **Secrets**: redacted from every span attribute; secret values are rejected from memory,
  diffs (verifier gate) and telemetry.
* **Coding service + Docker socket** (compose profile `a2a`): the coding service mounts
  `/var/run/docker.sock` to create sandboxes. Compromising it controls the host daemon. For
  production use rootless Docker/sysbox or a dedicated sandbox host.

## 6. Observability (one trace per task)

Span names and key attributes (all spans carry `aeos.task.id` and `langfuse.session.id`):

| Span | Attributes |
|---|---|
| `task` (root) | `aeos.task.goal`, `langfuse.trace.name` |
| `node <name>` | `aeos.node` |
| `agent <owner>` | `aeos.agent`, `aeos.step.id`, `aeos.step.attempt`, `aeos.agent.status`, `.steps` |
| `llm.call <model>` | `gen_ai.*` (model, tokens), `aeos.llm.cost_usd`, prompt/completion, `langfuse.observation.*` |
| `tool.call <name>` | `aeos.tool.{name,server,risk,result_origin,arguments}`, `aeos.policy.rule`, `aeos.tool.is_error` |
| `mcp.call` / MCP server span | MCP SDK conventions, linked through `_meta` trace context |
| `sandbox.exec` | `argv`, `exit_code`, `timed_out`, `duration_s` |
| `verifier.gates` | `aeos.verification.{status,gates,issues,attempt}` |
| `approval.wait` | `aeos.approval.{id,decision,actor,tool}`; wall-clock bounds even across restarts |
| `a2a.delegate` / `a2a.serve` | owner, endpoint, step |

The root context is stored in `TaskState.trace`; every node re-joins it, so a task that pauses for a
day or moves workers is still one trace. Export: process → OTLP → collector → Langfuse
(generations, tokens, cost, sessions). Metrics: LLM calls/tokens/cost/latency, tool calls/latency,
approvals, retries, replans, verifications, task outcome/duration. `GET /tasks/{id}/stats` returns
the per-task summary.

## 7. Memory (three tiers)

Short-term = the task state (gone with the task). Episodic = past tasks (pgvector). Semantic =
environment facts with supersession. Agents only *propose* memories; a deterministic gate (no
secrets/instructions, confidence ≥ 0.7, provenance computed from the cited tool calls, evidence,
dedup, rate limit) decides. Recall is injected into planning under a hard token budget, marked as
stale-able hints. Its benefit is a measured claim (see `EVAL_RESULTS.md`), not assumed.

## 8. Deployment topologies

* **Fast mode** (default for tests/dev): everything in one Python process (specialists in-process),
  MCP servers as child processes, sandbox on local Docker.
* **Full topology** (`docker compose --profile a2a`): Postgres, Redis, OTel collector, Langfuse
  (profile `observability`), plus seed, analytics, research, coding and api+worker containers.

# AEOS — Codebase Walkthrough & Interview Prep

This document explains **what the codebase does and how it actually works**, in plain language,
for someone who needs to understand and defend this project in an interview. It complements
(doesn't repeat) `docs/ARCHITECTURE.md` (diagrams + reference tables), `docs/DECISIONS.md`
(why non-obvious choices were made) and `docs/PROGRESS.md` (what was built per phase, with test
counts). Read this first for the mental model, then dip into those for specifics.

---

## 1. What this project actually is

AEOS ("Autonomous Engineering Operating System") is a multi-agent system that takes a plain-English
engineering task — e.g. *"checkout latency went up after the last deploy, find out why, fix it,
verify the fix, open a PR, don't merge without my approval"* — and runs it to completion
**autonomously but safely**:

1. An LLM **plans** a sequence of investigation/fix steps.
2. Specialist agents (analytics, research, coding) **investigate and fix** using real tools
   (log/metric queries, git history, a Docker sandbox), not by guessing from the prompt alone.
3. A **deterministic verifier** independently re-checks the fix (reruns tests, reruns the
   benchmark, re-diffs the code) — it does not trust the coding agent's self-report.
4. If verification fails, the supervisor **replans**, and after enough failures is forced to
   re-investigate instead of just retrying the same patch.
5. Before any irreversible action (opening a PR), a **deterministic policy engine** decides
   whether a human must approve. The run **pauses durably** — it can sit for hours, survive a
   process restart, and resume from the exact same point once a human approves.
6. The whole run is **crash-proof**: a worker can be `kill -9`'d at any point and another worker
   picks the task back up from the last database checkpoint with no duplicated side effects.
7. Every step is captured in **one OpenTelemetry trace** (prompts, completions, tool calls, cost)
   that survives process boundaries, A2A network calls, and multi-hour approval waits.
8. The system **evaluates itself**: a harness runs dozens of seeded tasks, grades outcomes against
   known ground truth, and reports success rate / cost / steps as numbers, not vibes.

The codebase is deliberately built to answer one interview question directly: **"why is this an
agent, and not just a workflow with an LLM bolted on?"** The answer lives in the code, not just in
prose — see §4.

---

## 2. The "fake company" it operates on (`demo_target/`)

Before any of the agent machinery matters, there needs to be something real to investigate. AEOS
ships its own target: a small `checkout-service` Python app with a **reproducible 20-commit git
history**. One commit (`b166e05`) silently introduces an N+1 database query and bypasses a cache —
a realistic latency regression. A deterministic *cost-meter* benchmark (not wall-clock, so it's
reproducible across machines/CI) shows p95 latency jumping 4.52ms → 27.92ms after that commit.

Synthetic logs, metrics, traces and deployment records matching that exact timeline are generated
and loaded into Postgres, so the **observability/database MCP tools have real data to query** —
this isn't mocked. Critically, the target is seeded with **red herrings**: an unrelated slow query
(`inventory_audit`) that is equally slow before and after the regression (so "fixing" it doesn't
help), plus noise like an unrelated cache-timeout burst. This is what makes "the agent's first fix
is plausible but wrong" a genuine, reproducible scenario rather than a scripted gotcha — the
verifier has to actually catch it via the p95 benchmark gate, every time, with no ground-truth
peeking. `demo_target/GROUND_TRUTH.json` records the real root cause and acceptable fixes, used
only by the eval harness to grade runs after the fact.

---

## 3. Following one task end to end (the actual control flow)

The orchestration is a **LangGraph state machine** (`src/aeos/graph/`) over one Pydantic object,
`TaskState` (`core/state.py`). Every node reads/writes this object; after every node LangGraph
checkpoints it to Postgres. Routing between nodes is a **plain Python function**
(`graph/routing.py::route`), not something hidden inside the LLM or the framework — given the
current `TaskState`, it returns the name of the next node. That's worth stressing in an interview:
control flow is explicit, unit-tested, deterministic code; only the *content* of the plan is LLM
output.

The node sequence for a typical run:

```
plan → dispatch (parallel: analytics + research) → synthesize (root cause)
     → dispatch (coding) → verify → [replan → dispatch → verify]* → publish_prepare
     → await_decision (human approval, durable pause) → publish_pr → finish → reflect
```

**`plan`** — recalls relevant memory (episodic + semantic), asks the planner LLM for a plan, then
**validates it in code**: checks owners are legal for this task type, step IDs are unique, the
dependency graph has no cycles (topological sort), and at least one coding step exists. If the LLM
produced something invalid, or errored, a **hard-coded default plan** is used instead — the system
never gets stuck because the LLM said something malformed. Regardless of what the LLM proposed,
the code **always appends** a verifier step and a supervisor "publish" step at the end — the model
cannot omit the safety gates.

**`dispatch`** — runs every plan step whose dependencies are satisfied, **concurrently**
(`asyncio.gather`), each specialist in its own sandboxed agent loop (§5b). A specialist crashing
becomes a structured `error` result, not a crashed task. Any claim a research agent makes about
"the last good commit" is **not trusted** — the harness recomputes it deterministically from actual
git ancestry (`<suspect commit>~1`) and rejects SHAs that don't resolve to real commits. This exists
because live runs showed models confusing "parent commit" with "last commit that touched this
file."

**`synthesize`** — one LLM call combines the analytics and research findings into a single
`RootCause` claim, informed by any previously **rejected** diagnoses (so a disproven theory is
never proposed twice).

**`verify`** — the single most important trust boundary in the system (full mechanics in §5d).
Deterministic checks are run against a **freshly rebuilt sandbox**, not the coding agent's own
container: diff size, forbidden paths (can't edit `benchmark.py` or existing tests to fake a pass),
no secrets in the diff, tests actually pass, lint passes, and the benchmark doesn't regress beyond
tolerance. An LLM "judge" separately opines on whether the patch addresses the *stated* root cause
and requirements — but it is **structurally unable to flip a failing result to passing**; it can
only make a passing result fail. This is how the system can use an LLM in verification without
undermining the guarantee that verification is trustworthy.

**`replan`** (on verify failure) — a three-tier retry policy (`core/retry.py`, pure code, no LLM):
1st failure → tweak the same fix; 2nd failure → the replan is **forced** to include a fresh
investigation step (code rejects an LLM replan that tries to just retry); 3rd failure → escalate to
a human rather than loop forever. This is the mechanism behind the demo's signature beat: wrong fix
(the red herring) passes all unit tests but fails the benchmark gate → forced re-investigation →
correct root cause found → fix passes.

**`publish_prepare` / `await_decision` / `publish_pr`** — split into three nodes specifically so a
crash or a multi-hour approval wait never re-does already-completed work (full mechanics in §5e).
The branch and commit are created first (both reversible, pre-approved by the static risk policy);
only then is a human approval request created if the policy requires one; the actual pause happens
in `await_decision` via LangGraph's `interrupt()` *before* anything expensive runs, so resuming
after approval re-enters this node cheaply.

**`finish` / `reflect`** — status is computed purely from verification + PR state (never "the LLM
says it's done"); a human-readable report is built; then (wrapped so it can never fail the task)
the run proposes new semantic facts and an episodic memory entry for future tasks to recall.

---

## 4. Why this is an agent system and not a workflow — and the deliberate non-LLM parts

The project's own thesis (see `docs/ARCHITECTURE.md §1`) is that **the plan is generated and
revised by a model from observations**, agents choose their own tool sequence and stop themselves
(bounded only by budgets/stagnation, not a fixed script), and the system **closes its own loop**:
it measures its own output independently and recovers when it was wrong — including the
red-herring scenario where a syntactically fine fix is caught and corrected without a human in the
loop.

Equally important — and a strong interview answer to "what's *not* an LLM, and why" — is the list
of things deliberately implemented as **plain deterministic code**: the risk policy, sandbox
limits, verifier hard gates, read-only database enforcement, the retry/recovery state machine,
idempotency-key computation, and the baseline-commit computation. The principle enforced throughout
the codebase: **the LLM advises, deterministic code decides**, wherever a wrong decision would be
a safety or correctness problem rather than a judgment call.

---

## 5. Subsystem deep dives

### 5a. State & graph (`core/state.py`, `graph/`)

`TaskState` is the single serializable record of a run — the rule stated in the code's own header
comment is: *anything needed to resume a task must live here, never in process memory.* Key pieces:

- **`plans: list[Plan]`** — full history, not just the current plan, each with a
  `reason_for_change`, so a report can show how the strategy evolved. A `Plan` step is "ready" once
  all its declared dependencies are `done`/`skipped` — this dependency graph (not a fixed script)
  is what lets `analytics` and `research` run in parallel while `coding` waits for both.
- **`observations`** — every specialist output, tagged `origin: trusted|untrusted` (prompt
  injection defense, see §5h).
- **`tool_calls`** — a full audit log including the computed idempotency key of every call (see
  §5b) — this is the basis of "no duplicate side effects after a crash."
- **`artifacts`** — root cause, **rejected root causes** (so a disproven theory is never retried),
  the actual patch text (meaning a sandbox can always be rebuilt from the patch alone — the
  container itself is disposable), baseline/base commit refs used for verification, branch/PR.
- **`budget`** — one shared steps/tokens/dollars/wall-clock tracker across *all* concurrently
  running specialists (not per-agent), checked before every LLM call.
- **`trace`** — the W3C trace context of the task's root span, stored so every later node — in any
  process, after any crash, across an A2A network call — reattaches to the *same* OpenTelemetry
  trace (see §5j).

Graph wiring (`graph/build.py`): every node is wrapped so it (1) can be killed intentionally for
chaos testing at a named point, (2) re-attaches to the task's trace, (3) is checkpointed to
Postgres right after it returns. `graph/routing.py::route()` is a pure function of `TaskState` that
decides the next node — e.g. "verifier failed → replan," "all investigation steps done but no root
cause yet → synthesize," "approval granted → publish_pr." Nothing about control flow is implicit or
hidden inside LangGraph; it's explicit, readable, unit-tested code sitting on top of the framework.

### 5b. The agent loop (`agents/loop.py`, `agents/tools.py`)

Every specialist (analytics/research/coding) runs the same generic **observe → think → act** loop:
call the LLM with the conversation so far and its tool schemas (plus a synthesized `finish` tool
built from its expected structured-output schema, so the agent is *forced* to end with typed,
validated output, not free text); execute whichever tool it picked; append the result; repeat.
Several deterministic safety nets wrap this loop:

- **Budget** is checked *before* every LLM call, not after, so a run can never slightly overshoot.
- **Context compaction**: once the conversation grows past a size threshold, older tool results are
  collapsed to short markers so long-running loops don't blow up context size/cost.
- **Malformed-output handling**: if the model returns no usable tool call several times in a row,
  the loop gives up with a `malformed` status instead of hanging.
- **Stagnation detection**: calls are fingerprinted by `(tool, sorted args)`. Repeating the exact
  same call a few times triggers a warning injected into the conversation; repeating it further
  stops the loop outright — but the counter **resets whenever a mutating tool runs**, so
  "edit → test → edit → test" isn't mistaken for a stuck loop.
- **`finish` validation**: the model's final structured answer is validated against a Pydantic
  schema; a validation error is fed back to the model for one or two retries before giving up.

**`ToolRuntime`** is the single choke point every tool call passes through (used by every
specialist). For each call it: builds a context describing the call (including whether the agent
has, at any point in this run, consumed untrusted tool output — once it has, every later write it
attempts is flagged `untrusted_only` for the risk policy); asks the risk policy for a decision
(`allow` / `require_approval` / `deny`); on `require_approval` it raises, which propagates out of
the loop and becomes a LangGraph interrupt; on `allow` it runs the tool inside a try/except (a
buggy/crashing tool implementation can never crash the agent) and wraps the result as
`<tool_result origin="trusted|untrusted">...` — a literal string the model sees, paired with a
system-prompt rule telling it never to treat instructions found inside tool results as commands.

**Idempotency keys** are computed as a hash of `(task_id, step_id, attempt, tool_name, canonical
JSON of the arguments)`. For MCP write tools, this key is **hidden from the model's schema** and
injected transparently by the runtime — the model can't forge or omit it, and because it's a pure
function of the call's identity, replaying the exact same step after a crash produces the exact
same key, which the MCP server side can recognize and treat as a no-op repeat rather than a
duplicate action (see §5h).

### 5c. Specialists & supervisor (`agents/specialists.py`, `agents/supervisor.py`)

- **Analytics** — tools: observability + read-only database MCP. Prompt prescribes an explicit
  investigation method (check deployments → metrics step-change → time-window comparison → traces
  → logs) and explicitly warns that something equally bad before *and* after a deploy is a red
  herring, not the cause.
- **Research** — tools: read-only GitHub + web. Prompt specifically tells it to use blame to find
  *which commit introduced the current value* of a suspicious line, not just "a commit that touched
  this file," and to confirm with a diff before claiming a root cause.
- **Coding** — the only specialist with write access, and only inside an ephemeral Docker sandbox.
  Crucially, **the final patch is never taken from the model's claims** — after the loop ends, the
  harness runs `git diff` **inside the sandbox itself** and uses that as the artifact. A model
  cannot claim a change it didn't make, or hide one it did. Its prompt explicitly forbids editing
  the benchmark/measurement code or the tests to fake a pass, and tells it its work will be
  independently re-verified.

The **supervisor** doesn't just free-associate a plan: `create_plan()` asks the LLM for a plan but
*validates* it (legal owners, no cycles, at least one coding step) and falls back to a fixed
default plan if the LLM's output is invalid or the call fails — then **always appends** the
verifier and publish steps itself, regardless of what the LLM proposed. `replan()` follows the same
pattern, and when the retry tier demands a strategy change, code-level validation **rejects** an
LLM replan that doesn't include a fresh investigation step.

### 5d. Verifier & judge (`agents/verifier.py`, `agents/judge.py`)

The verifier never trusts the coding agent's self-report. It re-applies the patch to a **freshly
rebuilt** sandbox and recomputes everything from scratch:

- *Static checks on the diff itself*: file/line-count limits, no edits to forbidden paths (the
  benchmark script, cost-measurement code) or to existing tests unless they're new files, no
  secrets in the added lines (regex patterns for cloud keys/PEM blocks/tokens, plus any explicitly
  registered secret values).
- *Sandbox checks*: benchmarks the declared baseline commit **and** the unpatched regressed commit
  separately and confirms the regression is actually reproducible (catches a wrong "last good
  commit" diagnosis before even looking at the fix); applies the patch fresh; runs the real test
  suite; runs lint; benchmarks the patched version and compares its p95 against baseline with a
  tolerance.

All of these are boolean gates; **any single failing gate fails the whole verification**, with
structured issues and a suggested focus handed to the replanner.

An LLM **judge** separately looks at the same patch and the already-computed gate results, and is
told explicitly it cannot overrule them, then opines on two softer questions: does the fix address
the *stated* root cause, and does it cover the requirements. The combination function is
structurally **monotone**: it has exactly one place where it sets the final status, and that one
place can only move a `passed` result to `failed` — there is no code path that turns a failed gate
result into a pass. This is the concrete mechanism behind "the LLM can advise the verifier but
cannot be the reason something wrongly passes."

### 5e. Risk policy & human-in-the-loop (`core/risk.py`, `policy/risk_policy.yaml`, `store/approvals.py`, `hitl.py`)

The policy engine is a small, declarative rules evaluator, not an LLM: **deny rules always win**
regardless of order (e.g. merge/delete/force-push tools, or any write to a branch outside the
`aeos/*` namespace, are denied no matter what else matches); otherwise the **first matching rule**
in file order decides; otherwise the configured default (`require_approval`) applies. Rules match
on the tool's **declared risk level** (set by the server that owns the tool, not inferred from its
name — so renaming a tool can't quietly downgrade its risk), on origin (trusted vs. driven by
untrusted content), and on argument patterns like diff size or branch name.

A human approval doesn't grant a blanket "yes" — it's **bound to a hash of the exact arguments**
the human was shown when the request was created. If anything about the pending call's arguments
differs by the time it's actually executed, the policy blocks it rather than silently reusing the
old approval.

Publication is deliberately split into three graph nodes (§3) so that resuming never repeats
completed work, and the actual pause uses LangGraph's native `interrupt()` mechanism — the paused
state, including exactly which approval is pending, is part of the checkpointed `TaskState` written
to Postgres. On the store side, `decide()` is a single atomic compare-and-set
(`UPDATE ... WHERE status='pending' AND not expired`) — this is what guarantees exactly one
approve/reject wins under concurrent clicks, and writing the audit entry plus the "please wake this
task up" instruction happen in the **same database transaction**, so a crash between "human decided"
and "task resumed" is structurally impossible. Approvals also expire (default 24h); an expired
pending approval auto-escalates the task to a human instead of hanging forever.

### 5f. Durable execution (`worker.py`, `store/tasks.py`, `core/retry.py`)

Tasks live in a Postgres-backed queue with **leases**: a worker claims a task with
`FOR UPDATE SKIP LOCKED` (so multiple worker processes can poll the same table without blocking
each other), sets a lease expiry, and must heartbeat-renew it periodically. If a worker dies
(`kill -9`), its lease simply expires and another worker's next claim picks the task back up — this
is the entire mechanism behind "survives a crash mid-run," there is no special-cased recovery logic
beyond "resume from the last Postgres checkpoint." A crash-loop guard force-escalates a task after
too many claim attempts, so a task that reliably crashes a worker can't do so forever.

On resume, the worker inspects the LangGraph checkpoint: never started → start fresh; paused at an
interrupt with a decision already waiting → resume with that decision; crashed mid-run with no
interrupt → simply re-invoke the graph with no new input, and LangGraph re-enters exactly where the
last checkpoint left off. Re-running a node that already partially executed is safe specifically
*because* every side-effecting tool call is idempotency-keyed (§5b/§5h) — replayed writes are
detected and treated as already-done, not duplicated.

The retry policy itself (`core/retry.py`) is pure, LLM-free code: a failed specialist step retries
up to 2 attempts then escalates; a failed verification follows the three-tier policy described in
§3 (replan → forced re-investigation → escalate to human). "Escalated" is a distinct status from
"failed" — it means a human could still resolve it (budget exhausted, retries exhausted, 3 failed
verifications); the system is designed to **fail loudly and ask for help**, never to silently
swallow an unrecoverable situation.

### 5g. Sandbox (`sandbox/manager.py`, `sandbox/policy.py`, `sandbox/helpers.py`)

Every piece of agent-written or agent-executed code runs inside a hardened, throwaway Docker
container: no network at all, read-only root filesystem with small size-capped tmpfs mounts for the
writable workspace and `/tmp` (`/tmp` itself is mounted no-exec, so nothing can be dropped and run
from there), hard memory/CPU/process-count limits, all Linux capabilities dropped, no privilege
escalation, and a non-root user for actual commands. A separate low-privilege user runs as PID 1
specifically so that the "kill everything the sandbox's main user owns" cleanup logic run after
every command can never accidentally kill the container's own init process. After *every* exec, the
system checks for any leftover processes (e.g. a fork bomb whose parent already exited); if cleanup
isn't possible, the entire sandbox is destroyed rather than left in an unknown state. Commands are
passed as argv lists with no shell available at all (so there's nothing for shell-metacharacter
tricks to exploit), and additionally allowlisted/denylisted (network tools, package installers, and
privilege tools are explicitly denied) as defense-in-depth on top of the container boundary itself.

On top of the raw exec API, deterministic helpers (`run_tests`, `run_benchmark`,
`compare_benchmarks`) parse structured results (JUnit XML, a JSON benchmark report) rather than
returning raw logs for an LLM to interpret — pass/fail and performance numbers are facts, not
opinions. A reaper process cleans up orphaned sandboxes left behind by crashes or finished tasks.

### 5h. MCP servers — agents' only door to the outside world (`mcp_servers/`)

Four independent MCP server processes stand between every agent and the real world: `database`
(read-only), `observability`, `github` (a local git remote + Postgres-backed PR metadata, so the
demo is fully deterministic without touching real GitHub), and `web`. Each tool is tagged with a
declared **risk level** in its own metadata, which is what the risk policy reads.

The **database guard** is two independent layers: an application-level parser (`sqlglot`) that
requires the query be a single `SELECT`/set-operation and walks the *entire* parsed syntax tree
rejecting any write/DDL/lock/transaction keyword anywhere in it (catching sneaky constructs like a
write hidden inside a CTE, not just a top-level check), plus a dangerous-function denylist; and
underneath that, a genuinely read-only Postgres role with read-only transactions and a statement
timeout. Either layer alone is sufficient to block the tested attack queries — it's deliberately
redundant.

The **github server's write tools** are where the idempotency-key mechanism (§5b) actually lands:
`commit_patch` embeds the idempotency key as a git commit-message trailer; before doing any work it
searches the branch's commit log for a commit that already carries that exact key and, if found,
returns that result immediately instead of re-applying the patch. This means the dedup record
survives even a crash between pushing the commit and recording it in the task's own database row —
the git history itself is the source of truth. All writes are restricted to an `aeos/*` branch
namespace and every git invocation explicitly ignores ambient user/system git config and refuses to
prompt for credentials, so it fails fast rather than hanging.

Content coming back from the **web** tools (and generally anything originating outside the trusted
tool set) is explicitly labeled `untrusted` — this is the other half of the prompt-injection
defense described in §5b: untrusted text is wrapped as inert data, agents are told never to follow
instructions found inside it, and the risk policy itself can additionally require human approval
for any write whose trigger traces back only to untrusted content.

### 5i. Memory — three tiers, with a hard write gate (`memory/gate.py`, `memory/manager.py`)

- **Short-term** is just the task's own `TaskState` — it disappears when the task ends; the
  boundary is explicit by design.
- **Episodic** memory is a record of past tasks (goal, root cause, fix, failed attempts, rejected
  diagnoses, cost) retrieved by similarity for a new, similar goal.
- **Semantic** memory is durable environment facts (`subject, predicate, value`) with confidence
  and provenance; a new value for the same `(subject, predicate)` **supersedes** the old one rather
  than duplicating it, and superseded facts are never recalled again.

The key design point: **agents only propose memory; code decides whether it's written.** A
dedicated gate function rejects a candidate fact if it's empty, too long, looks like a secret,
looks like an injected instruction ("ignore previous instructions," fake `<system>` tags, etc.),
has confidence below a threshold, comes only from untrusted content without independent
confirmation, has no supporting evidence, or would exceed a per-task rate limit, or duplicates an
existing fact. Provenance itself is **not** taken from the model's say-so — it's recomputed by the
harness from the actual tool-call records the model cited as evidence, so a model can't launder an
untrusted claim by simply labeling it trusted. One fact — the verified root cause of a successfully
fixed task — is synthesized by the harness directly from ground truth (bypassing the evidence
requirement, since it's not an LLM claim at all).

Recall before planning is capped by a hard token budget: the best-ranked item is truncated rather
than dropped if it doesn't fit, but lower-ranked items are dropped outright once the budget is
exhausted. Recalled memory is explicitly presented to agents as *hints to verify*, not facts to
trust blindly. (A live measured comparison found no significant benefit yet on the current easy
demo task — documented honestly in `docs/PROGRESS.md` Phase 8 rather than assumed.)

### 5j. A2A — specialists as independently deployed services (`a2a/server.py`, `a2a/client.py`)

Each specialist can run either in-process (fast path for tests/dev) or as its own standalone
service with its own Agent Card, discovered and called over the network via the A2A protocol — the
graph code is unaware which mode is active; it talks to the same abstract `Specialist` interface
either way. The A2A client tries a list of endpoints in order and fails over between them within a
single delegation if one is unreachable; a stalled (silently hanging, not crashed) remote service
is specifically detected via a read-timeout on the streaming connection ("a silent service is a dead
service"), separate from the overall wall-clock deadline for the whole call, which — when it fires —
actively cancels the remote work rather than just giving up locally. Whatever budget (LLM
calls/tokens/cost) the remote specialist consumed is reported back and merged into the **same
shared task-level budget tracker** the rest of the system uses, so a distributed deployment doesn't
accidentally lose cost control.

### 5k. Observability — one trace per task (`telemetry/tracing.py`)

Every layer of the system (graph nodes, the agent loop, tool calls, MCP calls, sandbox execs,
verifier gates, A2A calls) creates spans through exactly one shared helper function. Centralizing
span creation there is what makes two cross-cutting guarantees cheap to enforce universally:
**every span attribute is run through a redaction filter** (registered secret values plus regex
families for common key formats) before being recorded, so no span anywhere can leak a credential;
and every span is automatically tagged with the current task's ID via a context variable, with no
call site needing to remember to pass it explicitly.

The harder problem — keeping *one* trace across a run that spans multiple OS processes (A2A
specialist services, MCP server child processes) and real wall-clock gaps (a multi-hour approval
wait, or a worker crash and restart) — is solved by treating the trace context itself as
**durable, checkpointed state**: the task's root span's W3C trace-context is captured once and
stored inside `TaskState`, and every later node — in any process, after any restart — re-attaches to
it before opening its own spans. The same context is carried inside A2A message metadata so a
specialist running as a genuinely separate service still produces spans that nest under the
original task's root span.

### 5l. Evaluation harness (`evals/`)

A separate harness runs a dataset of seeded tasks (varying templates/difficulty) against either a
live LLM or a deterministic scripted one, in parallel trials, and grades each run with **two kinds
of graders**: deterministic outcome graders that independently re-apply the agent's final patch to
a fresh sandbox and re-run tests/benchmark themselves (never trusting the run's own self-report,
the same philosophy as the production verifier), plus trace graders that inspect the policy audit
log for forbidden actions, duplicate side effects, or missing approvals. A separate LLM-rubric
grader scores free-text report quality against a small human-validated rubric, with a deterministic
keyword-based stand-in for fully offline/CI runs. Results are aggregated per-task (not per raw
trial, so a task run many times doesn't dominate the variance calculation), compared against a
stored baseline with a regression tolerance, and the harness can deliberately cripple a safety
mechanism (e.g. the retry policy) as a sanity check that the evals actually have signal — i.e. would
visibly catch a regression if one existed.

---

## 6. Design decisions worth being ready to defend

The full list with one-line reasons is `docs/DECISIONS.md` (45 entries); a few that come up
naturally in interview conversation:

- **LangGraph + Postgres checkpointer over Temporal** — chosen because the custom lease-queue +
  checkpoint approach passes the full chaos suite (27/27) at this project's scale, without adding
  an operational dependency; explicitly revisit "at 100x."
- **The patch artifact always comes from the sandbox's own `git diff`, never from model text** — a
  model literally cannot claim a change it didn't make or hide one it did.
- **A strategy change after repeated failure is enforced in code, not requested in a prompt** — the
  replanner's output is validated and rejected if it doesn't include a fresh investigation step.
- **The baseline commit is computed from git ancestry by code**, never taken from the model's
  claim — found necessary after live runs showed models confusing "parent commit" with "last commit
  touching this file."
- **Memory's benefit is reported as a measured (currently inconclusive) claim**, not assumed —
  intellectual honesty about what was actually tested is itself a deliberate engineering stance
  here, and a good thing to say explicitly in an interview.

---

## 7. Answers to the standard questions this project is built to answer

**1. Why is this an agent and not a workflow?**
The plan isn't a fixed script — it's generated and *revised* by a model based on what was actually
observed (see §3's replan loop), and the supervisor can reject its own earlier diagnosis and change
strategy. Agents choose their own tool order and decide for themselves when they're done (bounded
by budgets/stagnation detection, not a hardcoded sequence). The system closes its own loop: it
independently measures its own output and recovers from being wrong, without a human in that inner
loop. What's deliberately *not* an LLM: risk policy, sandbox limits, verifier hard gates, DB
read-only enforcement, the retry state machine, idempotency, memory's write gate, baseline-commit
computation (§4).

**2. MCP vs A2A — where does each sit?**
MCP connects an agent to tools and data (database, observability, github, web) — always within one
agent process. A2A connects agent to agent — the supervisor delegating to independently deployable
specialist services over the network, with discovery, streaming, typed results, cancellation and
failover. A2A never touches tools directly; the specialist behind it owns its own MCP connections.
Both carry the same W3C trace context so one trace spans all of it (§5j, §5k).

**3. How does a run survive a crash mid-step, without duplicating side effects?**
LangGraph checkpoints `TaskState` to Postgres after every node. A dead worker's lease simply
expires; another worker's next claim resumes the graph from the last checkpoint — crashed-mid-node
work is deliberately not fine-grained-checkpointed, the node just re-runs. Re-running is safe
because every side-effecting tool call carries an idempotency key derived from
`(task, step, attempt, tool, args)`, and the MCP write tools detect a replayed key server-side
(e.g. the git commit-message trailer) and return the prior result instead of repeating the effect.

**4. How does human approval resume from the exact state?**
Publication is split into three graph nodes specifically so the pause point (`await_decision`) does
nothing expensive before calling LangGraph's `interrupt()` — resuming just re-executes that cheap
node. The approval decision and the "wake this task up" instruction are written in one database
transaction, so a crash between a human's decision and the task resuming is structurally
impossible. The grant itself is bound to a hash of the exact arguments shown to the human, so
tampering after approval is caught.

**5. How does the verifier avoid just agreeing with the coding agent?**
It never consumes the coding agent's report of what it did — it snapshots the actual diff from the
sandbox and reapplies it to a **fresh** sandbox, re-running tests, lint and benchmark itself. The
advisory LLM judge sees the gate results already computed and is structurally only able to make a
passing verdict fail, never a failing verdict pass.

**6. How do you defend against prompt injection in retrieved content?**
All tool results are labeled `origin=trusted|untrusted` and this label is visible to the model
inside the wrapped tool-result text, alongside a standing system-prompt instruction to never follow
instructions found in that data. The risk policy additionally requires human approval for writes
whose trigger traces only to untrusted content. The memory write gate independently rejects
anything that looks like injected instruction text or that is sourced only from untrusted content
without independent confirmation.

**7. How do you measure agent quality given nondeterminism?**
A dataset of seeded tasks with known ground truth is run in repeated trials (live or scripted),
graded by deterministic outcome re-checks (never trusting the run's own self-report) plus trace
audits plus a validated LLM rubric for fuzzy quality. Metrics are aggregated per-task before
averaging, so a heavily-retried task doesn't dominate variance. A deliberate "break the retry
policy" run exists purely to prove the eval suite would actually notice a regression.

**8. What did failure injection reveal?**
See `docs/PROGRESS.md` Phase 7's "what chaos tests found" list: idempotency-key handling for a
branch move, a double-written audit entry on a crash between PR creation and checkpointing, and a
lingering sandbox from a crashed worker — all three were real bugs the chaos suite caught and that
are now fixed and covered by regression tests.

**9. What's the memory write policy?**
Agents only *propose* memory; a deterministic gate decides, checking length/secrets/injection
patterns, a confidence floor, independently-recomputed provenance, evidence requirements, a
per-task rate limit, and deduplication (§5i). The measured benefit of memory on the current task is
inconclusive and reported as such rather than assumed.

**10. What would change at 100x scale?**
This is explicitly flagged as open in `docs/DECISIONS.md` (D-033) as a thing to revisit — the
honest answer in an interview is to talk through where the current design would need to change:
the single shared Postgres becomes a scaling bottleneck for both the task queue and checkpoints;
the lease-based polling queue would likely need to move to a push-based queue; Temporal or a
similar durable-execution system becomes more attractive once the custom lease/checkpoint machinery
has to handle multi-tenant fairness and much higher throughput; the sandbox host and Docker-socket
access (currently a documented, accepted risk for one service) would need a dedicated
rootless/sysbox sandbox fleet; and cost/budget tracking would need to move from a single
in-task tracker to per-tenant quotas.

---

## 8. Where to look in the code

| Question | File |
|---|---|
| How is a run orchestrated? | `src/aeos/graph/build.py`, `routing.py`, `nodes.py` |
| What's in the task's state? | `src/aeos/core/state.py` |
| How does an agent think/act/stop? | `src/aeos/agents/loop.py` |
| How are tool calls gated/traced/idempotent? | `src/aeos/agents/tools.py` |
| What do the three specialists do? | `src/aeos/agents/specialists.py` |
| How does planning/replanning work? | `src/aeos/agents/supervisor.py` |
| How is a fix verified? | `src/aeos/agents/verifier.py`, `judge.py` |
| What's allowed without a human? | `policy/risk_policy.yaml`, `src/aeos/core/risk.py` |
| How does approval pause/resume work? | `src/aeos/graph/publish.py`, `store/approvals.py`, `hitl.py` |
| How does crash recovery work? | `src/aeos/worker.py`, `store/tasks.py`, `core/retry.py` |
| How is the sandbox hardened? | `src/aeos/sandbox/manager.py`, `policy.py`, `helpers.py` |
| How are DB/GitHub/web tools implemented safely? | `src/aeos/mcp_servers/` |
| How does memory decide what to keep? | `src/aeos/memory/gate.py`, `manager.py` |
| How is A2A wired? | `src/aeos/a2a/server.py`, `client.py` |
| How does one trace span everything? | `src/aeos/telemetry/tracing.py` |
| How is the seeded bug built? | `demo_target/history.py`, `datagen.py`, `GROUND_TRUTH.json` |
| How are runs graded? | `evals/run_evals.py`, `graders.py`, `metrics.py` |
| How do I run it? | `src/aeos/cli.py` (`aeos run|serve|worker|submit|status|approve`) |

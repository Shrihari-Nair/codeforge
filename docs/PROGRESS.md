# Progress

## Plan summary (source: ROADMAP.md)
- Goal: durable multi-agent system that investigates, fixes, verifies, and PRs an engineering task with human approval, crash recovery, tracing, evals.
- Phases 0-5 = vertical slice (foundations, demo target, MCP, sandbox, agent loop, verifier+replan).
- Phases 6-7 = risk policy + HITL, durable execution.
- Phases 8-9 = memory, real A2A topology.
- Phases 10-13 = observability, evals, failure injection, security.
- Phases 14-15 = dashboard, docs, demo video.
- Deterministic code owns safety (risk policy, sandbox, verifier gates, DB read-only); LLMs advise.
- Never cut: verifier loop, HITL exact resume, durable execution, sandbox, evals, tracing.
- Tests use FakeLLM; live tests are marked `live`.
- Detailed plans live in docs/superpowers/plans/.

## Phase 0: Foundations
Status: complete (acceptance verified locally)

Built:
- uv project; ruff, mypy --strict (core, llm), pytest (+asyncio, `live` marker excluded by default); Makefile (`make check`, `make up`, `make infra-check`).
- `aeos.core.config` (env-based Settings, secrets masked), `aeos.core.logging` (JSON logs with task_id/run_id/agent via contextvars).
- `aeos.llm.base` provider interface + `aeos.llm.fake.FakeLLM` (scripted, loud on drift, zero network).
- docker-compose: postgres+pgvector, redis, otel-collector; Langfuse v4 stack under `observability` profile.
- GitHub Actions workflow (lint, typecheck, tests).

Deferred:
- Gemini provider implementation -> start of Phase 4 (needs live key + tool-call loop).
- CI unverified remotely (no git/remote managed from here).

Known issues / notes:
- Host ports remapped to avoid the user's other project: Redis 6380 (not 6379), MinIO 9092 (not 9090).
- langfuse-worker and otel-collector have no container healthcheck (worker has no probe upstream; collector image is distroless).

## Phase 1: Demo target
Status: complete (acceptance verified locally)

Built:
- `demo_target/history.py` + `build_repo.py`: 20-commit checkout-service history (reproducible SHAs) built into gitignored `demo_target/build/checkout-service`. Regression = commit `b166e05` "refactor(catalog): simplify product lookup" (N+1 + cache bypass); unit tests still pass.
- Deterministic benchmark (cost-meter, not wall-clock): baseline p95 4.52 ms vs regressed 27.92 ms (6.2x); `db_queries` 520 vs 3770.
- Red herring: `inventory_audit` slow scan (every 25th request): identical before/after; fixing it alone leaves p95 regressed (tested). Extra noise: unrelated cache-timeout burst at deploy-6h; decoy commits in the deploy range.
- `datagen.py` + `seed.py`: seeded logs/metrics/spans/deployments into Postgres schema `demo` (3 deployments, 10,440 logs, 1,440 metric rows, 3,722 spans). Idempotent loader.
- `GROUND_TRUTH.json`, `demo_target/README.md` with the human investigation queries (all executed and confirmed to lead to the root cause).

Deferred: multiple regression templates (n_plus_one only) -> Phase 11 (needs 30+ eval tasks).
Notes: git is used only inside `demo_target/build/` (config-isolated); outer project untouched. Seed test skips if Postgres is down.

## Phase 2: MCP servers
Status: complete (acceptance verified locally; 166 tests, lint + strict mypy green)

Built (each a separate process, `python -m aeos.mcp_servers.<name>`, stdio or `--transport streamable-http`):
- **database**: `get_schema`, `explain_query`, `query_database`. sqlglot guard (single statement, SELECT/set-ops/CTE only, no INTO/locks/DML/DDL anywhere in the tree, function denylist) + row cap (500) + 5s statement timeout + read-only Postgres role `aeos_readonly` (layer 2, tested independently).
- **observability**: `search_logs`, `get_trace`, `get_metrics`, `get_error_rate`, `list_deployments`, `compare_time_windows`. The seeded step change, db-queries growth, cache-hit collapse and the unchanged red herring are all discoverable through the tools alone (tested).
- **github** (local bare remote + Postgres PR metadata): `get_repository`, `search_code`, `get_file`, `list_commits`, `get_diff`, `create_branch`, `commit_patch`, `create_pull_request`, `get_pull_request`. Writes only to `aeos/*` branches; no merge tool exists; every write requires an `idempotency_key` (replays are exactly-once, tested); ref/path/branch abuse rejected.
- **web**: `search_web` (local corpus provider), `fetch_page` (host allowlist, no redirects, link-local blocked, text only, 500KB cap), `extract_information` (deterministic structure extraction). Every result carries `untrusted: true`.
- Risk level is in each tool's `meta["aeos"]["risk"]` (+ standard annotations); `create_pull_request` is `write_irreversible`, branch/commit are `write_reversible`, everything else `read`.
- `aeos.mcp_client.McpClient`: connection ownership in a dedicated task (usable from any task), per-call timeout, retries only for `read` tools or writes carrying an `idempotency_key`, structured `McpToolError(code, message)`, `traced()` span around every call (`aeos.telemetry.tracing`, logs now, OTel in Phase 10).
- `scripts/mcp_probe.py`: generic client to exercise any server standalone.

Deferred / notes:
- Real GitHub backend (COULD): not built; `GitBackend` is the seam. Live mode against Shrihari-Nair/broadcast-server is a later optional add-on.
- Real web search provider: default is the offline local corpus (deterministic); the provider interface is the seam.
- Official MCP Inspector (npx) not run here; the SDK client and `mcp_probe.py` serve as the generic client.
- Seeded Postgres data + `vcs` schema are created by `demo_target.seed` / the github server on first use; tests truncate `vcs.pull_requests`.

## Phase 3: Sandbox executor
Status: complete (acceptance verified locally; 223 tests total, lint + strict mypy green; 0 leftover containers after the suite)

Built (`aeos.sandbox`, sync API; async callers use `asyncio.to_thread`):
- Image `aeos-sandbox:<dockerfile-hash>` (python:3.12-slim + git + pytest 8.3.4), auto-built on first use.
- Hardened container per sandbox: network none, read-only rootfs, tmpfs `/workspace` (256MB) and `/tmp` (64MB, noexec), 512MB RAM with no swap, 1 CPU, 128 pids, all caps dropped, no-new-privileges, non-root uid 10001 for commands, keepalive as uid 10002, init process, no host mounts, labels for the reaper.
- API: `create_sandbox(RepoRef|files)`, `exec(argv)` (no shell; command allowlist/denylist; per-call wall-clock timeout; 1MB output cap with `truncated`), `read_file`, `write_file`, `apply_patch`, `snapshot_diff` (git diff against a baseline commit), `destroy`, context manager, `reap`.
- Cleanup after EVERY exec: leftover processes are detected (docker top) and killed; if that is impossible (e.g. pid limit exhausted) the sandbox is destroyed and `ExecResult.sandbox_destroyed` says so.
- Deterministic helpers: `run_tests` (structured via junit XML: counts, failing test names, timeout), `run_benchmark`, `compare_benchmarks(baseline, candidate, patch)` -> p95 ratio / change % / `regressed` / candidate test result.
- Reaper: `SandboxManager.reap(ttl_s, keep)` + `python -m aeos.sandbox.reaper`.

Verified in tests: infinite loop, fork bomb, memory hog (OOM-killed), workspace-full, network egress (DNS/TCP/HTTP all blocked, only `lo` exists), writes outside workspace, host path reads, non-root + zero capabilities, output flood, symlink to `/`, path traversal in the file API, escaping patches, orphan reaping after a simulated crash. End to end on the demo repo: baseline and regressed benchmark numbers match GROUND_TRUTH exactly inside Docker; the real fix passes the gate; the red-herring fix FAILS the p95 gate while all tests pass (Phase 5 demo beat, now proven).

Deferred: network allowlist mode for dependency installs (needs an egress proxy; demo has no deps). Gemini provider (Phase 4).

## Phase 4: Core agent loop + vertical slice
Status: complete (live Gemini run verified; scripted full-graph runs deterministic in tests)

Built:
- `aeos.llm.gemini.GeminiProvider` (google-genai; function calling with raw JSON schemas, thought-signature passthrough, retries with backoff on 429/5xx), `llm.structured.complete_structured`, `llm.pricing` (cost estimates), `RoutedFakeLLM` (per-role scripts so concurrent agents stay deterministic), `FakeLLM` now snapshots recorded calls.
- `core.state.TaskState` (plan history, observations with trust origin, tool-call log with idempotency keys, artifacts incl. patch + refs, verification results, risk decisions, budget), `core.budget`, `core.events` (+ConsoleSink), `core.risk` (RiskGate protocol + StaticRiskGate keyed on declared tool risk; `ApprovalRequired`).
- `agents.loop.run_agent`: observe→think→act with max steps, shared token/cost/wall-clock budget, stagnation detection (warn then stop), malformed-output recovery, structured `finish`, context compaction; never raises for model/tool misbehaviour.
- `agents.tools`: `ToolRuntime` (risk gate, tracing, truncation, results labelled `<tool_result origin=...>`, idempotency keys hidden from the model and injected deterministically per task/step/attempt/tool/args).
- Specialists with real tools and typed outputs: Analytics (observability+DB MCP), Research (GitHub+web MCP), Coding (sandbox tools; the PATCH IS TAKEN FROM THE SANDBOX BY THE HARNESS, never from model text). Supervisor: LLM planner with deterministic validation + default-plan fallback, root-cause synthesis.
- Verifier (deterministic gates, all recomputed in fresh sandboxes): patch present/applies, tests, lint (compileall), benchmark p95 vs baseline (+10% tolerance), diff size, no secrets (patterns + configured secret values), allowed paths (benchmark.py, costs.py, existing tests are protected: editing the measurement code is caught).
- LangGraph graph (`graph/`): plan → dispatch (parallel specialists) / synthesize / verify → finish; routing is explicit pure code in `graph/routing.py`.
- `aeos run` CLI (`--replay happy` for offline deterministic demo, `--llm gemini` for live).

Verified: live Gemini (gemini-2.5-flash) run found the seeded root cause, rejected the red herring, produced a fix that passed all gates (16 LLM calls, ~$0.02). Scripted runs use the real MCP servers + Docker sandbox. Tests cover loop limits/budget/stagnation/malformed output, planner fallback, parallelism, specialist failure, lazy coding agent.

Notes: live Gemini plans may serialise steps (planner decides dependencies). Provider is Gemini only (D-002).

## Phase 5: Verifier + replanning
Status: complete (M1 vertical slice reached)

Built:
- `core/retry.py`: explicit retry state machine. Step failures: retry up to 2 attempts, then escalate. Verification failures: 1st -> replan, 2nd -> replan that MUST re-investigate (enforced by validation; deterministic fallback if the LLM tries to just retry), 3rd -> escalate to a human. Budget exhaustion is never retried.
- `agents/judge.py`: LLM judgement of requirement coverage. ADVISORY: `apply_judgement` can only make a verdict stricter; a failed gate can never become a pass (property-tested over all judge outputs). Judge down -> gates decide. The patch is passed as untrusted data.
- `agents/supervisor.replan`: new plan version with `reason_for_change` always recorded (LLM-written, or deterministic from the verifier issues if the LLM is down/invalid). Rejected diagnoses are remembered (`artifacts.rejected_root_causes`) and shown to the investigators, the synthesis step and the planner. Plan history `plans[v1..vN]` kept in state and in the report.
- Graph: `verify` (gates + judge) -> `replan` -> dispatch again; escalation sets status `escalated` with a human-readable error.
- Scenario `--replay wrong_fix`: misdiagnosis (red herring) -> wrong fix passes all tests -> verifier fails it on the p95 benchmark (+517%) -> plan v2 (re-investigate) -> real cause -> fix passes. Asserted step by step in `tests/graph/test_replan_e2e.py`.

Notes: "failed" is now reserved for non-recoverable run errors; recoverable-by-human outcomes (retries exhausted, budget, 3 failed verifications) are `escalated`.

## Phase 6: Risk policy + human-in-the-loop
Status: complete (acceptance verified; see tests/core/test_risk_policy.py, tests/store/, tests/graph/test_hitl.py)

Built:
- **Declarative policy** (`policy/risk_policy.yaml`, `core/risk.PolicyEngine`): deny rules always win regardless of order; otherwise first match; otherwise `default: require_approval`. Keyed on the risk level DECLARED by the tool's server (`meta.aeos.risk`) plus normalised tool name, server, argument patterns (branch must be `aeos/*`), provenance (`origin: untrusted_only`), and diff size. Renaming/aliasing a tool cannot bypass it (a renamed irreversible tool is still `write_irreversible`; `merge_*`/`delete_*`/`force_push` name variants are denied even if the tool claims `read`).
- **Approvals are bound to the exact action**: an `ApprovalGrant` carries the hash recorded when the human saw the request; if the stored arguments change after approval the policy blocks the call (tested).
- **Publication = three nodes**: `publish_prepare` (create branch + commit patch, both reversible/allowed, then create the ApprovalRequest) → `await_decision` (LangGraph `interrupt`) → `publish_pr` (PR with the grant). Split so a resume never repeats earlier work.
- **Approvals store** (`aeos.approvals`, `aeos.audit_log`): idempotent create per (task, action); decisions are compare-and-set (one winner under concurrency); expiry (default 24h, `AEOS_APPROVAL_TTL_S`); every decision has a named actor and an audit entry.
- **API** (FastAPI): `POST /tasks`, `GET /tasks/{id}`, `GET /approvals`, `POST /approvals/{id}/approve|reject` (X-Actor required, optional feedback), `POST /approvals/expire`, `GET /tasks/{id}/audit`.
- **Rejection with feedback** goes to the replanner and the coding agent (plan v2 with the human's words in `reason`); repeated rejections escalate instead of looping.
- **Postgres checkpointer** (`graph/checkpoint.py`): the paused run survives the process.

Verified: run reaches `create_pull_request`, pauses; the API process is destroyed; a NEW process with an LLM that has nothing scripted (any model call would raise) resumes from the checkpoint on approval and creates exactly one PR with no repeated work; audit trail = requested → approved(alice) → pr_created. Also: concurrent approvals → one winner; double approve → 409; approving an expired request → 410 and the task escalates; tampering with approved arguments → blocked.

## Phase 7: Durable execution
Status: complete (M2 reached). Chaos suite: 27/27 pass (`uv run pytest -m chaos tests/chaos`, ~13 min).

Built:
- **Postgres checkpointer** after every node; **task queue with leases** (`aeos.tasks`): claim (`FOR UPDATE SKIP LOCKED`), heartbeat renewal, fencing (a worker that lost its lease cannot finish/overwrite the task), crash-loop guard (5 deaths -> `escalated` for a human), `paused`/`queued` bookkeeping.
- **`Worker`** (`aeos worker`, or embedded in `aeos serve`): resumes exactly where the checkpoint says: never started -> start; paused at approval -> `Command(resume)` with the stored decision; crashed mid-run -> continue from the last checkpoint. Housekeeping: expire overdue approvals (waking their tasks) and reap orphan sandboxes; a finished task's sandboxes are always removed.
- **Idempotent side effects everywhere**: branch (key recorded), commit (key in a git trailer), PR (unique key), approval request (unique per task/action), audit entries (`dedupe_key`), memory episode (per task).
- **Human decision + resume request in one transaction**: a crash can never strand an approved task. Status line on resume (`Task t-1: ✓ s1:analytics ✓ s2:research ✓ s3:coding ✓ v1:verifier ⏸ awaiting approval`).
- **Crash injection** (`failure/crash.py`, `AEOS_CRASH_AT=node:<n>:before|after,tool:<t>:before|after`): SIGKILL at a named point.
- CLI: `aeos serve | worker | submit | status | approvals | approve | reject`.

Chaos matrix (real worker subprocesses, SIGKILL, takeover by a second worker after the lease expires): every graph node x before/after (16), tool boundaries (7: mid-analytics, mid-sandbox, create_branch/commit_patch/create_pull_request before/after), 3 random-kill seeds (3 assassinations each), and crash-while-awaiting-approval then restart then approve. Invariants checked every time: task `completed`, attempts>=2, exactly 1 PR / 1 branch / +1 commit / 1 approval / 1 `pr_created` audit entry, no leftover sandbox.

**What the chaos tests found (all fixed):**
1. `create_branch` replay after our own commit moved the branch reported a conflict -> the idempotency key is now recorded and honoured.
2. `pr_created` audit entry written twice when the worker died between creating the PR and checkpointing -> audit entries are replay-safe.
3. A crashed worker's sandbox lingered until the next sweep -> the worker reaps a task's sandboxes when the task ends.

## Phase 8: Memory
Status: complete (mechanism + safety verified; BENEFIT NOT DEMONSTRATED, see below)

Built:
- **Three tiers, distinct**: short-term = `TaskState` (the boundary is explicit: it is the checkpointed run state, gone when the task ends); **episodic** = past tasks in `aeos.episodes` (goal, root cause, fix, failed attempts, wrong diagnoses, cost, lessons) with pgvector cosine retrieval; **semantic** = durable environment facts in `aeos.facts` keyed by (subject, predicate) with source, confidence, timestamps and **supersession** (a new value supersedes, same value only re-confirms; superseded facts are never recalled; explicit invalidation also supported).
- **Write policy** (`memory/gate.py`, deterministic): agents only PROPOSE (`memory-curator` step after each task). Rules in order: non-empty, length cap, no secrets (patterns + configured secret values), no instruction-like text, confidence >= 0.7, no facts derived only from untrusted content unless confirmed by the verifier, evidence required, per-task rate limit, dedup. Provenance (`trusted`/`untrusted_only`) is computed by the harness from the cited tool calls, never claimed by the model. The harness itself derives one verifier-confirmed fact (the root cause of a verified fix). Episodes are built deterministically from state.
- **Recall** before planning, bounded by `AEOS_MEMORY_TOKEN_BUDGET` (best item truncated, lower-ranked dropped); injected into the planner and as `memory_hints` into the specialists, which are told to treat it as stale hints and verify. Retrieval is logged (`memory_recalled`, `state.memory_trace`) with whether it was used. Memory failures never fail a task.
- Embeddings: Gemini (`gemini-embedding-001`, 768d) live; deterministic `HashEmbedder` for tests/replay.

Verified: 49 unit/integration tests (gating of dupes/secrets/injection/untrusted/low-confidence, supersession chains, retrieval ordering, token budget) + end-to-end (second task's planner and analytics prompts contain the first task's memory; poisoned curator proposals refused end to end).

**Benefit measurement (live Gemini, 3 trials per arm, 1 seeding run):** memory-on LLM calls 65/17/19 vs memory-off 19/21/62 (median 19 vs 21; mean 34 vs 34). **No benefit can be claimed**: variance is dominated by whether a run hits a wrong first diagnosis + replan, which happens with and without memory. The task is easy for the model (a typical clean run is ~17-21 calls). A real answer needs >=10 trials per arm on harder/varied tasks; that is the Phase 11 eval (memory on/off is one of its comparisons).

**What the live runs taught us (fixed):**
1. Models mistake "previous commit touching the file" for "parent commit"; the baseline then predated `benchmark.py` and the verifier's baseline benchmark crashed. The harness now derives the baseline as `git rev-parse <suspect>~1` and rejects suspect SHAs that are not commits (event `baseline_corrected`; fired in 2 of 3 live runs).
2. Stagnation detection flagged "edit -> run tests -> edit -> run tests" as a loop. Mutating tools now reset the repeat counters (`ToolDef.mutates`).
3. The independent verifier and the advisory judge caught wrong first fixes in the live runs (perf regression; "patch includes unrelated changes"), and the replanner recovered.

## Phase 9: A2A, independently deployed specialists
Status: complete (M3 reached). Reference SDK `a2a-sdk` 1.2.1 (stable 1.x), no hand-rolled protocol.

Built:
- `aeos.a2a.server`: wraps ANY specialist as an A2A service: Agent Card (`/.well-known/agent-card.json`: name, skills, streaming capability, bearer security scheme, JSON-RPC interface URL), JSON-RPC endpoint with a bearer-token check (card discovery stays public), streamed status updates (every agent/tool event), the typed `SpecialistResult` as a data artifact, cancellation, crash-as-failed-task (never a hung one).
- `aeos.a2a.client.A2ASpecialist`: same `Specialist` protocol as the in-process classes, so the graph is unchanged. Discovery + card sanity check, streaming consumption, per-event stall detection (a silent service is a dead service), whole-delegation deadline that CANCELS the remote work, endpoint failover within one delegation, remote budget usage added to the shared tracker, W3C `traceparent` + remaining budget in message metadata.
- `aeos agent <owner> --port N` runs one specialist as its own process (own MCP connections; coding also owns sandbox access). `AEOS_A2A_MODE=a2a` + `AEOS_A2A_ENDPOINTS` switches the supervisor to remote specialists; it then opens only the repo tools it needs to publish. In-process mode stays the default for fast tests.
- Deployment: `Dockerfile` (one image for every service) and the compose profile `a2a`: seed (non-destructive), analytics, research, coding, api(+worker) as separate containers.

Verified (real processes / containers): Agent Cards discoverable and distinct; a generic SDK client (not our supervisor) drives a specialist; bearer auth enforced; a full task completes through three separate specialist processes (supervisor holds no investigation tools); `kill -9` of the only research service mid-run -> detected, retried, escalated to a human in seconds (no hang); a second research instance takes over inside one delegation (failover event, no retry needed); a hung service is declared dead by the stall timeout; the deadline cancels remote work; trace context and budget cross the boundary. In containers: task submitted over HTTP, three specialist containers served it, approved by `alice`, PR created with full audit trail; with the analytics container stopped the task escalates in ~2s with a clear A2A error.

Bugs found by running it for real: `aeos agent` ignored `AEOS_REPLAY` (a formatter had silently defeated my edit; now covered by tests/cli); re-running the seed container wiped the git remote (seed is now build-once).

## Phase 10: Observability
Status: complete. One OpenTelemetry trace per task, verified in-process, across A2A/MCP processes, and in Langfuse.

Built:
- **Spans behind the existing `traced()` seam** (so every layer was already instrumented): `task` (root, created by the plan node, its W3C context stored in `state.trace` so ANY later node in ANY process/after a crash re-joins the same trace), `node <name>`, `agent <owner>`, `llm.call <model>`, `tool.call <name>` (risk, origin, policy rule, args, result), `mcp.call`, `sandbox.exec` (argv, exit code), `verifier.gates` (gates, issues), `approval.wait` (explicit wall-clock bounds even when the wait spanned a restart), `a2a.delegate` / `a2a.serve`. Every span carries `aeos.task.id` and `langfuse.session.id`.
- **`TracedLLM`** wraps any provider (planner, agents, judge, curator): prompt, completion, tool calls, `gen_ai.usage.*`, estimated cost, in Langfuse-compatible attributes (`langfuse.observation.type=generation`, `.input/.output/.usage_details/.cost_details`).
- **Cross-process propagation**: MCP uses the SDK's native W3C `_meta` propagation (server middleware makes a child server span); A2A carries the context in message metadata (OpenTelemetry propagators) and the service continues the trace.
- **Metrics** (OTel): LLM calls/tokens/cost/latency, tool calls/latency, approvals, step retries, replans, verifications, task outcome/duration.
- **Redaction**: secret patterns + registered exact secret values (API keys) are scrubbed from every span attribute (tested).
- **Export**: each process -> OTLP gRPC -> collector -> `debug` + Langfuse (OTLP/HTTP, project keys bootstrapped by compose). `aeos.telemetry.setup.init_telemetry` is called by every entrypoint.
- **Per-task summary API**: `GET /tasks/{id}/stats` (duration, LLM calls/tokens/cost, tool calls by server/tool + errors + mean latency, agent steps, plans, replans, step retries, verifications, approvals incl. total human wait, memory use, trace id).
- Checkpoint serialization now uses an explicit allowlist of our state types (LangGraph is moving to deny-by-default).

Verified: span-tree test walks task -> node -> agent -> llm.call (decision in the output) -> tool.call -> mcp.call, plus sandbox/verifier/approval spans, all in one trace; a task paused for approval and resumed by a brand-new runtime stays in the SAME trace; trace context crosses real MCP and A2A boundaries. Live check: a replayed task produced 88 observations in Langfuse (71 spans, 17 generations, 6,800/1,700 tokens, $0.0204) under session = task id; the collector also received the metrics.

Bugs found by looking at real output: attributes set on an already-ended `sandbox.exec` span; Langfuse v4 serves observations via `/api/public/v2/observations` (the v1 traces API is disabled in events-only mode).

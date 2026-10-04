# Decisions (lightweight ADRs)

## D-001: Checkpointing: LangGraph vs Temporal
Status: tentative. Use LangGraph + Postgres checkpointer; revisit after Phase 7.
Reason: roadmap tech stack fixes LangGraph; Temporal is a COULD.

## D-002: LLM provider is Gemini (user decision)
Provider interface stays provider-agnostic; Gemini is the real implementation (built in Phase 4), plus FakeLLM for tests.
Reason: user's chosen provider. The roadmap's "two providers" is relaxed to one real + Fake.

## D-003: No git operations by the agent (user decision)
The user manages git/remotes themselves; no repo init or commits from here.
Reason: user preference. Consequence: Phase 1's ~20-commit demo history will be generated inside demo_target's own nested repo only if the user agrees; to be raised at Phase 1.

## D-004: Langfuse in the `observability` compose profile
Reason: v4 needs ClickHouse + MinIO + Redis + Postgres; keeps the core dev stack light. Started with `docker compose --profile observability up -d --wait`. Bootstrap keys via LANGFUSE_INIT_* so Phase 10 needs no manual UI setup.

## D-005: OTel collector health via extension, not container healthcheck
Reason: distroless image has no shell/curl. Verified with `curl localhost:13133`.

## D-006: Host port remaps (Redis 6380, MinIO 9092)
Reason: 6379/9090 are held by an unrelated local project's containers.

## D-007: Added psycopg[binary] dependency
Reason: Postgres driver needed by the demo seed loader and later the MCP/DB layers; implied by the roadmap's Postgres stack.

## D-008: Benchmark latency is simulated (cost meter), not wall-clock
Reason: repeatable across machines/CI/sandbox; gate comparisons can't flake. Production-like ms numbers in the generated data come from datagen's own model, calibrated to show the same shape.

## D-009: Demo repo built from history.py into a gitignored build dir
Reason: stable SHAs, no nested .git inside the user's repo, and ground truth can pin the bad commit. Phase 2's GitHub MCP serves it from a local bare remote; the optional live mode uses Shrihari-Nair/broadcast-server (token in .env, never committed).

## D-010: The red herring only affects the tail (p99), by design
Reason: makes "fix the slow query" a plausible but wrong first attempt that the p95 benchmark gate catches (Phase 5 demo beat).

## D-011: MCP Python SDK v2 (`mcp` 2.x, `MCPServer`)
Reason: current official SDK; FastMCP was renamed. Added deps: mcp, sqlglot, httpx (explicit; mcp already needs httpx).

## D-012: Added a `commit_patch` tool to the GitHub MCP (not in the roadmap list)
Reason: the roadmap's tools can create a branch and a PR but give no way to put code on the branch. `commit_patch` applies a unified diff to an `aeos/*` branch only; never main.

## D-013: All GitHub write tools require `idempotency_key`
Reason: Phase 7 resume must not duplicate effects. Enforced server-side: branches are naturally idempotent, commits carry an `AEOS-Idempotency-Key` git trailer (replay detected from git itself, so it survives a crash between push and DB write), PRs use a unique (repo, key) row.

## D-014: McpClient owns the SDK connection in its own task
Reason: anyio cancel scopes must exit in the task that entered them; LangGraph nodes call tools from different tasks. Found via a real failure in tests.

## D-015: DB safety is two independent layers
sqlglot allowlist (application) + read-only Postgres role with read-only transactions and statement timeout (database). Either alone blocks the tested write attempts.

## D-016: Offline local research corpus as the default web search
Reason: deterministic evals/CI; fetch still goes over real HTTP with real egress controls. Phase 13 adversarial pages are added to this corpus.

## D-017: Sandbox files go in/out through exec, not docker cp
Docker's put_archive refuses read-only root filesystems and `docker cp` cannot see tmpfs. Rather than weaken the hardening (writable rootfs / host bind mounts), tar is streamed into an in-container `tar -x` over exec stdin and files are read with `cat`.

## D-018: Keepalive process runs as a different uid than commands
Reason: the post-command "kill everything the sandbox user owns" cleanup must not be able to stop the container. Found by a failing test.

## D-019: Leftover-process check after every exec
Reason: a fork bomb's children outlived the command (its parent died with exit 1, so no timeout fired). Cleanup is therefore unconditional, with destroy as the fallback; callers see `sandbox_destroyed`.

## D-020: Network allowlist mode deferred
Needs an egress proxy to be real (not a fake allowlist). The demo has no third-party dependencies. Revisit if eval repos need installs.

## D-021: Patch artifact comes from the sandbox, never from model output
The coding agent edits files in the sandbox; the harness snapshots `git diff` itself. A model can't claim a change it didn't make or hide one it did.

## D-022: Tool results are labelled data, idempotency keys are hidden from the model
Results are wrapped `<tool_result origin="trusted|untrusted">` and system prompts state the data-not-instructions rule. Write tools' `idempotency_key` is stripped from the schema shown to the model and derived by the runtime from (task, step, attempt, tool, args), so resume/replay can't duplicate effects and the model can't forge keys.

## D-023: Parallel agents share one budget tracker; routing is plain functions
Reason: runaway cost must be capped across all agents; `graph/routing.py` keeps control flow readable and unit-testable instead of hidden in the framework.

## D-024: Gemini 2.5 Flash as the default agent model
Configurable via AEOS_GEMINI_MODEL / AEOS_GEMINI_STRONG_MODEL (planner/synthesis use the strong model setting). Thinking budget 1024 tokens, counted as output tokens for cost.

## D-025: LLM judgement can only tighten a verdict
`apply_judgement` is monotone: gates failed -> stays failed; gates passed -> judge may fail it; judge unavailable -> gates decide. This is what lets the verifier be trusted even though one of its inputs is an LLM.

## D-026: Escalation, not failure, for anything a human could resolve
Retries exhausted, budget exhausted, or 3 failed verifications -> `escalated` (not silently `failed`). Fail loudly; never swallow unknown errors.

## D-027: A strategy change is enforced in code, not requested in a prompt
After the 2nd failed verification the replan must include a re-investigation step; if the LLM omits it the plan is rejected and a deterministic replan is used.

## D-028: Deny-wins, first-match policy with a default of require_approval
Reason: the roadmap's example is ordered first-match, but `merge_pull_request: deny` must be effective wherever it is written. Evaluation: any matching deny wins; else first match; else default. Unknown match keys raise instead of silently never matching.

## D-029: Sandbox tools are risk `read`
Risk levels describe EXTERNAL side effects. The sandbox is ephemeral and isolated, so editing/executing inside it must not trip the "untrusted-origin write" rule. Real writes (branch/commit/PR) are the only `write_*` tools.

## D-030: Approval grants bind to the arguments hash stored at request time
Reason: approving "open PR titled X" must not authorise "open PR titled Y". The hash is recorded when the human is shown the request, so even tampering with the DB row after approval is caught.

## D-031: Decision and resume request are one transaction
`approvals.decide` also stores the task's `pending_resume` payload in the same transaction (payload carries `approval_id`). A crash can never leave an approved request whose task is never resumed.

## D-032: Crash recovery = re-run the interrupted node, not fine-grained mid-node state
Nodes are written so re-running them is safe (idempotency keys, unique constraints, dedupe keys). Mid-node progress (e.g. a half-finished coding sandbox) is deliberately NOT checkpointed: the sandbox is rebuilt from the base ref. Simpler and verifiably correct; costs repeated LLM work only for the interrupted node.

## D-033: LangGraph + Postgres checkpointer kept (Temporal not adopted)
Reason: the chaos suite passes on LangGraph checkpoints + our own lease queue; Temporal would add an operational dependency for the same guarantees at this scale. Revisit at 100x (see INTERVIEW_PREP).

## D-034: Replay (FakeLLM) workers handle one task per process
Scripted LLMs are consumed once; a replay worker that claimed a second task would find them exhausted. Production workers use live LLMs and are unaffected. Chaos tests isolate the queue accordingly.

## D-035: Hard facts are derived by code, not reported by the model
The baseline commit is `<suspect>~1` computed by the harness; the suspect must resolve to a real commit. Observed live: models confuse "parent commit" with "previous commit touching the file".

## D-036: Memory is advisory and gated; its benefit is a measured claim, not an assumption
A first live comparison (n=3/arm) showed no effect; we report that and defer the real test to the Phase 11 eval harness.

## D-037: Provenance of a memory is computed from tool-call origins, never self-reported
Reason: a model cannot launder an untrusted claim by labelling it trusted.

## D-038: Use the A2A reference SDK (1.x), not a hand-rolled subset
It exists and is stable; we use agent card, message/send streaming, task states, artifacts, cancel. MCP = agent<->tools/data; A2A = agent<->agent.

## D-039: The A2A result is a typed artifact; progress is streamed status text
The final `SpecialistResult` (incl. harness-collected patch and remote budget usage) travels as a JSON data artifact; free-text status updates are for humans/dashboards only.

## D-040: Failure of a remote specialist is an ordinary step failure
Connection refused / stall / deadline / failed task all become `SpecialistResult(status="error")`, so the same retry-then-escalate state machine applies. Failover to another endpoint happens inside one delegation.

## D-041: The coding service mounts the Docker socket (documented risk)
Needed to create sandbox containers from inside a container. Anyone who compromises that service controls the host Docker daemon. Mitigations: only the coding service (and the api, which verifies) get it; for production use rootless Docker/sysbox or a dedicated sandbox host.

## D-042: One trace per task, re-joined from stored context
The root span is short-lived; its W3C context lives in `TaskState.trace`. Each node re-attaches it, so a task that pauses for a day or moves between workers is still one trace.

## D-043: Instrument at seams, not call sites
`traced()` (tracing), `TracedLLM` (all model calls) and `ToolRuntime` (all tool calls) were the existing choke points; Phase 10 only changed those, which is why coverage is complete.

## D-044: Telemetry never carries secrets
Redaction runs on every span attribute (patterns + registered exact secret values). Prompts/completions ARE recorded (capped at 4000 chars each) because the walkable decision trail is the point; secrets in them are masked.

## D-045: Real GitHub PRs are a mirror performed inside the approved tool call
Agents keep working against the local bare remote (reproducible, evals unaffected). Only `create_pull_request` (risk `write_irreversible`, so gated by human approval) pushes the `aeos/*` head branch and the base branch to GitHub and opens the PR via REST. The base is a dedicated `aeos-demo` branch holding the seeded demo code (the target repo's own `main` has none of it); `main`/`master` are rejected as base, pushes are never forced, no merge tool exists. Mirroring is idempotent (existing open PR for the head is reused), the token is passed via env-supplied git header (never URL/argv) and scrubbed from errors. Off unless `AEOS_GITHUB_PUBLISH` + repo + token are set.

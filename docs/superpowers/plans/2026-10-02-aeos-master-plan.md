# AEOS Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. This is the *master* plan: it fixes order, interfaces, and gates. Each phase gets its own bite-sized plan (`docs/superpowers/plans/<date>-phase-NN-<name>.md`) written with the writing-plans skill **right before that phase starts**, so it reflects the code that actually exists. Phase 0's detailed plan already exists: `2026-10-02-phase-00-foundations.md`.

**Goal:** Build the Autonomous Engineering OS: a durable, multi-agent system that investigates, fixes, verifies and PRs an engineering task with human approval, crash recovery, tracing and evals.

**Architecture:** LangGraph graph with explicit, in-repo state/routing/retry code. Supervisor delegates to Research/Analytics/Coding specialists (in-process first, A2A services in Phase 9). Specialists reach the world only via real MCP servers and a Docker sandbox. Deterministic code (risk policy, sandbox limits, verifier gates, DB read-only) decides safety; LLMs advise.

**Tech Stack:** Python 3.12, uv, FastAPI, Pydantic v2, LangGraph (+ Postgres checkpointer), Postgres/pgvector, Redis, MCP Python SDK, Docker SDK, OpenTelemetry + Langfuse, Next.js (Phase 14), ruff/mypy/pytest, GitHub Actions.

**Spec:** `ROADMAP.md` (source of truth; conflicts get raised, not silently resolved).

## Global Constraints

- Work one phase at a time; Phase N+1 starts only after N's acceptance checks pass and output is shown.
- Maintain `docs/PROGRESS.md` (built / deferred / issues / decisions per phase) and `docs/DECISIONS.md` (ADR per non-obvious choice).
- Every phase ships tests. LLM-dependent code is tested against `FakeLLM`; live tests use `@pytest.mark.live` and are excluded by default.
- Agent-generated code never runs on the host, including in tests.
- No provider-specific LLM calls outside `src/aeos/llm/`.
- No fake MCP/A2A (no single-process "A2A", no MCP wrappers around trivial in-process functions, no tool-less agents).
- Ask before adding a major dependency not in the roadmap or cutting a MUST item.
- Conventional commits, small and often. `mypy --strict` on `core`.
- State is explicit and serializable; side effects only through idempotent, risk-tagged tools; typed (Pydantic) boundaries; budgets for steps/tokens/dollars/wall-clock.

## Review Focus

- Live LLM returns malformed/partial tool-call JSON: loop must recover or escalate, never crash silently (Phase 4 test).
- Task text containing odd input (empty, huge, non-English, prompt-injection phrases): rejected/bounded at API/CLI, never reaches tools raw (Phases 4, 13).
- Approval arrives twice, after expiry, or after the task was cancelled: exactly-once PR creation, clear error otherwise (Phase 6/7).
- Postgres/Redis/Docker unavailable at startup: fail loudly with an actionable message, not a hang (Phase 0 healthcheck + Phase 7).
- Resumed run after crash when the sandbox container is gone: sandbox rebuilt from last patch artifact (Phase 7).

## Environment findings

Directory is greenfield (only `ROADMAP.md`) and **not a git repo**: Phase 0 starts with `git init`. Present: uv 0.11.7, Python 3.12.3, Docker 29.4.3 + Compose v5.1.3, make 4.3. Roadmap's `aeos/` root is this directory; the Python package lives at `src/aeos/`.

## Open decisions to confirm (recommended default in bold)

1. LLM providers (need two + FakeLLM): **Anthropic + OpenAI** behind `llm/base.py`.
2. Phase 0 compose includes full Langfuse (v3 needs ClickHouse+MinIO+Redis+Postgres): **yes, as spec'd**, in a `observability` compose profile so the base stack stays light.
3. A2A: **implement minimum spec surface ourselves** if no stable SDK exists at Phase 9 (decide then, log in DECISIONS.md).
4. Temporal: **LangGraph+Postgres checkpointer** (tentative per roadmap; revisit after Phase 7).
5. Git hosting: **local bare repo + PR metadata in Postgres**, no real GitHub.

## Phase map

Each row: dependency → deliverable → the hard acceptance gate (from ROADMAP.md) → key risk.

| # | Phase | Depends on | Core deliverables (module paths) | Gate | Key risk / note |
|---|---|---|---|---|---|
| 0 | Foundations | none | uv project, ruff/mypy/pytest, compose (pg+pgvector, redis, langfuse, otel), `core/config.py`, `core/logging.py`, `llm/base.py`, `llm/fake.py`, CI | `docker compose up` healthy; `make test` green; FakeLLM test with zero network | Langfuse stack weight; keep in a profile |
| 1 | Demo target | 0 | `demo_target/checkout-service` (~20-commit git history, 1 regression + red herring), data generator → Postgres tables, benchmark script, `GROUND_TRUTH.json` | Human can find bug from logs+code; benchmark delta repeatable | **Foundation of the demo.** Build the history with a script so it's reproducible; make benchmark deterministic (fixed seed, op counts not wall-clock where possible) |
| 2 | MCP servers | 0,1 | `mcp_servers/{github,observability,database,web}`, risk-level tool metadata, `agents`-side MCP client wrapper (timeouts/retries/spans) | Each server runs standalone; integration tests go over MCP protocol; DB MCP rejects DROP/UPDATE/stacked | Statement allowlist via SQL parser (propose `sqlglot`; ask first) + read-only DB role as defense in depth |
| 3 | Sandbox | 0,1 | `sandbox/` Docker executor, limits, `run_tests()`, `run_benchmark()`, baseline-vs-patch compare, reaper | Escape/abuse tests; clone+test+bench in sandbox; orphan reaping | Build a pinned sandbox base image; network off by default |
| 4 | Agent loop + vertical slice | 1-3, 0 LLM | `core/state.py` (task state), agent loop (budgets, stagnation), `graph/` supervisor→specialists→verifier→finish, 3 specialists with structured outputs, `aeos run` CLI | FakeLLM trajectory runs in CI; budget limits halt runaway; live run finds root cause (good run) | Define state schema carefully: everything later (checkpointing, approvals) depends on it being serializable |
| 5 | Verifier + replan | 4 | deterministic gates + LLM judge, `VerificationResult`, replanner, retry state machine, plan history, wrong-fix scenario | Scripted wrong-fix→fail→replan→correct-fix; LLM cannot override gates; plan_v1/v2 inspectable | **M1 reached.** The wrong-fix scenario (red herring) is a demo beat; script it as a golden FakeLLM trajectory |
| 6 | Risk policy + HITL | 2,5 | `core/risk.py` + YAML policy, LangGraph interrupt, `ApprovalRequest` store, API approve/reject, audit log, expiry | Pause→stop process→approve via API→PR with no repeated work; reject→replan; alias-bypass tests | Policy keyed on tool metadata from the MCP server, not tool name strings alone |
| 7 | Durable execution | 6 | Postgres checkpointer, task manager + lease/heartbeat, recovery process, idempotency keys (task,step,attempt), sandbox rebuild from patch artifact, status summary | Chaos test kill -9 at every node boundary + mid-node; crash while awaiting approval; idempotent replay | **M2 reached.** Hardest phase; budget extra time. Idempotency must be enforced inside the MCP tool (store key in Postgres), not only the caller |
| 8 | Memory | 4,7 | `memory/{short_term,episodic,semantic}`, pgvector retrieval, `propose_memory` + write gate, supersession | Write-gating unit tests; memory on/off eval delta; contradiction supersedes | Eval delta depends on Phase 11 harness; build a minimal on/off runner here |
| 9 | A2A | 4,7 | `a2a/` server+client, 3 specialist services w/ Agent Cards, compose profile, trace-context propagation, in-process mode flag | Kill a specialist mid-run → no hang; standalone A2A client works; single trace spans processes | **M3 reached.** If time-short, cut A2A entirely rather than fake it |
| 10 | Observability | 4 (instrument early) | OTel spans for every layer, Langfuse generations, metrics, cost summary API | Walk plan→agent→prompt→model→tool→result→decision in one trace; spans cross MCP+A2A | Add span hooks from Phase 4 onward so this is wiring, not retrofit |
| 11 | Evals | 5,10 | `evals/` dataset (30+ tasks from `demo_target/templates`), graders (outcome, trace, LLM judge), metrics, `run_evals.py`, versioned results | One command → metrics report; broken prompt lowers metrics; FakeLLM smoke eval in CI | Needs several regression templates in `demo_target` (extend Phase 1 design for templates) |
| 12 | Failure injection | 7,9,11 | `failure/` injector (config+API), per-fault expected behavior doc, eval scenarios | Fault→detection→recovery table all pass | No silent wrong output; every fault ends in recovery/replan/escalation |
| 13 | Security | 2,6 | `security/` untrusted-content pipeline, tool-call guardrails, adversarial set seeded into `demo_target` variants, secret redaction, JWT + RBAC | All adversarial cases blocked; exfil tests; redaction test | **M4 reached** (with 10-12). Origin-tracking (untrusted-only provenance) must be in state from Phase 4 |
| 14 | Dashboard | 6,10 | `web/` Next.js: task graph, drilldown, stats, approval inbox, fault controls, SSE | Whole demo drivable from UI; refresh restores state | Keep minimal; backend is the state owner |
| 15 | Hardening/docs/demo | all | README + `make demo`, ARCHITECTURE, DEMO, DECISIONS, EVAL_RESULTS, INTERVIEW_PREP, video, cost controls, clean-clone test | Stranger runs demo from README; all MUSTs met | **M5 reached** |

## Cross-phase design decisions to lock in early (they are expensive to change later)

1. **Task state schema (Phase 4)** carries from day one: `plan_versions`, `observations` with an `origin` field (`trusted|untrusted`), `tool_calls` with `idempotency_key`, `risk_decisions`, `verification_results`, `budget`, `artifacts` (including the latest patch, which Phase 7 uses to rebuild a sandbox). Adding these later forces checkpoint-format migrations.
2. **Tool registry with risk metadata (Phase 2)**: risk level is declared by the MCP server and surfaced to the client; the risk policy (Phase 6) reads it. Never infer risk from a tool's name.
3. **Telemetry hooks from Phase 4**: a single `traced()` helper wrapping LLM/tool/sandbox calls, a no-op until Phase 10 wires OTel.
4. **Specialist interface (Phase 4)**: `async def run(task: SpecialistRequest) -> SpecialistResult` (Pydantic). Phase 9 swaps the in-process implementation for an A2A client behind the same interface.
5. **Demo-target templates (Phase 1)**: generate regressions from templates (`n_plus_one`, `cache_bypass`, `serialization`, ...) even though only one is needed for the demo; Phase 11 needs 30+ eval tasks.
6. **FakeLLM trajectories as golden fixtures**: scripted by step, stored under `tests/fixtures/trajectories/`; reused by CI, demo replay mode, and smoke evals.

## Milestone schedule (roadmap estimates, ~30-35 focused days total)

| Milestone | Phases | Est. | Demonstrable |
|---|---|---|---|
| M1 Vertical slice | 0-5 | ~11-13 d | Agent finds+fixes seeded bug, verified, replanned |
| M2 Safe + durable | 6-7 | ~3.5-4 d | Approval gating, crash/resume |
| M3 Protocols | 8-9 | ~3.5-4 d | Memory, real MCP+A2A topology |
| M4 Provable | 10-13 | ~6-8 d | Traces, evals, fault injection, security |
| M5 Shippable | 14-15 | ~4 d | Dashboard, docs, demo video |

Cut order if short (last cut first): K8s/OAuth → Temporal → real GitHub → dashboard polish → A2A (only if real) → memory extras. **Never cut:** verifier loop, HITL exact resume, durable execution, sandbox, evals, tracing.

## Working protocol per phase

1. Write the phase's detailed plan (writing-plans skill) from this table + the ROADMAP section.
2. Execute task by task (TDD: failing test → minimal code → pass → commit).
3. Run the phase acceptance checks, show output.
4. Update `docs/PROGRESS.md` and `docs/DECISIONS.md`; commit.
5. Only then start the next phase.

# Phases 7-15: execution notes (written as built; see PROGRESS.md for results)

## Phase 7 (durable execution)
- Postgres checkpointer after every node (Phase 6), `aeos.tasks` queue with leases + heartbeat + fencing, crash-loop guard, decision+resume in one transaction, idempotent side effects (branch/commit/PR keyed; approval create keyed), sandbox reaper by task liveness.
- Acceptance: kill -9 a real worker subprocess at 16 node boundaries + 7 tool boundaries + 3 random-kill seeds + crash-while-awaiting-approval; all must complete exactly once.
- Found by chaos: create_branch replay after our own commit moved the branch -> fixed by recording the key.

## Phase 9 (A2A)
- Use the reference SDK `a2a-sdk` 1.x. Each specialist is its own process/port with an Agent Card and its own MCP connections.
- Supervisor-side `A2ASpecialist` implements the same `Specialist` protocol as the in-process classes: discovery via agent card, `message/send`, status polling/streaming, cancel, timeouts, endpoint failover (several URLs per owner).
- trace context (`traceparent`) in message metadata; MCP = agent<->tools, A2A = agent<->agent (documented in ARCHITECTURE.md).

## Phase 10 (observability)
- OpenTelemetry spans for task / plan / agent / LLM / tool / sandbox / verifier / approval wait via the existing `traced()` seam; OTLP -> collector; Langfuse generations (tokens, cost, latency) linked by task id; metrics; per-task summary API.

## Phase 11 (evals)
- Generalise `demo_target` into templates (n_plus_one, cache_bypass, serialization, missing_index, cache_ttl_zero, retry_storm) x red-herring variants x seeds -> 30+ eval tasks with ground truth.
- Graders: outcome (tests, benchmark), root-cause match, trace/policy, LLM-judge rubric (validated on a small labelled set). Metrics: success, tool-selection, planning, recovery, human-intervention, steps, cost, latency, regression vs previous run.

## Phase 12 (failure injection) / 13 (security) / 14 (dashboard) / 15 (docs+demo)
- See ROADMAP.md; built in that order.

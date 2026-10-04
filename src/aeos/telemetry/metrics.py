"""Metrics (OpenTelemetry): tool calls, LLM calls, tokens, cost, durations, approvals, retries,
replans, verifications. Instruments bind to whichever MeterProvider is configured (no-ops until
`setup_telemetry` runs), so call sites never need to care."""

from opentelemetry import metrics

_m = metrics.get_meter("aeos")

llm_calls = _m.create_counter("aeos.llm.calls", description="LLM calls")
llm_tokens = _m.create_counter("aeos.llm.tokens", description="LLM tokens (direction=input|output)")
llm_cost = _m.create_counter("aeos.llm.cost_usd", unit="USD", description="Estimated LLM spend")
llm_duration = _m.create_histogram("aeos.llm.duration_ms", unit="ms")
tool_calls = _m.create_counter("aeos.tool.calls", description="Tool calls")
tool_duration = _m.create_histogram("aeos.tool.duration_ms", unit="ms")
approvals = _m.create_counter("aeos.approvals", description="Approval requests and decisions")
step_retries = _m.create_counter("aeos.step.retries", description="Specialist step retries")
replans = _m.create_counter("aeos.replans", description="Plan revisions")
verifications = _m.create_counter("aeos.verifications", description="Verifier verdicts")
tasks_finished = _m.create_counter("aeos.tasks.finished", description="Tasks by final status")
task_duration = _m.create_histogram("aeos.task.duration_s", unit="s")

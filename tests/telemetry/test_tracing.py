import sys
from pathlib import Path

import pytest
from mcp.client.stdio import StdioServerParameters
from opentelemetry.trace import StatusCode

from aeos.llm.base import LLMResponse, Message, ToolCall, Usage
from aeos.llm.fake import FakeLLM, ScriptStep
from aeos.llm.traced import TracedLLM
from aeos.mcp_client import McpClient
from aeos.telemetry import (
    annotate,
    bind_task,
    current_trace_id,
    emit_span,
    inject_context,
    redact,
    register_secrets,
    remote_context,
    traced,
)


def by_name(exporter, prefix: str):  # type: ignore[no-untyped-def]
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


# ------------------------------------------------------------------ spans
def test_traced_records_attributes_nesting_and_task_binding(spans) -> None:  # type: ignore[no-untyped-def]
    with bind_task("t-1"), traced("outer", a=1) as outer, traced("inner", b="x"):
        outer.set("late", True)
        annotate(extra="y")
    inner, outer_span = by_name(spans, "inner")[0], by_name(spans, "outer")[0]
    assert inner.parent.span_id == outer_span.context.span_id  # type: ignore[union-attr]
    assert inner.context.trace_id == outer_span.context.trace_id
    want = {"a": 1, "late": True, "aeos.task.id": "t-1", "langfuse.session.id": "t-1"}
    assert want.items() <= dict(outer_span.attributes).items()  # type: ignore[arg-type]
    assert inner.attributes["b"] == "x" and inner.attributes["extra"] == "y"  # type: ignore[index]


def test_exceptions_mark_the_span_as_error_and_propagate(spans) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(ValueError), traced("boom"):
        raise ValueError("kaput")
    span = by_name(spans, "boom")[0]
    assert span.status.status_code == StatusCode.ERROR and "kaput" in (
        span.status.description or ""
    )
    assert span.attributes["error.type"] == "ValueError"  # type: ignore[index]
    assert any(e.name == "exception" for e in span.events)


def test_long_values_are_truncated_and_complex_values_serialised(spans) -> None:  # type: ignore[no-untyped-def]
    with traced("t", big="x" * 10_000, obj={"k": [1, 2]}):
        pass
    attrs = by_name(spans, "t")[0].attributes
    assert len(attrs["big"]) < 4100 and attrs["big"].endswith("[truncated]")  # type: ignore[index,union-attr]
    assert attrs["obj"] == '{"k": [1, 2]}'  # type: ignore[index]


def test_emit_span_uses_explicit_wall_clock_bounds(spans) -> None:  # type: ignore[no-untyped-def]
    with traced("root"):
        carrier = inject_context()
    emit_span("approval.wait", 1_000.0, 1_090.5, parent=carrier, who="alice")
    s = by_name(spans, "approval.wait")[0]
    assert (s.end_time - s.start_time) / 1e9 == pytest.approx(90.5)  # type: ignore[operator]
    assert (
        s.context.trace_id == by_name(spans, "root")[0].context.trace_id
    )  # joined the root's trace


def test_remote_context_continues_a_trace_from_another_process_or_time(spans) -> None:  # type: ignore[no-untyped-def]
    with traced("process A"):
        carrier = inject_context()
        trace_a = current_trace_id()
    assert current_trace_id() is None
    with remote_context(carrier), traced("process B"):  # e.g. after a worker restart
        assert current_trace_id() == trace_a
    b = by_name(spans, "process B")[0]
    assert (
        b.parent is not None
        and b.parent.trace_id == by_name(spans, "process A")[0].context.trace_id
    )


# ------------------------------------------------------------------ redaction
def test_secrets_never_reach_a_span(spans) -> None:  # type: ignore[no-untyped-def]
    register_secrets(["super-secret-key-value-123"])
    with traced(
        "leaky",
        prompt="use super-secret-key-value-123 and AIza" + "a" * 35,
        args={"api_key": "sk-live-abcdef123456", "note": "Bearer abcdefghijklmnopqrstuvwxyz0123"},
    ) as s:
        s.set("late", "ghp_" + "b" * 36)
    flat = " ".join(str(v) for v in by_name(spans, "leaky")[0].attributes.values())  # type: ignore[union-attr]
    for secret in ("super-secret-key-value-123", "sk-live-abcdef123456", "abcdefghijklmnopqrstuvwxyz0123",
                   "ghp_" + "b" * 36, "AIza" + "a" * 35):  # fmt: skip
        assert secret not in flat
    assert "[REDACTED]" in flat


def test_redact_keeps_the_key_name_for_key_value_secrets() -> None:
    assert redact("password=hunter2hunter2") == "password=[REDACTED]"
    assert redact('{"token": "abcdef123456"}') == '{"token": "[REDACTED]"}'
    assert redact("nothing sensitive here") == "nothing sensitive here"


# ------------------------------------------------------------------ LLM spans (Langfuse-compatible)
async def test_llm_call_span_carries_prompt_completion_tokens_and_cost(spans) -> None:  # type: ignore[no-untyped-def]
    reply = LLMResponse(content="hello", model="gemini-2.5-flash", finish_reason="stop",
                        usage=Usage(input_tokens=1000, output_tokens=200),
                        tool_calls=[ToolCall(id="c", name="search_logs", arguments={"q": "err"})])  # fmt: skip
    llm = TracedLLM(FakeLLM([ScriptStep(response=reply)]))
    with bind_task("t-9"):
        await llm.complete([Message(role="system", content="be brief"),
                            Message(role="user", content="what is wrong?")])  # fmt: skip
    (span,) = by_name(spans, "llm.call")
    a = span.attributes
    assert a["gen_ai.usage.input_tokens"] == 1000 and a["gen_ai.usage.output_tokens"] == 200  # type: ignore[index]
    assert a["gen_ai.response.model"] == "gemini-2.5-flash" and a["aeos.llm.cost_usd"] > 0  # type: ignore[index,operator]
    assert a["langfuse.observation.type"] == "generation" and a["langfuse.session.id"] == "t-9"  # type: ignore[index]
    assert "what is wrong?" in a["langfuse.observation.input"]  # type: ignore[operator]
    assert "search_logs" in a["langfuse.observation.output"]  # type: ignore[operator]
    assert '"input": 1000' in a["langfuse.observation.usage_details"]  # type: ignore[operator]


async def test_traced_llm_is_transparent_and_failures_are_recorded(spans) -> None:  # type: ignore[no-untyped-def]
    inner = FakeLLM([])
    llm = TracedLLM(inner)
    assert llm.remaining == 0  # attribute access falls through to the wrapped provider
    with pytest.raises(Exception):  # noqa: B017,PT011
        await llm.complete([Message(role="user", content="x")])
    assert by_name(spans, "llm.call")[0].status.status_code == StatusCode.ERROR


# ------------------------------------------------------------------ MCP boundary
async def test_trace_context_crosses_the_mcp_process_boundary(spans) -> None:  # type: ignore[no-untyped-def]
    server = Path(__file__).parents[1] / "mcp" / "echo_server.py"
    params = StdioServerParameters(command=sys.executable, args=[str(server)])
    async with McpClient(params, name="echo") as c:
        with traced("supervisor call"):
            mine = current_trace_id()
            res = await c.call("trace_id")
    assert res.data is not None and res.data["trace_id"] == mine  # server handled it in OUR trace

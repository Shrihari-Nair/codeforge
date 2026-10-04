"""Tiny MCP server used only to test McpClient (retry/timeout/error behaviour, trace context)."""

import asyncio
import pathlib

from mcp.server.mcpserver import MCPServer
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from aeos.mcp_servers.common import fail, serve, tool_kwargs
from aeos.telemetry import current_trace_id, setup_telemetry

mcp = MCPServer("echo")
# A real tracer (no network): the SDK's server middleware then creates a span whose parent is
# the client's span, taken from the request's `_meta` W3C trace context.
setup_telemetry("echo-server", exporter=InMemorySpanExporter(), metrics_enabled=False)


def _count(counter_file: str) -> None:
    with pathlib.Path(counter_file).open("a") as f:
        f.write("x\n")


@mcp.tool(**tool_kwargs("read"))
def trace_id() -> dict[str, str | None]:
    """Report the trace id this server handled the call under."""
    return {"trace_id": current_trace_id()}


@mcp.tool(**tool_kwargs("read"))
def echo(text: str) -> dict[str, str]:
    """Echo text back."""
    return {"text": text}


@mcp.tool(**tool_kwargs("read"))
def always_fails() -> dict[str, str]:
    """Always fails with a structured error."""
    fail("not_found", "nothing here", resource="x")


@mcp.tool(**tool_kwargs("read"))
async def slow_read(seconds: float, counter_file: str) -> dict[str, str]:
    """Sleep, counting invocations."""
    _count(counter_file)
    await asyncio.sleep(seconds)
    return {"ok": "yes"}


@mcp.tool(**tool_kwargs("write_reversible"))
async def slow_write(
    seconds: float, counter_file: str, idempotency_key: str = ""
) -> dict[str, str]:
    """Write-like tool that sleeps, counting invocations."""
    _count(counter_file)
    await asyncio.sleep(seconds)
    return {"ok": "yes"}


if __name__ == "__main__":
    serve(mcp, default_port=0)

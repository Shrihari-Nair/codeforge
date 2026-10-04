import sys
from pathlib import Path

import pytest
from mcp.client.stdio import StdioServerParameters

from aeos.mcp_client import McpClient, McpToolError, McpTransportError

SERVER = Path(__file__).parent / "echo_server.py"


@pytest.fixture
def params() -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=[str(SERVER)])


async def test_lists_tools_with_risk_metadata(params: StdioServerParameters) -> None:
    async with McpClient(params, name="echo") as c:
        tools = await c.list_tools()
    assert tools["echo"].risk == "read"
    assert tools["slow_write"].risk == "write_reversible"
    assert tools["echo"].output_schema is not None


async def test_call_returns_structured_data(params: StdioServerParameters) -> None:
    async with McpClient(params) as c:
        assert (await c.call("echo", {"text": "hi"})).data == {"text": "hi"}


async def test_structured_error_becomes_mcp_tool_error(params: StdioServerParameters) -> None:
    async with McpClient(params) as c:
        with pytest.raises(McpToolError) as ei:
            await c.call("always_fails")
    assert ei.value.code == "not_found"
    assert ei.value.details == {"resource": "x"}


async def test_invalid_arguments_are_a_tool_error_not_a_crash(
    params: StdioServerParameters,
) -> None:
    async with McpClient(params) as c:
        with pytest.raises(McpToolError):
            await c.call("echo", {"text": 123})


async def test_read_tool_timeout_is_retried(params: StdioServerParameters, tmp_path: Path) -> None:
    counter = tmp_path / "n"
    async with McpClient(params, max_retries=2, backoff=0.01) as c:
        with pytest.raises(McpTransportError):
            await c.call("slow_read", {"seconds": 5, "counter_file": str(counter)}, timeout=0.3)
    assert len(counter.read_text().splitlines()) == 3


async def test_write_without_idempotency_key_is_not_retried(
    params: StdioServerParameters, tmp_path: Path
) -> None:
    counter = tmp_path / "n"
    async with McpClient(params, max_retries=2, backoff=0.01) as c:
        with pytest.raises(McpTransportError):
            await c.call("slow_write", {"seconds": 5, "counter_file": str(counter)}, timeout=0.3)
    assert len(counter.read_text().splitlines()) == 1


async def test_write_with_idempotency_key_is_retried(
    params: StdioServerParameters, tmp_path: Path
) -> None:
    counter = tmp_path / "n"
    args = {"seconds": 5, "counter_file": str(counter), "idempotency_key": "k1"}
    async with McpClient(params, max_retries=2, backoff=0.01) as c:
        with pytest.raises(McpTransportError):
            await c.call("slow_write", args, timeout=0.3)
    assert len(counter.read_text().splitlines()) == 3


async def test_call_before_connect_fails_loudly(params: StdioServerParameters) -> None:
    with pytest.raises(McpTransportError, match="not connected"):
        await McpClient(params).call("echo", {"text": "x"})

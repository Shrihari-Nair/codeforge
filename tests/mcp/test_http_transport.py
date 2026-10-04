import asyncio
import socket
import subprocess
import sys
from collections.abc import Iterator

import pytest

from aeos.mcp_client import McpClient


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def web_http_server() -> Iterator[str]:
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "aeos.mcp_servers.web",
            "--transport",
            "streamable-http",
            "--port",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            if proc.poll() is not None:
                pytest.fail("server exited early")
            import time

            time.sleep(0.1)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_server_runs_standalone_over_streamable_http(web_http_server: str) -> None:
    async with McpClient(web_http_server, name="web-http") as c:
        tools = await c.list_tools()
        assert {"search_web", "fetch_page", "extract_information"} == set(tools)
        res = await c.call("search_web", {"query": "n+1 query"})
    assert res.data is not None and res.data["untrusted"] is True
    await asyncio.sleep(0)

"""Agent-side MCP client wrapper: connection, timeouts, retries, tracing.

Retry policy: only tools that are safe to repeat are retried on transport failures/timeouts:
`read` tools, and write tools called with a non-empty `idempotency_key`. Tool-level errors
(`isError`) are never retried; they surface as `McpToolError(code, message)`.
"""

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self

import anyio
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.shared.exceptions import MCPError

from aeos.telemetry import traced

_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD)", re.IGNORECASE)


class McpToolError(Exception):
    def __init__(self, tool: str, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(f"{tool}: [{code}] {message}")
        self.tool, self.code, self.message, self.details = tool, code, message, details or {}


class McpTransportError(Exception):
    """Connection/timeout failure after retries were exhausted (or not allowed)."""


@dataclass(frozen=True)
class ToolInfo:
    name: str
    description: str
    risk: str | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    idempotent: bool


@dataclass
class ToolResult:
    data: dict[str, Any] | None
    text: str = ""
    raw: Any = field(default=None, repr=False)


def stdio_server(
    module: str, *args: str, env: dict[str, str] | None = None
) -> StdioServerParameters:
    """Launch `python -m aeos.mcp_servers.<module>` as a child process.

    Passes AEOS_* config through, minus anything that looks like a secret, unless given in `env`.
    """
    passthrough = {
        k: v for k, v in os.environ.items() if k.startswith("AEOS_") and not _SECRET_ENV.search(k)
    }
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", f"aeos.mcp_servers.{module}", *args],
        env={**passthrough, **(env or {})},
    )


def _parse_error(tool: str, text: str) -> McpToolError:
    start = text.find("{")
    if start >= 0:
        try:
            body = json.loads(text[start:])
            if isinstance(body, dict) and "code" in body:
                details = {k: v for k, v in body.items() if k not in ("code", "message")}
                return McpToolError(tool, str(body["code"]), str(body.get("message", "")), details)
        except json.JSONDecodeError:
            pass
    return McpToolError(tool, "tool_error", text)


class McpClient:
    def __init__(
        self,
        target: StdioServerParameters | str,
        *,
        name: str = "mcp",
        default_timeout: float = 30.0,
        max_retries: int = 2,
        backoff: float = 0.2,
    ) -> None:
        self.name = name
        self._target = target
        self._timeout = default_timeout
        self._max_retries = max_retries
        self._backoff = backoff
        self._client: Client | None = None
        self._tools: dict[str, ToolInfo] | None = None
        self._stop = asyncio.Event()
        self._owner: asyncio.Task[None] | None = None

    async def _own_connection(self, ready: asyncio.Future[None]) -> None:
        """Owns the SDK client's cancel scopes so callers may use it from any task."""
        try:
            async with Client(self._target) as client:
                self._client = client
                ready.set_result(None)
                await self._stop.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
            else:
                raise
        finally:
            self._client = None

    async def __aenter__(self) -> Self:
        self._stop = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._owner = asyncio.create_task(self._own_connection(ready), name=f"mcp-{self.name}")
        await ready
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._owner is not None:
            await self._owner

    def _require(self) -> Client:
        if self._client is None:
            raise McpTransportError(f"{self.name}: client not connected")
        return self._client

    async def list_tools(self) -> dict[str, ToolInfo]:
        if self._tools is None:
            result = await self._require().list_tools()
            self._tools = {
                t.name: ToolInfo(
                    name=t.name,
                    description=t.description or "",
                    risk=((t.meta or {}).get("aeos") or {}).get("risk"),
                    input_schema=t.input_schema,
                    output_schema=t.output_schema,
                    idempotent=bool(t.annotations and t.annotations.idempotent_hint),
                )
                for t in result.tools
            }
        return self._tools

    async def _retryable(self, tool: str, arguments: dict[str, Any]) -> bool:
        info = (await self.list_tools()).get(tool)
        if info is None:
            return False
        return info.risk == "read" or bool(arguments.get("idempotency_key"))

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> ToolResult:
        arguments = arguments or {}
        limit = timeout if timeout is not None else self._timeout
        attempts = (self._max_retries if await self._retryable(tool, arguments) else 0) + 1
        last: Exception | None = None
        for attempt in range(attempts):
            with traced("mcp.call", server=self.name, tool=tool, attempt=attempt + 1):
                try:
                    async with asyncio.timeout(limit):
                        result = await self._require().call_tool(tool, arguments)
                except (
                    TimeoutError,
                    MCPError,
                    anyio.ClosedResourceError,
                    anyio.BrokenResourceError,
                    ConnectionError,
                ) as exc:
                    last = exc
                else:
                    text = "".join(getattr(c, "text", "") for c in result.content)
                    if result.is_error:
                        raise _parse_error(tool, text)
                    return ToolResult(result.structured_content, text, result)
            if attempt + 1 < attempts:
                await asyncio.sleep(self._backoff * (2**attempt))
        raise McpTransportError(f"{self.name}.{tool} failed after {attempts} attempt(s): {last!r}")

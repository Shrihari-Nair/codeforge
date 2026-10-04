"""Shared helpers for AEOS MCP servers: risk metadata, structured errors, CLI runner."""

import argparse
import json
from collections.abc import Sequence
from typing import Any, Literal, NoReturn

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

RiskLevel = Literal["read", "write_reversible", "write_irreversible"]
RISK_LEVELS: tuple[str, ...] = ("read", "write_reversible", "write_irreversible")


def tool_kwargs(
    risk: RiskLevel, *, idempotent: bool = False, open_world: bool = False
) -> dict[str, Any]:
    """Keyword args for `@mcp.tool(...)`: risk in `meta["aeos"]` plus standard hints.

    The risk policy (Phase 6) reads `meta["aeos"]["risk"]`; it is never inferred from a name.
    """
    return {
        "meta": {"aeos": {"risk": risk}},
        "annotations": ToolAnnotations(
            read_only_hint=risk == "read",
            destructive_hint=risk == "write_irreversible",
            idempotent_hint=idempotent or risk == "read",
            open_world_hint=open_world,
        ),
    }


def fail(code: str, message: str, **details: Any) -> NoReturn:
    """Raise a structured, client-visible tool error: `{"code","message",...}`."""
    raise ToolError(json.dumps({"code": code, "message": message, **details}))


def serve(mcp: MCPServer, *, default_port: int, argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog=mcp.name)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args(argv)
    from aeos.telemetry.setup import init_telemetry

    init_telemetry(f"aeos-mcp-{mcp.name.removeprefix('aeos-')}")
    if args.transport == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host=args.host, port=args.port)

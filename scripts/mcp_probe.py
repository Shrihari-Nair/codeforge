"""Generic MCP probe: list a server's tools (with risk levels) and optionally call one.

No agents involved; this is how a server is exercised standalone.

  uv run python scripts/mcp_probe.py database
  uv run python scripts/mcp_probe.py observability list_deployments '{"limit": 3}'
  uv run python scripts/mcp_probe.py --url http://127.0.0.1:8804/mcp web search_web '{"query": "x"}'
"""

import argparse
import asyncio
import json

from aeos.mcp_client import McpClient, McpToolError, stdio_server


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="connect over streamable HTTP instead of spawning via stdio")
    parser.add_argument("server", choices=["github", "observability", "database", "web"])
    parser.add_argument("tool", nargs="?")
    parser.add_argument("arguments", nargs="?", default="{}")
    args = parser.parse_args()
    target = args.url or stdio_server(args.server)
    async with McpClient(target, name=args.server) as client:
        tools = await client.list_tools()
        if not args.tool:
            for t in tools.values():
                print(f"{t.name:24} risk={t.risk or '?':18} {t.description.splitlines()[0]}")
            return 0
        try:
            result = await client.call(args.tool, json.loads(args.arguments))
        except McpToolError as exc:
            print(f"tool error [{exc.code}]: {exc.message}")
            return 1
        print(json.dumps(result.data, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

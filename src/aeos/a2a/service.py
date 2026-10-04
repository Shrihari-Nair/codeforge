"""Run ONE specialist as an independently deployed A2A service.

  uv run python -m aeos.a2a.service analytics --port 9101
  uv run aeos agent research --port 9102

The process owns its MCP connections (and, for the coding agent, sandbox access) and publishes an
Agent Card at /.well-known/agent-card.json.
"""

import argparse
import asyncio

import uvicorn

from a2a import types as t
from aeos.a2a.server import build_card, create_specialist_app
from aeos.core.config import Settings, get_settings
from aeos.llm.base import LLMProvider
from aeos.runtime import SPECIALIST_OWNERS, open_specialist

DESCRIPTIONS = {
    "analytics": (
        "Investigates latency regressions from logs, metrics, traces and read-only SQL.",
        [
            t.AgentSkill(
                id="investigate-regression",
                name="Investigate a metrics regression",
                description="Finds when and what changed after a deploy; rules out red herrings.",
                tags=["observability", "sql", "metrics"],
            )
        ],  # fmt: skip
    ),
    "research": (
        "Reads code, commit history and diffs (and docs) to find the change responsible.",
        [
            t.AgentSkill(
                id="find-culprit-commit",
                name="Find the responsible commit",
                description="Locates the commit and file that introduced a regression.",
                tags=["git", "code-search", "research"],
            )
        ],  # fmt: skip
    ),
    "coding": (
        "Implements and benchmarks fixes inside an isolated sandbox.",
        [
            t.AgentSkill(
                id="implement-fix",
                name="Implement a fix in a sandbox",
                description="Edits code, runs tests and benchmarks; returns a patch.",
                tags=["sandbox", "patch", "benchmark"],
            )
        ],  # fmt: skip
    ),
}


async def serve(
    owner: str,
    *,
    host: str,
    port: int,
    settings: Settings | None = None,
    llm: LLMProvider | None = None,
    public_url: str | None = None,
) -> None:
    s = settings or get_settings()
    description, skills = DESCRIPTIONS[owner]
    api_key = s.a2a_api_key.get_secret_value() if s.a2a_api_key else None
    async with open_specialist(owner, s, llm=llm) as specialist:
        card = build_card(owner=owner, description=description, skills=skills,
                          base_url=public_url or f"http://{host}:{port}")  # fmt: skip
        app = create_specialist_app(specialist, card, api_key=api_key)
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
        await server.serve()


def main(argv: list[str] | None = None) -> None:
    from aeos.core.logging import configure_logging
    from aeos.demo.trajectories import SCENARIOS

    parser = argparse.ArgumentParser(prog="aeos-agent")
    parser.add_argument("owner", choices=SPECIALIST_OWNERS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--public-url", help="URL advertised in the Agent Card")
    parser.add_argument("--replay", choices=sorted(SCENARIOS), help="scripted LLM (no API)")
    args = parser.parse_args(argv)
    configure_logging("WARNING")
    llm = SCENARIOS[args.replay]() if args.replay else None
    asyncio.run(
        serve(args.owner, host=args.host, port=args.port, llm=llm, public_url=args.public_url)
    )


if __name__ == "__main__":
    main()

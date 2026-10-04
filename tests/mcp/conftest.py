import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import psycopg
import pytest

from aeos.core.config import get_settings
from aeos.mcp_client import McpClient, stdio_server


@pytest.fixture
async def database_client(seeded_db: None) -> AsyncIterator[McpClient]:
    async with McpClient(stdio_server("database"), name="database") as c:
        yield c


@pytest.fixture
def gh(seeded_db: None, remote_template: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    """Factory for a github MCP client bound to a fresh copy of the bare remote."""
    from aeos.mcp_servers.github import store

    store.ensure_schema()
    with psycopg.connect(get_settings().database_url) as conn:
        conn.execute("TRUNCATE vcs.pull_requests RESTART IDENTITY")
        conn.execute("TRUNCATE vcs.branch_creations")
    remote = tmp_path / "checkout-service.git"
    shutil.copytree(remote_template, remote)

    def make() -> McpClient:
        return McpClient(
            stdio_server("github", env={"AEOS_GIT_REMOTE_PATH": str(remote)}), name="github"
        )

    make.remote = remote  # type: ignore[attr-defined]
    return make

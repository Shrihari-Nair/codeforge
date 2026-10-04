"""Real-GitHub publishing, against a fake GitHub: a bare repo for pushes + a stub REST API."""

import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aeos.mcp_client import McpClient, McpToolError, stdio_server
from tests.mcp.test_github import fix_patch

TOKEN = "ghp_SECRETSECRETSECRET1234567890"


class FakeGitHubApi(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.pulls: list[dict[str, Any]] = []
        self.auth_seen: list[str | None] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    server: FakeGitHubApi

    def log_message(self, *a: object) -> None:
        pass

    def _send(self, code: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        self.server.auth_seen.append(self.headers.get("authorization"))
        if self.path.rstrip("/") == "/repos/owner/repo":
            self._send(200, {"default_branch": "main"})
            return
        head = self.path.split("head=")[1].split("&")[0].replace("%3A", ":").replace("%2F", "/")
        found = [p for p in self.server.pulls if f"owner:{p['head']}" == head]
        self._send(200, found)

    def do_POST(self) -> None:  # noqa: N802
        self.server.auth_seen.append(self.headers.get("authorization"))
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        pr = {**body, "number": 100 + len(self.server.pulls),
              "html_url": f"https://github.com/owner/repo/pull/{100 + len(self.server.pulls)}"}  # fmt: skip
        self.server.pulls.append(pr)
        self._send(201, pr)


@pytest.fixture
def fake_github(tmp_path: Path):  # type: ignore[no-untyped-def]
    api = FakeGitHubApi()
    bare = tmp_path / "github.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    yield api, bare
    api.shutdown()


def _branches(bare: Path) -> set[str]:
    out = subprocess.run(
        ["git", "-C", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads"],
        capture_output=True, text=True, check=True,
    ).stdout  # fmt: skip
    return set(out.split())


async def test_approved_pr_is_mirrored_to_github_idempotently(gh, fake_github) -> None:  # type: ignore[no-untyped-def]
    api, bare = fake_github
    env = {
        "AEOS_GIT_REMOTE_PATH": str(gh.remote),
        "AEOS_GITHUB_PUBLISH": "true",
        "AEOS_GITHUB_REPO": "owner/repo",
        "AEOS_GITHUB_TOKEN": TOKEN,
        "AEOS_GITHUB_BASE_BRANCH": "aeos-demo",
        "AEOS_GITHUB_API_URL": api.url,
        "AEOS_GITHUB_GIT_URL": str(bare),
    }
    async with McpClient(stdio_server("github", env=env), name="github") as c:
        await c.call("create_branch", {"name": "aeos/fix-1", "idempotency_key": "b1"})
        await c.call("commit_patch", {"branch": "aeos/fix-1", "patch": await fix_patch(c),
                                      "message": "fix", "idempotency_key": "c1"})  # fmt: skip
        # nothing has reached "GitHub" before the PR call (which the policy gates on approval)
        assert _branches(bare) == set()
        args = {"title": "Fix N+1", "body": "b", "head": "aeos/fix-1", "idempotency_key": "p1"}
        pr1 = (await c.call("create_pull_request", args)).data
        pr2 = (await c.call("create_pull_request", args)).data  # replay
        got = (await c.call("get_pull_request", {"number": 100})).data

    assert pr1 is not None and pr2 is not None and got is not None
    assert pr1["url"] == "https://github.com/owner/repo/pull/100" and pr1["number"] == 100
    assert pr2["replayed"] is True and pr2["url"] == pr1["url"]
    assert got["url"] == pr1["url"]
    assert len(api.pulls) == 1  # the replay did not open a second PR
    assert api.pulls[0]["base"] == "aeos-demo" and api.pulls[0]["head"] == "aeos/fix-1"
    assert _branches(bare) == {"aeos-demo", "aeos/fix-1"}  # never main
    assert all(a == f"Bearer {TOKEN}" for a in api.auth_seen)


async def test_publish_failure_is_reported_without_leaking_the_token(gh, fake_github) -> None:  # type: ignore[no-untyped-def]
    api, _ = fake_github
    env = {
        "AEOS_GIT_REMOTE_PATH": str(gh.remote),
        "AEOS_GITHUB_PUBLISH": "true",
        "AEOS_GITHUB_REPO": "owner/repo",
        "AEOS_GITHUB_TOKEN": TOKEN,
        "AEOS_GITHUB_API_URL": api.url,
        "AEOS_GITHUB_GIT_URL": "/nonexistent/path.git",
    }
    async with McpClient(stdio_server("github", env=env), name="github") as c:
        await c.call("create_branch", {"name": "aeos/fix-1", "idempotency_key": "b1"})
        await c.call("commit_patch", {"branch": "aeos/fix-1", "patch": await fix_patch(c),
                                      "message": "fix", "idempotency_key": "c1"})  # fmt: skip
        with pytest.raises(McpToolError) as ei:
            await c.call("create_pull_request", {"title": "t", "body": "b", "head": "aeos/fix-1",
                                                 "idempotency_key": "p1"})  # fmt: skip
    assert ei.value.code == "github_push_failed"
    assert TOKEN not in str(ei.value) and not api.pulls


def test_main_is_never_an_allowed_github_base() -> None:
    from aeos.mcp_servers.github.remote import validate_branch_name

    for bad in ("main", "master", "a..b"):
        with pytest.raises(Exception):  # noqa: B017, PT011
            validate_branch_name(bad)

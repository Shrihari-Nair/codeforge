"""A "change" task end to end: real MCP servers, sandbox, verifier, approval and PR mirroring,
against a fake GitHub (a bare git repo + a stub REST API) and a fake npm registry."""

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import SecretStr

from aeos.core.config import get_settings
from aeos.core.events import ListSink
from aeos.demo.trajectories import turn
from aeos.graph.build import run_task
from aeos.hitl import auto_approver
from aeos.llm.fake import FakeLLM, RoutedFakeLLM
from aeos.runtime import open_runtime
from aeos.sandbox import NpmRegistry
from tests.mcp.test_github_remote import FakeGitHubApi

pytestmark = pytest.mark.docker

PKG = {
    "name": "app",
    "version": "1.0.0",
    "dependencies": {"ws": "^8.14.2", "uuid": "^9.0.1"},
}
LOCK = {
    "name": "app",
    "version": "1.0.0",
    "lockfileVersion": 3,
    "packages": {
        "": {
            "name": "app",
            "version": "1.0.0",
            "dependencies": {"ws": "^8.14.2", "uuid": "^9.0.1"},
        },
        "node_modules/ws": {"version": "8.14.2"},
        "node_modules/uuid": {"version": "9.0.1"},
    },
}
SERVER = 'const WebSocket = require("ws");\nconst wss = new WebSocket.Server({ port: 8080 });\n'


class FakeNpm(NpmRegistry):
    def __init__(self) -> None:  # no Docker, no network
        pass

    def info(self, package: str) -> dict:  # type: ignore[type-arg]
        return {"name": package, "dist_tags": {"latest": "8.18.0"}, "recent_versions": ["8.18.0"]}

    def update(self, files: dict[str, str], package: str, version: str) -> dict[str, str]:
        pkg, lock = json.loads(files["package.json"]), json.loads(files["package-lock.json"])
        pkg["dependencies"][package] = f"^{version}"
        lock["packages"][""]["dependencies"][package] = f"^{version}"
        lock["packages"][f"node_modules/{package}"]["version"] = version
        return {
            "package.json": json.dumps(pkg, indent=2) + "\n",
            "package-lock.json": json.dumps(lock, indent=2) + "\n",
        }


def _git(cwd: Path, *args: str) -> None:
    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd),
    }  # fmt: skip
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=env)


def _out(bare: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(bare), *args], capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def fake_github(tmp_path: Path):  # type: ignore[no-untyped-def]
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "package.json").write_text(json.dumps(PKG, indent=2) + "\n")
    (work / "package-lock.json").write_text(json.dumps(LOCK, indent=2) + "\n")
    (work / "src" / "server.js").write_text(SERVER)
    _git(work, "init", "-q", "-b", "main")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "initial")
    bare = tmp_path / "github.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    api = FakeGitHubApi()
    yield api, bare
    api.shutdown()


def _llm(*, break_syntax: bool = False) -> RoutedFakeLLM:
    plan = turn(("submit", {
        "rationale": "research then code",
        "steps": [
            {"id": "s1", "owner": "research", "description": "find where ws is used"},
            {"id": "s2", "owner": "coding", "depends_on": ["s1"], "description": "bump ws"},
        ],
    }))  # fmt: skip
    research = [
        turn(("get_repository", {})),
        turn(("search_code", {"pattern": "ws"})),
        turn(("npm_registry_info", {"package": "ws"})),
        turn(("finish", {
            "summary": "ws is declared in package.json and used in src/server.js",
            "current_state": "ws ^8.14.2",
            "relevant_files": [{"path": "package.json", "why": "bump ws"},
                               {"path": "package-lock.json", "why": "regenerate"}],
            "proposed_changes": ["ws -> 8.18.0"],
            "risks": ["none for the Server API"],
        })),
    ]  # fmt: skip
    edits = [turn(("npm_update_dependency", {"package": "ws", "version": "8.18.0"}))]
    if break_syntax:
        edits.append(turn(("sandbox_write_file", {"path": "src/server.js", "content": "const ("})))
    coding = [
        turn(("sandbox_read_file", {"path": "package.json"})),
        *edits,
        turn(("sandbox_exec", {"argv": ["node", "--check", "src/server.js"]})),
        turn(("finish", {
            "summary": "bumped ws to 8.18.0", "pr_title": "chore: bump ws to 8.18.0",
            "approach": "npm_update_dependency", "files_changed": ["package.json"],
            "checks_run": ["node --check src/server.js"],
            "limitations": ["no test suite in this repo"],
        })),
    ]  # fmt: skip
    judge = turn(("submit", {"addresses_root_cause": True, "covers_requirements": True,
                             "rationale": "ok"}))  # fmt: skip
    return RoutedFakeLLM({
        "ROLE: supervisor-planner": FakeLLM([plan]),
        "ROLE: research": FakeLLM(research),
        "ROLE: coding": FakeLLM(coding),
        "ROLE: verifier-judge": FakeLLM([judge]),
    })  # fmt: skip


def _settings(tmp_path: Path, api: FakeGitHubApi, bare: Path):  # type: ignore[no-untyped-def]
    return get_settings().model_copy(update={
        "github_repo": "owner/repo", "github_token": SecretStr("ghp_" + "x" * 36),
        "github_git_url": str(bare), "github_api_url": api.url, "github_publish": True,
        "target_enabled": True, "target_mirror_dir": str(tmp_path / "mirror"),
        "web_corpus_dir": "demo_target/web_corpus",
    })  # fmt: skip


async def test_change_task_updates_dependency_and_opens_real_pr(
    fake_github, tmp_path, seeded_db, remote_template
) -> None:  # type: ignore[no-untyped-def]
    api, bare = fake_github
    s = _settings(tmp_path, api, bare).model_copy(update={"git_remote_path": str(remote_template)})
    llm = _llm()
    async with open_runtime(s, llm=llm, sink=ListSink(), npm=FakeNpm()) as rt:
        assert rt.target_error is None and rt.target_branch == "main"
        state = await run_task(
            rt, "update ws to the latest version and raise a PR", approver=auto_approver,
            kind="change",
        )  # fmt: skip
    assert state.status == "completed", state.error
    assert state.kind == "change" and all(v == 0 for v in llm.remaining.values())
    assert [s.owner for s in state.plan.steps] == ["research", "coding", "verifier", "supervisor"]  # type: ignore[union-attr]
    v = state.verifications[-1]
    assert v.status == "passed" and v.gates["syntax_ok"] and v.gates["lockfile_consistent"]
    assert {f["path"] for f in v.metrics["files"]} == {"package.json", "package-lock.json"}
    # the real PR: opened against main of the (fake) GitHub repo, from an aeos/* branch
    pr = state.artifacts.pull_request
    assert pr and pr["url"] == "https://github.com/owner/repo/pull/100"
    assert api.pulls[0]["base"] == "main" and api.pulls[0]["head"] == f"aeos/{state.task_id}-v1"
    assert "chore: bump ws to 8.18.0" in api.pulls[0]["title"]
    assert (
        "Where changes were made" in api.pulls[0]["body"]
        and "no automated test suite" in api.pulls[0]["body"]
    )
    heads = _out(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads").split()
    assert sorted(heads) == sorted(["main", f"aeos/{state.task_id}-v1"])  # main itself untouched
    shown = _out(bare, "show", f"aeos/{state.task_id}-v1:package.json")
    assert '"ws": "^8.18.0"' in shown

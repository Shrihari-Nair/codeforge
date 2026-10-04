"""Materialise history.py into a real git repo with reproducible SHAs.

Git runs only inside the build directory, never on the outer project, and
ignores the user's/system git config.
"""

import io
import json
import os
import shutil
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from demo_target.history import COMMITS, Commit

ROOT = Path(__file__).resolve().parent
BUILD_DIR = ROOT / "build"
REPO_DIR = BUILD_DIR / "checkout-service"
COMMITS_JSON = BUILD_DIR / "commits.json"


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    full_env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        **(env or {}),
    }
    out = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        env=full_env,
    )
    return out.stdout.strip()


def build(dest: Path = REPO_DIR, commits: Sequence[Commit] | None = None) -> list[dict[str, Any]]:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _git(dest, "init", "-q", "-b", "main")
    info: list[dict[str, Any]] = []
    for commit in commits if commits is not None else COMMITS:
        for rel, content in commit.files.items():
            path = dest / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        name, email = commit.author
        env = {
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": commit.date,
            "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email,
            "GIT_COMMITTER_DATE": commit.date,
        }
        _git(dest, "add", "-A")
        _git(dest, "-c", "commit.gpgsign=false", "commit", "-q", "-m", commit.message, env=env)
        info.append(
            {
                "key": commit.key,
                "sha": _git(dest, "rev-parse", "HEAD"),
                "subject": commit.message,
                "author": name,
                "date": commit.date,
            }
        )
    if dest == REPO_DIR:
        COMMITS_JSON.write_text(json.dumps(info, indent=2))
    return info


def export_tree(repo: Path, sha: str, dest: Path) -> Path:
    """Extract the tree at `sha` into `dest` (no .git), for sandbox-style runs."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha],
        capture_output=True,
        check=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    return dest


def build_remote(
    source: Path = REPO_DIR, dest: Path = BUILD_DIR / "remote" / "checkout-service.git"
) -> Path:
    """Bare clone of the demo repo, used as the 'GitHub' remote by the github MCP server."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(dest.parent, "clone", "-q", "--bare", str(source), str(dest))
    return dest


def load_commits() -> list[dict[str, Any]]:
    if not COMMITS_JSON.exists():
        build()
    data: list[dict[str, Any]] = json.loads(COMMITS_JSON.read_text())
    return data


if __name__ == "__main__":
    for c in build():
        print(c["sha"][:10], c["date"][:10], c["subject"])
    print("remote:", build_remote())


TEMPLATES_DIR = BUILD_DIR / "templates"


def template_paths(template_id: str) -> tuple[Path, Path, Path]:
    """(working repo, bare remote, commits.json) for a template."""
    root = TEMPLATES_DIR / template_id
    return root / "checkout-service", root / "remote.git", root / "commits.json"


def build_template(template_id: str, *, force: bool = False) -> list[dict[str, Any]]:
    """Build (once) the repo + bare remote for a template; reuse if already built."""
    from demo_target.templates import TEMPLATES

    repo, remote, commits_json = template_paths(template_id)
    if not force and commits_json.exists() and remote.exists():
        data: list[dict[str, Any]] = json.loads(commits_json.read_text())
        return data
    info = build(repo, TEMPLATES[template_id].commits)
    build_remote(repo, remote)
    commits_json.write_text(json.dumps(info, indent=2))
    return info

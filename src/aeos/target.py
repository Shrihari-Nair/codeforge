"""Local bare mirror of the real target repository (read side of "change" tasks).

Agents never talk to GitHub directly: they read and write a local bare mirror, exactly like the
demo repo. The mirror's default branch is refreshed from GitHub before each task; the agents' own
`aeos/*` branches live only in the mirror until a human approves a PR (see mcp_servers/github/
remote.py, which is the only code that pushes).
"""

import base64
import subprocess
from pathlib import Path

import httpx

from aeos.core.config import Settings
from aeos.mcp_servers.github.backend import GitError
from aeos.mcp_servers.github.remote import parse_repo


def _auth_env(token: str) -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/nonexistent",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
    }


def _git(args: list[str], token: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, timeout=180, check=False,
        env=_auth_env(token),
    )  # fmt: skip
    if proc.returncode != 0:
        err = proc.stderr.decode(errors="replace").replace(token, "***").strip()[:400]
        raise GitError("git_error", err)
    return proc.stdout.decode(errors="replace").strip()


def default_branch(s: Settings) -> str:
    """The repository's default branch (what PRs target)."""
    assert s.github_repo and s.github_token
    if s.github_git_url:  # tests: a local "GitHub"
        return "main"
    r = httpx.get(
        f"{s.github_api_url.rstrip('/')}/repos/{parse_repo(s.github_repo)}",
        headers={
            "Authorization": f"Bearer {s.github_token.get_secret_value()}",
            "Accept": "application/vnd.github+json",
        },
        timeout=30,
    )
    if r.status_code != 200:
        raise GitError("github_error", f"cannot read repository: HTTP {r.status_code}")
    return str(r.json()["default_branch"])


def mirror_path(s: Settings) -> Path:
    assert s.github_repo
    return Path(s.target_mirror_dir).resolve() / (
        parse_repo(s.github_repo).replace("/", "__") + ".git"
    )


def sync_mirror(s: Settings) -> tuple[Path, str, str]:
    """Create or refresh the mirror. Returns (path, default branch, head sha).

    Only the default branch is fetched, forced: local `aeos/*` branches are never touched.
    """
    assert s.github_repo and s.github_token
    token = s.github_token.get_secret_value()
    url = s.github_git_url or f"https://github.com/{parse_repo(s.github_repo)}.git"
    branch = default_branch(s)
    path = mirror_path(s)
    if not (path / "HEAD").exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(["init", "--bare", "-q", "-b", branch, str(path)], token)
    refspec = f"+refs/heads/{branch}:refs/heads/{branch}"
    _git(["fetch", "--quiet", "--no-tags", url, refspec], token, cwd=path)
    sha = _git(["rev-parse", f"refs/heads/{branch}"], token, cwd=path)
    return path, branch, sha

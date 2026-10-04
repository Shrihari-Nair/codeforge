"""Optional mirror of an approved pull request to real GitHub.

The agents still work against the local bare remote. Only when `create_pull_request` runs (which
the risk policy gates behind human approval) are the `aeos/*` head branch and the demo base branch
pushed to GitHub and a real PR opened. Nothing else ever reaches GitHub: no merge, no other
branches, no force pushes.
"""

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from aeos.core.config import Settings
from aeos.mcp_servers.github.backend import GitError, validate_branch

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def parse_repo(value: str) -> str:
    v = value.strip().removesuffix("/").removesuffix(".git")
    v = re.sub(r"^https?://github\.com/", "", v)
    if not _REPO_RE.match(v):
        raise GitError("invalid_argument", "AEOS_GITHUB_REPO must look like 'owner/name'")
    return v


@dataclass
class RemotePR:
    number: int
    url: str


class GitHubRemote:
    def __init__(self, s: Settings) -> None:
        assert s.github_repo and s.github_token
        self.repo = parse_repo(s.github_repo)
        self._token = s.github_token.get_secret_value()
        # "@default": PRs target the repo's own default branch (real target repo). Otherwise a
        # dedicated demo branch that we create on first use (never main/master).
        self.use_default_base = s.github_base_branch == "@default"
        self.base_branch = (
            "" if self.use_default_base else validate_branch_name(s.github_base_branch)
        )
        self.api = s.github_api_url.rstrip("/")
        self.git_url = s.github_git_url or f"https://github.com/{self.repo}.git"

    @classmethod
    def from_settings(cls, s: Settings) -> "GitHubRemote | None":
        return cls(s) if s.github_publish and s.github_repo and s.github_token else None

    # ------------------------------------------------------------------ helpers
    def _scrub(self, text: str) -> str:
        return text.replace(self._token, "***")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _push(self, local: Path, src: str, dst: str) -> None:
        """Push `src` (a ref in the local bare repo) to `refs/heads/<dst>` on GitHub.

        Never forced: a diverged remote branch is an error, not an overwrite. The token travels in
        an environment-supplied header, never in the URL or argv.
        """
        import base64

        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        proc = subprocess.run(
            ["git", "push", self.git_url, f"{src}:refs/heads/{dst}"],
            cwd=local,
            capture_output=True,
            timeout=120,
            check=False,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/nonexistent",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
            },
        )
        if proc.returncode != 0:
            err = self._scrub(proc.stderr.decode(errors="replace").strip())[:500]
            raise GitError("github_push_failed", f"push to {dst!r} failed: {err}")

    def _api(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            return httpx.request(
                method, f"{self.api}{path}", headers=self._headers(), timeout=30, **kw
            )
        except httpx.HTTPError as exc:
            raise GitError("github_unreachable", self._scrub(str(exc))) from exc

    # ------------------------------------------------------------------ operations
    def _resolve_default_base(self) -> str:
        r = self._api("GET", f"/repos/{self.repo}")
        if r.status_code != 200:
            raise GitError("github_error", f"reading the repository failed: HTTP {r.status_code}")
        return str(r.json()["default_branch"])

    def ensure_base(self, local: Path, local_ref: str) -> None:
        """Demo mode: make the base branch exist on GitHub (fast-forward only, created once).
        Real-repo mode: the base is the repo's own default branch; nothing is pushed to it."""
        if self.use_default_base:
            self.base_branch = self.base_branch or self._resolve_default_base()
            return
        self._push(local, local_ref, self.base_branch)

    def find_open_pr(self, head: str) -> RemotePR | None:
        owner = self.repo.split("/")[0]
        r = self._api(
            "GET",
            f"/repos/{self.repo}/pulls",
            params={"head": f"{owner}:{head}", "base": self.base_branch, "state": "open"},
        )
        if r.status_code != 200:
            raise GitError("github_error", f"listing pull requests failed: HTTP {r.status_code}")
        items = r.json()
        return RemotePR(items[0]["number"], items[0]["html_url"]) if items else None

    def open_pr(self, local: Path, local_base: str, head: str, title: str, body: str) -> RemotePR:
        validate_branch(head)  # only aeos/* ever leaves this machine
        if self.use_default_base and not self.base_branch:
            self.base_branch = self._resolve_default_base()
        existing = self.find_open_pr(head)
        if existing:
            return existing
        self.ensure_base(local, local_base)
        self._push(local, f"refs/heads/{head}", head)
        r = self._api(
            "POST",
            f"/repos/{self.repo}/pulls",
            json={"title": title, "body": body, "head": head, "base": self.base_branch},
        )
        if r.status_code not in (200, 201):
            detail = self._scrub(r.text)[:300]
            raise GitError(
                "github_error", f"creating the pull request failed: HTTP {r.status_code} {detail}"
            )
        data = r.json()
        return RemotePR(data["number"], data["html_url"])


def validate_branch_name(name: str) -> str:
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", name)
        or name in ("main", "master")
        or ".." in name
    ):
        raise GitError("invalid_argument", "AEOS_GITHUB_BASE_BRANCH must not be main/master")
    return name

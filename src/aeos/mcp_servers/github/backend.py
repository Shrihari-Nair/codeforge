"""Git backend for the GitHub-like MCP server: a local bare repository.

The tool surface in server.py is backend-agnostic, so a real GitHub implementation can be
slotted in later. All git invocations ignore user/system git config and never take an
unvalidated string as an option.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

BRANCH_NAMESPACE = "aeos/"
MAX_FILE_BYTES = 200_000
MAX_PATCH_BYTES = 500_000
MAX_BLAME_LINES = 200
IDEMPOTENCY_TRAILER = "AEOS-Idempotency-Key"

_REF_RE = re.compile(r"^[A-Za-z0-9._/@^~-]{1,200}$")
_BRANCH_RE = re.compile(r"^aeos/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class GitError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class Branch(BaseModel):
    name: str
    sha: str


class Repository(BaseModel):
    name: str
    default_branch: str
    head_sha: str
    commit_count: int
    branches: list[Branch]


class CodeMatch(BaseModel):
    path: str
    line: int
    text: str


class CommitInfo(BaseModel):
    sha: str
    author: str
    date: str
    subject: str


class BlameLine(BaseModel):
    line: int
    sha: str
    author: str
    summary: str
    text: str


class FileChange(BaseModel):
    path: str
    additions: int | None
    deletions: int | None


def validate_ref(ref: str) -> str:
    if not _REF_RE.match(ref) or ref.startswith("-") or ".." in ref:
        raise GitError("invalid_argument", f"invalid ref {ref!r}")
    return ref


def validate_path(path: str) -> str:
    if (
        not path
        or path.startswith(("/", "-", ":"))
        or "\x00" in path
        or any(part in ("..", ".git") for part in path.split("/"))
    ):
        raise GitError("invalid_argument", f"invalid path {path!r}")
    return path


def validate_branch(name: str) -> str:
    if (
        not _BRANCH_RE.match(name)
        or ".." in name
        or name.endswith((".lock", "."))
        or len(name) > 120
    ):
        raise GitError(
            "invalid_argument",
            f"branch must be '{BRANCH_NAMESPACE}<name>' (letters, digits, . _ - /); got {name!r}",
        )
    return name


def validate_key(key: str) -> str:
    if not _KEY_RE.match(key):
        raise GitError("invalid_argument", "idempotency_key must be 1-128 chars of [A-Za-z0-9._:-]")
    return key


class GitBackend:
    def __init__(self, remote: Path) -> None:
        self.remote = remote
        self.name = remote.name.removesuffix(".git")
        if not (remote / "HEAD").exists():
            raise GitError("not_found", f"no git repository at {remote}")

    # ------------------------------------------------------------------ plumbing
    @staticmethod
    def _env() -> dict[str, str]:
        return {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        input: bytes | None = None,  # noqa: A002
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd or self.remote,
            input=input,
            capture_output=True,
            env={**self._env(), **(env or {})},
            timeout=60,
            check=False,
        )
        if check and proc.returncode != 0:
            raise GitError("git_error", proc.stderr.decode(errors="replace").strip()[:500])
        return proc

    def _out(self, *args: str) -> str:
        return self._git(*args).stdout.decode(errors="replace").strip()

    def resolve(self, ref: str) -> str:
        validate_ref(ref)
        proc = self._git(
            "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}", check=False
        )
        if proc.returncode != 0:
            raise GitError("not_found", f"unknown ref {ref!r}")
        return proc.stdout.decode().strip()

    def default_branch(self) -> str:
        return self._out("symbolic-ref", "--short", "HEAD")

    def _branch_sha(self, name: str) -> str | None:
        proc = self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False)
        return proc.stdout.decode().strip() if proc.returncode == 0 else None

    # ------------------------------------------------------------------ reads
    def get_repository(self) -> Repository:
        branches = [
            Branch(name=n, sha=s)
            for n, s in (
                line.split(" ", 1)
                for line in self._out(
                    "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads"
                ).splitlines()
            )
        ]
        default = self.default_branch()
        return Repository(
            name=self.name,
            default_branch=default,
            head_sha=self.resolve(default),
            commit_count=int(self._out("rev-list", "--count", default)),
            branches=branches,
        )

    def search_code(
        self, pattern: str, ref: str, path: str | None, regex: bool, limit: int
    ) -> tuple[list[CodeMatch], bool]:
        sha = self.resolve(ref)
        args = ["grep", "-n", "-I", "--no-color"]
        args.append("-E" if regex else "-F")
        args += ["-e", pattern, sha, "--"]
        if path:
            args.append(validate_path(path))
        proc = self._git(*args, check=False)
        if proc.returncode not in (0, 1):
            raise GitError("invalid_argument", proc.stderr.decode(errors="replace").strip()[:200])
        matches: list[CodeMatch] = []
        for line in proc.stdout.decode(errors="replace").splitlines():
            rest = line.removeprefix(f"{sha}:")
            file, _, tail = rest.partition(":")
            lineno, _, text = tail.partition(":")
            if lineno.isdigit():
                matches.append(CodeMatch(path=file, line=int(lineno), text=text[:300]))
        return matches[:limit], len(matches) > limit

    def get_file(self, path: str, ref: str) -> tuple[str, str, int, bool, bool]:
        """Returns (sha, content, size, truncated, binary)."""
        sha = self.resolve(ref)
        validate_path(path)
        kind = self._git("cat-file", "-t", f"{sha}:{path}", check=False)
        if kind.returncode != 0:
            raise GitError("not_found", f"{path!r} does not exist at {ref!r}")
        if kind.stdout.decode().strip() != "blob":
            raise GitError("invalid_argument", f"{path!r} is not a file")
        data = self._git("cat-file", "blob", f"{sha}:{path}").stdout
        if b"\x00" in data[:8000]:
            return sha, "", len(data), False, True
        truncated = len(data) > MAX_FILE_BYTES
        return sha, data[:MAX_FILE_BYTES].decode(errors="replace"), len(data), truncated, False

    def get_blame(
        self, path: str, ref: str, start: int | None, end: int | None
    ) -> tuple[str, list["BlameLine"], bool]:
        """Which commit last changed each line, at `ref`. Returns (sha, lines, truncated)."""
        sha = self.resolve(ref)
        validate_path(path)
        args = ["blame", "--line-porcelain"]
        if start is not None:
            args += ["-L", f"{start},{end if end is not None else start}"]
        proc = self._git(*args, sha, "--", path, check=False)
        if proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            raise GitError("not_found" if "no such path" in err else "invalid_argument", err[:200])
        lines: list[BlameLine] = []
        cur: dict[str, str] = {}
        for raw in proc.stdout.decode(errors="replace").splitlines():
            if raw.startswith("\t"):
                lines.append(
                    BlameLine(
                        line=int(cur["line"]),
                        sha=cur["sha"],
                        author=cur.get("author", ""),
                        summary=cur.get("summary", ""),
                        text=raw[1:][:200],
                    )
                )
                cur = {k: v for k, v in cur.items() if k in ("sha",)}  # next group re-announces
            elif re.match(r"^[0-9a-f]{40} \d+ \d+", raw):
                parts = raw.split()
                cur = {"sha": parts[0], "line": parts[2]}
            elif raw.startswith("author "):
                cur["author"] = raw[7:]
            elif raw.startswith("summary "):
                cur["summary"] = raw[8:]
        truncated = len(lines) > MAX_BLAME_LINES
        return sha, lines[:MAX_BLAME_LINES], truncated

    def list_commits(self, ref: str, path: str | None, limit: int) -> list[CommitInfo]:
        sha = self.resolve(ref)
        args = ["log", f"-n{limit}", "--format=%H%x1f%an%x1f%aI%x1f%s", sha, "--"]
        if path:
            args.append(validate_path(path))
        commits = []
        for line in self._out(*args).splitlines():
            h, author, date, subject = line.split("\x1f")
            commits.append(CommitInfo(sha=h, author=author, date=date, subject=subject))
        return commits

    def get_diff(
        self, base: str, head: str, path: str | None, max_bytes: int
    ) -> tuple[str, str, list[FileChange], str, bool]:
        b, h = self.resolve(base), self.resolve(head)
        tail = ["--", validate_path(path)] if path else []
        stat = self._out("diff", "--numstat", b, h, *tail)
        files = []
        for line in stat.splitlines():
            add, dele, fpath = line.split("\t", 2)
            files.append(
                FileChange(
                    path=fpath,
                    additions=int(add) if add.isdigit() else None,
                    deletions=int(dele) if dele.isdigit() else None,
                )
            )
        raw = self._git("diff", "--no-color", "--no-ext-diff", b, h, *tail).stdout
        truncated = len(raw) > max_bytes
        return b, h, files, raw[:max_bytes].decode(errors="replace"), truncated

    # ------------------------------------------------------------------ writes
    def create_branch(self, name: str, from_ref: str) -> tuple[str, bool]:
        """Returns (sha, created). Replays with the same name/base are no-ops."""
        validate_branch(name)
        sha = self.resolve(from_ref)
        existing = self._branch_sha(name)
        if existing is not None:
            if existing == sha:
                return sha, False
            raise GitError("conflict", f"branch {name!r} already exists at a different commit")
        self._git("branch", "--no-track", "--", name, sha)
        return sha, True

    def _find_keyed_commit(self, branch: str, key: str) -> str | None:
        out = self._out(
            "log",
            "-n50",
            "--format=%H",
            "-F",
            f"--grep={IDEMPOTENCY_TRAILER}: {key}",
            f"refs/heads/{branch}",
        )
        return out.splitlines()[0] if out else None

    def commit_patch(
        self, branch: str, patch: str, message: str, key: str
    ) -> tuple[str, int, bool]:
        """Apply a unified diff on `branch` and push. Returns (commit_sha, files, replayed)."""
        validate_branch(branch)
        validate_key(key)
        if self._branch_sha(branch) is None:
            raise GitError("not_found", f"branch {branch!r} does not exist")
        replay = self._find_keyed_commit(branch, key)
        if replay:
            files = len(self._out("show", "--name-only", "--format=", replay).splitlines())
            return replay, files, True
        if not message.strip() or len(message.splitlines()[0]) > 200:
            raise GitError("invalid_argument", "message needs a non-empty first line (<=200 chars)")
        raw = patch.encode()
        if not raw.strip() or len(raw) > MAX_PATCH_BYTES:
            raise GitError(
                "invalid_argument", f"patch must be non-empty and <= {MAX_PATCH_BYTES} bytes"
            )
        full_message = f"{message.strip()}\n\n{IDEMPOTENCY_TRAILER}: {key}\n"
        with tempfile.TemporaryDirectory(prefix="aeos-gh-") as tmp:
            work = Path(tmp) / "w"
            self._git(
                "clone",
                "-q",
                "--single-branch",
                "--branch",
                branch,
                str(self.remote),
                str(work),
                cwd=Path(tmp),
            )
            applied = self._git(
                "apply", "--index", "--whitespace=nowarn", "-", cwd=work, input=raw, check=False
            )
            if applied.returncode != 0:
                raise GitError(
                    "conflict",
                    "patch does not apply: "
                    + applied.stderr.decode(errors="replace").strip()[:300],
                )
            ident = {
                "GIT_AUTHOR_NAME": "AEOS Agent",
                "GIT_AUTHOR_EMAIL": "aeos-agent@localhost",
                "GIT_COMMITTER_NAME": "AEOS Agent",
                "GIT_COMMITTER_EMAIL": "aeos-agent@localhost",
            }
            self._git(
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-m",
                full_message,
                cwd=work,
                env=ident,
            )
            sha = self._git("rev-parse", "HEAD", cwd=work).stdout.decode().strip()
            files = len(
                self._git("show", "--name-only", "--format=", sha, cwd=work).stdout.decode().split()
            )
            self._git("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=work)
        return sha, files, False

    def ref_exists(self, branch: str) -> str | None:
        return self._branch_sha(branch)


def backend_info() -> dict[str, Any]:  # pragma: no cover - small helper for debugging
    return {"namespace": BRANCH_NAMESPACE}

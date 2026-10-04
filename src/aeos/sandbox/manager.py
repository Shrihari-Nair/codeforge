"""Docker-based ephemeral sandbox executor."""

import concurrent.futures
import hashlib
import io
import posixpath
import socket
import tarfile
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from pydantic import BaseModel

from aeos.sandbox.policy import Limits, PolicyViolation, check_command
from aeos.telemetry import traced

LABEL = "aeos.sandbox"
WORKSPACE = "/workspace"
SANDBOX_UID = 10001
KEEPALIVE_UID = 10002  # pid 1 runs as a different user so kill-all cleanup cannot stop it
DOCKERFILE_DIR = Path(__file__).parent
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "sandbox",
    "GIT_AUTHOR_EMAIL": "sandbox@localhost",
    "GIT_COMMITTER_NAME": "sandbox",
    "GIT_COMMITTER_EMAIL": "sandbox@localhost",
    "GIT_CONFIG_NOSYSTEM": "1",
}


class SandboxError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code, self.message = code, message


class RepoRef(BaseModel):
    remote: str  # path to the (bare) git repository
    ref: str = "main"


class ExecResult(BaseModel):
    stdout: str
    stderr: str
    exit_code: int
    duration_s: float
    timed_out: bool = False
    truncated: bool = False
    sandbox_destroyed: bool = False  # True if cleanup failed and the sandbox was removed


def image_tag() -> str:
    digest = hashlib.sha256((DOCKERFILE_DIR / "Dockerfile").read_bytes()).hexdigest()[:12]
    return f"aeos-sandbox:{digest}"


def ensure_image(client: docker.DockerClient) -> str:
    tag = image_tag()
    try:
        client.images.get(tag)
    except ImageNotFound:
        client.images.build(path=str(DOCKERFILE_DIR), tag=tag, rm=True)
    return tag


def _tar(entries: Iterable[tuple[str, bytes | None]]) -> bytes:
    """Tar of (relative path, content) entries owned by the sandbox user; None = directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.uid = info.gid = SANDBOX_UID
            info.mtime = int(time.time())
            if data is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            else:
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _safe_path(path: str) -> str:
    """Absolute path inside /workspace (or /tmp), never escaping via .. or NUL."""
    if not path or "\x00" in path:
        raise SandboxError("invalid_path", f"invalid path {path!r}")
    full = posixpath.normpath(path if path.startswith("/") else posixpath.join(WORKSPACE, path))
    if not (full.startswith(WORKSPACE + "/") or full.startswith("/tmp/")):
        raise SandboxError("invalid_path", f"path {path!r} is outside the sandbox workspace")
    return full


class SandboxManager:
    def __init__(self, client: docker.DockerClient | None = None, limits: Limits | None = None):
        self.client = client or docker.from_env()
        self.limits = limits or Limits()
        self._image = ensure_image(self.client)

    # ------------------------------------------------------------------ lifecycle
    def create_sandbox(
        self,
        repo: RepoRef | None = None,
        *,
        task_id: str = "",
        files: dict[str, str] | None = None,
    ) -> str:
        sid = f"aeos-sbx-{uuid.uuid4().hex[:10]}"
        lim = self.limits
        container = self.client.containers.run(
            self._image,
            command=["sleep", "infinity"],
            detach=True,
            name=sid,
            user=f"{KEEPALIVE_UID}:{KEEPALIVE_UID}",
            working_dir=WORKSPACE,
            network_mode="none",
            read_only=True,
            tmpfs={
                WORKSPACE: f"rw,exec,nosuid,nodev,uid={SANDBOX_UID},gid={SANDBOX_UID},"
                f"mode=755,size={lim.workspace_mb}m",
                "/tmp": f"rw,noexec,nosuid,nodev,uid={SANDBOX_UID},gid={SANDBOX_UID},"
                f"mode=1777,size={lim.tmp_mb}m",
            },
            mem_limit=lim.memory,
            memswap_limit=lim.memory,
            nano_cpus=int(lim.cpus * 1_000_000_000),
            pids_limit=lim.pids,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            init=True,
            environment={
                "HOME": "/tmp",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
                **_GIT_ENV,
            },
            labels={LABEL: "1", "aeos.task_id": task_id, "aeos.created_at": str(time.time())},
        )
        try:
            if repo is not None:
                self._extract(container, WORKSPACE, self._archive(repo))
            if files:
                self._extract(container, WORKSPACE, self._files_tar(files))
            self._baseline(sid)
        except BaseException:
            self.destroy(sid)
            raise
        return sid

    @staticmethod
    def _archive(repo: RepoRef) -> bytes:
        from aeos.mcp_servers.github.backend import GitBackend, GitError

        try:
            backend = GitBackend(Path(repo.remote))
            sha = backend.resolve(repo.ref)
            return backend._git("archive", "--format=tar", sha).stdout
        except GitError as exc:
            raise SandboxError(exc.code, exc.message) from exc

    @staticmethod
    def _files_tar(files: dict[str, str]) -> bytes:
        entries: list[tuple[str, bytes | None]] = []
        seen: set[str] = set()
        for rel, content in files.items():
            full = _safe_path(rel)
            parts = posixpath.relpath(full, WORKSPACE).split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in seen:
                    seen.add(d)
                    entries.append((d, None))
            entries.append(("/".join(parts), content.encode()))
        return _tar(entries)

    def _extract(self, container: Any, dest: str, data: bytes) -> None:
        """Extract a tar into `dest` by streaming it to an in-container `tar -x`.

        (Docker's put_archive refuses read-only root filesystems, and we will not weaken that.)
        """
        api = self.client.api
        exec_id = api.exec_create(
            container.id,
            ["tar", "-x", "-C", dest, "-f", "-"],
            stdin=True,
            user=f"{SANDBOX_UID}:{SANDBOX_UID}",
        )["Id"]
        sock = api.exec_start(exec_id, socket=True)
        raw = sock._sock  # noqa: SLF001 (docker-py exposes no public stdin API)
        raw.settimeout(60)
        try:
            raw.sendall(data)
            raw.shutdown(socket.SHUT_WR)
            output = b""
            while chunk := raw.recv(65536):
                output += chunk
        finally:
            sock.close()
        for _ in range(50):
            code = api.exec_inspect(exec_id).get("ExitCode")
            if code is not None:
                break
            time.sleep(0.1)
        if code != 0:
            raise SandboxError("extract_failed", f"tar exited {code}: {output[-300:]!r}")

    def _baseline(self, sid: str) -> None:
        """Commit the initial tree so `snapshot_diff` can produce a patch later."""
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "add", "-A"],
                    ["git", "commit", "-q", "--allow-empty", "-m", "baseline"]):  # fmt: skip
            r = self.exec(sid, cmd, timeout=30)
            if r.exit_code != 0:
                raise SandboxError("baseline_failed", r.stderr.strip()[:300])

    def destroy(self, sid: str) -> None:
        try:
            self.client.containers.get(sid).remove(force=True)
        except NotFound:
            pass

    @contextmanager
    def sandbox(
        self, repo: RepoRef | None = None, *, task_id: str = "", files: dict[str, str] | None = None
    ) -> Iterator[str]:
        sid = self.create_sandbox(repo, task_id=task_id, files=files)
        try:
            yield sid
        finally:
            self.destroy(sid)

    def _container(self, sid: str) -> Any:
        try:
            container = self.client.containers.get(sid)
        except NotFound as exc:
            raise SandboxError("not_found", f"sandbox {sid} does not exist") from exc
        container.reload()
        if container.status != "running":
            raise SandboxError("not_running", f"sandbox {sid} is {container.status}")
        return container

    def list_sandboxes(self) -> list[str]:
        return [c.name for c in self.client.containers.list(all=True, filters={"label": LABEL})]

    # ------------------------------------------------------------------ exec
    def exec(
        self,
        sid: str,
        argv: list[str],
        *,
        timeout: float | None = None,
        max_output: int | None = None,
        enforce_policy: bool = True,
        cleanup: bool = True,
    ) -> ExecResult:
        if enforce_policy:
            try:
                check_command(argv)
            except PolicyViolation as exc:
                raise SandboxError("command_denied", exc.message) from exc
        limit = timeout if timeout is not None else self.limits.exec_timeout_s
        cap = max_output if max_output is not None else self.limits.max_output_bytes
        container = self._container(sid)
        api = self.client.api
        wrapped = ["timeout", "--signal=KILL", f"{limit:g}", *argv]
        start = time.monotonic()
        try:
            exec_id = api.exec_create(
                container.id, wrapped, user=f"{SANDBOX_UID}:{SANDBOX_UID}", workdir=WORKSPACE
            )["Id"]
        except APIError as exc:
            return self._failed_exec(sid, f"cannot start command: {exc}", start, cleanup)

        def drain() -> tuple[bytes, bytes, bool]:
            out, err, truncated = bytearray(), bytearray(), False
            for o, e in api.exec_start(exec_id, stream=True, demux=True):
                for chunk, buf in ((o, out), (e, err)):
                    if chunk:
                        room = cap - len(buf)
                        if room > 0:
                            buf.extend(chunk[:room])
                        truncated = truncated or len(chunk) > room
            return bytes(out), bytes(err), truncated

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(drain)
        timed_out = False
        with traced("sandbox.exec", sandbox=sid, cmd=argv[0], argv=argv) as span:
            try:
                out, err, truncated = future.result(timeout=limit + 5)
            except concurrent.futures.TimeoutError:
                timed_out = True
                self._kill_everything(sid)
                out, err, truncated = future.result(timeout=15)
            except APIError as exc:
                return self._failed_exec(sid, f"command stream failed: {exc}", start, cleanup)
            finally:
                pool.shutdown(wait=False)
            duration = time.monotonic() - start
            code = api.exec_inspect(exec_id).get("ExitCode")
            exit_code = -1 if code is None else int(code)
            timed_out = timed_out or (exit_code == 137 and duration >= limit * 0.9)
            destroyed = self._ensure_clean(sid, force=timed_out) if cleanup else False
            span.set("exit_code", exit_code)
            span.set("timed_out", timed_out)
            span.set("duration_s", round(duration, 3))
        return ExecResult(
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
            exit_code=exit_code,
            duration_s=round(duration, 3),
            timed_out=timed_out,
            truncated=truncated,
            sandbox_destroyed=destroyed,
        )

    def _failed_exec(self, sid: str, message: str, start: float, cleanup: bool) -> ExecResult:
        destroyed = self._ensure_clean(sid, force=True) if cleanup else False
        return ExecResult(
            stdout="",
            stderr=message,
            exit_code=-1,
            duration_s=round(time.monotonic() - start, 3),
            sandbox_destroyed=destroyed,
        )

    def _kill_everything(self, sid: str) -> bool:
        """SIGKILL every process of the sandbox user. Returns False if even that failed."""
        try:
            eid = self.client.api.exec_create(
                self._container(sid).id,
                ["python3", "-c", "import os; os.kill(-1, 9)"],
                user=f"{SANDBOX_UID}:{SANDBOX_UID}",
            )["Id"]
            self.client.api.exec_start(eid)
            return True
        except (APIError, SandboxError):
            return False

    def _leftover_processes(self, sid: str) -> int:
        container = self._container(sid)
        procs = container.top(ps_args="-eo pid,comm").get("Processes") or []
        return max(len(procs) - 2, 0)  # pid 1 (init) and the keepalive sleep are expected

    def _ensure_clean(self, sid: str, *, force: bool = False) -> bool:
        """Kill any process a command left behind. Destroys the sandbox if that is impossible.

        Returns True if the sandbox was destroyed.
        """
        try:
            if not force and self._leftover_processes(sid) == 0:
                return False
            if self._kill_everything(sid):
                time.sleep(0.05)
                probe = self.exec(sid, ["true"], timeout=5, enforce_policy=False, cleanup=False)
                if probe.exit_code == 0 and self._leftover_processes(sid) == 0:
                    return False
        except (SandboxError, APIError, NotFound):
            pass
        self.destroy(sid)
        return True

    # ------------------------------------------------------------------ files
    def read_file(self, sid: str, path: str) -> str:
        """Read a file via the container (docker cp cannot see tmpfs mounts)."""
        full = _safe_path(path)
        cap = self.limits.max_file_bytes
        r = self.exec(
            sid, ["cat", "--", full], timeout=15, max_output=cap + 1, enforce_policy=False
        )
        if r.exit_code != 0:
            err = r.stderr.strip()
            code = "not_found" if "No such file" in err else "not_a_file"
            raise SandboxError(code, f"cannot read {path!r}: {err[:200]}")
        if r.truncated or len(r.stdout.encode()) > cap:
            raise SandboxError("too_large", f"{path!r} exceeds {cap} bytes")
        return r.stdout

    def write_file(self, sid: str, path: str, content: str) -> None:
        full = _safe_path(path)
        data = content.encode()
        if len(data) > self.limits.max_file_bytes:
            raise SandboxError("too_large", f"content exceeds {self.limits.max_file_bytes} bytes")
        container = self._container(sid)
        root = "/tmp" if full.startswith("/tmp/") else WORKSPACE
        rel = posixpath.relpath(full, root).split("/")
        entries: list[tuple[str, bytes | None]] = [
            ("/".join(rel[:i]), None) for i in range(1, len(rel))
        ]
        entries.append(("/".join(rel), data))
        self._extract(container, root, _tar(entries))

    def apply_patch(self, sid: str, patch: str) -> None:
        if not patch.strip():
            raise SandboxError("invalid_patch", "empty patch")
        self.write_file(sid, "/tmp/aeos.patch", patch if patch.endswith("\n") else patch + "\n")
        r = self.exec(sid, ["git", "apply", "--whitespace=nowarn", "/tmp/aeos.patch"], timeout=30)
        if r.exit_code != 0:
            raise SandboxError("patch_failed", r.stderr.strip()[:500])

    def snapshot_diff(self, sid: str) -> str:
        """Unified diff of the workspace against the initial tree."""
        for cmd in (["git", "add", "-A"],):
            self.exec(sid, cmd, timeout=30)
        r = self.exec(sid, ["git", "diff", "--cached", "--binary", "HEAD"], timeout=30,
                      max_output=self.limits.max_file_bytes)  # fmt: skip
        if r.exit_code != 0:
            raise SandboxError("diff_failed", r.stderr.strip()[:300])
        return r.stdout

    # ------------------------------------------------------------------ reaper
    def reap_task(self, task_id: str) -> list[str]:
        """Remove every sandbox that belongs to a finished task (including a crashed worker's)."""
        removed: list[str] = []
        for c in self.client.containers.list(
            all=True, filters={"label": f"aeos.task_id={task_id}"}
        ):
            c.remove(force=True)
            removed.append(c.name)
        return removed

    def reap(
        self,
        *,
        ttl_s: float | None = None,
        keep: Iterable[str] | None = None,
        active_tasks: Iterable[str] | None = None,
        grace_s: float = 0.0,
    ) -> list[str]:
        """Remove orphaned sandboxes.

        Orphan = older than `ttl_s`, or not in `keep`, or labelled with a task that is no longer
        active (older than `grace_s` so freshly created sandboxes are never raced).
        """
        keep_set = set(keep) if keep is not None else None
        active_set = set(active_tasks) if active_tasks is not None else None
        removed: list[str] = []
        now = time.time()
        for c in self.client.containers.list(all=True, filters={"label": LABEL}):
            created = float(c.labels.get("aeos.created_at", "0") or 0)
            age = now - created
            expired = ttl_s is not None and age > ttl_s
            orphan = keep_set is not None and c.name not in keep_set
            abandoned = (
                active_set is not None
                and c.labels.get("aeos.task_id", "") not in active_set
                and age > grace_s
            )
            if expired or orphan or abandoned:
                c.remove(force=True)
                removed.append(c.name)
        return removed

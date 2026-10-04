"""Narrow, deterministic package-registry access for dependency changes.

The coding sandbox has no network by design. Updating a Node dependency needs the registry (to
look up versions and to regenerate package-lock.json), so that one capability is provided as two
fixed operations in a separate throw-away container:

  * `info(package)`                -> `npm view <package> --json`
  * `update(files, package, ver)`  -> `npm install --package-lock-only --ignore-scripts ...`

No agent-controlled command line reaches the container: only a validated package name and version
are interpolated into a fixed argv. `--ignore-scripts` means no package code runs. The container
has no mounts, drops all capabilities, runs as an unprivileged user, and is destroyed afterwards.
"""

import io
import json
import re
import tarfile
import time
from typing import Any

import docker
from docker.errors import ImageNotFound

from aeos.sandbox.manager import LABEL, SandboxError

IMAGE = "node:20-slim"
WORK = "/work"
_NAME_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_VER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,60}$")  # exact version or dist-tag
FILES = ("package.json", "package-lock.json")


def validate_package(name: str) -> str:
    if len(name) > 214 or not _NAME_RE.match(name):
        raise SandboxError("invalid_argument", f"not a valid npm package name: {name!r}")
    return name


def validate_version(version: str) -> str:
    if not _VER_RE.match(version):
        raise SandboxError(
            "invalid_argument", f"version must be an exact version or dist-tag, got {version!r}"
        )
    return version


class NpmRegistry:
    def __init__(self, client: docker.DockerClient | None = None) -> None:
        self.client = client or docker.from_env()

    def _ensure_image(self) -> None:
        try:
            self.client.images.get(IMAGE)
        except ImageNotFound:
            self.client.images.pull(IMAGE)

    def _run(
        self, argv: list[str], files: dict[str, str] | None = None, timeout: float = 120
    ) -> tuple[int, str, dict[str, str]]:
        self._ensure_image()
        c = self.client.containers.run(
            IMAGE, command=["sleep", "infinity"], detach=True, user="node",
            working_dir=WORK, mem_limit="512m", memswap_limit="512m", pids_limit=256,
            cap_drop=["ALL"], security_opt=["no-new-privileges"], init=True,
            environment={"HOME": "/tmp", "npm_config_update_notifier": "false"},
            labels={LABEL: "1", "aeos.created_at": str(time.time()), "aeos.kind": "npm"},
        )  # fmt: skip
        try:
            c.exec_run(["mkdir", "-p", WORK], user="root")
            c.exec_run(["chown", "node:node", WORK], user="root")
            if files:
                buf = io.BytesIO()
                with tarfile.open(fileobj=buf, mode="w") as tar:
                    for name, content in files.items():
                        data = content.encode()
                        info = tarfile.TarInfo(name)
                        info.size, info.uid, info.gid, info.mode = len(data), 1000, 1000, 0o644
                        tar.addfile(info, io.BytesIO(data))
                c.put_archive(WORK, buf.getvalue())
            code, out = c.exec_run(["timeout", str(int(timeout)), *argv], user="node", demux=False)
            result = (out or b"").decode(errors="replace")
            changed: dict[str, str] = {}
            for name in FILES:
                rc, data = c.exec_run(["cat", f"{WORK}/{name}"], user="node")
                if rc == 0:
                    changed[name] = data.decode(errors="replace")
            return code, result, changed
        finally:
            try:
                c.remove(force=True)
            except Exception:  # noqa: S110 (best effort; the reaper removes labelled leftovers)
                pass

    def info(self, package: str) -> dict[str, Any]:
        package = validate_package(package)
        code, out, _ = self._run(
            ["npm", "view", package, "name", "dist-tags", "versions", "--json"]
        )
        if code != 0:
            raise SandboxError("registry_error", out[-400:])
        try:
            data = json.loads(out)
        except json.JSONDecodeError as exc:
            raise SandboxError(
                "registry_error", f"unexpected registry output: {out[:200]}"
            ) from exc
        versions = data.get("versions", [])
        return {
            "name": data.get("name", package),
            "dist_tags": data.get("dist-tags", {}),
            "recent_versions": versions[-15:] if isinstance(versions, list) else [],
            "version_count": len(versions) if isinstance(versions, list) else 0,
        }

    def update(self, files: dict[str, str], package: str, version: str) -> dict[str, str]:
        """Return new contents of package.json and package-lock.json with `package@version`."""
        package, version = validate_package(package), validate_version(version)
        if "package.json" not in files:
            raise SandboxError("not_found", "package.json is not in the workspace")
        argv = [
            "npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit",
            "--no-fund", "--save", f"{package}@{version}",
        ]  # fmt: skip
        code, out, changed = self._run(argv, files)
        if code != 0:
            raise SandboxError("npm_failed", out[-600:])
        return changed


def read_workspace_files(read: Any) -> dict[str, str]:
    """Collect the dependency manifests from a sandbox via `read(path) -> str`."""
    files: dict[str, str] = {}
    for name in FILES:
        try:
            files[name] = read(name)
        except SandboxError:
            continue
    return files


__all__ = ["FILES", "NpmRegistry", "read_workspace_files", "validate_package"]

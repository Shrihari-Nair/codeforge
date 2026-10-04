"""The coding agent's workspace: sandbox operations exposed as tools.

All execution happens inside the Docker sandbox. The authoritative patch is taken from the
sandbox by the harness (`snapshot_patch`), never from text the model wrote.
"""

import asyncio
import json
from typing import Any

from aeos.agents.tools import ToolContext, ToolDef, ToolOutput
from aeos.sandbox import (
    NpmRegistry,
    RepoRef,
    SandboxError,
    SandboxManager,
    compare_benchmarks,
    run_benchmark,
    run_tests,
)
from aeos.sandbox.npm import read_workspace_files
from aeos.sandbox.policy import ALLOWED_COMMANDS


def _err(exc: SandboxError) -> ToolOutput:
    return ToolOutput(f"[{exc.code}] {exc.message}", is_error=True)


class CodingWorkspace:
    def __init__(
        self,
        mgr: SandboxManager,
        repo: RepoRef,
        baseline: RepoRef,
        task_id: str,
        profile: str = "regression",
        npm: NpmRegistry | None = None,
    ) -> None:
        self.mgr, self.repo, self.baseline, self.task_id = mgr, repo, baseline, task_id
        self.profile, self.npm = profile, npm  # "regression" (bench tools) or "change" (generic)
        self.sid: str | None = None

    async def open(self, patch: str | None = None) -> None:
        self.sid = await asyncio.to_thread(self.mgr.create_sandbox, self.repo, task_id=self.task_id)
        if patch:  # rebuild from a previous artifact (resume / retry)
            await asyncio.to_thread(self.mgr.apply_patch, self.sid, patch)

    async def close(self) -> None:
        if self.sid:
            await asyncio.to_thread(self.mgr.destroy, self.sid)
            self.sid = None

    async def snapshot_patch(self) -> str:
        assert self.sid
        return await asyncio.to_thread(self.mgr.snapshot_diff, self.sid)

    def _sid(self) -> str:
        if not self.sid:
            raise SandboxError("not_open", "workspace is not open")
        return self.sid

    # ------------------------------------------------------------------ tools
    def tools(self) -> dict[str, ToolDef]:
        # Risk "read" = no EXTERNAL side effects: these only touch an isolated, ephemeral sandbox.
        m = self.mgr

        async def read_file(a: dict[str, Any], c: ToolContext) -> ToolOutput:
            try:
                return ToolOutput(await asyncio.to_thread(m.read_file, self._sid(), a["path"]))
            except SandboxError as e:
                return _err(e)

        async def write_file(a: dict[str, Any], c: ToolContext) -> ToolOutput:
            try:
                await asyncio.to_thread(m.write_file, self._sid(), a["path"], a["content"])
                return ToolOutput(f"wrote {a['path']} ({len(a['content'])} chars)")
            except SandboxError as e:
                return _err(e)

        async def edit_file(a: dict[str, Any], c: ToolContext) -> ToolOutput:
            try:
                sid = self._sid()
                text = await asyncio.to_thread(m.read_file, sid, a["path"])
                count = text.count(a["old"])
                if count != 1:
                    return ToolOutput(
                        f"`old` must match exactly once in {a['path']}; it matched {count} times",
                        is_error=True,
                    )
                await asyncio.to_thread(
                    m.write_file, sid, a["path"], text.replace(a["old"], a["new"])
                )
                return ToolOutput(f"edited {a['path']}")
            except SandboxError as e:
                return _err(e)

        async def exec_cmd(a: dict[str, Any], c: ToolContext) -> ToolOutput:
            try:
                r = await asyncio.to_thread(
                    m.exec,
                    self._sid(),
                    list(a["argv"]),
                    timeout=min(float(a.get("timeout_s", 60)), 120),
                )
            except SandboxError as e:
                return _err(e)
            return ToolOutput(
                json.dumps(
                    {
                        "exit_code": r.exit_code,
                        "stdout": r.stdout[-4000:],
                        "stderr": r.stderr[-2000:],
                        "timed_out": r.timed_out,
                    }  # fmt: skip
                ),
                is_error=False,
            )

        async def apply_patch(a: dict[str, Any], c: ToolContext) -> ToolOutput:
            try:
                await asyncio.to_thread(m.apply_patch, self._sid(), a["patch"])
                return ToolOutput("patch applied")
            except SandboxError as e:
                return _err(e)

        async def tests(a: dict[str, Any], c: ToolContext) -> ToolOutput:
            try:
                r = await asyncio.to_thread(run_tests, m, self._sid())
            except SandboxError as e:
                return _err(e)
            return ToolOutput(
                r.model_dump_json(exclude={"output_tail"}) + "\n" + r.output_tail[-800:]
            )

        async def bench(a: dict[str, Any], c: ToolContext) -> ToolOutput:
            try:
                r = await asyncio.to_thread(run_benchmark, m, self._sid())
            except SandboxError as e:
                return _err(e)
            return ToolOutput(r.model_dump_json())

        async def compare(a: dict[str, Any], c: ToolContext) -> ToolOutput:
            try:
                sid = self._sid()
                patch = await asyncio.to_thread(m.snapshot_diff, sid)
                baseline = RepoRef(
                    remote=self.baseline.remote, ref=a.get("baseline_ref") or self.baseline.ref
                )
                cmp = await asyncio.to_thread(
                    compare_benchmarks, m, baseline, self.repo, patch=patch or None,
                    task_id=self.task_id, with_tests=False,
                )  # fmt: skip
            except SandboxError as e:
                return _err(e)
            return ToolOutput(cmp.model_dump_json())

        async def npm_info(a: dict[str, Any], c: ToolContext) -> ToolOutput:
            if self.npm is None:
                return ToolOutput("[unavailable] registry access is not configured", is_error=True)
            try:
                data = await asyncio.to_thread(self.npm.info, a["package"])
            except SandboxError as e:
                return _err(e)
            return ToolOutput(json.dumps(data))

        async def npm_update(a: dict[str, Any], c: ToolContext) -> ToolOutput:
            if self.npm is None:
                return ToolOutput("[unavailable] registry access is not configured", is_error=True)
            try:
                sid = self._sid()
                files = await asyncio.to_thread(read_workspace_files, lambda p: m.read_file(sid, p))
                changed = await asyncio.to_thread(
                    self.npm.update, files, a["package"], a["version"]
                )
                for name, content in changed.items():
                    if files.get(name) != content:
                        await asyncio.to_thread(m.write_file, sid, name, content)
            except SandboxError as e:
                return _err(e)
            return ToolOutput(
                f"updated {a['package']} to {a['version']}; rewrote: "
                + ", ".join(n for n, v in changed.items() if files.get(n) != v)
            )

        mutating = {
            "sandbox_write_file",
            "sandbox_edit_file",
            "sandbox_exec",
            "sandbox_apply_patch",
            "npm_update_dependency",
        }

        def td(name: str, desc: str, props: dict[str, Any], required: list[str], fn: Any,
               risk: str = "read") -> ToolDef:  # fmt: skip
            return ToolDef(
                name=name, description=desc, server="sandbox", risk=risk, run=fn,
                parameters={"type": "object", "properties": props, "required": required},
                mutates=name in mutating,
            )  # fmt: skip

        path = {"path": {"type": "string", "description": "path relative to the repo root"}}
        defs = [
            td(
                "sandbox_read_file",
                "Read a file from the sandboxed checkout.",
                path,
                ["path"],
                read_file,
            ),
            td(
                "sandbox_write_file",
                "Create or overwrite a file in the sandbox.",
                {**path, "content": {"type": "string"}},
                ["path", "content"],
                write_file,
                "read",
            ),  # fmt: skip
            td(
                "sandbox_edit_file",
                "Replace one exact occurrence of `old` with `new` in a file (fails unless `old` "
                "matches exactly once). Prefer this over rewriting whole files.",
                {**path, "old": {"type": "string"}, "new": {"type": "string"}},
                ["path", "old", "new"],
                edit_file,
                "read",
            ),  # fmt: skip
            td(
                "sandbox_exec",
                "Run a command in the sandbox (no shell, no network). `argv` is a list, e.g. "
                f'["grep","-rn","get_product","."]. Allowed programs: {", ".join(sorted(ALLOWED_COMMANDS))}.',
                {
                    "argv": {"type": "array", "items": {"type": "string"}},
                    "timeout_s": {"type": "number", "description": "max 120"},
                },
                ["argv"],
                exec_cmd,
                "read",
            ),  # fmt: skip
            td(
                "sandbox_apply_patch",
                "Apply a unified diff to the sandbox checkout.",
                {"patch": {"type": "string"}},
                ["patch"],
                apply_patch,
                "read",
            ),  # fmt: skip
            td(
                "sandbox_run_tests",
                "Run the repo's test suite in the sandbox; returns structured results.",
                {},
                [],
                tests,
            ),  # fmt: skip
            td(
                "sandbox_run_benchmark",
                "Run the repo's deterministic benchmark on your current working tree (p50/p95/p99 and "
                "db_queries).",
                {},
                [],
                bench,
            ),  # fmt: skip
            td(
                "sandbox_compare_to_baseline",
                "Benchmark your current changes against the last known-good commit in fresh "
                "sandboxes. Returns p95 ratio and whether it regressed. This is the real test of "
                "whether your fix works.",
                {
                    "baseline_ref": {
                        "type": "string",
                        "description": "commit sha; defaults to last-good",
                    }
                },
                [],
                compare,
            ),  # fmt: skip
        ]
        if self.profile == "change":
            skip = {"sandbox_run_tests", "sandbox_run_benchmark", "sandbox_compare_to_baseline"}
            defs = [d for d in defs if d.name not in skip]
            defs += [
                td(
                    "npm_registry_info",
                    "Look up a package on the npm registry: dist-tags (latest...) and recent "
                    "versions. Use it to choose the target version of a dependency upgrade.",
                    {"package": {"type": "string"}},
                    ["package"],
                    npm_info,
                ),
                td(
                    "npm_update_dependency",
                    "Set a dependency to an exact version: rewrites package.json AND regenerates "
                    "package-lock.json (the sandbox has no network, so always use this tool for "
                    "dependency changes, never edit the lockfile by hand). No install scripts run.",
                    {"package": {"type": "string"}, "version": {"type": "string"}},
                    ["package", "version"],
                    npm_update,
                ),
            ]
        return {d.name: d for d in defs}


def registry_tools(npm: NpmRegistry) -> dict[str, ToolDef]:
    """Read-only registry lookup for agents that have no sandbox (the research agent)."""

    async def info(a: dict[str, Any], c: ToolContext) -> ToolOutput:
        try:
            return ToolOutput(json.dumps(await asyncio.to_thread(npm.info, a["package"])))
        except SandboxError as e:
            return _err(e)

    return {
        "npm_registry_info": ToolDef(
            name="npm_registry_info",
            description="Look up a package on the npm registry: dist-tags (latest, ...) and the "
            "most recent versions.",
            server="registry",
            risk="read",
            run=info,
            parameters={
                "type": "object",
                "properties": {"package": {"type": "string"}},
                "required": ["package"],
            },
        )  # fmt: skip
    }

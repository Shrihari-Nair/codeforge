"""Verifier for "change" tasks on a real repository.

Same principle as the regression verifier: nothing is taken on the coding agent's word. The patch
is re-applied to a fresh checkout of the target branch and checked deterministically. The gates are
honest about what exists: a repository without a test suite cannot be test-verified, so the gates
are diff safety, syntax of every touched file, manifest/lockfile consistency, and an advisory LLM
judgement of completeness. `metrics["tests"]` records that no suite was run.
"""

import asyncio
import json
import re

from aeos.agents.verifier import GateConfig, parse_patch, static_gates
from aeos.core.state import Issue, VerificationResult
from aeos.sandbox import RepoRef, SandboxError, SandboxManager
from aeos.telemetry import traced

CHANGE_CFG = GateConfig(
    max_files=12,
    max_diff_lines=800,
    forbidden_paths=[".env", "package-lock.json.bak"],
    protected_prefixes=[],
    forbidden_prefixes=[".git/", ".github/workflows/", "node_modules/", "data/"],
)
JS_EXT = (".js", ".mjs", ".cjs")
_VER = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_SPEC = re.compile(r"^([\^~]?)(\d+)\.(\d+)\.(\d+)")


def satisfies(version: str, spec: str) -> bool:
    """Minimal semver check for ^, ~ and exact specs; anything else is treated as satisfied."""
    v, sp = _VER.match(version), _SPEC.match(spec.strip())
    if not v or not sp:
        return True
    cur = tuple(int(x) for x in v.groups())
    op, want = sp.group(1), tuple(int(x) for x in sp.groups()[1:])
    if cur < want:
        return False
    if op == "^":
        return cur[0] == want[0] if want[0] else cur[:2] == want[:2]
    if op == "~":
        return cur[:2] == want[:2]
    return cur == want


def lockfile_issues(package_json: str, lock_json: str) -> list[str]:
    """Problems that make `npm ci` fail: the lockfile must mirror package.json."""
    try:
        pkg, lock = json.loads(package_json), json.loads(lock_json)
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc}"]
    problems: list[str] = []
    packages = lock.get("packages", {})
    root = packages.get("", {})
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        want, have = pkg.get(section, {}), root.get(section, {})
        for name, spec in want.items():
            if have.get(name) != spec:
                problems.append(
                    f"{name}: package.json says {spec!r} but the lockfile root says "
                    f"{have.get(name)!r}"
                )
                continue
            locked = packages.get(f"node_modules/{name}", {}).get("version")
            if locked is None:
                problems.append(f"{name}: not present in the lockfile")
            elif not satisfies(locked, spec):
                problems.append(f"{name}: locked {locked} does not satisfy {spec}")
    return problems


async def _run(
    mgr: SandboxManager, *, base: RepoRef, patch: str | None, cfg: GateConfig,
    attempt: int, task_id: str,
) -> VerificationResult:  # fmt: skip
    if not patch or not patch.strip():
        return VerificationResult(
            status="failed", attempt=attempt, gates={"patch_present": False},
            issues=[Issue(type="no_patch", detail="the coding agent produced no changes")],
            suggested_focus=["make an actual code change in the sandbox"],
        )  # fmt: skip
    gates, issues = static_gates(patch, cfg)
    gates = {"patch_present": True, **gates}
    files = parse_patch(patch)
    metrics: dict[str, object] = {
        "files": [f.model_dump() for f in files],
        "tests": {"ran": False, "note": "no automated test suite is run for this repository"},
    }
    focus: list[str] = []

    def checks() -> None:
        with mgr.sandbox(base, task_id=task_id) as sid:
            try:
                mgr.apply_patch(sid, patch)
                gates["patch_applies"] = True
            except SandboxError as exc:
                gates["patch_applies"] = False
                issues.append(Issue(type="patch_does_not_apply", detail=exc.message[:300]))
                return
            syntax_ok = True
            for f in files:
                if f.status == "deleted":
                    continue
                if f.path.endswith(JS_EXT):
                    r = mgr.exec(sid, ["node", "--check", f.path], timeout=30)
                elif f.path.endswith(".py"):
                    r = mgr.exec(sid, ["python", "-m", "py_compile", f.path], timeout=30)
                elif f.path.endswith(".json"):
                    try:
                        json.loads(mgr.read_file(sid, f.path))
                        continue
                    except (json.JSONDecodeError, SandboxError) as exc:
                        syntax_ok = False
                        issues.append(Issue(type="syntax_error", detail=f"{f.path}: {exc}"))
                        continue
                else:
                    continue
                if r.exit_code != 0:
                    syntax_ok = False
                    issues.append(
                        Issue(
                            type="syntax_error", detail=f"{f.path}: {(r.stderr or r.stdout)[-300:]}"
                        )
                    )
            gates["syntax_ok"] = syntax_ok
            if any(f.path == "package.json" for f in files):
                try:
                    lock = mgr.read_file(sid, "package-lock.json")
                    problems = lockfile_issues(mgr.read_file(sid, "package.json"), lock)
                except SandboxError:
                    problems = []  # no lockfile in this repo: nothing to keep consistent
                gates["lockfile_consistent"] = not problems
                if problems:
                    issues.append(
                        Issue(type="lockfile_inconsistent", detail="; ".join(problems[:4]))
                    )
                    focus.append("use npm_update_dependency so package.json and the lockfile agree")

    try:
        await asyncio.to_thread(checks)
    except SandboxError as exc:
        gates["sandbox_ok"] = False
        issues.append(Issue(type="verifier_error", detail=f"{exc.code}: {exc.message[:200]}"))
    return VerificationResult(
        status="passed" if all(gates.values()) else "failed", issues=issues,
        suggested_focus=focus, gates=gates, attempt=attempt, metrics=metrics,
    )  # fmt: skip


async def run_change_gates(
    mgr: SandboxManager, *, base: RepoRef, patch: str | None, cfg: GateConfig | None = None,
    attempt: int = 1, task_id: str = "",
) -> VerificationResult:  # fmt: skip
    with traced("verifier.gates", attributes={"aeos.verification.attempt": attempt}) as span:
        result = await _run(
            mgr, base=base, patch=patch, cfg=cfg or CHANGE_CFG, attempt=attempt, task_id=task_id
        )
        span.set("aeos.verification.status", result.status)
        span.set("aeos.verification.gates", result.gates)
        span.set("aeos.verification.issues", [f"{i.type}: {i.detail}" for i in result.issues])
        return result

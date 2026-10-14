"""Run a change request's own test suite in the offline sandbox, when it can run there.

The sandbox has Python (with pytest) and Node, but no npm and no network. So only suites that need
nothing installed can run: pytest, and Node's built-in runner (`node --test`). A repo that uses
jest, mocha, vitest and so on is reported as "could not run", never as passing.
"""

import json
import re
from dataclasses import dataclass

from aeos.sandbox import SandboxError, SandboxManager
from aeos.sandbox.helpers import run_tests as run_pytest

# Runners that need `npm install` first, which the sandbox cannot do.
NEEDS_NPM = ("jest", "mocha", "vitest", "ava", "tap", "jasmine", "playwright", "cypress")
_NPM_DEFAULT_TEST = re.compile(r"no test specified")


@dataclass
class SuiteResult:
    ok: bool
    total: int
    failed: int
    tail: str
    timed_out: bool = False


def _has_files(mgr: SandboxManager, sid: str, *names: str) -> bool:
    argv = ["find", ".", "(", "-name", names[0]]
    for n in names[1:]:
        argv += ["-o", "-name", n]
    argv += [")", "-not", "-path", "./node_modules/*", "-not", "-path", "./.git/*"]
    return bool(mgr.exec(sid, argv, timeout=20).stdout.strip())


def find_suite(mgr: SandboxManager, sid: str) -> tuple[str | None, str]:
    """(runner, why-not). runner is None when nothing can run, with the reason."""
    try:
        pkg = json.loads(mgr.read_file(sid, "package.json"))
    except (SandboxError, json.JSONDecodeError):
        pkg = None
    if isinstance(pkg, dict):
        script = str((pkg.get("scripts") or {}).get("test", ""))
        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        needs = [d for d in NEEDS_NPM if d in deps]
        if script.startswith("node --test"):
            return "node --test", ""
        if needs or (script and not _NPM_DEFAULT_TEST.search(script)):
            what = ", ".join(needs) or f"`{script[:60]}`"
            return None, f"its tests use {what}, which needs npm install (the sandbox is offline)"
        if _has_files(mgr, sid, "*.test.js", "*.test.mjs", "*.spec.js"):
            return "node --test", ""
    if _has_files(mgr, sid, "test_*.py", "*_test.py"):
        return "pytest", ""
    return None, "no test suite found in the repository"


def _count(pattern: str, text: str) -> int:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else 0


def run_suite(mgr: SandboxManager, sid: str, runner: str) -> SuiteResult:
    if runner == "pytest":
        t = run_pytest(mgr, sid)
        return SuiteResult(t.passed, t.total, t.failed + t.errors, t.output_tail, t.timed_out)
    r = mgr.exec(sid, ["node", "--test"], timeout=120)
    out = r.stdout + ("\n" + r.stderr if r.stderr else "")
    return SuiteResult(
        ok=r.exit_code == 0 and not r.timed_out,
        total=_count(r"(?:#|ℹ)\s*tests\s+(\d+)", out),
        failed=_count(r"(?:#|ℹ)\s*fail\s+(\d+)", out),
        tail=out[-1500:],
        timed_out=r.timed_out,
    )

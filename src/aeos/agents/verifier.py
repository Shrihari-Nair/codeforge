"""Verifier: deterministic hard gates, recomputed independently of the coding agent.

Nothing here trusts the agent's claims: the patch is re-applied to a fresh checkout and tests,
lint and the benchmark are re-run in fresh sandboxes. (The LLM judgement layer is advisory and
lives in `judge.py`; it can never override a failed gate.)
"""

import asyncio
import re

from pydantic import BaseModel, Field

from aeos.core.state import Issue, VerificationResult
from aeos.sandbox import RepoRef, SandboxError, SandboxManager, run_benchmark, run_tests
from aeos.telemetry import traced

SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]"),
]


class GateConfig(BaseModel):
    p95_tolerance_pct: float = 10.0
    max_files: int = 5
    max_diff_lines: int = 300
    forbidden_paths: list[str] = Field(
        default_factory=lambda: ["benchmark.py", "checkout/costs.py", "pytest.ini"]
    )
    protected_prefixes: list[str] = Field(default_factory=lambda: ["tests/"])
    forbidden_prefixes: list[str] = Field(default_factory=list)  # never editable, any status
    secret_values: list[str] = Field(default_factory=list)  # exact configured secrets to detect


class FileDiff(BaseModel):
    path: str
    status: str  # new | modified | deleted
    added: int = 0
    removed: int = 0


def parse_patch(patch: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    current: FileDiff | None = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            current = FileDiff(path=m.group(2) if m else line[11:], status="modified")
            files.append(current)
        elif current is None:
            continue
        elif line.startswith("new file mode"):
            current.status = "new"
        elif line.startswith("deleted file mode"):
            current.status = "deleted"
        elif line.startswith("+") and not line.startswith("+++"):
            current.added += 1
        elif line.startswith("-") and not line.startswith("---"):
            current.removed += 1
    return files


def static_gates(patch: str, cfg: GateConfig) -> tuple[dict[str, bool], list[Issue]]:
    gates: dict[str, bool] = {}
    issues: list[Issue] = []
    files = parse_patch(patch)
    lines = sum(f.added + f.removed for f in files)
    gates["diff_size_ok"] = len(files) <= cfg.max_files and lines <= cfg.max_diff_lines
    if not gates["diff_size_ok"]:
        issues.append(Issue(type="diff_too_large",
                            detail=f"{len(files)} files / {lines} changed lines "
                                   f"(max {cfg.max_files} / {cfg.max_diff_lines})"))  # fmt: skip

    bad: list[str] = []
    for f in files:
        if f.path in cfg.forbidden_paths:
            bad.append(f"{f.path} (measurement/config file)")
        elif any(f.path.startswith(p) for p in cfg.forbidden_prefixes):
            bad.append(f"{f.path} (protected location)")
        elif any(f.path.startswith(p) for p in cfg.protected_prefixes) and f.status != "new":
            bad.append(f"{f.path} (existing test {f.status})")
    gates["paths_allowed"] = not bad
    if bad:
        issues.append(Issue(type="forbidden_edit", detail="edits not allowed: " + ", ".join(bad)))

    added_text = "\n".join(
        ln[1:] for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")
    )
    leaked = any(p.search(added_text) for p in SECRET_PATTERNS) or any(
        v and v in patch for v in cfg.secret_values
    )
    gates["no_secrets_in_diff"] = not leaked
    if leaked:
        issues.append(Issue(type="secret_in_diff", detail="the patch appears to contain a secret"))
    return gates, issues


def _focus_for_perf(base_q: int, cand_q: int, files: list[FileDiff]) -> list[str]:
    focus = []
    if cand_q > base_q * 1.5:
        focus.append(
            f"database queries per run are still {cand_q} vs {base_q} at baseline: look for "
            "per-item queries in a loop or lost batching/caching on the hot path"
        )
    if files and all(f.path.endswith(("audit.py", "costs.py")) for f in files):
        focus.append("the change only touched code that is unrelated to the p95 regression")
    return focus or ["the benchmark p95 is still above the baseline: re-check the root cause"]


async def _run_gates(
    mgr: SandboxManager,
    *,
    base: RepoRef,
    baseline: RepoRef,
    patch: str | None,
    cfg: GateConfig | None = None,
    attempt: int = 1,
    task_id: str = "",
) -> VerificationResult:
    cfg = cfg or GateConfig()
    if not patch or not patch.strip():
        return VerificationResult(
            status="failed", attempt=attempt, gates={"patch_present": False},
            issues=[Issue(type="no_patch", detail="the coding agent produced no changes")],
            suggested_focus=["make an actual code change in the sandbox"],
        )  # fmt: skip

    gates, issues = static_gates(patch, cfg)
    gates = {"patch_present": True, **gates}
    files = parse_patch(patch)
    metrics: dict[str, object] = {"files": [f.model_dump() for f in files]}
    focus: list[str] = []

    def sandbox_checks() -> None:
        with mgr.sandbox(baseline, task_id=task_id) as sid:
            base_bench = run_benchmark(mgr, sid)
        with mgr.sandbox(base, task_id=task_id) as sid:
            head_bench = run_benchmark(mgr, sid)  # the UNPATCHED head
        head_ratio = head_bench.p95_ms / base_bench.p95_ms if base_bench.p95_ms else float("inf")
        gates["regression_reproduced"] = (head_ratio - 1) * 100 > cfg.p95_tolerance_pct
        if not gates["regression_reproduced"]:
            issues.append(
                Issue(
                    type="baseline_not_faster",
                    detail=f"unpatched head p95 {head_bench.p95_ms} ms is not slower than baseline "
                    f"{base_bench.p95_ms} ms: the baseline is not a valid known-good",
                )
            )
            focus.append("re-check which commit is the last known-good version")
        with mgr.sandbox(base, task_id=task_id) as sid:
            try:
                mgr.apply_patch(sid, patch)
                gates["patch_applies"] = True
            except SandboxError as exc:
                gates["patch_applies"] = False
                issues.append(Issue(type="patch_does_not_apply", detail=exc.message[:300]))
                return
            tests = run_tests(mgr, sid)
            gates["tests_pass"] = tests.passed
            metrics["tests"] = tests.model_dump(exclude={"output_tail"})
            if not tests.passed:
                names = ", ".join(f.test for f in tests.failures[:5]) or tests.output_tail[-200:]
                failing = tests.failed + tests.errors
                issues.append(Issue(type="test_failure", detail=f"{failing} failing: {names}"))
            lint = mgr.exec(sid, ["python", "-m", "compileall", "-q", "."], timeout=60)
            gates["lint_ok"] = lint.exit_code == 0
            if lint.exit_code != 0:
                issues.append(Issue(type="lint_failure", detail=(lint.stdout + lint.stderr)[-300:]))
            cand_bench = run_benchmark(mgr, sid)
        ratio = cand_bench.p95_ms / base_bench.p95_ms if base_bench.p95_ms else float("inf")
        change = (ratio - 1) * 100
        gates["benchmark_not_regressed"] = change <= cfg.p95_tolerance_pct
        metrics["benchmark"] = {
            "baseline": base_bench.model_dump(),
            "candidate": cand_bench.model_dump(),
            "p95_change_pct": round(change, 1),
            "tolerance_pct": cfg.p95_tolerance_pct,
        }
        if not gates["benchmark_not_regressed"]:
            detail = (
                f"p95 {change:+.0f}% vs baseline ({cand_bench.p95_ms} ms vs {base_bench.p95_ms} ms)"
            )
            issues.append(Issue(type="perf_regression", detail=detail))
            focus.extend(_focus_for_perf(base_bench.db_queries, cand_bench.db_queries, files))

    try:
        await asyncio.to_thread(sandbox_checks)
    except SandboxError as exc:
        gates["sandbox_ok"] = False
        issues.append(Issue(type="verifier_error", detail=f"{exc.code}: {exc.message[:200]}"))

    passed = all(gates.values())
    return VerificationResult(
        status="passed" if passed else "failed", issues=issues, suggested_focus=focus,
        gates=gates, attempt=attempt, metrics=metrics,
    )  # fmt: skip


async def run_gates(
    mgr: SandboxManager,
    *,
    base: RepoRef,
    baseline: RepoRef,
    patch: str | None,
    cfg: GateConfig | None = None,
    attempt: int = 1,
    task_id: str = "",
) -> VerificationResult:
    with traced("verifier.gates", attributes={"aeos.verification.attempt": attempt}) as span:
        result = await _run_gates(
            mgr,
            base=base,
            baseline=baseline,
            patch=patch,
            cfg=cfg,
            attempt=attempt,
            task_id=task_id,
        )
        span.set("aeos.verification.status", result.status)
        span.set("aeos.verification.gates", result.gates)
        span.set("aeos.verification.issues", [f"{i.type}: {i.detail}" for i in result.issues])
        return result

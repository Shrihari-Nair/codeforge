"""Deterministic helpers: structured test and benchmark results (no LLM opinion)."""

import json
import xml.etree.ElementTree as ET  # noqa: S405 (we parse our own pytest junit output)

from pydantic import BaseModel

from aeos.sandbox.manager import ExecResult, RepoRef, SandboxError, SandboxManager


class TestFailure(BaseModel):
    __test__ = False
    test: str
    message: str


class TestResult(BaseModel):
    __test__ = False
    passed: bool
    exit_code: int
    total: int
    failed: int
    errors: int
    skipped: int
    failures: list[TestFailure]
    duration_s: float
    timed_out: bool
    output_tail: str


class BenchmarkResult(BaseModel):
    requests: int
    db_queries: int
    p50_ms: float
    p95_ms: float
    p99_ms: float


class Comparison(BaseModel):
    baseline: BenchmarkResult
    candidate: BenchmarkResult
    p95_ratio: float
    p95_change_pct: float
    regressed: bool
    tolerance_pct: float
    tests: TestResult | None = None


def _tail(r: ExecResult, n: int = 1500) -> str:
    return (r.stdout + ("\n" + r.stderr if r.stderr else ""))[-n:]


def run_tests(mgr: SandboxManager, sid: str, *, timeout: float = 120) -> TestResult:
    r = mgr.exec(
        sid,
        ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml=/tmp/junit.xml"],
        timeout=timeout,
    )
    total = failed = errors = skipped = 0
    failures: list[TestFailure] = []
    try:
        root = ET.fromstring(mgr.read_file(sid, "/tmp/junit.xml"))  # noqa: S314
        for suite in root.iter("testsuite"):
            total += int(suite.get("tests", 0))
            failed += int(suite.get("failures", 0))
            errors += int(suite.get("errors", 0))
            skipped += int(suite.get("skipped", 0))
        for case in root.iter("testcase"):
            for kind in ("failure", "error"):
                node = case.find(kind)
                if node is not None:
                    name = f"{case.get('classname', '')}::{case.get('name', '')}"
                    failures.append(
                        TestFailure(test=name, message=(node.get("message") or "")[:300])
                    )
    except (SandboxError, ET.ParseError):
        pass  # no junit (collection crash, timeout): exit code and output tail carry the story
    return TestResult(
        passed=r.exit_code == 0 and not r.timed_out,
        exit_code=r.exit_code,
        total=total,
        failed=failed,
        errors=errors,
        skipped=skipped,
        failures=failures,
        duration_s=r.duration_s,
        timed_out=r.timed_out,
        output_tail=_tail(r),
    )


def run_benchmark(mgr: SandboxManager, sid: str, *, timeout: float = 120) -> BenchmarkResult:
    r = mgr.exec(sid, ["python", "benchmark.py", "--json"], timeout=timeout)
    if r.exit_code != 0:
        raise SandboxError("benchmark_failed", _tail(r, 500))
    try:
        return BenchmarkResult(**json.loads(r.stdout.strip().splitlines()[-1]))
    except (ValueError, IndexError) as exc:
        raise SandboxError("benchmark_unparseable", _tail(r, 500)) from exc


def compare_benchmarks(
    mgr: SandboxManager,
    baseline: RepoRef,
    candidate: RepoRef,
    *,
    patch: str | None = None,
    tolerance_pct: float = 10.0,
    task_id: str = "",
    with_tests: bool = True,
) -> Comparison:
    """Benchmark `baseline` vs `candidate` (+ optional patch), each in its own sandbox."""
    with mgr.sandbox(baseline, task_id=task_id) as base_id:
        base = run_benchmark(mgr, base_id)
    with mgr.sandbox(candidate, task_id=task_id) as cand_id:
        if patch:
            mgr.apply_patch(cand_id, patch)
        cand = run_benchmark(mgr, cand_id)
        tests = run_tests(mgr, cand_id) if with_tests else None
    ratio = cand.p95_ms / base.p95_ms if base.p95_ms else float("inf")
    return Comparison(
        baseline=base,
        candidate=cand,
        p95_ratio=round(ratio, 3),
        p95_change_pct=round((ratio - 1) * 100, 1),
        regressed=ratio > 1 + tolerance_pct / 100,
        tolerance_pct=tolerance_pct,
        tests=tests,
    )

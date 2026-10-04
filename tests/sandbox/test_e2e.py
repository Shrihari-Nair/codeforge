"""End to end on the real demo repo: clone, test, benchmark, compare, all inside the sandbox."""

import json
from pathlib import Path

import pytest

from aeos.mcp_servers.github.backend import GitBackend
from aeos.sandbox import RepoRef, SandboxManager, compare_benchmarks, run_benchmark, run_tests

pytestmark = pytest.mark.docker

GT = json.loads((Path(__file__).parents[2] / "demo_target" / "GROUND_TRUTH.json").read_text())
BAD, GOOD = GT["root_cause"]["commit_sha"], GT["last_good_commit_sha"]


@pytest.fixture(scope="module")
def remote(remote_template: Path) -> str:
    return str(remote_template)


def test_clone_run_tests_and_benchmark_at_baseline(mgr: SandboxManager, remote: str) -> None:
    with mgr.sandbox(RepoRef(remote=remote, ref=GOOD)) as sid:
        tests = run_tests(mgr, sid)
        bench = run_benchmark(mgr, sid)
    assert tests.passed and tests.total == 3 and tests.failed == 0 and not tests.failures
    expected = GT["benchmark"]["baseline"]
    assert bench.model_dump() == {**expected, "requests": 500}  # deterministic, even in Docker


def test_regressed_head_has_same_tests_but_worse_benchmark(
    mgr: SandboxManager, remote: str
) -> None:
    with mgr.sandbox(RepoRef(remote=remote, ref="main")) as sid:
        tests = run_tests(mgr, sid)
        bench = run_benchmark(mgr, sid)
    assert tests.passed  # the regression is invisible to the unit tests
    assert bench.model_dump() == {**GT["benchmark"]["regressed"], "requests": 500}


def _fix_patch(remote: str) -> str:
    return GitBackend(Path(remote)).get_diff(BAD, GOOD, "checkout/catalog.py", 100_000)[3]


def test_compare_flags_the_regression(mgr: SandboxManager, remote: str) -> None:
    cmp = compare_benchmarks(
        mgr, RepoRef(remote=remote, ref=GOOD), RepoRef(remote=remote, ref="main"), with_tests=False
    )
    assert cmp.regressed and cmp.p95_ratio > 6


def test_compare_accepts_the_real_fix(mgr: SandboxManager, remote: str) -> None:
    cmp = compare_benchmarks(
        mgr,
        RepoRef(remote=remote, ref=GOOD),
        RepoRef(remote=remote, ref="main"),
        patch=_fix_patch(remote),
    )
    assert not cmp.regressed and cmp.p95_change_pct <= 10
    assert cmp.tests is not None and cmp.tests.passed


def test_compare_rejects_the_red_herring_fix(mgr: SandboxManager, remote: str) -> None:
    with mgr.sandbox(
        RepoRef(remote=remote, ref="main")
    ) as sid:  # author the wrong fix in a sandbox
        costs = mgr.read_file(sid, "checkout/costs.py")
        mgr.write_file(
            sid, "checkout/costs.py", costs.replace("AUDIT_SCAN_MS = 40.0", "AUDIT_SCAN_MS = 0.0")
        )
        wrong_patch = mgr.snapshot_diff(sid)
    cmp = compare_benchmarks(
        mgr, RepoRef(remote=remote, ref=GOOD), RepoRef(remote=remote, ref="main"), patch=wrong_patch
    )
    assert cmp.regressed  # the benchmark gate catches it
    assert cmp.tests is not None and cmp.tests.passed  # ...even though every test still passes
    assert cmp.candidate.p99_ms < 66.5  # the herring did get faster, just not what mattered


def test_failing_test_is_reported_structurally(mgr: SandboxManager) -> None:
    files = {"test_x.py": "def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n"}
    with mgr.sandbox(files=files) as sid:
        res = run_tests(mgr, sid)
    assert not res.passed and res.total == 2 and res.failed == 1
    assert res.failures[0].test.endswith("test_bad")


def test_hanging_test_times_out(mgr: SandboxManager) -> None:
    files = {"test_hang.py": "import time\n\ndef test_hang():\n    time.sleep(600)\n"}
    with mgr.sandbox(files=files) as sid:
        res = run_tests(mgr, sid, timeout=3)
    assert res.timed_out and not res.passed


def test_unrelated_tree_without_benchmark_fails_loudly(mgr: SandboxManager) -> None:
    from aeos.sandbox import SandboxError

    with mgr.sandbox(files={"a.py": "x = 1\n"}) as sid, pytest.raises(SandboxError) as ei:
        run_benchmark(mgr, sid)
    assert ei.value.code == "benchmark_failed"
